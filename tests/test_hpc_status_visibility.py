import copy
import importlib.util
import json
from pathlib import Path

SOURCE = Path(__file__).resolve().parents[1] / 'template/scripts/render_hpc_status.py'
spec = importlib.util.spec_from_file_location('hpc_status_visibility', SOURCE)
view = importlib.util.module_from_spec(spec)
spec.loader.exec_module(view)


def payload(version='first'):
    return {'tasks': [dict(task='LMWeather FC',task_key='fc',root='/lmweather',
                state='已完成',completion_version=version,done_file='/lmweather/DONE',
                main_job_ids=[],gpu_job_ids=[],jobs=[],queue='',progress='',error='',log='')],
            'other_job_ids': ['other'], 'other_jobs': [dict(id='other',name='external',queue='workq',state='R',root='/other')]}


def test_exact_24_hours_after_first_view_and_refresh_does_not_extend_timer(tmp_path):
    state = tmp_path / 'visibility.json'
    assert len(view.filter_completed(payload(), state, now=100)['tasks']) == 1
    first = json.loads(state.read_text())
    assert len(view.filter_completed(payload(), state, now=500)['tasks']) == 1
    assert json.loads(state.read_text()) == first
    assert len(view.filter_completed(payload(), state, now=100+86400-1)['tasks']) == 1
    result = view.filter_completed(payload(), state, now=100+86400)
    assert not result['tasks']
    assert result['other_job_ids'] == ['other']
    assert result['other_jobs'][0]['name'] == 'external'


def test_new_completion_or_restart_is_visible_again(tmp_path):
    state = tmp_path / 'visibility.json'
    view.filter_completed(payload(), state, now=0)
    assert not view.filter_completed(payload(), state, now=86400)['tasks']
    assert view.filter_completed(payload('new'), state, now=86401)['tasks']
    active = payload('new')
    active['tasks'][0]['state'] = '运行'
    active['tasks'][0]['main_job_ids'] = ['newjob']
    assert view.filter_completed(active, state, now=200000)['tasks']
    assert not json.loads(state.read_text())['completed_views']
    assert view.filter_completed(payload('new'), state, now=200001)['tasks']


def test_stale_marker_never_hides_an_active_main(tmp_path):
    state = tmp_path / 'visibility.json'
    view.filter_completed(payload(), state, now=0)
    restarted = payload()
    restarted['tasks'][0]['main_job_ids'] = ['active']
    assert view.filter_completed(restarted, state, now=90000)['tasks']


def test_modes_all_hide_expired_tasks_without_touching_completion_artifacts(tmp_path):
    marker = tmp_path / 'DONE'
    checkpoint = tmp_path / 'checkpoint.pt'
    marker.write_text('completed')
    checkpoint.write_bytes(b'checkpoint')
    data = payload()
    data['tasks'][0]['done_file'] = str(marker)
    state = tmp_path / 'visibility.json'
    before = copy.deepcopy(data)
    view.filter_completed(data, state, now=0)
    filtered = view.filter_completed(data, state, now=86400)
    assert data == before
    assert 'LMWeather FC' not in view.render(filtered)
    assert 'LMWeather FC' not in view.render(filtered, details=True)
    assert 'LMWeather FC' not in json.dumps(filtered)
    assert marker.read_text() == 'completed'
    assert checkpoint.read_bytes() == b'checkpoint'


def test_incomplete_task_creates_no_local_receipt(tmp_path):
    state = tmp_path / 'visibility.json'
    data = payload()
    data['tasks'][0]['state'] = '排队'
    assert view.filter_completed(data, state, now=100) == data
    assert not state.exists()
