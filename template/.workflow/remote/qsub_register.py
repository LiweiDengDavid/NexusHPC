"""Transparent PBS registration for existing project-specific submitters.

The real qsub receives its original arguments/stdin. This adapter never retries,
cancels, moves or changes resources. Python 3.6+, standard library only.
"""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys
import time
import uuid

ROOT = Path(os.environ.get('HPC_REGISTRATION_ROOT', str(Path(__file__).resolve().parents[2]))).resolve()
spec = importlib.util.spec_from_file_location('_hpc_registry', Path(__file__).with_name('task_registry.py'))
registry = importlib.util.module_from_spec(spec)
spec.loader.exec_module(registry)


def qsub_path(real):
    """Resolve only qsub; leave qstat, qselect and qdel untouched."""
    if Path(str(real)).name != 'qsub':
        return real
    os.environ['HPC_REAL_QSUB_BIN'] = str(real)
    return str(Path(__file__).with_name('hpc-qsub'))


def settings(path):
    values = {}
    if path.is_file():
        for line in path.read_text().splitlines():
            match = re.match(r'^([A-Z][A-Z0-9_]*)=(.*)$', line)
            if match:
                value = shlex.split(match.group(2), comments=True)
                if len(value) == 1:
                    values[match.group(1)] = value[0]
    return values


def options(args):
    result = {}
    index = 0
    while index < len(args):
        arg = args[index]
        if arg in {'-V', '-h', '-I', '-z', '--'}:
            index += 1
        elif arg.startswith('-') and index + 1 < len(args):
            result.setdefault(arg, []).append(args[index + 1])
            index += 2
        else:
            result.setdefault('script', []).append(arg)
            index += 1
    return result


def environment(opts):
    env = {}
    for group in opts.get('-v', []):
        for field in group.split(','):
            key, _, value = field.partition('=')
            env[key] = value if '=' in field else os.environ.get(key, '')
    return env


def safe_id(value):
    original = str(value)
    value = re.sub(r'[^A-Za-z0-9_.-]+', '_', original).strip('._-') or 'task'
    if value != original or len(value) > 100:
        value = value[:75] + '-' + hashlib.sha256(original.encode()).hexdigest()[:16]
    return value


