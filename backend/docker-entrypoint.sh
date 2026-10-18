#!/usr/bin/env bash
# =========================================================================
# Controlvar API container entrypoint.
#
#   0. python -m backend.app.core.preflight: the settings check. Every problem
#      it finds is printed in one list and the container exits 78 (EX_CONFIG)
#      before anything else runs. It always requires ENVIRONMENT (or the
#      legacy APP_ENV), and refuses `test`, which is the test runner's; in
#      staging/production it also requires the secrets,
#      PUBLIC_BASE_URL and POSTGRES_SERVER, and refuses DATABASE_URL/_URI.
#   1. Wait for PostgreSQL (POSTGRES_SERVER/POSTGRES_PORT) to accept connections.
#   2. RUN_MIGRATIONS=true (default)  ->  python -m backend.app.db.bootstrap
#      (creates the schema on a fresh database, `alembic upgrade heads` otherwise).
#   3. SEED=<comma list of demo,shoplab,streampulse,sdk-contract>  ->  run the
#      matching backend/scripts/seed_*.py once. Completed seeds are recorded in
#      the `seed_markers` table (backend/scripts/seed_markers.py) so a restart
#      of the container does not re-seed. SEED_FORCE=true re-runs them.
#      Every seed is development-only: SEED=<any of them> is refused,
#      before step 1, unless ENVIRONMENT is development or test.
#   4. exec "$@" — by default uvicorn; `--workers ${WEB_CONCURRENCY:-1}` and the
#      proxy flags are appended when the command is uvicorn and the caller did
#      not pass them explicitly.
#
# Steps 1-3 can be skipped (step 0 cannot): RUN_MIGRATIONS=false, SEED= (empty),
# WAIT_FOR_DB=false. The script runs with `set -euo pipefail`: a failed
# bootstrap or seed stops the container instead of starting a half-initialised
# API.
#
# Who runs step 2 depends on the deployment (#298):
#   * AWS (CDK): the API task definition sets RUN_MIGRATIONS=false and SEED=
#     (infrastructure/cdk/stacks/fargate_service_stack.py). The API never
#     writes the schema; the migration task the Deploy workflow runs is the
#     only writer. A bootstrap refuses a database a newer release migrated, so
#     an older API revision started after a newer migration (a replacement, a
#     scale-out, a rollback) would otherwise exit instead of serving.
#   * Docker Compose: one API container, RUN_MIGRATIONS=true -- the only writer.
#   * Helm chart: the bootstrap runs in an init container
#     (charts/experimently/templates/api-deployment.yaml), not in the serving
#     container.
# The default stays true so that a single container started by hand, or by
# compose, still creates its schema.
# =========================================================================
set -euo pipefail

log() { printf '[entrypoint] %s\n' "$*" >&2; }

# -------------------------------------------------------------------------
# Seeds are development-only
#
# SEED=demo, shoplab and streampulse load the sample data for the demo apps,
# and sdk-contract creates the demo accounts and API keys the SDK contract
# suites use. They run when ENVIRONMENT is development or test (the legacy
# spellings dev and demo mean development) and are refused everywhere else,
# before anything else happens.
# The seed scripts make the same check (backend/scripts/seed_guard.py).
# -------------------------------------------------------------------------
refuse_seeds_outside_development() {
    local seed_list="${SEED:-}"
    [ -z "$seed_list" ] && return 0
    local env_raw="${ENVIRONMENT:-${APP_ENV:-development}}"
    local env_name
    env_name="$(printf '%s' "$env_raw" | tr '[:upper:]' '[:lower:]' | tr -d '[:space:]')"
    case "$env_name" in
        development|dev|demo|test) return 0 ;;
    esac
    local names=() raw name
    IFS=',' read -r -a names <<< "$seed_list"
    for raw in ${names[@]+"${names[@]}"}; do
        name="$(printf '%s' "$raw" | tr -d '[:space:]')"
        case "$name" in
            demo|shoplab|streampulse|sdk-contract)
                log "SEED=${seed_list} is for development and is refused when ENVIRONMENT=${env_raw}. Remove SEED."
                exit 78
                ;;
        esac
    done
}
refuse_seeds_outside_development

cd /app

# -------------------------------------------------------------------------
# 0. Settings preflight -- first, and before the database wait, so that a
#    misconfigured deployment is told everything that is wrong in one go
#    instead of one refusal per restart (or a long wait on localhost).
#    Exits 78 (EX_CONFIG) with the list; `set -e` makes that the
#    container's exit status.
# -------------------------------------------------------------------------
python -m backend.app.core.preflight

