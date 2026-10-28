import importlib.util
import json
import os
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
s=importlib.util.spec_from_file_location('status_repairs',ROOT/'template/.workflow/remote/task_status.py')
m=importlib.util.module_from_spec(s);s.loader.exec_module(m)


def job(root,jid,state,gpus,script):
    return dict(_id=jid,job_state=state,queue='med_gpuq' if gpus else 'workq',
                Submit_arguments=str(script),Variable_List={'PBS_O_WORKDIR':str(root)},Resource_List={'ngpus':gpus})


def test_frozen_identity_matches_renamed_gpu_and_explicit_relay(tmp_path):
    folder=tmp_path/'outputs/status/deferred_gpu/token';folder.mkdir(parents=True)
    script=folder/'gpu.pbs';script.write_text('export MODEL="BPR"\n')
    (folder/'state.json').write_text(json.dumps(dict(cwd=str(tmp_path),gpu_job_id='42.mock',relay_job_id='43.mock',
        gpu_args=[str(script)],dependency='afterany:41.mock')))
    task=dict(label='BPR',root=str(tmp_path),model='BPR',log_glob='*.log')
    jobs={'42.mock':job(tmp_path,'42.mock','R',1,script),'43.mock':job(tmp_path,'43.mock','H',0,folder/'relay.pbs')}
    rows,other=m.build_rows([task],jobs)
    assert not other and rows[0]['state']=='运行'
    assert rows[0]['main_job_ids']==['42.mock'] and rows[0]['control_job_ids']==['43.mock']
    main=next(j for j in rows[0]['jobs'] if j['id']=='42.mock')
    assert main['role']=='continuation' and main['parent_job_id']=='41.mock'
    wrong=dict(task,model='FITMM')
    assert not m.matches(wrong,jobs['42.mock'])


def test_stopped_incomplete_artifact_is_actionable_not_unknown_and_is_read_only(tmp_path):
    p=tmp_path/'results.json';p.write_text(json.dumps(dict(target_count=2,complete=False,counts={'IQA':{'ok':1,'error':1}})))
    before=p.read_bytes()
    log=tmp_path/'run.log';log.write_text('{"status":"ok","video_id":"19738"}\n')
    os.utime(p,(10,10));os.utime(log,(20,20))
    task=dict(label='IQA',root=str(tmp_path),dimension='IQA',target_count=2,log_glob='*.log',artifact='results.json',done='DONE')
    rows,_=m.build_rows([task],{})
    assert rows[0]['state']=='已停止/失败'
    assert rows[0]['artifact_progress']['summary_stale'] is True
    assert '历史汇总' in rows[0]['progress'] and '刷新' in rows[0]['error']
    assert p.read_bytes()==before and not (tmp_path/'DONE').exists()


def test_registered_failure_does_not_claim_legacy_shared_controller(tmp_path):
    (tmp_path/'receipt.json').write_text(json.dumps({'jobs':[{'id':'42.mock','role':'main'}]}))
    (tmp_path/'FAILED').write_text(json.dumps(dict(status='failed',job_id='42.mock',
        task_id='workload',run_id='retry1',updated_at=20,error='missing recovery module')))
    folder=tmp_path/'outputs/status/deferred_gpu/stale';folder.mkdir(parents=True)
    (folder/'state.json').write_text(json.dumps(dict(cwd=str(tmp_path),relay_job_id='99.mock',
        gpu_args=['-v','HPC_TASK_ID=hold,HPC_RUN_ID=reservation','old.pbs'])))
    task=dict(label='AG shard',root=str(tmp_path),registry=True,task_id='hold',run_id='reservation',
        workload_task_id='workload',workload_run_id='retry1',receipt='receipt.json',
        failure_marker='FAILED',submitted_after=10,log_glob='absent.*.log')
    unrelated=job(tmp_path,'99.mock','H',0,folder/'relay.pbs')
    unrelated['Variable_List'].update(HPC_TASK_ID='asr',HPC_RUN_ID='asr-run')
    rows,other=m.build_rows([task],{'99.mock':unrelated})
    assert rows[0]['state']=='已停止/失败'
    assert rows[0]['error']=='missing recovery module'
    assert rows[0]['control_job_ids']==[]
    assert other==['99.mock']


