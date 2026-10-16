#!/usr/bin/env bash
# Assert this repository's settings: branch protection and its required
# checks, secret scanning, push protection, private vulnerability reporting.
#
#   scripts/configure-repo.sh [--repo OWNER/NAME] [--dry-run]
#   scripts/configure-repo.sh --print-protection-payload
#
# Everything here is idempotent -- re-running it re-asserts the same settings
# and reads each one back rather than trusting the response to the write. Run
# it after changing a required check, or to check nothing has drifted.
#
# It changes required-check NAMES and nothing else about branch protection.
# Before it writes anything it runs scripts/check_required_checks.sh against
# live protection, and refuses -- writing nothing -- if live differs from what
# it would write in any other way: `strict`, the review rule, the app each
# check is pinned to (where this body names one), or the other protection
# flags. Those are the founder's decision, not this file's, and a PUT here
# would silently replace them.
# `--print-protection-payload` prints the protection body it would PUT and
# exits without calling GitHub; check_required_checks.sh reads it from there.
#
# It was `scripts/publish/configure-public-repo.sh` and ran once, by hand,
# immediately after the repository was flipped to public: neither branch
# protection nor secret scanning can be configured on a PRIVATE repository on
# this plan (the API answers `403 Upgrade to GitHub Pro or make this repository
# public to enable this feature`), so it could not be part of the export. There
# is no export any more -- development happens here -- so it is simply the
# repository's settings script now.
set -uo pipefail

REPO="Controlvar/controlvar"
DRY=0
PRINT_PAYLOAD=0
HERE=$(cd "$(dirname "$0")" && pwd)
while [ $# -gt 0 ]; do
    case "$1" in
        --repo) REPO="$2"; shift 2 ;;
        --dry-run) DRY=1; shift ;;
        --print-protection-payload) PRINT_PAYLOAD=1; shift ;;
        -h|--help) sed -n '2,28p' "$0"; exit 0 ;;
        *) echo "unknown argument: $1" >&2; exit 2 ;;
    esac
done

# The checks a pull request must pass.
REQUIRED_CHECKS=(
    "Release Gate Summary"
    "Security Scan Summary"
    "Unit Tests"
    "Smoke Tests"
    "Frontend Tests"
    "SDK Contract Tests"
    "integration-tests"
    "SDK Unit Tests"
    "Browser E2E"
    "Docker Smoke"
    "lint"
    "regression-guard"
    "SDK Live Contract / sdk-live-contract (core)"
    "core-build"
    "full-build"
    "Module Tests"
    "Base Requirements Only"
    "CDK Stack Tests (Python)"
    "Leak Guard"
    "DCO"
)

# The branch protection body this script PUTs. `required_status_checks.strict`
# makes a branch merge only when it is up to date with main, which is what
# stops two independently green pull requests combining into a red main.
# The checks go in the deprecated `contexts` list, with no app id. GitHub
# documents that a check sent that way is assigned to the app that most
# recently posted it, and that only an explicit `app_id: -1` in `checks`
# allows any app:
# https://docs.github.com/en/rest/branches/branch-protection#update-branch-protection
# (from github/rest-api-description). That is GitHub's documented behaviour,
# not something measured here; for checks GitHub Actions posts, a write would
# be expected to keep them pinned to it (app 15368).
# Live protection may differ from this (see the header); the guard below is
# what stops a run from imposing it.
protection_payload() {
    printf '%s\n' "${REQUIRED_CHECKS[@]}" | python3 -c '
import json, sys
checks = [line.rstrip("\n") for line in sys.stdin if line.strip()]
print(json.dumps({
    "required_status_checks": {"strict": True, "contexts": checks},
    "enforce_admins": False,
    "required_pull_request_reviews": {
        "required_approving_review_count": 1,
        "require_code_owner_reviews": True,
        "dismiss_stale_reviews": True,
    },
    "restrictions": None,
    "required_linear_history": True,
    "allow_force_pushes": False,
    "allow_deletions": False,
    "required_conversation_resolution": True,
}))
'
}

if [ "$PRINT_PAYLOAD" -eq 1 ]; then
    protection_payload
    exit $?
fi

run() {  # $1 = description, rest = command
    local what="$1"; shift
    if [ "$DRY" -eq 1 ]; then printf 'would: %s\n      %s\n' "$what" "$*"; return 0; fi
    if "$@" > /tmp/cpr.$$ 2>&1; then
        printf 'ok    %s\n' "$what"
    else
        printf 'FAIL  %s\n' "$what"; sed 's/^/        /' /tmp/cpr.$$
        FAILED=1
    fi
    rm -f /tmp/cpr.$$
}
FAILED=0

visibility=$(gh api "repos/$REPO" -q .visibility 2>/dev/null || echo unknown)
if [ "$visibility" != "public" ] && [ "$DRY" -eq 0 ]; then
    echo "configure-public-repo: $REPO is '$visibility', not public." >&2
    echo "  Branch protection and secret scanning cannot be set on a private" >&2
    echo "  repository on this plan. Flip it to public first, then re-run." >&2
    exit 1
elif [ "$visibility" != "public" ]; then
    echo "note: $REPO is '$visibility'; these calls would fail until it is public."
    echo
fi

