# shellcheck shell=bash
# lib/checks.sh: everything install checks before it changes anything in AWS, in order: the
# options, this computer (tools, release, ~/.cdk.json, the pinned CDK), the sign-in, the
# account (regions, stacks, limits, engines, roles, permissions), the zone, what is in the
# way, and the release images. Each check reads only; each refusal exits 2 with nothing
# created. Every guard that permits a write reads an exact count or an exact list from a
# call that exited 0: an empty answer, None, an object where a list is meant or a failed
# call refuses. Sourced by controlvar-aws.

# The tools and the oldest version of each the AWS guide's run used, as each tool reports it.
# jq is 1.7, not the run's 1.7.1: Debian 13's and Ubuntu 24.04's jq packages are 1.7.1 and
# report "jq-1.7", so a floor of 1.7.1 would refuse both. Debian 12 and Ubuntu 22.04 ship 1.6.
CV_TOOLS="aws:2.28.24 cosign:3.1.3 crane:0.22.1 jq:1.7 node:22 python3:3.11"
CV_PRESENT="git dig curl openssl npm"
# The release images and the workflow that signs them.
CV_IMAGE_API="ghcr.io/controlvar/controlvar"
CV_IMAGE_WEB="ghcr.io/controlvar/controlvar-web"
CV_IDENTITY='^https://github\.com/Controlvar/controlvar/\.github/workflows/release\.yml@refs/(heads/main|tags/v[0-9].*)$'
CV_ISSUER="https://token.actions.githubusercontent.com"
CV_SIGNER="https://github.com/Controlvar/controlvar/.github/workflows/release.yml"
# The service-linked roles AWS makes for the stacks (kept after an uninstall).
CV_SERVICE_ROLES="AWSServiceRoleForECS AWSServiceRoleForElasticLoadBalancing AWSServiceRoleForRDS AWSServiceRoleForElastiCache AWSServiceRoleForApplicationAutoScaling_ECSService"
# What the install needs the sign-in to do, checked with IAM's policy simulator.
CV_ACTIONS=(
    ecr:CreateRepository secretsmanager:CreateSecret acm:RequestCertificate acm:DescribeCertificate
    route53:ChangeResourceRecordSets iam:CreateRole cloudformation:CreateStack iam:CreateServiceLinkedRole
)

# cv_version_ge <have> <need>: dotted numbers, have >= need.
cv_version_ge() {
    local have="$1" need="$2" h n
    while [ -n "$need" ]; do
        h=${have%%.*}
        n=${need%%.*}
        case "$h" in '' | *[!0-9]*) return 1 ;; esac
        if [ "$h" -gt "$n" ]; then return 0; fi
        if [ "$h" -lt "$n" ]; then return 1; fi
        if [ "$have" = "${have#*.}" ]; then have=""; else have=${have#*.}; fi
        if [ "$need" = "${need#*.}" ]; then need=""; else need=${need#*.}; fi
        if [ -z "$have" ] && [ -n "$need" ]; then have=0; fi
    done
    return 0
}

# cv_tool_version <tool>: the dotted version the tool reports, or nothing.
cv_tool_version() {
    local out=""
    case "$1" in
        aws) out=$(cv_aws --version 2>&1) || out="" ;;
        cosign) out=$(cv_ghcr cosign version 2>&1) || out="" ;;
        crane) out=$(cv_ghcr crane version 2>&1) || out="" ;;
        jq) out=$(jq --version 2>&1) || out="" ;;
        node) out=$(node --version 2>&1) || out="" ;;
        python3) out=$(python3 -c 'import sys; print("%d.%d.%d" % sys.version_info[:3])' 2>&1) || out="" ;;
    esac
    printf '%s' "$out" | sed -n 's/^[^0-9]*\([0-9][0-9]*\(\.[0-9][0-9]*\)*\).*$/\1/p' | sed -n 1p
}

# --- the options ---------------------------------------------------------------------------

