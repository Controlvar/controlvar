#!/usr/bin/env bash
# One API fuzzing pass on this machine, with fuzz.yml's flags, filters, lists
# and verdict:
#
#   make fuzz FUZZ_SEED=<seed> SHA=<commit> PASS=<superuser|viewer|sdk>
#
# A public run prints counts only; this is where the failing operations are
# looked at. It
#
#   * refuses unless SHA is the commit checked out (HEAD) and the tree is
#     clean -- it never checks anything out itself, so the stack under test is
#     built from exactly that commit;
#   * removes whatever a killed run left of its compose project, then builds
#     and starts the API with Postgres and Redis as that project,
#     controlvar-fuzz, on its own ports (API 18700) and its own image tag
#     (experimently-api:fuzz), with the full profile and the data seed set
#     here (SEED=demo, plus sdk-contract for the sdk pass) -- never the make
#     variable -- and with tests/fuzz/compose.fuzz.yml giving the api the
#     workflow's AWS variables (every AWS call to a closed local port) and the
#     database and Redis the workflow's images;
#   * runs Schemathesis with the workflow's flags (the same filters, from
#     scripts/fuzz_check.py) in a new temporary directory outside the tree,
#     which holds its report, its output and the operations and requests
#     behind each count (details.json), and prints that directory's path;
#   * resolves which route answered each 5xx inside the api container (the
#     image and environment that answered it), as the workflow does on the
#     runner;
#   * removes the compose project, its volumes and the experimently-api:fuzz
#     image on the way out, whatever the result, and exits with the pass's
#     verdict (0 green, 1 red).
#
# How this stack differs from the workflow's: the API runs from the image,
# which installs backend/requirements/runtime.lock and modules/requirements.lock
# (the packages that ship), behind docker-compose.yml's entrypoint and
# environment; the workflow runs uvicorn on the runner from
# backend/requirements.txt and modules/requirements.txt, which leave some
# packages unpinned. Measured on the pull request that gave both stacks the
# same AWS variables and database image: on one runner, at one commit and seed
# 20261006, this and the workflow's pass found the same set of routes
# answering 5xx and the same set of unreached operations, in all three
# passes. The known-5xx and unreached lists are judged against the workflow's
# runs; a difference from them here is worth a look on the runner before it
# is read as a defect.
#
# The API key and bearer token are read into variables and never printed.
set -euo pipefail

usage="usage: make fuzz FUZZ_SEED=<seed> SHA=<commit> PASS=<superuser|viewer|sdk>"
fuzz_seed=${1-}
sha=${2-}
pass=${3-}

refuse() {
  echo "make fuzz: $1" >&2
  echo "$usage" >&2
  exit 2
}

case "$pass" in
  superuser | viewer | sdk) ;;
  *) refuse "PASS must be superuser, viewer or sdk" ;;
esac
[[ "$fuzz_seed" =~ ^(0|[1-9][0-9]{0,9})$ ]] ||
  refuse "FUZZ_SEED must be an integer of one to ten digits"
[ -n "$sha" ] || refuse "SHA must name the commit to fuzz"

root=$(git rev-parse --show-toplevel 2> /dev/null) ||
  refuse "this is not a git checkout"
cd "$root"
head=$(git rev-parse HEAD)
wanted=$(git rev-parse --verify --quiet "${sha}^{commit}") ||
  refuse "SHA $sha is not a commit of this repository"
[ "$wanted" = "$head" ] ||
  refuse "SHA $sha is not HEAD ($head); check that commit out first (make fuzz checks nothing out)"
[ -z "$(git status --porcelain)" ] ||
  refuse "the tree has changes or untracked files; commit or stash them first"

python=${FUZZ_PYTHON:-python3}
"$python" -c 'import sys; sys.exit(sys.version_info < (3, 11))' ||
  refuse "needs Python 3.11 or newer as python3 (or FUZZ_PYTHON)"

project=controlvar-fuzz
image=experimently-api:fuzz
api=http://127.0.0.1:18700
compose=(docker compose -p "$project" --env-file /dev/null
  -f docker-compose.yml -f tests/fuzz/compose.fuzz.yml)
# Every variable docker-compose.yml interpolates that matters here is set
# explicitly, so neither the developer's shell nor a make variable on the
# command line (SEED above all) reaches the stack.
export EXPERIMENTLY_PROFILE=full EXPERIMENTLY_IMAGE_TAG=fuzz
export API_HOST_PORT=18700 POSTGRES_HOST_PORT=18701 REDIS_HOST_PORT=18702 FRONTEND_HOST_PORT=18703
if [ "$pass" = sdk ]; then
  export SEED=demo,sdk-contract
else
  export SEED=demo
fi

