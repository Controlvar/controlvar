#!/usr/bin/env bash
# The compose-upgrade job (.github/workflows/compose-production.yml): a
# self-hoster's upgrade of the production compose stack, from the previous
# release's published images to this change's, by the blocks of
# deploy/compose/README.md -- extracted from the page (scripts/guide_blocks.py
# --marker compose-upgrade) and run as a reader pastes them, never a copy
# (#1807).
#
# One phase per subcommand, run in this order by the workflow:
#
#   previous  resolve N-1: the newest release at or below VERSION whose core
#             images, as that tag's own compose.yml names them, read
#             anonymously from GHCR (one release back, with a warning, while
#             the newest one's are still publishing), or PREVIOUS_VERSION,
#             with no fallback; writes version= to $GITHUB_OUTPUT
#   images    this change's two builds, recorded by ID, and the revisions its
#             API image carries, before anything is pulled under their names
#   blocks    the page's blocks, every bash block it has, in page order
#   install   the self-hoster's directory, a clone at vN-1; then a tag at this
#             change's commit, which the clone does not have (C5(a));
#             install-named, then install, which pulls N-1's images; one
#             experiment and one flag through the API; N-1's API image ID, its
#             revisions and alembic_version
#   prepare   backup, check-backup, get-files (RELEASE = the tag above),
#             check-settings (the old release still serving), pull
#   retag     the one step that is not a page block: this change's builds, by
#             ID, under the names the new compose.yml resolves
#   upgrade   upgrade, verify, addresses, check-migrations: the API runs this
#             change's build, the data and the sign-in survive, and the
#             migrations it logged are the delta, each applied once
#   restart   `restart api`, waited for by one more bootstrap line (C5(b)):
#             a restart migrates nothing
#   rollback  get-files (RELEASE = vN-1), pull, upgrade: in place, as the
#             page's step 7 says. N-1 serves again on an empty delta and
#             refuses the database on a non-empty one. Then the stack and its
#             volumes are removed, standing for a new host, and restore
#             brings N-1 back with the backup's rows
#   diagnose  containers and logs, for a failed run
#   teardown  remove the stack and its volumes
#
# Every block runs in run_block: in the clone's deploy/compose, under `env -i`
# with a fresh HOME (no registry credentials, so every pull is anonymous),
# PATH, and only the variables the page tells the reader to set. The job's own
# compose commands (dc) run the same way. Nothing logs in to a registry.
#
# Inputs (environment): optional PREVIOUS_VERSION (previous) and
# COMPOSE_UPGRADE_WORK (default: $RUNNER_TEMP/compose-upgrade).
set -euo pipefail

cd "$(dirname "$0")/.."
REPO_ROOT=$(pwd)

GUIDE=deploy/compose/README.md
MARKER=compose-upgrade
GUIDE_BLOCKS=install,install-named,addresses,backup,restore,check-backup,get-files,check-settings,pull,upgrade,verify,check-migrations
# What run_block may pass a block: the two variables of the Upgrade section's
# table, and the ENV_FILE that Run it names.
DECLARED='RELEASE PUBLIC_BASE_URL ENV_FILE'
# compose.yml's `name:`, which every command of the procedure names.
PROJECT=experimently-selfhost
PROFILE=core
# This change's builds, under local names (the workflow's build steps).
BUILD_API=compose-upgrade-api:core
BUILD_WEB=compose-upgrade-web:core
# Made at this change's commit after the clone, for get-files to check out.
CHANGE_TAG=ci-compose-upgrade
# The bound on the wait for the API after `restart api`, in seconds.
RESTART_WAIT=300
# The experiment and the flag the job adds, and reads back after every step.
PROBE_KEY=compose-upgrade-probe
PROBE_NAME='Compose upgrade probe'
PROBE_ROLLOUT=30
# What an older image says of a database a newer release migrated
# (backend/app/db/bootstrap.py), and step 7 tells the reader to look for.
REFUSAL='this database was migrated by a newer Controlvar release'
PULL_FAILED="The previous release's images could not be read anonymously: are the packages still public?"

WORK=${COMPOSE_UPGRADE_WORK:-${RUNNER_TEMP:-/tmp}/compose-upgrade}
# The self-hoster's clone, and the directory the page's commands run in.
SELFHOST=$WORK/selfhost
DIR=$SELFHOST/deploy/compose
BLOCKS=$WORK/blocks
# The reader's HOME: ~/controlvar-backups lives here, and no registry
# credentials do.
READER_HOME=$WORK/home
OUT=$WORK/out

mkdir -p "$WORK" "$OUT"

say() { printf '[compose-upgrade] %s\n' "$*"; }
# To stderr: a fail inside $(...) would otherwise be captured, not shown.
fail() {
    printf '::error::%s\n' "$*" >&2
    exit 1
}