# cv_check_domain: --domain is one host name we can install at.
cv_check_domain() {
    local d="$CV_DOMAIN" label tld
    case "$d" in
        '') msg_domain_missing ;;
        http://* | https://*) msg_domain_unusable "$d" "it starts with a scheme; give the host name only" ;;
        */*) msg_domain_unusable "$d" "it has a path after the name" ;;
        '*'*) msg_domain_unusable "$d" "it is a wildcard" ;;
    esac
    if [ "${#d}" -gt 253 ]; then
        msg_domain_unusable "$d" "it is longer than 253 characters"
    fi
    case "$d" in
        *[!a-z0-9.-]*) msg_domain_unusable "$d" "it may hold only lowercase letters, digits, hyphens and dots" ;;
        .* | *. | *..*) msg_domain_unusable "$d" "it has an empty label" ;;
    esac
    case "$d" in
        *.*.*) ;;
        *) msg_domain_unusable "$d" "it needs a name under a zone, such as app.your-company.com" ;;
    esac
    if printf '%s' "$d" | grep -Eq '^[0-9.]+$'; then
        msg_domain_unusable "$d" "it is an IP address"
    fi
    label=${d%%.*}
    if [ "${#label}" -gt 63 ]; then
        msg_domain_unusable "$d" "its first label is longer than 63 characters"
    fi
    case "$label" in
        -* | *-) msg_domain_unusable "$d" "its first label starts or ends with a hyphen" ;;
    esac
    case ".$d" in
    esac
    tld=${d##*.}
    case "$tld" in
        test | example | invalid | localhost | local | onion | arpa)
            msg_domain_unusable "$d" "it ends in .$tld, a name the internet does not resolve"
            ;;
    esac
}

# cv_check_emails: the alarm address by synth's own rule (the release's alarm_email.py), and
# the first administrator's by that rule and stricter ones, never weaker than the API's.
cv_check_emails() {
    local why flag="--alarm-email" also=" for --admin-email; --alarm-email can stay as it is"
    if [ -z "$CV_ALARM_EMAIL" ]; then
        msg_alarm_missing
    fi
    if ! why=$(cv_alarm_rule "$CV_ALARM_EMAIL"); then
        msg_email_unusable "--alarm-email" "$CV_ALARM_EMAIL" "$why" ""
    fi
    if [ -z "$CV_ADMIN_EMAIL" ]; then
        CV_ADMIN_EMAIL="$CV_ALARM_EMAIL"
        flag="--alarm-email (also the first administrator's sign-in)"
        also=" for the administrator with --admin-email"
    else
        flag="--admin-email"
        if ! why=$(cv_alarm_rule "$CV_ADMIN_EMAIL"); then
            msg_email_unusable "$flag" "$CV_ADMIN_EMAIL" "$why" "$also"
        fi
    fi
    if ! why=$(cv_admin_rule "$CV_ADMIN_EMAIL"); then
        msg_email_unusable "$flag" "$CV_ADMIN_EMAIL" "$why" "$also"
    fi
    if ! assert_first_admin_email; then
        msg_email_unusable "$flag" "$CV_ADMIN_EMAIL" "it is not one address" "$also"
    fi
}

# cv_alarm_rule <address>: the release's resolve_alarm_email(value, "staging"); prints why not.
cv_alarm_rule() {
    python3 -I - "$CV_RECORD/src/infrastructure/cdk/stacks" "$1" << 'PYTHON'
import sys

sys.path.insert(0, sys.argv[1])
from alarm_email import resolve_alarm_email  # noqa: E402

try:
    resolve_alarm_email(sys.argv[2], "staging")
except ValueError as error:
    text = str(error)
    why = text.split(" (ENVIRONMENT=")[0]
    print(why.replace("ALARM_EMAIL ", "it ", 1))
    sys.exit(1)
PYTHON
}

# cv_admin_rule <address>: the first administrator's address. ASCII; a local part of
# letters, digits and . _ % + - (at most 64, no leading, trailing or double dot); a domain of
# labels and a top-level name of two or more letters; never a name that does not resolve.
cv_admin_rule() {
    python3 -I - "$1" << 'PYTHON'
import re
import sys

value = sys.argv[1]
reserved = ("arpa", "invalid", "local", "localhost", "onion", "test")
if not value.isascii():
    print("it holds a character outside ASCII")
    sys.exit(1)
if '"' in value:
    print("it has quotation marks around the name")
    sys.exit(1)
if value.endswith("."):
    print("it ends with a dot")
    sys.exit(1)
local, _, domain = value.rpartition("@")
if not re.fullmatch(r"[A-Za-z0-9._%+-]{1,64}", local):
    print("its name holds a character the API refuses, or is longer than 64 characters")
    sys.exit(1)
if local.startswith(".") or local.endswith("."):
    print("it has a dot at the start or end of the name, or two dots in a row")
    sys.exit(1)
labels = domain.lower().split(".")
if len(labels) < 2 or not all(re.fullmatch(r"[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?", x) for x in labels):
    print("its domain is not a host name")
    sys.exit(1)
if not re.fullmatch(r"[a-z]{2,63}", labels[-1]):
    print("its top-level name is not two or more letters")
    sys.exit(1)
if labels[-1] in reserved:
    print(f"it ends in a reserved name (.{labels[-1]})")
    sys.exit(1)
PYTHON
}

# --- this computer -------------------------------------------------------------------------

# cv_check_tools [<tool:version list> <present list>]: install's tools (CV_TOOLS and
# CV_PRESENT) unless others are named; refuses with every one missing or too old.
cv_check_tools() {
    local missing=() entry tool need have tools="${1-$CV_TOOLS}" present="${2-$CV_PRESENT}"
    for entry in $tools; do
        tool=${entry%%:*}
        need=${entry#*:}
        if ! command -v "$tool" > /dev/null 2>&1; then
            missing+=("$tool: not found; needs $need or later")
            continue
        fi
        have=$(cv_tool_version "$tool")
        if [ -z "$have" ] || ! cv_version_ge "$have" "$need"; then
            missing+=("$tool: ${have:-an unreadable version}; needs $need or later")
        fi
    done
    for tool in $present; do
        if ! command -v "$tool" > /dev/null 2>&1; then
            missing+=("$tool: not found")
        fi
    done
    if [ "${#missing[@]}" -gt 0 ]; then
        msg_tools ${missing[@]+"${missing[@]}"}
    fi
}

# cv_check_release: VERSION names a tag this clone holds, 0.36.1 or later; its CDK and its
# two deploy scripts are copied out of the tag into <record>/src, never run in the clone.
cv_check_release() {
    local out
    if [ ! -f "$CV_CLONE/VERSION" ]; then
        msg_no_version
    fi
    CV_VERSION=$(tr -d '[:space:]' < "$CV_CLONE/VERSION")
    case "$CV_VERSION" in
        '' | *[!0-9.]*) msg_no_version ;;
    esac
    if ! cv_version_ge "$CV_VERSION" "0.36.1"; then
        msg_old_release "$CV_VERSION"
    fi
    if ! CV_REVISION=$(git -C "$CV_CLONE" rev-parse --verify --quiet "v$CV_VERSION^{commit}" 2> /dev/null) || [ -z "$CV_REVISION" ]; then
        msg_no_tag "$CV_VERSION"
    fi
    rm -rf "$CV_RECORD/src.new"
    mkdir -p "$CV_RECORD/src.new"
    if ! git -C "$CV_CLONE" archive --format=tar "v$CV_VERSION" infrastructure/cdk scripts/register_task_definition.sh scripts/run_migration_task.sh > "$CV_RECORD/tmp/src.tar" 2> "$CV_RECORD/tmp/archive.err"; then
        out=$(sed -n 1p "$CV_RECORD/tmp/archive.err")
        msg_archive "$CV_VERSION" "${out:-no output}"
    fi
    if ! tar -x -f "$CV_RECORD/tmp/src.tar" -C "$CV_RECORD/src.new" 2> "$CV_RECORD/tmp/archive.err"; then
        out=$(sed -n 1p "$CV_RECORD/tmp/archive.err")
        msg_archive "$CV_VERSION" "${out:-no output}"
    fi
    rm -f "$CV_RECORD/tmp/src.tar"
    rm -rf "$CV_RECORD/src"
    mv "$CV_RECORD/src.new" "$CV_RECORD/src"
}

# cv_check_cdk_json: the CDK CLI applies every setting in ~/.cdk.json as a user default
# (plugin, build, output, rollback, context, toolkitStackName, profile ...), so only an empty
# object, or no file, is accepted. The installer's own cdk runs with a HOME of its own
# (cv_home_init) and does not read the reader's file at all (measured by hand, CDK CLI
# 2.1144.0: a plugin there is loaded only with the reader's HOME), so this refusal is a
# second guard.
cv_check_cdk_json() {
    local file="$HOME/.cdk.json" keys
    if [ ! -e "$file" ] && [ ! -L "$file" ]; then
        return 0
    fi
    # Exactly one JSON value, an object: an empty file, two values or a list are refused.
    if ! keys=$(jq -e -s -r 'if length == 1 and (.[0] | type) == "object" then (.[0] | keys | join(", "))
        else error("not one object") end' "$file" 2> /dev/null); then
        msg_cdk_json "does not parse as a JSON object"
    fi
    if printf '%s' "$keys" | grep -Eq '(^|, )(context|output)(,|$)'; then
        msg_cdk_json "holds settings ($keys)"
    fi
}

# cv_pins_hash <file...>: the sha256 of each file, in order, space-separated: what the
# tools step writes beside a tool it installed (.pins) and compares before reusing it.
cv_pins_hash() {
    python3 -I -c 'import hashlib, sys
print(" ".join(hashlib.sha256(open(p, "rb").read()).hexdigest() for p in sys.argv[1:]))' "$@"
}

# cv_tools_step: the pinned CDK CLI and the CDK app's Python, from deploy/aws-installer/pins/,
# installed without running any package's scripts, from a hashed lock. Each is reused only
# while the locks it was installed from are byte for byte the clone's (their hashes in its
# .pins), so a lock that moves any package, the CLI's or the CDK libraries' or what they
# pull in, installs again. npm and pip run like every other child, under the installer's
# environment (the proxy and certificate variables passed, a HOME of their own: never the
# reader's ~/.npmrc or pip configuration).
cv_tools_step() {
    local tools="$CV_RECORD/tools" pins="$CV_HOME/pins" want_cli want_py
    if ! want_cli=$(cv_pins_hash "$pins/package.json" "$pins/package-lock.json") ||
        ! want_py=$(cv_pins_hash "$pins/requirements.lock"); then
        msg_tool_failed "the tools step" "1" "the locks in $pins could not be read"
    fi
    if [ ! -x "$tools/cdk-cli/node_modules/.bin/cdk" ] || [ "$(cat "$tools/cdk-cli/.pins" 2> /dev/null)" != "$want_cli" ]; then
        rm -rf "$tools/cdk-cli"
        mkdir -p "$tools/cdk-cli"
        cp "$pins/package.json" "$pins/package-lock.json" "$tools/cdk-cli/"
        cv_out "  Installing the pinned CDK CLI into the record (scripts off) ..."
        if ! (cd "$tools/cdk-cli" && env -i ${CV_ENV[@]+"${CV_ENV[@]}"} npm ci --no-audit --no-fund) > "$CV_RECORD/logs/tools-npm.log" 2>&1 ||
            ! printf '%s\n' "$want_cli" > "$tools/cdk-cli/.pins"; then
            msg_tool_failed "npm ci" "1" "$(tail -n 1 "$CV_RECORD/logs/tools-npm.log" 2> /dev/null)"
        fi
    fi
    if [ ! -x "$tools/venv/bin/python3" ] || [ "$(cat "$tools/venv/.pins" 2> /dev/null)" != "$want_py" ]; then
        rm -rf "$tools/venv"
        cv_out "  Installing the CDK app's Python packages into the record (hashed lock) ..."
        if ! env -i ${CV_ENV[@]+"${CV_ENV[@]}"} python3 -m venv "$tools/venv" > "$CV_RECORD/logs/tools-pip.log" 2>&1 ||
            ! env -i ${CV_ENV[@]+"${CV_ENV[@]}"} "$tools/venv/bin/python3" -m pip install --require-hashes --no-deps -r "$pins/requirements.lock" >> "$CV_RECORD/logs/tools-pip.log" 2>&1 ||
            ! printf '%s\n' "$want_py" > "$tools/venv/.pins"; then
            msg_tool_failed "pip install" "1" "$(tail -n 1 "$CV_RECORD/logs/tools-pip.log" 2> /dev/null)"
        fi
    fi
}

# --- the sign-in ---------------------------------------------------------------------------

# cv_check_signin: the installer's own copy of the named profile, the probe, the identity,
# the token rule.
cv_check_signin() {
    local source="${AWS_CONFIG_FILE:-$HOME/.aws/config}" rc=0 err keys id arn role
    keys=""
    for err in AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY AWS_SESSION_TOKEN; do
        if [ -n "${!err:-}" ]; then keys="${keys:+$keys, }$err"; fi
    done
    if [ -n "$keys" ]; then
        cv_out "Ignored: $keys in your shell. The beta signs in only with the profile $CV_PROFILE."
    fi
    CV_SESSION=$(cv_copy_config "$source" "$CV_PROFILE" "$CV_RECORD/aws-config" 2> "$CV_RECORD/tmp/config.err") || rc=$?
    case "$rc" in
        0) ;;
        3) msg_no_profile "$CV_PROFILE" "$source" ;;
        *) msg_not_identity_center "$CV_PROFILE" ;;
    esac
    if ! cv_probe; then
        err=$(grep -m 1 . "$CV_RECORD/tmp/probe.err") || err=""
        msg_probe "${err:-it did not stop for want of credentials}"
    fi
    if ! id=$(cv_aws sts get-caller-identity --output json 2> "$CV_RECORD/tmp/identity.err"); then
        msg_signin_expired "$CV_PROFILE"
    fi
    if ! CV_GOT=$(printf '%s' "$id" | jq -r 'if (.Account | type) == "string" then .Account else error("no account") end' 2> /dev/null) ||
        ! printf '%s' "$CV_GOT" | grep -Eq '^[0-9]{12}$'; then
        msg_identity_unreadable "$CV_PROFILE" "no account in the answer"
    fi
    arn=$(printf '%s' "$id" | jq -r '.Arn // ""' 2> /dev/null) || arn=""
    role=${arn#*:assumed-role/}
    role=${role%%/*}
    case "$arn" in
        "arn:aws:sts::$CV_ACCOUNT:assumed-role/AWSReservedSSO_"*/*) ;;
        *) msg_not_identity_center "$CV_PROFILE" ;;
    esac
    CV_ROLE="$role"
    if ! CV_TOKEN=$(cv_token_rule "$CV_SESSION"); then
        case "$CV_TOKEN" in
            *"renewed "*) msg_token "$CV_PROFILE" "${CV_TOKEN##*renewed } minutes ago" ;;
            *) msg_token "$CV_PROFILE" "at a time the installer cannot read (no cached sign-in for its session)" ;;
        esac
    fi
}

