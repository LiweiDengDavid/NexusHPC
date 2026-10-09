"""Read-only, one-row-per-task PBS view; compatible with CETUS Python 3.6."""
import argparse
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import time
import unicodedata

PROJECT = Path(__file__).resolve().parents[2]



def command(*args):
    settings = config_values(PROJECT / '.workflow/config.env')
    executable = settings.get(args[0].upper() + '_BIN') or args[0]
    p = subprocess.run([executable] + list(args[1:]), universal_newlines=True,
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if p.returncode:
        raise RuntimeError(p.stderr.strip() or p.stdout.strip())
    return p.stdout


def config_values(path):
    values = {}
    for line in Path(path).read_text().splitlines():
        match = re.match(r'^([A-Z][A-Z0-9_]*)=(.*)$', line)
        if match:
            parts = shlex.split(match.group(2), comments=True)
            if len(parts) == 1:
                values[match.group(1)] = parts[0]
    return values


def catalogue():
    settings = config_values(PROJECT / '.workflow/config.env')
    tasks = json.loads((PROJECT / 'configs/hpc_tasks.json').read_text())
    for t in tasks:
        t['root'] = str(PROJECT) if t['root_key'] == 'PROJECT' else settings[t['root_key']]
    return tasks


def matches(task, job):
    text = job.get('Submit_arguments', '') + ' ' + job.get('Variable_List', {}).get('PBS_O_WORKDIR', '')
    env = job.get('Variable_List', {})
    if task.get('task_id'):
        if env.get('HPC_TASK_ID') == task['task_id']:
            return env.get('PBS_O_WORKDIR', '').rstrip('/') == task['root'].rstrip('/')
        try:
            receipt = json.loads((Path(task['root']) / task['receipt']).read_text())
            if job.get('_id') in {r['id'] for r in receipt['jobs']}:
                return True
        except (OSError, ValueError, KeyError):
            pass
    if task.get('dimension'):
        return env.get('EVAL_SUITE_TASK') == task['dimension']
    if task['root'] not in text:
        return False
    if task.get('submit_scripts'):
        try:
            args = shlex.split(job.get('Submit_arguments', ''))
        except ValueError:
            return False
        relative = set(task['submit_scripts'])
        absolute = {str(Path(task['root']) / script) for script in relative}
        same_root = env.get('PBS_O_WORKDIR', '').rstrip('/') == task['root'].rstrip('/')
        return bool(absolute.intersection(args) or (same_root and relative.intersection(args)))
    if task.get('model'):
        return ('.' + task['model'] + '.') in text or env.get('MODEL_NAME') == task['model']
    if task.get('variant'):
        return env.get('EXPERIMENT_VARIANT') == task['variant']
    return True


def tail(path, limit=65536):
    try:
        with Path(path).open('rb') as f:
            f.seek(0, 2)
            f.seek(max(0, f.tell() - limit))
            return f.read().decode('utf-8', errors='replace')
    except OSError:
        return ''


def latest_log(task, gpu_jobs):
    root = Path(task['root'])
    candidates = list(root.glob(task['log_glob']))
    if task.get('current_log_only'):
        # Match the actual allocation, never an earlier attempt's failure log.
        numbers = [i.split('.')[0] for i, _ in gpu_jobs]
        candidates = [p for p in candidates if any(re.search(r'(?<!\d)' + re.escape(n) + r'(?!\d)', p.name) for n in numbers)]
        for _, job in gpu_jobs:
            output = job.get('Output_Path', '').partition(':')[2]
            p = Path(output) if output else None
            if p and p.is_file() and root in p.parents:
                started = job.get('stime') or job.get('ctime')
                try:
                    fresh = not started or p.stat().st_mtime >= time.mktime(time.strptime(started, '%a %b %d %H:%M:%S %Y'))
                except ValueError:
                    fresh = False
                if fresh:
                    candidates.append(p)
    # Generic LMWeather names do not identify variants; select by GPU job ID.
    if task.get('variant'):
        numbers = [i.split('.')[0] for i, _ in gpu_jobs]
        candidates = [p for p in candidates if any('.' + n + '.' in p.name for n in numbers)]
    return max(candidates, key=lambda p: p.stat().st_mtime) if candidates else None


def log_summary(text):
    for line in reversed(text.splitlines()):
        if re.search(r'(SMOKE_FAILED|TASK_FAILED|Fatal exit|exceed queue|Killed)', line):
            return '', line.strip()[:110]
    for line in reversed(text.splitlines()):
        try:
            row = json.loads(line)
            if row.get('variant') and 'round' in row and 'epoch' in row:
                return '第 {} 轮，{}，epoch {}'.format(row['round'], row.get('client', '-'), row['epoch']), ''
        except (ValueError, AttributeError):
            pass
    scored = []
    for line in text.splitlines():
        try:
            row = json.loads(line)
            if row.get('status') == 'ok' and row.get('video_id'):
                scored.append(str(row['video_id']))
        except (ValueError, AttributeError):
            pass
    if scored:
        return 'Video ID ' + scored[-1], ''
    if 'MICROLENS50K_SMOKE_VALIDATED' in text:
        return 'smoke 已通过', ''
    for line in reversed(text.splitlines()):
        if re.search(r'(Traceback|Error:|Exception:|SMOKE_FAILED|TASK_FAILED|Fatal exit|exceed queue|Killed)', line):
            return '', line.strip()[:110]
    for line in reversed(text.splitlines()):
        if line.startswith(('Max epochs', 'Completed trials:', 'Budget:', 'Scheduling continuation', 'Strategy:')):
            continue
        if re.search(r'(epoch|trial|ARTIMUSE_FRAME_PROGRESS|Loading checkpoint|WAIT_EXTRACTION_LOCK)', line, re.I):
            return line.strip()[:80], ''
    return '', ''


def completion(task):
    root = Path(task['root'])
    marker = task.get('done')
    artifact = task.get('artifact')
    if not marker or not (root / marker).is_file() or not artifact or not (root / artifact).is_file():
        return False
    if task.get('dimension') or task.get('task_id') == 'microlens50k_asr':
        try:
            r = json.loads((root / artifact).read_text())
            return r.get('complete') is True and r.get('target_count') == task.get('target_count')
        except (ValueError, OSError):
            return False
    return True


def build_rows(tasks, jobs):
    rows = []
    assigned = set()
    for jid, job in jobs.items():
        job['_id'] = jid
    for task in tasks:
        is_control = task.get('kind') == 'control'
        gpu = [(i, j) for i, j in jobs.items() if
               (int(j.get('Resource_List', {}).get('ngpus', 0)) == 0 if is_control else
                int(j.get('Resource_List', {}).get('ngpus', 0)) > 0) and matches(task, j)]
        ids = {i for i, _ in gpu}
        controls = []
        # Controller relation is recorded in relay state; never guess from a
        # shortened name. A CPU R row must not make a GPU task appear running.
        folder = Path(task['root']) / 'outputs/status/deferred_gpu'
        for p in ([] if is_control else folder.glob('*/state.json')):
            try:
                s = json.loads(p.read_text())
            except (OSError, ValueError):
                continue
            related = s.get('target_job_id') in ids or s.get('gpu_job_id') in ids
            parent_ids = re.findall(r'(\d+\.[A-Za-z0-9_-]+)', s.get('dependency') or '')
            related = related or bool(ids.intersection(parent_ids))
            target = jobs.get(s.get('target_job_id'), {})
            related = related or bool(target and matches(task, target))
            # Frozen continuation arguments are a full receipt, even before
            # a GPU successor exists. Resolve env values rather than PBS names.
            env = {}
            args = s.get('gpu_args', [])
            for n, arg in enumerate(args[:-1]):
                if arg == '-v':
                    env.update(dict(v.split('=', 1) for v in args[n + 1].split(',') if '=' in v))
            synthetic = {'Submit_arguments': ' '.join(args),
                         'Variable_List': dict(env, PBS_O_WORKDIR=s.get('cwd', ''))}
            related = related or (bool(args) and matches(task, synthetic))
            if related:
                for key in ['relay_job_id', 'target_job_id']:
                    jid = s.get(key)
                    if jid in jobs and int(jobs[jid].get('Resource_List', {}).get('ngpus', 0)) == 0:
                        controls.append((jid, jobs[jid]))
        controls = list(dict(controls).items())
        assigned.update(i for i, _ in gpu + controls)
        active = [(i, j) for i, j in gpu if j['job_state'] in {'R', 'E', 'B'}]
        queued = [(i, j) for i, j in gpu if j['job_state'] == 'Q']
        held = [(i, j) for i, j in gpu if j['job_state'] == 'H']
        selected = active or queued or held
        log = latest_log(task, active or queued or gpu)
        progress, error = log_summary(tail(log)) if log else ('', '')
        if is_control and active and not progress and not error:
            progress = '监控器运行中'
        if error and task.get('dimension'):
            records = list(Path(task['root']).glob('outputs/benchmarks/microlens50k/task_smoke/' + task['dimension'] + '/*/videos/MicroLens50K/1.json'))
            if records:
                try:
                    r = json.loads(max(records, key=lambda p: p.stat().st_mtime).read_text())
                    reason = r.get('dimensions', {}).get(task['dimension'], {}).get('reason')
                    if reason:
                        reason = re.sub(r'\x1b\[[0-?]*[ -/]*[@-~]', '', reason)
                        error = ' '.join(reason.split())[:140]
                except (ValueError, OSError):
                    pass
        if completion(task):
            state = '已完成'
        elif active:
            state = '运行'
        elif queued:
            state = '排队'
        elif held or controls:
            state = '等待续跑'
        else:
            state = '已停止/失败' if error else '未运行（待核验）'
        if len(active) > 1:
            error = '检测到多个运行中的{}作业，请核对；'.format('CPU 监控' if is_control else 'GPU ') + error
        rows.append({'task': task['label'], 'state': state, 'kind': 'control' if is_control else 'compute',
                     'task_key': task.get('task_id') or task.get('variant') or task.get('dimension') or task.get('model') or task['label'],
                     'completion_version': (str((Path(task['root']) / task['done']).stat().st_mtime_ns)
                                            if state == '已完成' else None),
                     'queue': ','.join(sorted({j['queue'] for _, j in selected})),
                     'gpu_job_ids': [] if is_control else [i for i, _ in selected],
                     'main_job_ids': [] if is_control else [i for i, _ in selected],
                     'primary_job_ids': [i for i, _ in selected],
                     'control_job_ids': [i for i, _ in gpu] if is_control else [i for i, _ in controls],
                     'progress': progress,
                     'error': error, 'log': str(log) if log else '',
                     'root': task['root'], 'done_file': str(Path(task['root']) / task['done']) if task.get('done') else '',
                     'artifact': str(Path(task['root']) / task['artifact']) if task.get('artifact') else '',
                     'checkpoint': str(Path(task['root']) / task['checkpoint']) if task.get('checkpoint') else '',
                     'jobs': [{'id': i, 'state': j['job_state'], 'queue': j['queue'],
                               'resources': j.get('Resource_List', {}),
                               'role': 'CPU 队列监控器' if is_control else
                                       ('GPU 主任务' if j['job_state'] != 'H' else '旧 GPU 续跑')} for i, j in gpu] +
                             [{'id': i, 'state': j['job_state'], 'queue': j['queue'], 'role': 'CPU 调度/兼容停放'} for i, j in controls]})
    for row, task in zip(rows, tasks):
        if task.get('receipt'):
            try:
                receipt = json.loads((Path(task['root']) / task['receipt']).read_text())
                bindings = {r['id']: r for r in receipt['jobs']}
                row['task_id'] = task['task_id']
                row['run_id'] = task.get('run_id') or receipt.get('run_id')
                row['receipt_jobs'] = receipt['jobs']
                for job in row['jobs']:
                    binding = bindings.get(job['id'], {})
                    job.update({k: v for k, v in binding.items() if k != 'id'})
            except (OSError, ValueError, KeyError):
                pass
    return rows, sorted(set(jobs) - assigned)


def display_width(text):
    return sum(0 if unicodedata.combining(c) else
               2 if unicodedata.east_asian_width(c) in {'W', 'F'} else 1
               for c in str(text))


def cell(text, width):
    text = ' '.join(str(text).split())
    if display_width(text) > width:
        clipped = ''
        for char in text:
            if display_width(clipped + char) > width - 1:
                break
            clipped += char
        text = clipped + '…'
    return text + ' ' * (width - display_width(text))


def render_table(headers, values, limits):
    widths = [min(limit, max([display_width(headers[n])] +
                            [display_width(row[n]) for row in values]))
              for n, limit in enumerate(limits)]
    def border(left, middle, right):
        return left + middle.join('─' * (w + 2) for w in widths) + right
    def line(row):
        return '│ ' + ' │ '.join(cell(v, w) for v, w in zip(row, widths)) + ' │'
    return '\n'.join([border('┌', '┬', '┐'), line(headers),
                      border('├', '┼', '┤')] + [line(row) for row in values] +
                     [border('└', '┴', '┘')])


def brief_error(error):
    if 'UnpicklingError' in error:
        return '权重加载失败 · UnpicklingError'
    if 'DECORDError' in error:
        return '视频解码失败 · DECORDError'
    return cell(error, 65).rstrip()


def render_cards(rows, color=False, details=False):
    def paint(text, code):
        return '\x1b[' + code + 'm' + text + '\x1b[0m' if color else text
    running = sum(r['state'] == '运行' and r.get('kind') != 'control' for r in rows)
    running_controls = {j['id'] for r in rows for j in r['jobs']
                        if j['state'] == 'R' and j['role'].startswith('CPU')}
    failed = sum(r['state'] == '已停止/失败' for r in rows)
    lines = ['', paint('PBS', '1') + '  ·  {} 个计算任务运行，{} 个 CPU 调度器运行，{} 个异常'.format(running, len(running_controls), failed)]
    groups = [('运行', '运行中', '32'), ('排队', '排队中', '33'),
              ('等待续跑', '等待续跑', '33'), ('已完成', '已完成', '36'),
              ('未运行（待核验）', '待确认', '33'), ('已停止/失败', '需要处理', '31'),
              ('control', 'CPU 调度/监控', '36')]
    for state, title, code in groups:
        members = [r for r in rows if (r.get('kind') == 'control' if state == 'control' else
                                      r.get('kind') != 'control' and r['state'] == state)]
        if not members:
            continue
        lines += ['', paint(title + '  (' + str(len(members)) + ')', '1;' + code)]
        for r in members:
            label = r['task'].replace('LMWeather fc', 'LMWeather FC').replace('LMWeather gpt2_lora', 'LMWeather GPT2 LoRA')
            lines.append('  ' + paint(label, '1'))
            meta = [r['queue']] if r['queue'] else []
            meta += ['#' + i.split('.')[0] for i in r.get('primary_job_ids', r['gpu_job_ids'])]
            if r['error']:
                meta.append(paint(brief_error(r['error']), code))
            elif r['progress']:
                meta.append(r['progress'])
            else:
                meta.append('暂无进度日志' if r['state'] == '运行' else r['state'])
            lines.append('    ' + ' · '.join(meta))
            if details:
                for j in r['jobs']:
                    lines.append('    ' + ' · '.join([j['id'], j['state'], j['queue'], j['role']]))
                    if j.get('resources'):
                        resource = j['resources']
                        lines.append('      {} GPU · {} CPU · RAM {} · walltime {}'.format(
                            resource.get('ngpus', '-'), resource.get('ncpus', '-'),
                            resource.get('mem', '-'), resource.get('walltime', '-')))
                for binding in r.get('receipt_jobs', []):
                    lines.append('    登记: {} · {} · parent={}'.format(binding['id'], binding['role'], binding.get('parent_job_id') or '-'))
                    lines.append('      日志: ' + (binding.get('log') or '未登记'))
                if r['error']:
                    lines.append('    错误: ' + r['error'])
                if r['log']:
                    lines.append('    日志: ' + r['log'])
                if r['done_file']:
                    lines.append('    完成标记: ' + r['done_file'])
                for key, title in [('root', '项目'), ('checkpoint', '断点'), ('artifact', '成果')]:
                    if r.get(key):
                        lines.append('    ' + title + ': ' + r[key])
            lines.append('')
    lines.append(paint('详情：./hpc-status --details', '2'))
    return '\n'.join(lines)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--details', action='store_true')
    parser.add_argument('--json', action='store_true')
    parser.add_argument('--color', dest='color', action='store_true')
    parser.add_argument('--no-color', dest='color', action='store_false')
    parser.set_defaults(color=False)
    args = parser.parse_args()
    ids = command('qselect', '-u', os.environ['USER']).split()
    jobs = json.loads(command('qstat', '-x', '-f', '-F', 'json', *ids))['Jobs'] if ids else {}
    jobs = {i: j for i, j in jobs.items() if j['job_state'] not in {'F', 'X'}}
    rows, other = build_rows(catalogue(), jobs)
    if args.json:
        print(json.dumps({'tasks': rows, 'other_job_ids': other,
                          'other_jobs': [{'id': i, 'name': jobs[i].get('Job_Name', ''),
                                          'state': jobs[i]['job_state'], 'queue': jobs[i]['queue'],
                                          'root': jobs[i].get('Variable_List', {}).get('PBS_O_WORKDIR', '')}
                                         for i in other]}, ensure_ascii=False, indent=2))
        return
    print(render_cards(rows, color=args.color, details=args.details))
    if other:
        print('其他/尚未关联作业:')
        for i in other:
            j = jobs[i]
            print('  ' + ' · '.join([i, j.get('Job_Name', ''), j['queue'], j['job_state'],
                                      j.get('Variable_List', {}).get('PBS_O_WORKDIR', '')]))


if __name__ == '__main__':
    main()
