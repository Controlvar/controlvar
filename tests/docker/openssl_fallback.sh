#!/usr/bin/env bash
# tests/docker/openssl_fallback.sh -- the API image's OpenSSL fallback reaches
# every Python process in the container, checked in the BUILT image (#1265).
#
#   bash tests/docker/openssl_fallback.sh [image]   (default experimently-api:core)
#
# Run by the Docker Smoke job (pr-qa-gate.yml) for both profile images. It
# starts throwaway containers with no published port and no database (the
# entrypoint runs with WAIT_FOR_DB=false RUN_MIGRATIONS=false and starts
# `sleep infinity`), and removes them and their volumes again.
#
# The crash happens only on some arm64 hardware (Docker Desktop on an Apple
# M4), never on a CI runner, so this runs against a MODEL of that CPU: a
# stand-in cryptography/hazmat/bindings/_rust, first on PYTHONPATH, that kills
# its own process with SIGILL unless OPENSSL_armcap is "0" -- the observable
# behaviour of the real import there. The real entrypoint probe and the real
# start-up hook (backend/docker/sitecustomize.py) run against it unchanged.
# The cases for a CPU that does not crash use a second stand-in that always
# imports, so the script gives the same answers on any host, an M4 included.
#
# What a pass means:
#   - the model is live: without the fallback, the import exits 132;
#   - with the model, the entrypoint saves its decision, and a process started
#     later with `docker exec` imports with exit 0 and an empty stderr: bare
#     python, /opt/venv/bin/python, `sh -c`, and `-u root`; it also does so
#     after a restart, when the previous start's marker is still in /tmp;
#   - a marker left by an earlier start on a CPU that does NOT crash is
#     removed, and nothing gets the variable;
#   - with no model nothing gets the variable, and the image sets none;
#   - a read-only root with no writable /tmp still starts and says the marker
#     could not be saved;
#   - the Helm chart's shape (read-only root, writable /tmp, uid 1001) works.
# Every check runs and reports; the script fails at the end if any of them
# did, so one run shows every property that broke.
set -euo pipefail

image="${1:-experimently-api:core}"
run_id="openssl-fallback-$$"
work="$(mktemp -d)"
containers=()
volumes=()
failures=0

cleanup() {
    for name in ${containers[@]+"${containers[@]}"}; do
        docker rm -f "$name" >/dev/null 2>&1 || true
    done
    for name in ${volumes[@]+"${volumes[@]}"}; do
        docker volume rm -f "$name" >/dev/null 2>&1 || true
    done
    rm -rf "$work"
}
trap cleanup EXIT

PROBE='from cryptography.hazmat.bindings._rust import openssl'
PRINT='import os; print(os.environ.get("OPENSSL_armcap"))'
MARKER=/tmp/controlvar-openssl-armcap0
SAVED='for every Python process in this container'

# The two CPU models: one whose import crashes unless told, one that never does.
for cpu in crashing healthy; do
    mkdir -p "$work/$cpu/cryptography/hazmat/bindings/_rust"
    : >"$work/$cpu/cryptography/__init__.py"
    : >"$work/$cpu/cryptography/hazmat/__init__.py"
    : >"$work/$cpu/cryptography/hazmat/bindings/__init__.py"
    printf 'openssl = None\n' >"$work/$cpu/cryptography/hazmat/bindings/_rust/__init__.py"
done
cat >"$work/crashing/cryptography/hazmat/bindings/_rust/__init__.py" <<'PY'
import os
import signal

if os.environ.get("OPENSSL_armcap") != "0":
    os.kill(os.getpid(), signal.SIGILL)
openssl = None
PY
chmod -R a+rX "$work"
model=(-v "$work/crashing:/fake:ro" -e PYTHONPATH=/fake:/app)
healthy=(-v "$work/healthy:/fake:ro" -e PYTHONPATH=/fake:/app)
base=(-e ENVIRONMENT=development -e WAIT_FOR_DB=false -e RUN_MIGRATIONS=false -e SEED=)

fail() {
    printf '::error::%s: %s\n' "$image" "$*"
    failures=$((failures + 1))
}
pass() { printf 'ok   %s\n' "$*"; }

# start <name> [docker run args...]: one container, entrypoint through to
# `sleep infinity`. Returns non-zero (after reporting) when it never got there.
start() {
    local name="$run_id-$1"
    shift
    containers+=("$name")
    docker run -d --name "$name" "${base[@]}" "$@" "$image" sleep infinity >/dev/null
    for _ in $(seq 1 60); do
        if docker logs "$name" 2>&1 | grep -qF '[entrypoint] starting: sleep infinity'; then
            return 0
        fi
        if [ "$(docker inspect -f '{{.State.Running}}' "$name")" != true ]; then
            break
        fi
        sleep 1
    done
    docker logs "$name" 2>&1 | sed 's/^/    | /'
    fail "$name did not reach its command"
    return 1
}

# run_in <label> <expected stdout or -> <docker exec args...>: exit 0, empty
# stderr, and (unless -) exactly the expected stdout.
run_in() {
    local label=$1 expected=$2 rc=0
    shift 2
    docker exec "$@" >"$work/out" 2>"$work/err" || rc=$?
    if [ "$rc" -ne 0 ]; then
        fail "$label: exit $rc (stderr: $(cat "$work/err"))"
    elif [ -s "$work/err" ]; then
        fail "$label: exit 0 but stderr is not empty: $(cat "$work/err")"
    elif [ "$expected" != - ] && [ "$(cat "$work/out")" != "$expected" ]; then
        fail "$label: printed '$(cat "$work/out")', expected '$expected'"
    else
        pass "$label"
    fi
}

