import importlib.util
import json
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('uni_global_status',ROOT/'template/.workflow/remote/task_status.py')
status=importlib.util.module_from_spec(spec)
spec.loader.exec_module(status)


def test_receipt_controller_is_associated_without_overwriting_pbs_state(tmp_path):
    receipt=tmp_path/'receipt.json'
    receipt.write_text(json.dumps({'jobs':[{'id':'1.server','role':'controller','state':'submitted','queue':'workq'},
                                         {'id':'2.server','role':'main','controller_job_id':'1.server','queue':'med_gpuq'}]}))
    task=dict(root=str(tmp_path),label='UniPercept',task_id='uni',run_id='run',receipt='receipt.json',
              log_glob='no_logs',artifact_validation='unipercept_iqa')
    env=dict(HPC_TASK_ID='uni',HPC_RUN_ID='run',PBS_O_WORKDIR=str(tmp_path))
    jobs={'1.server':dict(job_state='R',queue='workq',Resource_List={'ngpus':0,'ncpus':1,'mem':'256mb'},Variable_List=env),
          '2.server':dict(job_state='Q',queue='large_gpuq',Resource_List={'ngpus':1,'ncpus':8,'mem':'12gb'},Variable_List=env)}
    rows,other=status.build_rows([task],jobs)
    row=rows[0]
    assert row['state']=='排队' and row['queue']=='large_gpuq'
    assert row['main_job_ids']==['2.server'] and row['control_job_ids']==['1.server']
    binding={r['id']:r for r in row['jobs']}
    assert binding['1.server']['state']=='R'
    assert binding['2.server']['queue']=='large_gpuq'
    assert not other


def test_global_catalogue_registers_unipercept_once(tmp_path,monkeypatch):
    (tmp_path/'configs').mkdir()
    (tmp_path/'.workflow').mkdir()
    (tmp_path/'.workflow/config.env').write_text('')
    task=dict(label='UniPercept',task_id='uni',run_id='run',root_key='PROJECT',
              receipt='outputs/status/unipercept_iqa_submission.json',
              artifact_validation='unipercept_iqa')
    (tmp_path/'configs/hpc_tasks.json').write_text(json.dumps([task]))
    definitions=tmp_path/'configs/hpc_tasks.d'
    definitions.mkdir()
    (definitions/'uni.json').write_text(json.dumps(task))
    monkeypatch.setattr(status,'PROJECT',tmp_path)
    entries=[r for r in status.catalogue() if r.get('task_id')=='uni']
    assert len(entries)==1
    assert entries[0]['receipt']=='outputs/status/unipercept_iqa_submission.json'
    assert entries[0]['root_key']=='PROJECT'
    assert entries[0]['root']==str(tmp_path.resolve())


def test_gpu_assignment_failure_is_visible():
    progress,error=status.log_summary('GPU_SELECTION_FAILED: PBS assigned 1 but CUDA exposes 2')
    assert not progress and error.startswith('GPU_SELECTION_FAILED:')


def test_replacement_controller_clears_previous_gpu_failure(tmp_path,monkeypatch):
    receipt=tmp_path/'receipt.json'
    receipt.write_text(json.dumps({'jobs':[{'id':'1.server','role':'controller','state':'waiting_resources'}]}))
    log=tmp_path/'old.log';log.write_text('GPU_SELECTION_FAILED: previous allocation failed')
    monkeypatch.setattr(status,'latest_log',lambda *args:log)
    task=dict(root=str(tmp_path),label='UniPercept',task_id='uni',run_id='run',receipt='receipt.json',
              log_glob='no_logs',artifact_validation='unipercept_iqa')
    env=dict(HPC_TASK_ID='uni',HPC_RUN_ID='run',PBS_O_WORKDIR=str(tmp_path))
    rows,other=status.build_rows([task],{'1.server':dict(job_state='R',queue='workq',Resource_List={'ngpus':0},Variable_List=env)})
    assert rows[0]['state']=='等待续跑' and rows[0]['error']==''
    assert rows[0]['control_job_ids']==['1.server'] and not other
