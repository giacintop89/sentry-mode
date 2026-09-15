#!/usr/bin/env bash
set -euo pipefail
root_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
if [ ! -x "$root_dir/.venv/bin/python" ]; then
    echo 'Create .venv and install the application first.' >&2
    exit 1
fi
"$root_dir/.venv/bin/python" -m pip install -e "$root_dir[speech]"
# One Kokoro model speaks every voice, so it is fetched once and kept out of the repository.
kokoro_dir="$root_dir/models/kokoro"
kokoro_release=https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0
mkdir -p "$kokoro_dir"
for file in kokoro-v1.0.onnx voices-v1.0.bin; do
    if [ -s "$kokoro_dir/$file" ]; then
        echo "Kokoro $file already downloaded."
    else
        echo "Downloading Kokoro $file..."
        curl -fSL --retry 3 -o "$kokoro_dir/$file.part" "$kokoro_release/$file"
        mv "$kokoro_dir/$file.part" "$kokoro_dir/$file"
    fi
done
echo 'English and Italian female Studio voices ready.'
echo 'Restart sentry-mode serve to refresh voice choices.'
