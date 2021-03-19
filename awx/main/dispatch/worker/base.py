# Copyright (c) 2018 Ansible by Red Hat
# All Rights Reserved.

import os
import logging
import signal
import sys
import redis
import json
import psycopg2
import time
from uuid import UUID
from queue import Empty as QueueEmpty

from django import db
from django.conf import settings

from awx.main.dispatch.pool import WorkerPool
from awx.main.dispatch import pg_bus_conn
from awx.main.queue import CallbackQueueDispatcher

if 'run_callback_receiver' in sys.argv:
    logger = logging.getLogger('awx.main.commands.run_callback_receiver')
else:
    logger = logging.getLogger('awx.main.dispatch')


def signame(sig):
    return dict(
        (k, v) for v, k in signal.__dict__.items()
        if v.startswith('SIG') and not v.startswith('SIG_')
    )[sig]


class WorkerSignalHandler:

    def __init__(self):
        self.kill_now = False
        signal.signal(signal.SIGTERM, signal.SIG_DFL)
        signal.signal(signal.SIGINT, self.exit_gracefully)

    def exit_gracefully(self, *args, **kwargs):
        self.kill_now = True


class AWXConsumerBase(object):

    last_stats = time.time()

    def __init__(self, name, worker, queues=[], pool=None):
        self.should_stop = False

        self.name = name
        self.total_messages = 0
        self.queues = queues
        self.worker = worker
        self.pool = pool
        if pool is None:
            self.pool = WorkerPool()
        self.pool.init_workers(self.worker.work_loop)
        self.redis = redis.Redis.from_url(settings.BROKER_URL)

    @property
    def listening_on(self):
        return f'listening on {self.queues}'

    def control(self, body):
        logger.warn(body)
        control = body.get('control')
        if control in ('status', 'running'):
            reply_queue = body['reply_to']
            if control == 'status':
                msg = '\n'.join([self.listening_on, self.pool.debug()])
            elif control == 'running':
                msg = []
                for worker in self.pool.workers:
                    worker.calculate_managed_tasks()
                    msg.extend(worker.managed_tasks.keys())

            with pg_bus_conn() as conn:
                conn.notify(reply_queue, json.dumps(msg))
        elif control == 'reload':
            for worker in self.pool.workers:
                worker.quit()
        else:
            logger.error('unrecognized control message: {}'.format(control))

    def process_task(self, body):
        if 'control' in body:
            try:
                return self.control(body)
            except Exception:
                logger.exception(f"Exception handling control message: {body}")
                return
        if len(self.pool):
            if "uuid" in body and body['uuid']:
                try:
                    queue = UUID(body['uuid']).int % len(self.pool)
                except Exception:
                    queue = self.total_messages % len(self.pool)
            else:
                queue = self.total_messages % len(self.pool)
        else:
            queue = 0
        self.pool.write(queue, body)
        self.total_messages += 1
        self.record_statistics()

    def record_statistics(self):
        if time.time() - self.last_stats > 1:  # buffer stat recording to once per second
            try:
                self.redis.set(f'awx_{self.name}_statistics', self.pool.debug())
                self.last_stats = time.time()
            except Exception:
                logger.exception(f"encountered an error communicating with redis to store {self.name} statistics")
                self.last_stats = time.time()

    def run(self, *args, **kwargs):
        signal.signal(signal.SIGINT, self.stop)
        signal.signal(signal.SIGTERM, self.stop)

        # Child should implement other things here

    def stop(self, signum, frame):
        self.should_stop = True
        logger.warn('received {}, stopping'.format(signame(signum)))
        self.worker.on_stop()
        raise SystemExit()


class AWXConsumerRedis(AWXConsumerBase):
    def run(self, *args, **kwargs):
        super(AWXConsumerRedis, self).run(*args, **kwargs)
        self.worker.on_start()

        from awx.main.models import UnifiedJob
        self.dispatcher = CallbackQueueDispatcher()

        while True:
            logger.debug(f'{os.getpid()} is alive')
            with self.dispatcher.pipeline() as pipe_results:
                self.dispatcher.get_job_events_total()
                self.dispatcher.get_job_events_processed()

            events_total = self.dispatcher._redis_dict_to_int_int(pipe_results.pop(0))
            events_processed = self.dispatcher._redis_dict_to_int_int(pipe_results.pop(0))

            finished = []
            for job_id, total in events_total.items():
                count = events_processed.get(job_id, 0)
                if count == total:
                    finished.append(job_id)

            if len(finished) > 0:
                # TODO: Need to check job status before finishing the job
                # This sort of polling shouldn't be that expensive if we consider _when_ it likely happens. It likely happens that job event processing lags
                # a job finishing, especially when the system is under load. If this is the case, the Job status will only need to be looked up once.
                # We only wastefully lookup job status' when event processing finishes _before_ the job. This can happen for small jobs.
                with self.dispatcher.pipeline() as pipe_results:
                    [self.dispatcher.get_job_extra_data(job_id) for job_id in finished]
                    self.dispatcher.delete_in_flight_jobs(finished)
                    self.dispatcher.set_job_events_complete(finished)

                extra_data = pipe_results[0:len(finished)]

                for job_id in finished:
                    guid = json.loads(extra_data.pop(0))['guid']
                    uj = UnifiedJob.objects.get(id=job_id)
                    uj.do_finish_job(events_total[job_id], guid)

            time.sleep(1)


class AWXConsumerPG(AWXConsumerBase):
    def run(self, *args, **kwargs):
        super(AWXConsumerPG, self).run(*args, **kwargs)

        logger.warn(f"Running worker {self.name} listening to queues {self.queues}")
        init = False

        while True:
            try:
                with pg_bus_conn() as conn:
                    for queue in self.queues:
                        conn.listen(queue)
                    if init is False:
                        self.worker.on_start()
                        init = True
                    for e in conn.events():
                        self.process_task(json.loads(e.payload))
                    if self.should_stop:
                        return
            except psycopg2.InterfaceError:
                logger.warn("Stale Postgres message bus connection, reconnecting")
                continue


class BaseWorker(object):

    def read(self, queue):
        return queue.get(block=True, timeout=1)

    def work_loop(self, queue, finished, idx, *args):
        ppid = os.getppid()
        signal_handler = WorkerSignalHandler()
        while not signal_handler.kill_now:
            # if the parent PID changes, this process has been orphaned
            # via e.g., segfault or sigkill, we should exit too
            if os.getppid() != ppid:
                break
            try:
                body = self.read(queue)
                if body == 'QUIT':
                    break
            except QueueEmpty:
                continue
            except Exception as e:
                raise e
                logger.error("Exception on worker {}, restarting: ".format(idx) + str(e))
                continue
            try:
                for conn in db.connections.all():
                    # If the database connection has a hiccup during the prior message, close it
                    # so we can establish a new connection
                    conn.close_if_unusable_or_obsolete()
                    self.perform_work(body, *args)
            finally:
                if 'uuid' in body:
                    uuid = body['uuid']
                    finished.put(uuid)
        logger.warn('worker exiting gracefully pid:{}'.format(os.getpid()))

    def perform_work(self, body):
        raise NotImplementedError()

    def on_start(self):
        pass

    def on_stop(self):
        pass
