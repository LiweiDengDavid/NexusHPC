import copy
import importlib.util
import json
import os
from pathlib import Path
import select
import signal
import subprocess
import sys
import time
import pytest

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / 'template/scripts/render_hpc_status.py'
spec = importlib.util.spec_from_file_location('managed_visibility', SOURCE)
view = importlib.util.module_from_spec(spec)
spec.loader.exec_module(view)


class TerminalEnvironment(dict):
    def __repr__(self):
        return '<terminal environment>'


def snapshot():
    def row(label, run, completed=False):
        return dict(task=label, task_id='train', task_key='train:' + run, run_id=run,
            root='/project', state='已完成' if completed else '运行', kind='compute',
            completion_version='proof1' if completed else None, done_file='/project/' + run + '/DONE',
            main_job_ids=[] if completed else ['1.mock'], gpu_job_ids=[] if completed else ['1.mock'],
            primary_job_ids=[] if completed else ['1.mock'], control_job_ids=[], jobs=[], queue='gpuq',
            progress='', error='', log='')
    return dict(tasks=[row('运行任务', 'seed1'), row('完成任务', 'seed2', True)],
                other_job_ids=['3.mock'], other_jobs=[dict(id='3.mock',name='其他作业',root='/other',queue='workq',state='Q')])


@pytest.mark.parametrize('state,kind,category,label', [
    ('运行', 'compute', 'running', '正在运行'),
    ('已完成', 'compute', 'completed', '已完成'),
    ('未运行（待核验）', 'compute', 'confirm', '待确认'),
    ('等待前置任务', 'compute', 'prerequisite', '等待前置任务'),
    ('等待续跑', 'compute', 'continuation', '等待续跑'),
    ('已停止/失败', 'compute', 'failed', '已停止/失败'),
    ('Q', 'compute', 'queued', '排队中'),
    ('R', 'compute', 'running', '正在运行'),
    ('E', 'compute', 'running', '正在退出'),
    ('H', 'compute', 'continuation', '保留/等待'),
    ('F', 'compute', 'confirm', '待确认'),
    ('运行', 'control', 'control', '调度运行中'),
])
def test_menu_status_labels_preserve_collector_evidence(state, kind, category, label):
    entry = dict(kind='task', row=dict(state=state, kind=kind))
    before = copy.deepcopy(entry)
    assert view.entry_status(entry) == (category, label)
    assert entry == before


def test_grouped_menu_keeps_status_visible_with_long_names(monkeypatch):
    import curses
    data = snapshot()
    data['tasks'][0]['task'] = '很长的实验名称' * 30
    data['tasks'].reverse()
    entries = view.management_entries(data, {}, now=0)
    original = copy.deepcopy(entries)

    class Screen:
        def __init__(self):
            self.lines, self.frames = {}, []
            self.keys = iter([curses.KEY_DOWN, ' ', '\n'])
        def keypad(self, value):
            pass
        def erase(self):
            self.lines = {}
        def getmaxyx(self):
            return 24, 80
        def addstr(self, y, x, text, style):
            self.lines[y] = text
        def refresh(self):
            self.frames.append(dict(self.lines))
        def getch(self):
            key = next(self.keys)
            return ord(key) if isinstance(key, str) else key

    monkeypatch.setattr(curses, 'has_colors', lambda: False)
    monkeypatch.setattr(curses, 'curs_set', lambda value: None)
    screen = Screen()
    action, selected = view.choose_entries(screen, entries)
    # Running -> queued -> completed; navigation selects the displayed row.
    assert action == 'hide' and selected[0]['kind'] == 'job'
    first = screen.frames[0]
    assert '正在运行' in first[5] and '很长的实验名称' in first[5]
    assert '状态：正在运行' in first[19]
    assert '作业：1.mock' in first[19]
    text = '\n'.join(first.values())
    assert '正在运行（1 条）' in text and '排队中（1 条）' in text and '已完成（1 条）' in text
    assert entries == original


def test_manual_hide_filters_all_output_modes_and_preserves_artifacts(tmp_path):
    data = snapshot()
    before = copy.deepcopy(data)
    marker = tmp_path / 'DONE'
    marker.write_text('validated')
    state = tmp_path / 'visibility.json'
    entries = view.management_entries(data, {}, now=0)['hide']
    view.update_visibility([entries[0], entries[2]], 'hide', state, now=0)
    filtered = view.filter_completed(data, state, now=1)
    assert [r['task'] for r in filtered['tasks']] == ['完成任务']
    assert filtered['other_jobs'] == [] and filtered['other_job_ids'] == []
    assert '运行任务' not in view.render(filtered)
    assert '运行任务' not in view.render(filtered, details=True)
    assert '运行任务' not in json.dumps(filtered, ensure_ascii=False)
    assert data == before and marker.read_text() == 'validated'
    assert data['tasks'][0]['main_job_ids'] == ['1.mock']


