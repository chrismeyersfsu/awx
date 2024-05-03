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


class RelayWebsocketServer(App, WebsocketHandler, WebsocketServerParams):
    def __init__(self):
        return

    def ws_message(self, ws, payload, opcode):
        payload = json.loads(payload)  # TODO: Try catch
        group = payload.get('group', None)
        message = payload.get('message', None)
        # print(f"InternalClientWebsocket::ws_message() group, msg {group} {message}")
        if not group or not message:
            print(f"Message malformed? We expected a json payload w/ keys 'group' and 'message' but got payload {message}")
            return
        return ws.publish(group, json.dumps(message), opcode)

    def ws_open(self, ws):
        print("InternalClientWebsocket::ws_open()")


class UserFacingWebsocketServer(RelayWebsocketServer):
    def ws_open(self, ws):
        print("UserFacingWebsocketServer::ws_open()")
        # return App.ws_open(ws)

    def ws_message(self, ws, message, opcode):
        # TODO: only send this once
        ws.send(json.dumps({"accept": True, "user": 1}), OpCode.TEXT)

        print(f"UserFacingWebsocketServer::ws_message() opcode {opcode} vs. {OpCode.TEXT} {OpCode.BINARY}")
        # print("ExternalClientWebsocket::ws_message() Got message: " + message)
        d = json.loads(message)

        groups = d.get('groups', {})
        keys = ['jobs', 'job_events']
        user_requested_groups = []
        if groups:
            for k in keys:
                for v in groups.get(k):
                    user_requested_groups.append(f'{k}-{v}')

        if user_requested_groups:
            desired_groups = set(user_requested_groups)
            current_groups = set()
            ws.for_each_topic(lambda topic: current_groups.add(topic))
            unsubscribe_groups = current_groups - desired_groups
            subscribe_groups = desired_groups - current_groups

            for g in unsubscribe_groups:
                ws.unsubscribe(g)

            for g in subscribe_groups:
                ws.subscribe(g)


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
        wsp = UserFacingWebsocketServer()
        ws_config = wsp.get_ws_params()

        app = App()
        app.ws("/websocket/", UserFacingWebsocketServer().get_ws_params())
        app.ws("/websocket/relay/", RelayWebsocketServer().get_ws_params())
        app.listen(
            listen_port,
            handler=lambda config: print("Listening on port http://localhost:%d now\n" % (config.port)),
        )
        app.run()
