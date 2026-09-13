#!/usr/bin/env bash
set -eu
for tool in wpctl pactl arecord aplay bluetoothctl; do
    if ! command -v "$tool" >/dev/null; then
        echo "$tool unavailable"
        continue
    fi
    case "$tool" in
        wpctl) wpctl status || true ;;
        pactl)
            pactl info || true
            pactl list short sinks || true
            pactl list short sources || true
            ;;
        arecord) arecord -l || true ;;
        aplay) aplay -l || true ;;
        bluetoothctl) timeout 5 bluetoothctl devices || true ;;
    esac
done
