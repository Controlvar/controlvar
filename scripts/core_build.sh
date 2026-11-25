#!/usr/bin/env bash
# =========================================================================
# scripts/core_build.sh — prove that one profile of Controlvar builds,
# boots and passes its tests on its own.
#
#   scripts/core_build.sh                 # core profile: delete modules/, rebuild
#   scripts/core_build.sh --profile full  # full profile: the whole tree
#   make core-build                       # the first form
#   make full-build                       # the second form
#
#   scripts/core_build.sh --list-steps [--profile full] [--steps ...]
#                                         # print the steps this invocation
#                                         # would run, in order, and exit 0
#                                         # without touching anything
#
#   scripts/core_build.sh --profile core --shard I/N --shard-reports DIR
#                                         # one of N shards, as the core-build
#                                         # CI check runs it (below)
#
# The script NEVER touches the checkout it is run from.  It copies the working
# tree (every file `git ls-files` knows about that is not ignored — so no venv,
# no node_modules, no .env, no build output) into a temporary directory and
# works there.  On success the copy is removed; on failure it is kept and its
# path printed so the state can be inspected (`--keep` keeps it regardless).
#
# Core sequence, one step at a time, stopping at the first failure with a line
# naming the step:
#
#   copy         copy the tree; nothing below runs against the checkout
#   strip        rm -rf modules/ (that is the whole of it: the manifest lists
#                nothing outside modules/, and
#                backend/tests/smoke/test_core_boundary.py
#                ::test_manifest_paths_are_under_modules is what keeps it so —
#                it runs in the `smoke` step below and in the smoke-tests job)
#   reuse        `reuse lint` over the stripped tree: every file the release
#                would ship carries a licence (REUSE.toml, LICENSES/).  Skipped
#                with a warning when the reuse tool is not installed (CI
#                installs it)
#   secrets      gitleaks over the stripped tree — what a core release would
#                ship — before any build output lands in it; then over
#                tests/sdk-contract with no .gitleaksignore at all, because
#                its vectors are generated and a line pin would hide the next
#                regeneration's finding
#   import       `import backend.app.main` succeeds, logs "Running the core
#                profile", resolves `backend` inside the copy, and `modules`
#                is not importable at all (not present, not merely broken)
#   bootstrap    python -m backend.app.db.bootstrap against a scratch schema
#                (core_build_<pid>, dropped on exit): the tables created are
#                exactly Base.metadata's core set and none of the tables
#                modules-manifest.txt lists exist
#   unit / integration / smoke
#                three separate pytest sessions, -p no:cov -rfEs (each failed
#                or erroring test, and each skip with its reason, is listed at
#                the end of the log); tests marked
#                @pytest.mark.modules are skipped by backend/tests/conftest.py
#   lambda       one pytest session per backend/lambda/<function>/ (they all
#                define `tests.conftest`, so they cannot share a session)
#   frontend     EXPERIMENTLY_PROFILE=core npm run build, then the bundle must
#                carry no module route and must carry the workspaces stub page
#                — the same check the frontend-tests CI job runs
#   vectors      the cross-SDK golden vectors, against the Python and JS SDKs'
#                own hash functions (the node/python part of `make test-sdk`)
#   licences     every Python package the API image ships (the closure of
#                backend/requirements/runtime.txt, modules/requirements.txt too
#                for --profile full when it exists) carries an allow-listed
#                licence; see the note at `step_licences` for why this is
#                `--allow-only` and not `--fail-on="GPL;AGPL;SSPL"`
#
# `--profile full` runs the same sequence on the full tree: nothing is
# deleted, `import` expects the modules registration to load, `bootstrap`
# expects the module tables to exist, `modules/backend/tests` joins the pytest
# sessions, and `frontend` expects the module routes to be present in the
# bundle.
#
# Shards (`--shard I/N --shard-reports DIR`, core profile only)
#   N runs, I = 1..N, between them run the core sequence once, with the unit
#   and integration sessions cut into N slices: a test belongs to slice I
#   when sha256(its node id) % N == I-1 (backend/tests/shard.py).  Shard 1
#   runs every step; shards 2..N run copy, strip, import, unit and
#   integration, so every shard checks that its tree cannot import
#   `modules`.  Only the unit and integration sessions are sliced; each
#   writes what it collected, selected and ran to DIR/shard-unit-I.json and
#   DIR/shard-integration-I.json, and scripts/check_test_shards.py over all N
#   shards' reports proves every collected test ran exactly once.  I/N is
#   `[1-9][0-9]{0,2}/[1-9][0-9]{0,2}` with I <= N.  DIR must be an absolute
#   path; it is created when absent, and one that already holds this shard's
#   reports is refused.  Usage errors (exit 2): --shard with --steps or
#   CORE_BUILD_STEPS, with --profile full, without --shard-reports (except
#   with --list-steps), or with either of backend/tests/shard.py's two
#   variables already set; --shard-reports without --shard.  `--dir` with an
#   existing copy reuses it, as it does for --steps.
#
# Environment
#   POSTGRES_SERVER/POSTGRES_HOST, POSTGRES_PORT, POSTGRES_USER,
#   POSTGRES_PASSWORD, POSTGRES_DB   the database to use (defaults:
#                                    localhost, 5432, postgres, postgres,
#                                    experimentation).  The pytest sessions
#                                    create and drop their own databases, as
#                                    backend/tests/conftest.py always does.
#   PYTHON                           interpreter (default: `python` after
#                                    activating ./venv when it exists)
#   CORE_BUILD_DIR                   where to put the copy (default: mktemp)
#   CORE_BUILD_STEPS                 comma list of steps to run (debugging;
#                                    same as --steps).  Whitespace around a
#                                    name is ignored ("unit, frontend" selects
#                                    both).  The copy/strip steps are always
#                                    run when the copy is missing.  An unknown
#                                    name, or a filter that names no step at
#                                    all, is a usage error (exit 2), not a
#                                    silent "nothing ran, OK".
#
# Exit status: 0 only when every step it was asked for ran to the end and
# passed; otherwise non-zero, after printing `core-build: FAILED at step
# <name>` -- the failing command's own status for a failing step, 128+n when
# the script itself is stopped by SIGINT, SIGTERM or SIGHUP (130, 143, 129);
# 2 for a usage error.  The summary lists every step of the sequence with its
# outcome: ok, skipped (not asked for), FAILED, or not run (asked for, never
# reached).  A shard's OK or FAILED line names the shard, and a failed shard
# prints the command that runs it again.
#
# A signal sent to the script alone is acted on once the command the step is
# running returns (bash runs a trap only between commands); one sent to the
# whole process group, as Ctrl-C does, stops that command too.
# =========================================================================
set -euo pipefail