: "${POSTGRES_SERVER:=${POSTGRES_HOST:-localhost}}"
: "${POSTGRES_PORT:=5432}"
: "${POSTGRES_USER:=postgres}"
: "${POSTGRES_DB:=experimentation}"
: "${WAIT_FOR_DB:=true}"
: "${DB_WAIT_TIMEOUT:=120}"
: "${RUN_MIGRATIONS:=true}"
: "${SEED:=}"
: "${SEED_FORCE:=false}"
: "${WEB_CONCURRENCY:=1}"
export POSTGRES_SERVER POSTGRES_PORT POSTGRES_USER POSTGRES_DB

# -------------------------------------------------------------------------
# 0. OpenSSL CPU-feature probe (arm64 under some hypervisors)
#
# The OpenSSL statically linked into the `cryptography` wheel selects assembly
# code paths from the CPU flags the kernel advertises. Some arm64 VMs (Docker
# Desktop on Apple M4, for one) advertise SVE/SME that the guest cannot
# execute, and the import dies with SIGILL (exit 132). Probe once; if it
# crashes, tell OpenSSL to skip the optimised paths.
#
# The export reaches only this script's own children. A process started later
# with `docker compose exec` (or `kubectl exec`) starts from the container's
# configured environment instead, so the decision is also saved as a marker
# file, and backend/docker/sitecustomize.py -- which every Python process of
# the image's venv runs at start-up -- sets OPENSSL_armcap=0 when it finds the
# marker (#1265). The marker never sets anything on hardware that does not
# crash: OPENSSL_armcap=0 costs a large factor in AES-GCM and SHA-256 speed.
#
#   * The marker is removed FIRST, before the probe, on every start: a marker
#     left by an earlier start of this container would make the probe pass
#     (the hook sets the variable inside it) and hide what this CPU does.
#   * Only exit 132 (SIGILL) means "this CPU". Any other failure is a broken
#     install, logged as such, and saves nothing.
#   * An OPENSSL_armcap the operator set is left alone: no probe, no marker.
#   * Neither the removal nor the write can stop the container: with a
#     read-only /tmp the processes this script starts still get the variable,
#     and the line below says the rest do not.
# -------------------------------------------------------------------------
openssl_marker="${TMPDIR:-/tmp}/controlvar-openssl-armcap0"
rm -f "$openssl_marker" 2>/dev/null \
    || log "could not remove ${openssl_marker}; a process started with exec may get OPENSSL_armcap=0 from an earlier start"
if [ -z "${OPENSSL_armcap:-}" ]; then
    openssl_rc=0
    python -c "from cryptography.hazmat.bindings._rust import openssl" >/dev/null 2>&1 || openssl_rc=$?
    if [ "$openssl_rc" -eq 132 ]; then
        export OPENSSL_armcap=0
        if { : > "$openssl_marker"; } 2>/dev/null; then
            log "cryptography's OpenSSL crashed on this CPU (SIGILL); setting OPENSSL_armcap=0 (asm optimisations off) for every Python process in this container"
        else
            log "cryptography's OpenSSL crashed on this CPU (SIGILL); setting OPENSSL_armcap=0 (asm optimisations off) for the processes this entrypoint starts. Could not save ${openssl_marker}, so a Python process started with exec does not get it: pass -e OPENSSL_armcap=0 to exec, or give the container a writable /tmp"
        fi
    elif [ "$openssl_rc" -ne 0 ]; then
        log "the cryptography import failed (exit ${openssl_rc}); not a CPU problem, OPENSSL_armcap left unset"
    fi
fi

# -------------------------------------------------------------------------
# 1. Wait for the database
# -------------------------------------------------------------------------
wait_for_db() {
    local deadline=$(( $(date +%s) + DB_WAIT_TIMEOUT ))
    log "waiting for PostgreSQL at ${POSTGRES_SERVER}:${POSTGRES_PORT} (timeout ${DB_WAIT_TIMEOUT}s)"
    while true; do
        if command -v pg_isready >/dev/null 2>&1; then
            if pg_isready -q -h "$POSTGRES_SERVER" -p "$POSTGRES_PORT" -U "$POSTGRES_USER" -d "$POSTGRES_DB"; then
                break
            fi
        else
            # Fallback when postgresql-client is not installed: a raw TCP probe.
            if python - "$POSTGRES_SERVER" "$POSTGRES_PORT" <<'PY'
import socket, sys
try:
    socket.create_connection((sys.argv[1], int(sys.argv[2])), timeout=2).close()
except OSError:
    sys.exit(1)
PY
            then
                break
            fi
        fi
        if [ "$(date +%s)" -ge "$deadline" ]; then
            log "PostgreSQL did not become ready within ${DB_WAIT_TIMEOUT}s"
            exit 1
        fi
        sleep 1
    done
    log "PostgreSQL is ready"
}

