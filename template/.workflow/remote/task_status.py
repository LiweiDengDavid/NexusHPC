"""Read-only, one-row-per-task PBS view; compatible with CETUS Python 3.6."""
import argparse
import csv
from concurrent.futures import ThreadPoolExecutor
from functools import wraps
from itertools import islice
import json
import math
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys
import time
import unicodedata

PROJECT = (Path(REMOTE_INPUT['settings']['HPC_DASH_PRIMARY_ROOT'])
           if 'REMOTE_INPUT' in globals() else Path(__file__).resolve().parents[2])

_QUERY_CACHE = None


def query_snapshot(function):
    """Share read-only work within one refresh; never reuse it on a later query."""
    @wraps(function)
    def wrapped(*args, **kwargs):
        global _QUERY_CACHE
        if _QUERY_CACHE is not None:
            return function(*args, **kwargs)
        _QUERY_CACHE = {}
        try:
            return function(*args, **kwargs)
        finally:
            _QUERY_CACHE = None
    return wrapped


def snapshot_cached(key):
    def decorate(function):
        @wraps(function)
        def wrapped(*args):
            if _QUERY_CACHE is None:
                return function(*args)
            cache_key = (function, key(*args))
            if cache_key not in _QUERY_CACHE:
                # Retain the arguments so temporary task/job objects cannot reuse
                # an object ID that still names a cached result.
                _QUERY_CACHE[cache_key] = (args, function(*args))
            return _QUERY_CACHE[cache_key][1]
        return wrapped
    return decorate


@snapshot_cached(lambda path: str(path))
def resolved_path(path):
    return Path(path).resolve()


@snapshot_cached(lambda root, pattern: (str(root), pattern))
def glob_paths(root, pattern):
    return tuple(Path(root).glob(pattern))


@snapshot_cached(lambda path: str(path))
def metadata_text(path):
    """Small submission records shared by several task definitions/attempts."""
    return Path(path).read_text()


@snapshot_cached(lambda root: str(root))
def deferred_states(root):
    states = []
    for path in glob_paths(root, 'outputs/status/deferred_gpu/*/state.json'):
        try:
            states.append((path, json.loads(metadata_text(path))))
        except (OSError, ValueError):
            continue
    return states


