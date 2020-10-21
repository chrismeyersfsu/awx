# Copyright (c) 2017 Ansible by Red Hat
# All Rights Reserved.

from contextlib import contextmanager
import logging

from django_pglocks import advisory_lock as django_pglocks_advisory_lock
from django.db import connection

logger = logging.getLogger('awx.main.utils')


@contextmanager
def advisory_lock(*args, **kwargs):
    if connection.vendor == 'postgresql':
        with django_pglocks_advisory_lock(*args, **kwargs) as internal_lock:
            yield internal_lock
    else:
        yield True


def _get_waiter_lock_name(name):
    return f"{name}_waiter"


@contextmanager
def advisory_lock_waiter(name):
    lock1_id = name
    lock2_id = _get_waiter_lock_name(name)

    lock2_context_manager = advisory_lock(lock2_id)
    lock2_context_manager.__enter__()
    logger.info("Grabbed waiter lock")
    with advisory_lock(lock1_id):
        logger.info("\tGrabbed main lock")
        # unlock waiter
        lock2_context_manager.__exit__(None, None, None)
        logger.info("Released waiter lock")
        yield True
        logger.info("\tReleased main lock")

def advisory_lock_has_waiter(name):
    with advisory_lock(_get_waiter_lock_name(name), shared=True, wait=False) as acquired:
        logger.info(f"has_watier is {not acquired}")
        pass
    return not acquired
