#!/usr/bin/env bash
# The chart-kind job (.github/workflows/chart-kind.yml): the Helm chart
# installed on a kind cluster, behind ingress-nginx, from this pull request's
# own images, by the blocks of docs/self-hosting/kubernetes.md.
#
# One phase per subcommand, run in this order by the workflow:
#
#   cluster    kind cluster with ports 80/443 on the host, and ingress-nginx
#              from a manifest pinned by sha256
#   load       the PR's images, tagged as the chart's defaults, loaded into kind
#   dry-run    the rendered chart accepted by the real API server (and its
#              admission webhooks); an IP-literal or host:port Ingress host
#              refused by it -- the refusal the chart's schema mirrors
#   guide      the guide's blocks, extracted from the page (scripts/guide_blocks.py):
#                secrets twice (the second fails AlreadyExists, the Secret's
#                  resourceVersion unchanged), install, the pods run the PR's
#                  images, a request through the Ingress answers 401 (400 is
#                  the Host check refusing publicBaseUrl's host), first admin,
#                  helm test, migrations ran once, upgrade, no restarts
#   replicas   a fresh install with api.replicaCount=2: exactly one bootstrap
#              says "(created)", the other "(upgraded)", no restarts
#   upgrade-next
#              N -> N': that release upgraded to this PR's image plus one
#              more core revision (made here, never committed): exactly one
#              `migrate` logs `Running upgrade`, and every old pod stays Ready
#              and answers /health/ready until it is being deleted
#   previous   resolve N-1, the newest release at or below VERSION whose
#              images are published (falling back one release while the
#              newest one's are still publishing), or PREVIOUS_VERSION; writes
#              version= to $GITHUB_OUTPUT. It runs the same way on every
#              event, pull requests from forks included
#   upgrade-previous
#              N-1 -> N: the N-1 release's chart and images (pulled from GHCR
#              anonymously, with no credentials) upgraded to this PR's chart
#              and images; the same Ready
#              assertion, and it prints whether the delta has a migration
#   summary    the per-phase timings (also to $GITHUB_STEP_SUMMARY)
#   diagnose   pods, events and API logs, for a failed run
#   teardown   delete the cluster
#
# Inputs (environment): PROFILE (core|full); API_IMAGE and WEB_IMAGE, the local
# images to load; INGRESS_NGINX_MANIFEST_URL and INGRESS_NGINX_MANIFEST_SHA256;
# KIND_NODE_IMAGE; optional KIND_CLUSTER, KIND_HOST (must resolve to
# 127.0.0.1), CHART_KIND_WORK. For `previous`: optional PREVIOUS_VERSION. For
# `upgrade-previous`: PREVIOUS_VERSION.
set -euo pipefail

cd "$(dirname "$0")/.."
REPO_ROOT=$(pwd)

PROFILE=${PROFILE:?PROFILE must be core or full}
CLUSTER=${KIND_CLUSTER:-chart-kind}
HOST=${KIND_HOST:-controlvar.kind.test}
WORK=${CHART_KIND_WORK:-${RUNNER_TEMP:-/tmp}/chart-kind-$PROFILE}
GUIDE=docs/self-hosting/kubernetes.md
GUIDE_BLOCKS=secrets,install,first-admin,upgrade
CHART_DIR=charts/experimently
# The guide's own names: its blocks hard-code them.
NS=experimently
RELEASE=experimently
SECRET=experimently-secrets
ADMIN=admin@example.com
DRY_RUN_NS=chart-kind-dry-run
API_REPO=ghcr.io/controlvar/controlvar
WEB_REPO=ghcr.io/controlvar/controlvar-web

mkdir -p "$WORK"
TIMINGS="$WORK/timings.tsv"

say() { printf '[chart-kind] %s\n' "$*"; }
fail() {
    printf '::error::%s\n' "$*"
    exit 1
}

# phase NAME COMMAND...: run a phase and record how long it took.
phase() {
    local name=$1 start end
    shift
    start=$(date +%s)
    say "== $name"
    "$@"
    end=$(date +%s)
    printf '%s\t%s\n' "$name" "$((end - start))" >>"$TIMINGS"
    say "== $name: $((end - start))s"
}

app_version() {
    sed -n 's/^appVersion: "\([^"]*\)".*/\1/p' "$CHART_DIR/Chart.yaml"
}

sha256_of() {
    if command -v sha256sum >/dev/null 2>&1; then
        sha256sum "$1" | cut -d' ' -f1
    else
        shasum -a 256 "$1" | cut -d' ' -f1
    fi
}

# ---------------------------------------------------------------------------
do_cluster() {
    : "${KIND_NODE_IMAGE:?}" "${INGRESS_NGINX_MANIFEST_URL:?}" "${INGRESS_NGINX_MANIFEST_SHA256:?}"
    cat >"$WORK/kind.yaml" <<EOF
kind: Cluster
apiVersion: kind.x-k8s.io/v1alpha4
nodes:
  - role: control-plane
    image: $KIND_NODE_IMAGE
    kubeadmConfigPatches:
      - |
        kind: InitConfiguration
        nodeRegistration:
          kubeletExtraArgs:
            node-labels: "ingress-ready=true"
    extraPortMappings:
      - containerPort: 80
        hostPort: 80
        protocol: TCP
      - containerPort: 443
        hostPort: 443
        protocol: TCP
EOF
    kind create cluster --name "$CLUSTER" --config "$WORK/kind.yaml" --wait 180s
    kubectl get nodes -o wide

    curl -fsSL -o "$WORK/ingress-nginx.yaml" "$INGRESS_NGINX_MANIFEST_URL"
    local got
    got=$(sha256_of "$WORK/ingress-nginx.yaml")
    [ "$got" = "$INGRESS_NGINX_MANIFEST_SHA256" ] ||
        fail "ingress-nginx manifest sha256 $got, pinned $INGRESS_NGINX_MANIFEST_SHA256"
    kubectl apply -f "$WORK/ingress-nginx.yaml"
    kubectl wait --namespace ingress-nginx --for=condition=ready pod \
        --selector=app.kubernetes.io/component=controller --timeout=300s
}