summary() {
    say "$*"
    [ -z "${GITHUB_STEP_SUMMARY:-}" ] ||
        printf '### Compose Upgrade\n\n%s\n\n' "$*" >>"$GITHUB_STEP_SUMMARY"
}

# ---------------------------------------------------------------------------
# run_block NAME [VAR=VALUE...]: one of the page's blocks, as a reader runs it:
# in the clone's deploy/compose, under `env -i`, with HOME, PATH and only the
# variables given, each of which must be one the page declares. stdout and
# stderr go to $OUT/NAME.out and $OUT/NAME.err; the block's exit status is
# returned.
run_block() {
    local name=$1 status=0 pair
    shift
    for pair in "$@"; do
        case " $DECLARED " in
            *" ${pair%%=*} "*) ;;
            *) fail "run_block $name: ${pair%%=*} is not a variable the page tells the reader to set ($DECLARED)" ;;
        esac
    done
    say "--- page block '$name':"
    sed 's/^/    | /' "$BLOCKS/$name.sh"
    (cd "$DIR" && env -i HOME="$READER_HOME" PATH="$PATH" "$@" bash -euo pipefail "$BLOCKS/$name.sh") \
        >"$OUT/$name.out" 2>"$OUT/$name.err" || status=$?
    sed 's/^/    out: /' "$OUT/$name.out"
    sed 's/^/    err: /' "$OUT/$name.err"
    say "--- '$name' exited $status"
    return "$status"
}

# dc ARGS...: the job's own docker compose command, not a page block: the same
# directory, project and `env -i` the blocks get.
dc() {
    (cd "$DIR" && env -i HOME="$READER_HOME" PATH="$PATH" docker compose -p "$PROJECT" "$@")
}

# env_value NAME: NAME's value in the stack's .env, read as text, never sourced.
env_value() {
    sed -n "s/^$1=//p" "$DIR/.env" | sed -n '$p'
}

# connect: the dashboard's published address, and the Host the API answers for
# (the host name of PUBLIC_BASE_URL; any other answers 400). Sets BASE, HOST.
connect() {
    local url port
    url=$(env_value PUBLIC_BASE_URL)
    port=$(env_value HTTP_PORT)
    [ -n "$url" ] || fail "no PUBLIC_BASE_URL in $DIR/.env"
    HOST=$(python3 -c 'import sys, urllib.parse; print(urllib.parse.urlparse(sys.argv[1]).hostname)' "$url")
    BASE="http://127.0.0.1:${port:-8080}"
}

# sign_in: the first administrator, with the password in .env (SPEC (d)).
# Sets TOKEN. The password goes in a file, never on a command line.
sign_in() {
    local code admin password
    connect
    admin=$(env_value FIRST_SUPERUSER)
    password=$(env_value FIRST_SUPERUSER_PASSWORD)
    jq -n --arg email "$admin" --arg password "$password" \
        '{email: $email, password: $password}' >"$WORK/login-request.json"
    code=$(curl -s -o "$WORK/login.json" -w '%{http_code}' -H "Host: $HOST" \
        -H 'content-type: application/json' --data @"$WORK/login-request.json" \
        "$BASE/api/v1/auth/login" || true)
    [ "$code" = 200 ] ||
        fail "(d) the first administrator $admin could not sign in with the FIRST_SUPERUSER_PASSWORD in .env: $code"
    TOKEN=$(jq -er .access_token "$WORK/login.json") ||
        fail "(d) the sign-in answered 200 with no access_token"
    say "(d) signed in as $admin, the first administrator, with the password in .env"
}

# api METHOD PATH [BODY]: an API request through the dashboard's port as the
# signed-in administrator. Prints the body; fails on any answer but 2xx.
api() {
    local method=$1 path=$2
    if [ "$#" -ge 3 ]; then
        curl -fsS -X "$method" -H "Host: $HOST" -H "Authorization: Bearer $TOKEN" \
            -H 'content-type: application/json' -d "$3" "$BASE$path"
    else
        curl -fsS -X "$method" -H "Host: $HOST" -H "Authorization: Bearer $TOKEN" "$BASE$path"
    fi
}

