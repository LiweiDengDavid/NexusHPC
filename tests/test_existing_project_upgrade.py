import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

PACKAGE = Path(__file__).resolve().parents[1]


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def project(tmp_path):
    root = tmp_path / 'project with spaces'
    shutil.copytree(PACKAGE / 'template', root)
    fakebin = tmp_path / 'bin'
    fakebin.mkdir()
    qsub = fakebin / 'qsub'
    qsub.write_text('#!' + sys.executable + '''
import json, os, pathlib, sys
p=pathlib.Path(os.environ['CALLS'])
calls=json.loads(p.read_text()) if p.exists() else []
calls.append(sys.argv[1:]);p.write_text(json.dumps(calls))
print(str(100+len(calls))+'.mock')
sys.exit(int(os.environ.get('QSUB_EXIT', '0')))
''')
    qsub.chmod(0o755)
    (root / '.workflow/remote/hpc-qsub').chmod(0o755)
    env = dict(os.environ, CALLS=str(tmp_path / 'calls.json'),
               HPC_STATUS_REMOTE_PYTHON=sys.executable,
               PATH=str(fakebin) + os.pathsep + os.environ['PATH'])
    script = root / 'pbs/custom.pbs'
    script.write_text('#!/bin/bash\n#PBS -q workq\n#PBS -l select=1:ngpus=1:ncpus=4:mem=8gb\nexit 0\n')
    return root, qsub, env


def invoke(project, extra=(), script='pbs/custom.pbs'):
    root, qsub, env = project
    return subprocess.run([sys.executable, str(root / '.workflow/remote/qsub_register.py'),
                           '--real', str(qsub), '--', *extra, script],
                          cwd=root, env=env, text=True, capture_output=True)


def test_native_args_and_stdout_are_preserved_and_records_are_discovered(project, monkeypatch):
    root, _, env = project
    args = ['-q', 'small_gpuq', '-l', 'select=1:ngpus=1:ncpus=2:mem=12gb',
            '-v', 'HPC_TASK_ID=test,HPC_RUN_ID=seed1', '-W', 'depend=afterany:99.mock']
    result = invoke(project, args)
    assert result.returncode == 0, result.stderr
    assert result.stdout == '101.mock\n'
    assert json.loads(Path(env['CALLS']).read_text()) == [args + ['pbs/custom.pbs']]
    manifest, = (root / 'outputs/runs').glob('*/manifest.json')
    state = json.loads((manifest.parent / 'state.json').read_text())
    assert state['jobs'][0]['parent_job_id'] == '99.mock'
    assert state['jobs'][0]['requested_resources']['mem'] == '12gb'
    status = load('adapter_status', root / '.workflow/remote/task_status.py')
    monkeypatch.setattr(status, 'PROJECT', root)
    tasks = status.catalogue()
    assert len(tasks) == 1
    jobs = {'101.mock': dict(Variable_List={'PBS_O_WORKDIR': str(root)},
                            Resource_List={'ngpus': 1}, job_state='Q', queue='small_gpuq')}
    rows, other = status.build_rows(tasks, jobs)
    assert rows[0]['main_job_ids'] == ['101.mock'] and other == []


def test_failed_registration_retains_success_and_never_resubmits(project):
    root, _, env = project
    folder = root / 'outputs/runs/test--seed1'
    (folder / 'state.json').mkdir(parents=True)
    result = invoke(project, ['-v', 'HPC_TASK_ID=test,HPC_RUN_ID=seed1'])
    assert result.returncode == 0
    assert result.stdout == '101.mock\n'
    assert 'Do not resubmit' in result.stderr
    assert len(json.loads(Path(env['CALLS']).read_text())) == 1
    journal, = folder.glob('attempts/*/qsub.stdout')
    assert journal.read_text() == '101.mock\n'


def test_real_qsub_failure_preserves_exit_status_and_creates_no_visible_task(project, monkeypatch):
    root, _, env = project
    env['QSUB_EXIT'] = '17'
    result = invoke(project)
    assert result.returncode == 17
    status = load('failed_adapter_status', root / '.workflow/remote/task_status.py')
    monkeypatch.setattr(status, 'PROJECT', root)
    assert status.catalogue() == []


def test_cpu_controller_and_gpu_main_share_explicit_identity_without_double_counting(project, monkeypatch):
    root, _, _ = project
    assert invoke(project, ['-v', 'HPC_TASK_ID=test,HPC_RUN_ID=seed1,HPC_JOB_ROLE=main']).returncode == 0
    assert invoke(project, ['-l', 'select=1:ngpus=0:ncpus=1:mem=256mb',
        '-v', 'HPC_TASK_ID=test,HPC_RUN_ID=seed1,HPC_JOB_ROLE=controller,HPC_PARENT_JOB_ID=101.mock']).returncode == 0
    status = load('roles_adapter_status', root / '.workflow/remote/task_status.py')
    monkeypatch.setattr(status, 'PROJECT', root)
    jobs = {jid: dict(Variable_List={'PBS_O_WORKDIR': str(root)}, Resource_List={'ngpus': ngpus},
                      job_state='R', queue='workq') for jid, ngpus in [('101.mock',1),('102.mock',0)]}
    rows, other = status.build_rows(status.catalogue(), jobs)
    assert rows[0]['main_job_ids'] == ['101.mock']
    assert rows[0]['control_job_ids'] == ['102.mock']
    assert not other


