import json
import logging
import os
import signal
import time
import traceback

from django.conf import settings
from django.utils.timezone import now as tz_now
from django.db import DatabaseError, OperationalError, connection as django_connection
from django.db.utils import InterfaceError, InternalError, IntegrityError
from django_guid.middleware import GuidMiddleware

import psutil

import redis

from awx.main.consumers import emit_channel_notification
from awx.main.models import (JobEvent, AdHocCommandEvent, ProjectUpdateEvent,
                             InventoryUpdateEvent, SystemJobEvent, UnifiedJob,
                             Job)
from awx.main.tasks import handle_success_and_failure_notifications
from awx.main.models.events import emit_event_detail, get_event_job_relationship_name
from awx.main.utils.profiling import AWXProfiler
from awx.main.queue import CallbackQueueDispatcher

from .base import BaseWorker

logger = logging.getLogger('awx.main.commands.run_callback_receiver')


class CallbackBrokerWorker(BaseWorker):
    '''
    A worker implementation that deserializes callback event data and persists
    it into the database.

    The code that *generates* these types of messages is found in the
    ansible-runner display callback plugin.
    '''

    MAX_RETRIES = 2
    last_stats = time.time()
    last_flush = time.time()
    total = 0
    last_event = ''
    prof = None

    def __init__(self):
        self.buff = {}
        self.events_from_redis = []
        self.events_duplicate = set()
        self.events_saved = set()

        self.pid = os.getpid()
        self.dispatcher = CallbackQueueDispatcher()
        self.redis = redis.Redis.from_url(settings.BROKER_URL)
        self.prof = AWXProfiler("CallbackBrokerWorker")
        for key in self.redis.keys('awx_callback_receiver_statistics_*'):
            self.redis.delete(key)

        # Recover on-start
        # TODO: This may be racy. If other workers can start before the cleanup has finished
        # Ideally, use the parent to coordinate somehow
        self._on_start()

    def _on_start(self):
        total = self.redis.llen(settings.CALLBACK_PROCESSING_QUEUE)
        if total > 0:
            logger.warn(f"Recovering {total} events from {settings.CALLBACK_PROCESSING_QUEUE} redis queue")
        while True:
            res = self.redis.rpoplpush(settings.CALLBACK_PROCESSING_QUEUE, settings.CALLBACK_QUEUE)
            if res is None:
                break

    def read(self, queue):
        try:
            res = self.redis.brpoplpush(settings.CALLBACK_QUEUE, settings.CALLBACK_PROCESSING_QUEUE, timeout=1)
            if res is None:
                return {'event': 'FLUSH'}
            self.total += 1
            return json.loads(res)
        except redis.exceptions.RedisError:
            logger.exception("encountered an error communicating with redis")
            time.sleep(1)
        except (json.JSONDecodeError, KeyError):
            logger.exception("failed to decode JSON message from redis")
        finally:
            self.record_statistics()
        return {'event': 'FLUSH'}

    def record_statistics(self):
        # buffer stat recording to once per (by default) 5s
        if time.time() - self.last_stats > settings.JOB_EVENT_STATISTICS_INTERVAL:
            try:
                self.redis.set(f'awx_callback_receiver_statistics_{self.pid}', self.debug())
                self.last_stats = time.time()
            except Exception:
                logger.exception("encountered an error communicating with redis")
                self.last_stats = time.time()

    def debug(self):
        return f'.  worker[pid:{self.pid}] sent={self.total} rss={self.mb}MB {self.last_event}'

    @property
    def mb(self):
        return '{:0.3f}'.format(
            psutil.Process(self.pid).memory_info().rss / 1024.0 / 1024.0
        )

    def toggle_profiling(self, *args):
        if not self.prof.is_started():
            self.prof.start()
            logger.error('profiling is enabled')
        else:
            filepath = self.prof.stop()
            logger.error(f'profiling is disabled, wrote {filepath}')

    def work_loop(self, *args, **kw):
        if settings.AWX_CALLBACK_PROFILE:
            signal.signal(signal.SIGUSR1, self.toggle_profiling)
        return super(CallbackBrokerWorker, self).work_loop(*args, **kw)

    def flush(self, force=False):
        now = tz_now()
        if (
            force or
            (time.time() - self.last_flush) > settings.JOB_EVENT_BUFFER_SECONDS or
            any([len(events) >= 1000 for events in self.buff.values()])
        ):
            for cls, events in self.buff.items():
                duplicate_events = 0
                # key: job_identifier
                # value: number of events processed in this flush
                job_events_processed = {}
                logger.debug(f'{cls.__name__}.objects.bulk_create({len(events)})')
                for e in events:
                    if not e.created:
                        e.created = now
                    e.modified = now

                    job_identifier = getattr(e, get_event_job_relationship_name(e))
                    job_events_processed[job_identifier] = job_events_processed.get(job_identifier, 0) + 1
                try:
                    raise Exception
                    cls.objects.bulk_create(events)
                    self.events_saved = set([e.uuid for e in events])
                except Exception:
                    # if an exception occurs, we should re-attempt to save the
                    # events one-by-one, because something in the list is
                    # broken/stale
                    for e in events:
                        try:
                            e.save()
                            self.events_saved.add(e.uuid)
                        except IntegrityError:
                            self.events_duplicate.add(e.uuid)
                        except Exception:
                            job_identifier = getattr(e, get_event_job_relationship_name(e))
                            job_events_processed[job_identifier] -= 1
                            logger.exception('Database Error Saving Job Event')

                for e in events:
                    emit_event_detail(e)

                """
                The below code is to solve keeping two data-sources in sync, the postgres database and redis.
                Postgres stores the events, redis stores the count of events for efficiency. We have a sort of two phase commit. First, we "checkout" an
                event from the redis event queue by moving it from one queue to another. Next, we save the event to the database, finally we remove the
                event from the second redis queue to complete the transaction.

                Possible Failures:
                1 Crash after checkout but before writing event to postgres
                  * Event will be picked up on callback reciever restart
                2 Crash after event inserted into Postgres but before per-event processed count updated
                  * Event will be processed a second time upon callback receiver restart. Per-job event processed count will, correctly, be increased by 1.

                """

                events_to_commit = [e for e in self.events_from_redis if e['uuid'] in self.events_saved.union(self.events_duplicate)]

                with self.dispatcher.pipeline() as pipe_results:
                    [self.dispatcher.incr_job_events_processed(k, v) for k, v in job_events_processed.items()]
                    [self.dispatcher.commit_event(e) for e in events_to_commit]

            self.buff = {}
            self.events_from_redis = []
            self.events_saved = set()
            self.events_duplicate = set()
            self.last_flush = time.time()

    def perform_work(self, body):
        try:
            flush = body.get('event') == 'FLUSH'
            if flush:
                self.last_event = ''
            if not flush:
                event_map = {
                    'job_id': JobEvent,
                    'ad_hoc_command_id': AdHocCommandEvent,
                    'project_update_id': ProjectUpdateEvent,
                    'inventory_update_id': InventoryUpdateEvent,
                    'system_job_id': SystemJobEvent,
                }

                job_identifier = 'unknown job'
                for key, cls in event_map.items():
                    if key in body:
                        job_identifier = body[key]
                        break

                self.last_event = f'\n\t- {cls.__name__} for #{job_identifier} ({body.get("event", "")} {body.get("uuid", "")})'  # noqa

                try:
                    event = cls.create_from_data(**body)
                    self.buff.setdefault(cls, []).append(event)
                except IntegrityError:
                    self.events_duplicate.add(body['uuid'])

                self.events_from_redis.append(body)

            retries = 0
            while retries <= self.MAX_RETRIES:
                try:
                    self.flush(force=flush)
                    break
                except (OperationalError, InterfaceError, InternalError):
                    if retries >= self.MAX_RETRIES:
                        logger.exception('Worker could not re-establish database connectivity, giving up on one or more events.')
                        return
                    delay = 60 * retries
                    logger.exception('Database Error Saving Job Event, retry #{i} in {delay} seconds:'.format(
                        i=retries + 1,
                        delay=delay
                    ))
                    django_connection.close()
                    time.sleep(delay)
                    retries += 1
                except DatabaseError:
                    logger.exception('Database Error Saving Job Event')
                    break
        except Exception as exc:
            tb = traceback.format_exc()
            logger.error('Callback Task Processor Raised Exception: %r', exc)
            logger.error('Detail: {}'.format(tb))