logged() { docker logs "$run_id-$1" 2>&1 | grep -cF -- "$2" || true; }

printf '== %s\n' "$image"

# 1. The model is live: with no entrypoint, nothing sets the variable.
# --init: a process that is PID 1 ignores a signal it sends itself.
rc=0
docker run --rm --init "${model[@]}" --entrypoint python "$image" -c "$PROBE" \
    >/dev/null 2>&1 || rc=$?
if [ "$rc" -eq 132 ]; then
    pass "the CPU model crashes the import (exit 132) when nothing sets OPENSSL_armcap"
else
    fail "the CPU model is not live: the import exited $rc, not 132"
fi

# 2. Affected CPU: every way of starting Python afterwards gets the setting.
if start affected "${model[@]}"; then
    if [ "$(logged affected "$SAVED")" -eq 1 ]; then
        pass "the entrypoint saved the decision ($SAVED)"
    else
        fail "no '$SAVED' line in the entrypoint log"
    fi
    run_in "exec python imports" - "$run_id-affected" python -c "$PROBE"
    run_in "exec /opt/venv/bin/python imports" - "$run_id-affected" /opt/venv/bin/python -c "$PROBE"
    run_in "exec sh -c 'python ...' imports" - "$run_id-affected" sh -c "python -c '$PROBE'"
    run_in "exec -u root python imports" - -u root "$run_id-affected" python -c "$PROBE"
    run_in "exec python sees OPENSSL_armcap=0" 0 "$run_id-affected" python -c "$PRINT"

    # A restart keeps /tmp, so the previous start's marker is there when the
    # entrypoint runs again. It must be removed BEFORE the probe, or the hook
    # makes the probe pass and nothing is saved.
    docker restart "$run_id-affected" >/dev/null
    for _ in $(seq 1 60); do
        [ "$(logged affected '[entrypoint] starting: sleep infinity')" -ge 2 ] && break
        sleep 1
    done
    if [ "$(logged affected "$SAVED")" -eq 2 ]; then
        pass "after a restart, the probe crashed again and the decision was saved again"
    else
        fail "after a restart, the entrypoint did not save the decision again"
    fi
    run_in "after a restart, exec python imports" - "$run_id-affected" python -c "$PROBE"
fi

# 3. A marker from an earlier start, on a CPU that does not crash: removed.
volumes+=("$run_id-stale")
docker volume create "$run_id-stale" >/dev/null
docker run --rm -v "$run_id-stale:/tmp" --entrypoint sh "$image" -c ": > $MARKER"
if start stale -v "$run_id-stale:/tmp" "${healthy[@]}"; then
    run_in "a stale marker is removed: exec python sees no OPENSSL_armcap" None \
        "$run_id-stale" python -c "$PRINT"
    run_in "a stale marker is removed: no file left" - "$run_id-stale" sh -c "test ! -e $MARKER"
fi

# 4. Unaffected CPU: nothing set, nothing saved, and the image sets nothing.
if start unaffected "${healthy[@]}"; then
    run_in "unaffected: exec python sees no OPENSSL_armcap" None \
        "$run_id-unaffected" python -c "$PRINT"
    run_in "unaffected: exec python imports" - "$run_id-unaffected" python -c "$PROBE"
    run_in "unaffected: no marker" - "$run_id-unaffected" sh -c "test ! -e $MARKER"
    if docker inspect -f '{{json .Config.Env}}' "$image" | grep -qF OPENSSL_armcap; then
        fail "the image's configured environment sets OPENSSL_armcap"
    else
        pass "the image's configured environment does not set OPENSSL_armcap"
    fi
    if [ "$(logged unaffected OPENSSL_armcap)" -eq 0 ]; then
        pass "unaffected: the entrypoint says nothing about OPENSSL_armcap"
    else
        fail "unaffected: the entrypoint logged an OPENSSL_armcap line"
    fi
fi

# 5. Read-only root and no writable /tmp, affected CPU: still starts, and says
# that a process started with exec does not get the setting.
if start readonly --read-only "${model[@]}"; then
    if [ "$(logged readonly 'Could not save')" -eq 1 ]; then
        pass "read-only, no /tmp: the entrypoint says the marker could not be saved"
    else
        fail "read-only, no /tmp: no 'Could not save' line"
    fi
    run_in "read-only, no /tmp: the container runs; exec python sees no OPENSSL_armcap" None \
        "$run_id-readonly" python -c "$PRINT"
fi

# 6. The Helm chart's shape: read-only root, a writable /tmp, uid 1001.
if start chart --read-only --tmpfs /tmp --user 1001:1001 "${model[@]}"; then
    run_in "chart shape: exec python imports" - "$run_id-chart" python -c "$PROBE"
    run_in "chart shape: exec python sees OPENSSL_armcap=0" 0 "$run_id-chart" python -c "$PRINT"
fi

if [ "$failures" -ne 0 ]; then
    printf '%s: %d check(s) failed\n' "$image" "$failures" >&2
    exit 1
fi
printf '%s: the OpenSSL fallback reaches every Python process\n' "$image"
