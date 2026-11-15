# shellcheck shell=bash
# deploy/demo/lib.sh: what reset.sh, check.sh, diskguard.sh and redeploy.sh
# share. Sourced, never run: bash only, and the caller sets `set -euo pipefail`.
#
# Two trees (README.md, "Layout"):
#   DEMO_RECIPE   this directory's repository: the overlay, Caddyfile, probe,
#                 lockdown.sql and these scripts.
#   DEMO_RELEASE  the release the box runs, a checkout at its tag:
#                 deploy/compose/compose.yml names that release's api and web
#                 images, and the storefronts and simulators are built and run
#                 from its demo/ and sdk/react/. Default: the same repository
#                 (the CI boot job tests one checkout's images and recipe).
#
# Compose's project directory is set to $DEMO_RELEASE/deploy/compose (the
# first file's, named rather than left to the default), so every relative path
# in compose.demo.yml resolves from the release checkout, not the recipe's.

DEMO_RECIPE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
DEMO_RELEASE=${DEMO_RELEASE:-$(cd "$DEMO_RECIPE/../.." && pwd)}
DEMO_ENV_FILE=${DEMO_ENV_FILE:-$DEMO_RECIPE/.env}
DEMO_STATE_DIR=${DEMO_STATE_DIR:-/var/lib/controlvar-demo}

#: The compose project (compose.demo.yml's `name`): its containers carry it in
#: the com.docker.compose.project label, which demo_close falls back on.
DEMO_PROJECT=controlvar-demo
#: The probe's checks after every rebuild, and every hour (check.sh).
# shellcheck disable=SC2034 # read by the scripts that source this file
DEMO_RESET_CHECKS="health home config signin refusal anonymous passwords superuser storefronts sdk"
# shellcheck disable=SC2034
DEMO_HOURLY_CHECKS="signin refusal anonymous passwords superuser"
#: What `close` stops: everything a visitor can reach through Caddy.
DEMO_PUBLIC_SERVICES="web shoplab streampulse"
#: Present while the demo is closed after a wrong answer (probe exit 3): the
#: hourly check leaves it closed; only a reset whose probe passes removes it.
DEMO_WRONG_MARKER=$DEMO_STATE_DIR/closed-wrong

demo_log() { printf '[controlvar-demo] %s\n' "$*" >&2; }

demo_require() {
    if [ ! -f "$DEMO_ENV_FILE" ]; then
        demo_log "no $DEMO_ENV_FILE: run deploy/demo/generate-env.sh first"
        return 1
    fi
    if [ ! -f "$DEMO_RELEASE/deploy/compose/compose.yml" ]; then
        demo_log "DEMO_RELEASE=$DEMO_RELEASE has no deploy/compose/compose.yml"
        return 1
    fi
    mkdir -p "$DEMO_STATE_DIR"
}

# The base file alone, for the one value the overlay cannot read from it.
demo_base_compose() {
    docker compose --project-directory "$DEMO_RELEASE/deploy/compose" \
        --env-file "$DEMO_ENV_FILE" \
        -f "$DEMO_RELEASE/deploy/compose/compose.yml" "$@"
}

# The api image the release's compose.yml names: the seed service runs the
# same image, and an overlay cannot refer to another file's value.
demo_api_image() {
    demo_base_compose config --format json |
        python3 -c 'import json, sys; print(json.load(sys.stdin)["services"]["api"]["image"])'
}

dc() {
    if [ -z "${DEMO_API_IMAGE:-}" ]; then
        DEMO_API_IMAGE=$(demo_api_image)
        export DEMO_API_IMAGE
    fi
    docker compose --project-directory "$DEMO_RELEASE/deploy/compose" \
        --env-file "$DEMO_ENV_FILE" \
        -f "$DEMO_RELEASE/deploy/compose/compose.yml" \
        -f "$DEMO_RECIPE/compose.demo.yml" "$@"
}

# One lock for reset.sh, check.sh and diskguard.sh: a check never runs in the
# middle of a rebuild, and two rebuilds never overlap. Waits for the holder.
# check.sh hands its lock to reset.sh by exec: descriptor 9 and the exported
# DEMO_LOCK_HELD survive the exec, so reset.sh keeps the lock it already holds
# instead of taking it again.
demo_lock() {
    if [ "${DEMO_LOCK_HELD:-}" = "$DEMO_STATE_DIR/lock" ] && { : >&9; } 2>/dev/null; then
        return 0
    fi
    exec 9>"$DEMO_STATE_DIR/lock"
    flock 9
    DEMO_LOCK_HELD=$DEMO_STATE_DIR/lock
    export DEMO_LOCK_HELD
}