# check_rows WHEN: exactly one experiment and one flag, the seeded ones, read
# back by id with the key and the fields the seed gave them (SPEC (c)).
check_rows() {
    local when=$1 n eid fid
    [ -n "${TOKEN:-}" ] || sign_in
    api GET /api/v1/experiments/ >"$OUT/experiments.json" ||
        fail "$when: GET /api/v1/experiments/ failed"
    n=$(jq -er .total "$OUT/experiments.json") || fail "$when: the experiment list has no total"
    [ "$n" = 1 ] || fail "$when: expected 1 experiment, got $n"
    api GET /api/v1/feature-flags/ >"$OUT/flags.json" ||
        fail "$when: GET /api/v1/feature-flags/ failed"
    n=$(jq -er .total "$OUT/flags.json") || fail "$when: the flag list has no total"
    [ "$n" = 1 ] || fail "$when: expected 1 flag, got $n"
    if [ ! -s "$WORK/rows.txt" ]; then
        eid=$(jq -er '.items[0].id' "$OUT/experiments.json")
        fid=$(jq -er '.items[0].id' "$OUT/flags.json")
        printf 'experiment=%s\nflag=%s\n' "$eid" "$fid" >"$WORK/rows.txt"
    fi
    eid=$(sed -n 's/^experiment=//p' "$WORK/rows.txt")
    fid=$(sed -n 's/^flag=//p' "$WORK/rows.txt")
    api GET "/api/v1/experiments/$eid" >"$OUT/experiment.json" ||
        fail "$when: GET /api/v1/experiments/$eid failed"
    jq -e --arg id "$eid" --arg key "$PROBE_KEY" --arg name "$PROBE_NAME" \
        '.id == $id and .key == $key and .name == $name' "$OUT/experiment.json" >/dev/null ||
        fail "$when: experiment $eid reads back as $(jq -c '{id, key, name}' "$OUT/experiment.json")"
    api GET "/api/v1/feature-flags/$fid" >"$OUT/flag.json" ||
        fail "$when: GET /api/v1/feature-flags/$fid failed"
    jq -e --arg id "$fid" --arg key "$PROBE_KEY" --argjson rollout "$PROBE_ROLLOUT" \
        '.id == $id and .key == $key and .rollout_percentage == $rollout' "$OUT/flag.json" >/dev/null ||
        fail "$when: flag $fid reads back as $(jq -c '{id, key, rollout_percentage}' "$OUT/flag.json")"
    say "(c) $when: 1 experiment and 1 flag, the seeded ones ($eid, $fid)"
}

# check_version VERSION WHEN: /api/v1/modules reports the core profile at
# VERSION. The served version, not evidence of an upgrade (C7): the image of
# the release VERSION names and this change's build report the same string.
check_version() {
    connect
    curl -fsS -H "Host: $HOST" "$BASE/api/v1/modules" >"$OUT/modules.json" ||
        fail "$2: /api/v1/modules did not answer"
    jq -e --arg v "$1" '.profile == "core" and .version == $v' "$OUT/modules.json" >/dev/null ||
        fail "$2: /api/v1/modules reports $(jq -c '{profile, version}' "$OUT/modules.json"), expected core $1"
    say "$2: /api/v1/modules reports core $1"
}

# alembic_rows: the stack's alembic_version rows, sorted, one per line. The
# user and database are the postgres service's own, read inside it.
alembic_rows() {
    # shellcheck disable=SC2016
    dc exec -T postgres sh -c \
        'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -tA -c "SELECT version_num FROM experimentation.alembic_version ORDER BY 1"'
}

# api_image_id: the ID of the image the stack's api container runs, in the
# form `docker image inspect` gives it, which the builds are recorded in.
api_image_id() {
    local cid image
    cid=$(dc ps -q api)
    [ -n "$cid" ] || fail "the stack has no api container"
    image=$(docker inspect --format '{{.Image}}' "$cid")
    docker image inspect --format '{{.Id}}' "$image"
}

# log_count TEXT: how many lines of the api service's log, every start of its
# container, hold TEXT.
log_count() {
    dc logs --no-color api >"$OUT/api.log" 2>&1 || true
    grep -cF -- "$1" "$OUT/api.log" || true
}

# services_healthy: `ps` lists the four services, each running and healthy
# (the page's step 6: "each of the four services is listed and healthy").
services_healthy() {
    dc ps --format json >"$OUT/ps.json"
    jq -s -e '
        (if length == 1 and (.[0] | type) == "array" then .[0] else . end) as $c
        | ([$c[].Service] | sort) == ["api", "postgres", "redis", "web"]
          and all($c[]; .State == "running" and .Health == "healthy")' "$OUT/ps.json" >/dev/null ||
        fail "not every service is listed and healthy: $(jq -c -s '[(if length == 1 and (.[0] | type) == "array" then .[0] else . end)[] | {Service, State, Health}]' "$OUT/ps.json")"
    say "the four services are listed and healthy"
}

# revisions IMAGE: every alembic revision id the image carries, sorted, one
# per line, read from its files (as scripts/chart_kind.sh does).
revisions() {
    docker run --rm -i --entrypoint python "$1" - <<'PY'
import pathlib
import re

REVISION = re.compile(r"""^revision\s*(?::[^=\n]*)?=\s*['"]([^'"]+)['"]""", re.M)
CORE = pathlib.Path("/app/backend/app/db/migrations/versions")
MODULES = pathlib.Path("/app/modules/backend/app/db/migrations/versions")
if not CORE.is_dir():
    raise SystemExit(f"{CORE}: not a directory")
found = set()
for directory in (CORE, MODULES):
    if directory.is_dir():
        for path in directory.glob("*.py"):
            found.update(REVISION.findall(path.read_text(encoding="utf-8")))
if not found:
    raise SystemExit(f"no revisions under {CORE}")
print("\n".join(sorted(found)))
PY
}
# (Python's sorted() is code-point order: callers compare with LC_ALL=C.)