def prepare(args):
    opts = options(args)
    scripts = opts.get('script', [])
    script = Path(scripts[-1]) if scripts else None
    if script and not script.is_absolute():
        script = Path.cwd() / script
    header = []
    if script and script.is_file():
        for line in script.read_text(errors='replace').splitlines():
            if line.startswith('#PBS '):
                header.extend(shlex.split(line[5:]))
            elif line.strip() and not line.startswith('#'):
                break
    opts = options(header + args)
    env = environment(opts)
    config = settings(ROOT / '.workflow/config.env')
    # Read literal script identity fields without executing the PBS source.
    if script and script.is_file():
        for line in script.read_text(errors='replace').splitlines():
            match = re.match(r'^\s*(?:export\s+)?(HPC_TASK_ID|HPC_RUN_ID|HPC_JOB_ROLE)=(.*)$', line)
            if match and not any(c in match.group(2) for c in ('$', '`', ';')):
                value = shlex.split(match.group(2), comments=True)
                if len(value) == 1:
                    env.setdefault(match.group(1), value[0])
    metadata = {k: v for k, v in env.items() if k not in {
        'CONTINUATION_GENERATION', 'HPC_JOB_ROLE', 'HPC_PARENT_JOB_ID',
        'HPC_CONTROLLER_JOB_ID', 'PBS_JOBID', 'HPC_ATTEMPT_DIR'} and
        not re.search(r'(GENERATION|PARENT_JOB|CONTROLLER_JOB|TARGET_JOB|ATTEMPT_ID)$', k)}
    command = os.environ.get('HPC_REGISTER_COMMAND', '')
    task_id = env.get('HPC_TASK_ID') or os.environ.get('HPC_REGISTER_TASK_ID') or safe_id(
        script.stem if script else opts.get('-N', ['PBS task'])[-1])
    digest = hashlib.sha256(json.dumps([str(script), metadata, command], sort_keys=True).encode()).hexdigest()[:16]
    run_id = env.get('HPC_RUN_ID') or os.environ.get('HPC_REGISTER_RUN_ID') or 'run-' + digest
    # Same run_id can be shared by several distinct logical tasks (e.g. LMWeather).
    folder = ROOT / 'outputs/runs' / (safe_id(task_id) + '--' + safe_id(run_id))
    relative = str(folder.relative_to(ROOT))
    folder.mkdir(parents=True, exist_ok=True)
    attempt = folder / 'attempts' / uuid.uuid4().hex
    attempt.mkdir(parents=True)
    started = time.time()
    identity = dict(task_id=task_id, run_id=run_id, started_at=started)
    registry.atomic_json(attempt / 'identity.json', identity)
    resources = {}
    for group in opts.get('-l', []):
        for item in re.split('[:,]', group):
            key, sep, value = item.partition('=')
            if sep:
                resources[key] = value
    role = env.get('HPC_JOB_ROLE') or ('continuation' if env.get('HPC_PARENT_JOB_ID') or
            env.get('CONTINUATION_GENERATION', '0') != '0' else 'main')
    parent = env.get('HPC_PARENT_JOB_ID')
    if not parent:
        for dependency in opts.get('-W', []):
            match = re.search(r'depend=after(?:any|ok):([0-9]+\.[A-Za-z0-9_.-]+)', dependency)
            if match:
                parent = match.group(1)
    # Reuse detailed definitions where they already exist; their native receipts
    # remain authoritative for progress/relationships and artifact validation.
    definitions = []
    legacy = ROOT / 'configs/hpc_tasks.json'
    if legacy.is_file():
        definitions.extend(json.loads(legacy.read_text()))
    for path in sorted((ROOT / 'configs/hpc_tasks.d').glob('*.json')):
        value = json.loads(path.read_text())
        definitions.extend(value if isinstance(value, list) else [value])
    candidates = [t for t in definitions if t.get('task_id') == env.get('HPC_TASK_ID')
                  and (not t.get('run_id') or t['run_id'] == env.get('HPC_RUN_ID'))]
    native = dict(candidates[0]) if len(candidates) == 1 else {}
    marker = os.environ.get('HPC_REGISTER_DONE') or native.get('done')
    artifact = os.environ.get('HPC_REGISTER_ARTIFACT') or native.get('artifact')
    checkpoint = os.environ.get('HPC_REGISTER_CHECKPOINT') or native.get('checkpoint')
    manifest = dict(native, schema_version=1, registry=True, external_submission=True,
                    storage_key=folder.name,
                    task_id=task_id, run_id=run_id, root=str(ROOT),
                    label=native.get('label') or '{} / {}'.format(ROOT.name, task_id),
                    receipt=relative + '/state.json',
                    native_receipt=native.get('receipt'),
                    native_run_receipt=os.environ.get('HPC_REGISTER_NATIVE_RECEIPT'),
                    log_glob=native.get('log_glob') or 'outputs/logs/*',
                    current_log_only=True, current_run_only=True,
                    done=marker, artifact=artifact, checkpoint=checkpoint,
                    submitted_after=started,
                    compute_device='cpu' if resources.get('ngpus') == '0' else 'gpu')
    # A shared watcher with its own identity is a separate control group.
    if role in {'watcher', 'control'} or native.get('kind') == 'control':
        manifest['kind'] = 'control'
    registry.atomic_json(folder / 'manifest.json', manifest)
    binding = dict(role=role, parent_job_id=parent,
                   controller_job_id=env.get('HPC_CONTROLLER_JOB_ID'),
                   target_job_id=env.get('HPC_TARGET_JOB_ID'),
                   generation=int(env.get('CONTINUATION_GENERATION', '0')),
                   submitted_at=started, attempt=str(attempt.relative_to(ROOT)),
                   requested_resources=resources, queue=opts.get('-q', [None])[-1],
                   submission_workdir=str(Path.cwd()),
                   submit_script=str(script) if script else None)
    return folder, attempt, manifest, binding


def persist(folder, attempt, manifest, binding, stdout):
    # Save the exact successful qsub response before parsing/updating JSON.
    (attempt / 'qsub.stdout').write_bytes(stdout)
    job_id = stdout.decode().strip()
    if not re.fullmatch(r'\d+(?:\[[0-9-]*\])?\.[A-Za-z0-9_.-]+', job_id):
        raise ValueError('Unexpected qsub response; preserved at ' + str(attempt / 'qsub.stdout'))
    (attempt / ('job.{}.id'.format(binding['generation']))).write_text(job_id + '\n')
    binding['id'] = job_id
    import fcntl
    with (folder / '.lock').open('a+') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        path = folder / 'state.json'
        try:
            state = json.loads(path.read_text()) if path.exists() else {'jobs': []}
        except ValueError:
            state = {'jobs': []}
        state.update(task_id=manifest['task_id'], run_id=manifest['run_id'], schema_version=1)
        state['jobs'] = [r for r in state['jobs'] if r['id'] != job_id] + [binding]
        registry.atomic_json(path, state)


def main():
    args = sys.argv[1:]
    real = os.environ.get('HPC_REAL_QSUB_BIN', '/opt/pbs/bin/qsub')
    if args[:1] == ['--real']:
        real, args = args[1], args[2:]
        if args[:1] == ['--']:
            args = args[1:]
    # Prepare before launch. A registration preflight error cannot orphan a job.
    prepared = prepare(args)
    result = subprocess.run([real] + args, stdout=subprocess.PIPE)
    if result.returncode:
        sys.stdout.buffer.write(result.stdout)
        return result.returncode
    try:
        persist(*prepared, result.stdout)
    except Exception as error:
        print('WARNING: PBS submission succeeded; registration failed: {}. Do not resubmit. Accepted response: {}'.format(
            error, result.stdout.decode(errors='replace').strip()), file=sys.stderr)
    sys.stdout.buffer.write(result.stdout)
    return 0


if __name__ == '__main__':
    sys.exit(main())