usage() {
    # The header comment above, minus the rule lines.
    awk 'NR > 2 { if ($0 ~ /^# =+$/) exit; sub(/^# ?/, ""); print }' "${BASH_SOURCE[0]}"
}

PROFILE="core"
KEEP=0
STEPS="${CORE_BUILD_STEPS:-}"
# Whether a filter was asked for at all, so that one naming nothing is a usage
# error rather than a silent "no filter, run everything".
STEPS_GIVEN=0
[ -n "$STEPS" ] && STEPS_GIVEN=1
LIST_STEPS=0
BUILD_DIR="${CORE_BUILD_DIR:-}"
# --shard I/N and --shard-reports DIR.  SHARD stays empty without --shard:
# then nothing below differs from an unsharded run.
SHARD=""
SHARD_GIVEN=0
SHARD_INDEX=""
SHARD_REPORTS=""
SHARD_REPORTS_GIVEN=0

usage_error() {
    printf 'core-build: %s\n' "$*" >&2
    exit 2
}

# Every step name `run_step` knows, in order.  A --steps filter is matched
# against this list: a name that is not here selects nothing, and an
# unvalidated filter would therefore skip the whole sequence and still exit 0
# ("core-build: OK") -- a typo that silently proves nothing.
ALL_STEPS="copy strip reuse secrets import bootstrap unit integration smoke modules_tests lambda frontend vectors licences"

while [ $# -gt 0 ]; do
    case "$1" in
        --profile)
            PROFILE="${2:-}"; shift 2 ;;
        --profile=*)
            PROFILE="${1#--profile=}"; shift ;;
        --keep)
            KEEP=1; shift ;;
        --steps)
            STEPS="${2:-}"; STEPS_GIVEN=1; shift 2 ;;
        --steps=*)
            STEPS="${1#--steps=}"; STEPS_GIVEN=1; shift ;;
        --list-steps)
            LIST_STEPS=1; shift ;;
        --dir)
            BUILD_DIR="${2:-}"; shift 2 ;;
        --dir=*)
            BUILD_DIR="${1#--dir=}"; shift ;;
        --shard)
            [ $# -ge 2 ] || usage_error "--shard needs a value, i/n (for example 2/4)"
            SHARD="$2"; SHARD_GIVEN=1; shift 2 ;;
        --shard=*)
            SHARD="${1#--shard=}"; SHARD_GIVEN=1; shift ;;
        --shard-reports)
            [ $# -ge 2 ] || usage_error "--shard-reports needs a directory"
            SHARD_REPORTS="$2"; SHARD_REPORTS_GIVEN=1; shift 2 ;;
        --shard-reports=*)
            SHARD_REPORTS="${1#--shard-reports=}"; SHARD_REPORTS_GIVEN=1; shift ;;
        -h|--help)
            usage; exit 0 ;;
        *)
            echo "core-build: unknown argument '$1' (see --help)" >&2; exit 2 ;;
    esac
done

case "$PROFILE" in
    core|full) ;;
    *) echo "core-build: --profile must be core or full, not '$PROFILE'" >&2; exit 2 ;;
esac

