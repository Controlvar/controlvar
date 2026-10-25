# shellcheck shell=bash
# lib/env.sh: the environment every AWS, CDK, crane and cosign child runs under, the wrappers
# that start them, the sign-in checks and the guards. Sourced by controlvar-aws after
# lib/names.sh. Bash 3.2; every array expansion is guarded for set -u.
#
# Every child starts from `env -i` with exactly the variables below (cv_env_init): it never
# sees keys, profiles or settings from the reader's shell or ~/.aws/config, only the
# installer's own copy of one IAM Identity Center profile (cv_copy_config), and it runs with
# a HOME of its own (cv_home_init), so that only that copy and the shared sign-in cache
# reach it. The proxy and certificate variables are passed on purpose when the reader's
# shell sets them.

# The one region, environment and profile the beta installs.
CV_REGION="us-west-2"
CV_ENVIRONMENT="staging"
CV_CDK_PROFILE="core"
# The name the installer's copy of the reader's profile is written under, whatever the
# reader's profile is called (the record's own name): never `default`, which the CLI reads
# when no profile is named (the probe). Every aws child gets it as AWS_PROFILE and every cdk
# call as --profile.
CV_COPY_PROFILE="controlvar-aws"

# Passed to every child when set in the reader's shell: proxies and certificate bundles.
CV_PASSED_ON_PURPOSE=(
    HTTPS_PROXY https_proxy HTTP_PROXY http_proxy NO_PROXY no_proxy
    AWS_CA_BUNDLE SSL_CERT_FILE SSL_CERT_DIR NODE_EXTRA_CA_CERTS REQUESTS_CA_BUNDLE
)

# cv_env_init: builds the environment from the record (CV_RECORD) and the PATH the
# installer started with (CV_BASE_PATH). Call it again when CV_RECORD changes.
cv_env_init() {
    local name value
    CV_TOOLS_PATH="$CV_RECORD/tools/venv/bin:$CV_RECORD/tools/cdk-cli/node_modules/.bin:$CV_BASE_PATH"
    CV_CHILD_HOME="$CV_RECORD/home"
    CV_ENV=(
        "PATH=$CV_TOOLS_PATH"
        "HOME=$CV_CHILD_HOME"
        "AWS_CONFIG_FILE=$CV_RECORD/aws-config"
        "AWS_SHARED_CREDENTIALS_FILE=/dev/null"
        "AWS_PROFILE=$CV_COPY_PROFILE"
        "AWS_REGION=$CV_REGION"
        "AWS_EC2_METADATA_DISABLED=true"
        "AWS_PAGER="
        "AWS_DEFAULT_OUTPUT=json"
        "DOCKER_CONFIG=$CV_RECORD/no-login"
        "TUF_ROOT=$CV_RECORD/tools/sigstore"
        "TZ=UTC"
    )
    for name in ${CV_PASSED_ON_PURPOSE[@]+"${CV_PASSED_ON_PURPOSE[@]}"}; do
        if [ -n "${!name:-}" ]; then
            value="${!name}"
            CV_ENV+=("$name=$value")
        fi
    done
}

# cv_home_init: the children's HOME (CV_CHILD_HOME, in the record), made again on every run
# once the lock is held. Its only content is .aws/sso/cache, a link to the reader's own
# sign-in cache, shared on purpose: the CLI and the CDK read the profile's token there, and
# write it back when they renew it. Nothing else under the reader's HOME reaches a child:
# not ~/.aws/cli/alias, ~/.aws/models, ~/.cdk.json or any other file there. It removes
# only the record's own home/: a record that is not an absolute path of two parts or more
# (an empty one would make it /home) is refused first.
cv_home_init() {
    case "${CV_RECORD:-}" in
        /?*/?*) ;;
        *) msg_record_folder "${CV_RECORD:-(no record)}/home" ;;
    esac
    if [ "$CV_CHILD_HOME" != "$CV_RECORD/home" ] ||
        ! rm -rf "$CV_CHILD_HOME" || [ -e "$CV_CHILD_HOME" ] || [ -L "$CV_CHILD_HOME" ] ||
        ! mkdir "$CV_CHILD_HOME" "$CV_CHILD_HOME/.aws" "$CV_CHILD_HOME/.aws/sso" ||
        ! ln -s "$HOME/.aws/sso/cache" "$CV_CHILD_HOME/.aws/sso/cache"; then
        msg_record_folder "$CV_CHILD_HOME"
    fi
}