# The guard. Nothing above this line writes, and nothing below it runs unless
# live protection already matches what this script would write in everything
# but the required-check names. check_required_checks.sh exits 1 on such a
# difference and 2 when it cannot read live protection; both refuse. It runs
# in --dry-run too, so a dry run shows whether a real run would be refused.
"$HERE/check_required_checks.sh" --repo "$REPO" --names-only-ok
guard_rc=$?
if [ "$guard_rc" -ne 0 ]; then
    echo >&2
    echo "configure-repo: REFUSING -- nothing was written." >&2
    echo "  Live branch protection on $REPO differs from what this script would" >&2
    echo "  write in more than check names (see above). Those settings are the" >&2
    echo "  founder's call: change them on purpose with a targeted call, or" >&2
    echo "  update protection_payload() here once the intended values are agreed." >&2
    exit 1
fi
echo

# Secret scanning and push protection are free on public repositories. Push
# protection is the one that matters going forward: it rejects a commit
# containing a recognised credential at push time, rather than reporting it
# after it is already public.
run "secret scanning + push protection" \
    gh api -X PATCH "repos/$REPO" \
        -f 'security_and_analysis[secret_scanning][status]=enabled' \
        -f 'security_and_analysis[secret_scanning_push_protection][status]=enabled'

# Dependabot SECURITY updates -- not the version updates .github/dependabot.yml
# configures. Security updates only open a pull request when an advisory
# actually affects a pin, so they are low volume and are not what flooded the
# queue; #280 capped version updates and deliberately left this alone.
run "dependabot security updates" \
    gh api -X PATCH "repos/$REPO" \
        -f 'security_and_analysis[dependabot_security_updates][status]=enabled'

# Private vulnerability reporting has its OWN endpoint. Passing it inside the
# security_and_analysis PATCH above is accepted with a 2xx and silently does
# nothing -- the first run of this script printed `ok` for it and left it
# disabled. The verification block at the end is what caught that, and is why
# every setting here is read back rather than trusted.
run "private vulnerability reporting" \
    gh api -X PUT "repos/$REPO/private-vulnerability-reporting" 

# Branch protection, guarded above.
payload=$(protection_payload)
if [ "$DRY" -eq 1 ]; then
    echo "would: branch protection on $REPO main"
    echo "$payload" | python3 -m json.tool | sed 's/^/      /'
else
    if printf '%s' "$payload" | gh api -X PUT "repos/$REPO/branches/main/protection" --input - > /tmp/cpr.$$ 2>&1; then
        echo "ok    branch protection (${#REQUIRED_CHECKS[@]} required checks)"
    else
        echo "FAIL  branch protection"; sed 's/^/        /' /tmp/cpr.$$; FAILED=1
    fi
    rm -f /tmp/cpr.$$
fi

# CODEOWNERS is already in the tree; it does nothing unless code-owner review
# is required, which live protection does not do today. Check GitHub can resolve
# every handle in it -- an owner without write access is silently IGNORED, so
# a typo here is a rule that never fires rather than an error.
errs=$(gh api "repos/$REPO/codeowners/errors" -q '.errors | length' 2>/dev/null || echo "?")
if [ "$errs" = "0" ]; then
    echo "ok    CODEOWNERS resolves with no errors"
else
    echo "FAIL  CODEOWNERS has $errs error(s)"
    gh api "repos/$REPO/codeowners/errors" | sed 's/^/        /'
    FAILED=1
fi

# Read every setting back. A 2xx from GitHub is not evidence the setting took
# -- see the note on private vulnerability reporting above.
expect() {  # $1 = label, $2 = actual, $3 = wanted
    if [ "$2" = "$3" ]; then printf 'ok    %s = %s\n' "$1" "$2"
    else printf 'FAIL  %s = %s (wanted %s)\n' "$1" "$2" "$3"; FAILED=1; fi
}

if [ "$DRY" -eq 0 ]; then
    echo
    echo "verifying, by reading each setting back:"
    sa=$(gh api "repos/$REPO" -q '.security_and_analysis')
    expect "secret scanning"        "$(printf '%s' "$sa" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("secret_scanning",{}).get("status","?"))')" enabled
    expect "push protection"        "$(printf '%s' "$sa" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("secret_scanning_push_protection",{}).get("status","?"))')" enabled
    expect "dependabot security"    "$(printf '%s' "$sa" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("dependabot_security_updates",{}).get("status","?"))')" enabled
    expect "private vuln reporting" "$(gh api "repos/$REPO/private-vulnerability-reporting" -q '.enabled' 2>/dev/null)" true
    prot=$(gh api "repos/$REPO/branches/main/protection" 2>/dev/null)
    expect "required checks"        "$(printf '%s' "$prot" | python3 -c 'import json,sys; print(len(json.load(sys.stdin).get("required_status_checks",{}).get("contexts",[])))')" "${#REQUIRED_CHECKS[@]}"
    if "$HERE/check_required_checks.sh" --repo "$REPO" > /tmp/cpr.$$ 2>&1; then
        echo "ok    protection matches this script (names, strict, reviews, app ids)"
    else
        echo "FAIL  protection does not match this script"; sed 's/^/        /' /tmp/cpr.$$; FAILED=1
    fi
    rm -f /tmp/cpr.$$
    expect "force pushes blocked"   "$(printf '%s' "$prot" | python3 -c 'import json,sys; print(str(json.load(sys.stdin).get("allow_force_pushes",{}).get("enabled",True)).lower())')" false
    echo
fi

[ "$FAILED" -eq 0 ] && echo "configure-public-repo: OK" || { echo "configure-public-repo: FAILED" >&2; exit 1; }
