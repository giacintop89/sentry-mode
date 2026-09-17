#!/usr/bin/env bash
# Take a copy of everything that is this node rather than this release: its configuration,
# its identity, its certificates, and the sources the hub last sent it. The release itself
# is not in here, because it can be installed again from its own file.
#
# The file it writes holds a private key. It is created 0600 and owned by root, and it
# belongs somewhere that stays that way.
#
#   sudo ./backup.sh --into /var/backups
set -euo pipefail

into=''
prefix=''
service=1

while [ $# -gt 0 ]; do
    case "$1" in
        --into) into="${2:?--into needs a directory}"; shift 2 ;;
        --prefix) prefix="${2:?--prefix needs a directory}"; shift 2 ;;
        --no-service) service=0; shift ;;
        -h|--help) sed -n '2,10p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) echo "unknown option: $1" >&2; exit 2 ;;
    esac
done

[ -n "$into" ] || { echo 'Say where to write the backup with --into.' >&2; exit 2; }
[ -n "$prefix" ] && service=0
root="${prefix:-/}"
[ -d "$root/etc/sentry-satellite" ] || { echo "$root/etc/sentry-satellite does not exist." >&2; exit 1; }

install -d -m 0700 "$into"
stamp="$(date -u +%Y-%m-%dT%H-%M-%SZ)"
target="$into/sentry-satellite-$stamp.tar.gz"

# Stopped while the copy is taken, so the sources file cannot be half written into it.
restart=0
if [ "$service" -eq 1 ] && systemctl is-active --quiet sentry-satellite.service; then
    systemctl stop sentry-satellite.service
    restart=1
fi

umask 077
tar -czf "$target" -C "$root" \
    --exclude='*.sock' \
    etc/sentry-satellite \
    $([ -d "$root/var/lib/sentry-satellite" ] && echo var/lib/sentry-satellite)
chmod 0600 "$target"

[ "$restart" -eq 1 ] && systemctl start sentry-satellite.service

echo "$target"
tar -tzf "$target" | sed 's/^/  /'
