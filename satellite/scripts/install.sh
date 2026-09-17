#!/usr/bin/env bash
# Install or update the satellite agent from a release built by `sentry-satellite package`.
#
# It is safe to run again: the same release changes nothing, a new one is unpacked beside
# the old one and the `current` symlink moves in a single step. It never writes over a
# configuration, an identity or a certificate, and it touches no service but its own.
#
#   sudo ./install.sh --release dist/sentry-satellite-0.1.0.tar.gz
#   ./install.sh --release … --prefix /tmp/staged      # a staged copy, for a dry run
set -euo pipefail

release=''
prefix=''
service=1
user=sentry-satellite

while [ $# -gt 0 ]; do
    case "$1" in
        --release) release="${2:?--release needs a file}"; shift 2 ;;
        --prefix) prefix="${2:?--prefix needs a directory}"; shift 2 ;;
        --no-service) service=0; shift ;;
        -h|--help) sed -n '2,9p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) echo "unknown option: $1" >&2; exit 2 ;;
    esac
done

[ -n "$release" ] || { echo 'Say which release to install with --release.' >&2; exit 2; }
[ -f "$release" ] || { echo "$release is not a file." >&2; exit 2; }
release="$(cd -- "$(dirname -- "$release")" && pwd)/$(basename -- "$release")"

# A staged install goes under a prefix and asks nothing of systemd or of the user
# database: it is for looking at what would be written, and for the tests.
staged=0
if [ -n "$prefix" ]; then
    staged=1
    service=0
elif [ "$(id -u)" -ne 0 ]; then
    echo 'Run with sudo, or pass --prefix to stage the install somewhere you own.' >&2
    exit 1
fi

opt="$prefix/opt/sentry-satellite"
etc="$prefix/etc/sentry-satellite"
state="$prefix/var/lib/sentry-satellite"
units="$prefix/etc/systemd/system"

version="$(basename "$release" .tar.gz)"
version="${version#sentry-satellite-}"
target="$opt/releases/$version"

install -d -m 0755 "$opt" "$opt/releases" "$units"
install -d -m 0750 "$etc"
install -d -m 0700 "$state"

# Unpacked beside the release it replaces, and only moved into place once it checks out.
staging="$(mktemp -d "$opt/.incoming-XXXXXX")"
trap 'rm -rf "$staging"' EXIT
tar -xzf "$release" -C "$staging"
PYTHONPATH="$staging/src" python3 -B -m sentry_satellite.cli verify --release "$staging" >/dev/null || {
    echo "$release does not match its own manifest; nothing was installed." >&2
    exit 1
}

changed=1
if [ -d "$target" ] && python3 - "$staging" "$target" <<'PY'
import filecmp, sys
from pathlib import Path
new, old = Path(sys.argv[1]), Path(sys.argv[2])
def files(root):  # bytecode a check left behind is not part of the release
    return {p.relative_to(root) for p in root.rglob('*')
            if p.is_file() and '__pycache__' not in p.parts}
if files(new) != files(old):
    raise SystemExit(1)
raise SystemExit(0 if all(filecmp.cmp(new / n, old / n, shallow=False) for n in files(new)) else 1)
PY
then
    changed=0
else
    rm -rf "$target"
    mv "$staging" "$target"
    trap - EXIT
fi
chmod -R go-w "$target"

# One step, so a board that loses power is running either the old release or the new one.
ln -sfn "$target" "$opt/.current.new"
mv -T "$opt/.current.new" "$opt/current"

# The example is a starting point, never an overwrite: a node's own file stays as it is.
if [ ! -f "$etc/node.toml" ]; then
    install -m 0640 "$target/config/sensor-presence.example.toml" "$etc/node.toml"
    echo "Wrote $etc/node.toml from the example. Edit it before starting the service."
fi

if [ "$staged" -eq 0 ]; then
    getent group "$user" >/dev/null || groupadd --system "$user"
    getent passwd "$user" >/dev/null || useradd --system --gid "$user" \
        --home-dir /var/lib/sentry-satellite --shell /usr/sbin/nologin "$user"
    chown root:"$user" "$etc"
    chown -R "$user":"$user" "$state"
    for secret in node.toml identity.json node.key node.crt ca.crt; do
        [ -f "$etc/$secret" ] && chown root:"$user" "$etc/$secret" && chmod 0640 "$etc/$secret"
    done
fi

unit="$units/sentry-satellite.service"
if ! cmp -s "$target/systemd/sentry-satellite.service" "$unit"; then
    install -m 0644 "$target/systemd/sentry-satellite.service" "$unit"
    changed=1
    [ "$service" -eq 1 ] && systemctl daemon-reload
fi

if [ "$service" -eq 1 ]; then
    systemctl enable sentry-satellite.service >/dev/null
    if [ "$changed" -eq 1 ] && systemctl is-active --quiet sentry-satellite.service; then
        systemctl restart sentry-satellite.service
    fi
fi

if [ "$changed" -eq 1 ]; then
    echo "Installed $version at $target and pointed $opt/current at it."
else
    echo "$version was already installed; nothing changed."
fi
