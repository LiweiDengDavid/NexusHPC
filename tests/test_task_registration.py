"""Exercise real submission/collection entrypoints against a local fake PBS."""
import importlib.util
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def cluster(tmp_path, monkeypatch):
    project = tmp_path / 'base with spaces' / 'remote-project'
    shutil.copytree(ROOT / 'template', project)
    fakebin = tmp_path / 'bin'
    fakebin.mkdir()
    database = tmp_path / 'pbs.json'
    database.write_text('{}')
    monkeypatch.setenv('PATH', str(fakebin) + os.pathsep + os.environ['PATH'])
    monkeypatch.setenv('FAKE_PBS', str(database))
    monkeypatch.setenv('HPC_STATUS_PROJECTS_DIR', str(tmp_path / 'project-index'))
    monkeypatch.delenv('HPC_ATTEMPT_DIR', raising=False)

    def executable(name, body, python=False):
        path = fakebin / name
        path.write_text(('#!' + sys.executable if python else '#!/bin/bash') + '\n' + body)
        path.chmod(0o755)
        return path

    executable('qsub', '''import json, os, pathlib, sys
p = pathlib.Path(os.environ['FAKE_PBS'])
jobs = json.loads(p.read_text())
args = sys.argv[1:]
jid = str(100 + len(jobs)) + '.mock'
env = dict(v.split('=', 1) for v in args[args.index('-v')+1].split(','))
env['PBS_O_WORKDIR'] = os.getcwd()
jobs[jid] = dict(job_state='Q', queue=args[args.index('-q')+1],
    Variable_List=env, Resource_List=dict(
        dict(part.split('=',1) for part in args[args.index('-l')+1].split(':') if '=' in part),
        walltime='01:00:00'))
p.write_text(json.dumps(jobs))
print(jid)
''', python=True)
    executable('qselect', "import json, os\nprint(' '.join(json.load(open(os.environ['FAKE_PBS']))))\n", python=True)
    executable('qstat', "import json, os\nprint(json.dumps({'Jobs':json.load(open(os.environ['FAKE_PBS']))}))\n", python=True)
    executable('qdel', 'exit 0\n')
    executable('rsync', 'exit 0\n')
    executable('conda', 'shift 4\nexec "$@"\n')
    executable('ssh', 'import subprocess, sys\nsys.exit(subprocess.call(sys.argv[2], shell=True, executable="/bin/bash"))\n', python=True)
    executable('timeout', 'shift 3\nexec "$@"\n')
    conda_sh = tmp_path / 'conda.sh'
    conda_sh.write_text('conda() { return 0; }\n')
    config = project / '.workflow/config.env'
    config.write_text(config.read_text() + '\n' + '\n'.join(
        k + '=' + shlex.quote(str(v)) for k, v in dict(
            PROJECT_SLUG=project.name, REMOTE_HOST='mock', REMOTE_BASE_DIR=project.parent,
            LOCAL_CONDA_ENV='python312', REMOTE_CONDA_ENV='mock', PBS_QUEUE='gpuq',
            TRAIN_COMMAND='bash scripts/train.sh', MAX_CONTINUATIONS=1,
            HPC_STATUS_REMOTE_PYTHON=sys.executable, CONDA_SH=conda_sh,
            ARTIFACT_FILE='outputs/results/metrics.json').items()) + '\n')
    (project / 'scripts/train.sh').write_text(
        'mkdir -p outputs/results\nprintf "{}" > outputs/results/metrics.json\n'
        'touch outputs/status/TRAINING_COMPLETE\n')
    return project, database, fakebin


def run(*args, **kwargs):
    result = subprocess.run(list(args), text=True, capture_output=True, **kwargs)
    assert result.returncode == 0, result.stdout + result.stderr
    return result


def submit(project):
    return run('bash', str(ROOT / 'bin/remote-workflow'), 'submit', str(project))


def dashboard(project, *args):
    # The global catalogue contains neither a legacy task nor a project root.
    # It must discover the experiment through the submission-created reference.
    catalogue = project.parent / 'global-catalogue'
    (catalogue / '.workflow').mkdir(parents=True, exist_ok=True)
    (catalogue / '.workflow/config.env').write_text(
        'REMOTE_HOST="mock"\nHPC_DASH_PRIMARY_ROOT="/unused/catalogue"\n'
        'LOCAL_CONDA_ENV="python312"\nHPC_STATUS_REMOTE_PYTHON=' + shlex.quote(sys.executable) + '\n')
    return run('bash', str(ROOT / 'hpc-status'), '--standalone', str(catalogue), *args)


