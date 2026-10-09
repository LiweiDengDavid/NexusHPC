"""Run the local collector over SSH without deploying files or changing PBS jobs."""
import importlib.util
import json
from pathlib import Path
import shlex
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]

def main():
    if len(sys.argv) < 2:
        raise RuntimeError('Usage: query_hpc_status.py CATALOGUE_DIR [--details|--json]')
    catalogue_root = Path(sys.argv[1]).expanduser().resolve()
    source = ROOT / 'template/.workflow/remote/task_status.py'
    if source.stat().st_size >= 10 * 1024 * 1024:
        raise RuntimeError('Collector exceeds the permitted transfer size')
    spec = importlib.util.spec_from_file_location('collector', source)
    collector = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(collector)
    settings = collector.config_values(catalogue_root / '.workflow/config.env')
    if not settings.get('HPC_DASH_PRIMARY_ROOT'):
        settings['HPC_DASH_PRIMARY_ROOT'] = settings['REMOTE_BASE_DIR'].rstrip('/') + '/' + settings['PROJECT_SLUG']
    payload = json.dumps({'source': source.read_text(), 'settings': settings,
                          'tasks': json.loads((catalogue_root / 'configs/hpc_tasks.json').read_text())})
    if len(payload.encode()) >= 10 * 1024 * 1024:
        raise RuntimeError('Query payload exceeds the permitted transfer size')
    bootstrap = ("import json,sys; p=json.load(sys.stdin); "
                 "sys.argv=['hpc-status','--json']; "
                 "scope={'__name__':'__main__','__file__':'hpc_status_collector.py','REMOTE_INPUT':p}; "
                 "exec(compile(p['source'],'<hpc-status collector>','exec'),scope)")
    command = ' '.join(shlex.quote(x) for x in [settings.get('HPC_STATUS_REMOTE_PYTHON', '/usr/bin/python3'), '-B', '-c', bootstrap])
    result = subprocess.run(['ssh', settings['REMOTE_HOST'], command], input=payload,
                            text=True, stdout=subprocess.PIPE)
    if result.returncode:
        return result.returncode
    json.loads(result.stdout)  # Validate before the display layer writes its local receipt.
    return subprocess.run([sys.executable, '-B', str(ROOT / 'template/scripts/render_hpc_status.py'),
                           *sys.argv[2:]], input=result.stdout, text=True,
                          env=dict(__import__('os').environ, HPC_STATUS_STATE_ROOT=str(catalogue_root))).returncode

if __name__ == '__main__':
    sys.exit(main())