# ---------------------------------------------------------------------------
do_load() {
    : "${API_IMAGE:?}" "${WEB_IMAGE:?}"
    local tag
    tag="$PROFILE-$(app_version)"
    # The chart's default references, so the guide's install command needs no
    # image override. pullPolicy IfNotPresent then uses these; the guide phase
    # asserts the running pods are these images, not a registry's.
    docker tag "$API_IMAGE" "$API_REPO:$tag"
    docker tag "$WEB_IMAGE" "$WEB_REPO:$tag"
    kind load docker-image --name "$CLUSTER" "$API_REPO:$tag" "$WEB_REPO:$tag"
    # Assigned first: `echo "x=$(cmd)"` succeeds when cmd fails.
    local api web
    api=$(docker image inspect --format '{{.Id}}' "$API_REPO:$tag")
    web=$(docker image inspect --format '{{.Id}}' "$WEB_REPO:$tag")
    : "${api:?no image id for $API_REPO:$tag}" "${web:?no image id for $WEB_REPO:$tag}"
    printf 'api=%s\nweb=%s\napi_repo=%s\nweb_repo=%s\n' "$api" "$web" "$API_REPO" "$WEB_REPO" >"$WORK/images.txt"
    cat "$WORK/images.txt"
}

# ---------------------------------------------------------------------------
# kubectl apply --dry-run=server, retried while ingress-nginx's admission
# webhook is not answering yet (it becomes Ready before its endpoint does).
server_dry_run() {
    local file=$1 out=$2 attempt
    for attempt in $(seq 1 30); do
        if kubectl apply --dry-run=server -n "$DRY_RUN_NS" -f "$file" >"$out" 2>&1; then
            return 0
        fi
        if grep -q 'failed calling webhook' "$out"; then
            sleep 2
            continue
        fi
        return 1
    done
    return 1
}

do_dry_run() {
    local values
    # A Pod is admitted only if its ServiceAccount exists, so that one object
    # is created for real, in a namespace of its own.
    kubectl create namespace "$DRY_RUN_NS"
    helm template exp "$CHART_DIR" -f "$CHART_DIR/ci/values-core.yaml" \
        --show-only templates/serviceaccount.yaml | kubectl apply -n "$DRY_RUN_NS" -f -
    for values in "$CHART_DIR/ci/values-core.yaml" "$CHART_DIR/ci/values-full.yaml"; do
        helm template exp "$CHART_DIR" -f "$values" >"$WORK/render.yaml"
        server_dry_run "$WORK/render.yaml" "$WORK/dry-run.log" ||
            { cat "$WORK/dry-run.log"; fail "the API server refused the chart rendered with $values"; }
        say "$values: accepted by the API server ($(grep -c 'server dry run' "$WORK/dry-run.log") objects)"
    done

    # The chart refuses these publicBaseUrl hosts before Kubernetes sees them;
    # this is the evidence that Kubernetes would refuse them too.
    helm template exp "$CHART_DIR" -f "$CHART_DIR/ci/values-core.yaml" \
        --show-only templates/ingress.yaml >"$WORK/ingress.yaml"
    local bad expect
    for bad in '10.0.0.5|must be a DNS name, not an IP address' \
        'controlvar.example.com:8080|a lowercase RFC 1123 subdomain'; do
        expect=${bad#*|}
        bad=${bad%%|*}
        sed "s/host: \"controlvar.example.com\"/host: \"$bad\"/" "$WORK/ingress.yaml" >"$WORK/bad-ingress.yaml"
        grep -qF "host: \"$bad\"" "$WORK/bad-ingress.yaml" ||
            fail "could not plant host $bad in the rendered Ingress"
        if server_dry_run "$WORK/bad-ingress.yaml" "$WORK/bad.log"; then
            fail "the API server accepted an Ingress host of $bad"
        fi
        grep -qF "$expect" "$WORK/bad.log" || { cat "$WORK/bad.log"; fail "host $bad refused, but not for: $expect"; }
        say "Ingress host $bad: refused by the API server ($expect)"
    done
    kubectl delete namespace "$DRY_RUN_NS" --wait=false
}

# ---------------------------------------------------------------------------
# run_block NAME: one of the guide's blocks, as a reader runs it, in a shell
# with the guide's four variables. stdout and stderr go to $WORK/NAME.{out,err}.
run_block() {
    local name=$1 status=0
    say "--- guide block '$name':"
    sed 's/^/    | /' "$WORK/blocks/$name.sh"
    CHART="$REPO_ROOT/$CHART_DIR" PUBLIC_BASE_URL="http://$HOST" ADMIN_EMAIL="$ADMIN" PROFILE="$PROFILE" \
        bash -euo pipefail "$WORK/blocks/$name.sh" >"$WORK/$name.out" 2>"$WORK/$name.err" || status=$?
    sed 's/^/    out: /' "$WORK/$name.out"
    sed 's/^/    err: /' "$WORK/$name.err"
    say "--- '$name' exited $status"
    return "$status"
}

# assert_images NS [IMAGES]: the pods running our images run exactly the
# images this job loaded (IMAGES, default $WORK/images.txt: api=ID, web=ID,
# and the repositories they were loaded under, api_repo= and web_repo=; N-1's
# are the ones its own chart names, which need not be this checkout's).
assert_images() {
    local ns=$1 images=${2:-$WORK/images.txt} api web api_repo web_repo ids
    api=$(sed -n 's/^api=//p' "$images")
    web=$(sed -n 's/^web=//p' "$images")
    api_repo=$(sed -n 's/^api_repo=//p' "$images")
    web_repo=$(sed -n 's/^web_repo=//p' "$images")
    : "${api:?no api image id in $images}" "${web:?no web image id in $images}"
    : "${api_repo:?no api repository in $images}" "${web_repo:?no web repository in $images}"
    ids=$(kubectl get pods -n "$ns" -l "app.kubernetes.io/component in (api,web)" \
        -o jsonpath='{range .items[*]}{range .status.initContainerStatuses[*]}{.image}{" "}{.imageID}{"\n"}{end}{range .status.containerStatuses[*]}{.image}{" "}{.imageID}{"\n"}{end}{end}')
    printf '%s\n' "$ids"
    [ -n "$ids" ] || fail "no api or web containers in $ns"
    local image id want
    while read -r image id; do
        [ -n "$image" ] || continue
        case "$image" in
            "$web_repo":* | */"$web_repo":*) want=$web ;;
            "$api_repo":* | */"$api_repo":*) want=$api ;;
            *) fail "unexpected image $image in $ns" ;;
        esac
        [ "$id" = "$want" ] ||
            fail "$image runs $id, not the image this job built ($want)"
    done <<<"$ids"
    say "$ns: every api/web container runs this job's images"
}