def test_submit_and_continuation_appear_in_global_dashboard_without_manual_catalogue(cluster):
    project, database, _ = cluster
    legacy = project / 'configs/hpc_tasks.json'
    original = legacy.read_bytes()
    result = submit(project)
    assert '100.mock' in result.stdout
    assert legacy.read_bytes() == original
    manifest_path, = (project / 'outputs/runs').glob('*/manifest.json')
    manifest = json.loads(manifest_path.read_text())
    jobs = json.loads(database.read_text())
    env = jobs['100.mock']['Variable_List']
    assert env['HPC_RUN_ID'] == manifest['run_id']
    # Invoke the same continuation function the actual PBS launcher uses.
    run('bash', '-c', 'source .workflow/remote/common.sh; submit_one 1 afterany:100.mock',
        cwd=project, env=dict(os.environ, **env))
    state = json.loads((manifest_path.parent / 'state.json').read_text())
    assert [r['id'] for r in state['jobs']] == ['100.mock', '101.mock']
    assert state['jobs'][1]['role'] == 'continuation'
    assert state['jobs'][1]['parent_job_id'] == '100.mock'
    assert state['jobs'][1]['requested_resources']['mem'] == '64gb'
    payload = json.loads(dashboard(project, '--json').stdout)
    row, = payload['tasks']
    assert row['task_id'] == 'train'
    assert row['run_id'] == manifest['run_id']
    assert set(row['main_job_ids']) == {'100.mock', '101.mock'}
    assert payload['other_job_ids'] == []
    assert row['task'] in dashboard(project).stdout
    details = dashboard(project, '--details').stdout
    assert '101.mock' in details and 'parent=100.mock' in details


def test_real_job_launcher_records_completion_and_history_survives_queue_exit(cluster):
    project, database, _ = cluster
    submit(project)
    # A queued continuation must not pick up later edits to the project config.
    with (project / '.workflow/config.env').open('a') as stream:
        stream.write('TRAIN_COMMAND=false\nHPC_RUN_ID=unrelated-later-edit\n')
    jobs = json.loads(database.read_text())
    env = dict(os.environ, **jobs['100.mock']['Variable_List'], PBS_JOBID='100.mock')
    run('bash', '.workflow/remote/job.sh', cwd=project, env=env)
    database.write_text('{}')
    # Completed experiments remain discoverable through the local project link.
    payload = json.loads(dashboard(project, '--json').stdout)
    row, = payload['tasks']
    assert row['state'] == '已完成'
    assert len(row['receipt_jobs']) == 2
    visibility = project.parent / 'global-catalogue/outputs/status/hpc_status_visibility.json'
    state = json.loads(visibility.read_text())
    for entry in state['completed_views'].values():
        entry['hide_after'] = 0
    visibility.write_text(json.dumps(state))
    assert json.loads(dashboard(project, '--json').stdout)['tasks'] == []
    history = json.loads(dashboard(project, '--history', '--json').stdout)
    assert history['tasks'][0]['state'] == '已完成'


def test_registration_failure_preserves_job_id_and_recovers_without_resubmitting(cluster):
    project, database, _ = cluster
    registry = project / '.workflow/remote/task_registry.py'
    registry.write_text(registry.read_text().replace(
        "record(project, args.attempt, args.job_id, args.generation, args.parent)",
        "raise RuntimeError('simulated failed JSON write')"))
    result = submit(project)
    assert '100.mock' in result.stdout
    assert 'Do not resubmit' in result.stderr
    assert len(json.loads(database.read_text())) == 1
    assert not list((project / 'outputs/runs').glob('*/state.json'))
    payload = json.loads(dashboard(project, '--json').stdout)
    assert payload['tasks'][0]['main_job_ids'] == ['100.mock']
    assert payload['other_job_ids'] == []


def test_cpu_compute_is_registered_as_compute_not_controller(cluster):
    project, _, _ = cluster
    with (project / '.workflow/config.env').open('a') as stream:
        stream.write('PBS_NGPUS=0\nARTIFACT_FILE=""\n')
    submit(project)
    row, = json.loads(dashboard(project, '--json').stdout)['tasks']
    assert row['kind'] == 'compute'
    assert row['main_job_ids'] == ['100.mock']
    assert row['gpu_job_ids'] == []
    assert row['control_job_ids'] == []
    assert row['jobs'][0]['role'] == 'main'


def test_corrupt_json_receipt_recovers_accepted_ids(cluster):
    project, _, _ = cluster
    submit(project)
    state, = (project / 'outputs/runs').glob('*/state.json')
    state.write_text('{interrupted')
    payload = json.loads(dashboard(project, '--json').stdout)
    assert payload['tasks'][0]['main_job_ids'] == ['100.mock']
    assert payload['other_job_ids'] == []


def test_project_aliases_do_not_duplicate_runs_or_break_association(cluster, monkeypatch):
    project, database, _ = cluster
    submit(project)
    alias = project.parent / 'alias'
    alias.symlink_to(project, target_is_directory=True)
    with (project / '.workflow/config.env').open('a') as stream:
        stream.write('HPC_DASH_ALIAS_ROOT=' + shlex.quote(str(alias)) + '\n')
    status = load('status_aliases', ROOT / 'template/.workflow/remote/task_status.py')
    monkeypatch.setattr(status, 'PROJECT', project)
    jobs = json.loads(database.read_text())
    jobs['100.mock']['Variable_List']['PBS_O_WORKDIR'] = str(alias)
    tasks = status.catalogue(jobs)
    assert len(tasks) == 1
    rows, other = status.build_rows(tasks, jobs)
    assert rows[0]['main_job_ids'] == ['100.mock']
    assert other == []


