#!/usr/bin/env bash
# Refuse a deploy unless the commit a release tag points at passed the
# Release Gate.
#
#   scripts/verify_release_gate.sh <owner/repo> <commit sha> <tag>
#
# Exit status: 0 the gate passed; 1 refused (no result, failed, or still
# running); 2 could not ask (bad arguments, or the API call failed).
#
# How it asks, and why each part is there:
#
# * `?check_name=Release%20Gate%20Summary` -- the API filters by name. The
#   unfiltered list is paginated at 30, and a release commit carries 33 or more
#   check runs, so an unfiltered, unpaginated read could simply not see the
#   gate and report it absent (v0.2.7's commit: the gate was on page 2).
# * `--paginate` anyway -- a commit whose gate was re-run several times has
#   several runs with this name.
# * the name is compared EXACTLY as well. A reusable-workflow caller reports
#   its job as `<caller> / Release Gate Summary`; the server-side filter does
#   not return that, and if some future API did, it is not the gate this
#   repository's release-gate.yml reports, so it counts as absent. Failing
#   closed on a naming change is the point.
# * the LATEST completed run wins, so a re-run that passed after a flaky
#   failure is honoured, and a later failure is not hidden by an earlier pass.
#   Any run still in progress refuses outright: its result is unknown.
# * with no summary at all, the workflow's own runs for the commit are read
#   once, only to choose the message: a run still queued or in progress gets
#   "still running", none gets the dispatch hint. Exit 1 either way.
#
# Needs `gh` (GH_TOKEN in the environment) and nothing else; the workflow runs
# it in a job that holds no AWS credential.
set -euo pipefail

CHECK_NAME="Release Gate Summary"

if [ "$#" -ne 3 ] || [ -z "$1" ] || [ -z "$2" ] || [ -z "$3" ]; then
  echo "usage: $0 <owner/repo> <commit sha> <tag>" >&2
  exit 2
fi
REPO="$1"
SHA="$2"
TAG="$3"
SHORT="${SHA:0:7}"

# One line per run: completed_at<TAB>status<TAB>conclusion<TAB>url<TAB>name.
# completed_at is ISO 8601 in UTC, so it sorts as text.
if ! RUNS="$(gh api --paginate \
      "repos/${REPO}/commits/${SHA}/check-runs?check_name=Release%20Gate%20Summary&per_page=100" \
      --jq '.check_runs[] | [(.completed_at // ""), .status, (.conclusion // ""), .html_url, .name] | @tsv')"; then
  echo "::error::Could not read the check runs of ${TAG} (${SHORT}) from the GitHub API, so the Release Gate cannot be confirmed. Nothing was deployed; re-run the workflow."
  exit 2
fi

# Exactly this name, nothing prefixed.
RUNS="$(awk -F '\t' -v name="$CHECK_NAME" 'NF >= 5 && $5 == name' <<<"$RUNS")"

if [ -z "$RUNS" ]; then
  # The summary check run exists only once backend-gate, frontend-gate and
  # security-gate have finished, so "no summary" can also mean the push run of
  # Release Gate is still working. Ask the workflow's runs for this commit
  # before telling the operator to dispatch one. A failed read changes the
  # message only, never the outcome: this branch refuses either way.
  if ! WORKFLOW_RUNS="$(gh api --paginate \
        "repos/${REPO}/actions/workflows/release-gate.yml/runs?head_sha=${SHA}&per_page=100" \
        --jq '.workflow_runs[] | select(.status != "completed") | .html_url')"; then
    echo "::error title=No Release Gate result::${TAG} (${SHORT}) has no Release Gate result, and the Release Gate workflow runs for it could not be read, so it cannot be told whether one is still running. Nothing was deployed; re-run this deploy in a few minutes."
    exit 1
  fi
  # awk, not `head -n 1`: see the SIGPIPE note below.
  ACTIVE_URL="$(awk 'NF { print; exit }' <<<"$WORKFLOW_RUNS")"
  if [ -n "$ACTIVE_URL" ]; then
    echo "::error title=Release Gate still running::Release Gate is still running on ${TAG}: ${ACTIVE_URL}. Re-run when it finishes."
    exit 1
  fi
  echo "::error title=No Release Gate result::${TAG} (${SHORT}) has no Release Gate result. Release Gate runs on every push to main; for a tag cut before that, run it on the tag with \`gh workflow run release-gate.yml --ref ${TAG}\`, then re-run this deploy."
  exit 1
fi

# awk stops at the first match itself: `| head -n 1` would SIGPIPE awk and,
# under pipefail, end this script without a message.
RUNNING="$(awk -F '\t' '$2 != "completed" { print; exit }' <<<"$RUNS")"
if [ -n "$RUNNING" ]; then
  URL="$(cut -f 4 <<<"$RUNNING")"
  echo "::error title=Release Gate still running::Release Gate is still running on ${TAG}: ${URL}. Re-run when it finishes."
  exit 1
fi

LATEST="$(sort -t "$(printf '\t')" -k 1,1 <<<"$RUNS" | tail -n 1)"
CONCLUSION="$(cut -f 3 <<<"$LATEST")"
URL="$(cut -f 4 <<<"$LATEST")"

if [ "$CONCLUSION" != "success" ]; then
  echo "::error title=Release Gate failed::Release Gate failed on ${TAG}: ${URL}. A failed release cannot be deployed; cut a new one."
  exit 1
fi

echo "Release Gate passed on ${TAG} (${SHORT}): ${URL}"
