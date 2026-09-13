#!/usr/bin/env bash
set -euo pipefail
if [ "$(id -u)" -ne 0 ] || [ -z "${SUDO_USER:-}" ] || [ "$SUDO_USER" = root ]; then
    echo 'Run with sudo from the normal account that will run Sentry Node.' >&2
    exit 1
fi
root_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
[ -x "$root_dir/.venv/bin/sentry-node" ] || { echo 'Create .venv and install first.' >&2; exit 1; }
# Render literal paths using Python, including systemd quoting.
python3 - "$root_dir" "$SUDO_USER" <<'PY'
import pathlib
import pwd
import re
import shlex
import sys

TEMPLATE_ROOT = '/opt/sentry-node'
UNITS = ('sentry-node.service', 'sentry-node-web.service')
TLS_OPTIONS = ('--https-port', '--tls-cert', '--tls-key', '--tls-ca')
SAFE = re.compile(r'[A-Za-z0-9_@:./,=+-]+')
root, user = sys.argv[1:]
uid = pwd.getpwnam(user).pw_uid


def quoted(value):
    if SAFE.fullmatch(value):
        return value
    return '"' + value.replace('\\', '\\\\').replace('"', '\\"').replace('%', '%%') + '"'


def relocated(value):
    if value == TEMPLATE_ROOT or value.startswith(TEMPLATE_ROOT + '/'):
        return root + value[len(TEMPLATE_ROOT):]
    return value


def without_tls(tokens):
    kept, skip = [], False
    for token in tokens:
        if skip:
            skip = False
        elif token in TLS_OPTIONS:
            skip = True
        else:
            kept.append(token)
    return kept


def exec_start(line):
    tokens = [relocated(token) for token in shlex.split(line.partition('=')[2])]
    missing = [
        tokens[index + 1]
        for index, token in enumerate(tokens[:-1])
        if token.startswith('--tls-') and not pathlib.Path(tokens[index + 1]).exists()
    ]
    if missing:
        print('Missing ' + ', '.join(missing) + '; installing HTTP only.')
        print('Run scripts/setup_phone_https.py, then rerun this installer for HTTPS.')
        tokens = without_tls(tokens)
    return 'ExecStart=' + ' '.join(quoted(token) for token in tokens)


for unit in UNITS:
    lines = []
    for line in (pathlib.Path(root) / 'systemd' / unit).read_text().splitlines():
        key, _, value = line.partition('=')
        if key == 'User':
            line = 'User=' + user
        elif key in ('WorkingDirectory', 'EnvironmentFile'):
            optional = '-' if value.startswith('-') else ''
            line = f'{key}={optional}{quoted(relocated(value[len(optional):]))}'
        elif key == 'ExecStart':
            line = exec_start(line)
        elif key == 'Type':
            line += (f'\nEnvironment=XDG_RUNTIME_DIR=/run/user/{uid}'
                     f'\nEnvironment=PULSE_SERVER=unix:/run/user/{uid}/pulse/native')
        lines.append(line)
    pathlib.Path('/etc/systemd/system/' + unit).write_text('\n'.join(lines) + '\n')
PY
systemctl daemon-reload
echo 'Installed without enabling. Review: systemctl cat sentry-node sentry-node-web'
echo 'To start explicitly: sudo systemctl enable --now sentry-node'
echo 'Dashboard on 8083/8443:  sudo systemctl enable --now sentry-node-web'
