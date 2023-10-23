import logging
import yaml
import json

from django.conf import settings
from django.core.management.base import BaseCommand
from awx.settings.defaults import BROADCAST_WEBSOCKET_GROUP_NAME
from socketify import App, AppOptions, OpCode, CompressOptions


logger = logging.getLogger('awx.main.websockets')


class WebsocketHandler(object):
    def ws_open(self, ws):
        RuntimeError("Implement me")

    def ws_message(self, ws, message, opcode):
        RuntimeError("Implement me")


class WebsocketServerParams(object):
    def get_ws_route(self):
        return '/websocket'

    def get_ws_params(self):
        return {
            "compression": CompressOptions.SHARED_COMPRESSOR,
            "max_payload_length": 16 * 1024 * 1024,
            "idle_timeout": 60,
            "open": self.ws_open,
            "message": self.ws_message,
            # The library guarantees proper unsubscription at close
            "close": lambda ws, code, message: print("WebSocket closed"),
            "subscription": lambda ws, topic, subscriptions, subscriptions_before: print(
                f'subscription/unsubscription on topic {topic} {subscriptions} {subscriptions_before}'
            ),
        }


class ClientWebsocket(App, WebsocketHandler, WebsocketServerParams):
    def __init__(self):
        pass

    def send_json(self, ws, data):
        return ws.send(json.dumps(data), OpCode.TEXT)

    @classmethod
    def broadcast_json(self, ws, group, data):
        return ws.broadcast(group, json.dumps(data), OpCode.TEXT)

    def ws_open(self, ws):
        print("A WebSocket got connected!")
        self.send_json(ws, {"accept": True, "user": 1})


class ExternalClientWebsocket(ClientWebsocket):
    @classmethod
    def ws_message(cls, ws, message, opcode):
        # Broadcast this message
        print("Got message: " + message)
        d = json.loads(message)

        if 'groups' in d:
            desired_groups = set(d['groups'])
            current_groups = set()
            ws.for_each_topic(lambda topic: current_groups.add(topic))
            unsubscribe_groups = current_groups - desired_groups
            subscribe_groups = desired_groups - current_groups

            for g in unsubscribe_groups:
                ws.unsubscribe(g)

            for g in subscribe_groups:
                ws.subscribe(g)


class InternalClientWebsocket(ClientWebsocket):
    pass


class RelayWebsocket(object):
    @classmethod
    def unwrap_broadcast_msg(payload: dict):
        return (payload['group'], payload['message'])

    @classmethod
    def ws_open(cls, ws):
        ws.subscribe(BROADCAST_WEBSOCKET_GROUP_NAME)

    @classmethod
    def ws_message(cls, ws, message, opcode):
        (group, message) = cls.unwrap_broadcast_msg(message)
        ws.broadcast(group, message)
        # ws.broadcast(BROADCAST_WEBSOCKET_GROUP_NAME)


class Command(BaseCommand):
    help = 'Launch the websocket server'

    def add_arguments(self, parser):
        parser.add_argument('--port', dest='port', type=int, help='the port', default=8051)

    def handle(self, *arg, **options):
        listen_port = options.get('port')
        '''
        {"xrftoken":"pZQMuHn9yXV3JPFv2vbjPPFTivWRMG3i",
         "groups":{"jobs":["status_changed"],"control":["limit_reached_1"]}}
        '''

        app = App()
        app.ws(
            "/*",
            {
                "compression": CompressOptions.SHARED_COMPRESSOR,
                "max_payload_length": 16 * 1024 * 1024,
                "idle_timeout": 60,
                "open": ws_open,
                "message": ws_message,
                # The library guarantees proper unsubscription at close
                "close": lambda ws, code, message: print("WebSocket closed"),
                "subscription": lambda ws, topic, subscriptions, subscriptions_before: print(
                    f'subscription/unsubscription on topic {topic} {subscriptions} {subscriptions_before}'
                ),
            },
        )
        app.listen(
            listen_port,
            handler=lambda config: print("Listening on port http://localhost:%d now\n" % (config.port)),
        )
        app.run()