# assert_migrations NS RELEASE PODS CREATED UPGRADED: the bootstrap ran once
# per pod, in its `migrate` init container, never in the `api` container.
assert_migrations() {
    local ns=$1 release=$2 want_pods=$3 want_created=$4 want_upgraded=$5
    local pods pod n=0 created=0 upgraded=0 in_api=0 not_skipped=0 c
    pods=$(kubectl get pods -n "$ns" \
        -l "app.kubernetes.io/component=api,app.kubernetes.io/instance=$release" \
        -o jsonpath='{.items[*].metadata.name}')
    for pod in $pods; do
        n=$((n + 1))
        # A bootstrap that failed and was retried leaves only the retry's log
        # behind, so the counts below cannot see it; its restart can.
        c=$(kubectl get pod -n "$ns" "$pod" \
            -o jsonpath='{.status.initContainerStatuses[?(@.name=="migrate")].restartCount}')
        if [ "${c:-0}" -ne 0 ]; then
            kubectl logs -n "$ns" "$pod" -c migrate --previous --tail=40 || true
            fail "$pod: the migrate container restarted $c time(s): a bootstrap failed and was retried"
        fi
        kubectl logs -n "$ns" "$pod" -c migrate >"$WORK/$pod.migrate.log"
        kubectl logs -n "$ns" "$pod" -c api >"$WORK/$pod.api.log"
        c=$(grep -c 'Database bootstrap complete (created)' "$WORK/$pod.migrate.log" || true)
        created=$((created + c))
        c=$(grep -c 'Database bootstrap complete (upgraded)' "$WORK/$pod.migrate.log" || true)
        upgraded=$((upgraded + c))
        c=$(grep -c 'Database bootstrap complete' "$WORK/$pod.api.log" || true)
        in_api=$((in_api + c))
        grep 'Database bootstrap complete\|skipping bootstrap' "$WORK/$pod.migrate.log" "$WORK/$pod.api.log" || true
        grep -qF 'RUN_MIGRATIONS=false: skipping bootstrap' "$WORK/$pod.api.log" ||
            not_skipped=$((not_skipped + 1))
    done
    say "$ns: $n api pods; migrate: $created created, $upgraded upgraded; api container: $in_api"
    [ "$n" -eq "$want_pods" ] || fail "$ns: $n api pods, expected $want_pods"
    [ "$in_api" -eq 0 ] || fail "$ns: the bootstrap ran in the api container $in_api time(s); migrations must run only in migrate"
    [ "$not_skipped" -eq 0 ] || fail "$ns: $not_skipped api container(s) did not log that they skipped the bootstrap"
    [ "$created" -eq "$want_created" ] || fail "$ns: $created bootstraps said (created), expected exactly $want_created"
    [ "$upgraded" -eq "$want_upgraded" ] || fail "$ns: $upgraded bootstraps said (upgraded), expected $want_upgraded"
}

assert_no_restarts() {
    local ns=$1 counts restarts
    kubectl get pods -n "$ns" -o wide
    counts=$(kubectl get pods -n "$ns" \
        -o jsonpath='{range .items[*]}{range .status.initContainerStatuses[*]}{.restartCount}{"\n"}{end}{range .status.containerStatuses[*]}{.restartCount}{"\n"}{end}{end}')
    # An empty namespace sums to 0 too; that is not "no restarts".
    [ -n "$counts" ] || fail "$ns: no pods or container statuses to check for restarts"
    restarts=$(printf '%s\n' "$counts" | awk '{s += $1} END {print s + 0}')
    [ "$restarts" -eq 0 ] || fail "$ns: $restarts container restart(s)"
    say "$ns: 0 restarts in $(printf '%s\n' "$counts" | grep -c .) containers"
}

# wait_routed URL CODE: wait until the Ingress answers URL with CODE five
# times in a row, half a second apart; the last status is left in ROUTED_CODE.
#
# `helm install --wait` returns when the pods are Ready; ingress-nginx picks up
# their endpoints a few seconds later and answers 503 until then. And one
# answer is not enough: each nginx worker takes the new endpoints on its own
# timer (about once a second), so for up to a second one request can reach the
# API and the next a worker that still answers 503 -- a 401 followed 70 ms later
# by a 503 is how a run failed. Two and a half seconds of the same answer spans
# that timer. Only "not routed yet" (no answer, 404, 502, 503, 504) is waited
# out; any other status ends the wait at once and is the caller's to judge.
ROUTED_CODE=
wait_routed() {
    local url=$1 want=$2 streak=0 attempt code
    for attempt in $(seq 1 180); do
        code=$(curl -s -o "$WORK/ingress.body" -w '%{http_code}' "$url" || true)
        if [ "$code" = "$want" ]; then
            streak=$((streak + 1))
            [ "$streak" -lt 5 ] || break
        else
            streak=0
            case "$code" in
                000 | 404 | 502 | 503 | 504) ;;
                *) break ;;
            esac
        fi
        sleep 0.5
    done
    say "through the Ingress, $url: $code after $attempt request(s), the last $streak in a row"
    if [ "$code" = "$want" ] && [ "$streak" -lt 5 ]; then
        code="$code (not stable: $streak in a row)"
    fi
    ROUTED_CODE=$code
}