# ---------------------------------------------------------------------------
# N-1: the newest release at or below VERSION whose core images are public.
# Right after a release its tag exists before release.yml has published its
# images, so when the newest tag's are not on GHCR yet, N-1 is the release
# before it, and the job summary says so. One step back and no further: if
# that release's images cannot be read either, the cause is not the
# publishing window, and the job fails.

# image_ref COMPOSE_FILE SERVICE: the image COMPOSE_FILE names for SERVICE,
# its profile variable read as core and its trailing comment dropped; read by
# indentation, compose.yml's own two-space layout.
image_ref() {
    awk -v want="$2" '
        /^[A-Za-z]/ { top = $0; sub(/:.*/, "", top); svc = ""; next }
        top == "services" && /^  [A-Za-z]/ { svc = $0; sub(/^ */, "", svc); sub(/:.*/, "", svc); next }
        top == "services" && svc == want && /^    image:/ {
            r = $0
            sub(/^    image:[ ]*/, "", r)
            sub(/[ ]+#.*$/, "", r)
            gsub(/["\047]/, "", r)
            print r
            exit
        }
    ' "$1" | sed 's/\${[A-Z_]*PROFILE:-[a-z]*}/'"$PROFILE"'/'
}

# tag_images VERSION: the api and web images vVERSION's own
# deploy/compose/compose.yml names, read from the tag's tree, never from this
# checkout. Sets TAG_API and TAG_WEB.
tag_images() {
    local v=$1 file ref
    mkdir -p "$WORK/tags"
    file="$WORK/tags/v$v.compose.yml"
    git show "v$v:deploy/compose/compose.yml" >"$file" 2>/dev/null ||
        fail "N-1 cannot be resolved: v$v has no deploy/compose/compose.yml"
    TAG_API=$(image_ref "$file" api)
    TAG_WEB=$(image_ref "$file" web)
    for ref in "$TAG_API" "$TAG_WEB"; do
        printf '%s\n' "$ref" | grep -Eqx "[a-z0-9][a-z0-9._/-]*:$PROFILE-${v//./\\.}" ||
            fail "N-1 cannot be resolved: v$v's compose.yml names [$TAG_API] [$TAG_WEB], not two $PROFILE-$v images"
    done
}

# readable REF...: every REF can be read from its registry anonymously: a
# manifest read with an empty client configuration, so no stored credential
# is sent and nothing is pulled.
readable() {
    local anon ref err rc=0
    anon=$(mktemp -d "${TMPDIR:-/tmp}/compose-upgrade-anon.XXXXXX")
    printf '{}\n' >"$anon/config.json"
    for ref in "$@"; do
        if ! DOCKER_CONFIG=$anon docker manifest inspect "$ref" >/dev/null 2>"$anon/err"; then
            err=$(tr '\n' ' ' <"$anon/err")
            say "not published (or not readable anonymously): $ref: ${err:0:200}"
            rc=1
            break
        fi
    done
    rm -rf "$anon"
    return "$rc"
}

# published VERSION: both images vVERSION's own compose.yml names read
# anonymously.
published() {
    tag_images "$1"
    readable "$TAG_API" "$TAG_WEB"
}

do_previous() {
    local version prev tags candidates newest older note
    version=$(cat VERSION)
    tags=$(git tag --list 'v*' | sed -n 's#^v\([0-9][0-9]*\.[0-9][0-9]*\.[0-9][0-9]*\)$#\1#p')
    [ -n "$tags" ] || fail "N-1 cannot be resolved: no vX.Y.Z tags in this checkout (it needs fetch-depth: 0)"
    if [ -n "${PREVIOUS_VERSION:-}" ]; then
        prev=$PREVIOUS_VERSION
        printf '%s\n' "$prev" | grep -Eqx '[0-9]+\.[0-9]+\.[0-9]+' ||
            fail "N-1 cannot be resolved: previous_version '$prev' is not X.Y.Z"
        printf '%s\n' "$tags" | grep -Fqx "$prev" ||
            fail "N-1 cannot be resolved: there is no release tag v$prev"
        # Asked for by name: no fallback.
        published "$prev" ||
            fail "N-1 cannot be resolved: previous_version $prev has no $PROFILE images that read anonymously. $PULL_FAILED"
    else
        # The tags that sort at or below VERSION, oldest first.
        candidates=$(printf '%s\n' "$tags" | while read -r t; do
            [ "$(printf '%s\n%s\n' "$t" "$version" | sort -V | sed -n '$p')" != "$version" ] || printf '%s\n' "$t"
        done | sort -V)
        newest=$(printf '%s\n' "$candidates" | sed -n '$p')
        [ -n "$newest" ] || fail "N-1 cannot be resolved: no release tag at or below VERSION $version"
        if published "$newest"; then
            prev=$newest
        else
            older=$(printf '%s\n' "$candidates" | sed '$d' | sed -n '$p')
            [ -n "$older" ] ||
                fail "N-1 cannot be resolved: v$newest has no published $PROFILE images and there is no earlier release to fall back to"
            published "$older" ||
                fail "N-1 cannot be resolved: neither v$newest nor v$older has published $PROFILE images. $PULL_FAILED"
            prev=$older
            note="N-1 is $older, not $newest: v$newest is tagged but its $PROFILE images are not published yet (release.yml publishes them after the tag), so the upgrade starts from the release before it."
            printf '::warning::%s\n' "$note"
            summary "$note"
        fi
    fi
    say "N-1 = $prev (VERSION $version)"
    printf '%s\n' "$prev" >"$WORK/previous-version"
    [ -z "${GITHUB_OUTPUT:-}" ] || printf 'version=%s\n' "$prev" >>"$GITHUB_OUTPUT"
}

# ---------------------------------------------------------------------------
do_images() {
    local api web
    api=$(docker image inspect --format '{{.Id}}' "$BUILD_API")
    web=$(docker image inspect --format '{{.Id}}' "$BUILD_WEB")
    if [ -z "$api" ] || [ -z "$web" ]; then
        fail "no image ID for $BUILD_API or $BUILD_WEB"
    fi
    printf 'api=%s\nweb=%s\n' "$api" "$web" >"$WORK/build.txt"
    cat "$WORK/build.txt"
    # Read by ID now: once this change's build carries the published name, a
    # pull of that name moves it to N-1's image.
    revisions "$api" >"$WORK/revisions-this"
    say "this change's api image carries $(grep -c . "$WORK/revisions-this") revisions"
}

do_blocks() {
    rm -rf "$BLOCKS"
    python3 scripts/guide_blocks.py "$GUIDE" --marker "$MARKER" --expect "$GUIDE_BLOCKS" --out "$BLOCKS"
}

do_install() {
    local prev
    prev=$(cat "$WORK/previous-version")
    rm -rf "$SELFHOST" "$READER_HOME" "$WORK/rows.txt"
    mkdir -p "$READER_HOME"
    # The self-hoster's directory: a clone at the release, as the install the
    # root README gives makes one (git clone, then cd deploy/compose).
    git clone --quiet --branch "v$prev" "$REPO_ROOT" "$SELFHOST"
    git -C "$SELFHOST" rev-parse HEAD >"$WORK/clone-head-before"
    # C5(a), first half: get-files' ref is made after the clone, so the page's
    # `git fetch --tags` is the only way the clone can have it.
    git -C "$REPO_ROOT" tag -f "$CHANGE_TAG" HEAD
    if git -C "$SELFHOST" rev-parse -q --verify "refs/tags/$CHANGE_TAG" >/dev/null; then
        fail "(C5a) the clone already has $CHANGE_TAG, made after it"
    fi
    (cd "$DIR" && env -i HOME="$READER_HOME" PATH="$PATH" docker compose version) ||
        fail "docker compose does not run under env -i with a fresh HOME"

    run_block install-named ENV_FILE="$WORK/named.env" || fail "the page's install-named block failed"
    grep -qx 'PUBLIC_BASE_URL=https://controlvar.example.com' "$WORK/named.env" ||
        fail "install-named did not write PUBLIC_BASE_URL=https://controlvar.example.com to ENV_FILE"
    grep -qx 'FIRST_SUPERUSER=you@example.com' "$WORK/named.env" ||
        fail "install-named did not write FIRST_SUPERUSER=you@example.com to ENV_FILE"
    [ ! -e "$DIR/.env" ] || fail "install-named wrote .env beside compose.yml, not ENV_FILE"

    run_block install || fail "the page's install block failed"
    api_image_id >"$WORK/before-api"
    say "N-1's api image: $(cat "$WORK/before-api")"
    revisions "$(cat "$WORK/before-api")" >"$WORK/revisions-previous"
    alembic_rows >"$WORK/alembic-before"
    [ -s "$WORK/alembic-before" ] || fail "N-1's database records no alembic_version row"
    say "N-1's alembic_version: $(tr '\n' ' ' <"$WORK/alembic-before")"
    check_version "$prev" "N-1 after the install"

    # The rows the upgrade must keep: one experiment, with a key, and one flag.
    sign_in
    api POST /api/v1/experiments/ "$(jq -nc --arg key "$PROBE_KEY" --arg name "$PROBE_NAME" '{
        name: $name, key: $key,
        variants: [
            {name: "control", is_control: true, traffic_allocation: 50},
            {name: "treatment", traffic_allocation: 50}
        ],
        metrics: [{name: "Purchase", event_name: "purchase", is_primary: true}]
    }')" >"$OUT/seed-experiment.json" || fail "adding the experiment failed"
    api POST /api/v1/feature-flags/ "$(jq -nc --arg key "$PROBE_KEY" --arg name "$PROBE_NAME" \
        --argjson rollout "$PROBE_ROLLOUT" '{key: $key, name: $name, rollout_percentage: $rollout}')" \
        >"$OUT/seed-flag.json" || fail "adding the flag failed"
    check_rows "N-1, after the seed"
}

do_prepare() {
    local before head want still
    before=$(cat "$WORK/before-api")
    run_block backup || fail "the page's backup block failed"
    [ -s "$READER_HOME/controlvar-backups/controlvar.dump" ] || fail "the backup block wrote no dump"
    cmp -s "$DIR/.env" "$READER_HOME/controlvar-backups/controlvar.env" ||
        fail "the backup's .env is not the stack's"
    run_block check-backup || fail "the page's check-backup block refused the dump the backup block wrote"

    if git -C "$SELFHOST" rev-parse -q --verify "refs/tags/$CHANGE_TAG" >/dev/null; then
        fail "(C5a) the clone has $CHANGE_TAG before get-files"
    fi
    run_block get-files RELEASE="$CHANGE_TAG" || fail "the page's get-files block failed"
    # C5(a), second half: the page's checkout moved the clone to this change.
    head=$(git -C "$SELFHOST" rev-parse HEAD)
    want=$(git -C "$REPO_ROOT" rev-parse HEAD)
    [ "$head" = "$want" ] || fail "(C5a) after get-files the clone HEAD is not this change: $head, expected $want"
    if [ "$(cat "$WORK/clone-head-before")" = "$want" ]; then
        say "(C5a) N-1's tag is this change's commit (a run at the tag), so get-files moved nothing"
    else
        say "(C5a) get-files moved the clone from v$(cat "$WORK/previous-version") to this change ($want)"
    fi

    if ! run_block check-settings; then
        still=$(api_image_id)
        if [ "$still" = "$before" ]; then
            say "the old release still serves: the api runs N-1's image $still"
        else
            say "the api runs $still, not N-1's image $before"
        fi
        fail "the page's check-settings block refused this change's compose.yml: $(tr '\n' ' ' <"$OUT/check-settings.err")"
    fi
    [ ! -s "$OUT/check-settings.out" ] || fail "check-settings printed something; with every setting in .env it prints nothing"
    [ "$(api_image_id)" = "$before" ] || fail "check-settings changed the running api"

    if ! run_block pull; then
        local api_image web_image err
        dc config --format json >"$WORK/resolved.json"
        api_image=$(jq -er .services.api.image "$WORK/resolved.json")
        web_image=$(jq -er .services.web.image "$WORK/resolved.json")
        if ! readable "$api_image" "$web_image"; then
            fail "the page's pull block failed: this change's compose.yml names $api_image and $web_image, which are not published yet. Inside the release window (VERSION bumped, release.yml still publishing) this job is red until they are."
        fi
        err=$(tr '\n' ' ' <"$OUT/pull.err")
        fail "the page's pull block failed: ${err:0:600}"
    fi
}

# The one step that is not a page block: on a self-hoster's host, `pull` gets
# the release's images; here those are this change's builds, put under the
# names the new compose.yml resolves (on a pull request, `pull` has just
# fetched N-1's image under the same name).
do_retag() {
    local api web api_image web_image
    api=$(sed -n 's/^api=//p' "$WORK/build.txt")
    web=$(sed -n 's/^web=//p' "$WORK/build.txt")
    dc config --format json >"$WORK/resolved.json"
    api_image=$(jq -er .services.api.image "$WORK/resolved.json")
    web_image=$(jq -er .services.web.image "$WORK/resolved.json")
    docker tag "$api" "$api_image"
    docker tag "$web" "$web_image"
    say "this change's builds, by ID, under the names the new compose.yml resolves:"
    say "  api: $api_image = $api"
    say "  web: $web_image = $web"
}

do_upgrade() {
    local prev before build after version delta row line url
    prev=$(cat "$WORK/previous-version")
    before=$(cat "$WORK/before-api")
    build=$(sed -n 's/^api=//p' "$WORK/build.txt")
    version=$(cat VERSION)

    run_block upgrade || fail "the page's upgrade block failed"
    # (a): the one witness that the upgrade happened.
    after=$(api_image_id)
    [ "$after" != "$before" ] || fail "(a) api image unchanged by upgrade: before $before, after $after"
    [ "$after" = "$build" ] ||
        fail "(a) after the upgrade the api runs $after, not this change's build $build (N-1's was $before)"
    say "(a) the api image changed from N-1's $before to this change's build $after"

    # The page's table: PUBLIC_BASE_URL is the value in .env.
    url=$(env_value PUBLIC_BASE_URL)
    run_block verify PUBLIC_BASE_URL="$url" || fail "the page's verify block failed"
    services_healthy
    sed -n '$p' "$OUT/verify.out" >"$OUT/verify-modules.json"
    jq -e --arg v "$version" '.profile == "core" and .version == $v' "$OUT/verify-modules.json" >/dev/null ||
        fail "(b) the verify block's /api/v1/modules: $(cat "$OUT/verify-modules.json"), expected core $version"
    say "(b) the verify block's /api/v1/modules reports core $version (the served version, not upgrade evidence)"

    # The delta: the revisions this change's image carries and N-1's does not.
    LC_ALL=C comm -13 "$WORK/revisions-previous" "$WORK/revisions-this" >"$WORK/revisions-delta"
    LC_ALL=C comm -23 "$WORK/revisions-previous" "$WORK/revisions-this" >"$WORK/revisions-only-previous"
    if [ -s "$WORK/revisions-only-previous" ]; then
        cat "$WORK/revisions-only-previous"
        fail "N-1 ($prev) carries revisions this change's image does not: an upgrade from it would be refused"
    fi
    delta=$(grep -c . "$WORK/revisions-delta" || true)
    say "the delta, v$prev to this change: $delta revision(s): [$(tr '\n' ' ' <"$WORK/revisions-delta")]"

    # (e), the witness first: alembic_version, before any log is read.
    alembic_rows >"$WORK/alembic-after"
    if [ "$delta" -eq 0 ]; then
        cmp -s "$WORK/alembic-before" "$WORK/alembic-after" ||
            fail "(e) alembic_version moved from [$(tr '\n' ' ' <"$WORK/alembic-before")] to [$(tr '\n' ' ' <"$WORK/alembic-after")] with an empty delta"
    else
        if cmp -s "$WORK/alembic-before" "$WORK/alembic-after"; then
            fail "(e) alembic_version unchanged, delta [$(tr '\n' ' ' <"$WORK/revisions-delta")]"
        fi
        while read -r row; do
            grep -Fqx -- "$row" "$WORK/revisions-delta" ||
                fail "(e) alembic_version records $row, which is not in the delta"
        done <"$WORK/alembic-after"
    fi
    say "(e) alembic_version: [$(tr '\n' ' ' <"$WORK/alembic-after")]"

    sign_in
    check_rows "after the upgrade"

    run_block addresses || fail "the page's addresses block failed"
    [ "$(grep -c . "$OUT/addresses.out" || true)" -ge 1 ] || fail "the addresses block printed no address"

    run_block check-migrations ||
        fail "the page's check-migrations block failed: no bootstrap line and no Running upgrade line in what it read"
    # C5(c): the log it read is this stack's.
    [ "$(grep -c 'running database bootstrap' "$OUT/check-migrations.out" || true)" -ge 1 ] ||
        fail "(C5c) check-migrations printed no bootstrap line: it read no log of this stack"
    # (e), (g): the Running upgrade lines are the delta, each once, and there
    # are none when the delta is empty.
    sed -n 's/.*Running upgrade .* -> \([^ ,"]*\).*/\1/p' "$OUT/check-migrations.out" |
        LC_ALL=C sort >"$WORK/revisions-logged"
    cmp -s "$WORK/revisions-logged" "$WORK/revisions-delta" ||
        fail "(e) the API logged Running upgrade to [$(tr '\n' ' ' <"$WORK/revisions-logged")], the delta is [$(tr '\n' ' ' <"$WORK/revisions-delta")]"

    if [ "$delta" -gt 0 ]; then
        line="v$prev -> this change: the delta contains $delta migration(s), each applied once: $(tr '\n' ' ' <"$WORK/revisions-delta")"
    else
        line="v$prev -> this change: the delta contains no migration, so this run proves the page's commands and that the data survives, not a schema change"
    fi
    summary "$line"
}

# wait_for_bootstrap BEFORE: until the api log holds more than BEFORE
# bootstrap lines and the container is healthy, for at most RESTART_WAIT s.
wait_for_bootstrap() {
    local before=$1 waited=0 count=0 health=
    while [ "$waited" -lt "$RESTART_WAIT" ]; do
        count=$(log_count 'running database bootstrap')
        health=$(docker inspect --format '{{.State.Health.Status}}' "$(dc ps -q api)" 2>/dev/null || true)
        if [ "$count" -gt "$before" ] && [ "$health" = healthy ]; then
            say "the api is healthy after ${waited}s, with $count bootstrap line(s)"
            return 0
        fi
        sleep 5
        waited=$((waited + 5))
    done
    say "after ${RESTART_WAIT}s: $count bootstrap line(s), health '$health'"
}

do_restart() {
    local before after upgrades rows started
    before=$(log_count 'running database bootstrap')
    upgrades=$(log_count 'Running upgrade')
    rows=$(alembic_rows)
    started=$(docker inspect --format '{{.State.StartedAt}}' "$(dc ps -q api)")
    dc restart api
    # C5(b): `restart` returns before the entrypoint has run again.
    wait_for_bootstrap "$before"
    [ "$(docker inspect --format '{{.State.StartedAt}}' "$(dc ps -q api)")" != "$started" ] ||
        fail "restart api did not restart the api container"
    after=$(log_count 'running database bootstrap')
    [ "$((after - before))" -eq 1 ] ||
        fail "(C5b) after restart api: bootstrap lines +$((after - before)), expected +1"
    # (f): nothing migrates again.
    [ "$(log_count 'Running upgrade')" -eq "$upgrades" ] ||
        fail "(f) restart api logged a new Running upgrade line; a restart must migrate nothing"
    [ "$(alembic_rows)" = "$rows" ] || fail "(f) restart api moved alembic_version"
    check_rows "after restart api"
}

do_rollback() {
    local prev before delta rows id
    prev=$(cat "$WORK/previous-version")
    before=$(cat "$WORK/before-api")
    delta=$(grep -c . "$WORK/revisions-delta" || true)
    rows=$(alembic_rows)
    say "rolling back in place to v$prev, as the page's step 7 says (the delta: $delta migration(s))"

    run_block get-files RELEASE="v$prev" || fail "the page's get-files block failed going back to v$prev"
    [ "$(git -C "$SELFHOST" rev-parse HEAD)" = "$(git -C "$SELFHOST" rev-parse "v$prev^{commit}")" ] ||
        fail "after get-files the clone is not at v$prev"
    run_block pull || fail "the page's pull block failed for v$prev"
    if [ "$delta" -eq 0 ]; then
        run_block upgrade ||
            fail "rolling back in place to v$prev failed; with no migration in the delta the old release starts on the database"
        id=$(api_image_id)
        [ "$id" = "$before" ] || fail "after rolling back in place the api runs $id, not N-1's $before"
        check_version "$prev" "after rolling back in place"
        [ "$(alembic_rows)" = "$rows" ] || fail "rolling back in place moved alembic_version"
        sign_in
        check_rows "after rolling back in place"
        summary "rolled back in place to v$prev: N-1's image serves the same rows"
    else
        if run_block upgrade; then
            fail "rolling back in place to v$prev succeeded on a database this change migrated; the old release must refuse it"
        fi
        dc logs --no-color api >"$OUT/api.log" 2>&1 || true
        grep -F -- "$REFUSAL" "$OUT/api.log" ||
            fail "the in-place rollback failed, but the api log does not say '$REFUSAL'"
        [ "$(alembic_rows)" = "$rows" ] || fail "the refused rollback changed alembic_version"
        summary "rolled back in place to v$prev: refused, as the page says ('$REFUSAL'), and the database is unchanged"
    fi

    # A new host, as Restore starts from: the job's own removal of the stack
    # and its volumes, then the page's restore on N-1's files.
    dc down -v --remove-orphans
    run_block restore || fail "the page's restore block failed"
    id=$(api_image_id)
    [ "$id" = "$before" ] || fail "after the restore the api runs $id, not N-1's $before"
    check_version "$prev" "after the restore"
    [ "$(alembic_rows)" = "$(cat "$WORK/alembic-before")" ] ||
        fail "after the restore alembic_version is not N-1's: $(alembic_rows | tr '\n' ' ')"
    sign_in
    check_rows "after the restore"
    summary "restored the backup into v$prev: it serves the seeded rows, and the first administrator signs in"
}

do_diagnose() {
    set +e
    [ -f "$DIR/.env" ] || {
        say "no stack to diagnose"
        return 0
    }
    dc ps -a
    dc logs --no-color --tail=200
    return 0
}

do_teardown() {
    [ -f "$DIR/.env" ] || {
        say "no stack to remove"
        return 0
    }
    dc down -v --remove-orphans
}

case "${1:-}" in
    previous) do_previous ;;
    images) do_images ;;
    blocks) do_blocks ;;
    install) do_install ;;
    prepare) do_prepare ;;
    retag) do_retag ;;
    upgrade) do_upgrade ;;
    restart) do_restart ;;
    rollback) do_rollback ;;
    diagnose) do_diagnose ;;
    teardown) do_teardown ;;
    *)
        printf 'usage: %s previous|images|blocks|install|prepare|retag|upgrade|restart|rollback|diagnose|teardown\n' "$0" >&2
        exit 2
        ;;
esac