def test_failed_qsub_creates_no_task_and_dry_run_creates_no_registration(cluster):
    project, database, fakebin = cluster
    run('bash', str(ROOT / 'bin/remote-workflow'), 'submit', str(project), '--dry-run')
    assert not (project / 'outputs/runs').exists()
    assert not Path(os.environ['HPC_STATUS_PROJECTS_DIR']).exists()
    (fakebin / 'qsub').write_text('#!/bin/bash\nexit 17\n')
    result = subprocess.run(['bash', str(ROOT / 'bin/remote-workflow'), 'submit', str(project)],
                            text=True, capture_output=True)
    assert result.returncode != 0
    assert json.loads(database.read_text()) == {}
    assert json.loads(dashboard(project, '--json').stdout)['tasks'] == []


def test_retry_keeps_identity_and_explicit_new_run_stays_separate(cluster):
    project, database, _ = cluster
    submit(project)
    submit(project)
    assert len(list((project / 'outputs/runs').glob('*/manifest.json'))) == 1
    with (project / '.workflow/config.env').open('a') as stream:
        stream.write('HPC_RUN_ID=seed2025\n')
    submit(project)
    payload = json.loads(dashboard(project, '--json').stdout)
    assert len(payload['tasks']) == 2
    groups = {r['run_id']: set(r['main_job_ids']) for r in payload['tasks']}
    assert groups['seed2025'] == {'102.mock'}
    assert any(ids == {'100.mock', '101.mock'} for ids in groups.values())


def test_multiple_completed_runs_have_independent_visibility_timers(cluster):
    project, database, _ = cluster
    submit(project)
    with (project / '.workflow/config.env').open('a') as stream:
        stream.write('HPC_RUN_ID=another-seed\n')
    submit(project)
    (project / 'outputs/results').mkdir(parents=True, exist_ok=True)
    (project / 'outputs/results/metrics.json').write_text('{}')
    for identity_path in (project / 'outputs/runs').glob('*/attempts/*/identity.json'):
        identity = json.loads(identity_path.read_text())
        (identity_path.parents[2] / 'complete.json').write_text(
            json.dumps(dict(identity, completed_at=identity['started_at'] + 1)))
    database.write_text('{}')
    assert len(json.loads(dashboard(project, '--json').stdout)['tasks']) == 2
    visibility = project.parent / 'global-catalogue/outputs/status/hpc_status_visibility.json'
    state = json.loads(visibility.read_text())
    assert len(state['completed_views']) == 2
    for entry in state['completed_views'].values():
        entry['hide_after'] = 0
    visibility.write_text(json.dumps(state))
    assert json.loads(dashboard(project, '--json').stdout)['tasks'] == []


def test_upgrade_backs_up_launchers_and_preserves_config_and_catalogue(cluster):
    project, _, _ = cluster
    config = (project / '.workflow/config.env').read_bytes()
    legacy = (project / 'configs/hpc_tasks.json').read_bytes()
    job = project / '.workflow/remote/job.sh'
    job.write_text('# custom launcher\n')
    run('bash', str(ROOT / 'bin/remote-workflow'), 'upgrade', str(project), '--dry-run')
    assert job.read_text() == '# custom launcher\n'
    run('bash', str(ROOT / 'bin/remote-workflow'), 'upgrade', str(project))
    backup, = (project / '.workflow/backups').glob('*/.workflow/remote/job.sh')
    assert backup.read_text() == '# custom launcher\n'
    assert (project / '.workflow/config.env').read_bytes() == config
    assert (project / 'configs/hpc_tasks.json').read_bytes() == legacy


def test_project_links_follow_current_config_and_do_not_cross_hosts(tmp_path, monkeypatch):
    registry = load('registry_links', ROOT / 'template/.workflow/remote/task_registry.py')
    query = load('query_links', ROOT / 'scripts/query_hpc_status.py')
    status = load('status_links', ROOT / 'template/.workflow/remote/task_status.py')
    monkeypatch.setenv('HPC_STATUS_PROJECTS_DIR', str(tmp_path / 'index'))
    project = tmp_path / 'project'
    (project / '.workflow').mkdir(parents=True)
    config = project / '.workflow/config.env'
    config.write_text('REMOTE_HOST=other\nREMOTE_BASE_DIR=/remote\nPROJECT_SLUG=first\n')
    registry.link_project(project)
    assert query.linked_project_roots(status, {'REMOTE_HOST': 'mock'}) == []
    config.write_text('REMOTE_HOST=mock\nREMOTE_BASE_DIR=/remote\nPROJECT_SLUG=renamed\n')
    registry.link_project(project)
    assert len(list((tmp_path / 'index').glob('*.json'))) == 1
    assert query.linked_project_roots(status, {'REMOTE_HOST': 'mock'}) == ['/remote/renamed']