# Through Caddy on this host. DEMO_PROBE_CAFILE adds a CA to trust (the CI
# boot job's Caddy issues its own certificates); unset on the box.
# DEMO_PROBE_TIMEOUT is each request's limit in seconds (default 10).
demo_probe() {
    local ca=()
    if [ -n "${DEMO_PROBE_CAFILE:-}" ]; then
        ca=(--cafile "$DEMO_PROBE_CAFILE")
    fi
    python3 "$DEMO_RECIPE/probe.py" --env-file "$DEMO_ENV_FILE" --connect 127.0.0.1 \
        --timeout "${DEMO_PROBE_TIMEOUT:-10}" "${ca[@]}" "$@"
}

# The probe, and once more after the login limit's window (10 a minute per
# address; one run signs in five times) when it got no answer (1). Returns the
# last run's status: 0 passed, 1 no answer twice, 3 a wrong answer (never
# retried), 2 a usage error.
demo_probe_twice() {
    local rc=0
    demo_probe "$@" || rc=$?
    if [ "$rc" -eq 1 ]; then
        demo_log "no answer from the demo; probing again in 65 s"
        sleep 65
        rc=0
        demo_probe "$@" || rc=$?
    fi
    return "$rc"
}

# Fail closed: stop everything a visitor can reach. Caddy then answers every
# host with the rebuild page. When compose itself fails (its interpolation, or
# the config call `dc` makes), the containers are stopped by their compose
# labels instead. DEMO_CLOSED is set only once they are stopped.
demo_close() {
    demo_log "CLOSING: $*"
    # shellcheck disable=SC2086 # a list of service names
    if dc stop $DEMO_PUBLIC_SERVICES; then
        DEMO_CLOSED=1
        demo_log "closed: $DEMO_PUBLIC_SERVICES stopped"
        return 0
    fi
    demo_log "compose could not stop them; stopping them by their labels"
    local service ids failed=0
    for service in $DEMO_PUBLIC_SERVICES; do
        if ! ids=$(docker ps -q \
            --filter "label=com.docker.compose.project=$DEMO_PROJECT" \
            --filter "label=com.docker.compose.service=$service"); then
            failed=1
            continue
        fi
        if [ -n "$ids" ]; then
            # shellcheck disable=SC2086 # one or more container ids
            docker stop $ids >/dev/null || failed=1
        fi
    done
    if [ "$failed" -ne 0 ]; then
        demo_log "could not stop $DEMO_PUBLIC_SERVICES; stop them by hand"
        return 1
    fi
    DEMO_CLOSED=1
    demo_log "closed: $DEMO_PUBLIC_SERVICES stopped by label"
}

# A wrong answer: close at once, and leave the marker that keeps the hourly
# check from reopening. Written first, so a close that fails still leaves it.
demo_close_wrong() {
    mkdir -p "$DEMO_STATE_DIR"
    printf '%s %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*" >"$DEMO_WRONG_MARKER"
    demo_close "$* (a wrong answer: closed until a reset passes its probe)"
}

# For the callers' EXIT trap: any failure that has not closed yet, closes.
demo_close_on_failure() {
    local rc=$1
    if [ "$rc" -ne 0 ] && [ -z "${DEMO_CLOSED:-}" ]; then
        demo_close "$2 failed (exit $rc)" || true
    fi
}

# From nothing to a seeded, locked-down, running stack. The first step stops
# web, so Caddy serves the rebuild page throughout; `down -v` removes every
# volume, so nothing a visitor wrote survives. The seeds date their history
# from today, so the data is rebuilt, never restored.
demo_rebuild() {
    demo_require
    local demo_email
    demo_email=$(sed -n 's/^DEMO_ACCOUNT_EMAIL=//p' "$DEMO_ENV_FILE")
    demo_log "rebuild: stopping the public services"
    # shellcheck disable=SC2086 # a list of service names
    dc stop $DEMO_PUBLIC_SERVICES
    demo_log "rebuild: down -v"
    dc down -v --remove-orphans
    demo_log "rebuild: postgres and redis"
    dc up -d --wait --wait-timeout 300 postgres redis
    demo_log "rebuild: seeds"
    dc run --rm seed
    demo_log "rebuild: lockdown"
    # ON_ERROR_STOP here and in lockdown.sql, both kept: with neither, psql
    # exits 0 when the file refuses, and the rollback leaves every seeded
    # password usable (test_demo_box.py pins each).
    # The address goes in as $0 and reaches SQL only through psql's quoting.
    # shellcheck disable=SC2016 # expanded by the container's shell
    dc exec -T postgres sh -c \
        'psql -X -q -v ON_ERROR_STOP=1 -v demo_email="$0" -U "$POSTGRES_USER" -d "$POSTGRES_DB" -f -' \
        "$demo_email" <"$DEMO_RECIPE/lockdown.sql"
    demo_log "rebuild: the rest of the stack"
    dc up -d --wait --wait-timeout 900
}
