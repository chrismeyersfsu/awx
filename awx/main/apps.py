import os

import logging

from django.apps import AppConfig
from django.utils.translation import gettext_lazy as _
from awx.main.utils.handlers import AWXOTLPStreamHandler, AWXOTLPWatchedFileHandler
from awx.main.utils.named_url_graph import _customize_graph, generate_graph
from awx.conf import register, fields


class MainConfig(AppConfig):
    name = 'awx.main'
    verbose_name = _('Main')

    def load_named_url_feature(self):
        models = [m for m in self.get_models() if hasattr(m, 'get_absolute_url')]
        generate_graph(models)
        _customize_graph()
        register(
            'NAMED_URL_FORMATS',
            field_class=fields.DictField,
            read_only=True,
            label=_('Formats of all available named urls'),
            help_text=_('Read-only list of key-value pairs that shows the standard format of all available named URLs.'),
            category=_('Named URL'),
            category_slug='named-url',
        )
        register(
            'NAMED_URL_GRAPH_NODES',
            field_class=fields.DictField,
            read_only=True,
            label=_('List of all named url graph nodes.'),
            help_text=_(
                'Read-only list of key-value pairs that exposes named URL graph topology.'
                ' Use this list to programmatically generate named URLs for resources'
            ),
            category=_('Named URL'),
            category_slug='named-url',
        )

    def load_oltp_logging(self):
        from django.conf import settings

        log_mode = settings.AWX_LOGGING_MODE

        if log_mode == 'stdout':
            return

        if log_mode == 'file':
            handler = AWXOTLPWatchedFileHandler(settings.LOG_ROOT)
        elif log_mode == 'stdout-otlp':
            handler = AWXOTLPStreamHandler()

        for name in settings.LOGGING['loggers'].keys():
            if not settings.LOGGING['loggers'][name].get('propagate', True):
                logger = logging.getLogger(name)
                logger.addHandler(handler)

        # Everything without explicit propagate=False ends up logging to 'awx' so add it
        logger = logging.getLogger('awx')
        logger.addHandler(handler)

    def ready(self):
        super().ready()

        self.load_oltp_logging()
        self.load_named_url_feature()