# cv_aws <arguments...>: the AWS CLI under the installer's environment.
cv_aws() {
    env -i ${CV_ENV[@]+"${CV_ENV[@]}"} aws "$@"
}

# cv_cdk <arguments...>: the pinned CDK CLI under the installer's environment, with the
# app's inputs (app.py reads them; the CLI sets the account and region itself). The CDK
# CLI's telemetry is off for the installer's calls.
cv_cdk() {
    env -i ${CV_ENV[@]+"${CV_ENV[@]}"} \
        "CDK_DISABLE_CLI_TELEMETRY=true" \
        "ENVIRONMENT=$CV_ENVIRONMENT" \
        "$NAMES_PROFILE_VARIABLE=$CV_CDK_PROFILE" \
        "ALARM_EMAIL=${CV_ALARM_EMAIL:-}" \
        "PUBLIC_BASE_URL=https://${CV_DOMAIN:-}" \
        "CERTIFICATE_ARN=${CERTIFICATE_ARN:-}" \
        "JWT_SECRET_ARN=${JWT_SECRET_ARN:-}" \
        "FIRST_SUPERUSER_PASSWORD_SECRET_ARN=${FIRST_SUPERUSER_PASSWORD_SECRET_ARN:-}" \
        cdk "$@"
}

# cv_ghcr <crane|cosign> <arguments...>: a read of the public release images, with no
# registry login at all.
cv_ghcr() {
    env -i ${CV_ENV[@]+"${CV_ENV[@]}"} "$@"
}

# cv_ecr <crane arguments...>: crane against the account's registry, with the login the
# copy step keeps in <record>/docker-config.
cv_ecr() {
    env -i ${CV_ENV[@]+"${CV_ENV[@]}"} "DOCKER_CONFIG=$CV_RECORD/docker-config" crane "$@"
}

# cv_script <script> <arguments...>: one of the release's own deploy scripts, which call aws
# themselves, under the installer's environment.
cv_script() {
    env -i ${CV_ENV[@]+"${CV_ENV[@]}"} bash "$@"
}

# cv_probe: with no profile, under the installer's environment, the CLI must find no
# credentials at all (exit 253, "Unable to locate credentials"). With no profile named the
# CLI reads [default], and the copy has none (CV_COPY_PROFILE). The call is aimed at a closed
# local port: keys the CLI did find go no further than that port (exit 255, "Could not
# connect to the endpoint URL"; scripts/check_installer_cli.py measures it). An Identity
# Center profile the CLI did find would first ask its sign-in portal for credentials
# (measured by hand, aws-cli 2.28.24, with a local stand-in for the portal), which is why
# the copy never holds a [default]. Exit 0 when it found none.
# No profile means no AWS_PROFILE at all: the CLI reads AWS_PROFILE= as a profile named ""
# ("The config profile () could not be found").
cv_probe() {
    local out="$CV_RECORD/tmp/probe.err" rc=0 entry kept=()
    for entry in ${CV_ENV[@]+"${CV_ENV[@]}"}; do
        case "$entry" in
            AWS_PROFILE=*) ;;
            *) kept+=("$entry") ;;
        esac
    done
    env -i ${kept[@]+"${kept[@]}"} aws sts get-caller-identity --endpoint-url http://127.0.0.1:9 > /dev/null 2> "$out" || rc=$?
    if [ "$rc" -eq 253 ] && grep -q 'Unable to locate credentials' "$out"; then
        return 0
    fi
    return 1
}

# cv_error_code <file>: the code an AWS error names in parentheses ("An error occurred
# (Code) when calling ..."), whichever form the CLI wrote the line in; empty if none.
cv_error_code() {
    local code
    code=$(sed -n 's/^.*An error occurred (\([A-Za-z0-9._-]*\)) when calling.*$/\1/p' "$1" 2> /dev/null) || code=""
    printf '%s' "${code%%$'\n'*}"
}

