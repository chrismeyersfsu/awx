#!/usr/bin/env python

import uuid
import json
from io import StringIO
from multiprocessing import Process, Queue
from datetime import datetime
from typing import List

from django.db import connection
from django.db.models import sql, Model

from awx.main.models.events import JobEvent

class TS():
    def __init__(self):
        self._begin = None
        self._end = None

    def begin(self):
        if not self._begin:
            self._begin = self.now()
        return self._begin

    def end(self):
        if not self._end:
            self._end = self.now()
        return self._end

    def duration(self):
        if self._end:
            return self._end - self._begin
        elif self.begin:
            return self.now() - self._begin
        else:
            return None

    def now(self):
        return datetime.now()


class Experiment():
    COLUMNS = ('parent_uuid', 'job_id', 'host_name', 'event', 'failed', 'changed', 'uuid', 'playbook', 'play', 'role', 'task', 'counter', 'stdout', 'verbosity', 'start_line', 'end_line', 'modified', 'event_data',)

    def __init__(self, process_count=1, event_size=1):
        self.queue = Queue()
        self.ts_experiment = TS()

        self.exp_params = {
            'event_batch_size': 1000,
            'event_size': event_size,
            'process_count': process_count,
            'write_loops': 100,
        }
        self.exp_results = {
            'records_total': 0,
            'records_per_second': 0,
        }

        self.experiment_time_seconds = 10

        self.id = uuid.uuid4()
        self.epoch = datetime.now()

        self.VALUES = ('1', '1', 'blah', 'runner_on_ok', False, False, f'{self.id}', 'test.yml', 'play', 'role', 'task', 0, 'stdout', 0, 0, 0, f'{self.epoch}',)

    def run_pre(self):
        # Fills self.events
        raise RuntimeError("Define me")

    def run_critical(self):
        raise RuntimeError("Define me")

    def run_post(self):
        records_total = 0
        for i in range(0, self.exp_params['process_count']):
            res = self.queue.get()
            records_total += res['records']

        self.exp_results['records_per_second'] = int(records_total / self.ts_experiment.duration().seconds)
        print(f"{self.__class__.__name__}, {self.exp_params['process_count']}, {self.exp_params['event_size']}, {self.exp_results['records_per_second']}")

    # Per-process function
    def run_experiment(self):
        connection.close()

        self.loops = 0

        ts_experiment = TS()
        ts_experiment.begin()
        with connection.cursor() as cursor:
            while True:
                self.run_critical(cursor)
                self.loops += 1

                if self.loops >= self.exp_params['write_loops']:
                    break
        ts_experiment.end()
        self.queue.put(dict(duration=ts_experiment.duration(), records=self.loops*self.exp_params['event_batch_size']))
        return

    def run(self):
        self.run_pre()

        jobs = [Process(target=self.run_experiment) for i in range(self.exp_params['process_count'])]

        self.ts_experiment.begin()
        for j in jobs: j.start()
        for j in jobs: j.join()
        self.ts_experiment.end()

        self.run_post()


class InsertExperiment(Experiment):
    def run_pre(self):
        event = json.dumps({'h': 'a'*self.exp_params['event_size']})
        self.VALUES += (event,)

        self.values_str = ''
        for i in range(self.exp_params['event_batch_size']):
            self.values_str += '('
            for v in self.VALUES:
                if type(v) is bool:
                    if v:
                        self.values_str += 'true'
                    else:
                        self.values_str += 'false'
                elif type(v) is int:
                    self.values_str += f"{v}"
                else:
                    self.values_str += f"'{v}'"
                self.values_str += ','

            self.values_str = self.values_str[:-1] # strip last ,
            self.values_str += '),'

        self.values_str = self.values_str[:-1] # strip last ,

    def run_critical(self, cursor):
        p = f"INSERT INTO main_jobevent ({','.join(self.COLUMNS)}) VALUES {self.values_str}"
        cursor.execute(p)


class CopyFromExperiment(Experiment):
    def run_pre(self):
        extra = "a" * self.exp_params['event_size']
        event_data = json.dumps(dict(h=extra))
        event_entry = ''
        for value in self.VALUES:
            if value is True:
                event_entry += 't'
            elif value is False:
                event_entry += 'f'
            else:
                event_entry += f"{value}"
            event_entry += ','
        event_entry += f"{event_data}"

        res = ''
        for i in range(self.exp_params['event_batch_size']):
            res += event_entry + "\n"

        self.events = res

    def run_critical(self, cursor):
        cursor.copy_from(StringIO(self.events), 'main_jobevent', sep=',', columns=self.COLUMNS)


def run():
    single_threaded = [1, 128, 256, 512, 1024, 2048, 4096, 8192, 16384]
    multi_threaded = [1, 2, 3, 4, 5, 6, 7, 8, 9, 10]

    print("Single threaded INSERT experiment")
    print(f"experiment, process_count, event_size, inserts_per_second")
    for i in single_threaded:
        exp = InsertExperiment(process_count=1, event_size=i)
        exp.run()
    print("")

    print("Single threaded COPY FROM experiment")
    print(f"experiment, process_count, event_size, inserts_per_second")
    for i in single_threaded:
        exp = CopyFromExperiment(process_count=1, event_size=i)
        exp.run()

    print("Multi threaded INSERT experiment")
    print(f"experiment, process_count, event_size, inserts_per_second")
    for i in multi_threaded:
        exp = InsertExperiment(process_count=i, event_size=2048)
        exp.run()
    print("")

    print("Multi threaded COPY FROM experiment")
    print(f"experiment, process_count, event_size, inserts_per_second")
    for i in multi_threaded:
        exp = CopyFromExperiment(process_count=i, event_size=2048)
        exp.run()
    print("")


