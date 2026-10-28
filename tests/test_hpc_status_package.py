import importlib.util
from pathlib import Path
import json
import os
import subprocess

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / 'template/.workflow/remote/task_status.py'
spec = importlib.util.spec_from_file_location('packaged_status', SOURCE)
status = importlib.util.module_from_spec(spec)
spec.loader.exec_module(status)


def test_pbs_command_path_comes_from_project_config(tmp_path, monkeypatch):
    (tmp_path / '.workflow').mkdir()
    (tmp_path / '.workflow/config.env').write_text('QSTAT_BIN="/configured/qstat"\n')
    monkeypatch.setattr(status, 'PROJECT', tmp_path)
    calls = []
    def fake_run(args, **kwargs):
        calls.append(args)
        return type('Result', (), dict(returncode=0, stdout='{}', stderr=''))()
    monkeypatch.setattr(status.subprocess, 'run', fake_run)
    status.command('qstat', '-f')
    status.command('qselect', '-u', 'demo')
    assert calls == [['/configured/qstat', '-f'], ['qselect', '-u', 'demo']]


def test_collection_launcher_preserves_paths_with_spaces_and_arguments(tmp_path):
    project = tmp_path / 'project with spaces'
    (project / 'scripts').mkdir(parents=True)
    (project / 'scripts/hpc-status').write_text('printf "%s\\n" "$@"\n')
    env = dict(os.environ)
    env.pop('HPC_STATUS_PROJECT', None)
    result = subprocess.run(['bash', str(ROOT / 'hpc-status'), str(project), '--details'],
                            env=env, text=True, capture_output=True, check=True)
    assert result.stdout == '--details\n'
    env['HPC_STATUS_PROJECT'] = str(project)
    result = subprocess.run(['bash', str(ROOT / 'hpc-status'), '--json'],
                            env=env, text=True, capture_output=True, check=True)
    assert result.stdout == '--json\n'


def test_gpu_queue_and_cpu_controller_are_separate(tmp_path):
    task = dict(task_id='demo', run_id='seed0', label='Demo', root=str(tmp_path),
                log_glob='outputs/logs/*', done='DONE', artifact='result.json')
    env = dict(HPC_TASK_ID='demo', PBS_O_WORKDIR=str(tmp_path))
    jobs = {'1.server': dict(job_state='Q', queue='gpuq', Variable_List=env,
                            Resource_List=dict(ngpus=1, ncpus=8, mem='32gb', walltime='06:00:00')),
            '2.server': dict(job_state='R', queue='workq', Variable_List=env,
                            Resource_List=dict(ngpus=0, ncpus=1, mem='1gb', walltime='12:00:00')),
            '3.server': dict(job_state='H', queue='gpuq', Variable_List=env,
                            Resource_List=dict(ngpus=1, ncpus=16, mem='64gb', walltime='03:00:00'))}
    folder = tmp_path / 'outputs/status/deferred_gpu/demo'
    folder.mkdir(parents=True)
    (folder / 'state.json').write_text(json.dumps(dict(target_job_id='1.server', relay_job_id='2.server')))
    rows, unmatched = status.build_rows([task], jobs)
    assert rows[0]['state'] == '排队'
    assert rows[0]['main_job_ids'] == ['1.server']
    assert rows[0]['control_job_ids'] == ['2.server']
    assert unmatched == []
    assert '0 个计算任务运行，1 个 CPU 调度器运行' in status.render_cards(rows)
    default = status.render_cards(rows)
    assert '申请：GPU 1 · CPU 8 核 · RAM 32gb · walltime 06:00:00' in default
    assert 'CPU 16 核' not in default
    assert 'CPU 1 核' not in default
    details = status.render_cards(rows, details=True)
    assert '申请：GPU 0 · CPU 1 核 · RAM 1gb · walltime 12:00:00' in details
    assert '申请：GPU 1 · CPU 16 核 · RAM 64gb · walltime 03:00:00' in details
    assert rows[0]['jobs'][-1]['resources'] == jobs['2.server']['Resource_List']


def test_missing_resources_are_unknown_and_zero_cpu_is_preserved():
    assert status.resource_summary({}) == '申请：GPU 未知 · CPU 未知 核 · RAM 未知 · walltime 未知 · 剩余 未知'
    assert 'CPU 0 核' in status.resource_summary({'ncpus': 0})
