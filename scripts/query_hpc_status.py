"""Run the local collector over SSH without deploying files or changing PBS jobs."""
import importlib.util
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]


def linked_project_roots(collector, settings):
    index = Path(os.environ.get('HPC_STATUS_PROJECTS_DIR',
                 str(Path.home() / '.local/state/nexushpc/projects'))).expanduser()
    roots = set()
    for reference in sorted(index.glob('*.json')):
        try:
            link = json.loads(reference.read_text())
            config = Path(link['config'])
            project = collector.config_values(config)
            if link.get('format') == 'shell-defaults':
                # Existing native sync scripts keep their binding in literal
                # ${NAME:-default} assignments. Read those defaults, never run it.
                import re
                for key in ('REMOTE_HOST', 'REMOTE_PROJECT_DIR'):
                    value = project.get(key, '')
                    match = re.fullmatch(r'\$\{[A-Z_]+:-(.*)\}', value)
                    if match:
                        project[key] = match.group(1)
            if project.get('REMOTE_HOST') != settings['REMOTE_HOST']:
                continue
            root = (project.get(link['root_key']) if link.get('root_key') else
                    project.get('REMOTE_PROJECT_DIR') or project.get('HPC_DASH_PRIMARY_ROOT'))
            if not root:
                root = project['REMOTE_BASE_DIR'].rstrip('/') + '/' + project['PROJECT_SLUG']
            if not Path(root).is_absolute():
                raise ValueError('Expected an absolute remote project root')
            roots.add(root)
        except (OSError, ValueError, KeyError, TypeError) as error:
            print('WARNING: cannot read project reference {}: {}'.format(reference, error), file=sys.stderr)
    return sorted(roots)


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
    legacy = catalogue_root / 'configs/hpc_tasks.json'
    payload = json.dumps({'source': source.read_text(), 'settings': settings,
                          'tasks': json.loads(legacy.read_text()) if legacy.exists() else [],
                          'project_roots': linked_project_roots(collector, settings)})
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
