#!/usr/bin/env bash
# deploy/demo/diskguard.sh: every 10 minutes (systemd/controlvar-demo-diskguard.timer).
#
# At DEMO_DISK_LIMIT percent (default 80) used on the filesystem that holds
# Docker's data, it first removes unused images and build cache; if that is
# not enough, it runs reset.sh, whose `down -v` drops everything visitors
# wrote since the last reset. README.md, "Disk", has the arithmetic.
set -euo pipefail

# shellcheck source=deploy/demo/lib.sh
. "$(dirname "$0")/lib.sh"

limit=${DEMO_DISK_LIMIT:-80}
data_root=$(docker info --format '{{.DockerRootDir}}')

used() {
    df --output=pcent "$data_root" | tail -n 1 | tr -dc '0-9'
}

now=$(used)
if [ -z "$now" ]; then
    demo_log "diskguard: could not read the disk use of $data_root"
    exit 1
fi
if [ "$now" -lt "$limit" ]; then
    exit 0
fi

demo_log "diskguard: ${now}% used on $data_root (limit ${limit}%); pruning unused images and build cache"
docker image prune -f >/dev/null
docker builder prune -f >/dev/null
now=$(used)
if [ "$now" -lt "$limit" ]; then
    demo_log "diskguard: ${now}% after pruning"
    exit 0
fi

demo_log "diskguard: still ${now}%; resetting"
exec "$DEMO_RECIPE/reset.sh"
