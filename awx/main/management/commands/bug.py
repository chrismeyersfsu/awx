from awx.main.models import UnifiedJob, Job
from django.db import connection

from django.contrib.auth.models import User
from django.core.management.base import BaseCommand, CommandError


class Command(BaseCommand):
    """A command that reports whether a username exists within the
    system or not.
    """
    def handle(self, *args, **options):

        def defer_stdout(f):
            def _wrapped(*args, **kwargs):
                objs = f(*args, **kwargs)
                print("Wrapped and defering for {}".format(objs))
                objs.query.deferred_loading[0].add('result_stdout_text')
                return objs
            return _wrapped


        for cls in UnifiedJob.__subclasses__():
            print("Registering cls {}".format(cls))
            cls.base_objects.filter = defer_stdout(cls.base_objects.filter)

        qcount = len(connection.queries)
        js=UnifiedJob.objects.filter().defer('result_stdout_text')
        j=js[0]
        import pdb; pdb.set_trace()
        j.save()
        qs = connection.queries[qcount:]
        for q in qs:
            print(q['sql'])
            print("=================")