if [ "$WAIT_FOR_DB" = "true" ]; then
    wait_for_db
fi

# -------------------------------------------------------------------------
# 2. Schema bootstrap / migrations
# -------------------------------------------------------------------------
if [ "$RUN_MIGRATIONS" = "true" ]; then
    log "running database bootstrap (python -m backend.app.db.bootstrap)"
    python -m backend.app.db.bootstrap
else
    log "RUN_MIGRATIONS=${RUN_MIGRATIONS}: skipping bootstrap"
fi

# -------------------------------------------------------------------------
# 3. Seeds (once, tracked in the seed_markers table)
# -------------------------------------------------------------------------
seed_script_for() {
    case "$1" in
        demo)          echo "backend/scripts/seed_demo_data.py" ;;
        shoplab)       echo "backend/scripts/seed_shoplab.py" ;;
        streampulse)   echo "backend/scripts/seed_streampulse.py" ;;
        sdk-contract)  echo "backend/scripts/seed_sdk_contract.py" ;;
        *)             echo "" ;;
    esac
}

if [ -n "$SEED" ]; then
    IFS=',' read -r -a seeds <<< "$SEED"
    for raw in "${seeds[@]}"; do
        name="$(echo "$raw" | tr -d '[:space:]')"
        [ -z "$name" ] && continue
        script="$(seed_script_for "$name")"
        if [ -z "$script" ]; then
            log "unknown seed '${name}' (expected one of demo, shoplab, streampulse, sdk-contract)"
            exit 1
        fi
        if [ "$SEED_FORCE" != "true" ] && python -m backend.scripts.seed_markers check "$name"; then
            log "seed '${name}' already applied; skipping (SEED_FORCE=true to re-run)"
            continue
        fi
        log "applying seed '${name}' (${script})"
        python "$script"
        python -m backend.scripts.seed_markers mark "$name"
        log "seed '${name}' recorded"
    done
fi

# -------------------------------------------------------------------------
# 4. Hand over to the server
# -------------------------------------------------------------------------
if [ "$#" -eq 0 ]; then
    set -- uvicorn backend.app.main:app --host 0.0.0.0 --port "${PORT:-8000}"
fi

if [ "$1" = "uvicorn" ]; then
    has_workers=false
    for arg in "$@"; do
        case "$arg" in
            --workers|--workers=*) has_workers=true ;;
        esac
    done
    if [ "$has_workers" = "false" ]; then
        set -- "$@" --workers "$WEB_CONCURRENCY"
    fi
    # --forwarded-allow-ips decides WHOSE X-Forwarded-* uvicorn believes, and
    # `*` meant everybody's. Measured against the pinned uvicorn 0.54.0, with a
    # client that forged a hop and an ALB that appended the real one:
    #
    #   X-Forwarded-For: 9.9.9.9, 203.0.113.9, 10.0.3.10
    #                    ^forged  ^real client ^the ALB
    #
    #   --forwarded-allow-ips="*"      -> client = 9.9.9.9      (the forgery)
    #   --forwarded-allow-ips=<private> -> client = 203.0.113.9 (correct)
    #
    # With `*`, uvicorn takes `x_forwarded_for_hosts[0]` -- the LEFTMOST entry,
    # which is whatever the client sent, because each proxy APPENDS. Narrowed,
    # it walks the list in reverse and returns the first host it does not
    # trust. Same header, opposite answer.
    #
    # Two things rode on that: `scope["scheme"]` came from a client-settable
    # X-Forwarded-Proto, and the rate limiter keyed its buckets on the client
    # address -- so prepending a fresh random IP per request bought an
    # unlimited budget, including on the strict login limit (#237).
    #
    # The default is the PRIVATE ranges rather than a specific VPC CIDR, on
    # purpose. A load balancer is always inside the VPC and therefore always
    # RFC1918; a real client arriving from the internet never is. So uvicorn
    # trusts the hop in front, skips any private hops in the list, and returns
    # the first public address -- without this container having to be told its
    # own VPC's CIDR. Getting that value wrong is the failure mode this avoids:
    # a list that does not contain the load balancer makes uvicorn ignore the
    # proxy headers entirely, every client appears to BE the load balancer, and
    # the rate limiter silently collapses to a single shared bucket.
    #
    # FORWARDED_ALLOW_IPS overrides it for a topology this does not fit.
    set -- "$@" --proxy-headers \
        --forwarded-allow-ips="${FORWARDED_ALLOW_IPS:-127.0.0.1,::1,10.0.0.0/8,172.16.0.0/12,192.168.0.0/16}" \
        --no-server-header
fi

log "starting: $*"
exec "$@"