secret_state() {
    kubectl get secret "$SECRET" -n "$NS" -o jsonpath='{.metadata.resourceVersion} {.data}' | sha256_of /dev/stdin
}

do_guide() {
    python3 scripts/guide_blocks.py "$GUIDE" --expect "$GUIDE_BLOCKS" --out "$WORK/blocks"

    run_block secrets || fail "the guide's secrets block failed"
    local rv before after
    rv=$(kubectl get secret "$SECRET" -n "$NS" -o jsonpath='{.metadata.resourceVersion}')
    before=$(secret_state)
    if run_block secrets; then
        fail "the guide's secrets block succeeded a second time: it must create, never replace"
    fi
    grep -qF "secrets \"$SECRET\" already exists" "$WORK/secrets.err" ||
        fail "the second secrets run failed, but not with AlreadyExists"
    after=$(secret_state)
    [ "$before" = "$after" ] || fail "the second secrets run changed the Secret"
    say "secrets block run twice: AlreadyExists, resourceVersion $rv unchanged"

    run_block install || fail "the guide's install block failed"
    assert_images "$NS"

    local code
    wait_routed "http://$HOST/api/v1/experiments/" 401
    code=$ROUTED_CODE
    case "$code" in
        401) say "through the Ingress, http://$HOST/api/v1/experiments/: 401" ;;
        400)
            cat "$WORK/ingress.body"; echo
            fail "through the Ingress the API refused the Host (400): its ALLOWED_HOSTS does not include $HOST, publicBaseUrl's host"
            ;;
        *)
            cat "$WORK/ingress.body"; echo
            fail "through the Ingress, http://$HOST/api/v1/experiments/: $code, expected 401"
            ;;
    esac
    wait_routed "http://$HOST/" 200
    code=$ROUTED_CODE
    [ "$code" = 200 ] || fail "through the Ingress, the dashboard http://$HOST/: $code, expected 200"
    say "through the Ingress, the dashboard http://$HOST/: 200"

    run_block first-admin || fail "the guide's first-admin block failed"
    [ "$(cat "$WORK/first-admin.out")" = $'401\nADMIN' ] ||
        fail "the first-admin block printed something other than 401 then ADMIN"

    helm test "$RELEASE" -n "$NS" --logs
    assert_migrations "$NS" "$RELEASE" 1 1 0

    run_block upgrade || fail "the guide's upgrade block failed"
    local revision
    revision=$(helm history "$RELEASE" -n "$NS" --max 1 -o json | python3 -c 'import json,sys; r=json.load(sys.stdin)[-1]; print(r["revision"], r["status"])')
    [ "$revision" = "2 deployed" ] || fail "after the upgrade block: revision/status $revision, expected 2 deployed"
    say "upgrade: revision 2 deployed"
    assert_migrations "$NS" "$RELEASE" 1 1 0
    assert_no_restarts "$NS"

    # Free the node for the next phase.
    helm uninstall "$RELEASE" -n "$NS" --wait
    kubectl delete namespace "$NS" --wait=true
}

# ---------------------------------------------------------------------------
do_replicas() {
    local ns=chart-kind-replicas release=r2
    kubectl create namespace "$ns"
    helm install "$release" "$CHART_DIR" -n "$ns" \
        --set profile="$PROFILE" \
        --set publicBaseUrl="http://replicas.$HOST" \
        --set firstSuperuser="$ADMIN" \
        --set ingress.enabled=false \
        --set api.replicaCount=2 \
        --set secrets.secretKey="$(openssl rand -hex 32)" \
        --set secrets.firstSuperuserPassword="$(openssl rand -hex 16)" \
        --set secrets.postgresUser=experimently \
        --set secrets.postgresPassword="$(openssl rand -hex 32)" \
        --set secrets.auditHmacKey="$(openssl rand -hex 32)" \
        --wait --timeout 10m
    assert_images "$ns"
    assert_migrations "$ns" "$release" 2 1 1
    assert_no_restarts "$ns"
}

# ---------------------------------------------------------------------------
# The upgrade legs. Each upgrades a running two-replica release with
# `helm upgrade --wait` while a sampler watches the old api pods, then checks
# which `migrate` container applied which revisions.

# revisions IMAGE: every alembic revision id the image carries, core and
# modules, sorted, one per line. Read from the files, not by loading alembic.
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

# applied_revisions NS RELEASE OUT: what each api pod's `migrate` logged as
# `Running upgrade A -> B, ...`; OUT gets one "POD B" line per revision.
applied_revisions() {
    local ns=$1 release=$2 out=$3 pod
    : >"$out"
    for pod in $(kubectl get pods -n "$ns" \
        -l "app.kubernetes.io/component=api,app.kubernetes.io/instance=$release" \
        -o jsonpath='{.items[*].metadata.name}'); do
        kubectl logs -n "$ns" "$pod" -c migrate >"$WORK/$pod.migrate.log"
        grep 'Running upgrade' "$WORK/$pod.migrate.log" || true
        sed -n "s/.*Running upgrade .* -> \([^ ,]*\).*/$pod \1/p" "$WORK/$pod.migrate.log" >>"$out"
    done
}

