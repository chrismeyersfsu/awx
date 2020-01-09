import psycopg2
from pgpubsub import PubSub

from contextlib import contextmanager

from django.conf import settings


def get_local_queuename():
    return settings.CLUSTER_HOST_ID


@contextmanager
def pg_bus_conn():
    conf = settings.DATABASES['default']
    conn = psycopg2.connect(dbname=conf['NAME'],
			    host=conf['HOST'],
			    user=conf['USER'],
			    password=conf['PASSWORD'])
    # Django connection.cursor().connection doesn't have autocommit=True on
    conn.set_session(autocommit=True)
    pubsub = PubSub(conn)
    yield pubsub
    conn.close()