# --shard: every refusal here is a usage error, before preflight.  The two
# variables would reach every session, and the second one to start would find
# the first one's report and stop (exit 4); --shard sets them on the unit and
# integration sessions alone.  A shard runs its own steps, so a --steps filter
# beside it could only drop part of the sequence without saying so.
if [ "$SHARD_GIVEN" = 1 ]; then
    _shard_spec='^([1-9][0-9]{0,2})/([1-9][0-9]{0,2})$'
    if ! [[ "$SHARD" =~ $_shard_spec ]]; then
        usage_error "--shard must be i/n with 1 <= i <= n (for example 2/4), not '$SHARD'"
    fi
    SHARD_INDEX="${BASH_REMATCH[1]}"
    [ "$SHARD_INDEX" -le "${BASH_REMATCH[2]}" ] \
        || usage_error "--shard $SHARD: there is no shard $SHARD_INDEX of ${BASH_REMATCH[2]}"
    unset _shard_spec
    [ "$PROFILE" = core ] || usage_error "--shard runs only with --profile core"
    [ "$STEPS_GIVEN" = 0 ] \
        || usage_error "--shard runs its own steps; it cannot be combined with --steps or CORE_BUILD_STEPS"
    for _var in EXPERIMENTLY_TEST_SHARD EXPERIMENTLY_TEST_SHARD_REPORT; do
        [ -z "${!_var:-}" ] \
            || usage_error "$_var is set; --shard sets it for the unit and integration sessions only, so unset it"
    done
    unset _var
    if [ "$LIST_STEPS" = 0 ] && [ "$SHARD_REPORTS_GIVEN" = 0 ]; then
        usage_error "--shard needs --shard-reports DIR, where the unit and integration sessions write their reports"
    fi
    if [ "$SHARD_REPORTS_GIVEN" = 1 ]; then
        case "$SHARD_REPORTS" in
            /*) ;;
            *) usage_error "--shard-reports must be an absolute path, not '$SHARD_REPORTS'" ;;
        esac
        # A second run into the same directory would stop at the first
        # session, whose report already exists (backend/tests/shard.py).
        for _report in "shard-unit-$SHARD_INDEX.json" "shard-integration-$SHARD_INDEX.json"; do
            [ ! -e "$SHARD_REPORTS/$_report" ] \
                || usage_error "$SHARD_REPORTS already holds $_report; give a fresh directory"
        done
        unset _report
    fi
    # Shard 1 runs the whole sequence; the others run the unit and
    # integration slices and the steps that make and check their tree.
    [ "$SHARD_INDEX" = 1 ] || STEPS="copy,strip,import,unit,integration"
elif [ "$SHARD_REPORTS_GIVEN" = 1 ]; then
    usage_error "--shard-reports needs --shard"
fi

# Validate the filter AND normalise it in place.  `want_step` matches
# ",$STEPS," against ",<name>,", so `--steps "unit, frontend"` used to
# validate (the validator stripped whitespace into a throwaway local), run
# `unit`, and skip ` frontend` as if it had never been asked for -- exiting 0
# with "core-build: OK" having proved half of what was requested.  STEPS is
# therefore rebuilt from the validated tokens, and a filter that names nothing
# is a usage error rather than a silent "run everything".
if [ "$STEPS_GIVEN" = 1 ]; then
    IFS=',' read -r -a _requested <<< "$STEPS"
    _normalised=""
    for _step in "${_requested[@]}"; do
        _step="${_step//[[:space:]]/}"
        [ -z "$_step" ] && continue
        case " $ALL_STEPS " in
            *" $_step "*) ;;
            *)
                echo "core-build: unknown step '$_step'" >&2
                echo "core-build: known steps: ${ALL_STEPS// /, }" >&2
                exit 2 ;;
        esac
        if [ "$_step" = modules_tests ] && [ "$PROFILE" != full ]; then
            echo "core-build: step 'modules_tests' only runs with --profile full" >&2
            exit 2
        fi
        case ",$_normalised," in
            *",$_step,"*) continue ;;
        esac
        _normalised="${_normalised:+$_normalised,}$_step"
    done
    if [ -z "$_normalised" ]; then
        echo "core-build: --steps '$STEPS' names no step" >&2
        echo "core-build: known steps: ${ALL_STEPS// /, }" >&2
        exit 2
    fi
    STEPS="$_normalised"
    unset _requested _step _normalised
fi

# -------------------------------------------------------------------------
# Environment
# -------------------------------------------------------------------------
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

if [ -z "${VIRTUAL_ENV:-}" ] && [ -f "$REPO_ROOT/venv/bin/activate" ]; then
    # shellcheck source=/dev/null
    source "$REPO_ROOT/venv/bin/activate"
fi
PYTHON="${PYTHON:-python}"

# Same process environment every backend test job uses (CONTRIBUTING.md, "Tests").
export APP_ENV=test
export TESTING=true
export EXPERIMENTLY_PROFILE="$PROFILE"
export POSTGRES_SERVER="${POSTGRES_SERVER:-${POSTGRES_HOST:-localhost}}"
export POSTGRES_HOST="$POSTGRES_SERVER"
export POSTGRES_PORT="${POSTGRES_PORT:-5432}"
export POSTGRES_USER="${POSTGRES_USER:-postgres}"
export POSTGRES_PASSWORD="${POSTGRES_PASSWORD:-postgres}"
export POSTGRES_DB="${POSTGRES_DB:-experimentation}"
export NEXT_TELEMETRY_DISABLED=1
export PYTHONDONTWRITEBYTECODE=1

SCRATCH_SCHEMA="core_build_$$"
SCRATCH_SCHEMA_CREATED=0
COPY=""
STARTED_AT=$SECONDS
# What is running right now: "setup" (arguments, preflight) until the first
# step starts, then each step from its start until it returns.  A run that
# ends while STEP_RUNNING is 1 ended inside CURRENT_STEP, whatever $? says.
CURRENT_STEP="setup"
STEP_STARTED=$SECONDS
STEP_RUNNING=1
# The signal that stopped the script, if one did (on_signal sets it).
SIGNALLED=""
# The sequence this invocation runs (set below, once the steps are checked).
SEQUENCE=""

# -------------------------------------------------------------------------
# Reporting
# -------------------------------------------------------------------------
log()  { printf '[core-build] %s\n' "$*" >&2; }
warn() { printf '[core-build] WARNING: %s\n' "$*" >&2; }
die()  { printf '[core-build] ERROR: %s\n' "$*" >&2; exit 1; }

banner() {
    printf '\n[core-build] ==== %s ====\n' "$*" >&2
}

drop_scratch_schema() {
    [ "$SCRATCH_SCHEMA_CREATED" = 1 ] || return 0
    SCRATCH_SCHEMA_CREATED=0
    SCHEMA="$SCRATCH_SCHEMA" "$PYTHON" - <<'PY' || warn "could not drop scratch schema $SCRATCH_SCHEMA"
import os
import psycopg2

conn = psycopg2.connect(
    host=os.environ["POSTGRES_SERVER"],
    port=os.environ["POSTGRES_PORT"],
    user=os.environ["POSTGRES_USER"],
    password=os.environ["POSTGRES_PASSWORD"],
    dbname=os.environ["POSTGRES_DB"],
)
conn.autocommit = True
with conn, conn.cursor() as cur:
    cur.execute('DROP SCHEMA IF EXISTS "%s" CASCADE' % os.environ["SCHEMA"].replace('"', ""))
conn.close()
print(f"[core-build] dropped scratch schema {os.environ['SCHEMA']}")
PY
}

on_exit() {
    local status=$?
    trap - EXIT
    drop_scratch_schema
    local total=$((SECONDS - STARTED_AT))
    local step var outcome failed_at="" not_run=""
    [ "$STEP_RUNNING" = 1 ] && failed_at="$CURRENT_STEP"
    # The summary is the whole sequence, not just the steps that got as far
    # as reporting: a step that was asked for and never finished is a line
    # of its own, so it cannot drop out of the summary unnoticed.
    printf '\n[core-build] summary (--profile %s):\n' "$PROFILE" >&2
    if [ "$failed_at" = setup ]; then
        printf '  %-14s FAILED  (%ss)\n' setup "$((SECONDS - STEP_STARTED))" >&2
    fi
    for step in $SEQUENCE; do
        var="STEP_RESULT_$step"
        if [ -n "${!var:-}" ]; then
            outcome="${!var}"
        elif ! want_step "$step"; then
            outcome="skipped"
        elif [ "$failed_at" = "$step" ]; then
            outcome="FAILED  ($((SECONDS - STEP_STARTED))s)"
            [ -n "$SIGNALLED" ] && outcome="$outcome, stopped by SIG$SIGNALLED"
        else
            outcome="not run"
            not_run="${not_run:+$not_run, }$step"
            # Ended between two steps: the first one it never reached is
            # where the run stopped.
            [ -z "$failed_at" ] && failed_at="$step"
        fi
        printf '  %-14s %s\n' "$step" "$outcome" >&2
    done
    # A signal the script has no trap for (SIGUSR1, SIGALRM, ...) still runs
    # this function, with $? the status of the last command that finished --
    # 0, as often as not.  A run that did not finish every step it was asked
    # for is not a success, whatever $? says.
    if [ "$status" -eq 0 ] && [ -n "$failed_at" ]; then
        status=1
    fi
    local shard_note=""
    [ -n "$SHARD" ] && shard_note=" (shard $SHARD)"
    if [ "$status" -ne 0 ]; then
        if [ -n "$failed_at" ]; then
            printf '\ncore-build: FAILED at step %s%s (exit %s) after %ss\n' "$failed_at" "$shard_note" "$status" "$total" >&2
        else
            printf '\ncore-build: FAILED after its last step%s (exit %s) after %ss\n' "$shard_note" "$status" "$total" >&2
        fi
        if [ -n "$SHARD" ]; then
            # The same shard again, locally: a fresh report directory each
            # time, because a report that exists stops the session.
            # shellcheck disable=SC2016  # printed for the reader to run
            printf 'core-build: to run this shard again: scripts/core_build.sh --profile core --shard %s --shard-reports "$(mktemp -d)"\n' "$SHARD" >&2
        fi
        if [ -n "$SIGNALLED" ]; then
            printf 'core-build: stopped by SIG%s\n' "$SIGNALLED" >&2
        fi
        if [ -n "$not_run" ]; then
            printf 'core-build: asked for but not run: %s\n' "$not_run" >&2
        fi
        if [ -n "$COPY" ] && [ -d "$COPY" ]; then
            printf 'core-build: the working copy is kept for inspection: %s\n' "$COPY" >&2
        fi
        exit "$status"
    fi
    printf '\ncore-build: OK (--profile %s%s) in %ss\n' "$PROFILE" "${SHARD:+, shard $SHARD}" "$total" >&2
    if [ -n "$COPY" ] && [ -d "$COPY" ]; then
        if [ "$KEEP" = 1 ]; then
            printf 'core-build: working copy kept: %s\n' "$COPY" >&2
        else
            rm -rf "$COPY"
        fi
    fi
}
trap on_exit EXIT

# Without these, SIGTERM or SIGHUP ends bash after running on_exit with $? set
# to the last command that FINISHED (0, usually), so the summary printed
# "core-build: OK" with the interrupted step simply missing from it, and the
# copy was deleted under a step that was still running in it.  SIGINT sent to
# the script alone was ignored outright whenever the step's command did not
# die of it.  A trapped signal is recorded and ends the run with 128+n.
on_signal() {
    [ -z "$SIGNALLED" ] && SIGNALLED="$1"
    exit "$2"
}
trap 'on_signal HUP 129' HUP
trap 'on_signal INT 130' INT
trap 'on_signal TERM 143' TERM

want_step() {
    # No filter: run everything.  With a filter, run only the named steps
    # (copy and strip are forced when the copy does not exist yet).
    [ -z "$STEPS" ] && return 0
    case ",$STEPS," in
        *",$1,"*) return 0 ;;
    esac
    return 1
}

run_step() {
    local name="$1"
    if ! want_step "$name"; then
        if [ -n "$SHARD" ]; then
            log "skipping step $name (not in shard $SHARD)"
        else
            log "skipping step $name (--steps $STEPS)"
        fi
        return 0
    fi
    CURRENT_STEP="$name"
    STEP_STARTED=$SECONDS
    STEP_RUNNING=1
    banner "$name"
    "step_$name"
    STEP_RUNNING=0
    local elapsed=$((SECONDS - STEP_STARTED))
    # on_exit reads this back by name: a step has a result only once it
    # has returned.
    printf -v "STEP_RESULT_$name" 'ok      (%ss)' "$elapsed"
    log "step $name ok in ${elapsed}s"
}

# -------------------------------------------------------------------------
# Preflight: fail fast on a missing tool, before the long steps run
# -------------------------------------------------------------------------
preflight() {
    local missing=()
    command -v "$PYTHON" >/dev/null || missing+=("python ($PYTHON)")
    command -v git >/dev/null || missing+=("git")
    command -v node >/dev/null || missing+=("node")
    command -v npm >/dev/null || missing+=("npm")
    command -v gitleaks >/dev/null || missing+=("gitleaks (brew install gitleaks / github.com/gitleaks/gitleaks/releases)")
    if [ "${#missing[@]}" -gt 0 ]; then
        die "missing tools: ${missing[*]}"
    fi
    "$PYTHON" -c "import pytest, psycopg2, sqlalchemy" 2>/dev/null \
        || die "$PYTHON cannot import pytest/psycopg2/sqlalchemy — activate the venv (source venv/bin/activate) or pip install -r backend/requirements.txt"
    "$PYTHON" -c "import piplicenses" 2>/dev/null \
        || die "$PYTHON cannot import piplicenses — pip install pip-licenses"
    "$PYTHON" - <<'PY' || die "PostgreSQL is not reachable at $POSTGRES_SERVER:$POSTGRES_PORT/$POSTGRES_DB as $POSTGRES_USER (make db)"
import os, psycopg2
psycopg2.connect(
    host=os.environ["POSTGRES_SERVER"], port=os.environ["POSTGRES_PORT"],
    user=os.environ["POSTGRES_USER"], password=os.environ["POSTGRES_PASSWORD"],
    dbname=os.environ["POSTGRES_DB"], connect_timeout=5,
).close()
PY
    log "python: $("$PYTHON" --version 2>&1) at $(command -v "$PYTHON")"
    log "node: $(node --version), npm: $(npm --version), gitleaks: $(gitleaks version 2>/dev/null | tail -1), reuse: $(command -v reuse >/dev/null && reuse --version | head -1 | awk '{print $3}' || echo 'not installed')"
    log "postgres: $POSTGRES_USER@$POSTGRES_SERVER:$POSTGRES_PORT/$POSTGRES_DB"
}

# -------------------------------------------------------------------------
# copy — the working tree, as git sees it, into a fresh directory
# -------------------------------------------------------------------------
step_copy() {
    if [ -z "$BUILD_DIR" ]; then
        BUILD_DIR="$(mktemp -d "${TMPDIR:-/tmp}/controlvar-${PROFILE}-build.XXXXXX")"
    else
        mkdir -p "$BUILD_DIR"
    fi
    COPY="$(cd "$BUILD_DIR" && pwd)"
    case "$COPY" in
        "$REPO_ROOT"|"$REPO_ROOT"/*)
            die "the copy must live outside the checkout (CORE_BUILD_DIR=$COPY is under $REPO_ROOT)" ;;
    esac
    if [ -n "$(ls -A "$COPY")" ]; then
        die "$COPY is not empty; refusing to build into it"
    fi
    log "copying the working tree to $COPY"
    # Tracked files plus untracked-but-not-ignored ones: the tree as a release
    # tarball would see it.  Ignored files (venv, node_modules, .env*, build
    # output, caches) never come along.  A file deleted in the working tree
    # but still in the index is skipped rather than failing the copy.
    local listing
    listing="$(mktemp "${TMPDIR:-/tmp}/controlvar-files.XXXXXX")"
    git -C "$REPO_ROOT" ls-files -z --cached --others --exclude-standard --deduplicate > "$listing"
    SRC="$REPO_ROOT" DST="$COPY" LISTING="$listing" "$PYTHON" - <<'PY'
import os
import shutil

src = os.environ["SRC"]
dst = os.environ["DST"]
with open(os.environ["LISTING"], "rb") as handle:
    names = handle.read().split(b"\0")
copied = skipped = 0
for raw in names:
    if not raw:
        continue
    rel = os.fsdecode(raw)
    source = os.path.join(src, rel)
    if not os.path.lexists(source):
        skipped += 1
        continue
    target = os.path.join(dst, rel)
    os.makedirs(os.path.dirname(target), exist_ok=True)
    if os.path.islink(source):
        os.symlink(os.readlink(source), target)
    else:
        shutil.copy2(source, target)
    copied += 1
print(f"[core-build] copied {copied} files" + (f" ({skipped} listed but absent)" if skipped else ""))
PY
    rm -f "$listing"
    [ -f "$COPY/modules-manifest.txt" ] || die "the copy has no modules-manifest.txt"
    [ -f "$COPY/backend/app/main.py" ] || die "the copy has no backend/app/main.py"
}

# -------------------------------------------------------------------------
# strip — delete the modules (core only)
# -------------------------------------------------------------------------
step_strip() {
    if [ "$PROFILE" = full ]; then
        log "--profile full: nothing is deleted"
        [ -d "$COPY/modules" ] || die "--profile full but the tree has no modules/ directory"
        return 0
    fi
    if [ -d "$COPY/modules" ]; then
        log "rm -rf modules/ ($(find "$COPY/modules" -type f | wc -l | tr -d ' ') files)"
        : PLANTED, modules stay: rm -rf "$COPY/modules"
    else
        warn "the tree has no modules/ directory"
    fi
}

# -------------------------------------------------------------------------
# reuse — every file the tree ships carries a licence
# -------------------------------------------------------------------------
step_reuse() {
    if ! command -v reuse >/dev/null; then
        warn "the reuse tool is not installed (pip install reuse); REUSE compliance of the $PROFILE tree not checked"
        return 0
    fi
    # The copy has no .git, so reuse walks the directory; this runs before any
    # build output (node_modules, .next, out) exists in it.
    (cd "$COPY" && reuse lint) || die "the $PROFILE tree is not REUSE-compliant (see above)"
}

# -------------------------------------------------------------------------
# secrets — gitleaks over the tree a release would ship
# -------------------------------------------------------------------------
step_secrets() {
    local report
    report="$COPY/../$(basename "$COPY").gitleaks.json"
    # --no-git: the copy has no .git; every file is scanned as-is, which is the
    # tarball view.  The repository's .gitleaks.toml carries the allowlist.
    # Scanned from inside the copy with --source . so a finding's fingerprint
    # is the repository-relative path:rule:line, which a .gitleaksignore line
    # can name; with --source "$COPY" it carries the copy's random mktemp path.
    (cd "$COPY" && gitleaks detect --no-git --source . --config .gitleaks.toml \
        --redact --exit-code 1 --report-format json --report-path "$report") \
        || die "gitleaks found secrets in the $PROFILE tree (report: $report)"
    rm -f "$report"

    # tests/sdk-contract again, raw: no .gitleaksignore applies (#1497).  Its
    # ruleset vectors are generated, so a path:rule:line pin for them masks
    # whatever the next regeneration puts on that line; a false positive there
    # is fixed in backend/scripts/generate_ruleset_vectors.py instead.
    # gitleaks loads a .gitleaksignore from the -i path (the cwd by default)
    # and, whatever -i says, from the scanned directory.  So the scan runs from
    # an empty directory that is also the -i path, and the scanned directory
    # may not carry one.  --exit-code 3 keeps "found something" apart from
    # gitleaks itself failing, which exits 1.  A --source that does not exist
    # is only a warning and exits 0, hence the -f first.
    local vectors="$COPY/tests/sdk-contract" noignore raw_report raw_status=0 raw_findings
    [ -f "$vectors/ruleset-vectors.json" ] \
        || die "the $PROFILE tree has no tests/sdk-contract/ruleset-vectors.json to scan"
    [ ! -e "$vectors/.gitleaksignore" ] \
        || die "tests/sdk-contract/.gitleaksignore exists: nothing may exempt the SDK contract vectors from the raw gitleaks scan"
    noignore="$(mktemp -d "${TMPDIR:-/tmp}/controlvar-noignore.XXXXXX")"
    raw_report="$COPY/../$(basename "$COPY").sdk-contract.gitleaks.json"
    (cd "$noignore" && gitleaks detect --no-git --source "$vectors" \
        --config "$COPY/.gitleaks.toml" --gitleaks-ignore-path "$noignore" \
        --redact --exit-code 3 --report-format json --report-path "$raw_report") \
        || raw_status=$?
    rm -rf "$noignore"
    case "$raw_status" in
        0|3) ;;
        *) die "gitleaks could not scan tests/sdk-contract (exit $raw_status)" ;;
    esac
    raw_findings="$(REPORT="$raw_report" PREFIX="$vectors/" "$PYTHON" - <<'PY'
import json
import os

with open(os.environ["REPORT"], encoding="utf-8") as handle:
    findings = json.load(handle)
if not isinstance(findings, list):
    raise SystemExit(f"not a list of findings: {type(findings).__name__}")
prefix = os.environ["PREFIX"]
where = [f"{f['File'].removeprefix(prefix)}:{f['StartLine']} {f['RuleID']}" for f in findings]
print(f"{len(findings)} raw gitleaks findings: " + ", ".join(where))
PY
    )" || die "could not read the raw gitleaks report of tests/sdk-contract ($raw_report)"
    if [ "$raw_status" != 0 ] || [ "${raw_findings%% *}" != 0 ]; then
        die "tests/sdk-contract has $raw_findings (gitleaks exit $raw_status; report: $raw_report). The vectors are generated: rename the key in backend/scripts/generate_ruleset_vectors.py, never pin the line in .gitleaksignore"
    fi
    rm -f "$raw_report"
}

# -------------------------------------------------------------------------
# import — the API imports, in the right profile, from the copy
# -------------------------------------------------------------------------
step_import() {
    (cd "$COPY" && COPY="$COPY" PROFILE="$PROFILE" "$PYTHON" - <<'PY')
import importlib.util
import logging
import os
import sys

copy = os.path.realpath(os.environ["COPY"])
profile = os.environ["PROFILE"]

# The loader says "Running the core profile" (or "Modules loaded ...") while
# backend.app.api.api is being imported -- before main.py configures logging.
# Listen on that one logger so the line is captured whatever the app does with
# the root logger afterwards.
lines = []


class _Capture(logging.Handler):
    def emit(self, record):
        lines.append(record.getMessage())


loader_log = logging.getLogger("backend.app.modules_loader")
loader_log.setLevel(logging.DEBUG)
loader_log.addHandler(_Capture())

import backend  # noqa: E402

backend_dir = os.path.realpath(os.path.dirname(backend.__file__))
assert backend_dir == os.path.join(copy, "backend"), (
    f"`backend` resolved to {backend_dir}, not the copy {copy}/backend -- the "
    "checkout leaked into the build (an editable install on sys.path?)"
)

import backend.app.main  # noqa: E402,F401
from backend.app.modules_loader import load_modules, modules_failure  # noqa: E402

loaded = load_modules()
failure = modules_failure()
for line in lines:
    print(f"[core-build] modules_loader: {line}")

if profile == "core":
    assert not loaded and failure is None, (
        f"expected the core profile; loaded={loaded} failure={failure!r}"
    )
    assert any("Running the core profile" in line for line in lines), (
        f"modules_loader never logged 'Running the core profile': {lines!r}"
    )
    assert importlib.util.find_spec("modules") is None, (
        "`modules` is still importable from the core copy: "
        f"{importlib.util.find_spec('modules')}"
    )
    print("[core-build] import backend.app.main: core profile, modules not importable")
else:
    assert loaded and failure is None, (
        f"expected the modules to load; loaded={loaded} failure={failure!r}"
    )
    assert any("Modules loaded" in line for line in lines), (
        f"modules_loader never logged 'Modules loaded': {lines!r}"
    )
    from backend.app.core import hooks

    installed = sorted(hooks.installed_modules())
    assert installed, "the modules loaded but hooks.installed_modules() is empty"
    print(f"[core-build] import backend.app.main: full profile, modules {installed}")
PY
}

# -------------------------------------------------------------------------
# bootstrap — the schema a fresh deployment gets
# -------------------------------------------------------------------------
step_bootstrap() {
    SCRATCH_SCHEMA_CREATED=1
    log "python -m backend.app.db.bootstrap into scratch schema $SCRATCH_SCHEMA"
    (cd "$COPY" && POSTGRES_SCHEMA="$SCRATCH_SCHEMA" "$PYTHON" -m backend.app.db.bootstrap)
    (cd "$COPY" && SCHEMA="$SCRATCH_SCHEMA" PROFILE="$PROFILE" "$PYTHON" - <<'PY')
import os

import psycopg2

from backend.app.models import register_core_models
from backend.app.modules_loader import require_modules_or_absent
from backend.tests.smoke.modules_manifest import load

schema = os.environ["SCHEMA"]
profile = os.environ["PROFILE"]

# What Base.metadata says this profile's schema is: the core models, plus
# whatever the modules registration added (nothing, in a core tree).
Base = register_core_models()
loaded = require_modules_or_absent()
expected = {table.name for table in Base.metadata.tables.values()} | {"alembic_version"}

conn = psycopg2.connect(
    host=os.environ["POSTGRES_SERVER"], port=os.environ["POSTGRES_PORT"],
    user=os.environ["POSTGRES_USER"], password=os.environ["POSTGRES_PASSWORD"],
    dbname=os.environ["POSTGRES_DB"],
)
with conn, conn.cursor() as cur:
    cur.execute(
        "SELECT table_name FROM information_schema.tables WHERE table_schema = %s",
        (schema,),
    )
    actual = {row[0] for row in cur.fetchall()}
conn.close()

module_tables = set(load().tables)
print(f"[core-build] bootstrap created {len(actual)} tables in {schema} "
      f"(modules loaded: {loaded})")

missing = sorted(expected - actual)
extra = sorted(actual - expected)
assert not missing and not extra, (
    f"bootstrap did not create exactly Base.metadata's tables: missing={missing} extra={extra}"
)

if profile == "core":
    present = sorted(module_tables & actual)
    assert not present, f"core bootstrap created module tables: {present}"
    print(f"[core-build] none of the {len(module_tables)} module tables exist; "
          f"{len(actual) - 1} core tables + alembic_version")
else:
    absent = sorted(module_tables - actual)
    assert not absent, f"full bootstrap is missing module tables: {absent}"
    print(f"[core-build] all {len(module_tables)} module tables exist "
          f"({len(actual) - 1} tables + alembic_version)")
PY
    drop_scratch_schema
}

# -------------------------------------------------------------------------
# pytest sessions
# -------------------------------------------------------------------------
pytest_session() {
    # Each suite is its own session, from the copy's root, with the copy's
    # pyproject.toml as the configuration and its conftest as the harness.
    # -rfEs lists every failed and erroring test, and every skipped test with
    # its reason, at the end of the log (-rs alone replaced pytest's default
    # -rfE, so a failing test was named only in the FAILURES section, far
    # above): a skip counts as a pass in the exit status, so the log is where
    # a test that did not run shows.
    # --durations=30 prints the 30 slowest tests of the session at the end of
    # the log; it only reports, it selects nothing.
    (cd "$COPY" && "$PYTHON" -m pytest "$@" -p no:cov -q --tb=short -rfEs --durations=30)
}

# The unit or integration session, cut to this shard's slice under --shard.
# The two variables are exported in a subshell around this one call, so they
# reach that pytest and no other session: smoke, lambda and vectors run whole,
# and a report path is used once.  Without --shard it is pytest_session.
shard_session() {
    local name="$1" suite="$2"
    if [ -z "$SHARD" ]; then
        pytest_session "$suite"
        return
    fi
    (
        export EXPERIMENTLY_TEST_SHARD="$SHARD"
        export EXPERIMENTLY_TEST_SHARD_REPORT="$SHARD_REPORTS/shard-$name-$SHARD_INDEX.json"
        pytest_session "$suite"
    )
}

step_unit() {
    shard_session unit backend/tests/unit
}

step_integration() {
    shard_session integration backend/tests/integration
}

step_smoke() {
    pytest_session backend/tests/smoke
}

step_modules_tests() {
    # modules/backend/tests carries its own conftest; only meaningful with the
    # modules present.
    if [ -d "$COPY/modules/backend/tests" ]; then
        pytest_session modules/backend/tests
    else
        log "no modules/backend/tests directory; nothing to run"
    fi
}

step_lambda() {
    local dir rel ran=0 roots="backend/lambda"
    # The module Lambda packages (modules/lambda/*) carry their tests under
    # modules/backend/tests today; any tests/ directory they grow is run the
    # same way.
    [ "$PROFILE" = full ] && roots="backend/lambda modules/lambda"
    for root in $roots; do
        for dir in "$COPY/$root"/*/; do
            [ -d "$dir/tests" ] || continue
            rel="$root/$(basename "${dir%/}")"
            log "pytest $rel"
            pytest_session "$rel"
            ran=$((ran + 1))
        done
    done
    [ "$ran" -gt 0 ] || die "no backend/lambda/<function>/tests directories found"
    log "$ran Lambda packages tested, one session each"
}

