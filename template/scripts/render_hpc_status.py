"""Local display: 24-hour completion dismissal and interactive hide/restore.

Remote status collection and completion artifacts remain read-only. This layer
only writes its own local presentation receipt; all output modes share a filter.
"""
import argparse
import fcntl
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import unicodedata

PROJECT = Path(__file__).resolve().parents[1]
STATE_FILE = Path(os.environ.get('HPC_STATUS_STATE_ROOT', str(PROJECT))) / 'outputs/status/hpc_status_visibility.json'
RETENTION_SECONDS = 24 * 60 * 60


def read_state(path):
    return json.loads(path.read_text()) if path.exists() else {'schema_version': 1, 'completed_views': {}}


def timer_key(row):
    return json.dumps([row.get('root', ''), row.get('task_key') or row.get('task_id') or row['task']], ensure_ascii=False)


def legacy_keys(row):
    return {timer_key(row), json.dumps([row.get('root', ''), row['task']], ensure_ascii=False)}


def selection_key(row, kind='task'):
    if kind == 'job':
        parts = ['job', row.get('root', ''), row['id']]
    else:
        parts = ['task', row.get('root', ''), row.get('task_id') or row.get('task_key') or row['task'], row.get('run_id')]
    return json.dumps(parts, ensure_ascii=False)


def hidden_manually(row, record, kind='task'):
    if kind == 'task' and legacy_keys(row).intersection(record.get('dismissed_tasks', [])):
        return True
    entry = record.get('manual_hidden', {}).get(selection_key(row, kind))
    if entry is None:
        return False
    # Hiding an active experiment includes its continuations and completion.
    # A hidden completed record becomes visible when it restarts or completes anew.
    version = entry.get('completion')
    return (version is None or kind == 'job' or
            (row['state'] == '已完成' and not row.get('main_job_ids', row.get('gpu_job_ids', []))
             and version == [row.get('done_file'), row.get('completion_version')]))


def expired_completion(row, record, now):
    if row['state'] != '已完成' or row.get('main_job_ids', row.get('gpu_job_ids', [])):
        return False
    entry = record.get('completed_views', {}).get(timer_key(row))
    version = [row.get('done_file'), row.get('completion_version'), row.get('run_id')]
    return bool(entry and entry['completion_version'] == version and now >= entry['hide_after'])


def management_entries(payload, record, now=None):
    now = time.time() if now is None else now
    groups = {'hide': [], 'restore': []}
    for kind, rows in [('task', payload['tasks']), ('job', payload.get('other_jobs', []))]:
        for row in rows:
            hidden = hidden_manually(row, record, kind) or (kind == 'task' and expired_completion(row, record, now))
            groups['restore' if hidden else 'hide'].append(dict(kind=kind, row=row, key=selection_key(row, kind)))
    return groups


def hide_entry(row, kind, now):
    completed = kind == 'task' and row['state'] == '已完成' and not row.get('main_job_ids', row.get('gpu_job_ids', []))
    return {'hidden_at': now, 'label': row.get('task') or row.get('name') or row.get('id'),
            'completion': [row.get('done_file'), row.get('completion_version')] if completed else None}


def update_visibility(entries, action, state_file=STATE_FILE, now=None, snapshot=None):
    if not entries:
        return
    now = time.time() if now is None else now
    path = Path(state_file)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.with_suffix('.lock').open('a+') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        record = read_state(path)
        hidden = record.setdefault('manual_hidden', {})
        dismissed = set(record.get('dismissed_tasks', []))
        if action == 'restore' and snapshot:
            selected = {entry['key'] for entry in entries}
            removed = set().union(*(legacy_keys(entry['row']) for entry in entries if entry['kind'] == 'task'))
            for row in snapshot['tasks']:
                key = selection_key(row)
                if key not in selected and legacy_keys(row).intersection(removed & dismissed):
                    hidden[key] = hide_entry(row, 'task', now)
        for entry in entries:
            row, kind = entry['row'], entry['kind']
            if action == 'hide':
                hidden[entry['key']] = hide_entry(row, kind, now)
            elif action == 'restore':
                hidden.pop(entry['key'], None)
                if kind == 'task':
                    dismissed.difference_update(legacy_keys(row))
                    # A restored completed item gets another 24h from its next view.
                    record.setdefault('completed_views', {}).pop(timer_key(row), None)
            else:
                raise ValueError('Unknown visibility action: ' + action)
        if 'dismissed_tasks' in record:
            record['dismissed_tasks'] = sorted(dismissed)
        write_atomic(path, record)