def test_retained_failed_workload_is_a_running_reservation(tmp_path):
    (tmp_path/'receipt.json').write_text(json.dumps(dict(jobs=[dict(id='42.mock',role='control')],
        workload=dict(state='failed',resource_retained=True,error='smoke failed'))))
    task=dict(label='Reserved AG GPU',root=str(tmp_path),registry=True,task_id='hold',run_id='run',
        kind='control',gpu_resource_hold=True,receipt='receipt.json',log_glob='absent.*.log')
    current=job(tmp_path,'42.mock','R',1,tmp_path/'hold.pbs')
    current['Variable_List'].update(HPC_TASK_ID='hold',HPC_RUN_ID='run')
    rows,other=m.build_rows([task],{'42.mock':current})
    assert not other
    assert rows[0]['kind']=='control'
    assert rows[0]['main_job_ids']==[]
    assert rows[0]['primary_job_ids']==['42.mock']
    assert 'GPU 已保留' in rows[0]['progress']
    assert rows[0]['error']=='smoke failed'


def test_receipt_log_fallback_for_finished_asr(tmp_path):
    log=tmp_path/'asr.9.log';log.write_text('ASR_TASK_FAILED exit=1\n')
    (tmp_path/'receipt.json').write_text(json.dumps({'jobs':[{'id':'9.mock','role':'main','log':str(log)}]}))
    task=dict(root=str(tmp_path),current_log_only=True,receipt='receipt.json',log_glob='*.log')
    assert m.latest_log(task,[])==log


def test_finished_expansion_variant_keeps_receipt_log(tmp_path):
    log=tmp_path/'microlens_expansion.AQ.123893.cont0.log'
    log.write_text('EXPANSION_INCOMPLETE missing_sources=9\n')
    (tmp_path/'receipt.json').write_text(json.dumps({'jobs':[{'id':'123893.mock','role':'main','log':str(log)}]}))
    task=dict(root=str(tmp_path),variant='microlens100k_AQ',current_log_only=True,receipt='receipt.json',log_glob='*.log')
    assert m.latest_log(task,[])==log


def test_expansion_prefix_and_missing_source_summary(tmp_path):
    assert m.log_summary('EXPANSION_VIDEO {"task":"CMA","video_id":"12097","status":"ok"}')==('Video ID 12097','')
    (tmp_path/'results.json').write_text(json.dumps(dict(target_count=19738,complete=False,counts={'AQ':{'ok':19729,'missing_source':9}},unresolved_sources={str(i):{} for i in range(9)})))
    task=dict(root=str(tmp_path),artifact_dimension='AQ',artifact='results.json')
    progress,error,info=m.artifact_progress(task,None)
    assert '19729/19738' in progress and '缺源 9' in progress
    assert info['errors']==0 and info['missing_sources']==9 and '缺少 9 个视频来源' in error


def test_final_expansion_log_does_not_make_matching_summary_stale(tmp_path):
    report=dict(target_count=10,complete=False,counts={'AQ':{'ok':9,'missing_source':1}},unresolved_sources={'10':{}})
    (tmp_path/'results.json').write_text(json.dumps(report))
    (tmp_path/'progress.json').write_text(json.dumps(dict(processed=9,expected=10)))
    log=tmp_path/'run.log';log.write_text('EXPANSION_INCOMPLETE missing_sources=1\n')
    os.utime(tmp_path/'results.json',(10,10));os.utime(log,(20,20))
    task=dict(root=str(tmp_path),artifact_dimension='AQ',artifact='results.json',checkpoint='progress.json')
    progress,error,info=m.artifact_progress(task,log)
    assert not info['summary_stale'] and '刷新' not in error and '历史汇总' not in progress


