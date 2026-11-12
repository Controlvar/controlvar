#!/usr/bin/env bash
# deploy/demo/reset.sh: rebuild the demo from nothing, then probe it.
#
# The nightly timer (systemd/controlvar-demo-reset.timer), check.sh (after two
# probes without an answer), diskguard.sh and redeploy.sh run it; so may an
# operator. In order: stop the public services (Caddy serves the rebuild
# page), `down -v`, start postgres and redis, run the seeds, apply
# lockdown.sql, start the rest, then the probe's reset checks (lib.sh,
# DEMO_RESET_CHECKS). It never leaves the demo open unprobed:
#   - passed: the demo is open; the stamp and any closed-wrong marker are
#     updated;
#   - a wrong answer (exit 3): closed at once, marker written, exit 3;
#   - no answer twice, a failed step or lockdown's refusal: closed, exit 1;
#     the next hourly check finds no answer and runs this again, unless the
#     closed-wrong marker is present: then the hourly check neither probes
#     nor resets, and the demo stays closed until the nightly reset or an
#     operator's (by design).
#
# Environment: DEMO_RELEASE, DEMO_ENV_FILE, DEMO_STATE_DIR (lib.sh).
set -euo pipefail

# shellcheck source=deploy/demo/lib.sh
. "$(dirname "$0")/lib.sh"

demo_require
demo_lock
trap 'demo_close_on_failure $? reset' EXIT
# systemd's stop or timeout (TERM): exit through the EXIT trap, which closes.
trap 'exit 143' TERM INT HUP

started=$(date -u +%Y-%m-%dT%H:%M:%SZ)
demo_log "reset: started $started"
demo_rebuild
rc=0
# shellcheck disable=SC2086 # a list of check names
demo_probe_twice $DEMO_RESET_CHECKS || rc=$?
case $rc in
    0)
        rm -f "$DEMO_WRONG_MARKER"
        date -u +%Y-%m-%dT%H:%M:%SZ >"$DEMO_STATE_DIR/last-reset"
        demo_log "reset: passed (started $started)"
        ;;
    3)
        demo_close_wrong "the reset's probe got a wrong answer"
        exit 3
        ;;
    *)
        demo_close "the reset's probe failed (exit $rc)"
        exit 1
        ;;
esac