work=$(mktemp -d "${TMPDIR:-/tmp}/controlvar-fuzz.XXXXXX")
cleanup() {
  if ! "${compose[@]}" down -v --remove-orphans > "$work/compose-down.log" 2>&1; then
    echo "make fuzz: could not remove the $project project; run: docker compose -p $project down -v" >&2
  fi
  if docker image inspect "$image" > /dev/null 2>&1 &&
    ! docker image rm "$image" > "$work/image-rm.log" 2>&1; then
    echo "make fuzz: could not remove the $image image; run: docker image rm $image" >&2
  fi
}
trap cleanup EXIT

# A run killed before its trap ran leaves the project (and its volumes)
# behind; start from nothing.
"${compose[@]}" down -v --remove-orphans > "$work/compose-preclean.log" 2>&1 || true

echo "make fuzz: building and starting the $project stack (logs in $work)"
"${compose[@]}" up -d --build --wait api > "$work/compose-up.log" 2>&1 || {
  echo "make fuzz: the stack did not start; see $work/compose-up.log" >&2
  exit 1
}
# The same refusal as the workflow's: nothing may reach AWS.
[ "$("${compose[@]}" exec -T api printenv AWS_ENDPOINT_URL)" = "http://127.0.0.1:9" ] || {
  echo "make fuzz: the api container does not have AWS_ENDPOINT_URL=http://127.0.0.1:9" >&2
  exit 1
}

# As the workflow installs it (the same script): exactly the pinned closure,
# then checked.
PYTHON="$python" scripts/check_fuzz_install.sh "$work/venv" > "$work/pip.log" 2>&1 || {
  echo "make fuzz: could not install tests/fuzz/requirements.txt; see $work/pip.log" >&2
  exit 1
}

if [ "$pass" = sdk ]; then
  key=$("${compose[@]}" exec -T api cat /data/demo/sdk-contract/.api_key_local)
  auth=(-H "X-API-Key: $key")
else
  case "$pass" in
    superuser) email=admin@demo.com ;;
    viewer) email=viewer@demo.com ;;
  esac
  token=$(curl -sf -X POST "$api/api/v1/auth/login" \
    -H 'Content-Type: application/json' \
    -d "{\"email\": \"$email\", \"password\": \"Demo1234!\"}" |
    "$python" -c 'import json, sys; print(json.load(sys.stdin)["access_token"])') || {
    echo "make fuzz: could not sign in as $email" >&2
    exit 1
  }
  auth=(-H "Authorization: Bearer $token")
fi

curl -sf "$api/api/v1/openapi.json" -o "$work/openapi.json"
"$python" scripts/fuzz_check.py args --pass "$pass" --schema "$work/openapi.json" > "$work/filters.txt"
filters=()
while IFS= read -r arg; do filters+=("$arg"); done < "$work/filters.txt"

echo "make fuzz: fuzzing the $pass pass (seed $fuzz_seed); this takes a while"
set +e
(
  cd "$work" &&
    "$work/venv/bin/schemathesis" run "$api/api/v1/openapi.json" \
      --checks not_a_server_error --phases examples,coverage,fuzzing \
      --max-examples 5 --workers 1 --rate-limit 4/s \
      --seed "$fuzz_seed" --generation-deterministic \
      --report ndjson --report-dir "$work/report" \
      "${filters[@]}" "${auth[@]}"
) > "$work/schemathesis.out" 2>&1
set -e

# Which route answered each 5xx: resolved by the application in the api
# container, from the image that served the run, with the database and Redis
# pointed at a closed port. A failure leaves no routes file, and the
# evaluation below is then red.
{
  "$python" scripts/fuzz_check.py requests --report-dir "$work/report" --out "$work/requests.json" &&
    "${compose[@]}" cp scripts/fuzz_check.py api:/tmp/fuzz_check.py &&
    "${compose[@]}" cp "$work/openapi.json" api:/tmp/fuzz_openapi.json &&
    "${compose[@]}" cp "$work/requests.json" api:/tmp/fuzz_requests.json &&
    "${compose[@]}" exec -T -w /app -e POSTGRES_PORT=1 -e REDIS_PORT=1 api \
      python /tmp/fuzz_check.py routes --app-root /app \
      --schema /tmp/fuzz_openapi.json --requests /tmp/fuzz_requests.json --out /tmp/fuzz_routes.json &&
    "${compose[@]}" cp api:/tmp/fuzz_routes.json "$work/routes.json"
} > "$work/routes.log" 2>&1 || true

verdict=0
"$python" scripts/fuzz_check.py evaluate --pass "$pass" \
  --schema "$work/openapi.json" --report-dir "$work/report" \
  --routes "$work/routes.json" \
  --seed "$fuzz_seed" --sha "$head" --details "$work/details.json" || verdict=$?
echo "make fuzz: the reproduction directory is $work"
exit "$verdict"
