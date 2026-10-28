"""Repeated reads are shared within a refresh and invalidated on the next one."""
import importlib.util
import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    'status_performance', ROOT / 'template/.workflow/remote/task_status.py')
status = importlib.util.module_from_spec(spec)
spec.loader.exec_module(status)


def test_shared_script_and_relay_are_read_once_per_refresh(tmp_path, monkeypatch):
    script = tmp_path / 'train.pbs'
    script.write_text('export MODEL="BPR"\n')
    folder = tmp_path / 'outputs/status/deferred_gpu/demo'
    folder.mkdir(parents=True)
    relay = folder / 'state.json'
    relay.write_text(json.dumps({'target_job_id': '1.mock'}))
    jobs = {'1.mock': dict(job_state='R', queue='gpuq',
                          Submit_arguments='-N demo -l ngpus=1 train.pbs',
                          Variable_List={'PBS_O_WORKDIR': str(tmp_path)},
                          Resource_List={'ngpus': 1})}
    tasks = [dict(label=model, model=model, root=str(tmp_path), log_glob='*.log')
             for model in ('BPR', 'LightGCN', 'NeuMF')]
    reads = []
    original = Path.read_text

    def read_text(path, *args, **kwargs):
        reads.append(path)
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, 'read_text', read_text)
    rows, other = status.build_rows(tasks, jobs)
    assert not other
    assert rows[0]['main_job_ids'] == ['1.mock']
    assert all(not row['main_job_ids'] for row in rows[1:])
    assert reads.count(script) == reads.count(relay) == 1

    # A subsequent query must observe a replacement script and changed binding.
    script.write_text('export MODEL="LightGCN"\n')
    relay.write_text(json.dumps({'target_job_id': 'missing.mock'}))
    rows, other = status.build_rows(tasks, jobs)
    assert not other
    assert rows[1]['main_job_ids'] == ['1.mock']
    assert not rows[0]['main_job_ids']
    assert reads.count(script) == reads.count(relay) == 2
    assert status._QUERY_CACHE is None


def test_completion_is_shared_without_skipping_next_refresh_validation(tmp_path, monkeypatch):
    artifact = tmp_path / 'results.json'
    artifact.write_text(json.dumps(dict(complete=True, target_count=2,
                                        counts={'IQA': {'ok': 2}})))
    marker = tmp_path / 'DONE'
    marker.touch()
    task = dict(label='IQA', root=str(tmp_path), dimension='IQA',
                target_count=2, artifact='results.json', done='DONE', log_glob='*.log')
    reads = []
    original = Path.read_text

    def read_text(path, *args, **kwargs):
        reads.append(path)
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, 'read_text', read_text)
    rows, _ = status.build_rows([task], {})
    assert rows[0]['state'] == '已完成'
    # One read for progress, one for validation (rather than validating twice).
    assert reads.count(artifact) == 2
    marker.unlink()
    rows, _ = status.build_rows([task], {})
    assert rows[0]['state'] != '已完成'
    assert status._QUERY_CACHE is None


def test_snapshot_clears_after_error_and_direct_helpers_stay_fresh(tmp_path):
    task = dict(root=str(tmp_path), job_reference={'path': 'job.json', 'keys': ['id']})
    path = tmp_path / 'job.json'
    path.write_text('{"id":"1.mock"}')
    assert status.referenced_job_id(task) == '1.mock'
    path.write_text('{"id":"2.mock"}')
    assert status.referenced_job_id(task) == '2.mock'

    @status.query_snapshot
    def broken():
        assert status.referenced_job_id(task) == '2.mock'
        raise RuntimeError('interrupted collection')

    try:
        broken()
    except RuntimeError:
        pass
    assert status._QUERY_CACHE is None


def test_script_symlink_outside_project_is_not_trusted(tmp_path):
    project = tmp_path / 'project'
    project.mkdir()
    outside = tmp_path / 'outside.pbs'
    outside.write_text('export MODEL="BPR"\n')
    (project / 'escape.pbs').symlink_to(outside)
    task = dict(root=str(project))
    job = dict(Submit_arguments='-N demo escape.pbs',
               Variable_List={'PBS_O_WORKDIR': str(project)})
    assert 'MODEL' not in status.submission_environment(task, job)


