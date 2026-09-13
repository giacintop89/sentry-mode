#!/usr/bin/env bash
set -eu
if command -v v4l2-ctl >/dev/null; then
    v4l2-ctl --list-devices || true
    for device in /dev/video*; do
        [ -e "$device" ] || continue
        printf '\nCapabilities for %s\n' "$device"
        v4l2-ctl --device="$device" --list-formats-ext || true
    done
else
    echo 'v4l2-ctl unavailable; install v4l-utils.'
fi