# -------------------------------------------------------------------------
# frontend — the dashboard the profile ships
# -------------------------------------------------------------------------
provision_node_modules() {
    local target="$COPY/frontend/node_modules"
    [ -d "$target" ] && return 0
    if [ -d "$REPO_ROOT/frontend/node_modules" ]; then
        # Reuse the checkout's install: hard links where the filesystem allows,
        # copies otherwise.  Build tools only read node_modules, so a link
        # cannot write back into the checkout.
        log "linking frontend/node_modules from the checkout"
        SRC="$REPO_ROOT/frontend/node_modules" DST="$target" "$PYTHON" - <<'PY'
import os
import shutil

src = os.environ["SRC"]
dst = os.environ["DST"]
linked = copied = 0
for root, dirs, files in os.walk(src):
    rel = os.path.relpath(root, src)
    out = dst if rel == "." else os.path.join(dst, rel)
    os.makedirs(out, exist_ok=True)
    for name in list(dirs):
        path = os.path.join(root, name)
        if os.path.islink(path):
            os.symlink(os.readlink(path), os.path.join(out, name))
            dirs.remove(name)
    for name in files:
        path = os.path.join(root, name)
        target = os.path.join(out, name)
        if os.path.islink(path):
            os.symlink(os.readlink(path), target)
            continue
        try:
            os.link(path, target)
            linked += 1
        except OSError:
            shutil.copy2(path, target)
            copied += 1
print(f"[core-build] node_modules: {linked} files linked, {copied} copied")
PY
    else
        log "npm ci in the copy (the checkout has no frontend/node_modules)"
        (cd "$COPY/frontend" && npm ci --no-audit --no-fund)
    fi
}