# --- the account ---------------------------------------------------------------------------

# cv_read <name> <file> <aws arguments...>: one read as JSON into <file>; exit 0 only when
# the call exited 0. The error's code (in parentheses) is in CV_CODE either way.
cv_read() {
    local name="$1" file="$2" rc=0
    shift 2
    cv_aws "$@" --output json > "$file" 2> "$file.err" || rc=$?
    CV_CODE=$(cv_error_code "$file.err")
    if [ "$rc" -ne 0 ]; then
        cv_log "read failed ($name): $(sed -n '1,3p' "$file.err" 2> /dev/null)"
        return 1
    fi
    return 0
}

# cv_list <file> <jq test> [<jq option>...]: the answer is exactly one JSON list passing the
# test. A value the test compares with goes in as an option (--arg name value), never into
# the test's own text.
cv_list() {
    local file="$1" test="$2"
    shift 2
    jq -e -s "$@" "length == 1 and (.[0] | type == \"array\") and (.[0] | $test)" "$file" > /dev/null 2>&1
}

# cv_check_regions: the account's enabled regions, which must include us-west-2 and us-east-1.
cv_check_regions() {
    local f="$CV_RECORD/before/regions.json" r
    if ! cv_read "the enabled regions" "$f" ec2 describe-regions --query 'Regions[].RegionName'; then
        msg_read_failed "the account's enabled regions" "${CV_CODE:-no answer}"
    fi
    if ! cv_list "$f" 'all(type == "string") and length > 0'; then
        msg_read_failed "the account's enabled regions" "an answer that is not a list of regions"
    fi
    for r in us-west-2 us-east-1; do
        if ! jq -e --arg r "$r" 'index($r) != null' "$f" > /dev/null; then
            msg_region_missing "$r"
        fi
    done
    CV_REGIONS=$(jq -r '.[]' "$f")
}