def write_atomic(path, value):
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix='.hpc-visibility-')
    try:
        with os.fdopen(fd, 'w') as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.write('\n')
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def filter_completed(payload, state_file=STATE_FILE, now=None):
    now = time.time() if now is None else now
    path = Path(state_file)
    rows = payload['tasks']
    if not path.exists() and not any(r['state'] == '已完成' for r in rows):
        return payload
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.with_suffix('.lock').open('a+') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        before = read_state(path)
        record = json.loads(json.dumps(before))
        entries = record.setdefault('completed_views', {})
        visible = []
        for row in rows:
            key = timer_key(row)
            if hidden_manually(row, record):
                continue
            # Never dismiss a main task that has restarted, even if its remote
            # completion classification still reflects an earlier marker.
            main_ids = row.get('main_job_ids', row.get('gpu_job_ids', []))
            if row['state'] != '已完成' or main_ids:
                entries.pop(key, None)
                visible.append(row)
                continue
            version = [row.get('done_file'), row.get('completion_version'), row.get('run_id')]
            seen = entries.get(key)
            if seen is None or seen['completion_version'] != version:
                seen = {'completion_version': version, 'first_viewed_at': now,
                        'hide_after': now + RETENTION_SECONDS}
                entries[key] = seen
            if now < seen['hide_after']:
                visible.append(row)
        if record != before:
            write_atomic(path, record)
        other_jobs = [row for row in payload.get('other_jobs', []) if not hidden_manually(row, record, 'job')]
        other_ids = {row['id'] for row in other_jobs}
    result = dict(payload, tasks=visible)
    if 'other_jobs' in payload:
        result['other_jobs'] = other_jobs
        result['other_job_ids'] = [jid for jid in payload.get('other_job_ids', []) if jid in other_ids]
    return result


def clipped(text, width):
    result, used = '', 0
    for char in ' '.join(str(text).split()):
        if unicodedata.category(char).startswith('C'):
            continue
        size = 0 if unicodedata.combining(char) else 2 if unicodedata.east_asian_width(char) in {'W', 'F'} else 1
        if used + size > width:
            break
        result += char
        used += size
    return result


STATUS_GROUPS = [
    ('running', '正在运行', 'green'), ('queued', '排队中', 'yellow'),
    ('prerequisite', '等待前置任务', 'yellow'), ('continuation', '等待续跑', 'yellow'),
    ('completed', '已完成', 'cyan'), ('processed', '已处理（含跳过）', 'yellow'),
    ('confirm', '待确认', 'yellow'), ('failed', '已停止/失败', 'red'),
    ('control', 'CPU 调度/监控', 'cyan'),
]


def entry_status(entry):
    """Presentation labels only; preserve the collector's evidence and state."""
    row = entry['row']
    state = row.get('state', '')
    category = {
        '运行': 'running', 'R': 'running', 'B': 'running', 'E': 'running',
        '排队': 'queued', 'Q': 'queued', 'W': 'queued', 'T': 'queued',
        '等待前置任务': 'prerequisite', '等待续跑': 'continuation',
        'H': 'continuation', 'S': 'continuation', 'U': 'continuation',
        '已完成': 'completed', '已处理（含跳过）': 'processed',
        '已停止/失败': 'failed',
    }.get(state, 'confirm')
    label = next(title for key, title, _ in STATUS_GROUPS if key == category)
    label = {'E': '正在退出', 'H': '保留/等待', 'S': '已挂起', 'U': '已挂起',
             'T': '正在转移'}.get(state, label)
    if row.get('kind') == 'control':
        category = 'control'
        label = {'运行': '调度运行中', 'R': '调度运行中', '排队': '调度排队中',
                 'Q': '调度排队中'}.get(state, label)
    return category, label


def menu_entries(entries):
    order = {key: index for index, (key, _, _) in enumerate(STATUS_GROUPS)}
    return sorted(entries, key=lambda entry: order[entry_status(entry)[0]])


def entry_label(entry):
    row = entry['row']
    return row['task'] if entry['kind'] == 'task' else '{} #{}'.format(row.get('name') or '其他作业', row['id'])


def padded(text, width):
    text = clipped(text, width)
    used = sum(0 if unicodedata.combining(c) else 2 if unicodedata.east_asian_width(c) in {'W', 'F'} else 1 for c in text)
    return text + ' ' * max(0, width - used)


