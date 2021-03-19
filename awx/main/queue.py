# Copyright (c) 2015 Ansible, Inc.
# All Rights Reserved.

# Python
import json
import logging
import redis
from contextlib import contextmanager

# Django
from django.conf import settings


__all__ = ['CallbackQueueDispatcher']


# use a custom JSON serializer so we can properly handle !unsafe and !vault
# objects that may exist in events emitted by the callback plugin
# see: https://github.com/ansible/ansible/pull/38759
class AnsibleJSONEncoder(json.JSONEncoder):

    def default(self, o):
        if getattr(o, 'yaml_tag', None) == '!vault':
            return o.data
        return super(AnsibleJSONEncoder, self).default(o)


class CallbackQueueDispatcher(object):

    @contextmanager
    def pipeline(self):
        self.conn = self.connection_pipe
        res = []
        try:
            yield res
        finally:
            self.conn = self.connection
            res.extend(self.execute())

    def __init__(self):
        self.queue = getattr(settings, 'CALLBACK_QUEUE', '')
        self.logger = logging.getLogger('awx.main.queue.CallbackQueueDispatcher')
        self.connection = redis.Redis.from_url(settings.BROKER_URL)
        self.connection_pipe = redis.Redis.from_url(settings.BROKER_URL, decode_responses=True).pipeline()

        self.conn = self.connection

    def dispatch(self, obj):
        self.conn.lpush(self.queue, json.dumps(obj, cls=AnsibleJSONEncoder))

    def get_job_events_total(self):
        return self.conn.hgetall('awx_job_events_total')

    def get_job_events_processed(self):
        return self.conn.hgetall('awx_job_events_processed')

    @staticmethod
    def _redis_dict_to_int_int(d):
        """
        Results from get_job_events_total() should be passed through this helper function to convert the dict key and values that are strings into integers.
        """
        new_d = {}
        for k, v in d.items():
            new_d[int(k)] = int(v)
        return new_d

    def get_job_extra_data(self, job_id):
        return self.conn.hget('awx_job_events_extra', job_id)

    def set_job_event_total(self, job_id, total):
        return self.conn.hset('awx_job_events_total', int(job_id), int(total))

    def set_job_extra_data(self, job_id, extra_data):
        return self.conn.hset('awx_job_events_extra', int(job_id), json.dumps(extra_data))

    def incr_job_events_processed(self, job_id, count=1):
        return self.conn.hincrby('awx_job_events_processed', job_id, count)

    def delete_in_flight_jobs(self, job_ids):
        self.conn.hdel('awx_job_events_total', *job_ids)
        self.conn.hdel('awx_job_events_processed', *job_ids)
        self.conn.hdel('awx_job_events_extra', *job_ids)

    def set_job_events_complete(self, job_ids):
        now = int(time.time())
        mapping = {job_id: now for job_id in job_ids}
        return self.conn.zadd('awx_job_events_complete', mapping)

    def commit_event(self, event_raw):
        return self.conn.lrem(settings.CALLBACK_PROCESSING_QUEUE, 1, json.dumps(event_raw, cls=AnsibleJSONEncoder))

    def execute(self):
        # execute() only makes sense for pipeline actions
        return self.connection_pipe.execute()