def test_shell_upgrade_keeps_pbs_directives_and_uses_registration(project):
    root, _, env = project
    upgrade = load('existing_upgrade_shell', PACKAGE / 'scripts/upgrade_existing_projects.py')
    text = '#!/bin/bash\n#PBS -q workq\n#PBS -l select=1:ngpus=1\ncd "' + str(root) + '"\nqsub pbs/custom.pbs\n'
    script = root / 'pbs/submit.pbs'
    script.write_text(upgrade.shell_hook(text))
    assert script.read_text().startswith('#!/bin/bash\n#PBS -q workq\n#PBS -l select=1:ngpus=1\n')
    result = subprocess.run(['bash', str(script)], cwd=root, env=env, text=True, capture_output=True)
    assert result.returncode == 0, result.stderr
    assert result.stdout == '101.mock\n'
    assert list((root / 'outputs/runs').glob('*/state.json'))


def test_python_upgrade_preserves_future_imports_and_other_pbs_commands(project):
    root, _, env = project
    upgrade = load('existing_upgrade_python', PACKAGE / 'scripts/upgrade_existing_projects.py')
    text = '''#!/usr/bin/env python3
"""Submit."""
from __future__ import annotations
import subprocess
PBS='/opt/pbs/bin/'
assert (PBS + 'qstat') == '/opt/pbs/bin/qstat'
result=subprocess.check_output(['/opt/pbs/bin/qsub', 'pbs/custom.pbs'], text=True)
print(result, end='')
'''
    script = root / 'scripts/custom.py'
    script.write_text(upgrade.python_hook(text))
    assert script.read_text().startswith('#!/usr/bin/env python3')
    env['HPC_REAL_QSUB_BIN'] = str(project[1])
    # qsub_path records the passed real binary; use the fake qsub literal here.
    script.write_text(script.read_text().replace("_hpc_qsub_path('/opt/pbs/bin/qsub')", "_hpc_qsub_path(" + repr(str(project[1])) + ")"))
    result = subprocess.run([sys.executable, str(script)], cwd=root, env=env, text=True, capture_output=True)
    assert result.returncode == 0, result.stderr
    assert result.stdout == '101.mock\n'
    assert list((root / 'outputs/runs').glob('*/state.json'))


def test_existing_deferred_qsub_function_is_preserved(project):
    root, _, env = project
    command = 'function /opt/pbs/bin/qsub() { echo DEFERRED; }; source .workflow/remote/auto_register.sh; /opt/pbs/bin/qsub pbs/custom.pbs'
    result = subprocess.run(['bash','-c',command], cwd=root, env=env, text=True, capture_output=True)
    assert result.returncode == 0
    assert result.stdout == 'DEFERRED\n'
    assert not Path(env['CALLS']).exists()


def test_repeated_upgrade_does_not_instrument_its_own_adapters(project):
    root, _, _ = project
    upgrade = load('existing_upgrade_idempotence', PACKAGE / 'scripts/upgrade_existing_projects.py')
    changes = upgrade.plans(root)
    upgrade.apply(root, changes, 'test-backup')
    assert upgrade.plans(root) == {}
    assert upgrade.MARKER not in (root / '.workflow/remote/auto_register.sh').read_text()
    assert upgrade.MARKER not in (root / '.workflow/remote/qsub_register.py').read_text()


def test_project_definition_fragments_preserve_distinct_legacy_variants(project, monkeypatch):
    root, _, _ = project
    folder = root / 'configs/hpc_tasks.d'
    folder.mkdir()
    for variant in ['fc', 'gpt2_lora']:
        (folder / (variant + '.json')).write_text(json.dumps(dict(
            label=variant, root_key='PROJECT', variant=variant, log_glob='outputs/logs/*')))
    status = load('fragment_status', root / '.workflow/remote/task_status.py')
    monkeypatch.setattr(status, 'PROJECT', root)
    assert {t['label'] for t in status.catalogue()} == {'fc', 'gpt2_lora'}


def test_broad_legacy_definition_does_not_double_count_an_automatic_run(project, monkeypatch):
    root, _, _ = project
    assert invoke(project).returncode == 0
    (root / 'configs/hpc_tasks.json').write_text(json.dumps([
        dict(label='Legacy', root_key='PROJECT', log_glob='outputs/logs/*')]))
    status = load('broad_legacy_status', root / '.workflow/remote/task_status.py')
    monkeypatch.setattr(status, 'PROJECT', root)
    jobs = {'101.mock': dict(Variable_List={'PBS_O_WORKDIR': str(root)},
                            Resource_List={'ngpus': 1}, job_state='R', queue='workq')}
    rows, other = status.build_rows(status.catalogue(jobs), jobs)
    assert len([r for r in rows if r['main_job_ids']]) == 1
    assert not other


def test_local_references_read_native_sync_defaults_and_aggregate_root_keys(tmp_path, monkeypatch):
    query = load('native_link_query', PACKAGE / 'scripts/query_hpc_status.py')
    status = load('native_link_status', PACKAGE / 'template/.workflow/remote/task_status.py')
    index = tmp_path / 'index'
    index.mkdir()
    monkeypatch.setenv('HPC_STATUS_PROJECTS_DIR', str(index))
    source = tmp_path / 'sync.sh'
    source.write_text('REMOTE_HOST="${REMOTE_HOST:-mock}"\nREMOTE_PROJECT_DIR="${REMOTE_PROJECT_DIR:-/remote/project with spaces}"\n')
    (index / 'sync.json').write_text(json.dumps({'config': str(source), 'format': 'shell-defaults'}))
    config = tmp_path / 'config.env'
    config.write_text('REMOTE_HOST=mock\nHPC_DASH_PRIMARY_ROOT=/primary\nHPC_DASH_NEXUS_ROOT=/nexus\n')
    (index / 'aggregate.json').write_text(json.dumps({'config': str(config), 'root_key': 'HPC_DASH_NEXUS_ROOT'}))
    assert query.linked_project_roots(status, {'REMOTE_HOST': 'mock'}) == ['/nexus', '/remote/project with spaces']