step_frontend() {
    provision_node_modules
    local static="$COPY/frontend/out/_next/static"
    if [ "$PROFILE" = core ]; then
        # Same shape as the "core build carries no module route" step of the
        # frontend-tests job; the copy has no modules/, so the alias resolves
        # to the stubs on its own -- EXPERIMENTLY_PROFILE=core says so
        # explicitly.
        (cd "$COPY/frontend" && rm -rf .next out && EXPERIMENTLY_PROFILE=core npm run build)
        [ -d "$static" ] || die "next build produced no out/_next/static"
        local leaked
        leaked="$(grep -rlE '/api/v1/workspaces|/api/v1/rbac/|/api/v1/hipaa|/api/v1/warehouse/' "$static" || true)"
        if [ -n "$leaked" ]; then
            printf '%s\n' "$leaked" | sed 's/^/    /' >&2
            die "the core bundle references module routes (files above)"
        fi
        # The other half of the check: without it the grep above also passes
        # when the stub index page vanished entirely, which is the one
        # regression it exists for. It greps for the stub page's own
        # description rather than a chunk filename -- Turbopack, the default
        # bundler from Next 16, emits flat content-hashed chunks with no
        # `chunks/pages/` directory, so the old
        # `find "$static/chunks/pages" -name 'workspaces-*.js'` matched
        # nothing in either profile. The real module's page says "Isolated
        # project namespaces" instead, so the string is in no full bundle.
        if ! grep -rl 'Separate teams into workspaces' "$static" >/dev/null; then
            die "the core bundle has no workspaces stub page (its description is in no chunk)"
        fi
        log "core bundle: no module route, workspaces stub present"
    else
        (cd "$COPY/frontend" && rm -rf .next out && EXPERIMENTLY_PROFILE=full npm run build)
        [ -d "$static" ] || die "next build produced no out/_next/static"
        grep -rqlE '/api/v1/workspaces' "$static" \
            || die "the full bundle does not reference /api/v1/workspaces -- the @modules alias resolved to the stubs"
        log "full bundle: module routes present"
    fi
}

