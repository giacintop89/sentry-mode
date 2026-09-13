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
import sys
root, user = sys.argv[1:]
def quoted(value):
    return '"' + value.replace('\\', '\\\\').replace('"', '\\"').replace('%', '%%') + '"'
text = (pathlib.Path(root) / 'systemd/sentry-node.service').read_text()
text = text.replace('User=sentry-node', 'User=' + user)
text = text.replace('WorkingDirectory=/opt/sentry-node', 'WorkingDirectory=' + quoted(root))
text = text.replace('EnvironmentFile=-/opt/sentry-node/.env',
                    'EnvironmentFile=-' + quoted(root + '/.env'))
text = text.replace('ExecStart=/opt/sentry-node/.venv/bin/sentry-node run',
                    'ExecStart=' + quoted(root + '/.venv/bin/sentry-node') + ' run')
uid = __import__('pwd').getpwnam(user).pw_uid
text = text.replace('Type=simple', f'Type=simple\nEnvironment=XDG_RUNTIME_DIR=/run/user/{uid}\n'
                    f'Environment=PULSE_SERVER=unix:/run/user/{uid}/pulse/native')
pathlib.Path('/etc/systemd/system/sentry-node.service').write_text(text)
PY
systemctl daemon-reload
echo 'Installed without enabling. Review: systemctl cat sentry-node'
echo 'To start explicitly: sudo systemctl enable --now sentry-node'
