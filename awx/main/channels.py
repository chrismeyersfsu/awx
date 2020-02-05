
import os
import json
import logging
import aiohttp
import asyncio
import datetime

from channels_redis.core import RedisChannelLayer
from channels.layers import get_channel_layer

from django.utils.encoding import force_bytes
from django.conf import settings
from django.apps import apps
from django.core.serializers.json import DjangoJSONEncoder


logger = logging.getLogger('awx.main')


def wrap_broadcast_msg(group, message: str):
    # TODO: Maybe wrap as "group","message" so that we don't need to
    # encode/decode as json.
    return json.dumps(dict(group=group, message=message), cls=DjangoJSONEncoder)


def unwrap_broadcast_msg(payload: dict):
    return (payload['group'], payload['message'])


def get_broadcast_hosts():
    Instance = apps.get_model('main', 'Instance')
    return [h[0] for h in Instance.objects.filter(rampart_groups__controller__isnull=True)
                                          .exclude(hostname=Instance.objects.me().hostname)
                                          .order_by('hostname')
                                          .values_list('hostname')
                                          .distinct()]


class WebsocketTask():
    def __init__(self, name):
        self.name = name

    def run_loop(self, websocket: aiohttp.ClientWebSocketResponse):
        raise RuntimeError("Implement me")


class HealthWebsocketTask(WebsocketTask):
    def __init__(self, period: int = 10, *args, **kwargs):
        super().__init__(*args, **kwargs)

        self.period = period
        self.period_abs = None
        # Ideally, we send a health beat at exactly the period. In reality
        # there is always jitter due to OS needs, system load, etc.
        # This variable tracks that offset.
        self.last_period_offset = 0

    def run_loop(self, websocket: aiohttp.ClientWebSocketResponse):
        now = datetime.datetime.now()
        if not self.period_abs:
            self.period_abs = now

        sleep_time = self.period_abs + self.period


        if now <= next_period:
            logger.warn("Websocket broadcast missed sending health ping.")
        else:
            await asyncio.sleep(sleep_time)


        sleep_time = datetime.datetime.now() - (self.last_period + datetime.timedelta(seconds=PERIOD))



class BroadcastWebsocketTask(WebsocketTask):
    def run_loop(self, websocket: aiohttp.ClientWebSocketResponse):
        async for msg in websocket:
            if msg.type == aiohttp.WSMsgType.ERROR:
                break
            elif msg.type == aiohttp.WSMsgType.TEXT:
                try:
                    payload = json.loads(msg.data)
                except json.JSONDecodeError:
                    logmsg = "Failed to decode broadcast message"
                    if logger.isEnabledFor(logging.DEBUG):
                        logmsg = "{} {}".format(logmsg, payload)
                    logger.warn(logmsg)
                    continue

                (group, message) = unwrap_broadcast_msg(payload)

                await channel_layer.group_send(group, {"type": "internal.message", "text": message})


class RedisGroupBroadcastChannelLayer(RedisChannelLayer):
    def __init__(self, *args, **kwargs):
        super(RedisGroupBroadcastChannelLayer, self).__init__(*args, **kwargs)

        self.broadcast_hosts = get_broadcast_hosts()
        self.broadcast_websockets = set()

        loop = asyncio.get_event_loop()
        for host in self.broadcast_hosts:
            loop.create_task(self.connect(host, settings.BROADCAST_WEBSOCKETS_PORT, run_loop=run_loop_websocket_broadcast))

        for host in self.broadcast_hosts:
            loop.create_task(self.connect(host, settings.BROADCAST_WEBSOCKETS_PORT, run_loop=run_loop_health))

    async def connect(self, host, port, secret='abc123', attempt=0, run_loop):
        from awx.main.consumers import WebsocketSecretAuthHelper # noqa

        if attempt > 0:
            await asyncio.sleep(5)
        channel_layer = get_channel_layer()
        uri = f"{settings.BROADCAST_WEBSOCKETS_PROTOCOL}://{host}:{port}/websocket/broadcast/"
        timeout = aiohttp.ClientTimeout(total=10)

        secret_val = WebsocketSecretAuthHelper.construct_secret()
        try:
            async with aiohttp.ClientSession(headers={'secret': secret_val},
                                             timeout=timeout) as session:
                async with session.ws_connect(uri, ssl=settings.BROADCAST_WEBSOCKETS_VERIFY_CERT) as websocket:
                    run_loop(websocket)
        except Exception as e:
            # Early on, this is our canary. I'm not sure what exceptions we can really encounter.
            # Does aiohttp throws an exception if a disconnect happens?
            logger.warn("Websocket broadcast client exception {}".format(e))
        finally:
            # Reconnect
            loop = asyncio.get_event_loop()
            loop.create_task(self.connect(host, port, secret, attempt=attempt+1))