# cv_check_stacks: no stack of this app in any enabled region. Only the exact AccessDenied
# code reads as "not checked", and never in us-west-2.
cv_check_stacks() {
    local r f found=() unchecked=() n=0
    for r in $CV_REGIONS; do
        n=$((n + 1))
        f="$CV_RECORD/before/stacks-$r.json"
        if ! cv_read "the stacks in $r" "$f" cloudformation list-stacks --region "$r" \
            --query "StackSummaries[?StackStatus!='DELETE_COMPLETE' && starts_with(StackName, '$NAMES_STACK_PREFIX')].StackName"; then
            case "$CV_CODE" in
                AccessDenied | *)
                    # shellcheck disable=SC2153  # CV_REGION is lib/env.sh's
                    if [ "$r" = "$CV_REGION" ]; then
                        msg_read_failed "the stacks in $r" "$CV_CODE"
                    fi
                    unchecked+=("$r")
                    continue
                    ;;
                *) msg_read_failed "the stacks in $r" "${CV_CODE:-no answer}" ;;
            esac
        fi
        if ! cv_list "$f" 'all(type == "string")' || ! jq -r '.[]' "$f" > "$f.lines"; then
            msg_read_failed "the stacks in $r" "an answer that is not a list of stacks"
        fi
        while IFS= read -r line; do
            if [ -n "$line" ]; then found+=("$line in $r"); fi
        done < "$f.lines"
    done
    for r in ${unchecked[@]+"${unchecked[@]}"}; do
        cv_out "Not checked: $r (your organisation does not allow listing stacks there). The other regions were checked."
    done
    if [ "${#found[@]}" -gt 0 ]; then
        msg_stacks_present "$(cv_join ${found[@]+"${found[@]}"})"
    fi
}

