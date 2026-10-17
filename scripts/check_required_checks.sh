#!/usr/bin/env bash
# Compare main's LIVE branch protection with what scripts/configure-repo.sh
# would write, and print every difference.
#
#   scripts/check_required_checks.sh [--repo OWNER/NAME] [--live-json FILE]
#                                    [--want-json FILE] [--names-only-ok]
#
# It compares, in this order:
#   names     the required-check names (REQUIRED_CHECKS in configure-repo.sh)
#   strict    required_status_checks.strict ("require branches to be up to date")
#   reviews   the required pull request review rule, or its absence
#   app id    the app each required check is pinned to, where the body that
#             would be written names one (`checks[].app_id`; -1 is "any app").
#             A check sent with no app id -- the deprecated `contexts` list,
#             which is what configure-repo.sh sends today, or a `checks` entry
#             without `app_id` -- is not compared. GitHub documents that it
#             assigns such a check to the app that most recently posted it
#             (https://docs.github.com/en/rest/branches/branch-protection#update-branch-protection,
#             from github/rest-api-description), so the result is decided by
#             GitHub at write time. That is GitHub's documented behaviour,
#             not something measured here. It is printed as a `note`, not
#             as drift.
#   flags     enforce_admins, linear history, force pushes, deletions,
#             conversation resolution, push restrictions
#
# Run it by hand, with your own `gh` login: the Actions token cannot read
# branch protection, so this is not a CI job. Reads only -- it calls one GET.
# `--live-json FILE` compares a saved `gh api .../branches/main/protection`
# response instead (a before/after snapshot, or a test fixture).
# `--want-json FILE` compares against that protection body instead of the one
# configure-repo.sh prints (an intended change, or a test fixture).
#
# configure-repo.sh runs this with `--names-only-ok` as its guard: a
# difference in names alone is what that script exists to change, so it is
# reported but does not fail; any other difference does.
#
# Exit: 0 no drift (or names only, with --names-only-ok); 1 drift;
#       2 could not compare (live protection unreadable, bad arguments).
# A `note` line never changes the exit status.
set -uo pipefail

HERE=$(cd "$(dirname "$0")" && pwd)
REPO="Controlvar/controlvar"
LIVE_FILE=""
WANT_FILE=""
NAMES_OK=0
while [ $# -gt 0 ]; do
    case "$1" in
        --repo) REPO="$2"; shift 2 ;;
        --live-json) LIVE_FILE="$2"; shift 2 ;;
        --names-only-ok) NAMES_OK=1; shift ;;
        --want-json) WANT_FILE="$2"; shift 2 ;;
        -h|--help) sed -n '2,39p' "$0"; exit 0 ;;
        *) echo "unknown argument: $1" >&2; exit 2 ;;
    esac
done

if [ -n "$WANT_FILE" ]; then
    if ! want=$(cat "$WANT_FILE"); then
        echo "check_required_checks: cannot read $WANT_FILE" >&2
        exit 2
    fi
elif ! want=$("$HERE/configure-repo.sh" --print-protection-payload); then
    echo "check_required_checks: configure-repo.sh --print-protection-payload failed" >&2
    exit 2
fi

if [ -n "$LIVE_FILE" ]; then
    if ! live=$(cat "$LIVE_FILE"); then
        echo "check_required_checks: cannot read $LIVE_FILE" >&2
        exit 2
    fi
    source_label="$LIVE_FILE"
else
    if ! live=$(gh api "repos/$REPO/branches/main/protection" 2>&1); then
        echo "check_required_checks: cannot read live protection on $REPO main:" >&2
        printf '%s\n' "$live" | sed 's/^/    /' >&2
        exit 2
    fi
    source_label="live $REPO main"
fi

WANT="$want" LIVE="$live" NAMES_OK="$NAMES_OK" SOURCE="$source_label" python3 - <<'PY'
import json
import os
import sys

try:
    want = json.loads(os.environ["WANT"])
    live = json.loads(os.environ["LIVE"])
except ValueError as exc:
    print(f"check_required_checks: not JSON: {exc}", file=sys.stderr)
    sys.exit(2)
if not isinstance(live, dict) or "url" not in live and "required_status_checks" not in live:
    print("check_required_checks: that is not a branch protection response", file=sys.stderr)
    sys.exit(2)

# A check the body sends with no app id: GitHub picks the app at write time.
UNSENT = "unsent"


