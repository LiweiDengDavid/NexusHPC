import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]


def test_standalone_queries_without_project_or_remote_dashboard_files(tmp_path):
    catalogue = tmp_path / 'catalogue with spaces'
    (catalogue / '.workflow').mkdir(parents=True)
    (catalogue / 'configs').mkdir()
    executable = tmp_path / 'bin'
    executable.mkdir()
    ssh = executable / 'ssh'
    ssh.write_text('#!' + sys.executable + '\nimport subprocess, sys, shlex\n'
                   'p=subprocess.run(shlex.split(sys.argv[2]), input=sys.stdin.read(), text=True)\n'
                   'sys.exit(p.returncode)\n')
    ssh.chmod(0o755)
    qselect = executable / 'qselect'
    qselect.write_text('#!/bin/sh\nexit 0\n')
    qselect.chmod(0o755)
    (catalogue / '.workflow/config.env').write_text(
        'REMOTE_HOST="mock-host"\nHPC_DASH_PRIMARY_ROOT="/remote/project that does not exist"\n'
        'HPC_STATUS_REMOTE_PYTHON="' + sys.executable + '"\nQSELECT_BIN="' + str(qselect) + '"\n')
    (catalogue / 'configs/hpc_tasks.json').write_text('[]')
    result = subprocess.run([sys.executable, '-B', str(ROOT / 'scripts/query_hpc_status.py'),
                             str(catalogue), '--json'], text=True, capture_output=True,
                            env=dict(os.environ, PATH=str(executable) + os.pathsep + os.environ['PATH']))
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {'tasks': [], 'other_job_ids': [], 'other_jobs': []}
    assert not (catalogue / 'scripts').exists()
    assert not (catalogue / 'outputs').exists()


def test_failed_ssh_preserves_visibility_receipt(tmp_path):
    catalogue = tmp_path / 'catalogue'
    (catalogue / '.workflow').mkdir(parents=True)
    (catalogue / 'configs').mkdir()
    (catalogue / 'outputs/status').mkdir(parents=True)
    receipt = catalogue / 'outputs/status/hpc_status_visibility.json'
    receipt.write_text('{"unchanged":true}')
    (catalogue / '.workflow/config.env').write_text('REMOTE_HOST="mock-host"\nHPC_DASH_PRIMARY_ROOT="/remote/project"\n')
    (catalogue / 'configs/hpc_tasks.json').write_text('[]')
    ssh = tmp_path / 'ssh'
    ssh.write_text('#!/bin/sh\nexit 17\n')
    ssh.chmod(0o755)
    result = subprocess.run([sys.executable, '-B', str(ROOT / 'scripts/query_hpc_status.py'),
                             str(catalogue), '--json'], text=True, capture_output=True,
                            env=dict(os.environ, PATH=str(tmp_path) + os.pathsep + os.environ['PATH']))
    assert result.returncode == 17
    assert receipt.read_text() == '{"unchanged":true}'


def test_reference_id_associates_renamed_job_and_keeps_last_log(tmp_path):
    spec = importlib.util.spec_from_file_location('referenced_status', ROOT / 'template/.workflow/remote/task_status.py')
    status = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(status)
    (tmp_path / 'state.json').write_text(json.dumps({'evaluation': {'job_id': '42.mock'}}))
    (tmp_path / 'attempt.42.log').write_text('epoch 3')
    task = dict(label='Evaluation', task_id='eval', run_id='seed0', root=str(tmp_path),
                job_reference={'path': 'state.json', 'keys': ['evaluation', 'job_id']},
                log_glob='*.log', current_log_only=True)
    job = dict(_id='42.mock', Job_Name='renamed', Submit_arguments='unknown',
               Variable_List={'PBS_O_WORKDIR': str(tmp_path)}, job_state='R', queue='gpuq',
               Resource_List={'ngpus': 1})
    rows, other = status.build_rows([task], {'42.mock': job})
    assert not other
    assert rows[0]['state'] == '运行'
    assert rows[0]['receipt_jobs'][0]['id'] == '42.mock'
    completed, _ = status.build_rows([task], {})
    assert completed[0]['log'] == str(tmp_path / 'attempt.42.log')
    job['Variable_List']['PBS_O_WORKDIR'] = '/wrong/root'
    assert not status.matches(task, job)