# mask: the account ID in a line becomes <acct>.
mask() {
    sed -E 's/[0-9]{12}/<acct>/g'
}

# --- the installer's own copy of the reader's profile ---------------------------------------

# cv_copy_config <reader config file> <profile> <copy>: writes <copy> with exactly six keys,
# under the profile name CV_COPY_PROFILE whatever <profile> is called: the profile's
# sso_session, sso_account_id and sso_role_name, and its session's sso_start_url, sso_region
# and sso_registration_scopes. Prints the session's name. Exit 3: no such profile; 4: the
# profile holds a way to sign in the beta refuses (named on stderr); 5: no sso_session; 6:
# the session is missing one of its keys; 7: a value runs over more than one line (named on
# stderr), which the copy would otherwise write as a line of its own.
cv_copy_config() {
    python3 -I - "$1" "$2" "$3" "$CV_COPY_PROFILE" << 'PYTHON'
import configparser
import os
import sys

source, profile, target, copy_profile = sys.argv[1:5]
parser = configparser.RawConfigParser()
try:
    parser.read(source)
except configparser.Error as error:
    print(f"the config file does not parse: {error.__class__.__name__}", file=sys.stderr)
    sys.exit(3)
section = "default" if profile == "default" else f"profile {profile}"
if not parser.has_section(section):
    sys.exit(3)
refused = [
    key
    for key in ("aws_access_key_id", "credential_process", "role_arn", "source_profile")
    if parser.has_option(section, key)
]
if refused:
    print(" ".join(refused), file=sys.stderr)
    sys.exit(4)
if not parser.has_option(section, "sso_session"):
    sys.exit(5)
session = parser.get(section, "sso_session").strip()
session_section = f"sso-session {session}"
keys = {}
for key in ("sso_account_id", "sso_role_name"):
    if not parser.has_option(section, key):
        print(key, file=sys.stderr)
        sys.exit(6)
    keys[key] = parser.get(section, key).strip()
session_keys = {}
for key in ("sso_start_url", "sso_region", "sso_registration_scopes"):
    if not parser.has_option(session_section, key):
        print(key, file=sys.stderr)
        sys.exit(6)
    session_keys[key] = parser.get(session_section, key).strip()
for key, value in [("sso_session", session), *keys.items(), *session_keys.items()]:
    if "\n" in value or "\r" in value:
        print(key, file=sys.stderr)
        sys.exit(7)
lines = [f"[profile {copy_profile}]", f"sso_session = {session}"]
lines += [f"{key} = {value}" for key, value in keys.items()]
lines += [f"[{session_section}]"]
lines += [f"{key} = {value}" for key, value in session_keys.items()]
partial = target + ".new"
with open(partial, "w") as handle:
    handle.write("\n".join(lines) + "\n")
os.chmod(partial, 0o600)
os.replace(partial, target)
print(session)
PYTHON
}

# --- the sign-in checks --------------------------------------------------------------------

