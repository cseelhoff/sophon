import os
from pathlib import Path
import re
import sys
import time
from urllib.parse import quote

from ansible.plugins.callback import CallbackBase

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from deployment import ROOT, STAGES, new_report, write_report


SECRET_KEY = re.compile(r'password|passwd|passphrase|token|secret|authorization|cookie|ticket|jwt|private.?key|api.?key|licen[cs]e.?key', re.I)
SECRET_ASSIGNMENT = re.compile(
    r'''(?ix)([\w-]*(?:password|passwd|passphrase|token|secret|authorization|cookie|ticket|jwt|private.?key|api.?key|licen[cs]e.?key)[\w-]*["']?\s*[:=]\s*)(?:"[^"]*"|'[^']*'|[^\s,;&}\]]+)''')


def remember_secrets(callback, value, sensitive=False):
    if isinstance(value, dict):
        sensitive = sensitive or bool(value.get('_ansible_no_log'))
        for key, item in value.items():
            remember_secrets(callback, item, sensitive or bool(SECRET_KEY.search(str(key))))
    elif isinstance(value, (list, tuple)):
        for item in value:
            remember_secrets(callback, item, sensitive)
    elif sensitive and isinstance(value, str) and value:
        callback.secrets.add(value)


def diagnostic_text(callback, value, limit=4000):
    if not isinstance(value, str):
        return ''
    for secret in sorted(callback.secrets, key=len, reverse=True):
        value = value.replace(secret, '[redacted]').replace(quote(secret, safe=''), '[redacted]')
    value = re.sub(r'-----BEGIN [^-]*PRIVATE KEY-----.*?-----END [^-]*PRIVATE KEY-----', '[redacted private key]', value, flags=re.S)
    value = re.sub(r'(?i)\b(Bearer|Basic)\s+[A-Za-z0-9._~+/=-]+', r'\1 [redacted]', value)
    value = re.sub(r'(https?://)[^/\s@]+@', r'\1[redacted]@', value)
    value = SECRET_ASSIGNMENT.sub(r'\1[redacted]', value)
    return value[:limit] + ('\n[truncated]' if len(value) > limit else '')


def task_record(callback, task):
    tasks = callback.report['stages'][callback.report['current']]['tasks']
    identifier = str(task._uuid)
    if identifier not in callback.task_rows:
        if len(tasks) >= 1000:
            tasks.pop(0)
            callback.task_rows = {key: index - 1 for key, index in callback.task_rows.items() if index > 0}
        hidden = bool(task.no_log)
        remember_secrets(callback, task.args, hidden)
        callback.task_rows[identifier] = len(tasks)
        tasks.append({'id': identifier, 'name': 'Protected task' if hidden else diagnostic_text(callback, task.get_name(), 300),
                      'action': task.action, 'source': diagnostic_text(callback, (task.get_path() or '').removeprefix(str(ROOT) + '/'), 500),
                      'state': 'running', 'started_at': time.time()})
    return tasks[callback.task_rows[identifier]]


def record_diagnostics(callback, result, outcome, ignore_errors):
    task = result._task
    record = task_record(callback, task)
    data = result._result
    hidden = bool(task.no_log or data.get('_ansible_no_log'))
    remember_secrets(callback, data, hidden)
    record['state'] = 'ignored' if ignore_errors else 'changed' if outcome == 'ok' and data.get('changed') else outcome
    record['finished_at'] = time.time()
    record['duration'] = round(record['finished_at'] - record['started_at'], 2)
    for key in ('message', 'stdout', 'stderr', 'items', 'rc', 'status', 'attempts'):
        record.pop(key, None)
    if hidden:
        record['name'] = 'Protected task'
        record['message'] = 'Output hidden by Ansible no_log.'
        return
    record['name'] = diagnostic_text(callback, task.get_name(), 300)
    for key in ('rc', 'status', 'attempts'):
        if type(data.get(key)) is int:
            record[key] = data[key]
    if outcome == 'failed':
        for key in ('msg', 'stdout', 'stderr'):
            text = diagnostic_text(callback, data.get(key))
            if text:
                record['message' if key == 'msg' else key] = text
        if isinstance(data.get('results'), list):
            failures = []
            for index, item in enumerate(data['results']):
                if isinstance(item, dict) and (item.get('failed') or item.get('unreachable')):
                    if item.get('_ansible_no_log'):
                        failures.append(f'Item {index + 1}: output hidden by Ansible no_log.')
                    else:
                        failures.append(f'Item {index + 1}: ' + diagnostic_text(callback, item.get('msg') or item.get('stderr') or 'Failed', 500))
                if len(failures) == 20:
                    break
            record['items'] = failures