# -------------------------------------------------------------------------
# vectors — the cross-SDK golden vectors (make test-sdk)
# -------------------------------------------------------------------------
# The Python and JS SDKs only: the JS one builds sdk/js (npm ci + tsc) in the
# copy. Go, edge and React Native run in the SDK Contract Tests job, which sets
# up their toolchains; the SDKs are the same in every profile.
step_vectors() {
    (cd "$COPY" && "$PYTHON" -m pytest tests/sdk-contract -p no:cov -q -o addopts="")
    (cd "$COPY" && "$PYTHON" tests/sdk-contract/hash_contract.py python js)
}

# -------------------------------------------------------------------------
# licences — what the API image ships
# -------------------------------------------------------------------------
# `pip-licenses --fail-on="GPL;AGPL;SSPL"` is the obvious spelling, and it does
# not work: pip-licenses matches --fail-on/--allow-only against the WHOLE
# licence name, case-insensitively, so `GPL` matches a package whose licence
# is literally "GPL" and nothing else -- that flag passes psycopg2's "GNU
# Library or Lesser General Public License (LGPL)" *and* python-debian's
# "GPL-2.0-or-later" (verified: exit 0 with both installed).  Its
# --partial-match mode is the substring bug the third-party report once had:
# "GPL" then matches LGPL and AGPL too and psycopg2 fails the build.  So this
# is an allow-list of exact licence names, checked over exactly the packages
# the image ships: the transitive closure of backend/requirements/runtime.txt
# (+ modules/requirements.txt for --profile full, when it exists).  LGPL is
# allowed on purpose -- weak copyleft, linked not modified, which is why
# psycopg2-binary needs no per-package exception; GPL and AGPL are not on the
# list, because either would bind an Apache-2.0 distribution.  A new
# dependency with a licence not on the list fails here until someone reads it
# and adds it.
LICENCE_ALLOW_LIST='MIT;MIT License;MIT-0;MIT No Attribution License (MIT-0);MIT OR Apache-2.0;MIT AND PSF-2.0;BSD;BSD License;BSD-2-Clause;BSD-3-Clause;3-Clause BSD License;Apache-2.0;Apache 2.0;Apache License 2.0;Apache Software License;Apache-2.0 OR BSD-3-Clause;Apache-2.0 OR BSD-2-Clause;Apache-2.0 AND BSD-2-Clause;Apache-2.0 OR MIT;ISC;ISC License (ISCL);MPL-2.0;Mozilla Public License 2.0 (MPL 2.0);PSF-2.0;Python Software Foundation License;The Unlicense (Unlicense);Unlicense;GNU Library or Lesser General Public License (LGPL);LGPL-2.1-or-later;LGPL-3.0-or-later'