def test_script_alias_without_pbs_suffix_preserves_identity(tmp_path):
    script = tmp_path / 'real.pbs'
    script.write_text('export MODEL="BPR"\n')
    (tmp_path / 'script-alias').symlink_to(script)
    env = status.submission_environment(
        dict(root=str(tmp_path)),
        dict(Submit_arguments='script-alias',
             Variable_List={'PBS_O_WORKDIR': str(tmp_path)}))
    assert env['MODEL'] == 'BPR'


def auditor_fixture(root, count=40):
    records = []
    for number in range(1, count + 1):
        path = root / ('auditor_{}.json'.format(number))
        data = json.dumps(dict(overall_summary='summary', segments=[{}])).encode()
        path.write_bytes(data)
        records.append(dict(video_id=str(number), auditor_path=str(path),
                            sha256=hashlib.sha256(data).hexdigest()))
    report = dict(complete=True, run_id='run', target_count=count,
                  processed_count=count, completed_count=count, failed_count=0,
                  results=records, failures=[])
    data = json.dumps(report).encode()
    (root / 'results.json').write_bytes(data)
    (root / 'DONE').write_text(json.dumps(dict(
        status='complete', run_id='run', target_count=count,
        completed_count=count, failed_count=0,
        results_sha256=hashlib.sha256(data).hexdigest())))
    task = dict(root=str(root), artifact='results.json', done='DONE',
                run_id='run', target_count=count, artifact_validation='shared_auditor',
                failure_marker='FAILED')
    return task, report


def test_auditor_checks_hash_and_schema_with_one_read_per_result(tmp_path, monkeypatch):
    task, report = auditor_fixture(tmp_path)
    reads = []
    original = Path.read_bytes

    def read_bytes(path):
        reads.append(path)
        return original(path)

    monkeypatch.setattr(Path, 'read_bytes', read_bytes)
    assert status.completion(task)
    assert all(reads.count(Path(row['auditor_path'])) == 1 for row in report['results'])
    # A change beyond the first batch must invalidate completion next query.
    last = Path(report['results'][-1]['auditor_path'])
    last.write_text('{"changed":true}')
    assert not status.completion(task)
    last.unlink()
    assert not status.completion(task)


def test_auditor_rejects_valid_hash_with_invalid_schema(tmp_path):
    for payload in (dict(overall_summary=' ', segments=[{}]),
                    dict(overall_summary='summary', segments=[]),
                    dict(overall_summary='summary', segments=[{}], extra=True),
                    ['not', 'an', 'object']):
        data = json.dumps(payload).encode()
        path = tmp_path / 'auditor.json'
        path.write_bytes(data)
        row = dict(auditor_path=str(path), sha256=hashlib.sha256(data).hexdigest())
        assert not status.verify_result_files([row], path_key='auditor_path', auditor=True)


def test_invalid_aggregate_does_not_scan_auditor_files(tmp_path, monkeypatch):
    task, report = auditor_fixture(tmp_path)
    report['complete'] = False
    (tmp_path / 'results.json').write_text(json.dumps(report))
    original = Path.read_bytes

    def read_bytes(path):
        assert not path.name.startswith('auditor_')
        return original(path)

    monkeypatch.setattr(Path, 'read_bytes', read_bytes)
    assert not status.completion(task)


def test_parallel_json_record_hashes_reject_late_mismatch(tmp_path):
    rows = []
    for number in range(70):
        path = tmp_path / str(number)
        path.write_bytes(b'result')
        rows.append(dict(result_path=str(path), sha256=hashlib.sha256(b'result').hexdigest()))
    assert status.verify_result_files(rows)
    Path(rows[-1]['result_path']).write_bytes(b'tampered')
    assert not status.verify_result_files(rows)
    assert status.verify_result_files([])
