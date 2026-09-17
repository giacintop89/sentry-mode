#!/usr/bin/env bash
# Go back to a release that is still on the board, and optionally to the data that went
# with it. Nothing is downloaded and nothing is built: a rollback is a symlink and, if a
# backup is named, a restore of /etc and /var/lib beside it.
#
#   sudo ./rollback.sh --list
#   sudo ./rollback.sh --to 0.1.0
#   sudo ./rollback.sh --to 0.1.0 --data /var/backups/sentry-satellite-2026-09-17.tar.gz
set -euo pipefail

to=''
data=''
prefix=''
service=1
list=0

while [ $# -gt 0 ]; do
    case "$1" in
        --to) to="${2:?--to needs a version}"; shift 2 ;;
        --data) data="${2:?--data needs a backup file}"; shift 2 ;;
        --prefix) prefix="${2:?--prefix needs a directory}"; shift 2 ;;
        --no-service) service=0; shift ;;
        --list) list=1; shift ;;
        -h|--help) sed -n '2,8p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) echo "unknown option: $1" >&2; exit 2 ;;
    esac
done

opt="$prefix/opt/sentry-satellite"
etc="$prefix/etc/sentry-satellite"
state="$prefix/var/lib/sentry-satellite"
[ -n "$prefix" ] && service=0

[ -d "$opt/releases" ] || { echo "$opt/releases does not exist: nothing is installed." >&2; exit 1; }

current="$(basename "$(readlink -f "$opt/current" 2>/dev/null || true)")"
if [ "$list" -eq 1 ]; then
    for path in "$opt"/releases/*/; do
        version="$(basename "$path")"
        [ "$version" = "$current" ] && echo "$version (current)" || echo "$version"
    done
    exit 0
fi

[ -n "$to" ] || { echo 'Say which release to go back to with --to, or use --list.' >&2; exit 2; }
target="$opt/releases/$to"
[ -d "$target" ] || { echo "$to is not on this board. Try --list." >&2; exit 1; }

PYTHONPATH="$target/src" python3 -B -m sentry_satellite.cli verify --release "$target" >/dev/null || {
    echo "$to is on the board but does not match its manifest; it was not made current." >&2
    exit 1
}

if [ -n "$data" ]; then
    [ -f "$data" ] || { echo "$data is not a file." >&2; exit 2; }
    [ "$service" -eq 1 ] && systemctl stop sentry-satellite.service || true
    # The configuration and the identity are restored as a pair: a node's certificate and
    # the file that says who it is belong to the same moment or to neither.
    tar -xzf "$data" -C "${prefix:-/}" etc/sentry-satellite var/lib/sentry-satellite
    echo "Restored $etc and $state from $data."
fi

ln -sfn "$target" "$opt/.current.new"
mv -T "$opt/.current.new" "$opt/current"
echo "$opt/current now points at $to."

if [ "$service" -eq 1 ]; then
    systemctl daemon-reload
    systemctl restart sentry-satellite.service
    systemctl --no-pager --lines 0 status sentry-satellite.service || true
fi
