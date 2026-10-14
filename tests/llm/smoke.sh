#!/usr/bin/env bash
# tests/llm/smoke.sh -- one /complete per provider, through the real SDKs in
# the real image, against tests/llm/stub_provider.py.
#
# Run by the Docker Smoke job after
#   docker compose -f docker-compose.yml -f tests/llm/docker-compose.llm-stub.yml \
#     up -d --wait --no-build api llm-stub
# and runnable by hand against the same stack.
#
# NOT FOR A DEVELOPMENT STACK. The override this goes with re-creates the api
# container of the CURRENT compose project (by default the directory name,
# i.e. the same project `docker compose up` gave your dev stack) with its LLM
# providers pointed at the stub, and this script creates an LLM experiment in
# that project's database. So it refuses to run unless CI=true (GitHub Actions
# sets it for every step) or EXPERIMENTLY_LLM_SMOKE=1 is set. By hand, use a
# throwaway project: COMPOSE_PROJECT_NAME=llmsmoke plus spare *_HOST_PORTs.
#
# What a pass means: the image imports `anthropic` and `openai` at request
# time, builds a client from ANTHROPIC_* / OPENAI_*, sends a request the stub
# recognises, and parses the stub's answer into a 200 carrying the stub's text.
# Before #196 the same calls answered 502 "LLM provider error: No module named
# 'anthropic'".
#
# Then the judge (POST /judge) scores those completions through the same
# Anthropic client. The stub's text carries no score, so every evaluation must
# come back unscored -- `"score": null`, `"error": "no_score"` -- and the
# results must count every completion as unscored. Before #1192 each one was
# stored as 0.5 and counted as a score.
#
#   API_URL  the api service (default http://localhost:8000)
#   ADMIN_EMAIL / ADMIN_PASSWORD  the seeded demo admin
#
# Responses go to files, never into a pipe, for the reason tests/alb/rehearse.sh
# gives.
set -euo pipefail

if [ "${CI:-}" != "true" ] && [ "${EXPERIMENTLY_LLM_SMOKE:-}" != "1" ]; then
    echo "refusing: this re-creates the api container of the current compose project (${COMPOSE_PROJECT_NAME:-default}) with stub LLM provider settings; set CI=true or EXPERIMENTLY_LLM_SMOKE=1 to run it" >&2
    exit 2
fi

API_URL="${API_URL:-http://localhost:8000}"
ADMIN_EMAIL="${ADMIN_EMAIL:-admin@demo.com}"
ADMIN_PASSWORD="${ADMIN_PASSWORD:-Demo1234!}"
COMPOSE=(docker compose -f docker-compose.yml -f tests/llm/docker-compose.llm-stub.yml)

work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT

fail() {
    echo "::error::$*"
    exit 1
}

jq -n --arg e "$ADMIN_EMAIL" --arg p "$ADMIN_PASSWORD" '{email: $e, password: $p}' >"$work/login.json"
curl -sf -X POST "$API_URL/api/v1/auth/login" -H 'content-type: application/json' \
    --data @"$work/login.json" -o "$work/token.json"
TOKEN="$(jq -er .access_token "$work/token.json")"
auth=(-H "Authorization: Bearer $TOKEN" -H 'content-type: application/json')

# One experiment, one variant per provider, half the traffic each.
cat >"$work/experiment.json" <<'JSON'
{
  "name": "docker-smoke-llm-stub",
  "task_type": "text_generation",
  "evaluation_metric": "latency",
  "variants": [
    {"name": "anthropic", "is_control": true, "traffic_split": 0.5,
     "provider": "anthropic", "model_name": "claude-sonnet-5",
     "system_prompt": "Answer briefly.", "prompt_template": "Say {{word}}.", "max_tokens": 16},
    {"name": "openai", "is_control": false, "traffic_split": 0.5,
     "provider": "openai", "model_name": "gpt-4o-mini",
     "system_prompt": "Answer briefly.", "prompt_template": "Say {{word}}.", "max_tokens": 16}
  ]
}
JSON
code="$(curl -s -o "$work/created.json" -w '%{http_code}' -X POST "$API_URL/api/v1/llm-experiments/" \
    "${auth[@]}" --data @"$work/experiment.json")"