def test_asr_matching_checkpoint_is_not_stale_just_because_log_ended_later(tmp_path):
    value=dict(target_count=2,completed=1,error_ids=['2'],complete=False)
    (tmp_path/'results.json').write_text(json.dumps(value))
    (tmp_path/'progress.json').write_text(json.dumps(value))
    log=tmp_path/'asr.log';log.write_text('ASR_TASK_FAILED exit=1')
    os.utime(tmp_path/'results.json',(10,10));os.utime(log,(20,20))
    task=dict(root=str(tmp_path),task_id='microlens50k_asr',artifact='results.json',checkpoint='progress.json')
    text,error,info=m.artifact_progress(task,log)
    assert info['summary_stale'] is False and '历史汇总' not in text


def test_conflicting_model_fields_and_different_run_are_not_merged(tmp_path):
    script=tmp_path/'gpu.pbs';script.write_text('export MODEL="BPR"\n')
    task=dict(label='BPR',root=str(tmp_path),model='BPR')
    conflicting=job(tmp_path,'42.mock','R',1,script)
    conflicting['Variable_List']['MODEL_NAME']='FITMM'
    assert not m.matches(task,conflicting)
    run=dict(task,task_id='hpo',run_id='new')
    other=job(tmp_path,'42.mock','R',1,script)
    other['Variable_List'].update(HPC_TASK_ID='hpo',HPC_RUN_ID='old')
    assert not m.matches(run,other)


def test_cpu_computation_from_list_receipt_is_not_counted_as_controller(tmp_path):
    script=tmp_path/'retry.pbs';script.write_text('# PBS retry\n')
    log=tmp_path/'aq.42.log';log.write_text('ERROR_RETRY_SMOKE_VALIDATED task=AQ\n')
    binding=dict(id='42.mock',role='main',log=str(log),parent_job_id=None)
    (tmp_path/'receipt.json').write_text(json.dumps([dict(task='AQ',task_id='microlens50k_AQ',job_id='42.mock',jobs=[binding])]))
    task=dict(label='AQ',root=str(tmp_path),dimension='AQ',task_id='microlens50k_AQ',compute_device='cpu',
              receipt='receipt.json',log_glob='*.log',current_log_only=True,
              job_reference=dict(path='receipt.json',select={'task':'AQ'},keys=['job_id']))
    jobs={'42.mock':job(tmp_path,'42.mock','R',0,script), '43.mock':job(tmp_path,'43.mock','R',2,script)}
    rows,other=m.build_rows([task],jobs)
    assert rows[0]['state']=='运行' and rows[0]['kind']=='compute'
    assert rows[0]['primary_job_ids']==['42.mock'] and rows[0]['control_job_ids']==[]
    assert rows[0]['jobs'][0]['role']=='main' and other==['43.mock']
def test_manual_skip_is_reported_separately_from_errors(tmp_path):
    (tmp_path/'results.json').write_text(json.dumps(dict(target_count=3, complete=False,
        counts={'TQ': {'ok': 1, 'pending': 1, 'skipped': 1}})))
    text,error,info=m.artifact_progress(dict(root=str(tmp_path),artifact='results.json',
        dimension='TQ',target_count=3),None)
    assert info['skipped']==1 and info['errors']==0 and info['pending']==1
    assert '已跳过 1' in text and '存在失败记录' not in error

