import itertools
import random
import string

import aioredis
import asyncio

from channels_redis.core import ConnectionPool
from channels_redis.core import RedisChannelLayer


# https://github.com/rakyi/channels_redis/tree/redis-sentinel


class SentinelManager:
    def __init__(self, sentinel_hosts, **kwargs):
        self.sentinel_hosts = sentinel_hosts
        self.kwargs = kwargs
        self.sentinel = None
        self.masters = {}
        self.pools_loop = None

    async def get_sentinel(self, force_new=False):
        if not self.sentinel or force_new:
            self.sentinel = await aioredis.create_sentinel(
                self.sentinel_hosts, **self.kwargs
            )
        return self.sentinel

    async def client(self, service: str):
        # HACK: event loop not what expected so create a new connection
        # Ideally, we need to do what channels_redis does. Keep a record of all the event
        # loops and provide the correct event loop to the create_sentinel()
        if service not in self.masters or self.pools_loop != asyncio.get_event_loop():
            sentinel = await self.get_sentinel(force_new=True if self.pools_loop != asyncio.get_event_loop() else False)
            master = sentinel.master_for(service)
            self.masters[service] = master
            return master
        return self.masters[service]


class RedisSentinelChannelLayer(RedisChannelLayer):
    def __init__(self, sentinels=None, services=None, **kwargs):
        super(RedisSentinelChannelLayer, self).__init__(**kwargs)
        # Configure Sentinel
        self.sentinels = sentinels or [("localhost", 26379)]
        self.sentinel_manager = SentinelManager(self.sentinels)
        # TODO: Discover services?
        if not services:
            raise ValueError("At least one service must be provided")
        self.services = services
        self.ring_size = len(services)
        # Normal channels choose a service index by cycling through
        # the available services.
        self._receive_index_generator = itertools.cycle(range(len(self.services)))
        self._send_index_generator = itertools.cycle(range(len(self.services)))

    def __str__(self):
        return "{}(sentinels={}, services={})".format(
            self.__class__.__name__, self.sentinels, self.services
        )

    ### Connection handling ###

    def connection(self, index):
        """
        Returns the correct connection for the index given.
        Lazily instantiates pools.
        """
        # Catch bad indexes
        if not 0 <= index < self.ring_size:
            raise ValueError(
                "There are only %s hosts - you asked for %s!" % (self.ring_size, index)
            )
        return self.ConnectionContextManager(
            self.sentinel_manager, self.services[index]
        )

    class ConnectionContextManager:
        def __init__(self, sentinel_manager, service):
            self.sentinel_manager = sentinel_manager
            self.service = service

        async def __aenter__(self):
            self.conn = await self.sentinel_manager.client(self.service)
            return self.conn

        async def __aexit__(self, exc_type, exc, tb):
            if exc:
                # TODO: What now?
                print("Exception occurred", exc_type, exc)
            else:
                print("Context manager exit without exception")