def choose_entries(screen, groups):
    import curses
    screen.keypad(True)
    try:
        curses.curs_set(0)
    except curses.error:
        pass
    colors = {}
    if not os.environ.get('NO_COLOR') and curses.has_colors():
        try:
            curses.start_color()
            background = curses.COLOR_BLACK
            try:
                curses.use_default_colors()
                background = -1
            except curses.error:
                pass
            for pair, (name, foreground) in enumerate([
                    ('green', curses.COLOR_GREEN), ('yellow', curses.COLOR_YELLOW),
                    ('cyan', curses.COLOR_CYAN), ('red', curses.COLOR_RED)], 1):
                curses.init_pair(pair, foreground, background)
                colors[name] = curses.color_pair(pair)
        except curses.error:
            colors = {}
    groups = {mode: menu_entries(entries) for mode, entries in groups.items()}
    mode = 'hide'
    cursor = {'hide': 0, 'restore': 0}
    scroll = {'hide': 0, 'restore': 0}
    checked = {'hide': set(), 'restore': set()}
    while True:
        screen.erase()
        height, width = screen.getmaxyx()
        def put(y, text, style=0):
            if 0 <= y < height and width > 1:
                try:
                    screen.addstr(y, 0, clipped(text, width - 1), style)
                except curses.error:
                    pass
        action = '隐藏' if mode == 'hide' else '恢复'
        entries = groups[mode]
        put(0, 'HPC 任务显示管理 — {}记录（{} 条）'.format(action, len(entries)), curses.A_BOLD)
        put(1, '↑/↓ 移动  空格勾选  回车{}  Tab 切换隐藏/恢复  q/Esc 取消'.format(action))
        usable = height >= 12 and width >= 40
        if not usable:
            put(3, '请放大终端窗口；q/Esc 退出')
        else:
            counts = {key: sum(entry_status(entry)[0] == key for entry in entries) for key, _, _ in STATUS_GROUPS}
            put(2, ' · '.join('{} {}'.format(title, counts[key]) for key, title, _ in STATUS_GROUPS if counts[key]))
            put(3, '    状态          任务名称（按状态分组）', curses.A_DIM)
            size = height - 10
            body, positions = [], {}
            for category, title, color in STATUS_GROUPS:
                members = [(index, entry) for index, entry in enumerate(entries) if entry_status(entry)[0] == category]
                if not members:
                    continue
                style = colors.get(color, 0)
                body.append(('── {}（{} 条）'.format(title, len(members)), style | curses.A_BOLD))
                for index, entry in members:
                    positions[index] = len(body)
                    text = '{} [{}] {} {}'.format('›' if index == cursor[mode] else ' ',
                           'x' if entry['key'] in checked[mode] else ' ',
                           padded(entry_status(entry)[1], 14), entry_label(entry))
                    body.append((text, style | (curses.A_REVERSE if index == cursor[mode] else 0)))
            if entries:
                position = positions[cursor[mode]]
                # Keep the group heading above the current row when possible.
                if position <= scroll[mode]:
                    scroll[mode] = max(0, position - 1)
                elif position >= scroll[mode] + size:
                    scroll[mode] = position - size + 1
                scroll[mode] = min(scroll[mode], max(0, len(body) - size))
            if not entries:
                put(4, '没有可{}的记录，按 Tab 切换列表。'.format(action))
            for y, (text, style) in enumerate(body[scroll[mode]:scroll[mode] + size], 4):
                put(y, text, style)
            if entries:
                entry = entries[cursor[mode]]
                row = entry['row']
                ids = row.get('primary_job_ids') or row.get('main_job_ids') or row.get('control_job_ids') or ([row['id']] if entry['kind'] == 'job' else [])
                put(height - 6, '选中：' + entry_label(entry), curses.A_BOLD)
                put(height - 5, '状态：{} · 队列：{} · 作业：{}'.format(entry_status(entry)[1], row.get('queue') or '无活动队列', ', '.join(ids) or '无活动作业'))
                fallback = ('当前无活动作业，完成凭据尚未确认' if entry_status(entry)[0] == 'confirm'
                            else '暂无进度日志')
                put(height - 4, '进度：' + (row.get('error') or row.get('progress') or fallback))
                put(height - 3, '实验：' + (row.get('run_id') or '未提供实验 ID'))
                put(height - 2, '项目：' + row.get('root', ''))
            put(height - 1, '{}/{} · 已选 {} 条 · PgUp/PgDn 翻页 · 回车{}'.format(
                cursor[mode] + 1 if entries else 0, len(entries), len(checked[mode]), action))
        screen.refresh()
        try:
            key = screen.getch()
        except curses.error:
            continue
        if 0 <= key < 256:
            key = chr(key)
        if key in ('q', 'Q', '\x1b'):
            return None
        if key == '\t':
            mode = 'restore' if mode == 'hide' else 'hide'
        elif key in (curses.KEY_DOWN, 'j') and entries:
            cursor[mode] = min(cursor[mode] + 1, len(entries) - 1)
        elif key in (curses.KEY_UP, 'k') and entries:
            cursor[mode] = max(cursor[mode] - 1, 0)
        elif key in (curses.KEY_NPAGE, curses.KEY_PPAGE) and entries:
            step = max(1, (height - 10) // 2)
            cursor[mode] = max(0, min(len(entries) - 1, cursor[mode] + (step if key == curses.KEY_NPAGE else -step)))
        elif key in (curses.KEY_HOME, curses.KEY_END) and entries:
            cursor[mode] = 0 if key == curses.KEY_HOME else len(entries) - 1
        elif key == ' ' and entries and usable:
            key = entries[cursor[mode]]['key']
            if key in checked[mode]:
                checked[mode].remove(key)
            else:
                checked[mode].add(key)
        elif key in ('\n', '\r', curses.KEY_ENTER) and checked[mode]:
            return mode, [entry for entry in entries if entry['key'] in checked[mode]]


def manage(payload, state_file=STATE_FILE):
    groups = management_entries(payload, read_state(Path(state_file)))
    if not any(groups.values()):
        return
    import curses
    import locale
    try:
        locale.setlocale(locale.LC_ALL, '')
    except locale.Error:
        pass
    # JSON arrives on a pipe. Read keys and draw through the controlling terminal,
    # then restore the original descriptors for regular text/JSON output.
    try:
        tty = open('/dev/tty', 'r+b', buffering=0)
    except OSError as error:
        raise RuntimeError('--manage 需要交互式终端，请在终端中运行 hpc-status --manage。') from error
    with tty:
        saved = [os.dup(0), os.dup(1)]
        try:
            sys.stdout.flush()
            os.dup2(tty.fileno(), 0)
            os.dup2(tty.fileno(), 1)
            try:
                choice = curses.wrapper(choose_entries, groups)
            except curses.error as error:
                raise RuntimeError('无法打开选择菜单，请使用支持光标控制的交互式终端。') from error
        finally:
            for target, fd in enumerate(saved):
                os.dup2(fd, target)
                os.close(fd)
    if choice:
        update_visibility(choice[1], choice[0], state_file, snapshot=payload)


def render(payload, color=False, details=False):
    source = PROJECT / '.workflow/remote/task_status.py'
    spec = importlib.util.spec_from_file_location('hpc_status_display', source)
    status = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(status)
    result = status.render_cards(payload['tasks'], color=color, details=details)
    if payload.get('other_jobs'):
        heading = '\x1b[1;35m其他/尚未关联作业:\x1b[0m' if color else '其他/尚未关联作业:'
        result += '\n' + heading
        for job in payload['other_jobs']:
            job_id = '\x1b[1;36m' + job['id'] + '\x1b[0m' if color else job['id']
            result += '\n  ' + ' · '.join([job_id, job.get('name', ''), job['queue'], job['state'], job.get('root', '')])
            result += '\n    ' + status.resource_summary(job.get('resources'), job.get('resources_used'), job.get('state'), color=color)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--details', action='store_true')
    parser.add_argument('--json', action='store_true')
    parser.add_argument('--history', action='store_true', help='Include completed tasks hidden after 24 hours')
    parser.add_argument('--manage', action='store_true', help='方向键选择、空格勾选、回车隐藏；Tab 切换恢复')
    parser.add_argument('--color', dest='color', action='store_true')
    parser.add_argument('--no-color', dest='color', action='store_false')
    parser.set_defaults(color=False)
    args = parser.parse_args()
    # An SSH/JSON failure must not update the presentation receipt.
    payload = json.load(sys.stdin)
    if args.manage:
        try:
            manage(payload)
        except KeyboardInterrupt:
            return 130
        except (RuntimeError, OSError) as error:
            print(str(error), file=sys.stderr)
            return 2
    if not args.history:
        payload = filter_completed(payload)
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(render(payload, color=args.color, details=args.details))
    return 0


if __name__ == '__main__':
    sys.exit(main())