def test_tq_shards_are_distinct_and_transition_is_shared_cpu_control(tmp_path):
    folder=tmp_path/'outputs/status/deferred_gpu/token';folder.mkdir(parents=True)
    (folder/'state.json').write_text(json.dumps(dict(mode='tq_split',dependency='afterany:40.mock',
        relay_job_id='43.mock',gpu_args=['-v','EVAL_SUITE_TASK=TQ'],cwd=str(tmp_path))))
    (tmp_path/'control.json').write_text(json.dumps(dict(jobs=[dict(id='43.mock',role='controller')],job_id='43.mock')))
    script=tmp_path/'shard.pbs';script.write_text('# PBS shard\n')
    tasks=[dict(label='old TQ',root=str(tmp_path),task_id='tq',dimension='TQ'),
        dict(label='s0',root=str(tmp_path),task_id='tq_s0',submit_scripts=['shard.pbs']),
        dict(label='s1',root=str(tmp_path),task_id='tq_s1',submit_scripts=['shard.pbs']),
        dict(label='shared',root=str(tmp_path),task_id='split_control',kind='control',receipt='control.json')]
    jobs={str(i)+'.mock':job(tmp_path,str(i)+'.mock','R' if i<43 else 'H',1 if i<43 else 0,script) for i in range(40,44)}
    jobs['40.mock']['Variable_List']['EVAL_SUITE_TASK']='TQ'
    jobs['40.mock']['Submit_arguments']=str(tmp_path/'old.pbs')
    for t in tasks:t['log_glob']='*.log'
    for i in (41,42):jobs[str(i)+'.mock']['Variable_List'].update(EVAL_SUITE_TASK='TQ',HPC_TASK_ID='tq_s'+str(i-41))
    rows,other=m.build_rows(tasks,jobs)
    assert not other
    assert rows[0]['main_job_ids']==['40.mock'] and rows[0]['control_job_ids']==[]
    assert rows[1]['main_job_ids']==['41.mock'] and rows[2]['main_job_ids']==['42.mock']
    assert rows[3]['kind']=='control' and rows[3]['primary_job_ids']==['43.mock']

def test_tq_shard_completion_requires_matching_ids_recipe_and_scores(tmp_path):
    group=dict(run_id='run',recipe='recipe',items=[dict(video_id='7')])
    proof=dict(run_id='run',recipe='recipe',target_count=1)
    report=dict(complete=True,target_count=1,records=[dict(video_id='7',statuses={'TQ':'ok'},scores={'TQ':42})])
    for name,value in [('group.json',group),('done',proof),('results.json',report)]:
        (tmp_path/name).write_text(json.dumps(value))
    task=dict(root=str(tmp_path),done='done',artifact='results.json',expected_items='group.json',
        run_id='run',artifact_validation='tq_shard',artifact_dimension='TQ')
    assert m.completion(task)
    report['records'][0]['video_id']='8';(tmp_path/'results.json').write_text(json.dumps(report))
    assert not m.completion(task)
    report['records'][0].update(video_id='7',scores={'TQ':float('nan')})
    (tmp_path/'results.json').write_text(json.dumps(report));assert not m.completion(task)

def test_tq_parent_waits_for_shards_without_counting_their_shared_control(tmp_path):
    (tmp_path/'control.json').write_text(json.dumps(dict(jobs=[dict(id='43.mock',role='controller')],job_id='43.mock')))
    task=dict(label='TQ',root=str(tmp_path),dimension='TQ',task_id='tq',log_glob='*.log',successor_control_receipt='control.json')
    script=tmp_path/'split.pbs';script.write_text('# CPU transition\n')
    jobs={'43.mock':job(tmp_path,'43.mock','R',0,script)}
    rows,other=m.build_rows([task],jobs)
    assert rows[0]['state']=='等待续跑' and not rows[0]['error']
    assert rows[0]['main_job_ids']==[] and rows[0]['control_job_ids']==[]
    assert other==['43.mock']

def test_tq_shared_control_completion_requires_canonical_proof(tmp_path):
    report=dict(complete=True,target_count=2,counts={'TQ':{'ok':1,'skipped':1}})
    proof=dict(run_id='run',target_count=2,counts=report['counts'])
    for name,value in [('done',proof),('results.json',report)]:
        (tmp_path/name).write_text(json.dumps(value))
    task=dict(root=str(tmp_path),done='done',artifact='results.json',target_count=2,
        run_id='run',artifact_validation='tq_split_control')
    assert m.completion(task)
    proof['counts']={'TQ':{'ok':2}};(tmp_path/'done').write_text(json.dumps(proof))
    assert not m.completion(task)