def test_hide_does_not_extend_24h_timer_and_restore_restarts_it(tmp_path):
    data = snapshot()
    state = tmp_path / 'visibility.json'
    view.filter_completed(data, state, now=0)
    entry = view.management_entries(data, view.read_state(state), now=1)['hide'][1]
    view.update_visibility([entry], 'hide', state, now=1)
    assert view.read_state(state)['completed_views'][view.timer_key(data['tasks'][1])]['first_viewed_at'] == 0
    assert len(view.filter_completed(data, state, now=2)['tasks']) == 1
    view.update_visibility([entry], 'restore', state, now=90000)
    assert len(view.filter_completed(data, state, now=90001)['tasks']) == 2
    seen = view.read_state(state)['completed_views'][view.timer_key(data['tasks'][1])]
    assert seen['first_viewed_at'] == 90001 and seen['hide_after'] == 90001 + 86400


def test_expired_completed_entries_can_be_restored_from_the_menu(tmp_path):
    data = snapshot()
    state = tmp_path / 'visibility.json'
    view.filter_completed(data, state, now=0)
    entry, = view.management_entries(data, view.read_state(state), now=86400)['restore']
    assert entry['row']['task'] == '完成任务'
    view.update_visibility([entry], 'restore', state, now=86401)
    assert len(view.filter_completed(data, state, now=86402)['tasks']) == 2


def test_new_run_or_restarted_completed_run_reappears(tmp_path):
    data = snapshot()
    state = tmp_path / 'visibility.json'
    entry = view.management_entries(data, {}, now=0)['hide'][1]
    view.update_visibility([entry], 'hide', state, now=0)
    new = copy.deepcopy(data)
    new['tasks'][1]['run_id'] = 'seed3'
    assert len(view.filter_completed(new, state, now=1)['tasks']) == 2
    restarted = copy.deepcopy(data)
    restarted['tasks'][1].update(state='运行', main_job_ids=['2.mock'], completion_version=None)
    assert len(view.filter_completed(restarted, state, now=2)['tasks']) == 2
    completed = copy.deepcopy(data)
    completed['tasks'][1]['completion_version'] = 'proof2'
    assert len(view.filter_completed(completed, state, now=3)['tasks']) == 2


def test_continuations_and_completion_of_a_hidden_active_run_stay_hidden(tmp_path):
    data = snapshot()
    state = tmp_path / 'visibility.json'
    entry = view.management_entries(data, {}, now=0)['hide'][0]
    view.update_visibility([entry], 'hide', state, now=0)
    new = copy.deepcopy(data)
    new['tasks'][0]['main_job_ids'] = ['4.mock']
    assert len(view.filter_completed(new, state, now=1)['tasks']) == 1
    new['tasks'][0].update(state='已完成', main_job_ids=[], completion_version='new')
    assert len(view.filter_completed(new, state, now=2)['tasks']) == 1


def test_existing_dismissals_are_preserved_and_one_shared_label_can_be_restored(tmp_path):
    data = snapshot()
    data['tasks'][0]['task'] = data['tasks'][1]['task'] = '同名实验'
    state = tmp_path / 'visibility.json'
    state.write_text(json.dumps(dict(completed_views={}, dismissed_tasks=[
        json.dumps(['/project', '同名实验'], ensure_ascii=False)], unrelated='preserve')))
    assert not view.filter_completed(data, state, now=0)['tasks']
    entries = view.management_entries(data, view.read_state(state), now=1)['restore']
    view.update_visibility([entries[0]], 'restore', state, now=2, snapshot=data)
    filtered = view.filter_completed(data, state, now=3)
    assert [r['run_id'] for r in filtered['tasks']] == ['seed1']
    assert view.read_state(state)['unrelated'] == 'preserve'


def test_history_shows_manually_hidden_records_without_changing_state(tmp_path):
    data = snapshot()
    state_root = tmp_path / 'catalogue'
    state = state_root / 'outputs/status/hpc_status_visibility.json'
    entry = view.management_entries(data, {}, now=0)['hide'][0]
    view.update_visibility([entry], 'hide', state, now=0)
    before = state.read_bytes()
    result = subprocess.run([sys.executable, '-B', str(SOURCE), '--history', '--json'],
        input=json.dumps(data), text=True, capture_output=True,
        env=dict(os.environ, HPC_STATUS_STATE_ROOT=str(state_root)))
    assert result.returncode == 0, result.stderr
    assert len(json.loads(result.stdout)['tasks']) == 2
    assert state.read_bytes() == before


