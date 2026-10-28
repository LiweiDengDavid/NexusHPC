"""Automatic submission records. Python 3.6+, standard library only."""
import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import tempfile
import time
import uuid


def atomic_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix='.registry-', dir=str(path.parent))
    try:
        with os.fdopen(fd, 'w') as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.write('\n')
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, str(path))
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def identifier(value):
    if not re.match(r'^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$', value):
        raise ValueError('Task/run IDs must use 1-128 letters, digits, dots, underscores or hyphens')
    return value


def project_index():
    return Path(os.environ.get('HPC_STATUS_PROJECTS_DIR',
                str(Path.home() / '.local/state/nexushpc/projects'))).expanduser()


def link_project(project):
    """Store a reference only; the project's config remains the binding source."""
    config = project.resolve() / '.workflow/config.env'
    if not config.is_file():
        raise ValueError('Missing project configuration: ' + str(config))
    key = hashlib.sha256(str(config).encode()).hexdigest()[:24]
    atomic_json(project_index() / (key + '.json'), {'config': str(config)})


def prepare(project):
    env = os.environ
    task_id = identifier(env.get('HPC_TASK_ID') or 'train')
    # The command and output paths identify the default experiment. An explicit
    # HPC_RUN_ID distinguishes seeds/configurations passed inside another script.
    identity = [task_id, env['TRAIN_COMMAND'], env['CHECKPOINT_FILE'],
                env['DONE_FILE'], env.get('ARTIFACT_FILE', '')]
    run_id = identifier(env.get('HPC_RUN_ID') or
                        'run-' + hashlib.sha256(json.dumps(identity).encode()).hexdigest()[:16])
    for value in (env['CHECKPOINT_FILE'], env['DONE_FILE'], env.get('ARTIFACT_FILE', '')):
        if value and (Path(value).is_absolute() or '..' in Path(value).parts):
            raise ValueError('Output paths must be project-relative without ..')
    folder = project / 'outputs/runs' / run_id
    folder.mkdir(parents=True, exist_ok=True)
    with (folder / '.lock').open('a+') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        manifest_path = folder / 'manifest.json'
        previous = json.loads(manifest_path.read_text()) if manifest_path.exists() else {}
        if previous and previous['task_id'] != task_id:
            raise ValueError('HPC_RUN_ID already belongs to a different task')
        if previous and previous['identity'] != identity:
            raise ValueError('HPC_RUN_ID has different command/output paths; use a new run ID')
        attempt = folder / 'attempts' / uuid.uuid4().hex
        attempt.mkdir(parents=True)
        shutil.copyfile(str(project / '.workflow/config.env'), str(attempt / 'config.env'))
        relative = str(folder.relative_to(project))
        manifest = dict(schema_version=1, registry=True, task_id=task_id, run_id=run_id,
                        identity=identity, root=str(project),
                        label=env.get('HPC_TASK_LABEL') or '{} / {}'.format(env.get('PROJECT_SLUG') or project.name, run_id),
                        compute_device='cpu' if int(env['PBS_NGPUS']) == 0 else 'gpu',
                        log_glob='outputs/logs/train.*.log', current_log_only=True,
                        current_run_only=True, checkpoint=env['CHECKPOINT_FILE'],
                        done=relative + '/complete.json', application_done=env['DONE_FILE'],
                        artifact=env.get('ARTIFACT_FILE') or None,
                        failure_marker=relative + '/failure.json',
                        receipt=relative + '/state.json', submitted_after=time.time())
        atomic_json(manifest_path, manifest)
        atomic_json(attempt / 'identity.json', dict(task_id=task_id, run_id=run_id,
                                                  started_at=manifest['submitted_after']))
        (attempt / 'identity.env').write_text(
            'export HPC_TASK_ID={}\nexport HPC_RUN_ID={}\nexport HPC_ATTEMPT_DIR={}\n'.format(
                task_id, run_id, attempt.relative_to(project)))
    return str(attempt.relative_to(project))


def complete(project, attempt_relative):
    attempt = project / attempt_relative
    identity = json.loads((attempt / 'identity.json').read_text())
    marker = project / os.environ['DONE_FILE']
    if not marker.is_file() or marker.stat().st_mtime < identity['started_at']:
        raise ValueError('Completion marker is missing or predates this attempt')
    artifact = os.environ.get('ARTIFACT_FILE')
    if artifact and not (project / artifact).is_file():
        raise ValueError('Expected result file is missing: ' + artifact)
    atomic_json(attempt.parent.parent / 'complete.json',
                dict(identity, completed_at=time.time(), marker=str(marker),
                     artifact=str(project / artifact) if artifact else None))


def fail(project, attempt_relative, exit_status):
    attempt = project / attempt_relative
    identity = json.loads((attempt / 'identity.json').read_text())
    atomic_json(attempt.parent.parent / 'failure.json',
                dict(identity, failed_at=time.time(), exit_status=exit_status))


def record(project, attempt_relative, job_id, generation, parent):
    if not re.match(r'^\d+(?:\[[0-9-]*\])?\.[A-Za-z0-9_.-]+$', job_id):
        raise ValueError('Expected a full PBS job ID, received: ' + repr(job_id))
    attempt = project / attempt_relative
    identity = json.loads((attempt / 'identity.json').read_text())
    folder = attempt.parent.parent
    env = os.environ
    binding = dict(id=job_id, role='continuation' if generation else 'main',
                   generation=generation, parent_job_id=parent or None,
                   controller_job_id=None, submitted_at=time.time(),
                   attempt=str(attempt.relative_to(project)),
                   queue=env['PBS_QUEUE'],
                   requested_resources=dict(ngpus=int(env['PBS_NGPUS']), ncpus=int(env['PBS_NCPUS']),
                                            mem=env['PBS_MEM'], walltime=env['PBS_WALLTIME']),
                   log=str(project / 'outputs/logs' / ('train.{}.cont{}.log'.format(job_id.split('.')[0], generation))))
    with (folder / '.lock').open('a+') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        path = folder / 'state.json'
        state = json.loads(path.read_text()) if path.exists() else dict(identity, schema_version=1, jobs=[])
        state['jobs'] = [r for r in state['jobs'] if r['id'] != job_id] + [binding]
        state['jobs'].sort(key=lambda r: r['submitted_at'])
        state['updated_at'] = time.time()
        atomic_json(path, state)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('operation', choices=['prepare', 'record', 'complete', 'fail', 'link-project'])
    parser.add_argument('project', type=Path)
    parser.add_argument('--attempt')
    parser.add_argument('--job-id')
    parser.add_argument('--generation', type=int, default=0)
    parser.add_argument('--parent', default='')
    parser.add_argument('--exit-status', type=int)
    args = parser.parse_args()
    project = args.project.resolve()
    if args.operation == 'prepare':
        print(prepare(project))
    elif args.operation == 'link-project':
        link_project(project)
    elif args.operation == 'complete':
        complete(project, args.attempt)
    elif args.operation == 'fail':
        fail(project, args.attempt, args.exit_status)
    else:
        record(project, args.attempt, args.job_id, args.generation, args.parent)


if __name__ == '__main__':
    main()