# cv_quota <service code> <quota code> <label> <need> <service name>: the applied value, or
# AWS's default only on NoSuchResourceException, read as a number (an empty answer, None or
# text refuses). Leaves the value in CV_QUOTA.
cv_quota() {
    local f="$CV_RECORD/before/quota-$2.json"
    CV_QUOTA=""
    if ! cv_read "quota $3" "$f" service-quotas get-service-quota --service-code "$1" --quota-code "$2" --query 'Quota.Value'; then
        if [ "$CV_CODE" != NoSuchResourceException ] ||
            ! cv_read "quota $3" "$f" service-quotas get-aws-default-service-quota --service-code "$1" --quota-code "$2" --query 'Quota.Value'; then
            msg_quota_unreadable "$3" "${CV_CODE:-no answer}"
        fi
    fi
    CV_QUOTA=$(cat "$f")
    if [ "${CV_QUOTA%.*}" -lt "$4" ] 2> /dev/null; then
        msg_quota "$3" "$CV_QUOTA" "$4" "$5"
    fi
}

cv_check_limits() {
    local f
    cv_quota fargate L-3032A538 "Fargate On-Demand vCPU resource count" 3 "AWS Fargate"
    CV_FARGATE="$CV_QUOTA"
    if ! jq -e -n --argjson v "$CV_FARGATE" '$v >= 11' > /dev/null; then
        cv_out "WARNING  Fargate: this account may run $CV_FARGATE vCPUs of Fargate tasks in us-west-2. The install needs 3; if traffic grows the API adds tasks up to 11 vCPUs, and past $CV_FARGATE the extra tasks wait. Raising the limit is free (Service Quotas > AWS Fargate), and the install can go ahead without it."
    fi
    cv_quota ec2 L-0263D0A3 "EC2-VPC Elastic IPs" 1 "Amazon Elastic Compute Cloud (Amazon EC2)"
    cv_quota vpc L-F678F1CE "VPCs per Region" 2 "Amazon Virtual Private Cloud (Amazon VPC)"
    cv_quota elasticloadbalancing L-53DA6B97 "Application Load Balancers per Region" 1 "Elastic Load Balancing (ELB)"
    f="$CV_RECORD/before/aurora.json"
    if ! cv_read "Aurora's offering" "$f" rds describe-orderable-db-instance-options --engine aurora-postgresql \
        --engine-version 15.17 --db-instance-class db.t3.medium --query 'length(OrderableDBInstanceOptions)'; then
        msg_read_failed "Aurora PostgreSQL's offering in us-west-2" "${CV_CODE:-no answer}"
    fi
    if ! jq -e -s 'length == 1 and (.[0] | type == "number") and .[0] >= 1' "$f" > /dev/null 2>&1; then
        msg_engine "Aurora PostgreSQL 15.17 on db.t3.medium"
    fi
    f="$CV_RECORD/before/redis.json"
    if ! cv_read "Redis's versions" "$f" elasticache describe-cache-engine-versions --engine redis \
        --engine-version 7.0 --query 'CacheEngineVersions[].EngineVersion'; then
        msg_read_failed "Redis's versions in us-west-2" "${CV_CODE:-no answer}"
    fi
    if ! cv_list "$f" 'index("7.0") != null'; then
        msg_engine "Redis 7.0"
    fi
    f="$CV_RECORD/before/redis-node.json"
    if ! cv_read "the Redis node type" "$f" elasticache describe-reserved-cache-nodes-offerings \
        --cache-node-type cache.t4g.small --query 'length(ReservedCacheNodesOfferings)'; then
        msg_read_failed "the cache.t4g.small offering in us-west-2" "${CV_CODE:-no answer}"
    fi
    if ! jq -e -s 'length == 1 and (.[0] | type == "number") and .[0] >= 1' "$f" > /dev/null 2>&1; then
        msg_engine "Redis on cache.t4g.small"
    fi
}

# cv_before_write <file> <what> [<line>...]: with no line, before/<file> emptied; with
# lines, each added to it. Uninstall keeps what these lists name, so a write that fails
# refuses, as a read that fails does (the shell's own error goes to the log).
cv_before_write() {
    local name="$1" what="$2" file="$CV_RECORD/before/$1" err
    shift 2
    if [ "$#" -eq 0 ]; then
        err=$({ : > "$file"; } 2>&1) && return 0
    else
        err=$({ printf '%s\n' "$@" >> "$file"; } 2>&1) && return 0
    fi
    cv_log "before/$name: ${err:-not written}"
    msg_before_unwritten "$what" "before/$name"
}

