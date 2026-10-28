"""Upgrade existing PBS projects without replacing custom launchers."""
import argparse
import ast
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys

PACKAGE = Path(__file__).resolve().parents[1]
TEMPLATE = PACKAGE / 'template'
SKIP = {'.git', 'outputs', 'output', 'tmp', 'archive', 'archives', 'history',
        'data', 'datasets', 'Datasets', 'node_modules', '__pycache__', '.venv', 'backups', 'tests'}
MARKER = '# BEGIN HPC AUTOMATIC REGISTRATION'
SHELL_HOOK = '''# BEGIN HPC AUTOMATIC REGISTRATION
for _hpc_candidate in "${PBS_O_WORKDIR:-$PWD}" "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)" "$PWD"; do
    _hpc_directory="${_hpc_candidate}"
    while [[ "${_hpc_directory}" != / ]]; do
        if [[ -f "${_hpc_directory}/.workflow/remote/auto_register.sh" ]]; then
            source "${_hpc_directory}/.workflow/remote/auto_register.sh"
            break 2
        fi
        _hpc_directory="$(dirname "${_hpc_directory}")"
    done
done
# END HPC AUTOMATIC REGISTRATION
'''
PYTHON_HOOK = '''# BEGIN HPC AUTOMATIC REGISTRATION
import importlib.util as _hpc_importlib
from pathlib import Path as _HpcPath
_hpc_root = next(p for p in _HpcPath(__file__).resolve().parents
                 if (p / '.workflow/remote/qsub_register.py').is_file())
_hpc_spec = _hpc_importlib.spec_from_file_location('_project_qsub_registration', _hpc_root / '.workflow/remote/qsub_register.py')
_hpc_registration = _hpc_importlib.module_from_spec(_hpc_spec)
_hpc_spec.loader.exec_module(_hpc_registration)
_hpc_qsub_path = _hpc_registration.qsub_path
# END HPC AUTOMATIC REGISTRATION
'''


def shell_hook(text):
    if MARKER in text:
        return text
    lines = text.splitlines(keepends=True)
    # PBS directives must stay before the first executable statement.
    at = next((i for i, line in enumerate(lines) if line.strip() and not line.lstrip().startswith('#')), len(lines))
    return ''.join(lines[:at]) + SHELL_HOOK + '\n' + ''.join(lines[at:])


def python_hook(text):
    if MARKER in text:
        return text
    tree = ast.parse(text)
    offsets = [0]
    for line in text.splitlines(keepends=True):
        offsets.append(offsets[-1] + len(line))

    def position(line, column):
        # AST columns count UTF-8 bytes, not Unicode characters.
        source_line = text.splitlines(keepends=True)[line - 1]
        return offsets[line - 1] + len(source_line.encode()[:column].decode())

    replacements = []
    for node in ast.walk(tree):
        replacement = None
        if isinstance(node, ast.Constant) and node.value == '/opt/pbs/bin/qsub':
            replacement = "_hpc_qsub_path('/opt/pbs/bin/qsub')"
        elif (isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add)
              and isinstance(node.left, ast.Name) and node.left.id == 'PBS'
              and isinstance(node.right, ast.Constant) and node.right.value == 'qsub'):
            replacement = "_hpc_qsub_path(PBS + 'qsub')"
        elif (isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add)
              and isinstance(node.left, ast.Name) and node.left.id == 'PBS'
              and isinstance(node.right, ast.Name) and node.right.id == 'name'):
            replacement = '_hpc_qsub_path(PBS + name)'
        if replacement is not None:
            replacements.append((position(node.lineno, node.col_offset),
                                 position(node.end_lineno, node.end_col_offset), replacement))
    if not replacements:
        return text
    for start, end, value in sorted(replacements, reverse=True):
        text = text[:start] + value + text[end:]
    # Module docstring and future imports must remain at the beginning.
    tree = ast.parse(text)
    at = 0
    for node in tree.body:
        if ((isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant)
             and isinstance(node.value.value, str)) or
                isinstance(node, ast.ImportFrom) and node.module == '__future__'):
            at = node.end_lineno
        else:
            break
    if at == 0:
        at = next((i for i, line in enumerate(text.splitlines())
                   if line.strip() and not line.lstrip().startswith('#')), 0)
    lines = text.splitlines(keepends=True)
    return ''.join(lines[:at]) + '\n' + PYTHON_HOOK + '\n' + ''.join(lines[at:])


def remote_exp_hook(text):
    if '"/.workflow/backups/",' not in text:
        text = text.replace('        excludes = [\n', '        excludes = [\n            "/.workflow/backups/",\n')
    if '# HPC automatic registration for isolated source snapshots' in text:
        return text
    anchor = '        pbs_script = self.build_pbs_script(experiment, run_dir, source_mode)\n'
    setup = '''        # HPC automatic registration for isolated source snapshots
        registration_lines = ['mkdir -p "$RUN_DIR/meta/hpc_registration"']
        for registration_name in ('task_registry.py', 'qsub_register.py'):
            registration_source = (ROOT / '.workflow/remote' / registration_name).read_text()
            registration_lines.append(
                "printf '%s' " + shlex.quote(b64_text(registration_source)) +
                ' | base64 -d > "$RUN_DIR/meta/hpc_registration/' + registration_name + '"')
        registration_setup = "\\n".join(registration_lines)
'''
    old = 'if ! qsub "$RUN_DIR/meta/job.pbs" >"$QSUB_LOG" 2>&1; then'
    new = '''{registration_setup}
if ! HPC_REAL_QSUB_BIN="$(command -v qsub)" HPC_REGISTRATION_ROOT="$REMOTE_REPO" \\
  HPC_REGISTER_TASK_ID={shlex.quote(experiment.name)} \\
  HPC_REGISTER_RUN_ID={shlex.quote(str(metadata['run_id']))} \\
  HPC_REGISTER_NATIVE_RECEIPT="$RUN_DIR/meta/run.json" \\
  "$PYTHON_BIN" "$RUN_DIR/meta/hpc_registration/qsub_register.py" "$RUN_DIR/meta/job.pbs" >"$QSUB_LOG" 2>&1; then'''
    if text.count(anchor) != 1 or text.count(old) != 1:
        raise ValueError('Unexpected remote-exp submission layout; refusing an automatic rewrite')
    return text.replace(anchor, anchor + setup).replace(old, new)


