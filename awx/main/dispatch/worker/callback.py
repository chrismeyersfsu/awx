import json
import logging
import os
import signal
import time
import datetime

from django.conf import settings
from django.utils.functional import cached_property
from django.utils.timezone import now as tz_now
from django.db import transaction, connection as django_connection
from awx.main.tasks.callback import RunnerCallback
from django_guid import set_guid

import psutil

import redis

from awx.main.consumers import emit_channel_notification
from awx.main.models import JobEvent, AdHocCommandEvent, ProjectUpdateEvent, InventoryUpdateEvent, SystemJobEvent, UnifiedJob
from awx.main.constants import ACTIVE_STATES, JOB_FOLDER_PREFIX
from awx.main.models.events import emit_event_detail
from awx.main.utils.profiling import AWXProfiler
import awx.main.analytics.subsystem_metrics as s_metrics
from .base import BaseWorker

logger = logging.getLogger('awx.main.commands.run_callback_receiver')


def job_stats_wrapup(job_identifier, event=None):
    """Fill in the unified job host_status_counts, fire off notifications if needed"""
    try:
        # empty dict (versus default of None) can still indicate that events have been processed
        # for job types like system jobs, and jobs with no hosts matched
        host_status_counts = {}
        if event:
            host_status_counts = event.get_host_status_counts()

        # Update host_status_counts while holding the row lock
        with transaction.atomic():
            uj = UnifiedJob.objects.select_for_update().get(pk=job_identifier)
            uj.host_status_counts = host_status_counts
            uj.save(update_fields=['host_status_counts'])

        uj.log_lifecycle("stats_wrapup_finished")

        # If the status was a finished state before this update was made, send notifications
        # If not, we will send notifications when the status changes
        if uj.status not in ACTIVE_STATES:
            uj.send_notification_templates('succeeded' if uj.status == 'successful' else 'failed')

    except Exception:
        logger.exception('Worker failed to save stats or emit notifications: Job {}'.format(job_identifier))


class FakeConfig:
    def __init__(self, settings):
        self.settings = settings
        self.command = None
        self.cwd = None
        self.env = None


