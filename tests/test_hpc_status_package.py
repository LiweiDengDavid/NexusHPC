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
                            Resource_List=dict(ngpus=1)),
            '2.server': dict(job_state='R', queue='workq', Variable_List=env,
                            Resource_List=dict(ngpus=0))}
    folder = tmp_path / 'outputs/status/deferred_gpu/demo'
    folder.mkdir(parents=True)
    (folder / 'state.json').write_text(json.dumps(dict(target_job_id='1.server', relay_job_id='2.server')))
    rows, unmatched = status.build_rows([task], jobs)
    assert rows[0]['state'] == '排队'
    assert rows[0]['main_job_ids'] == ['1.server']
    assert rows[0]['control_job_ids'] == ['2.server']
    assert unmatched == []
    assert '0 个计算任务运行，1 个 CPU 调度器运行' in status.render_cards(rows)