step_licences() {
    local packages_file packages
    packages_file="$(mktemp "${TMPDIR:-/tmp}/controlvar-packages.XXXXXX")"
    (cd "$COPY" && PROFILE="$PROFILE" "$PYTHON" - > "$packages_file" <<'PY'
import os
import sys
from importlib import metadata

from packaging.requirements import Requirement

files = ["backend/requirements/runtime.txt"]
if os.environ["PROFILE"] == "full" and os.path.exists("modules/requirements.txt"):
    files.append("modules/requirements.txt")


def canonical(name):
    return name.lower().replace("_", "-").replace(".", "-")


roots = []
for path in files:
    for line in open(path, encoding="utf-8"):
        line = line.split("#", 1)[0].strip()
        if not line or line.startswith(("-", "git+")):
            continue
        roots.append(Requirement(line))

closure, missing, pending = set(), [], list(roots)
while pending:
    req = pending.pop()
    name = canonical(req.name)
    if name in closure:
        continue
    try:
        dist = metadata.distribution(req.name)
    except metadata.PackageNotFoundError:
        missing.append(req.name)
        continue
    closure.add(name)
    for spec in dist.requires or ():
        child = Requirement(spec)
        if child.marker is not None:
            wanted = any(child.marker.evaluate({"extra": e}) for e in req.extras)
            if not wanted and not child.marker.evaluate({"extra": ""}):
                continue
        pending.append(child)

if missing:
    print(f"pinned but not installed here: {sorted(missing)}", file=sys.stderr)
    sys.exit(1)
print(f"[core-build] {len(closure)} packages in the closure of {', '.join(files)}", file=sys.stderr)
print(" ".join(sorted(closure)))
PY
    )
    packages="$(cat "$packages_file")"
    rm -f "$packages_file"
    # shellcheck disable=SC2086  # $packages is a space-separated list on purpose
    (cd "$COPY" && "$PYTHON" -m piplicenses --from=mixed --allow-only="$LICENCE_ALLOW_LIST" \
        --packages $packages --format=plain) \
        || die "a shipped Python package carries a licence outside the allow-list (see above)"
}

