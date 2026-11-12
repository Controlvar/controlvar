#!/usr/bin/env bash
# deploy/demo/check.sh: the hourly check on the box
# (systemd/controlvar-demo-check.timer).
#
# Runs the probe's hourly checks (lib.sh, DEMO_HOURLY_CHECKS: the demo account
# signs in as a viewer, writes are refused with and without credentials, the
# published passwords and FIRST_SUPERUSER's are refused) through Caddy on this
# host, after any reset that is running.
#   - passed: nothing to do.
#   - a wrong answer (exit 3): close at once and stay closed; only a reset
#     whose probe passes reopens (the next nightly one, or an operator's).
#   - no answer, twice 65 s apart: exec reset.sh, keeping the lock, so the
#     demo is rebuilt and reopens within the hour if its probe then passes.
#   - while closed after a wrong answer: no probe; keep it closed.
# What failed is in this unit's journal, never in a public place.
set -euo pipefail

# shellcheck source=deploy/demo/lib.sh
. "$(dirname "$0")/lib.sh"

demo_require
demo_lock
trap 'demo_close_on_failure $? check' EXIT
# systemd's stop or timeout (TERM): exit through the EXIT trap, which closes.
trap 'exit 143' TERM INT HUP

if [ -e "$DEMO_WRONG_MARKER" ]; then
    demo_log "closed after a wrong answer ($(cat "$DEMO_WRONG_MARKER")); only a reset that passes its probe reopens it"
    exit 3
fi

rc=0
# shellcheck disable=SC2086 # a list of check names
demo_probe_twice $DEMO_HOURLY_CHECKS || rc=$?
case $rc in
    0)
        exit 0
        ;;
    3)
        demo_close_wrong "the hourly check got a wrong answer"
        exit 3
        ;;
    1)
        demo_log "no answer twice; rebuilding now (reset.sh keeps this lock)"
        # A failed exec ends the shell at once, with no EXIT trap, unless
        # execfail is set AND errexit is off (measured, bash 5.3): then it
        # returns, and the exit below runs the trap, which closes the demo.
        shopt -s execfail
        set +e
        exec "$DEMO_RECIPE/reset.sh"
        set -e
        demo_log "could not run $DEMO_RECIPE/reset.sh"
        exit 1
        ;;
    *)
        exit "$rc"
        ;;
esac