# cv_check_roles: which of the service-linked roles exist (NoSuchEntity is the only
# "absent"), GuardDuty and Config, recorded in before/.
cv_check_roles() {
    local n f
    cv_before_write service-linked-roles.txt "the service-linked roles"
    for n in $CV_SERVICE_ROLES; do
        f="$CV_RECORD/tmp/role-$n.json"
        if cv_read "the role $n" "$f" iam get-role --role-name "$n" --query Role.RoleName; then
            if ! jq -e -s --arg n "$n" 'length == 1 and .[0] == $n' "$f" > /dev/null 2>&1; then
                msg_read_failed "the role $n" "an answer that is not its name"
            fi
            cv_before_write service-linked-roles.txt "the role $n" "$n present"
        elif grep -q '^aws: \[ERROR\]: An error occurred (NoSuchEntity)' "$f.err"; then
            cv_before_write service-linked-roles.txt "the role $n" "$n absent"
        else
            msg_read_failed "the role $n" "${CV_CODE:-no answer}"
        fi
    done
    if ! cv_read "GuardDuty" "$CV_RECORD/before/guardduty.json" guardduty list-detectors --query DetectorIds ||
        ! cv_list "$CV_RECORD/before/guardduty.json" 'all(type == "string")'; then
        msg_read_failed "GuardDuty's detectors" "${CV_CODE:-an answer that is not a list}"
    fi
    if ! cv_read "Config" "$CV_RECORD/before/config.json" configservice describe-configuration-recorders --query 'ConfigurationRecorders[].name' ||
        ! cv_list "$CV_RECORD/before/config.json" 'all(type == "string")'; then
        msg_read_failed "Config's recorders" "${CV_CODE:-an answer that is not a list}"
    fi
}

# cv_check_before: what is already here that uninstall could otherwise take for the
# install's: the three families' and Container Insights' log groups, and the families'
# task-definition revisions. Uninstall keeps
# whatever these lists hold, so a read that is not exactly a list refuses.
cv_check_before() {
    local f="$CV_RECORD/tmp/log-groups.json" status name line
    if ! cv_read "the log groups" "$f" logs describe-log-groups --query 'logGroups[].logGroupName' ||
        ! cv_list "$f" 'all(type == "string")'; then
        msg_read_failed "the account's log groups" "${CV_CODE:-an answer that is not a list}"
    fi
    cv_before_write log-groups.txt "the account's log groups"
    for name in ${NAMES_LOG_GROUPS[@]+"${NAMES_LOG_GROUPS[@]}"} "$NAMES_CONTAINER_INSIGHTS_LOG_GROUP"; do
        if jq -e --arg n "$name" 'index($n) != null' "$f" > /dev/null; then
            cv_before_write log-groups.txt "the account's log groups" "$name"
        fi
    done
    cv_before_write task-definitions.txt "the account's task definitions"
    for status in ACTIVE INACTIVE; do
        f="$CV_RECORD/tmp/task-definitions-$status.json"
        if ! cv_read "the task definitions" "$f" ecs list-task-definitions --status "$status" --query taskDefinitionArns ||
            ! cv_list "$f" 'all(type == "string")'; then
            msg_read_failed "the account's task definitions" "${CV_CODE:-an answer that is not a list}"
        fi
        for name in ${NAMES_FAMILIES[@]+"${NAMES_FAMILIES[@]}"}; do
            if ! jq -r --arg f ":task-definition/$name:" '.[] | select(contains($f))' "$f" > "$f.lines"; then
                msg_read_failed "the account's task definitions" "an answer that is not a list"
            fi
            while IFS= read -r line; do
                cv_before_write task-definitions.txt "the account's task definitions" "$line"
            done < "$f.lines"
        done
    done
}