def command(*args):
    settings = REMOTE_INPUT['settings'] if 'REMOTE_INPUT' in globals() else config_values(PROJECT / '.workflow/config.env')
    executable = settings.get(args[0].upper() + '_BIN') or args[0]
    p = subprocess.run([executable] + list(args[1:]), universal_newlines=True,
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if p.returncode:
        raise RuntimeError(p.stderr.strip() or p.stdout.strip())
    return p.stdout


def config_values(path):
    values = {}
    for line in Path(path).read_text().splitlines():
        match = re.match(r'^([A-Z][A-Z0-9_]*)=(.*)$', line)
        if match:
            parts = shlex.split(match.group(2), comments=True)
            if len(parts) == 1:
                values[match.group(1)] = parts[0]
    return values


def catalogue(jobs=None):
    if 'REMOTE_INPUT' in globals():
        settings = REMOTE_INPUT['settings']
        tasks = [dict(t) for t in REMOTE_INPUT['tasks']]
    else:
        settings = config_values(PROJECT / '.workflow/config.env')
        path = PROJECT / 'configs/hpc_tasks.json'
        tasks = json.loads(path.read_text()) if path.exists() else []
    for t in tasks:
        key = t.get('root_key', 'PROJECT')
        t['root'] = settings.get('HPC_DASH_PRIMARY_ROOT', str(PROJECT)) if key == 'PROJECT' else settings[key]
    roots = {str(PROJECT)} | {t['root'] for t in tasks}
    roots.update(value for key, value in settings.items()
                 if key.startswith('HPC_DASH_') and key.endswith('_ROOT') and value)
    if 'REMOTE_INPUT' in globals():
        roots.update(REMOTE_INPUT.get('project_roots', []))
    # This also discovers an active project that has never been listed centrally.
    roots.update(j.get('Variable_List', {}).get('PBS_O_WORKDIR', '') for j in (jobs or {}).values())
    def identity_of(t, root):
        return (root, t.get('task_id') or t.get('variant') or t.get('dimension') or
                t.get('model') or t['label'], t.get('run_id'))
    identities = {identity_of(t, str(Path(t['root']).resolve())) for t in tasks}
    roots = {str(Path(r).resolve()) for r in roots if r and Path(r).is_absolute()}
    for root in sorted(roots):
        definitions = []
        legacy = Path(root) / 'configs/hpc_tasks.json'
        if legacy.is_file():
            try:
                definitions.extend(json.loads(legacy.read_text()))
            except (OSError, ValueError) as error:
                print('WARNING: cannot read {}: {}'.format(legacy, error), file=sys.stderr)
        for definition in sorted((Path(root) / 'configs/hpc_tasks.d').glob('*.json')):
            try:
                value = json.loads(definition.read_text())
                definitions.extend(value if isinstance(value, list) else [value])
            except (OSError, ValueError) as error:
                print('WARNING: cannot read {}: {}'.format(definition, error), file=sys.stderr)
        for definition in definitions:
            if definition.get('root_key', 'PROJECT') != 'PROJECT':
                continue
            identity = identity_of(definition, root)
            if identity not in identities:
                tasks.append(dict(definition, root=root))
                identities.add(identity)
        for path in sorted((Path(root) / 'outputs/runs').glob('*/manifest.json')):
            try:
                t = json.loads(path.read_text())
                if (t.get('schema_version') != 1 or t.get('registry') is not True
                        or Path(t['root']).resolve() != Path(root).resolve()
                        or (t['run_id'] != path.parent.name and
                            not (t.get('external_submission') and path.parent.name == t.get('storage_key')))
                        or t['receipt'] != str(path.parent.relative_to(root) / 'state.json')):
                    raise ValueError('Invalid automatic task manifest')
                t['root'] = root
                if dispatched_reservation_claimed(t, root, tasks):
                    continue
                receipt = task_receipt(t)
                if not receipt.get('jobs'):
                    continue  # A failed qsub/preflight does not create a task row.
                accepted_attempts = {r.get('attempt') for r in receipt['jobs'] if r.get('attempt')}
                starts = [json.loads(metadata_text(Path(root) / a / 'identity.json'))['started_at']
                          for a in accepted_attempts]
                if starts:
                    t['submitted_after'] = max(starts)
                identity = (root, t['task_id'], t['run_id'])
                if identity not in identities:
                    tasks.append(t)
                    identities.add(identity)
            except (OSError, ValueError, KeyError, TypeError) as error:
                print('WARNING: skipping task record {}: {}'.format(path, error), file=sys.stderr)
    # A broad legacy root/variant rule must not claim jobs already registered
    # with a distinct explicit experiment identity.
    automatic_ids = {}
    for task in tasks:
        if task.get('registry'):
            try:
                automatic_ids.setdefault(task['root'], set()).update(r['id'] for r in task_receipt(task)['jobs'])
            except (OSError, ValueError, KeyError, TypeError):
                pass
    for task in tasks:
        if not task.get('registry'):
            task['_excluded_automatic_ids'] = automatic_ids.get(str(Path(task['root']).resolve()), set())
    return tasks


@snapshot_cached(lambda task: id(task))
def referenced_job_id(task):
    reference = task.get('job_reference')
    if not reference:
        return None
    try:
        value = json.loads(metadata_text(Path(task['root']) / reference['path']))
        if reference.get('select'):
            candidates = [r for r in value if all(r.get(k) == v for k,v in reference['select'].items())]
            if len(candidates) != 1:
                return None
            value = candidates[0]
        for key in reference['keys']:
            value = value[key]
        return value if isinstance(value, str) else None
    except (OSError, ValueError, KeyError, TypeError):
        return None


def dispatched_reservation_claimed(manifest, root, tasks):
    """Hide the reservation's automatic row when its linked experiment owns it."""
    target = manifest.get('dispatch_target')
    if not isinstance(target, dict):
        return False
    try:
        related = next((task for task in tasks
            if resolved_path(task.get('root', '')) == resolved_path(root)
            and task.get('task_id') == target.get('task_id')
            and task.get('run_id') == target.get('run_id')), None)
        if related is None:
            return False
        phase_relative = target.get('phase_receipt')
        phase_path = (Path(root) / phase_relative).resolve()
        phase_path.relative_to(Path(root).resolve())
        phase = json.loads(phase_path.read_text())
        if (phase.get('task_id'), phase.get('run_id'), phase.get('main_job_id')) != (
                target.get('task_id'), target.get('run_id'), target.get('job_id')):
            return False
        if (phase.get('reservation_task_id'), phase.get('reservation_run_id')) != (
                manifest.get('task_id'), manifest.get('run_id')):
            return False
        hold_receipt = json.loads((Path(root) / manifest['receipt']).read_text())
        return any(row.get('id') == target.get('job_id') for row in hold_receipt.get('jobs', []))
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return False


def reservation_receipt(task):
    path = task.get('reservation_phase_receipt')
    if not path:
        return {}
    try:
        receipt = json.loads((Path(task['root']) / path).read_text())
        if (receipt.get('task_id') != task.get('task_id')
                or receipt.get('run_id') != task.get('run_id')
                or receipt.get('execution_phase') not in {'waiting_for_ag', 'scoring'}):
            return {}
        return receipt
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        return {}


def reservation_job_matches(task, job, receipt):
    try:
        job_id = receipt.get('main_job_id')
        binding = next((row for row in receipt.get('jobs', []) if row.get('id') == job_id), {})
        env = job.get('Variable_List', {})
        reservation_task_id = receipt.get('reservation_task_id', task.get('task_id'))
        reservation_run_id = receipt.get('reservation_run_id', task.get('run_id'))
        return bool(job_id == job.get('_id')
            and binding.get('role') in {'main', 'continuation'}
            and job.get('job_state') in {'R', 'E', 'B', 'Q', 'H', 'W', 'T'}
            and int(job.get('Resource_List', {}).get('ngpus', 0)) >= 1
            and env.get('HPC_TASK_ID') == reservation_task_id
            and env.get('HPC_RUN_ID') == reservation_run_id
            and resolved_path(env.get('PBS_O_WORKDIR', '/')) == resolved_path(task['root']))
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        return False


@snapshot_cached(lambda task: tuple(task.get(key) for key in
                 ('root', 'receipt', 'registry', 'native_receipt', 'task_id', 'dimension')))
def task_receipt(task):
    path = Path(task['root']) / task['receipt']
    if task.get('registry'):
        try:
            value = json.loads(metadata_text(path)) if path.exists() else {'jobs': []}
            if not isinstance(value, dict) or not isinstance(value.get('jobs'), list):
                raise ValueError('Invalid receipt schema')
            if any(not isinstance(r, dict) or not isinstance(r.get('id'), str) for r in value['jobs']):
                raise ValueError('Invalid job binding')
        except (OSError, ValueError, TypeError):
            value = {'jobs': []}
        bindings = {r['id']: r for r in value['jobs']}
        if task.get('native_receipt'):
            try:
                native = json.loads(metadata_text(Path(task['root']) / task['native_receipt']))
                if isinstance(native, dict):
                    for row in native.get('jobs', []):
                        bindings[row['id']] = dict(bindings.get(row['id'], {}), **row)
            except (OSError, ValueError, KeyError, TypeError):
                pass
        # qsub output is saved before JSON registration. Recover accepted IDs
        # read-only if the JSON update failed or the submitter was interrupted.
        for journal in sorted(path.parent.glob('attempts/*/job.*.id')):
            jid = metadata_text(journal).strip()
            if not re.match(r'^\d+(?:\[[0-9-]*\])?\.[A-Za-z0-9_.-]+$', jid):
                continue
            if jid in bindings:
                continue  # A registered binding does not need journal recovery.
            generation = int(journal.name.split('.')[1])
            parent = journal.parent / ('job.{}.id'.format(generation - 1))
            bindings.setdefault(jid, dict(id=jid, role='continuation' if generation else 'main',
                generation=generation, parent_job_id=metadata_text(parent).strip() if parent.exists() else None,
                attempt=str(journal.parent.relative_to(task['root'])),
                log=str(Path(task['root']) / 'outputs/logs' / ('train.{}.cont{}.log'.format(jid.split('.')[0], generation)))))
        value['jobs'] = list(bindings.values())
        return value
    value = json.loads(metadata_text(path))
    if isinstance(value, list):
        candidates = [r for r in value if r.get('task_id') == task.get('task_id')
                      or (task.get('dimension') and r.get('task') == task['dimension'])]
        if len(candidates) != 1:
            raise ValueError('Task receipt is ambiguous')
        value = candidates[0]
    if not isinstance(value, dict):
        raise ValueError('Invalid task receipt')
    return value


@snapshot_cached(lambda task, job: (task['root'], id(job)))
def submission_environment(task, job):
    """Read literal identity fields from the recorded PBS script; never execute it."""
    env = dict(job.get('Variable_List', {}))
    try:
        args = shlex.split(job.get('Submit_arguments', ''))
    except ValueError:
        return env
    root = resolved_path(task['root'])
    for arg in args:
        path = Path(arg)
        if not path.is_absolute():
            if env.get('PBS_O_WORKDIR', '').rstrip('/') != str(root):
                continue
            path = root / path
        try:
            # Resolve each distinct path once per query, retaining support for
            # script aliases whose symlink target has the .pbs suffix.
            path = resolved_path(path)
            if root not in path.parents or path.suffix != '.pbs' or not path.is_file() or path.stat().st_size >= 1024 * 1024:
                continue
            for line in path.read_text(errors='replace').splitlines():
                match = re.match(r'^\s*(?:export\s+)?(MODEL|MODEL_NAME|HPC_TASK_ID|HPC_RUN_ID|HPC_JOB_ROLE|EVAL_SUITE_TASK|EXPERIMENT_VARIANT)=(.*)$', line)
                if not match or any(c in match.group(2) for c in ('$','`',';')):
                    continue
                value = shlex.split(match.group(2), comments=True)
                if len(value) == 1:
                    env.setdefault(match.group(1), value[0])
        except (OSError, ValueError):
            continue
    return env


def matches(task, job):
    if job.get('_id') in task.get('_excluded_automatic_ids', set()):
        return False
    text = job.get('Submit_arguments', '') + ' ' + job.get('Variable_List', {}).get('PBS_O_WORKDIR', '')
    if any(value in job.get('Submit_arguments', '')
           for value in task.get('exclude_submit_argument_substrings', [])):
        return False
    env = submission_environment(task, job)
    same_root = env.get('PBS_O_WORKDIR', '').rstrip('/') == task['root'].rstrip('/')
    if task.get('registry') and env.get('PBS_O_WORKDIR'):
        same_root = resolved_path(env['PBS_O_WORKDIR']) == resolved_path(task['root'])
    reference_id = referenced_job_id(task)
    if task.get('task_id') and env.get('HPC_TASK_ID') and env['HPC_TASK_ID'] != task['task_id']:
        if reference_id == job.get('_id') and same_root:
            return True
        return reservation_job_matches(task, job, reservation_receipt(task))
    if task.get('run_id') and env.get('HPC_RUN_ID') and env['HPC_RUN_ID'] != task['run_id']:
        if reference_id == job.get('_id') and same_root:
            return True
        return reservation_job_matches(task, job, reservation_receipt(task))
    if reference_id and reference_id == job.get('_id') and env.get('PBS_O_WORKDIR', '').rstrip('/') == task['root'].rstrip('/'):
        return True
    if task.get('task_id'):
        if env.get('HPC_TASK_ID') and env['HPC_TASK_ID'] != task['task_id']:
            return False
        if env.get('HPC_TASK_ID') == task['task_id'] and (not task.get('registry') or env.get('HPC_RUN_ID') == task['run_id']):
            if same_root or not task.get('registry'):
                return same_root
        if task.get('receipt'):
            try:
                receipt = task_receipt(task)
                binding = next((r for r in receipt['jobs'] if r['id'] == job.get('_id')), None)
                recorded_workdir = binding.get('submission_workdir') if binding else None
                return (binding is not None and (not task.get('registry') or same_root or
                        recorded_workdir and resolved_path(recorded_workdir) == resolved_path(env.get('PBS_O_WORKDIR', '/'))))
            except (OSError, ValueError, KeyError):
                return False
    if task.get('dimension'):
        return env.get('EVAL_SUITE_TASK') == task['dimension']
    if task['root'] not in text:
        return False
    if task.get('submit_scripts'):
        try:
            args = shlex.split(job.get('Submit_arguments', ''))
        except ValueError:
            return False
        relative = set(task['submit_scripts'])
        absolute = {str(Path(task['root']) / script) for script in relative}
        same_root = env.get('PBS_O_WORKDIR', '').rstrip('/') == task['root'].rstrip('/')
        return bool(absolute.intersection(args) or (same_root and relative.intersection(args)))
    if task.get('model'):
        models = {env[k] for k in ('MODEL_NAME','MODEL') if env.get(k)}
        return models == {task['model']} if models else ('.' + task['model'] + '.') in text
    if task.get('variant'):
        return env.get('EXPERIMENT_VARIANT') == task['variant']
    return True


def tail(path, limit=65536):
    try:
        with Path(path).open('rb') as f:
            f.seek(0, 2)
            f.seek(max(0, f.tell() - limit))
            return f.read().decode('utf-8', errors='replace')
    except OSError:
        return ''


def latest_log(task, gpu_jobs):
    root = Path(task['root'])
    candidates = list(glob_paths(root, task['log_glob']))
    if task.get('current_log_only'):
        # Match the actual allocation, never an earlier attempt's failure log.
        numbers = [i.split('.')[0] for i, _ in gpu_jobs]
        # A receipt's main_job_id names the chain's first job; its successors hold newer logs.
        if not numbers and task.get('receipt'):
            try:
                receipt = task_receipt(task)
                bindings = [r for r in receipt.get('jobs', []) if r.get('role') in {'main','continuation'} and r.get('log')]
                existing = [Path(r['log']) for r in bindings if Path(r['log']).is_file() and root in Path(r['log']).parents]
                if existing:
                    candidates = [max(existing, key=lambda p: p.stat().st_mtime)]
                    numbers = [r['id'].split('.')[0] for r in bindings if Path(r['log']) == candidates[0]]
            except (OSError, ValueError, KeyError):
                pass
        if not numbers and referenced_job_id(task):
            numbers = [referenced_job_id(task).split('.')[0]]
        candidates = [p for p in candidates if any(re.search(r'(?<!\d)' + re.escape(n) + r'(?!\d)', p.name) for n in numbers)]
        for _, job in gpu_jobs:
            output = job.get('Output_Path', '').partition(':')[2]
            p = Path(output) if output else None
            if p and p.is_file() and root in p.parents:
                started = job.get('stime') or job.get('ctime')
                try:
                    fresh = not started or p.stat().st_mtime >= time.mktime(time.strptime(started, '%a %b %d %H:%M:%S %Y'))
                except ValueError:
                    fresh = False
                if fresh:
                    candidates.append(p)
    # Generic LMWeather names do not identify variants; select by GPU job ID.
    if task.get('variant') and gpu_jobs:
        numbers = [i.split('.')[0] for i, _ in gpu_jobs]
        candidates = [p for p in candidates if any('.' + n + '.' in p.name for n in numbers)]
    # An empty PBS spool can be newer than a populated application log from the
    # same allocation. Keep the current-job filter above, then prefer real output.
    populated = [p for p in candidates if p.stat().st_size]
    return max(populated or candidates, key=lambda p: p.stat().st_mtime) if candidates else None


def event_line(line):
    """Read JSON events with or without an entrypoint's uppercase prefix."""
    prefix = ''
    try:
        row = json.loads(line)
    except ValueError:
        prefix, separator, payload = line.partition(' ')
        if not separator or not re.fullmatch(r'[A-Z][A-Z0-9_]*', prefix):
            return {}
        try:
            row = json.loads(payload)
        except ValueError:
            return {}
    if not isinstance(row, dict):
        return {}
    if prefix and not row.get('event'):
        row = dict(row, event=prefix)
    return row


def latest_log_message(text):
    """Show one real line when the application has no recognized progress event."""
    for line in reversed(text.splitlines()):
        line = re.sub(r'\x1b\[[0-?]*[ -/]*[@-~]', '', line).strip()
        if not line or re.search(r'(?:api[_-]?key|access[_-]?token|authorization|password|secret)[\"\']?\s*[:=]', line, re.I):
            continue
        return '最新日志：' + ' '.join(line.split())[:160]
    return ''


def event_counts(row):
    try:
        completed = int(row.get('completed', row.get('recovered', 0)))
        errors = int(row.get('errors', 0))
        processed = int(row.get('processed', completed + errors))
        target = int(row['target_count'])
        pending = int(row.get('pending', target - processed))
        if min(completed, errors, processed, pending) < 0 or completed + errors != processed or processed + pending != target:
            return None
        return completed, errors, processed, target, pending
    except (KeyError, TypeError, ValueError, OverflowError):
        return None


def handled_event(row):
    if row.get('event') == 'RECOVERY_VIDEO_SAVED':
        return event_counts(row) is not None and not row.get('fatal')
    if row.get('event') == 'RECOVERY_ADMISSION_WAIT':
        return (type(row.get('attempt')) is int and row['attempt'] > 0 and
                any(word in str(row.get('error', '')).lower() for word in
                    ('would exceed', 'max_queued', 'maximum number', 'queue limit')))
    return False


def log_summary(text):
    lines = text.splitlines()
    events = [(index, event_line(line)) for index, line in enumerate(lines)]
    missing = re.search(r'EXPANSION_INCOMPLETE missing_sources=(\d+)', text)
    if missing:
        return '', '缺少 {} 个视频来源，未全量完成'.format(missing.group(1))
    # These events explicitly confirm that an input error was saved and processing
    # continued, or that quota admission is being retried by its registered control.
    # Errors after that boundary remain fatal; ordinary heartbeat events never
    # suppress a traceback or GPU initialization failure.
    handled = max((index for index, row in events if handled_event(row)), default=-1)
    for line in reversed(lines[handled + 1:]):
        if re.search(r'(SMOKE_FAILED|TASK_FAILED|GPU_SELECTION_FAILED|Fatal exit|exceed queue|Killed|Traceback \(most recent call last\):|^[A-Za-z_]*(?:Error|Exception):)', line):
            return '', line.strip()[:110]
    for _, row in reversed(events):
        try:
            event = row.get('event', '')
            if event in {'AG_ALLOCATED_PROGRESS', 'RECOVERY_VIDEO_SAVED', 'RECOVERY_SLICE_FINISHED'} and 'target_count' in row:
                counts = event_counts(row)
                if counts is None:
                    continue
                completed, errors, processed, target, pending = counts
                progress = '已处理 {}/{}，成功 {}，错误 {}，待处理 {}'.format(processed, target, completed, errors, pending)
                if row.get('skipped_oom'):
                    progress += '，OOM 跳过 {}'.format(row['skipped_oom'])
                if row.get('video_id'):
                    progress += '；Video ID {}'.format(row['video_id'])
                return progress, ''
            if event == 'NC_COOPERATIVE_VIDEO_COMPLETE':
                return '本段成功 {}，错误 {}；Video ID {}'.format(row['completed'], row['errors'], row['video_id']), ''
            if event in {'NC_COOPERATIVE_SMOKE_VALIDATED', 'AG_V4_RETRY_GPU_SMOKE_VALIDATED'}:
                prefix = 'GPU smoke' if event.startswith('AG_') else 'smoke'
                return '{} 已通过；Video ID {}'.format(prefix, row['video_id']), ''
            if event in {'AG_V4_FAILED_RETRY_VIDEO_COMPLETE'}:
                return '已处理 {}/{}，恢复 {}，错误 {}；Video ID {}'.format(
                    row['completed'], row['target_count'], row['recovered'], row['errors'], row['video_id']), ''
            if event == 'RECOVERY_ADMISSION_WAIT':
                return '等待 GPU 队列名额，第 {} 次尝试'.format(row['attempt']), ''
            if row.get('event') == 'MICROLENS100K_WAIT_AUDITOR_VIDEO':
                return '等待 Auditor Video ID {}，已等待 {} 秒'.format(row['video_id'], row['wait_seconds']), ''
            if row.get('event') == 'MICROLENS100K_AUDITOR_REUSED':
                return 'Video ID {}，复用已有 Auditor 报告'.format(row['video_id']), ''
            if row.get('event') in {'SHARED_AUDITOR_VIDEO_COMPLETE', 'SHARED_AUDITOR_SHARD_VIDEO_COMPLETE'}:
                return 'Video ID {}，已完成 {}/{}'.format(row['video_id'], row['completed'], row['target_count']), ''
            if row.get('event') in {'AG_VIDEO_COMPLETE','AG_VIDEO_START','NC_VIDEO_COMPLETE','NC_VIDEO_START',
                                     'ASR_CMP_VIDEO_COMPLETE','ASR_SEQ_VIDEO_COMPLETE',
                                     'MICROLENS100K_NC_VIDEO_COMPLETE','MICROLENS100K_NC_VIDEO_START',
                                     'MICROLENS100K_AG_VIDEO_COMPLETE','MICROLENS100K_AG_VIDEO_START'}:
                return 'Video ID {}，已完成 {}'.format(row['video_id'],row['completed']), ''
            if row.get('variant') and 'round' in row and 'epoch' in row:
                return '第 {} 轮，{}，epoch {}'.format(row['round'], row.get('client', '-'), row['epoch']), ''
        except (ValueError, AttributeError, KeyError, TypeError, OverflowError):
            pass
    scored = []
    for _, row in events:
        try:
            if row.get('status') == 'ok' and row.get('video_id'):
                scored.append(str(row['video_id']))
        except (ValueError, AttributeError):
            pass
    if scored:
        return 'Video ID ' + scored[-1], ''
    if 'MICROLENS50K_SMOKE_VALIDATED' in text:
        return 'smoke 已通过', ''
    for line in reversed(lines[handled + 1:]):
        if re.search(r'(Traceback|Error:|Exception:|SMOKE_FAILED|TASK_FAILED|Fatal exit|exceed queue|Killed)', line):
            return '', line.strip()[:110]
    for line in reversed(text.splitlines()):
        if line.startswith(('UNIPERCEPT_PROGRESS ', 'UNIPERCEPT_PROCESSING_COMPLETE ')):
            return line.strip()[:110], ''
        if line.startswith('Completed trials:'):
            return line.strip(), ''
        if line.startswith(('Max epochs', 'Budget:', 'Scheduling continuation', 'Strategy:')):
            continue
        if re.search(r'(epoch|trial|ARTIMUSE_FRAME_PROGRESS|Loading checkpoint|WAIT_EXTRACTION_LOCK)', line, re.I):
            return line.strip()[:80], ''
    return '', ''


def verify_result_files(records, path_key='result_path', hash_key='sha256', auditor=False):
    """Validate every result using one read, with bounded shared-filesystem I/O.

    Keep at most 32 outstanding checks and eight workers, even for full datasets.
    No file contents or validation results survive the current query.
    """
    import hashlib

    def verify(row):
        try:
            data = Path(row[path_key]).read_bytes()
            if hashlib.sha256(data).hexdigest() != row[hash_key]:
                return False
            if auditor:
                value = json.loads(data.decode('utf-8'))
                return (set(value) == {'overall_summary', 'segments'}
                        and isinstance(value['overall_summary'], str)
                        and bool(value['overall_summary'].strip())
                        and isinstance(value['segments'], list) and bool(value['segments']))
            return True
        except (OSError, ValueError, KeyError, TypeError):
            return False

    pending = iter(records)
    first = list(islice(pending, 32))
    if len(first) < 8:
        return all(verify(row) for row in first)
    with ThreadPoolExecutor(max_workers=8) as pool:
        batch = first
        while batch:
            if not all(pool.map(verify, batch)):
                return False
            batch = list(islice(pending, 32))
    return True


@snapshot_cached(lambda task: id(task))
def completion(task):
    root = Path(task['root'])
    marker = task.get('done')
    artifact = task.get('artifact')
    if task.get('registry'):
        try:
            if task.get('external_submission'):
                if task.get('native_run_receipt'):
                    native = json.loads(Path(task['native_run_receipt']).read_text())
                    return (native.get('status') == 'success' and native.get('exit_code') == 0
                            and native.get('job_id') in {r['id'] for r in task_receipt(task)['jobs']})
                if not marker or not (root / marker).is_file() or (root / marker).stat().st_mtime < task['submitted_after']:
                    return False
                if artifact:
                    native = dict(task)
                    native.pop('registry')
                    return completion(native)
                return True  # Application-owned marker is written after its validation.
            proof = json.loads((root / marker).read_text())
            return (proof['run_id'] == task['run_id'] and proof['task_id'] == task['task_id']
                    and proof['started_at'] >= task['submitted_after']
                    and proof['completed_at'] >= proof['started_at']
                    and (not artifact or (root / artifact).is_file()))
        except (OSError, ValueError, KeyError, TypeError):
            return False
    if not marker or not (root / marker).is_file() or not artifact or not (root / artifact).is_file():
        return False
    if task.get('artifact_validation') == 'shared_auditor':
        try:
            import hashlib
            result = json.loads((root / artifact).read_text())
            proof = json.loads((root / marker).read_text())
            successful = result['results']
            failures = result['failures']
            success_ids = [str(row['video_id']) for row in successful]
            failed_ids = [str(row['video_id']) for row in failures]
            all_ids = success_ids + failed_ids
            expected = [str(i) for i in range(1, task['target_count'] + 1)]
            if task.get('expected_ids_file'):
                inventory = json.loads((root / task['expected_ids_file']).read_text())
                expected = sorted([str(x['video_id'] if isinstance(x, dict) else x)
                                   for x in inventory], key=int)
            if task.get('video_id_modulo'):
                shard = task['video_id_modulo']
                expected = [str(i) for i in range(1, shard['total_count'] + 1)
                            if i % shard['modulus'] == shard['remainder']]
            return (result.get('complete') is True and result.get('run_id') == task['run_id']
                    and result.get('target_count') == task['target_count']
                    and result.get('processed_count') == task['target_count']
                    and result.get('completed_count') == len(successful)
                    and result.get('failed_count') == len(failures)
                    and len(all_ids) == len(set(all_ids)) == task['target_count']
                    and sorted(all_ids, key=int) == expected
                    and all(row.get('error_type') and row.get('error') for row in failures)
                    and proof.get('status') in {'complete', 'complete_with_skips'}
                    and proof.get('run_id') == task['run_id']
                    and proof.get('target_count') == task['target_count']
                    and proof.get('completed_count') == len(successful)
                    and proof.get('failed_count') == len(failures)
                    and proof.get('results_sha256') == hashlib.sha256((root / artifact).read_bytes()).hexdigest()
                    and not (root / task.get('failure_marker', '')).is_file()
                    and verify_result_files(successful, path_key='auditor_path', auditor=True))
        except (OSError, ValueError, KeyError, TypeError):
            return False
    if task.get('artifact_validation') == 'paired_scores':
        try:
            import hashlib
            report = json.loads((root / artifact).read_text())
            proof = json.loads((root / marker).read_text())
            records = report['results']
            return (report.get('complete') is True and report['run_id'] == proof['run_id'] == task['run_id']
                    and report['target_count'] == task['target_count'] == len(records)
                    and [r['video_id'] for r in records] == task['expected_ids']
                    and all(((type(r[m]['score']) is int and 1 <= r[m]['score'] <= 5)
                             or (task.get('allow_null_scores') is True
                                 and report.get('allow_null_scores') is True
                                 and proof.get('allow_null_scores') is True
                                 and r[m]['score'] is None
                                 and r[m].get('status') == 'insufficient_evidence'
                                 and isinstance(r[m].get('reason'), str) and r[m]['reason'].strip()))
                            and hashlib.sha256(Path(r[m]['result_path']).read_bytes()).hexdigest() == r[m]['sha256']
                            for r in records for m in task['score_dimensions'])
                    and (not task.get('shared_observer') or all(
                        hashlib.sha256(Path(r['shared_observer_path']).read_bytes()).hexdigest() == r['shared_observer_sha256']
                        for r in records))
                    and proof['results_sha256'] == hashlib.sha256((root / artifact).read_bytes()).hexdigest())
        except (OSError, ValueError, KeyError, TypeError):
            return False
    if task.get('artifact_validation') == 'asr_media':
        try:
            import hashlib
            report = json.loads((root / artifact).read_text())
            proof = json.loads((root / marker).read_text())
            expected = task['target_count']
            flags = (root / artifact).parent / 'asr_flags.json'
            return (report.get('complete') is True and proof.get('complete') is True
                    and report['run_id'] == proof['run_id'] == task['run_id']
                    and report['target_count'] == proof['target_count'] == expected
                    and report['completed'] == proof['completed'] == expected
                    and report['errors'] == proof['errors'] == 0
                    and proof['flags_sha256'] == hashlib.sha256(flags.read_bytes()).hexdigest())
        except (OSError, ValueError, KeyError, TypeError):
            return False
    if task.get('artifact_validation') == 'audiobox_ce':
        try:
            import hashlib
            result = json.loads((root / artifact).read_text())
            proof = json.loads((root / marker).read_text())
            rows = result['videos']
            return (result['complete'] is True and result['run_id'] == proof['run_id'] == task['run_id']
                    and result['target_count'] == len(rows) == 19220
                    and len(set(r['video_id'] for r in rows)) == 19220
                    and all((r['status'] == 'ok' and isinstance(r['CE'], (int,float)) and math.isfinite(r['CE']))
                            or (r['status'] in ('no_audio','error') and r['CE'] is None and r.get('error')) for r in rows)
                    and proof['results_sha256'] == hashlib.sha256((root / artifact).read_bytes()).hexdigest())
        except (OSError, ValueError, KeyError, TypeError):
            return False
    if task.get('artifact_validation') in {'unipercept_iqa', 'unipercept_iaa'}:
        try:
            import hashlib
            path = root / artifact
            report = json.loads(path.read_text())
            receipt = json.loads((root / marker).read_text())
            rows = report['results']
            return (report.get('complete') is True and report.get('run_id') == task['run_id']
                    and report.get('target_count') == task.get('target_count', 19220) and len(rows) == task.get('target_count', 19220)
                    and len({row['video_id'] for row in rows}) == task.get('target_count', 19220)
                    and all(row['status'] == 'success' and math.isfinite(row['iaa' if task.get('artifact_validation') == 'unipercept_iaa' else 'iqa'])
                            and 0 <= row['iaa' if task.get('artifact_validation') == 'unipercept_iaa' else 'iqa'] <= 100 and 1 <= row['frame_count'] <= 16 for row in rows)
                    and receipt['run_id'] == task['run_id']
                    and receipt['results_sha256'] == hashlib.sha256(path.read_bytes()).hexdigest())
        except (OSError, ValueError, KeyError, TypeError):
            return False
    if task.get('artifact_validation') == 'json_records':
        try:
            import hashlib
            report = json.loads((root / artifact).read_text())
            proof = json.loads((root / marker).read_text())
            manifest = json.loads((root / task['expected_items']).read_text())
            expected = [r['video_id'] for r in manifest['items']]
            records = report['results']
            return (report.get('complete') is True and report['run_id'] == task['run_id']
                    and report['target_count'] == task['target_count'] == len(expected)
                    and [r['video_id'] for r in records] == sorted(expected,key=int)
                    and all(type(r['score']) is int and 1 <= r['score'] <= 5 for r in records)
                    and proof['results_sha256'] == hashlib.sha256((root/artifact).read_bytes()).hexdigest()
                    and verify_result_files(records))
        except (OSError,ValueError,KeyError,TypeError):
            return False
    if task.get('artifact_validation') == 'json_sample_bundle':
        try:
            import hashlib
            report = json.loads((root / artifact).read_text())
            proof = json.loads((root / marker).read_text())
            expected = [r['video_id'] for r in json.loads((root / task['expected_items']).read_text())['items']]
            return (report.get('complete') is True and report['run_id'] == task['run_id'] == proof['run_id']
                    and report['target_count'] == len(expected)
                    and [r['video_id'] for r in report['videos']] == expected
                    and proof['results_sha256'] == hashlib.sha256((root/artifact).read_bytes()).hexdigest())
        except (OSError,ValueError,KeyError,TypeError):
            return False
    if task.get('artifact_validation') == 'timeffm_newdomains_metrics':
        try:
            failed = root / task['failure_marker']
            if failed.is_file() and failed.stat().st_mtime_ns >= (root / marker).stat().st_mtime_ns:
                return False
            with (root / artifact).open(newline='') as stream:
                rows = list(csv.DictReader(stream))
            expected = {(source, target, horizon)
                        for source in ('ETTh1', 'ETTh2', 'ETTm1', 'ETTm2', 'Weather', 'Electricity', 'Exchange')
                        for target in ('solar', 'traffic') for horizon in (96, 192, 336, 720)}
            observed = {(r['source'], r['target'], int(r['eval_horizon'])) for r in rows}
            return observed == expected and len(rows) == len(expected) and all(
                math.isfinite(float(r[k])) and float(r[k]) >= 0 for r in rows for k in ('mse', 'mae'))
        except (OSError, ValueError, KeyError, TypeError, csv.Error):
            return False
    if task.get('artifact_validation') == 'tq_split_control':
        try:
            report = json.loads((root / artifact).read_text())
            proof = json.loads((root / marker).read_text())
            return (report.get('complete') is True and proof['run_id'] == task['run_id']
                    and proof['target_count'] == report['target_count'] == task['target_count']
                    and proof['counts'] == report['counts'])
        except (OSError, ValueError, KeyError, TypeError):
            return False
    if task.get('artifact_validation') == 'tq_shard':
        try:
            report = json.loads((root / artifact).read_text())
            proof = json.loads((root / marker).read_text())
            group = json.loads((root / task['expected_items']).read_text())
            ids = [r['video_id'] for r in group['items']]
            return (report.get('complete') is True and proof['run_id'] == group['run_id'] == task['run_id']
                    and proof['recipe'] == group['recipe']
                    and proof['target_count'] == report['target_count'] == len(ids)
                    and [r['video_id'] for r in report['records']] == ids
                    and all(r['statuses']['TQ'] == 'ok' and math.isfinite(float(r['scores']['TQ'])) for r in report['records']))
        except (OSError, ValueError, KeyError, TypeError):
            return False
    if task.get('dimension') or task.get('artifact_dimension') or task.get('task_id') in {'microlens50k_asr','microlens1m_asr'}:
        try:
            r = json.loads((root / artifact).read_text())
            return r.get('complete') is True and r.get('target_count') == task.get('target_count')
        except (ValueError, OSError):
            return False
    return True


def unipercept_processed(task):
    if task.get('artifact_validation') not in {'unipercept_iqa', 'unipercept_iaa'}:
        return False
    try:
        import hashlib
        root = Path(task['root'])
        path = root / task['artifact']
        result = json.loads(path.read_text())
        marker = json.loads((root / task['processing_done']).read_text())
        successful, errors = result['results'], result['errors']
        rows = successful + errors
        return (result.get('processing_complete') is True and result['target_count'] == task.get('target_count', 19220)
                and result['run_id'] == marker['run_id'] == task['run_id']
                and marker['target_count'] == len(rows) == task.get('target_count', 19220)
                and marker['completed_videos'] == len(successful) and marker['skipped_videos'] == len(errors)
                and len({r['video_id'] for r in rows}) == task.get('target_count', 19220)
                and all(r['status'] == 'success' and math.isfinite(r['iaa' if task.get('artifact_validation') == 'unipercept_iaa' else 'iqa']) and 0 <= r['iaa' if task.get('artifact_validation') == 'unipercept_iaa' else 'iqa'] <= 100
                        and 1 <= r['frame_count'] <= 16 for r in successful)
                and all(r['status'] == 'failed' and r.get('video_path') and isinstance(r.get('title'), str)
                        and r.get('error') for r in errors)
                and marker['results_sha256'] == hashlib.sha256(path.read_bytes()).hexdigest())
    except (OSError, ValueError, KeyError, TypeError):
        return False


def artifact_progress(task, log):
    """Describe incomplete artifacts without promoting them to completion."""
    path = Path(task['root']) / (task.get('artifact') or '')
    if task.get('artifact_validation') == 'shared_auditor':
        try:
            report = json.loads(path.read_text()) if path.is_file() else {}
            checkpoint_path = Path(task['root']) / task.get('checkpoint', '')
            checkpoint = json.loads(checkpoint_path.read_text()) if checkpoint_path.is_file() else {}
            target = int(task['target_count'])
            if not report and not checkpoint:
                return '', '', None
            if report.get('complete') is True:
                completed = int(report['completed_count'])
                failed = int(report['failed_count'])
                processed = int(report['processed_count'])
            else:
                completed = int(checkpoint.get('completed', 0))
                failed = int(checkpoint.get('failed', 0))
                processed = int(checkpoint.get('processed', completed + failed))
            if min(completed, failed, processed) < 0 or processed > target or completed + failed > processed:
                return '', 'Auditor 进度文件与目标数量不一致', None
            pending = target - processed
            failures = report.get('failures')
            if failures is None:
                ledger = checkpoint_path.parent / 'video_failures.json'
                failures = json.loads(ledger.read_text()) if ledger.is_file() else []
            detail = dict(completed=completed, errors=failed, processed=processed,
                          pending=pending, target_count=target,
                          skipped_video_ids=[str(row.get('video_id')) for row in failures])
            text = '已处理 {}/{}，成功 {}，跳过 {}，待处理 {}'.format(
                processed, target, completed, failed, pending)
            error = '存在单视频跳过记录' if failed and report.get('complete') is not True else ''
            return text, error, detail
        except (OSError, ValueError, KeyError, TypeError):
            return '', '', None
    if task.get('artifact_validation') == 'paired_scores':
        try:
            report = json.loads(path.read_text())
            done, errors, target = report['completed'], report['errors'], report['target_count']
            if target != task['target_count'] or not 0 <= done + errors <= target:
                raise ValueError('Invalid paired-score progress')
            detail = dict(completed=done, errors=errors, target_count=target, pending=target-done-errors)
            progress = 'AG+NC {}/{}，错误 {}'.format(done, target, errors)
            if report.get('allow_null_scores'):
                detail.update(null_scores=report.get('null_scores', {}), valid_pairs=report.get('valid_pairs', 0))
                progress += '，有效配对 {}，null AG {} / NC {}'.format(
                    detail['valid_pairs'], detail['null_scores'].get('AG', 0), detail['null_scores'].get('NC', 0))
            return progress, '存在失败记录' if errors else '', detail
        except (OSError, ValueError, KeyError, TypeError):
            return '', '', None
    if task.get('artifact_validation') == 'json_records':
        try:
            report = json.loads(path.read_text())
            target = int(report['target_count'])
            completed = int(report['completed'])
            errors = int(report['errors'])
            processed = int(report['processed'])
            pending = int(report['pending'])
            if (target != task.get('target_count') or completed + errors != processed
                    or processed + pending != target or min(completed, errors, pending) < 0):
                raise ValueError('Invalid JSON-record progress')
            info = dict(completed=completed, errors=errors, processed=processed,
                        pending=pending, target_count=target,
                        last_video_id=report['results'][-1]['video_id'] if report.get('results') else None)
            progress = '{} {}/{}，错误 {}，待处理 {}'.format(
                task.get('dimension', '评分'), completed, target, errors, pending)
            return progress, '存在失败记录' if errors else '', info
        except (OSError, ValueError, KeyError, TypeError, IndexError):
            return '', '', None
    if task.get('artifact_validation') == 'asr_media':
        try:
            current = json.loads((Path(task['root']) / task['checkpoint']).read_text())
            target = int(current['target_count'])
            done, errors = int(current['completed']), int(current['errors'])
            if target != task['target_count'] or not 0 <= done <= target or not 0 <= errors <= target - done:
                return '', 'ASR 进度与任务清单不一致', None
            pending = target - done - errors
            repeated = int(current.get('repetition_flagged', 0))
            info = dict(completed=done, target_count=target, errors=errors, pending=pending,
                        repetition_flagged=repeated, last_video_id=current.get('last_video_id'))
            text = '已处理 {}/{}，错误 {}，待处理 {}，疑似重复 {}'.format(done,target,errors,pending,repeated)
            return text, '存在 ASR 失败记录' if errors else '', info
        except (OSError, ValueError, KeyError, TypeError):
            return '', '', None
    dimension = task.get('artifact_dimension') or task.get('dimension')
    if not path.is_file() or not (dimension or task.get('task_id') in {'microlens50k_asr','microlens1m_asr'}):
        return '', '', None
    try:
        report = json.loads(path.read_text())
        target = int(report['target_count'])
        skipped = 0
        if task.get('target_count') and target != task['target_count']:
            return '', '结果数量与任务清单不一致', None
        if dimension:
            counts = report['counts'][dimension]
            ok = int(counts.get('ok',0)) + int(counts.get('not_applicable',0))
            skipped = int(counts.get('skipped', 0))
            errors = sum(int(v) for k,v in counts.items() if k not in {'ok','not_applicable','pending','missing_source','skipped'})
            pending = int(counts.get('pending',0))
        else:
            ok = int(report['completed'])
            errors = len(report.get('error_ids',[]))
            pending = max(0,target-ok-errors)
        stale = bool(log and log.stat().st_mtime > path.stat().st_mtime + 2)
        if not dimension and task.get('checkpoint'):
            checkpoint = Path(task['root']) / task['checkpoint']
            if checkpoint.is_file():
                current = json.loads(checkpoint.read_text())
                if (current.get('completed') == ok and current.get('target_count') == target
                        and set(current.get('error_ids',[])) == set(report.get('error_ids',[]))):
                    stale = False
        missing_sources = len(report.get('unresolved_sources', {}))
        if dimension and task.get('checkpoint'):
            checkpoint = Path(task['root']) / task['checkpoint']
            if checkpoint.is_file():
                current = json.loads(checkpoint.read_text())
                if (current.get('processed') == ok+errors+pending and current.get('expected') == target
                        and missing_sources and log
                        and 'EXPANSION_INCOMPLETE missing_sources={}'.format(missing_sources) in tail(log)):
                    stale = False  # Final log is written after the matching full summary.
        info = dict(missing_sources=missing_sources, skipped=skipped, completed=ok, errors=errors, pending=pending, target_count=target, summary_stale=stale)
        progress = '{}成功 {}/{}，错误 {}，待处理 {}'.format('历史汇总：' if stale else '',ok,target,errors,pending)
        reasons = []
        if skipped:
            progress += '，已跳过 {}'.format(skipped)
        if missing_sources:
            progress += '，缺源 {}'.format(missing_sources)
            reasons.append('缺少 {} 个视频来源'.format(missing_sources))
        if stale:
            reasons.append('汇总早于最新日志，需刷新')
        if errors:
            reasons.append('存在失败记录')
        if report.get('complete') is not True and not reasons:
            reasons.append('全量结果尚未完成')
        if report.get('complete') is True and not completion(task):
            reasons.append('缺少有效完成标记')
        return progress, '；'.join(reasons), info
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return '', '结果汇总无法校验', None


def reservation_phase(task, jobs):
    """Read an opted-in, identity-bound GPU reservation phase without mutation."""
    receipt = reservation_receipt(task)
    if not receipt:
        return {}
    try:
        job_id = receipt.get('main_job_id')
        job = jobs.get(job_id, {})
        if not reservation_job_matches(task, dict(job, _id=job_id), receipt):
            return {}
        return dict(execution_phase=receipt['execution_phase'], reservation_job_id=job_id,
                    wait_for_job_id=receipt.get('wait_for_job_id'),
                    progress=receipt.get('execution_phase_progress'))
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        return {}


def registered_failure(task):
    """Accept current native/workload failure markers as well as adapter failures."""
    try:
        path = Path(task['root']) / task['failure_marker']
        failure = json.loads(metadata_text(path))
        timestamp = next((failure[key] for key in ('started_at', 'failed_at', 'updated_at', 'ended_at')
                          if isinstance(failure.get(key), (int, float))), path.stat().st_mtime)
        if timestamp < task.get('submitted_after', 0):
            return ''
        if failure.get('job_id') and failure['job_id'] not in {
                binding.get('id') for binding in task_receipt(task).get('jobs', [])}:
            return ''
        for key, workload_key in (('task_id', 'workload_task_id'), ('run_id', 'workload_run_id')):
            if failure.get(key) and failure[key] not in {task.get(key), task.get(workload_key)}:
                return ''
        return (str(failure.get('error') or '') or
                '作业退出码 {}'.format(failure.get('exit_status', failure.get('exit_code', '未知'))))
    except (OSError, ValueError, KeyError, TypeError):
        return ''


@query_snapshot
def build_rows(tasks, jobs):
    rows = []
    assigned = set()
    for jid, job in jobs.items():
        job['_id'] = jid
    for task in tasks:
        phase = reservation_phase(task, jobs)
        waiting_reservation = phase.get('execution_phase') == 'waiting_for_ag'
        is_control = task.get('kind') == 'control' or waiting_reservation
        gpu_resource_hold = bool(task.get('gpu_resource_hold') or waiting_reservation)
        is_cpu_compute = task.get('compute_device') == 'cpu'
        automatic_compute = task.get('registry') and not is_control
        registered_roles = {}
        if task.get('receipt') and not is_control:
            try:
                registered_roles = {r['id']: r.get('role') for r in task_receipt(task)['jobs']}
            except (OSError, ValueError, KeyError, TypeError):
                pass
        gpu = [(i, j) for i, j in jobs.items() if
               (automatic_compute or ((int(j.get('Resource_List', {}).get('ngpus', 0)) > 0 if gpu_resource_hold else
                                       int(j.get('Resource_List', {}).get('ngpus', 0)) == 0) if is_control or is_cpu_compute else
                int(j.get('Resource_List', {}).get('ngpus', 0)) > 0)) and
               registered_roles.get(i) not in {'controller', 'watcher', 'control'} and matches(task, j)]
        ids = {i for i, _ in gpu}
        controls = []
        cpu_compute = []
        relay_bindings = {}
        if (not is_control or waiting_reservation) and task.get('receipt'):
            try:
                receipt = task_receipt(task)
                for binding in receipt.get('jobs', []):
                    jid = binding.get('id')
                    if (binding.get('role') == 'controller' and jid in jobs
                            and int(jobs[jid].get('Resource_List', {}).get('ngpus', 0)) == 0
                            and (not waiting_reservation or (
                                matches(task, jobs[jid])
                                and jobs[jid].get('Variable_List', {}).get('HPC_TASK_ID') == task.get('task_id')
                                and jobs[jid].get('Variable_List', {}).get('HPC_RUN_ID') == task.get('run_id')))):
                        controls.append((jid, jobs[jid]))
                        relay_bindings[jid] = binding
                    elif (not is_cpu_compute and not automatic_compute and binding.get('role') in {'main', 'continuation'}
                          and jid in jobs and int(jobs[jid].get('Resource_List', {}).get('ngpus', 0)) == 0
                          and matches(task, jobs[jid])):
                        cpu_compute.append((jid, jobs[jid]))
            except (OSError, ValueError, KeyError, TypeError):
                pass
        # Controller relation is recorded in relay state; never guess from a
        # shortened name. A CPU R row must not make a GPU task appear running.
        # Automatic records already own explicit job/role relationships. Legacy
        # deferred records can contain an unrelated shared controller in this root.
        for p, s in ([] if is_control or task.get('registry') else deferred_states(task['root'])):
            if s.get('mode') == 'tq_split':
                continue  # The split transition is registered as a shared CPU control group.
            related = s.get('target_job_id') in ids or s.get('gpu_job_id') in ids
            parent_ids = re.findall(r'(\d+\.[A-Za-z0-9_-]+)', s.get('dependency') or '')
            related = related or bool(ids.intersection(parent_ids))
            target = jobs.get(s.get('target_job_id'), {})
            related = related or bool(target and matches(task, target))
            # Frozen continuation arguments are a full receipt, even before
            # a GPU successor exists. Resolve env values rather than PBS names.
            env = {}
            args = s.get('gpu_args', [])
            for n, arg in enumerate(args[:-1]):
                if arg == '-v':
                    env.update(dict(v.split('=', 1) for v in args[n + 1].split(',') if '=' in v))
            synthetic = {'Submit_arguments': ' '.join(args),
                         'Variable_List': dict(env, PBS_O_WORKDIR=s.get('cwd', ''))}
            related = related or (bool(args) and matches(task, synthetic))
            if related:
                parent = parent_ids[0] if len(parent_ids) == 1 else None
                gid = s.get('gpu_job_id')
                if gid in ids:
                    relay_bindings[gid] = dict(id=gid, role='continuation' if parent else 'main', parent_job_id=parent,
                                              controller_job_id=s.get('relay_job_id'))
                rid = s.get('relay_job_id')
                if rid in jobs:
                    relay_bindings[rid] = dict(id=rid, role='controller', parent_job_id=parent, target_job_id=gid,
                                              log=str(p.parent/'relay.log'))
                for key in ['relay_job_id', 'target_job_id']:
                    jid = s.get(key)
                    if jid in jobs and int(jobs[jid].get('Resource_List', {}).get('ngpus', 0)) == 0:
                        controls.append((jid, jobs[jid]))
        controls = list(dict(controls).items())
        assigned.update(i for i, _ in gpu + cpu_compute + controls)
        active_gpu = [(i, j) for i, j in gpu if j['job_state'] in {'R', 'E', 'B'}]
        queued_gpu = [(i, j) for i, j in gpu if j['job_state'] == 'Q']
        held_gpu = [(i, j) for i, j in gpu if j['job_state'] == 'H']
        active_cpu = [(i, j) for i, j in cpu_compute if j['job_state'] in {'R', 'E', 'B'}]
        queued_cpu = [(i, j) for i, j in cpu_compute if j['job_state'] == 'Q']
        held_cpu = [(i, j) for i, j in cpu_compute if j['job_state'] == 'H']
        active = active_gpu + active_cpu
        queued = queued_gpu + queued_cpu
        held = held_gpu + held_cpu
        selected = active or queued or held
        selected_gpu = active_gpu or queued_gpu or held_gpu
        log = latest_log(task, selected or gpu)
        log_text = tail(log) if log else ''
        progress, error = log_summary(log_text)
        if not active and (task.get('task_id') == 'microlens50k_unipercept_iqa' or task.get('artifact_validation') in {'unipercept_iqa', 'unipercept_iaa'}):
            queues = {j['queue'] for _, j in selected}
            if {'med_gpuq', 'large_gpuq'} <= queues:
                progress = 'med_gpuq 与 large_gpuq 同时排队，先分到 GPU 的作业计算 Uni-IQA'
                error = ''
            elif controls and not selected:
                progress = ('正在向 large_gpuq 申请 GPU，复用已完成的 50K IAA'
                            if task.get('task_id') == 'microlens100k_unipercept_iaa' else
                            '等待 IAA GPU 配额，按视频 ID 升序运行'
                            if task.get('artifact_validation') == 'unipercept_iaa' else
                            '正在向 med_gpuq 和 large_gpuq 提交，先分到 GPU 的队列开跑')
                error = ''  # Prior GPU failure logs do not override a newly registered controller.
        artifact_text, artifact_error, artifact_info = artifact_progress(task, log)
        if task.get('registry') and not selected and not controls:
            error = registered_failure(task) or error
        if artifact_text and (not active or not progress or task.get('artifact_validation') in {'asr_media', 'paired_scores', 'shared_auditor'}):
            progress = artifact_text
        if artifact_error and not selected and not controls:
            error = error if error and artifact_error in error else artifact_error + ('；' + error if error else '')
        if is_control and not progress and not error and task.get('receipt'):
            try:
                progress = task_receipt(task).get('progress', '')
            except (OSError, ValueError, KeyError):
                pass
        if not is_control and controls and not selected and not progress and task.get('receipt'):
            try:
                progress = task_receipt(task).get('progress', '')
                error = ''
            except (OSError, ValueError, KeyError):
                pass
        if is_control and active and not progress and not error:
            progress = 'GPU 资源保留中' if gpu_resource_hold else '监控器运行中'
        if is_control and gpu_resource_hold and active and task.get('receipt'):
            try:
                workload = task_receipt(task).get('workload', {})
                if workload.get('state') == 'failed' and workload.get('resource_retained'):
                    progress = '上次计算失败，GPU 已保留，等待修复后派发'
                    error = workload.get('error') or error
            except (OSError, ValueError, KeyError, TypeError):
                pass
        if waiting_reservation:
            parent = phase.get('wait_for_job_id')
            target = ' #' + parent if parent else ''
            progress = (phase.get('progress') if active else None) or (
                ('GPU 资源保留中，等待前置任务' if active else '等待 GPU 分配，随后等待前置任务')
                + target + '；就绪后在同一作业内计算')
            error, artifact_info = '', None
        if active and not progress and not error:
            progress = (latest_log_message(log_text) or '日志已创建，等待输出') if log else '当前作业日志尚未生成'
        if error and task.get('dimension'):
            records = list(Path(task['root']).glob('outputs/benchmarks/microlens50k/task_smoke/' + task['dimension'] + '/*/videos/MicroLens50K/1.json'))
            if records:
                try:
                    r = json.loads(max(records, key=lambda p: p.stat().st_mtime).read_text())
                    reason = r.get('dimensions', {}).get(task['dimension'], {}).get('reason')
                    if reason:
                        reason = re.sub(r'\x1b\[[0-?]*[ -/]*[@-~]', '', reason)
                        error = ' '.join(reason.split())[:140]
                except (ValueError, OSError):
                    pass
        if completion(task) and not waiting_reservation and not (task.get('current_run_only') and selected):
            state = '已完成'
        elif active:
            state = '运行'
        elif queued:
            state = '排队'
        elif held or controls:
            prerequisite_ids = {binding.get('id') for binding in relay_bindings.values()
                                if binding.get('control_phase') == 'prerequisite'}
            waiting_for_prerequisite = any(i in prerequisite_ids for i, _ in controls)
            state = '等待前置任务' if waiting_for_prerequisite else '等待续跑'
        elif unipercept_processed(task):
            state = '已处理（含跳过）'
        else:
            state = '已停止/失败' if error else '未运行（待核验）'
        if not gpu and task.get('receipt'):
            try:
                planned = task_receipt(task)
                if planned.get('state') == 'waiting_parent':
                    state = '等待续跑'
                    progress = '当前 TQ 作业结束后，按最终剩余 Video ID 分片'
            except (OSError, ValueError, KeyError):
                pass
        if not gpu and task.get('successor_control_receipt') and not completion(task):
            try:
                successor = json.loads((Path(task['root']) / task['successor_control_receipt']).read_text())
                if any(j.get('id') in jobs and jobs[j['id']]['job_state'] in {'R','E','B','Q','H'} for j in successor.get('jobs', [])):
                    state = '等待续跑'
                    progress = '已切换为两个 TQ 分片，进度分别见分片 1 / 2；结束后统一核验'
                    error = ''
            except (OSError, ValueError, KeyError):
                pass
        if len(active) > 1:
            error = '检测到多个运行中的{}作业，请核对；'.format('CPU 监控' if is_control else '计算') + error
        visible_jobs = selected if selected else controls
        rows.append({'task': task['label'], 'state': state, 'kind': 'control' if is_control else 'compute',
                     'task_key': ('{}:{}'.format(task['task_id'], task['run_id']) if task.get('registry') else
                                  task.get('task_id') or task.get('variant') or task.get('dimension') or task.get('model') or task['label']),
                     'completion_version': (str((Path(task['native_run_receipt']) if task.get('native_run_receipt') else
                                                 Path(task['root']) / task['done']).stat().st_mtime_ns)
                                            if state == '已完成' else None),
                     'queue': ','.join(sorted({j['queue'] for _, j in visible_jobs})),
                     'gpu_job_ids': [] if is_control else [i for i, j in selected_gpu
                                                          if int(j.get('Resource_List', {}).get('ngpus', 0)) > 0],
                     'main_job_ids': [] if is_control else [i for i, _ in selected],
                     'primary_job_ids': [i for i, _ in selected],
                     'control_job_ids': [i for i, _ in gpu + controls] if is_control else [i for i, _ in controls],
                     'progress': progress, 'artifact_progress': artifact_info,
                     'error': error, 'log': str(log) if log else '',
                     'root': task['root'], 'done_file': str(Path(task['root']) / task['done']) if task.get('done') else '',
                     'artifact': str(Path(task['root']) / task['artifact']) if task.get('artifact') else '',
                     'checkpoint': str(Path(task['root']) / task['checkpoint']) if task.get('checkpoint') else '',
                     'jobs': [{'id': i, 'state': j['job_state'], 'queue': j['queue'], 'priority': j.get('Priority'),
                               'resources': ({**j.get('Resource_List', {}), **({'ngpus': 0} if is_cpu_compute and 'ngpus' not in j.get('Resource_List', {}) else {})}),
                               'resources_used': j.get('resources_used', {}),
                               'walltime_remaining': walltime_remaining(j.get('Resource_List'), j.get('resources_used'), j['job_state']),
                                       'role': 'GPU 资源保留' if gpu_resource_hold else 'CPU 队列监控器' if is_control else 'CPU 主任务' if (is_cpu_compute or
                                           automatic_compute and int(j.get('Resource_List', {}).get('ngpus', 0)) == 0) else
                                       ('GPU 主任务' if j['job_state'] != 'H' else '旧 GPU 续跑')} for i, j in gpu] +
                             [{'id': i, 'state': j['job_state'], 'queue': j['queue'], 'priority': j.get('Priority'),
                               'resources': j.get('Resource_List', {}), 'resources_used': j.get('resources_used', {}),
                               'walltime_remaining': walltime_remaining(j.get('Resource_List'), j.get('resources_used'), j['job_state']),
                               'role': 'CPU 续跑计算'} for i, j in cpu_compute] +
                             [{'id': i, 'state': j['job_state'], 'queue': j['queue'], 'priority': j.get('Priority'),
                               'resources': j.get('Resource_List', {}), 'resources_used': j.get('resources_used', {}),
                               'walltime_remaining': walltime_remaining(j.get('Resource_List'), j.get('resources_used'), j['job_state']),
                               'role': 'CPU 调度/兼容停放'} for i, j in controls]})
        if phase:
            rows[-1].update(execution_phase=phase['execution_phase'],
                            gpu_resource_hold=gpu_resource_hold,
                            reservation_job_id=phase['reservation_job_id'],
                            wait_for_job_id=phase.get('wait_for_job_id'))
        if relay_bindings:
            rows[-1]['receipt_jobs'] = list(relay_bindings.values())
            for binding in rows[-1]['jobs']:
                binding.update({k:v for k,v in relay_bindings.get(binding['id'], {}).items() if k not in {'id', 'state', 'queue', 'resources', 'resources_used', 'priority'}})
    for row, task in zip(rows, tasks):
        reference_id = referenced_job_id(task)
        if reference_id:
            row['receipt_jobs'] = [{'id': reference_id, 'role': '评估主任务',
                                    'parent_job_id': None, 'log': row['log']}]
        if task.get('task_id'):
            row['task_id'] = task['task_id']
            row['run_id'] = task.get('run_id')
        if task.get('receipt'):
            try:
                receipt = task_receipt(task)
                bindings = {r['id']: r for r in receipt['jobs']}
                row['task_id'] = task['task_id']
                row['run_id'] = task.get('run_id') or receipt.get('run_id')
                row['receipt_jobs'] = receipt['jobs']
                for job in row['jobs']:
                    binding = bindings.get(job['id'], {})
                    job.update({k: v for k, v in binding.items() if k not in {'id', 'resources', 'resources_used', 'state', 'queue'}})
            except (OSError, ValueError, KeyError):
                pass
        if row.get('gpu_resource_hold') and row.get('reservation_job_id'):
            for job in row['jobs']:
                if job['id'] == row['reservation_job_id']:
                    job['receipt_role'] = job['role']
                    job['role'] = 'GPU 资源保留'
    return rows, sorted(set(jobs) - assigned)


def display_width(text):
    return sum(0 if unicodedata.combining(c) else
               2 if unicodedata.east_asian_width(c) in {'W', 'F'} else 1
               for c in str(text))


def cell(text, width):
    text = ' '.join(str(text).split())
    if display_width(text) > width:
        clipped = ''
        for char in text:
            if display_width(clipped + char) > width - 1:
                break
            clipped += char
        text = clipped + '…'
    return text + ' ' * (width - display_width(text))


def render_table(headers, values, limits):
    widths = [min(limit, max([display_width(headers[n])] +
                            [display_width(row[n]) for row in values]))
              for n, limit in enumerate(limits)]
    def border(left, middle, right):
        return left + middle.join('─' * (w + 2) for w in widths) + right
    def line(row):
        return '│ ' + ' │ '.join(cell(v, w) for v, w in zip(row, widths)) + ' │'
    return '\n'.join([border('┌', '┬', '┐'), line(headers),
                      border('├', '┼', '┤')] + [line(row) for row in values] +
                     [border('└', '┴', '┘')])


def brief_error(error):
    if 'UnpicklingError' in error:
        return '权重加载失败 · UnpicklingError'
    if 'DECORDError' in error:
        return '视频解码失败 · DECORDError'
    return cell(error, 65).rstrip()


def walltime_seconds(value):
    """Parse PBS [days:]hours:minutes:seconds durations."""
    if not value:
        return None
    try:
        parts = str(value).split(':')
        if len(parts) == 4:
            days, hours, minutes, seconds = map(int, parts)
        elif len(parts) == 3:
            days, (hours, minutes, seconds) = 0, tuple(map(int, parts))
        else:
            return None
        if hours < 0 or minutes not in range(60) or seconds not in range(60):
            return None
        return days * 86400 + hours * 3600 + minutes * 60 + seconds
    except (TypeError, ValueError):
        return None


def walltime_remaining(resources, resources_used, state):
    requested = walltime_seconds((resources or {}).get('walltime'))
    if requested is None:
        return '未知'
    if state != 'R':
        return '未开始' if state in {'Q', 'H', 'W', 'T'} else '不可用'
    elapsed = walltime_seconds((resources_used or {}).get('walltime'))
    if elapsed is None:
        return '未知'
    remaining = max(0, requested - elapsed)
    hours, remainder = divmod(remaining, 3600)
    minutes, seconds = divmod(remainder, 60)
    return '{:02d}:{:02d}:{:02d}'.format(hours, minutes, seconds)


def resource_summary(resources, resources_used=None, state=None, color=False):
    resources = resources or {}
    gpu = str(resources.get('ngpus', '未知'))
    cpu = str(resources.get('ncpus') if resources.get('ncpus') is not None else '未知')
    memory = str(resources.get('mem') or '未知')
    requested = str(resources.get('walltime') or '未知')
    remaining = walltime_remaining(resources, resources_used, state)
    if not color:
        return '申请：GPU {} · CPU {} 核 · RAM {} · walltime {} · 剩余 {}'.format(gpu, cpu, memory, requested, remaining)

    def paint(text, code):
        return '\x1b[' + code + 'm' + text + '\x1b[0m'

    remaining_seconds = walltime_seconds(remaining)
    if remaining_seconds is None:
        remaining_color = '36' if remaining == '未开始' else '90'
    elif remaining_seconds <= 600:
        remaining_color = '1;31'
    elif remaining_seconds <= 3600:
        remaining_color = '1;33'
    else:
        remaining_color = '1;32'
    return '申请：GPU {} · CPU {} 核 · RAM {} · walltime {} · 剩余 {}'.format(
        paint(gpu, '36'), paint(cpu, '35'), paint(memory, '32'), paint(requested, '33'),
        paint(remaining, remaining_color))


def render_cards(rows, color=False, details=False):
    def paint(text, code):
        return '\x1b[' + code + 'm' + text + '\x1b[0m' if color else text
    running = sum(r['state'] == '运行' and r.get('kind') != 'control' for r in rows)
    running_controls = {j['id'] for r in rows for j in r['jobs']
                        if j['state'] == 'R' and j['id'] in r.get('control_job_ids', [])}
    failed = sum(r['state'] == '已停止/失败' for r in rows)
    controller_label = '控制作业' if any(r.get('gpu_resource_hold') for r in rows) else 'CPU 调度器'
    summary = '{} 个计算任务运行，{} 个{}运行，{} 个异常'.format(running, len(running_controls), ' ' + controller_label if controller_label.startswith('CPU') else controller_label, failed)
    lines = ['', paint('PBS', '1;36') + paint('  ·  ' + summary, '90' if color else '0')]
    groups = [('运行', '运行中', '1;32'), ('排队', '排队中', '1;33'),
              ('等待前置任务', '等待前置任务', '1;33'),
              ('等待续跑', '等待续跑', '1;33'), ('已完成', '已完成', '1;36'),
              ('已处理（含跳过）', '已处理（含跳过）', '1;36'),
              ('未运行（待核验）', '待确认', '1;35'), ('已停止/失败', '需要处理', '1;31'),
              ('control', 'CPU 调度/监控', '1;35')]
    if any(r.get('gpu_resource_hold') for r in rows):
        groups[-1] = ('control', '调度 / 监控 / GPU 资源保留', '1;35')
    state_badges = {'运行': ('● 运行中', '1;32'), '排队': ('◷ 排队中', '1;33'),
                    '等待前置任务': ('◷ 等待前置', '1;33'), '等待续跑': ('◷ 等待续跑', '1;33'),
                    '已完成': ('✓ 已完成', '1;36'), '已处理（含跳过）': ('✓ 已处理', '1;36'),
                    '未运行（待核验）': ('? 待核验', '1;35'), '已停止/失败': ('! 异常', '1;31')}
    for state, title, code in groups:
        members = [r for r in rows if (r.get('kind') == 'control' if state == 'control' else
                                      r.get('kind') != 'control' and r['state'] == state)]
        if not members:
            continue
        lines += ['', paint(title + '  (' + str(len(members)) + ')', code)]
        for r in members:
            label = r['task'].replace('LMWeather fc', 'LMWeather FC').replace('LMWeather gpt2_lora', 'LMWeather GPT2 LoRA')
            badge, badge_color = state_badges.get(r['state'], ('● ' + r['state'], '1;36'))
            if r.get('kind') == 'control' and r['state'] == '运行':
                badge, badge_color = ('● GPU 保留中' if r.get('gpu_resource_hold') else '● 监控运行'), '1;35'
            lines.append('  ' + paint(badge, badge_color) + '  ' + paint(label, '1'))
            meta = [paint(r['queue'], '90')] if r['queue'] else []
            meta += [paint('#' + i.split('.')[0], '1;36') for i in r.get('primary_job_ids', r['gpu_job_ids'])]
            if r['progress']:
                meta.append(paint(r['progress'], '37'))
            if r['error']:
                meta.append(paint(brief_error(r['error']), '1;31'))
            if not r['error'] and not r['progress']:
                meta.append('暂无进度日志' if r['state'] == '运行' else r['state'])
            lines.append('    ' + ' · '.join(meta))
            if not details:
                primary_ids = r.get('primary_job_ids', r.get('main_job_ids', r['gpu_job_ids']))
                primary_jobs = [j for j in r['jobs'] if j['id'] in primary_ids]
                for j in primary_jobs:
                    prefix = '#' + j['id'].split('.')[0] + ' · ' if len(primary_jobs) > 1 else ''
                    lines.append('    ' + prefix + resource_summary(j.get('resources'), j.get('resources_used'), j.get('state'), color=color))
                if not primary_jobs:
                    lines.append('    当前无活动计算作业')
            if details:
                for j in r['jobs']:
                    lines.append('    ' + ' · '.join([j['id'], j['state'], j['queue'], j['role']]))
                    if j.get('priority') is not None:
                        lines.append('      Priority: ' + str(j['priority']))
                    resources = j.get('resources') or {}
                    lines.append('      ' + resource_summary(resources, j.get('resources_used'), j.get('state'), color=color))
                for binding in r.get('receipt_jobs', []):
                    parents = binding.get('parent_job_ids') or binding.get('depends_on_job_ids')
                    if isinstance(parents, (list, tuple)) and parents:
                        relationship = 'parents=' + ','.join(str(parent) for parent in parents)
                    else:
                        relationship = 'parent=' + str(binding.get('parent_job_id') or '-')
                    lines.append('    登记: {} · {} · {}'.format(binding['id'], binding['role'], relationship))
                    lines.append('      日志: ' + (binding.get('log') or '未登记'))
                if r['error']:
                    lines.append('    错误: ' + r['error'])
                if r['log']:
                    lines.append('    日志: ' + r['log'])
                if r['done_file']:
                    lines.append('    完成标记: ' + r['done_file'])
                for key, title in [('root', '项目'), ('checkpoint', '断点'), ('artifact', '成果')]:
                    if r.get(key):
                        lines.append('    ' + title + ': ' + r[key])
            lines.append('')
    lines.append(paint('详情：hpc-status --details', '2'))
    return '\n'.join(lines)


@query_snapshot
def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--details', action='store_true')
    parser.add_argument('--json', action='store_true')
    parser.add_argument('--history', action='store_true', help='Include history (remote collection always includes it)')
    parser.add_argument('--color', dest='color', action='store_true')
    parser.add_argument('--no-color', dest='color', action='store_false')
    parser.set_defaults(color=False)
    args = parser.parse_args()
    ids = command('qselect', '-u', os.environ['USER']).split()
    jobs = json.loads(command('qstat', '-x', '-f', '-F', 'json', *ids))['Jobs'] if ids else {}
    jobs = {i: j for i, j in jobs.items() if j['job_state'] not in {'F', 'X'}}
    rows, other = build_rows(catalogue(jobs), jobs)
    if args.json:
        print(json.dumps({'tasks': rows, 'other_job_ids': other,
                          'other_jobs': [{'id': i, 'name': jobs[i].get('Job_Name', ''),
                                          'state': jobs[i]['job_state'], 'queue': jobs[i]['queue'],
                                          'resources': jobs[i].get('Resource_List', {}),
                                          'resources_used': jobs[i].get('resources_used', {}),
                                          'walltime_remaining': walltime_remaining(jobs[i].get('Resource_List'), jobs[i].get('resources_used'), jobs[i]['job_state']),
                                          'root': jobs[i].get('Variable_List', {}).get('PBS_O_WORKDIR', '')}
                                         for i in other]}, ensure_ascii=False, indent=2))
        return
    print(render_cards(rows, color=args.color, details=args.details))
    if other:
        print('其他/尚未关联作业:')
        for i in other:
            j = jobs[i]
            print('  ' + ' · '.join([i, j.get('Job_Name', ''), j['queue'], j['job_state'],
                                      j.get('Variable_List', {}).get('PBS_O_WORKDIR', '')]))
            print('    ' + resource_summary(j.get('Resource_List'), j.get('resources_used'), j.get('job_state')))


if __name__ == '__main__':
    main()