def test_manage_without_a_terminal_fails_without_writing_state(tmp_path):
    result = subprocess.run([sys.executable, '-B', str(SOURCE), '--manage'],
        input=json.dumps(snapshot()), text=True, capture_output=True, start_new_session=True,
        env=dict(os.environ, HPC_STATUS_STATE_ROOT=str(tmp_path)))
    assert result.returncode == 2
    assert '交互式终端' in result.stderr
    assert not (tmp_path / 'outputs').exists()


def terminal_run(command, env, keys):
    """Real controlling terminal; renderer still receives JSON through a pipe."""
    import pty
    import fcntl
    import struct
    import termios
    pid, master = pty.fork()
    if pid == 0:
        os.execvpe(command[0], command, dict(env, TERM='xterm-256color'))
    fcntl.ioctl(master, termios.TIOCSWINSZ, struct.pack('HHHH', 24, 120, 0, 0))
    output = b''
    sent = False
    deadline = time.monotonic() + 15
    finished = False
    try:
        while time.monotonic() < deadline:
            ready, _, _ = select.select([master], [], [], 0.1)
            if ready:
                try:
                    chunk = os.read(master, 65536)
                except OSError:
                    chunk = b''
                output += chunk
                if not sent and b'HPC' in output:
                    os.write(master, keys)
                    sent = True
            child, status = os.waitpid(pid, os.WNOHANG)
            if child:
                finished = True
                assert os.waitstatus_to_exitcode(status) == 0, output.decode(errors='replace')
                assert sent, output.decode(errors='replace')
                return output
        raise AssertionError('Interactive menu timed out: ' + output.decode(errors='replace'))
    finally:
        if not finished:
            try:
                os.killpg(pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            os.waitpid(pid, 0)
        os.close(master)


def test_global_entrypoint_arrow_space_enter_then_tab_restore(tmp_path):
    catalogue = tmp_path / 'catalogue with spaces'
    (catalogue / '.workflow').mkdir(parents=True)
    (catalogue / '.workflow/config.env').write_text('REMOTE_HOST=mock\nHPC_DASH_PRIMARY_ROOT=/mock\nLOCAL_CONDA_ENV=python312\n')
    fakebin = tmp_path / 'bin'
    fakebin.mkdir()
    snapshot_file = tmp_path / 'snapshot.json'
    data = snapshot()
    data['other_job_ids'] = []
    data['other_jobs'] = []
    snapshot_file.write_text(json.dumps(data))
    scripts = {
        'ssh': '#!' + sys.executable + '\nimport sys,os\nfrom pathlib import Path\nsys.stdin.read()\nprint(Path(os.environ["SNAPSHOT"]).read_text())\n',
        'conda': '#!/bin/bash\nshift 4\nshift\nexec ' + __import__('shlex').quote(sys.executable) + ' "$@"\n',
    }
    for name, content in scripts.items():
        path = fakebin / name
        path.write_text(content)
        path.chmod(0o755)
    env = TerminalEnvironment(os.environ, PATH=str(fakebin) + os.pathsep + os.environ['PATH'],
               SNAPSHOT=str(snapshot_file), HPC_STATUS_PROJECTS_DIR=str(tmp_path / 'empty-index'))
    command = ['bash', str(ROOT / 'hpc-status'), '--standalone', str(catalogue), '--manage', '--json']
    # xterm application-mode down arrow, space, Enter: hide the second row.
    terminal_run(command, env, b'\x1bOB \r')
    state = catalogue / 'outputs/status/hpc_status_visibility.json'
    filtered = view.filter_completed(data, state)
    assert [r['task'] for r in filtered['tasks']] == ['运行任务']
    # Tab switches to restore; select its first row and commit.
    terminal_run(command, env, b'\t \r')
    assert len(view.filter_completed(data, state)['tasks']) == 2


def test_escape_and_q_cancel_without_visibility_changes(tmp_path):
    import shlex
    data = snapshot()
    data['tasks'] = data['tasks'][:1]
    data['other_jobs'] = []
    data['other_job_ids'] = []
    source = tmp_path / 'payload.json'
    source.write_text(json.dumps(data))
    env = TerminalEnvironment(os.environ, HPC_STATUS_STATE_ROOT=str(tmp_path))
    command = ['bash', '-c', 'cat ' + shlex.quote(str(source)) + ' | ' + shlex.quote(sys.executable) +
               ' -B ' + shlex.quote(str(SOURCE)) + ' --manage --json']
    for keys in (b' \x1b', b' q'):
        terminal_run(command, env, keys)
        assert not (tmp_path / 'outputs').exists()
