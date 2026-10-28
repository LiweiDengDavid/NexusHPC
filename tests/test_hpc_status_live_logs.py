"""Current allocation logs must supply useful status without masking failures."""
import importlib.util
import json
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('live_logs', ROOT/'template/.workflow/remote/task_status.py')
status = importlib.util.module_from_spec(spec)
spec.loader.exec_module(status)


def fixture(root, log='worker.42.log'):
    task = dict(label='Live task', root=str(root), task_id='live', run_id='same-run',
        registry=True, external_submission=True, submitted_after=0,
        log_glob='*.log', current_log_only=True)
    job = dict(job_state='R', queue='large_gpuq', Submit_arguments='',
        Variable_List=dict(PBS_O_WORKDIR=str(root),HPC_TASK_ID='live',HPC_RUN_ID='same-run'),
        Resource_List=dict(ngpus=1))
    return task, {'42.mock':job}


def test_ag_manager_prefix_displays_real_counts():
    line='AG_ALLOCATED_PROGRESS '+json.dumps(dict(target_count=4277,completed=839,errors=50,processed=889,pending=3388))
    progress,error=status.log_summary(line)
    assert '889/4277' in progress and '成功 839' in progress
    assert '错误 50' in progress and '待处理 3388' in progress
    assert not error


def test_nc_api_reports_this_slice_without_claiming_total_completion():
    progress,error=status.log_summary(json.dumps(dict(event='NC_COOPERATIVE_VIDEO_COMPLETE',
        video_id='12362',completed=12,errors=0)))
    assert progress=='本段成功 12，错误 0；Video ID 12362'
    assert not error


def test_handled_video_oom_does_not_mask_later_progress_but_fatal_error_does():
    event=json.dumps(dict(event='RECOVERY_VIDEO_SAVED',video_id='9',
        completed=1,errors=4,processed=5,pending=95,target_count=100,skipped_oom=4))
    text='Traceback (most recent call last):\nRuntimeError: CUDA out of memory\n'+event
    progress,error=status.log_summary(text)
    assert '5/100' in progress and 'OOM 跳过 4' in progress and not error
    progress,error=status.log_summary(text+'\nRuntimeError: disk write failed')
    assert not progress and 'disk write failed' in error


def test_ordinary_heartbeat_does_not_hide_gpu_startup_failure():
    text='GPU_SELECTION_FAILED: no eligible GPU\nAG_ALLOCATED_PROGRESS '+json.dumps(
        dict(target_count=10,completed=0,errors=0,processed=0,pending=10))
    progress,error=status.log_summary(text)
    assert not progress and error.startswith('GPU_SELECTION_FAILED')


def test_malformed_recovery_event_cannot_hide_a_fatal_error():
    text='RuntimeError: disk write failed\n'+json.dumps(dict(event='RECOVERY_VIDEO_SAVED',target_count='bad'))
    progress,error=status.log_summary(text)
    assert not progress and 'disk write failed' in error


def test_active_unknown_log_shows_its_last_line(tmp_path):
    task,jobs=fixture(tmp_path)
    (tmp_path/'worker.42.log').write_text('bootstrap complete\nPreparing current video assets\n')
    rows,_=status.build_rows([task],jobs)
    assert rows[0]['progress']=='最新日志：Preparing current video assets'
    assert not rows[0]['error']


def test_active_structured_artifact_fills_missing_parsed_progress(tmp_path):
    task,jobs=fixture(tmp_path)
    task.update(artifact='result.json',artifact_validation='json_records',target_count=100,dimension='AG')
    (tmp_path/'worker.42.log').write_text('bootstrap complete\n')
    (tmp_path/'result.json').write_text(json.dumps(dict(target_count=100,completed=20,
        errors=1,processed=21,pending=79,results=[])))
    rows,_=status.build_rows([task],jobs)
    assert rows[0]['progress']=='AG 20/100，错误 1，待处理 79'


def test_empty_spool_does_not_displace_populated_current_log(tmp_path):
    task,jobs=fixture(tmp_path)
    real=tmp_path/'worker.42.log';real.write_text('Preparing assets\n')
    empty=tmp_path/'spool.42.log';empty.touch()
    os.utime(real,(10,10));os.utime(empty,(20,20))
    jobs['42.mock'].update(Output_Path='node:'+str(empty),stime='Thu Jan 01 00:00:01 1970')
    assert status.latest_log(task,list(jobs.items()))==real


def test_new_allocation_never_displays_old_jobs_output(tmp_path):
    task,jobs=fixture(tmp_path)
    (tmp_path/'worker.41.log').write_text('epoch 99 from old allocation\n')
    rows,_=status.build_rows([task],jobs)
    assert rows[0]['progress']=='当前作业日志尚未生成'
    (tmp_path/'worker.42.log').touch()
    rows,_=status.build_rows([task],jobs)
    assert rows[0]['progress']=='日志已创建，等待输出'


def test_last_line_removes_ansi_and_does_not_echo_secret_assignments():
    text='\x1b[32mPreparing input\x1b[0m\nAPI_KEY=hidden-value\n'
    assert status.latest_log_message(text)=='最新日志：Preparing input'


def test_queue_capacity_retry_is_described_as_waiting():
    line=json.dumps(dict(event='RECOVERY_ADMISSION_WAIT',attempt=5,
        error="qsub: would exceed queue generic's per-user limit"))
    progress,error=status.log_summary(line)
    assert progress=='等待 GPU 队列名额，第 5 次尝试' and not error
