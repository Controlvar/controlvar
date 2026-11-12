#!/usr/bin/env bash
# deploy/demo/provision.sh: set up a fresh Ubuntu 24.04 box as the demo.
#
#   sudo DEMO_ALLOW_FROM=<your address>/32 bash provision.sh vX.Y.Z [<recipe ref>]
#
# Run it after the three A records exist (it checks). It installs Docker, the
# compose plugin and Caddy from their own apt repositories; sets Docker's log
# rotation, a 2 GB swap file and a journald cap; clones the recipe (default
# ref: main) and the release (the tag) under /opt/controlvar-demo; writes
# /etc/controlvar-demo/ (box.env, demo.env from generate-env.sh, caddy.env);
# points Caddy at the recipe's Caddyfile behind the DEMO_ALLOW_FROM allow-list;
# installs and starts the three timers; then runs redeploy.sh for the tag.
#
# Safe to run again: it keeps existing clones and the existing demo.env, and
# rewrites the rest. It never opens the demo: DEMO_ALLOW_FROM must name
# addresses, and opening is README.md's "Open the demo".
set -euo pipefail

BASE=/opt/controlvar-demo
CONF=/etc/controlvar-demo
REPO=https://github.com/Controlvar/controlvar.git
HOSTS="demo.controlvar.com shoplab.demo.controlvar.com streampulse.demo.controlvar.com"

die() {
    printf 'provision: %s\n' "$*" >&2
    exit 1
}

[ "$(id -u)" = 0 ] || die "run it as root (sudo)"
tag=${1:-}
recipe_ref=${2:-main}
printf '%s\n' "$tag" | grep -Eqx 'v[0-9]+\.[0-9]+\.[0-9]+' ||
    die "usage: provision.sh vX.Y.Z [<recipe ref>], got '${tag}'"
allow=${DEMO_ALLOW_FROM:-}
[ -n "$allow" ] || die "set DEMO_ALLOW_FROM to the addresses that may see the demo while it is rehearsed"
case " $allow " in
    *" 0.0.0.0/0 "* | *" ::/0 "*) die "DEMO_ALLOW_FROM opens the demo to everyone; that is README.md's 'Open the demo', not provisioning" ;;
esac
for host in $HOSTS; do
    getent ahostsv4 "$host" >/dev/null || die "$host does not resolve yet; add the A records first"
done

export DEBIAN_FRONTEND=noninteractive
apt-get update -q
apt-get install -y -q ca-certificates curl gnupg git python3 unattended-upgrades \
    debian-keyring debian-archive-keyring apt-transport-https

# Docker and the compose plugin (docs.docker.com/engine/install/ubuntu).
install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
chmod a+r /etc/apt/keyrings/docker.asc
# shellcheck disable=SC1091 # the box's own os-release
codename=$(. /etc/os-release && echo "$VERSION_CODENAME")
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu $codename stable" \
    >/etc/apt/sources.list.d/docker.list
# Caddy (caddyserver.com/docs/install, Debian/Ubuntu stable).
curl -1sLf https://dl.cloudsmith.io/public/caddy/stable/gpg.key |
    gpg --dearmor --yes -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
curl -1sLf https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt \
    >/etc/apt/sources.list.d/caddy-stable.list
apt-get update -q
apt-get install -y -q docker-ce docker-ce-cli containerd.io docker-buildx-plugin \
    docker-compose-plugin caddy

# Docker's own default for containers the overlay does not name.
cat >/etc/docker/daemon.json <<'EOF'
{"log-driver": "json-file", "log-opts": {"max-size": "10m", "max-file": "3"}}
EOF
systemctl restart docker

if [ ! -e /swapfile ]; then
    fallocate -l 2G /swapfile
    chmod 600 /swapfile
    mkswap /swapfile >/dev/null
    swapon /swapfile
    grep -q '^/swapfile ' /etc/fstab || echo '/swapfile none swap sw 0 0' >>/etc/fstab
fi
mkdir -p /etc/systemd/journald.conf.d
printf '[Journal]\nSystemMaxUse=200M\n' >/etc/systemd/journald.conf.d/controlvar-demo.conf
systemctl restart systemd-journald

# The two checkouts (README.md, "Layout").
mkdir -p "$BASE"
[ -d "$BASE/recipe/.git" ] || git clone --quiet "$REPO" "$BASE/recipe"
git -C "$BASE/recipe" fetch --quiet origin
# A branch name means the remote's branch, not the clone's stale local copy.
recipe_sha=$(git -C "$BASE/recipe" rev-parse --verify --quiet "origin/$recipe_ref^{commit}" ||
    git -C "$BASE/recipe" rev-parse --verify "$recipe_ref^{commit}") ||
    die "no commit named $recipe_ref"
git -C "$BASE/recipe" checkout --quiet --detach "$recipe_sha"
echo "provision: recipe at $recipe_sha"
[ -d "$BASE/release/.git" ] || git clone --quiet "$REPO" "$BASE/release"
recipe=$BASE/recipe/deploy/demo
[ -f "$recipe/reset.sh" ] || die "the recipe ref $recipe_ref has no deploy/demo/reset.sh"

install -d -m 0700 "$CONF"
cat >"$CONF/box.env" <<EOF
DEMO_RELEASE=$BASE/release
DEMO_ENV_FILE=$CONF/demo.env
DEMO_STATE_DIR=/var/lib/controlvar-demo
EOF
if [ ! -e "$CONF/demo.env" ]; then
    PUBLIC_BASE_URL=https://demo.controlvar.com ENV_FILE="$CONF/demo.env" "$recipe/generate-env.sh"
fi
(umask 077 && printf 'DEMO_ALLOW_FROM=%s\n' "$allow" >"$CONF/caddy.env")

# Caddy: the recipe's sites, with the allow-list from caddy.env.
printf 'import %s/Caddyfile\n' "$recipe" >/etc/caddy/Caddyfile
mkdir -p /etc/systemd/system/caddy.service.d
printf '[Service]\nEnvironmentFile=%s/caddy.env\n' "$CONF" \
    >/etc/systemd/system/caddy.service.d/controlvar-demo.conf

# The units and timers, then the first deploy (it runs the first reset; on a
# release without demo mode that reset fails closed, which is the point of
# rehearsing on one: README.md, "Rehearsal").
install -m 0644 "$recipe"/systemd/controlvar-demo-*.service \
    "$recipe"/systemd/controlvar-demo-*.timer /etc/systemd/system/
systemctl daemon-reload
systemctl restart caddy
systemctl enable --now controlvar-demo-reset.timer controlvar-demo-check.timer \
    controlvar-demo-diskguard.timer
DEMO_RELEASE=$BASE/release DEMO_ENV_FILE=$CONF/demo.env DEMO_STATE_DIR=/var/lib/controlvar-demo \
    "$recipe/redeploy.sh" "$tag"
echo "provision: done. The demo answers only DEMO_ALLOW_FROM ($allow) until it is opened."
