#!/usr/bin/env bash
set -euo pipefail
root_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
if [ ! -x "$root_dir/.venv/bin/python" ]; then
    echo 'Create .venv and install the application first.' >&2
    exit 1
fi
"$root_dir/.venv/bin/python" -m pip install -e "$root_dir[speech]"
mkdir -p "$root_dir/models/piper"
"$root_dir/.venv/bin/python" -m piper.download_voices --data-dir "$root_dir/models/piper" \
    en_US-lessac-medium en_US-amy-medium en_US-kristin-medium \
    en_GB-alba-medium en_GB-cori-medium en_GB-jenny_dioco-medium it_IT-paola-medium
echo 'English and Italian female neural voices ready. Restart sentry-node serve to refresh voice choices.'