class CallbackBrokerWorker(BaseWorker):
    """
    A worker implementation that deserializes callback event data and persists
    it into the database.

    The code that *generates* these types of messages is found in the
    ansible-runner display callback plugin.
    """

    MAX_RETRIES = 2
    INDIVIDUAL_EVENT_RETRIES = 3
    last_stats = time.time()
    last_flush = time.time()
    total = 0
    last_event = ''
    prof = None

    def __init__(self):
        self.buff = {}
        self.redis = redis.Redis.from_url(settings.BROKER_URL)
        self.subsystem_metrics = s_metrics.Metrics(auto_pipe_execute=False)
        self.queue_pop = 0
        self.queue_name = settings.CALLBACK_QUEUE
        self.prof = AWXProfiler("CallbackBrokerWorker")
        for key in self.redis.keys('awx_callback_receiver_statistics_*'):
            self.redis.delete(key)

    @cached_property
    def pid(self):
        """This needs to be obtained after forking, or else it will give the parent process"""
        return os.getpid()

    def old_read(self, queue):
        try:
            res = self.redis.blpop(self.queue_name, timeout=1)
            if res is None:
                return {'event': 'FLUSH'}
            self.total += 1
            self.queue_pop += 1
            self.subsystem_metrics.inc('callback_receiver_events_popped_redis', 1)
            self.subsystem_metrics.inc('callback_receiver_events_in_memory', 1)
            return json.loads(res[1])
        except redis.exceptions.RedisError:
            logger.exception("encountered an error communicating with redis")
            time.sleep(1)
        except (json.JSONDecodeError, KeyError):
            logger.exception("failed to decode JSON message from redis")
        finally:
            self.record_statistics()
            self.record_read_metrics()

        return {'event': 'FLUSH'}

    def read(self, queue):
        from glob import glob
        from ansible_runner.loader import ArtifactLoader
        from ansible_runner.utils.streaming import unstream_dir

        candidate_job_private_dirs = glob(f'{settings.AWX_ISOLATION_BASE_PATH}/{JOB_FOLDER_PREFIX}*/')

        for private_dir in candidate_job_private_dirs:
            self._loader = ArtifactLoader(private_dir)
            # TODO: might need to crawl subfolders of artifacts. The subfolders will be db job primary keys
            project_artifacts = os.path.abspath(os.path.join(private_dir, 'artifacts'))
            job_events_path = os.path.join(self.artifact_dir, 'job_events')

            rcb = RunnerCallback()
            fake_config = FakeConfig()

            while True:
                try:
                    line = self._input.readline()
                    data = json.loads(line)
                except (json.decoder.JSONDecodeError, IOError) as exc:
                    self.status_handler(
                        {
                            'status': 'error',
                            'job_explanation': (f'Failed to JSON parse a line from worker stream. Error: {exc} Line with invalid JSON data: {line[:1000]}'),
                        },
                        fake_config,
                    )
                    break

                if 'status' in data:
                    rcb.status_handler(data, fake_config)
                elif 'zipfile' in data:
                    length = data['zipfile']
                    unstream_dir(self._input, length, self.artifact_dir)
                    rcb.artifacts_handler(None)
                elif 'eof' in data:
                    break
                elif data.get('event') == 'keepalive':
                    # just ignore keepalives
                    continue
                else:
                    rcb.event_handler(data)

            rcb.finished_callback(None)

        return self.old_read(queue)

        # return self.status, self.rc

        #     try:
        #         signal_state.raise_exception = True
        #         # address race condition where SIGTERM was issued after this dispatcher task started
        #         if signal_callback():
        #             raise SignalExit()
        #         res = processor_future.result()
        #     except SignalExit:
        #         receptor_ctl.simple_command(f"work cancel {self.unit_id}")
        #         resultsock.shutdown(socket.SHUT_RDWR)
        #         resultfile.close()
        #         result = namedtuple('result', ['status', 'rc'])
        #         res = result('canceled', 1)
        #     finally:
        #         signal_state.raise_exception = False

        #     if res.status == 'error':
        #         # If ansible-runner ran, but an error occured at runtime, the traceback information
        #         # is saved via the status_handler passed in to the processor.
        #         if 'result_traceback' in self.task.runner_callback.extra_update_fields:
        #             return res

        #         try:
        #             unit_status = receptor_ctl.simple_command(f'work status {self.unit_id}')
        #             detail = unit_status.get('Detail', None)
        #             state_name = unit_status.get('StateName', None)
        #             stdout_size = unit_status.get('StdoutSize', 0)
        #         except Exception:
        #             detail = ''
        #             state_name = ''
        #             stdout_size = 0
        #             logger.exception(f'An error was encountered while getting status for work unit {self.unit_id}')

        #         if 'exceeded quota' in detail:
        #             logger.warning(detail)
        #             log_name = self.task.instance.log_format
        #             logger.warning(f"Could not launch pod for {log_name}. Exceeded quota.")
        #             self.task.update_model(self.task.instance.pk, status='pending')
        #             return

        #         try:
        #             receptor_output = ''
        #             if state_name == 'Failed' and self.task.runner_callback.event_ct == 0:
        #                 # if receptor work unit failed and no events were emitted, work results may
        #                 # contain useful information about why the job failed. In case stdout is
        #                 # massive, only ask for last 1000 bytes
        #                 startpos = max(stdout_size - 1000, 0)
        #                 resultsock, resultfile = receptor_ctl.get_work_results(self.unit_id, startpos=startpos, return_socket=True, return_sockfile=True)
        #                 lines = resultfile.readlines()
        #                 receptor_output = b"".join(lines).decode()
        #             if receptor_output:
        #                 self.task.runner_callback.delay_update(result_traceback=f'Worker output:\n{receptor_output}')
        #             elif detail:
        #                 self.task.runner_callback.delay_update(result_traceback=f'Receptor detail:\n{detail}')
        #             else:
        #                 logger.warning(f'No result details or output from {self.task.instance.log_format}, status:\n{state_name}')
        #         except Exception:
        #             logger.exception(f'Work results error from job id={self.task.instance.id} work_unit={self.task.instance.work_unit_id}')
        #             raise RuntimeError(detail)

        # return res

    def record_read_metrics(self):
        if self.queue_pop == 0:
            return
        if self.subsystem_metrics.should_pipe_execute() is True:
            queue_size = self.redis.llen(self.queue_name)
            self.subsystem_metrics.set('callback_receiver_events_queue_size_redis', queue_size)
            self.subsystem_metrics.pipe_execute()
            self.queue_pop = 0

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
        return '{:0.3f}'.format(psutil.Process(self.pid).memory_info().rss / 1024.0 / 1024.0)

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
        if force or (time.time() - self.last_flush) > settings.JOB_EVENT_BUFFER_SECONDS or any([len(events) >= 1000 for events in self.buff.values()]):
            metrics_bulk_events_saved = 0
            metrics_singular_events_saved = 0
            metrics_events_batch_save_errors = 0
            metrics_events_broadcast = 0
            metrics_events_missing_created = 0
            metrics_total_job_event_processing_seconds = datetime.timedelta(seconds=0)
            for cls, events in self.buff.items():
                if not events:
                    continue
                logger.debug(f'{cls.__name__}.objects.bulk_create({len(events)})')
                for e in events:
                    e.modified = now  # this can be set before created because now is set above on line 149
                    if not e.created:
                        e.created = now
                        metrics_events_missing_created += 1
                    else:  # only calculate the seconds if the created time already has been set
                        metrics_total_job_event_processing_seconds += e.modified - e.created
                metrics_duration_to_save = time.perf_counter()
                saved_events = []
                try:
                    cls.objects.bulk_create(events)
                    metrics_bulk_events_saved += len(events)
                    saved_events = events
                    self.buff[cls] = []
                except Exception as exc:
                    # If the database is flaking, let ensure_connection throw a general exception
                    # will be caught by the outer loop, which goes into a proper sleep and retry loop
                    django_connection.ensure_connection()
                    logger.warning(f'Error in events bulk_create, will try indiviually, error: {str(exc)}')
                    # if an exception occurs, we should re-attempt to save the
                    # events one-by-one, because something in the list is
                    # broken/stale
                    metrics_events_batch_save_errors += 1
                    for e in events.copy():
                        try:
                            e.save()
                            metrics_singular_events_saved += 1
                            events.remove(e)
                            saved_events.append(e)  # Importantly, remove successfully saved events from the buffer
                        except Exception as exc_indv:
                            retry_count = getattr(e, '_retry_count', 0) + 1
                            e._retry_count = retry_count

                            # special sanitization logic for postgres treatment of NUL 0x00 char
                            # This used to check the class of the exception but on the postgres3 upgrade it could appear
                            #   as either DataError or ValueError, so now lets just try if its there.
                            if (retry_count == 1) and ("\x00" in e.stdout):
                                e.stdout = e.stdout.replace("\x00", "")

                            if retry_count >= self.INDIVIDUAL_EVENT_RETRIES:
                                logger.error(f'Hit max retries ({retry_count}) saving individual Event error: {str(exc_indv)}\ndata:\n{e.__dict__}')
                                events.remove(e)
                            else:
                                logger.info(f'Database Error Saving individual Event uuid={e.uuid} try={retry_count}, error: {str(exc_indv)}')

                metrics_duration_to_save = time.perf_counter() - metrics_duration_to_save
                for e in saved_events:
                    if not getattr(e, '_skip_websocket_message', False):
                        metrics_events_broadcast += 1
                        emit_event_detail(e)
                    if getattr(e, '_notification_trigger_event', False):
                        job_stats_wrapup(getattr(e, e.JOB_REFERENCE), event=e)
            self.last_flush = time.time()
            # only update metrics if we saved events
            if (metrics_bulk_events_saved + metrics_singular_events_saved) > 0:
                self.subsystem_metrics.inc('callback_receiver_batch_events_errors', metrics_events_batch_save_errors)
                self.subsystem_metrics.inc('callback_receiver_events_insert_db_seconds', metrics_duration_to_save)
                self.subsystem_metrics.inc('callback_receiver_events_insert_db', metrics_bulk_events_saved + metrics_singular_events_saved)
                self.subsystem_metrics.observe('callback_receiver_batch_events_insert_db', metrics_bulk_events_saved)
                self.subsystem_metrics.inc('callback_receiver_events_in_memory', -(metrics_bulk_events_saved + metrics_singular_events_saved))
                self.subsystem_metrics.inc('callback_receiver_events_broadcast', metrics_events_broadcast)
                self.subsystem_metrics.set(
                    'callback_receiver_event_processing_avg_seconds',
                    metrics_total_job_event_processing_seconds.total_seconds()
                    / (metrics_bulk_events_saved + metrics_singular_events_saved - metrics_events_missing_created),
                )
            if self.subsystem_metrics.should_pipe_execute() is True:
                self.subsystem_metrics.pipe_execute()

    def perform_work(self, body):
        try:
            flush = body.get('event') == 'FLUSH'
            if flush:
                self.last_event = ''
            if not flush:
                job_identifier = 'unknown job'
                for cls in (JobEvent, AdHocCommandEvent, ProjectUpdateEvent, InventoryUpdateEvent, SystemJobEvent):
                    if cls.JOB_REFERENCE in body:
                        job_identifier = body[cls.JOB_REFERENCE]
                        break

                self.last_event = f'\n\t- {cls.__name__} for #{job_identifier} ({body.get("event", "")} {body.get("uuid", "")})'  # noqa

                notification_trigger_event = bool(body.get('event') == cls.WRAPUP_EVENT)

                if body.get('event') == 'EOF':
                    try:
                        if 'guid' in body:
                            set_guid(body['guid'])
                        final_counter = body.get('final_counter', 0)
                        logger.info('Starting EOF event processing for Job {}'.format(job_identifier))
                        # EOF events are sent when stdout for the running task is
                        # closed. don't actually persist them to the database; we
                        # just use them to report `summary` websocket events as an
                        # approximation for when a job is "done"
                        emit_channel_notification('jobs-summary', dict(group_name='jobs', unified_job_id=job_identifier, final_counter=final_counter))

                        if notification_trigger_event:
                            job_stats_wrapup(job_identifier)
                    except Exception:
                        logger.exception('Worker failed to perform EOF tasks: Job {}'.format(job_identifier))
                    finally:
                        self.subsystem_metrics.inc('callback_receiver_events_in_memory', -1)
                        set_guid('')
                    return

                skip_websocket_message = body.pop('skip_websocket_message', False)

                event = cls.create_from_data(**body)

                if skip_websocket_message:  # if this event sends websocket messages, fire them off on flush
                    event._skip_websocket_message = True

                if notification_trigger_event:  # if this is an Ansible stats event, ensure notifications on flush
                    event._notification_trigger_event = True

                self.buff.setdefault(cls, []).append(event)

            retries = 0
            while retries <= self.MAX_RETRIES:
                try:
                    self.flush(force=flush)
                    break
                except Exception as exc:
                    # Aside form bugs, exceptions here are assumed to be due to database flake
                    if retries >= self.MAX_RETRIES:
                        logger.exception('Worker could not re-establish database connectivity, giving up on one or more events.')
                        self.buff = {}
                        return
                    delay = 60 * retries
                    logger.warning(f'Database Error Flushing Job Events, retry #{retries + 1} in {delay} seconds: {str(exc)}')
                    django_connection.close()
                    time.sleep(delay)
                    retries += 1
        except Exception:
            logger.exception(f'Callback Task Processor Raised Unexpected Exception processing event data:\n{body}')