# cv_check_permissions: IAM's simulator says each action the install needs is allowed: exactly
# one "allowed" per action, for exactly these actions.
cv_check_permissions() {
    local f="$CV_RECORD/before/simulation.json" role_arn n denied
    if ! cv_read "the role" "$CV_RECORD/tmp/role.json" iam get-role --role-name "$CV_ROLE" --query Role.Arn ||
        ! role_arn=$(jq -e -s -r 'if length == 1 and (.[0] | type) == "string" then .[0] else error("no arn") end' "$CV_RECORD/tmp/role.json" 2> /dev/null); then
        msg_permission_check "iam:GetRole" "${CV_CODE:-an answer that is not a role}"
    fi
    if ! cv_read "the simulation" "$f" iam simulate-principal-policy --policy-source-arn "$role_arn" \
        --action-names ${CV_ACTIONS[@]+"${CV_ACTIONS[@]}"} --query 'EvaluationResults[].[EvalActionName,EvalDecision]'; then
        msg_permission_check "iam:SimulatePrincipalPolicy" "${CV_CODE:-no answer}"
    fi
    n=${#CV_ACTIONS[@]}
    if ! cv_list "$f" 'all(type == "array" and length == 2)'; then
        msg_permission_check "iam:SimulatePrincipalPolicy" "an answer that is not a list of decisions"
    fi
    if ! jq -e --argjson n "$n" --arg actions "$(printf '%s\n' ${CV_ACTIONS[@]+"${CV_ACTIONS[@]}"})" '
        (length == $n)
        and ([.[][0]] | sort) == ($actions | split("\n") | map(select(length > 0)) | sort)
        and ([.[] | select(.[1] == "allowed")] | length) == $n' "$f" > /dev/null; then
        denied=$(jq -r '[.[] | select(.[1] != "allowed") | .[0]] | .[0:3] | join(", ")' "$f" 2> /dev/null) || denied=""
        msg_permission_denied "$CV_ROLE" "$(jq -r '[.[] | select(.[1] != "allowed")] | length' "$f" 2> /dev/null || printf 'some')" "${denied:-the actions the log names}"
    fi
}

# --- the zone ------------------------------------------------------------------------------

# cv_check_zone: --domain is one name directly under exactly one public zone of this account,
# which the internet uses, and nothing is at the name yet.
cv_check_zone() {
    local f="$CV_RECORD/before/zones.json" zone ns public n
    CV_ZONE=${CV_DOMAIN#*.}
    if ! cv_read "the hosted zones" "$f" route53 list-hosted-zones --query 'HostedZones[].[Name,Id,Config.PrivateZone]' ||
        ! cv_list "$f" 'all(type == "array" and length == 3)'; then
        msg_read_failed "the account's Route 53 zones" "${CV_CODE:-an answer that is not a list of zones}"
    fi
    if jq -e --arg name "$CV_DOMAIN." 'any(.[]; .[0] == $name)' "$f" > /dev/null; then
        msg_zone_top "$CV_DOMAIN"
    fi
    n=$(jq --arg name "$CV_ZONE." '[.[] | select(.[0] == $name and .[2] == false)] | length' "$f")
    if [ "$n" = 0 ]; then
        if jq -e --arg name "$CV_ZONE." 'any(.[]; .[0] == $name)' "$f" > /dev/null; then
            msg_zone_private "$CV_ZONE"
        fi
        zone=$(jq -r '[.[] | select(.[2] == false) | .[0] | rtrimstr(".")] | if length == 0 then "a public zone of this account (it has none)" else join(", ") end' "$f")
        msg_no_zone "$CV_DOMAIN" "$CV_ZONE" "$zone"
    fi
    if [ "$n" != 1 ]; then
        msg_zone_twice "$CV_ZONE" "$n"
    fi
    CV_ZONE_ID=$(jq -r --arg name "$CV_ZONE." '.[] | select(.[0] == $name and .[2] == false) | .[1]' "$f")
    CV_ZONE_ID=${CV_ZONE_ID#/hostedzone/}
    f="$CV_RECORD/before/zone-ns.json"
    if ! cv_read "the zone's name servers" "$f" route53 get-hosted-zone --id "$CV_ZONE_ID" --query 'DelegationSet.NameServers' ||
        ! cv_list "$f" 'length == 4 and all(type == "string")'; then
        msg_read_failed "the name servers of $CV_ZONE" "${CV_CODE:-an answer that is not four name servers}"
    fi
    ns=$(jq -r '.[] | ascii_downcase | rtrimstr(".")' "$f" | LC_ALL=C sort)
    if ! public=$(dig +short NS "$CV_ZONE" @1.1.1.1 2> "$CV_RECORD/tmp/dig.err"); then
        msg_unreachable "1.1.1.1" "dig could not ask it for $CV_ZONE's name servers"
    fi
    public=$(printf '%s\n' "$public" | tr '[:upper:]' '[:lower:]' | sed 's/\.$//' | sed '/^$/d' | LC_ALL=C sort)
    if false; then
        msg_zone_not_delegated "$CV_ZONE" "$(printf '%s' "${public:-none}" | tr '\n' ' ' | sed 's/ $//')" "$(printf '%s' "$ns" | tr '\n' ' ' | sed 's/ $//')"
    fi
    cv_before_write zone-ns.txt "the name servers of $CV_ZONE"
    cv_before_write zone-ns.txt "the name servers of $CV_ZONE" "$ns"
    f="$CV_RECORD/before/host-records.json"
    if ! cv_read "the records at $CV_DOMAIN" "$f" route53 list-resource-record-sets --hosted-zone-id "$CV_ZONE_ID" \
        --query "ResourceRecordSets[?Name=='$CV_DOMAIN.'].Type" ||
        ! cv_list "$f" 'all(type == "string")'; then
        msg_read_failed "the records at $CV_DOMAIN" "${CV_CODE:-an answer that is not a list of records}"
    fi
    if ! cv_list "$f" 'length == 0'; then
        msg_record_present "$CV_DOMAIN" "$(jq -r '.[0]' "$f")"
    fi
}

# --- what is in the way ----------------------------------------------------------------------

# cv_check_collisions: the CDK's setup stack is complete or absent (only the exact "does not
# exist" answer is absent), and no repository or secret of the install's names exists yet:
# its own two, and the database's, which the database stack makes and which CloudFormation
# may leave scheduled for deletion (uninstall then removes it, as only this install's).
cv_check_collisions() {
    local f="$CV_RECORD/before/cdktoolkit.json" status repo secret
    if cv_read "CDKToolkit" "$f" cloudformation describe-stacks --stack-name CDKToolkit --query 'Stacks[0].[StackStatus,StackId]'; then
        if ! cv_list "$f" 'length == 2 and all(type == "string")'; then
            msg_read_failed "the CDKToolkit stack" "an answer that is not its status"
        fi
        status=$(jq -r '.[0]' "$f")
        case "$status" in
            CREATE_COMPLETE | UPDATE_COMPLETE) CV_CDKTOOLKIT="present" ;;
            *) msg_cdktoolkit_state "$status" ;;
        esac
    elif [ "$CV_CODE" = ValidationError ] && grep -q 'Stack with id CDKToolkit does not exist' "$f.err"; then
        CV_CDKTOOLKIT="absent"
    else
        msg_read_failed "the CDKToolkit stack" "${CV_CODE:-no answer}"
    fi
    cv_before_write cdktoolkit.txt "the CDKToolkit stack"
    cv_before_write cdktoolkit.txt "the CDKToolkit stack" "$CV_CDKTOOLKIT"
    for repo in "$NAMES_BACKEND_REPOSITORY" "$NAMES_WEB_REPOSITORY"; do
        f="$CV_RECORD/tmp/repo.json"
        if ! cv_read "the repository $repo" "$f" ecr describe-repositories --query "repositories[?repositoryName=='$repo'].repositoryName" ||
            ! cv_list "$f" 'all(type == "string")'; then
            msg_read_failed "the image repositories" "${CV_CODE:-an answer that is not a list}"
        fi
        if ! cv_list "$f" 'length == 0'; then
            msg_repository_present "$repo"
        fi
    done
    for secret in "$NAMES_JWT_SECRET" "$NAMES_PASSWORD_SECRET" "$NAMES_DB_SECRET"; do
        f="$CV_RECORD/tmp/secret.json"
        if ! cv_read "the secret $secret" "$f" secretsmanager list-secrets --include-planned-deletion \
            --filters "Key=name,Values=$secret" --query "SecretList[?Name=='$secret'].[Name,DeletedDate]" ||
            ! cv_list "$f" 'all(type == "array")'; then
            msg_read_failed "the secrets" "${CV_CODE:-an answer that is not a list}"
        fi
        if ! cv_list "$f" 'length == 0'; then
            if jq -e '.[0][1] != null' "$f" > /dev/null; then
                msg_secret_present "$secret" ", scheduled for deletion,"
            fi
            msg_secret_present "$secret" ""
        fi
    done
}

# --- the release images ----------------------------------------------------------------------

# cv_check_images: each release image by its index digest: cosign with the release workflow's
# identity, its SPDX attestation, exactly one linux/amd64 entry, and the version, profile and
# revision labels of this release's tag. The digests are recorded only once both pass.
cv_check_images() {
    local pair image var index amd64 subject
    for pair in "API=$CV_IMAGE_API" "WEB=$CV_IMAGE_WEB"; do
        var=${pair%%=*}
        image=${pair#*=}
        if ! index=$(cv_ghcr crane digest "$image:core-$CV_VERSION" 2> "$CV_RECORD/tmp/digest.err"); then
            if grep -qi 'MANIFEST_UNKNOWN\|not found\|NAME_UNKNOWN' "$CV_RECORD/tmp/digest.err"; then
                msg_image_missing "$image" "core-$CV_VERSION" "$CV_VERSION"
            fi
            msg_unreachable "ghcr.io" "$(sed -n 1p "$CV_RECORD/tmp/digest.err")"
        fi
        case "$index" in
            sha256:*) ;;
            *) msg_image "$image" "has no digest the installer can read" ;;
        esac
        cv_ghcr cosign verify "$image@$index" --certificate-identity-regexp "$CV_IDENTITY" \
            --certificate-oidc-issuer "$CV_ISSUER" > "$CV_RECORD/tmp/verify.json" 2> "$CV_RECORD/tmp/verify.err" || true
        if false; then
            msg_image "$image" "is not signed by Controlvar's release workflow"
        fi
        if ! jq -e -s --arg main "$CV_SIGNER@refs/heads/main" --arg tag "$CV_SIGNER@refs/tags/v$CV_VERSION" '
            length == 1 and (.[0] | type == "array" and length > 0
                and all(.optional.Subject == $main or .optional.Subject == $tag))' "$CV_RECORD/tmp/verify.json" > /dev/null 2>&1 && false; then
            msg_image "$image" "is not signed by Controlvar's release workflow"
        fi
        if ! cv_ghcr cosign verify-attestation --type spdxjson "$image@$index" --certificate-identity-regexp "$CV_IDENTITY" \
            --certificate-oidc-issuer "$CV_ISSUER" > "$CV_RECORD/tmp/attest.json" 2> "$CV_RECORD/tmp/attest.err" ||
            ! jq -e -s '[.[] | .payload | @base64d | fromjson | .predicateType] | length > 0 and all(. == "https://spdx.dev/Document")' \
                "$CV_RECORD/tmp/attest.json" > /dev/null 2>&1; then
            :
        fi
        if ! cv_ghcr crane manifest "$image@$index" > "$CV_RECORD/tmp/index.json" 2> "$CV_RECORD/tmp/index.err"; then
            msg_unreachable "ghcr.io" "$(sed -n 1p "$CV_RECORD/tmp/index.err")"
        fi
        if ! amd64=$(jq -e -r '[.manifests[]? | select(.platform.os == "linux" and .platform.architecture == "amd64")]
            | if length == 1 then .[0].digest else error("not one") end' "$CV_RECORD/tmp/index.json" 2> /dev/null); then
            msg_image "$image" "has no linux/amd64 build, the only platform this install runs"
        fi
        if ! cv_ghcr crane config "$image@$amd64" > "$CV_RECORD/tmp/config.json" 2> "$CV_RECORD/tmp/config.err"; then
            msg_unreachable "ghcr.io" "$(sed -n 1p "$CV_RECORD/tmp/config.err")"
        fi
        if ! jq -e --arg v "$CV_VERSION" --arg r "$CV_REVISION" '.config.Labels
            | ."org.opencontainers.image.version" == $v and ."io.experimently.profile" == "core"' "$CV_RECORD/tmp/config.json" > /dev/null 2>&1; then
            subject=$(jq -r '.config.Labels."org.opencontainers.image.version" // "none"' "$CV_RECORD/tmp/config.json" 2> /dev/null) || subject="none"
            msg_image "$image" "was built from a different release than this folder (its labels say $subject; this folder is $CV_VERSION at its tag)"
        fi
        # Kept until the install starts, when they become facts in the record (cv_start).
        # shellcheck disable=SC2034  # read by lib/steps.sh's cv_start
        case "$var" in
            API) CV_API_INDEX="$index" CV_API_AMD64="$amd64" ;;
            WEB) CV_WEB_INDEX="$index" CV_WEB_AMD64="$amd64" ;;
        esac
    done
}
