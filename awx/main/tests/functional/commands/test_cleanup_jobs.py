import pytest
from datetime import datetime, timedelta
from pytz import timezone
from collections import OrderedDict

from django.db.models.deletion import Collector, SET_NULL, CASCADE
from django.core.management import call_command

from awx.main.models import (
    JobTemplate, User, Job, JobEvent, Notification,
    WorkflowJobNode, JobHostSummary
)


@pytest.fixture
def setup_environment(inventory, project, machine_credential, host, notification_template, label):
    '''
    Create old jobs and new jobs, with various other objects to hit the
    related fields of Jobs. This makes sure on_delete() effects are tested
    properly.
    '''
    old_jobs = []
    new_jobs = []
    days = 10
    days_str = str(days)

    jt = JobTemplate.objects.create(name='testjt', inventory=inventory, project=project)
    jt.credentials.add(machine_credential)
    jt_user = User.objects.create(username='jobtemplateuser')
    jt.execute_role.members.add(jt_user)

    notification = Notification()
    notification.notification_template = notification_template
    notification.save()

    for i in range(3):
        job1 = jt.create_job()
        job1.created =datetime.now(tz=timezone('UTC'))
        job1.save()
        # create jobs with current time
        JobEvent.create_from_data(job_id=job1.pk, uuid='abc123', event='runner_on_start',
                                  stdout='a' * 1025).save()
        new_jobs.append(job1)

        job2 = jt.create_job()
        # create jobs 10 days ago
        job2.created = datetime.now(tz=timezone('UTC')) - timedelta(days=days)
        job2.save()
        job2.dependent_jobs.add(job1)
        JobEvent.create_from_data(job_id=job2.pk, uuid='abc123', event='runner_on_start',
                                  stdout='a' * 1025).save()
        old_jobs.append(job2)

    jt.last_job = job2
    jt.current_job = job2
    jt.save()
    host.last_job = job2
    host.save()
    notification.unifiedjob_notifications.add(job2)
    label.unifiedjob_labels.add(job2)
    jn = WorkflowJobNode.objects.create(job=job2)
    jn.save()
    jh = JobHostSummary.objects.create(job=job2)
    jh.save()

    return (old_jobs, new_jobs, days_str)


@pytest.mark.django_db
def test_cleanup_jobs(setup_environment):
    (old_jobs, new_jobs, days_str) = setup_environment

    # related_fields
    related = [f for f in Job._meta.get_fields(include_hidden=True)
               if f.auto_created and not
               f.concrete and
               (f.one_to_one or f.one_to_many)]

    job = old_jobs[-1] # last job

    # gather related objects for job
    related_should_be_removed = {}
    related_should_be_null = {}
    for r in related:
        qs = r.related_model._base_manager.using('default').filter(
            **{"%s__in" % r.field.name: [job.pk]}
        )
        if qs.exists():
            if r.field.remote_field.on_delete == CASCADE:
                related_should_be_removed[qs.model] = set(qs.values_list('pk', flat=True))
            if r.field.remote_field.on_delete == SET_NULL:
                related_should_be_null[(qs.model,r.field.name)] = set(qs.values_list('pk', flat=True))

    assert related_should_be_removed
    assert related_should_be_null

    call_command('cleanup_jobs', '--days', days_str)
    # make sure old jobs are removed
    assert not Job.objects.filter(pk__in=[obj.pk for obj in old_jobs]).exists()

    # make sure new jobs are untouched
    assert len(new_jobs) == Job.objects.filter(pk__in=[obj.pk for obj in new_jobs]).count()

    # make sure related objects are destroyed or set to NULL (none)
    for model, values in related_should_be_removed.items():
        assert not model.objects.filter(pk__in=values).exists()

    for (model,fieldname), values in related_should_be_null.items():
        for v in values:
            assert not getattr(model.objects.get(pk=v), fieldname)