# -------------------------------------------------------------------------
# Sequence
# -------------------------------------------------------------------------
# ALL_STEPS is the contract --steps is validated against; keep it and the
# step_* functions in sync or a valid name would still select nothing.  Before
# preflight, so it costs nothing and runs even without the build tooling.
for _step in $ALL_STEPS; do
    declare -F "step_$_step" >/dev/null \
        || die "ALL_STEPS names '$_step' but there is no step_$_step function"
done
while read -r _fn; do
    case " $ALL_STEPS " in
        *" ${_fn#step_} "*) ;;
        *) die "$_fn exists but '${_fn#step_}' is not in ALL_STEPS" ;;
    esac
done < <(declare -F | awk '{print $3}' | grep '^step_')
unset _step _fn

# The sequence this profile runs, in order: ALL_STEPS minus the steps the
# profile has no use for.
SEQUENCE="$ALL_STEPS"
if [ "$PROFILE" != full ]; then
    SEQUENCE="${SEQUENCE/ modules_tests/}"
fi

REUSING_COPY=0
if [ -n "$SHARD" ] && [ -n "$BUILD_DIR" ] && [ -f "$BUILD_DIR/backend/app/main.py" ]; then
    # A shard against a copy an earlier `--keep` run left: its steps without
    # copy and strip, as a --steps filter is run there.
    REUSING_COPY=1
    _reused=""
    for _step in $SEQUENCE; do
        case "$_step" in copy|strip) continue ;; esac
        want_step "$_step" && _reused="${_reused:+$_reused,}$_step"
    done
    STEPS="$_reused"
    unset _reused _step
elif [ -n "$STEPS" ] && [ -n "$BUILD_DIR" ] && [ -f "$BUILD_DIR/backend/app/main.py" ]; then
    # Debugging: re-run some steps against a copy an earlier `--keep` run left.
    REUSING_COPY=1
    case ",$STEPS," in
        *",copy,"*|*",strip,"*) die "--steps cannot include copy or strip when reusing a copy" ;;
    esac
elif [ -n "$STEPS" ]; then
    # A step filter still needs a tree to work on.
    want_step copy || STEPS="copy,strip,$STEPS"
fi

# --list-steps: what the run WOULD do, without touching anything.  The filter
# is matched here by the same `want_step` the sequence uses, so this is the
# contract `--steps` promises and not a second reading of it.
if [ "$LIST_STEPS" = 1 ]; then
    trap - EXIT   # nothing ran; an "OK" summary here would be a lie
    for _step in $SEQUENCE; do
        want_step "$_step" && printf '%s\n' "$_step"
    done
    exit 0
fi

banner "preflight"
if [ -n "$SHARD" ]; then
    mkdir -p "$SHARD_REPORTS" || die "cannot create the report directory $SHARD_REPORTS"
    log "shard $SHARD: steps $(for _step in $SEQUENCE; do want_step "$_step" && printf '%s ' "$_step"; done)"
    log "shard $SHARD: reports in $SHARD_REPORTS"
fi
preflight

if [ "$REUSING_COPY" = 1 ]; then
    COPY="$(cd "$BUILD_DIR" && pwd)"
    KEEP=1
    warn "reusing the existing copy $COPY; copy/strip are not re-run"
fi
STEP_RUNNING=0   # setup is done

for _step in $SEQUENCE; do
    run_step "$_step"
done