def plans(project):
    changes = {}
    managed = ['.workflow/remote/task_registry.py', '.workflow/remote/qsub_register.py',
                     '.workflow/remote/hpc-qsub', '.workflow/remote/auto_register.sh',
                     '.workflow/remote/task_status.py', 'scripts/render_hpc_status.py', 'scripts/hpc-status']
    for relative in managed:
        path = project / relative
        value = (TEMPLATE / relative).read_text()
        if not path.exists() or path.read_text() != value:
            changes[relative] = value
    files = subprocess.run(['rg', '--files', '--hidden', '-g', '*.py', '-g', '*.sh', '-g', '*.pbs',
                            '-g', 'remote-workflow', str(project)], text=True,
                           stdout=subprocess.PIPE, check=True).stdout.splitlines()
    for filename in files:
        path = Path(filename)
        relative = path.relative_to(project)
        if set(relative.parts) & SKIP or str(relative) in managed:
            continue
        text = path.read_text(errors='replace')
        if (path.name.startswith('sync') and path.name.endswith('to_cetus.sh')
                and 'rsync' in text and "--exclude '.workflow/backups/'" not in text):
            value = text.replace('--exclude ', "--exclude '.workflow/backups/' \\\n  --exclude ", 1)
            if value != text:
                changes[str(relative)] = value
                text = value
        if not re.search(r'\bqsub\b', text):
            continue
        if str(relative) == 'tools/remote_exp.py':
            value = remote_exp_hook(text)
            if value != text:
                changes[str(relative)] = value
            continue
        value = python_hook(text) if path.suffix == '.py' else shell_hook(text)
        if str(relative) == 'scripts/hpo_auto_continue.sh':
            # The CPU relay is generated as a standalone PBS script. Preserve
            # its original qsub executable while connecting the generated call.
            value = value.replace('qsub_bin="${qsub_bin}"',
                'export HPC_REAL_QSUB_BIN="${qsub_bin}"\nqsub_bin="${repo_workdir}/.workflow/remote/hpc-qsub"')
        if value != text:
            changes[str(relative)] = value
    # Protect backups when a project uses the existing sync exclusion file.
    excludes = project / '.workflow/sync-exclude.txt'
    if excludes.exists() and '/.workflow/backups/' not in excludes.read_text().splitlines():
        changes['.workflow/sync-exclude.txt'] = excludes.read_text().rstrip() + '\n/.workflow/backups/\n'
    ignore = project / '.gitignore'
    ignored = ignore.read_text() if ignore.exists() else ''
    if '/.workflow/backups/' not in ignored.splitlines():
        changes['.gitignore'] = ignored.rstrip() + '\n/.workflow/backups/\n'
    rules = project / 'AGENTS.md'
    if rules.exists() and '## Automatic task registration upgrade' not in rules.read_text():
        changes['AGENTS.md'] = rules.read_text().rstrip() + '''

## Automatic task registration upgrade

- Existing PBS submission entrypoints now register through `.workflow/remote/qsub_register.py`; keep this adapter when changing submitters. Preserve the project's resource, retention, retry and continuation behavior. New experiments do not require appending to a central `hpc_tasks.json`.
- The project owns records in `outputs/runs/`; existing detailed task definitions/native receipts remain supported. Use explicit `HPC_TASK_ID`, `HPC_RUN_ID` and `HPC_JOB_ROLE` for new custom launchers. Give distinct experiments separate identities and output paths. Unknown completion/progress remains unverified; queue disappearance is never success.
- Updated collectors discover project-owned `configs/hpc_tasks.d/*.json` and automatic run records. Keep machine bindings in the project's configuration. `--history` includes completed records hidden by the default 24-hour view. Status queries remain read-only.
'''
    return changes


def apply(project, changes, stamp):
    backup = project / '.workflow/backups' / stamp
    for relative, value in changes.items():
        path = project / relative
        if path.exists():
            saved = backup / relative
            saved.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, saved)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(value)
        if path.name in {'hpc-qsub', 'hpc-status'}:
            path.chmod(path.stat().st_mode | 0o111)
    return str(backup)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('projects', nargs='+', type=Path)
    parser.add_argument('--dry-run', action='store_true')
    parser.add_argument('--report', type=Path, required=True)
    args = parser.parse_args()
    stamp = 'auto-registration-' + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    report = []
    for project in args.projects:
        project = project.resolve()
        changes = plans(project)
        row = dict(project=str(project), files=list(changes), dry_run=args.dry_run)
        if not args.dry_run:
            row['backup'] = apply(project, changes, stamp)
        report.append(row)
        print('{}: {} files'.format(project.name, len(changes)))
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')


if __name__ == '__main__':
    main()