# sample_surge NS RELEASE OLD_PODS: prints "SERVING BAD NEW MIGRATED" --
# old pods (not being deleted) that are Ready and answer /health/ready with
# 200; old pods (not being deleted) that are not; new pods; new pods whose
# `migrate` has exited 0. Returns 3, naming the pod in $WORK/migrate-failed,
# when a new pod's `migrate` exited non-zero.
sample_surge() {
    local ns=$1 release=$2 old=" $3 " json name deleting ready migrate_ok code gone
    local serving=0 bad=0 new=0 migrated=0
    json=$(kubectl get pods -n "$ns" \
        -l "app.kubernetes.io/component=api,app.kubernetes.io/instance=$release" -o json)
    while IFS=$'\t' read -r name deleting ready migrate_ok; do
        [ -n "$name" ] || continue
        case "$old" in
            *" $name "*) ;;
            *)
                new=$((new + 1))
                case "$migrate_ok" in
                    0) migrated=$((migrated + 1)) ;;
                    '' | none) ;;
                    *)
                        printf '%s exited %s\n' "$name" "$migrate_ok" >"$WORK/migrate-failed"
                        return 3
                        ;;
                esac
                continue
                ;;
        esac
        [ "$deleting" = false ] || continue
        code=$(kubectl exec -n "$ns" "$name" -c api -- \
            curl -s -o /dev/null -w '%{http_code}' --max-time 5 http://localhost:8000/health/ready 2>/dev/null || true)
        if [ "$ready" = True ] && [ "$code" = 200 ]; then
            serving=$((serving + 1))
            continue
        fi
        # Deleted, or being deleted, since the listing: that is the rollout.
        gone=$(kubectl get pod -n "$ns" "$name" --ignore-not-found \
            -o jsonpath='{.metadata.name}/{.metadata.deletionTimestamp}')
        case "$gone" in
            '' | */?*) continue ;;
        esac
        bad=$((bad + 1))
        say "old pod $name: Ready=$ready, /health/ready answered '$code'" >&2
    done < <(jq -r '.items[] | [
            .metadata.name,
            (.metadata.deletionTimestamp != null),
            ([(.status.conditions // [])[] | select(.type == "Ready") | .status][0] // "Unknown"),
            ([.status.initContainerStatuses[]? | select(.name == "migrate")
                | (.state.terminated.exitCode // .lastState.terminated.exitCode)][0] // "none")
        ] | @tsv' <<<"$json")
    printf '%s %s %s %s\n' "$serving" "$bad" "$new" "$migrated"
}

# upgrade_watched NS RELEASE HELM_UPGRADE_ARGS...: `helm upgrade` in the
# background, sampled every second until it returns. Fails when an old pod
# stops being Ready or answering /health/ready before it is deleted, when a
# new pod's `migrate` fails (at once, not at helm's timeout), when no sample
# saw an old pod serving after a new pod had migrated -- the moment the leg
# exists to cover -- or when helm fails. Waits for the old pods to be gone.
upgrade_watched() {
    local ns=$1 release=$2 old helm_pid status=0 sample samples line
    shift 2
    old=$(kubectl get pods -n "$ns" \
        -l "app.kubernetes.io/component=api,app.kubernetes.io/instance=$release" \
        -o jsonpath='{.items[*].metadata.name}')
    [ -n "$old" ] || fail "$ns: no api pods before the upgrade"
    say "$ns: old api pods: $old"
    samples="$WORK/$release.surge"
    : >"$samples"
    rm -f "$WORK/migrate-failed"
    helm upgrade "$release" "$@" >"$WORK/$release.upgrade.log" 2>&1 &
    helm_pid=$!
    while kill -0 "$helm_pid" 2>/dev/null; do
        sample=$(sample_surge "$ns" "$release" "$old") || status=$?
        if [ "$status" -eq 3 ]; then
            kill "$helm_pid" 2>/dev/null || true
            wait "$helm_pid" 2>/dev/null || true
            line=$(cat "$WORK/migrate-failed")
            kubectl logs -n "$ns" "${line%% *}" -c migrate --tail=60 || true
            kubectl logs -n "$ns" "${line%% *}" -c migrate --previous --tail=60 2>/dev/null || true
            fail "$ns: the upgrade's migrate failed ($line)"
        fi
        [ "$status" -eq 0 ] || fail "$ns: sampling the surge failed ($status)"
        printf '%s\n' "$sample" >>"$samples"
        sleep 1
    done
    wait "$helm_pid" || status=$?
    cat "$WORK/$release.upgrade.log"
    [ "$status" -eq 0 ] || fail "$ns: helm upgrade $release exited $status"

    local n bad overlap
    n=$(grep -c . "$samples" || true)
    bad=$(awk '$2 > 0' "$samples" | grep -c . || true)
    overlap=$(awk '$1 > 0 && $4 > 0' "$samples" | grep -c . || true)
    say "$ns: $n samples during the upgrade; $bad with an old pod not serving; $overlap with an old pod serving after a new pod migrated"
    [ "$bad" -eq 0 ] || fail "$ns: an old api pod stopped serving before it was deleted"
    [ "$overlap" -gt 0 ] ||
        fail "$ns: no sample saw an old pod serving after a new pod had migrated; the surge was not observed"

    local pod
    for pod in $old; do
        kubectl wait -n "$ns" --for=delete "pod/$pod" --timeout=120s
    done
}

# ---------------------------------------------------------------------------
# N -> N': the release the replicas phase installed (N, two api pods), upgraded
# to this PR's image plus one more core revision. The revision is made here, in
# a copy of the image, and never committed; its down_revision is the image's
# own core head.
NEXT_REVISION=zz_next_release_0001

do_upgrade_next() {
    local ns=chart-kind-replicas release=r2 tag next core_head cid
    tag="$PROFILE-$(app_version)"
    next="$tag-next"
    core_head=$(docker run --rm --entrypoint python "$API_REPO:$tag" -c '
from alembic.config import Config
from alembic.script import ScriptDirectory

config = Config()
config.set_main_option("script_location", "backend/app/db/migrations")
config.set_main_option("version_locations", "backend/app/db/migrations/versions")
(head,) = ScriptDirectory.from_config(config).get_heads()
print(head)
')
    : "${core_head:?no core head in $API_REPO:$tag}"
    say "N' = $API_REPO:$tag plus $NEXT_REVISION (down_revision $core_head)"
    cat >"$WORK/$NEXT_REVISION.py" <<EOF
"""A migration the next release carries (chart-kind fixture, never committed).

Revision ID: $NEXT_REVISION
Revises: $core_head
"""

import os

import sqlalchemy as sa
from alembic import op

revision = "$NEXT_REVISION"
down_revision = "$core_head"
branch_labels = None
depends_on = None

SCHEMA = os.environ.get("POSTGRES_SCHEMA", "experimentation")


def upgrade() -> None:
    op.create_table(
        "next_release_probe",
        sa.Column("id", sa.Integer(), primary_key=True),
        schema=SCHEMA,
    )


def downgrade() -> None:
    op.drop_table("next_release_probe", schema=SCHEMA)
EOF
    chmod 0644 "$WORK/$NEXT_REVISION.py"
    # No build: the docker-container builder cannot see a local image.
    cid=$(docker create "$API_REPO:$tag")
    docker cp "$WORK/$NEXT_REVISION.py" "$cid:/app/backend/app/db/migrations/versions/$NEXT_REVISION.py"
    docker commit "$cid" "$API_REPO:$next" >/dev/null
    docker rm "$cid" >/dev/null
    docker tag "$WEB_REPO:$tag" "$WEB_REPO:$next"
    kind load docker-image --name "$CLUSTER" "$API_REPO:$next" "$WEB_REPO:$next"
    local api web
    api=$(docker image inspect --format '{{.Id}}' "$API_REPO:$next")
    web=$(docker image inspect --format '{{.Id}}' "$WEB_REPO:$next")
    printf 'api=%s\nweb=%s\napi_repo=%s\nweb_repo=%s\n' "$api" "$web" "$API_REPO" "$WEB_REPO" >"$WORK/images-next.txt"
    [ "$(revisions "$API_REPO:$next" | grep -cx "$NEXT_REVISION")" = 1 ] ||
        fail "$API_REPO:$next does not carry $NEXT_REVISION"

    upgrade_watched "$ns" "$release" "$CHART_DIR" -n "$ns" \
        --reuse-values --set image.tag="$next" --wait --timeout 8m
    assert_images "$ns" "$WORK/images-next.txt"

    applied_revisions "$ns" "$release" "$WORK/next.applied"
    say "N -> N': applied: $(tr '\n' ';' <"$WORK/next.applied")"
    [ "$(grep -c . "$WORK/next.applied" || true)" = 1 ] ||
        fail "N -> N': $(grep -c . "$WORK/next.applied" || true) 'Running upgrade' lines across the migrate containers, expected exactly 1"
    grep -q " $NEXT_REVISION\$" "$WORK/next.applied" ||
        fail "N -> N': the one 'Running upgrade' was not to $NEXT_REVISION"
    assert_no_restarts "$ns"
    say "N -> N': exactly one migrate applied $NEXT_REVISION; the old pods served until deleted"

    helm uninstall "$release" -n "$ns" --wait
    kubectl delete namespace "$ns" --wait=true
}

# ---------------------------------------------------------------------------
# N-1: the newest release at or below VERSION, whose images are public on
# GHCR.
#
# The leg runs on every event -- pull requests from forks, this repository's
# pull requests, main, the nightly run, a manual run -- and is never skipped.
# An N-1 that cannot be resolved, or whose images cannot be pulled
# anonymously, fails the job.
#
# Right after a release its tag exists minutes before release.yml has
# published its images (and the bot-pushed tag needs a manual dispatch to
# start that run). So when the newest tag's images for this profile are not
# on GHCR yet, N-1 is the release before it, and the job summary says so. The
# fallback is one step and no further: if that release's images cannot be
# read either, the cause is not the publishing window (a package no longer
# public reads as "manifest unknown" too), and the job fails.
do_previous() {
    local version prev tags candidates newest older note
    version=$(cat VERSION)
    tags=$(git ls-remote --tags --refs origin 'v*' | sed -n 's#.*refs/tags/v\([0-9][0-9]*\.[0-9][0-9]*\.[0-9][0-9]*\)$#\1#p')
    [ -n "$tags" ] || fail "N-1 cannot be resolved: no vX.Y.Z tags on origin"
    if [ -n "${PREVIOUS_VERSION:-}" ]; then
        prev=$PREVIOUS_VERSION
        printf '%s\n' "$prev" | grep -Eqx '[0-9]+\.[0-9]+\.[0-9]+' ||
            fail "N-1 cannot be resolved: PREVIOUS_VERSION '$prev' is not X.Y.Z"
        printf '%s\n' "$tags" | grep -Fqx "$prev" ||
            fail "N-1 cannot be resolved: there is no release tag v$prev"
        # Asked for by name: no fallback.
        published "$prev" ||
            fail "N-1 cannot be resolved: PREVIOUS_VERSION $prev has no published $PROFILE images. $PULL_FAILED"
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
            note="N-1 is $older, not $newest: v$newest is tagged but its $PROFILE images are not published yet (release.yml publishes them after the tag), so the upgrade leg starts from the release before it."
            printf '::warning::%s\n' "$note"
            [ -z "${GITHUB_STEP_SUMMARY:-}" ] ||
                printf '### chart-kind (%s): N-1\n\n%s\n\n' "$PROFILE" "$note" >>"$GITHUB_STEP_SUMMARY"
        fi
    fi
    say "N-1 = $prev (VERSION $version)"
    [ -z "${GITHUB_OUTPUT:-}" ] || printf 'version=%s\n' "$prev" >>"$GITHUB_OUTPUT"
}

# values_repo VALUES COMPONENT: image.COMPONENT.repository (api or web) from a
# chart's values.yaml, read by indentation: the chart's own two-space layout.
values_repo() {
    awk -v want="$2" '
        /^[A-Za-z]/ { top = $0; sub(/:.*/, "", top); mid = ""; next }
        /^  [A-Za-z]/ { mid = $0; sub(/^ */, "", mid); sub(/:.*/, "", mid); next }
        top == "image" && mid == want && /^    repository:/ {
            r = $0
            sub(/^    repository:[ ]*/, "", r)
            sub(/[ ]+#.*$/, "", r)
            gsub(/["\047]/, "", r)
            print r
            exit
        }
    ' "$1"
}

# tag_images VERSION: the chart release vVERSION ships and the image
# repositories that chart names, read from that tag's own tree (extracted under
# $WORK/tags/vVERSION), never from this checkout. Sets TAG_CHART (the chart
# directory), TAG_API_REPO and TAG_WEB_REPO. A release is resolved, pulled and
# installed under its own names, so a later change to this checkout's chart
# directory or image repositories leaves N-1 resolvable.
tag_images() {
    local v=$1 dest charts
    dest="$WORK/tags/v$v"
    if [ ! -d "$dest/charts" ]; then
        git fetch --quiet --no-tags --depth 1 origin "+refs/tags/v$v:refs/tags/v$v" ||
            fail "N-1 cannot be resolved: tag v$v could not be fetched"
        rm -rf "$dest"
        mkdir -p "$dest"
        git archive "v$v" charts | tar -x -C "$dest" ||
            fail "N-1 cannot be resolved: v$v has no charts/"
    fi
    charts=$(ls -d "$dest"/charts/*/Chart.yaml 2>/dev/null || true)
    [ "$(printf '%s\n' "$charts" | grep -c .)" = 1 ] ||
        fail "N-1 cannot be resolved: v$v ships $(printf '%s\n' "$charts" | grep -c .) charts/*/Chart.yaml, expected exactly 1"
    TAG_CHART=$(dirname "$charts")
    TAG_API_REPO=$(values_repo "$TAG_CHART/values.yaml" api)
    TAG_WEB_REPO=$(values_repo "$TAG_CHART/values.yaml" web)
    [ "$(printf '%s\n%s\n' "$TAG_API_REPO" "$TAG_WEB_REPO" | grep -Ecx '[a-z0-9][a-z0-9._/-]*')" = 2 ] ||
        fail "N-1 cannot be resolved: v$v's values.yaml names no image.api/image.web repository (read [$TAG_API_REPO] [$TAG_WEB_REPO])"
}

# published VERSION: both of this profile's images for VERSION, under the
# repositories vVERSION's own chart names, can be read from GHCR anonymously --
# a manifest read with an empty client configuration, so no stored credential
# is sent and nothing is pulled.
published() {
    local v=$1 anon ref rc=0
    tag_images "$v"
    anon=$(mktemp -d "${TMPDIR:-/tmp}/chart-kind-anon.XXXXXX")
    printf '{}\n' >"$anon/config.json"
    for ref in "$TAG_API_REPO:$PROFILE-$v" "$TAG_WEB_REPO:$PROFILE-$v"; do
        if ! DOCKER_CONFIG=$anon docker manifest inspect "$ref" >/dev/null 2>"$anon/err"; then
            say "not published (or not readable anonymously): $ref: $(head -c 200 "$anon/err" | tr '\n' ' ')"
            rc=1
            break
        fi
    done
    rm -rf "$anon"
    return "$rc"
}

# pull_anonymously REF: docker pull REF with an empty client configuration, so
# no stored credential is sent. The images are public, and a pull request from
# a fork has no token to send: a pull that works here works there. A package
# that stops being public fails this on every event, never only on forks.
PULL_FAILED="The previous release image could not be pulled anonymously: is the package still public?"

pull_anonymously() {
    local ref=$1 anon
    anon=$(mktemp -d "${TMPDIR:-/tmp}/chart-kind-anon.XXXXXX")
    printf '{}\n' >"$anon/config.json"
    if ! DOCKER_CONFIG=$anon docker pull "$ref"; then
        rm -rf "$anon"
        fail "$PULL_FAILED (docker pull $ref failed)"
    fi
    rm -rf "$anon"
}

do_upgrade_previous() {
    local prev=${PREVIOUS_VERSION:?PREVIOUS_VERSION: run the previous phase first}
    local ns=chart-kind-previous release=prev ptag ltag ref this_api papi pweb pchart
    ptag="$PROFILE-$prev"
    # This PR's api image, by the ID `load` recorded, read BEFORE the pull
    # below. When N-1 is the release VERSION names (the usual case), $ptag is
    # the very tag `load` gave this PR's image, and the pull moves that tag to
    # N-1's image: read through the tag afterwards, "this PR's revisions" were
    # N-1's, and the delta was empty however many migrations the PR added.
    this_api=$(sed -n 's/^api=//p' "$WORK/images.txt")
    : "${this_api:?no api image id in $WORK/images.txt: run the load phase first}"
    revisions "$this_api" >"$WORK/revisions-this"
    # N-1's own chart and the image repositories it names, from its tag: its
    # images are pulled, tagged and loaded under those names, the ones its
    # pods will ask for.
    tag_images "$prev"
    pchart=$TAG_CHART
    papi=$TAG_API_REPO
    pweb=$TAG_WEB_REPO
    # A local tag of its own: N-1 is usually the release VERSION names, whose
    # tag is the one this job gave the PR's images. Same profile prefix, so the
    # chart's tag rule accepts it.
    ltag="$ptag-previous"
    for ref in "$papi" "$pweb"; do
        pull_anonymously "$ref:$ptag"
        docker tag "$ref:$ptag" "$ref:$ltag"
    done
    kind load docker-image --name "$CLUSTER" "$papi:$ltag" "$pweb:$ltag"
    local api web
    api=$(docker image inspect --format '{{.Id}}' "$papi:$ltag")
    web=$(docker image inspect --format '{{.Id}}' "$pweb:$ltag")
    printf 'api=%s\nweb=%s\napi_repo=%s\nweb_repo=%s\n' "$api" "$web" "$papi" "$pweb" >"$WORK/images-previous.txt"

    # What N adds: revisions in this PR's image (read above) and not in N-1's.
    revisions "$papi:$ltag" >"$WORK/revisions-previous"
    LC_ALL=C comm -13 "$WORK/revisions-previous" "$WORK/revisions-this" >"$WORK/revisions-delta"
    LC_ALL=C comm -23 "$WORK/revisions-previous" "$WORK/revisions-this" >"$WORK/revisions-only-previous"
    if [ -s "$WORK/revisions-only-previous" ]; then
        cat "$WORK/revisions-only-previous"
        fail "N-1 ($prev) carries revisions this PR's image does not: an upgrade from it would be refused"
    fi

    kubectl create namespace "$ns"
    helm install "$release" "$pchart" -n "$ns" \
        --set profile="$PROFILE" \
        --set image.tag="$ltag" \
        --set publicBaseUrl="http://previous.$HOST" \
        --set firstSuperuser="$ADMIN" \
        --set ingress.enabled=false \
        --set api.replicaCount=2 \
        --set secrets.secretKey="$(openssl rand -hex 32)" \
        --set secrets.firstSuperuserPassword="$(openssl rand -hex 16)" \
        --set secrets.postgresUser=experimently \
        --set secrets.postgresPassword="$(openssl rand -hex 32)" \
        --set secrets.auditHmacKey="$(openssl rand -hex 32)" \
        --wait --timeout 10m
    assert_images "$ns" "$WORK/images-previous.txt"

    # A reader's upgrade: this chart, the install's values kept. The one
    # difference is image.tag, set at install only because of the local tag
    # above; cleared here, the chart's default (this PR's images) applies.
    upgrade_watched "$ns" "$release" "$CHART_DIR" -n "$ns" \
        --reset-then-reuse-values --set image.tag= --wait --timeout 8m
    assert_images "$ns"

    applied_revisions "$ns" "$release" "$WORK/previous.applied"
    local delta applied pods line
    delta=$(grep -c . "$WORK/revisions-delta" || true)
    applied=$(cut -d' ' -f2 "$WORK/previous.applied" | LC_ALL=C sort)
    pods=$(cut -d' ' -f1 "$WORK/previous.applied" | sort -u | grep -c . || true)
    [ "$applied" = "$(cat "$WORK/revisions-delta")" ] ||
        fail "N-1 -> N: the migrate containers applied [$(printf '%s' "$applied" | tr '\n' ' ')], the delta is [$(tr '\n' ' ' <"$WORK/revisions-delta")]"
    if [ "$delta" -gt 0 ]; then
        [ "$pods" -eq 1 ] || fail "N-1 -> N: $pods migrate containers applied revisions, expected exactly 1"
        line="N-1 -> N ($prev -> this PR): the delta contains $delta migration(s), applied by one migrate: $(tr '\n' ' ' <"$WORK/revisions-delta")"
    else
        line="N-1 -> N ($prev -> this PR): the delta contains no migration, so this run proves the upgrade path, not a schema change"
    fi
    assert_no_restarts "$ns"
    say "$line"
    [ -z "${GITHUB_STEP_SUMMARY:-}" ] ||
        printf '### chart-kind (%s)\n\n%s\n\n' "$PROFILE" "$line" >>"$GITHUB_STEP_SUMMARY"

    helm uninstall "$release" -n "$ns" --wait
    kubectl delete namespace "$ns" --wait=true
}

# ---------------------------------------------------------------------------
do_summary() {
    [ -f "$TIMINGS" ] || { say "no timings recorded"; return 0; }
    local total
    total=$(awk -F'\t' '{s += $2} END {print s + 0}' "$TIMINGS")
    {
        echo "### chart-kind ($PROFILE): per-phase timings"
        echo
        echo "| phase | seconds |"
        echo "|---|---:|"
        awk -F'\t' '{printf "| %s | %s |\n", $1, $2}' "$TIMINGS"
        echo "| **total** | **$total** |"
    } >"$WORK/summary.md"
    cat "$WORK/summary.md"
    if [ -n "${GITHUB_STEP_SUMMARY:-}" ]; then
        cat "$WORK/summary.md" >>"$GITHUB_STEP_SUMMARY"
    fi
}

do_diagnose() {
    set +e
    kubectl get pods -A -o wide
    kubectl get events -A --sort-by=.lastTimestamp | tail -n 60
    local ns pod
    for ns in "$NS" chart-kind-replicas chart-kind-previous; do
        for pod in $(kubectl get pods -n "$ns" -l app.kubernetes.io/component=api -o jsonpath='{.items[*].metadata.name}' 2>/dev/null); do
            echo "=== $ns/$pod migrate"
            kubectl logs -n "$ns" "$pod" -c migrate --tail=100
            echo "=== $ns/$pod api"
            kubectl logs -n "$ns" "$pod" -c api --tail=100
        done
        kubectl describe pods -n "$ns" 2>/dev/null | tail -n 120
    done
    return 0
}

do_teardown() {
    kind delete cluster --name "$CLUSTER"
}

# `time NAME START_EPOCH`: record a phase the workflow ran itself (the image
# builds are docker/build-push-action steps).
do_time() {
    printf '%s\t%s\n' "$1" "$(($(date +%s) - $2))" >>"$TIMINGS"
}

case "${1:-}" in
    cluster) phase cluster do_cluster ;;
    load) phase load do_load ;;
    dry-run) phase dry-run do_dry_run ;;
    guide) phase guide do_guide ;;
    replicas) phase replicas do_replicas ;;
    upgrade-next) phase upgrade-next do_upgrade_next ;;
    previous) do_previous ;;
    upgrade-previous) phase upgrade-previous do_upgrade_previous ;;
    summary) do_summary ;;
    diagnose) do_diagnose ;;
    teardown) do_teardown ;;
    time) do_time "$2" "$3" ;;
    *)
        printf 'usage: %s cluster|load|dry-run|guide|replicas|upgrade-next|previous|upgrade-previous|summary|diagnose|teardown|time NAME START\n' "$0" >&2
        exit 2
        ;;
esac
