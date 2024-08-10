# Copyright (c) 2017 Ansible by Red Hat
# All Rights Reserved.

# Python
import base64
import json
import logging
import sys
import traceback
import os
from datetime import datetime
import typing
from enum import Enum

# Django
from django.conf import settings
from django.utils.timezone import now
from django.utils.encoding import force_str

# AWX
from awx.main.exceptions import PostRunError

# OTEL
from awx.main.utils.formatters import JSONNLFormatter
from opentelemetry._logs import set_logger_provider, get_logger_provider
from opentelemetry.exporter.otlp.proto.grpc._log_exporter import OTLPLogExporter as OTLPGrpcLogExporter
from opentelemetry.exporter.otlp.proto.http._log_exporter import OTLPLogExporter as OTLPHttpLogExporter

from opentelemetry.exporter.otlp.proto.common._log_encoder import encode_logs

from opentelemetry.proto.logs.v1.logs_pb2 import (
    ResourceLogs,
)

from opentelemetry.sdk._logs import LoggerProvider, LoggingHandler, LogData
from opentelemetry.sdk._logs.export import BatchLogRecordProcessor, LogExporter, LogExportResult, SimpleLogRecordProcessor
from opentelemetry.sdk.resources import Resource


class RSysLogHandler(logging.handlers.SysLogHandler):
    append_nul = False

    def _connect_unixsocket(self, address):
        super(RSysLogHandler, self)._connect_unixsocket(address)
        self.socket.setblocking(False)

    def handleError(self, record):
        # for any number of reasons, rsyslogd has gone to lunch;
        # this usually means that it's just been restarted (due to
        # a configuration change) unfortunately, we can't log that
        # because...rsyslogd is down (and would just put us back down this
        # code path)
        # as a fallback, it makes the most sense to just write the
        # messages to sys.stderr (which will end up in supervisord logs,
        # and in containerized installs, cascaded down to pod logs)
        # because the alternative is blocking the
        # socket.send() in the Python process, which we definitely don't
        # want to do)
        dt = datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')
        msg = f'{dt} ERROR rsyslogd was unresponsive: '
        exc = traceback.format_exc()
        try:
            msg += exc.splitlines()[-1]
        except Exception:
            msg += exc
        msg = '\n'.join([msg, force_str(record.msg), ''])  # force_str used in case of translated strings
        sys.stderr.write(msg)

    def emit(self, msg):
        if not settings.LOG_AGGREGATOR_ENABLED:
            return
        return super(RSysLogHandler, self).emit(msg)


class SpecialInventoryHandler(logging.Handler):
    """Logging handler used for the saving-to-database part of inventory updates
    ran by the task system
    this dispatches events directly to be processed by the callback receiver,
    as opposed to ansible-runner
    """

    def __init__(self, event_handler, cancel_callback, job_timeout, verbosity, start_time=None, counter=0, initial_line=0, **kwargs):
        self.event_handler = event_handler
        self.cancel_callback = cancel_callback
        self.job_timeout = job_timeout
        if start_time is None:
            self.job_start = now()
        else:
            self.job_start = start_time
        self.last_check = self.job_start
        self.counter = counter
        self.skip_level = [logging.WARNING, logging.INFO, logging.DEBUG, 0][verbosity]
        self._current_line = initial_line
        super(SpecialInventoryHandler, self).__init__(**kwargs)

    def emit(self, record):
        # check cancel and timeout status regardless of log level
        this_time = now()
        if (this_time - self.last_check).total_seconds() > 0.1:
            self.last_check = this_time
            if self.cancel_callback():
                raise PostRunError('Inventory update has been canceled', status='canceled')
        if self.job_timeout and ((this_time - self.job_start).total_seconds() > self.job_timeout):
            raise PostRunError('Inventory update has timed out', status='canceled')

        # skip logging for low severity logs
        if record.levelno < self.skip_level:
            return

        self.counter += 1
        msg = self.format(record)
        n_lines = len(msg.strip().split('\n'))  # don't count line breaks at boundry of text
        dispatch_data = dict(
            created=now().isoformat(), event='verbose', counter=self.counter, stdout=msg, start_line=self._current_line, end_line=self._current_line + n_lines
        )
        self._current_line += n_lines

        self.event_handler(dispatch_data)