def want_checks(rsc):
    """Check name -> app id the body sends (-1 is any app), or UNSENT."""
    if not rsc:
        return {}
    if "checks" in rsc:
        return {c["context"]: c.get("app_id", UNSENT) for c in rsc["checks"]}
    return {name: UNSENT for name in rsc.get("contexts", [])}


def live_checks(rsc):
    """Check name -> app id GitHub reports; null and -1 both mean any app."""
    if not rsc:
        return {}
    if "checks" in rsc:
        return {c["context"]: (-1 if c.get("app_id") is None else c["app_id"]) for c in rsc["checks"]}
    return {name: -1 for name in rsc.get("contexts", [])}


def app_label(app_id):
    return "any app" if app_id == -1 else f"app {app_id}"


def enabled(value):
    """Live flags come back as {"enabled": bool}; the PUT body uses a bare bool."""
    if isinstance(value, dict):
        return bool(value.get("enabled", False))
    return bool(value)


def review_rule(rule, keys):
    if rule is None:
        return None
    return {k: rule.get(k, False) for k in keys}


w_rsc = want.get("required_status_checks") or {}
l_rsc = live.get("required_status_checks") or {}
w_checks, l_checks = want_checks(w_rsc), live_checks(l_rsc)

names, other, notes = [], [], []

for name in sorted(set(l_checks) - set(w_checks)):
    names.append(f'names    required live, not in configure-repo.sh: "{name}"')
for name in sorted(set(w_checks) - set(l_checks)):
    names.append(f'names    in configure-repo.sh, not required live: "{name}"')

w_strict = bool(w_rsc.get("strict", False))
l_strict = bool(l_rsc.get("strict", False)) if l_rsc else None
if w_strict != l_strict:
    other.append(f"strict   live {json.dumps(l_strict)}, configure-repo.sh writes {json.dumps(w_strict)}")

# The review keys compared: every key configure-repo.sh sets, plus
# require_last_push_approval, which a PUT that omits it turns off.
w_rev_raw = want.get("required_pull_request_reviews")
keys = sorted(set((w_rev_raw or {}).keys()) | {"require_last_push_approval"})
w_rev = review_rule(w_rev_raw, keys)
l_rev = review_rule(live.get("required_pull_request_reviews"), keys)
if w_rev != l_rev:
    other.append(
        f"reviews  live {json.dumps(l_rev, sort_keys=True) if l_rev else 'no review rule'}, "
        f"configure-repo.sh writes {json.dumps(w_rev, sort_keys=True) if w_rev else 'no review rule'}"
    )

unsent = []
for name in sorted(set(w_checks) & set(l_checks)):
    sent = w_checks[name]
    if sent == UNSENT:
        unsent.append(name)
    elif (-1 if sent is None else sent) != l_checks[name]:
        other.append(
            f'app id   "{name}": live {app_label(l_checks[name])}, '
            f"configure-repo.sh writes {app_label(-1 if sent is None else sent)}"
        )
if unsent:
    pins = sorted({app_label(l_checks[n]) for n in unsent})
    notes.append(
        f"app id   {len(unsent)} check(s) sent with no app id: GitHub assigns each to the app "
        f"that most recently posted it (documented, not measured). Live today: {', '.join(pins)}."
    )

for flag in (
    "enforce_admins",
    "required_linear_history",
    "allow_force_pushes",
    "allow_deletions",
    "required_conversation_resolution",
):
    if flag in want and enabled(want[flag]) != enabled(live.get(flag, False)):
        other.append(
            f"flags    {flag}: live {json.dumps(enabled(live.get(flag, False)))}, "
            f"configure-repo.sh writes {json.dumps(enabled(want[flag]))}"
        )
if "restrictions" in want and (want["restrictions"] is None) != (live.get("restrictions") is None):
    other.append(
        "flags    restrictions: live "
        f"{'set' if live.get('restrictions') is not None else 'none'}, configure-repo.sh writes "
        f"{'set' if want['restrictions'] is not None else 'none'}"
    )

print(f"comparing configure-repo.sh with {os.environ['SOURCE']}")
for line in names + other:
    print(f"drift  {line}")
for line in notes:
    print(f"note   {line}")

names_ok = os.environ["NAMES_OK"] == "1"
if not names and not other:
    print(f"no drift: {len(l_checks)} required checks, strict {json.dumps(l_strict)}")
    sys.exit(0)
print(f"drift: {len(names)} in check names, {len(other)} in other settings")
if other:
    sys.exit(1)
if names_ok:
    print("names only: configure-repo.sh may change these")
    sys.exit(0)
sys.exit(1)
PY