[ "$code" = "201" ] || fail "create LLM experiment: HTTP $code $(head -c 300 "$work/created.json")"
EXP="$(jq -er .id "$work/created.json")"
code="$(curl -s -o "$work/started.json" -w '%{http_code}' -X POST "$API_URL/api/v1/llm-experiments/$EXP/start" "${auth[@]}")"
[ "$code" = "200" ] || fail "start LLM experiment: HTTP $code $(head -c 300 "$work/started.json")"

# Assignment is a consistent hash of user_id, so walk user ids until each
# provider has answered once. 40 users at 50/50 miss one provider with
# probability 2^-39.
declare -A seen=()
completed=0
for i in $(seq 1 40); do
    code="$(curl -s -o "$work/complete.json" -w '%{http_code}' -X POST \
        "$API_URL/api/v1/llm-experiments/$EXP/complete" "${auth[@]}" \
        --data "{\"user_id\": \"docker-smoke-llm-$i\", \"input_variables\": {\"word\": \"ok\"}}")"
    [ "$code" = "200" ] || fail "POST /complete (user $i): HTTP $code $(head -c 300 "$work/complete.json")"
    completed=$((completed + 1))
    provider="$(jq -er .provider "$work/complete.json")"
    text="$(jq -er .response "$work/complete.json")"
    [ "$text" = "stub-$provider-ok" ] || fail "provider $provider answered '$text', expected 'stub-$provider-ok'"
    if [ -z "${seen[$provider]:-}" ]; then
        echo "llm smoke: $provider -> 200 '$text' ($(jq -c '{input_tokens, output_tokens}' "$work/complete.json"))"
    fi
    seen[$provider]=1
    if [ -n "${seen[anthropic]:-}" ] && [ -n "${seen[openai]:-}" ]; then
        break
    fi
done
[ -n "${seen[anthropic]:-}" ] || fail "no /complete was routed to the anthropic variant"
[ -n "${seen[openai]:-}" ] || fail "no /complete was routed to the openai variant"

# The judge, through the image's Anthropic client to the stub, on every
# completion above. The stub's reply has no JSON score, so nothing is scored.
code="$(curl -s -o "$work/judged.json" -w '%{http_code}' -X POST "$API_URL/api/v1/llm-experiments/$EXP/judge" \
    "${auth[@]}" --data '{"criteria": "helpfulness"}')"
[ "$code" = "200" ] || fail "POST /judge: HTTP $code $(head -c 300 "$work/judged.json")"
judged="$(jq -er 'length' "$work/judged.json")"
[ "$judged" = "$completed" ] || fail "the judge answered $judged entries for $completed completions"
jq -e 'all(.[]; .score == null and .error == "no_score")' "$work/judged.json" >/dev/null \
    || fail "a judge entry has a score from a reply with none (expected score null, error no_score): $(head -c 300 "$work/judged.json")"
code="$(curl -s -o "$work/results.json" -w '%{http_code}' "$API_URL/api/v1/llm-experiments/$EXP/results" "${auth[@]}")"
[ "$code" = "200" ] || fail "GET /results: HTTP $code $(head -c 300 "$work/results.json")"
unscored="$(jq -er '.total_auto_eval_unscored' "$work/results.json")"
per_variant="$(jq -er '[.variant_stats[].n_auto_eval_unscored] | add' "$work/results.json")"
[ "$unscored" = "$completed" ] && [ "$per_variant" = "$completed" ] \
    || fail "the results count $unscored unscored ($per_variant by variant) for $completed completions"
jq -e 'all(.variant_stats[]; .mean_auto_eval_score == null)' "$work/results.json" >/dev/null \
    || fail "a variant has an auto-eval mean although nothing was scored: $(head -c 300 "$work/results.json")"
echo "llm smoke: the judge left all $completed completions unscored, and the results count them"

# And the stub saw both, so the 200s are not something else answering.
"${COMPOSE[@]}" exec -T llm-stub python -c \
    'import urllib.request; print(urllib.request.urlopen("http://127.0.0.1:8080/requests").read().decode())' \
    >"$work/served.json"
jq -e 'index("/v1/messages") != null and index("/v1/chat/completions") != null' "$work/served.json" >/dev/null \
    || fail "the stub did not serve both providers: $(cat "$work/served.json")"
echo "llm smoke: the stub served $(jq -c 'group_by(.) | map({(.[0]): length}) | add' "$work/served.json")"