if settings.COLOR_LOGS is True:
    try:
        from logutils.colorize import ColorizingStreamHandler
        import colorama

        colorama.deinit()
        colorama.init(wrap=False, convert=False, strip=False)

        class ColorHandler(ColorizingStreamHandler):
            def colorize(self, line, record):
                # comment out this method if you don't like the job_lifecycle
                # logs rendered with cyan text
                previous_level_map = self.level_map.copy()
                if record.name == "awx.analytics.job_lifecycle":
                    self.level_map[logging.INFO] = (None, 'cyan', True)
                msg = super(ColorHandler, self).colorize(line, record)
                self.level_map = previous_level_map
                return msg

            def format(self, record):
                message = logging.StreamHandler.format(self, record)
                return '\n'.join([self.colorize(line, record) for line in message.splitlines()])

            level_map = {
                logging.DEBUG: (None, 'green', True),
                logging.INFO: (None, None, True),
                logging.WARNING: (None, 'yellow', True),
                logging.ERROR: (None, 'red', True),
                logging.CRITICAL: (None, 'red', True),
            }

    except ImportError:
        # logutils is only used for colored logs in the dev environment
        pass
else:
    ColorHandler = logging.StreamHandler


class BaseOTLPHandler(LoggingHandler):
    def __init__(self, service_name=None, instance_id=None):
        self.service_name = service_name or self.generate_service_name()
        self.instance_id = instance_id or os.uname().nodename

        logger_provider = LoggerProvider(
            resource=Resource.create(
                {
                    "service.name": self.service_name,
                    "service.instance.id": self.instance_id,
                }
            ),
        )
        set_logger_provider(logger_provider)

        # trace_provider = TracerProvider()

        super().__init__(level=logging.NOTSET, logger_provider=logger_provider)

    def get_service_name(self):
        self.service_name

    def generate_service_name(self):
        return sys.argv[1] if len(sys.argv) > 1 else (sys.argv[0] or 'unknown_service')

    def emit(self, record: logging.LogRecord) -> None:
        # Calls provider exporters.export()
        return super().emit(record)


class OTLPHandler(BaseOTLPHandler):
    def __init__(self, endpoint=None, protocol='grpc', service_name=None, instance_id=None, auth=None, username=None, password=None):
        super(BaseOTLPHandler, self).__init__(service_name=None, instance_id=None)
        if not endpoint:
            raise ValueError("endpoint required")

        if auth == 'basic' and (username is None or password is None):
            raise ValueError("auth type basic requires username and passsword parameters")

        self.endpoint = endpoint

        headers = {}
        if auth == 'basic':
            secret = f'{username}:{password}'
            headers['Authorization'] = "Basic " + base64.b64encode(secret.encode()).decode()

        if protocol == 'grpc':
            otlp_exporter = OTLPGrpcLogExporter(endpoint=self.endpoint, insecure=True, headers=headers)
        elif protocol == 'http':
            otlp_exporter = OTLPHttpLogExporter(endpoint=self.endpoint, headers=headers)

        get_logger_provider().add_log_record_processor(BatchLogRecordProcessor(otlp_exporter))


class ExporterHandlerAdapter(LogExporter):
    # python handler
    #   opentelemetry processor
    #     opentelemetry exporter.export()
    #       python handler.emit()
    def __init__(self, handler):
        self._handler = handler
        super().__init__()

    def _translate_data(self, data: typing.Sequence[LogData]) -> typing.List[ResourceLogs]:
        return encode_logs(data)

    def export(self, batch: typing.Sequence[LogData]) -> LogExportResult:
        self._handler.emit(self._translate_data(batch))  # this is the magic
        return LogExportResult.SUCCESS

    def shutdown(self) -> None:
        pass


class AWXOTLPHandler(BaseOTLPHandler):
    def __init__(self, handler: logging.Handler, service_name=None, instance_id=None):
        super().__init__(service_name=service_name, instance_id=instance_id)

        get_logger_provider().add_log_record_processor(SimpleLogRecordProcessor(ExporterHandlerAdapter(handler)))


class AWXOTLPWatchedFileHandler(AWXOTLPHandler):
    def __init__(self, directory, instance_id=None):
        service_name = self.generate_service_name()
        filename = f'{service_name}.log'
        handler = logging.handlers.WatchedFileHandler(os.path.join(directory, filename))
        handler.setFormatter(JSONNLFormatter())
        super().__init__(handler, service_name=service_name)


class AWXOTLPStreamHandler(AWXOTLPHandler):
    def __init__(self):
        handler = logging.StreamHandler()
        handler.setFormatter(JSONNLFormatter())
        super().__init__(handler)