def finish_stage(callback):
    current = callback.report['current']
    if current is None:
        return
    stage = callback.report['stages'][current]
    stage['state'] = ('failed' if callback.failures else
                      'completed' if stage['ok'] or stage['ignored'] or stage['rescued'] else 'skipped')
    callback.failures.clear()


def record_result(callback, result, outcome, ignore_errors=False):
    if not callback.path or callback.report['current'] is None:
        return
    task = result._task
    if outcome != 'failed' and task.action.split('.')[-1] in ('include_role', 'include_tasks', 'meta'):
        return
    stage = callback.report['stages'][callback.report['current']]
    record_diagnostics(callback, result, outcome, ignore_errors)
    if outcome == 'failed' and not ignore_errors:
        parents = set()
        parent = task._parent
        while parent is not None:
            parents.add(str(parent._uuid))
            parent = getattr(parent, '_parent', None)
        callback.failures[str(task._uuid)] = parents
    else:
        stage['ignored' if ignore_errors else outcome] += 1
    write_report(callback.path, callback.report)


class CallbackModule(CallbackBase):
    CALLBACK_VERSION = 2.0
    CALLBACK_TYPE = 'notification'
    CALLBACK_NAME = 'sophon_progress'
    CALLBACK_NEEDS_ENABLED = True

    def __init__(self):
        super().__init__()
        self.path = os.environ.get('SOPHON_PROGRESS_FILE')
        self.report = new_report()
        self.failures = {}
        self.task_rows = {}
        self.secrets = set()
        remember_secrets(self, dict(os.environ))

    def v2_playbook_on_start(self, playbook):
        if self.path:
            write_report(self.path, self.report)

    def v2_playbook_on_task_start(self, task, is_conditional):
        if not self.path:
            return
        if task._role is None and 'sophon_stage_end' in task.tags:
            finish_stage(self)
            self.report['current'] = None
        if task._role is None and task.action.split('.')[-1] == 'include_role':
            markers = [tag.removeprefix('sophon_stage_') for tag in task.tags
                       if tag.startswith('sophon_stage_')]
            if len(markers) == 1 and markers[0] in STAGES:
                finish_stage(self)
                self.task_rows.clear()
                self.report['current'] = markers[0]
                self.report['stages'][markers[0]]['state'] = 'running'
        child = task
        parent = task._parent
        while parent is not None:
            if any(member._uuid == child._uuid for member in getattr(parent, 'rescue', [])):
                recovered = [failure for failure, ancestors in self.failures.items()
                             if str(parent._uuid) in ancestors]
                for failure in recovered:
                    del self.failures[failure]
                    if failure in self.task_rows:
                        self.report['stages'][self.report['current']]['tasks'][self.task_rows[failure]]['state'] = 'rescued'
                if self.report['current']:
                    self.report['stages'][self.report['current']]['rescued'] += len(recovered)
            child, parent = parent, getattr(parent, '_parent', None)
        if self.report['current'] and task.action.split('.')[-1] not in ('include_role', 'include_tasks', 'meta'):
            self.task_rows.pop(str(task._uuid), None)
            task_record(self, task)
        write_report(self.path, self.report)

    def v2_runner_on_ok(self, result):
        record_result(self, result, 'ok')

    def v2_runner_on_skipped(self, result):
        record_result(self, result, 'skipped')

    def v2_runner_on_failed(self, result, ignore_errors=False):
        record_result(self, result, 'failed', ignore_errors)

    def v2_runner_on_unreachable(self, result):
        record_result(self, result, 'failed')

    def v2_runner_retry(self, result):
        if self.path and self.report['current']:
            record_diagnostics(self, result, 'failed', False)
            record = task_record(self, result._task)
            record['state'] = 'retrying'
            write_report(self.path, self.report)

    def v2_playbook_on_stats(self, stats):
        if not self.path:
            return
        finish_stage(self)
        self.report['state'] = 'failed' if stats.failures or stats.dark else 'completed'
        self.report['current'] = None
        write_report(self.path, self.report)