# cv_token_rule <session>: the token cache of the profile's own sso_session holds a refresh
# token, was written in the last 15 minutes and has more than 30 minutes left. Prints only
# the cached token's expiresAt, and on a refusal how long ago it was renewed. Exit 0 or 1.
cv_token_rule() {
    python3 -I - "$1" "$HOME" << 'PYTHON'
import datetime
import hashlib
import json
import os
import sys

session, home = sys.argv[1:3]
name = hashlib.sha1(session.encode()).hexdigest() + ".json"
path = os.path.join(home, ".aws", "sso", "cache", name)
try:
    with open(path) as handle:
        token = json.load(handle)
    written = os.stat(path).st_mtime
    expires = token["expiresAt"]
    when = datetime.datetime.fromisoformat(expires.replace("UTC", "+00:00").replace("Z", "+00:00"))
except (OSError, ValueError, KeyError, TypeError):
    print("never")
    sys.exit(1)
now = datetime.datetime.now(datetime.timezone.utc)
minutes_ago = int((now.timestamp() - written) // 60)
left = (when - now).total_seconds()
print(f"expiresAt {expires}")
if not token.get("refreshToken") or minutes_ago > 15 or left <= 30 * 60:
    print(f"renewed {minutes_ago}")
    sys.exit(1)
PYTHON
}

# cv_refresh_token <session>: exit 0 when the profile's token cache still holds a refresh
# token (the session check's second half; no time window).
cv_refresh_token() {
    python3 -I - "$1" "$HOME" << 'PYTHON'
import hashlib
import json
import os
import sys

session, home = sys.argv[1:3]
name = hashlib.sha1(session.encode()).hexdigest() + ".json"
try:
    with open(os.path.join(home, ".aws", "sso", "cache", name)) as handle:
        sys.exit(0 if json.load(handle).get("refreshToken") else 1)
except (OSError, ValueError, AttributeError):
    sys.exit(1)
PYTHON
}

# --- the guards ------------------------------------------------------------------------------

# assert_account <expected account>: the profile signs in to exactly that account. Exit 0;
# 1 another account (CV_GOT holds it); 2 the identity could not be read (CV_GOT is empty).
assert_account() {
    CV_GOT=""
    if ! CV_GOT=$(cv_aws sts get-caller-identity --query Account --output text 2> "$CV_RECORD/tmp/identity.err"); then
        CV_GOT=""
        return 2
    fi
    CV_GOT=${CV_GOT%%$'\n'*}
    case "$CV_GOT" in
        '' | None | null | *[!0-9]*) CV_GOT=""; return 2 ;;
    esac
    if [ "${#CV_GOT}" -ne 12 ]; then
        CV_GOT=""
        return 2
    fi
    if [ "$CV_GOT" != "$1" ]; then
        return 1
    fi
    return 0
}

# assert_cdk_target <list file> <cdk output folder> <account>: the cdk call that wrote both
# lists exactly the nine staging core stacks, and the manifest in THAT call's own output
# folder puts every one of them in <account>, us-west-2. Exit 0, or 1 with the reason on
# stderr.
assert_cdk_target() {
    local listfile="$1" outdir="$2" want="aws://$3/$CV_REGION" listed expected names
    if [ "$CV_ENVIRONMENT" != staging ] || [ "$CV_CDK_PROFILE" != core ]; then
        printf 'the target is not staging and core\n' >&2
        return 1
    fi
    expected=$(printf '%s\n' ${NAMES_STACKS[@]+"${NAMES_STACKS[@]}"} | LC_ALL=C sort) || return 1
    if ! listed=$(grep -E "^${NAMES_STACK_PREFIX}" "$listfile"); then
        printf 'cdk list printed no stack\n' >&2
        return 1
    fi
    listed=$(printf '%s\n' "$listed" | LC_ALL=C sort) || return 1
    if [ "$listed" != "$expected" ]; then
        printf 'cdk list is not exactly the nine staging stacks\n' >&2
        return 1
    fi
    if [ ! -f "$outdir/manifest.json" ]; then
        printf 'the cdk call wrote no manifest in its output folder\n' >&2
        return 1
    fi
    if ! names=$(printf '%s\n' "$expected" | jq -R -s -c 'split("\n") | map(select(length > 0))'); then
        return 1
    fi
    if ! jq -e --arg want "$want" --argjson names "$names" '
        [.artifacts | to_entries[] | select(.value.type == "aws:cloudformation:stack")]
        | length == 9
          and all(.value.environment == $want)
          and ([.[].key] | sort) == ($names | sort)' "$outdir/manifest.json" > /dev/null; then
        printf 'not every stack in the manifest is one of the nine in %s\n' "$want" | mask >&2
        return 1
    fi
    return 0
}

# assert_first_admin_email: CV_ADMIN_EMAIL is one address, not at example.com.
assert_first_admin_email() {
    local e="${CV_ADMIN_EMAIL:-}" d
    case "$e" in
        "" | *[[:space:]]* | *@*@* | @* | *@) return 1 ;;
        *@*.*) ;;
        *) return 1 ;;
    esac
    d=$(printf '%s' "${e##*@}" | tr '[:upper:]' '[:lower:]')
    d=${d%.}
    case "$d" in
        example.com | *.example.com) return 1 ;;
    esac
    return 0
}
