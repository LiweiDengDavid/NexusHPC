"""Mac display layer: dismiss completed entries 24h after their first view.

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

PROJECT = Path(__file__).resolve().parents[1]
STATE_FILE = Path(os.environ.get('HPC_STATUS_STATE_ROOT', str(PROJECT))) / 'outputs/status/hpc_status_visibility.json'
RETENTION_SECONDS = 24 * 60 * 60


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
        before = json.loads(path.read_text()) if path.exists() else {'schema_version': 1, 'completed_views': {}}
        record = json.loads(json.dumps(before))
        entries = record['completed_views']
        visible = []
        for row in rows:
            key = json.dumps([row.get('root', ''), row.get('task_key') or row.get('task_id') or row['task']], ensure_ascii=False)
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
    return dict(payload, tasks=visible)


def render(payload, color=False, details=False):
    source = PROJECT / '.workflow/remote/task_status.py'
    spec = importlib.util.spec_from_file_location('hpc_status_display', source)
    status = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(status)
    result = status.render_cards(payload['tasks'], color=color, details=details)
    if payload.get('other_jobs'):
        result += '\n其他/尚未关联作业:'
        for job in payload['other_jobs']:
            result += '\n  ' + ' · '.join([job['id'], job.get('name', ''), job['queue'], job['state'], job.get('root', '')])
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--details', action='store_true')
    parser.add_argument('--json', action='store_true')
    parser.add_argument('--color', dest='color', action='store_true')
    parser.add_argument('--no-color', dest='color', action='store_false')
    parser.set_defaults(color=False)
    args = parser.parse_args()
    # An SSH/JSON failure must not update the presentation receipt.
    payload = filter_completed(json.load(sys.stdin))
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(render(payload, color=args.color, details=args.details))


if __name__ == '__main__':
    main()
