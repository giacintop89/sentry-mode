#!/usr/bin/env bash
set -euo pipefail
root_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
if [ "$(id -u)" -eq 0 ]; then
    echo 'Run as your normal user; sudo is used only for OS packages.' >&2
    exit 1
fi
if [ ! -f /etc/debian_version ]; then
    echo 'This helper requires a Debian-like system.' >&2
    exit 1
fi
if ! tr -d '\0' < /proc/device-tree/model 2>/dev/null | grep -q 'Raspberry Pi'; then
    echo 'Raspberry Pi not detected; proceeding on Debian-compatible development host.'
fi
sudo apt-get update
packages=()
for package in python3 python3-venv python3-pip v4l-utils ffmpeg espeak-ng pipewire-bin pipewire-utils pulseaudio-utils alsa-utils openssh-client; do
    if apt-cache show "$package" >/dev/null 2>&1; then
        packages+=("$package")
    else
        echo "Skipping unavailable package: $package"
    fi
done
sudo apt-get install -y "${packages[@]}"
[ -d "$root_dir/.venv" ] || python3 -m venv "$root_dir/.venv"
"$root_dir/.venv/bin/python" -m pip install -e "$root_dir"
"$root_dir/scripts/detect_camera.sh"
"$root_dir/scripts/detect_audio.sh"
"$root_dir/.venv/bin/sentry-node" config validate
printf '\nNext: source %s/.venv/bin/activate\nsentry-node status\nsentry-node camera test\nsentry-node audio test-output\n' "$root_dir"
