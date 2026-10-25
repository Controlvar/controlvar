# shellcheck shell=bash
# lib/steps.sh: install's nine steps, the ones that change the account. Each runs the AWS
# guide's end-to-end run's step with that run's literals turned into the options, and each is
# announced before it changes anything. Once anything exists the installer never continues:
# a failure exits 1 and says what exists and what it bills; the next step is uninstall, then
# install. Sourced by controlvar-aws.

# Polls and caps, from the environment so a test can run them at once (no test asserts a
# duration). The defaults are the AWS guide's run's.
CV_POLL_SECONDS="${CONTROLVAR_AWS_POLL_SECONDS:-10}"
CV_TRIES="${CONTROLVAR_AWS_TRIES:-30}"
CV_CERT_MINUTES="${CONTROLVAR_AWS_CERT_MINUTES:-45}"
CV_WATCH_POLL_SECONDS="${CONTROLVAR_AWS_WATCH_POLL_SECONDS:-60}"
CV_WATCH_CAP_MINUTES="${CONTROLVAR_AWS_WATCH_CAP_MINUTES:-35}"
CV_WATCH_WAIT_MINUTES="${CONTROLVAR_AWS_WATCH_WAIT_MINUTES:-150}"
CV_TICK_SECONDS="${CONTROLVAR_AWS_TICK_SECONDS:-5}"
CV_HEARTBEAT_SECONDS="${CONTROLVAR_AWS_HEARTBEAT_SECONDS:-60}"
# Those settings, and uninstall's CV_STACK_WAIT_MINUTES: each takes a whole number.
CV_SETTINGS="POLL_SECONDS TRIES CERT_MINUTES WATCH_POLL_SECONDS WATCH_CAP_MINUTES WATCH_WAIT_MINUTES TICK_SECONDS HEARTBEAT_SECONDS STACK_WAIT_MINUTES"
# The tag that marks what this install made, written before the first change.
CV_TAG_KEY="controlvar-install-id"
CV_OUT=""
CV_CDK_N=0

# cv_check_settings: each CONTROLVAR_AWS_* setting (CV_SETTINGS) is a whole number of at
# most five digits, with no leading zero; anything else is refused before any call.
cv_check_settings() {
    local name var value
    for name in $CV_SETTINGS; do
        var="CV_$name"
        value="${!var:-}"
        case "$value" in
            0 | [1-9] | [1-9][0-9] | [1-9][0-9][0-9] | [1-9][0-9][0-9][0-9] | [1-9][0-9][0-9][0-9][0-9]) ;;
            *) msg_setting "CONTROLVAR_AWS_$name" "$value" ;;
        esac
    done
}

# cv_next_out: a new, empty output folder for one cdk call, in CV_OUT.
cv_next_out() {
    CV_CDK_N=$((CV_CDK_N + 1))
    while [ -e "$CV_RECORD/cdk-out/$CV_CDK_N" ]; do
        CV_CDK_N=$((CV_CDK_N + 1))
    done
    CV_OUT="$CV_RECORD/cdk-out/$CV_CDK_N"
    mkdir -p "$CV_OUT"
}

# cv_write <name> <file> <aws arguments...>: one change, as JSON into <file>; a failure ends
# the install (exit 1, what exists listed).
cv_write() {
    local name="$1" file="$2" rc=0
    shift 2
    cv_aws "$@" --output json > "$file" 2> "$file.err" || rc=$?
    CV_CODE=$(cv_error_code "$file.err")
    if [ "$rc" -ne 0 ]; then
        cv_log "$(sed -n '1,5p' "$file.err" 2> /dev/null)"
        msg_tool_failed "aws $1 $2 ($name)" "$rc" "${CV_CODE:-$(sed -n 2p "$file.err" 2> /dev/null)}"
    fi
}

# cv_string <file>: the one JSON string <file> holds, or exit 1.
cv_string() {
    jq -e -s -r 'if length == 1 and (.[0] | type) == "string" then .[0] else error("no string") end' "$1" 2> /dev/null
}

# cv_wait_change <change id>: Route 53 says the change is INSYNC. A read that fails stops
# the install; only a read that answers, and not yet INSYNC, is waited on.
cv_wait_change() {
    local i=0 f="$CV_RECORD/tmp/change.json" status
    while [ "$i" -lt "$CV_TRIES" ]; do
        i=$((i + 1))
        if ! cv_read "the change" "$f" route53 get-change --id "$1" --query ChangeInfo.Status; then
            msg_tool_failed "aws route53 get-change" "1" "${CV_CODE:-no answer}"
        fi
        if status=$(cv_string "$f") && [ "$status" = INSYNC ]; then
            return 0
        fi
        cv_sleep "$CV_POLL_SECONDS"
    done
    msg_tool_failed "Route 53" "1" "the change $1 was not INSYNC after $CV_TRIES reads"
}

# cv_record_change <n> <name> <type> <change json> <before json>: records/<n>.json, pending,
# written before its call, then the CREATE; the record is marked created once Route 53 has
# taken it. Never an UPSERT: a record already at the name makes the CREATE fail instead.
cv_record_change() {
    local n="$1" file="$CV_RECORD/records/$1.json" change="$CV_RECORD/records/$1-change.json" id
    if ! jq -n --arg name "$2" --arg type "$3" --arg zone "$CV_ZONE_ID" --argjson change "$4" --argjson before "$5" \
        '{name: $name, type: $type, zone_id: $zone, change: $change, before: $before, status: "pending"}' > "$file.new" ||
        ! mv "$file.new" "$file" || ! printf '%s\n' "$4" > "$change"; then
        msg_tool_failed "the record" "1" "records/$n.json could not be written"
    fi
    cv_write "create the record $2" "$CV_RECORD/tmp/change-$n.json" route53 change-resource-record-sets \
        --hosted-zone-id "$CV_ZONE_ID" --change-batch "file://$change" --query ChangeInfo.Id
    if ! id=$(cv_string "$CV_RECORD/tmp/change-$n.json"); then
        msg_tool_failed "aws route53 change-resource-record-sets" "1" "it named no change"
    fi
    if ! jq --arg id "$id" '.status = "created" | .change_id = $id' "$file" > "$file.new" || ! mv "$file.new" "$file"; then
        msg_tool_failed "the record" "1" "records/$n.json could not be written"
    fi
    cv_wait_change "$id"
}

# --- prepare ---------------------------------------------------------------------------------

# [1/9] The account as it is before anything is made (the AWS guide's "sweep before").
cv_step_sweep() {
    local rc=0 failed
    cv_step 1 "Record what is in the account now (read-only)"
    rm -rf "$CV_RECORD/sweep/before"
    : > "$CV_RECORD/tmp/sweep.out"
    : > "$CV_RECORD/tmp/sweep.err"
    cv_run_long "$CV_RECORD/tmp/sweep.out" "$CV_RECORD/tmp/sweep.err" \
        sweep_take "$CV_RECORD/sweep/before" "$CV_ACCOUNT" || rc=$?
    cv_log "$(cat "$CV_RECORD/tmp/sweep.out" "$CV_RECORD/tmp/sweep.err" 2> /dev/null)"
    case "$rc" in
        0) ;;
        1)
            failed=$(grep -rl '^READ FAILED: ' "$CV_RECORD/sweep/before" 2> /dev/null | sed "s#^$CV_RECORD/sweep/before/##" | LC_ALL=C sort | sed -n 1p)
            msg_sweep_failed "$(grep -rl '^READ FAILED: ' "$CV_RECORD/sweep/before" 2> /dev/null | wc -l | tr -d ' ') of its lists (the first: ${failed:-unknown})" "$CV_RECORD/sweep/before"
            ;;
        *) msg_tool_failed "the account's record" "$rc" "$(sed -n 1p "$CV_RECORD/tmp/sweep.err")" ;;
    esac
    cv_step_done 1
}

# STARTED and a new INSTALL_ID, immediately before the first change, then the verified
# image digests. An earlier install's record (uninstalled: UX-B9 refuses any other) moves to
# history/ first, so that nothing this install reports is the earlier one's.
cv_start() {
    local id old
    if ! id=$(openssl rand -hex 6) || ! printf '%s' "$id" | grep -Eq '^[0-9a-f]{12}$'; then
        msg_tool_failed "openssl rand" "1" "no install id"
    fi
    if [ -f "$CV_RECORD/inputs.sh" ] || [ -n "$(ls -A "$CV_RECORD/env.d" 2> /dev/null)" ] ||
        [ -n "$(ls -A "$CV_RECORD/records" 2> /dev/null)" ]; then
        old="$CV_RECORD/history/$(date -u +%Y%m%dT%H%M%SZ)-$id"
        if ! mkdir -p "$old" || ! mv "$CV_RECORD/env.d" "$CV_RECORD/records" "$old/" ||
            ! mkdir "$CV_RECORD/env.d" "$CV_RECORD/records" ||
            { [ -f "$CV_RECORD/inputs.sh" ] && ! mv "$CV_RECORD/inputs.sh" "$old/"; }; then
            msg_tool_failed "the record" "1" "the earlier install's record could not be moved to $old"
        fi
    fi
    INSTALL_ID="$id"
    if ! {
        printf 'export CV_ACCOUNT=%q CV_DOMAIN=%q CV_ALARM_EMAIL=%q CV_ADMIN_EMAIL=%q\n' \
            "$CV_ACCOUNT" "$CV_DOMAIN" "$CV_ALARM_EMAIL" "$CV_ADMIN_EMAIL"
        printf 'export CV_PROFILE=%q CV_VERSION=%q INSTALL_ID=%q\n' "$CV_PROFILE" "$CV_VERSION" "$INSTALL_ID"
    } > "$CV_RECORD/inputs.sh.new" || ! mv "$CV_RECORD/inputs.sh.new" "$CV_RECORD/inputs.sh"; then
        msg_tool_failed "the record" "1" "inputs.sh could not be written"
    fi
    if ! cv_put_fact STARTED "$(date -u +%Y-%m-%dT%H:%M:%SZ)"; then
        msg_tool_failed "the record" "1" "STARTED could not be written"
    fi
    if ! cv_put_fact ZONE_ID "$CV_ZONE_ID"; then
        msg_tool_failed "the record" "1" "ZONE_ID could not be written"
    fi
    if ! cv_put_fact API_INDEX "$CV_API_INDEX" || ! cv_put_fact API_AMD64 "$CV_API_AMD64" ||
        ! cv_put_fact WEB_INDEX "$CV_WEB_INDEX" || ! cv_put_fact WEB_AMD64 "$CV_WEB_AMD64"; then
        msg_tool_failed "the record" "1" "the image digests could not be written"
    fi
}

# [2/9] The CDK's setup stack, only when there is none. The intent is written first, so that
# uninstall knows this install made it; one that was here before is kept as it is.
cv_step_bootstrap() {
    local f="$CV_RECORD/tmp/cdktoolkit.json" rc=0 status
    cv_step 2 "Set up the CDK in us-west-2"
    if [ "$CV_CDKTOOLKIT" = present ]; then
        cv_out "  The CDK's setup stack (CDKToolkit) was already here; it is kept as it is."
        cv_step_done 2
        return 0
    fi
    if ! cv_put_fact CDKTOOLKIT_INTENT "$(date -u +%Y-%m-%dT%H:%M:%SZ)"; then
        msg_tool_failed "the record" "1" "CDKTOOLKIT_INTENT could not be written"
    fi
    cv_next_out
    mkdir -p "$CV_RECORD/tmp/bootstrap"
    cd "$CV_RECORD/tmp/bootstrap" || msg_tool_failed "cd" "1" "the bootstrap folder"
    : > "$CV_RECORD/logs/cdk-bootstrap.log"
    cv_run_long "$CV_RECORD/logs/cdk-bootstrap.log" "$CV_RECORD/logs/cdk-bootstrap.log" \
        cv_cdk bootstrap "aws://$CV_ACCOUNT/$CV_REGION" --profile "$CV_COPY_PROFILE" --output "$CV_OUT" || rc=$?
    cd "$CV_RECORD" || :
    if [ "$rc" -ne 0 ]; then
        msg_tool_failed "cdk bootstrap" "$rc" "$(tail -n 1 "$CV_RECORD/logs/cdk-bootstrap.log" 2> /dev/null)"
    fi
    if ! cv_read "CDKToolkit" "$f" cloudformation describe-stacks --stack-name CDKToolkit --query 'Stacks[0].[StackStatus,StackId]' ||
        ! cv_list "$f" 'length == 2 and all(type == "string")'; then
        msg_tool_failed "cdk bootstrap" "1" "the CDKToolkit stack could not be read back (${CV_CODE:-no list})"
    fi
    status=$(jq -r '.[0]' "$f")
    case "$status" in
        CREATE_COMPLETE | UPDATE_COMPLETE) ;;
        *) msg_tool_failed "cdk bootstrap" "1" "CDKToolkit is $status" ;;
    esac
    if ! cv_put_fact CDKTOOLKIT_STACK_ID "$(jq -r '.[1]' "$f")"; then
        msg_tool_failed "the record" "1" "CDKTOOLKIT_STACK_ID could not be written"
    fi
    cv_step_done 2
}

# [3/9] The two image repositories and the two secrets, each tagged with INSTALL_ID at its
# create. Each secret's value exists only in tmp/ until its create call, and goes to AWS only
# as file://, its bytes exact.
cv_step_repositories_and_secrets() {
    local repo n=0 f="$CV_RECORD/tmp/create.json" pair name file arn var description
    cv_step 3 "Create the two image repositories and the two secrets"
    for repo in "$NAMES_BACKEND_REPOSITORY" "$NAMES_WEB_REPOSITORY"; do
        cv_out "  Create the image repository $repo"
        cv_write "the repository $repo" "$f" ecr create-repository --repository-name "$repo" \
            --image-scanning-configuration scanOnPush=true --tags "Key=$CV_TAG_KEY,Value=$INSTALL_ID" \
            --query repository.repositoryName
        if ! jq -e -s --arg r "$repo" 'length == 1 and .[0] == $r' "$f" > /dev/null 2>&1; then
            msg_tool_failed "aws ecr create-repository" "1" "it did not name $repo"
        fi
        n=$((n + 1))
        cv_put_fact REPOSITORIES_CREATED "$n" || msg_tool_failed "the record" "1" "REPOSITORIES_CREATED"
    done
    if ! openssl rand -base64 48 | tr -d '\n' > "$CV_RECORD/tmp/jwt-secret" ||
        ! python3 -I -c 'import secrets, sys
alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnpqrstuvwxyz23456789"
sys.stdout.write("".join(secrets.choice(alphabet) for _ in range(24)))' > "$CV_RECORD/tmp/first-superuser-password" ||
        ! python3 -I -c 'import sys
for path, size in (sys.argv[1], 64), (sys.argv[2], 24):
    data = open(path, "rb").read()
    assert len(data) == size and data == data.strip() and data.isascii()' \
            "$CV_RECORD/tmp/jwt-secret" "$CV_RECORD/tmp/first-superuser-password"; then
        msg_tool_failed "the secret values" "1" "they could not be made"
    fi
    for pair in "JWT_SECRET_ARN=$NAMES_JWT_SECRET=jwt-secret" "FIRST_SUPERUSER_PASSWORD_SECRET_ARN=$NAMES_PASSWORD_SECRET=first-superuser-password"; do
        var=${pair%%=*}
        name=${pair#*=}
        file=${name#*=}
        name=${name%%=*}
        case "$file" in
            jwt-secret) description="JWT token signing secret for the Controlvar API" ;;
            *) description="Password for the first administrator account (FIRST_SUPERUSER_PASSWORD)" ;;
        esac
        cv_out "  Create the secret $name"
        cv_write "the secret $name" "$f" secretsmanager create-secret --name "$name" \
            --description "$description" --secret-string "file://$CV_RECORD/tmp/$file" \
            --tags "[{\"Key\":\"$CV_TAG_KEY\",\"Value\":\"$INSTALL_ID\"},{\"Key\":\"Environment\",\"Value\":\"$CV_ENVIRONMENT\"}]" \
            --query ARN
        rm -f "$CV_RECORD/tmp/$file"
        if ! arn=$(cv_string "$f"); then
            msg_tool_failed "aws secretsmanager create-secret" "1" "it named no ARN for $name"
        fi
        # Read back by name: the ARN and this install's tag, exactly.
        if ! cv_read "the secret $name" "$f" secretsmanager describe-secret --secret-id "$name" \
            --query "[ARN, Tags[?Key=='$CV_TAG_KEY'].Value | [0]]" ||
            ! jq -e -s --arg arn "$arn" --arg id "$INSTALL_ID" 'length == 1 and .[0] == [$arn, $id]' "$f" > /dev/null 2>&1; then
            msg_tool_failed "aws secretsmanager describe-secret" "1" "$name did not read back with its ARN and this install's tag"
        fi
        case "$arn" in
            "arn:aws:secretsmanager:$CV_REGION:$CV_ACCOUNT:secret:$name-"??????) ;;
            *) msg_tool_failed "aws secretsmanager create-secret" "1" "the ARN of $name is not a complete ARN" ;;
        esac
        cv_put_fact "$var" "$arn" || msg_tool_failed "the record" "1" "$var"
    done
    cv_step_done 3
}

# [4/9] One certificate for the one name, DNS-validated, its idempotency token INSTALL_ID;
# its validation record (CREATE only; an identical one already there is used and kept); and
# the ECS service-linked role when the checks read it as absent.
cv_step_certificate() {
    local f="$CV_RECORD/tmp/cert.json" i=0 name type value change before
    cv_step 4 "Request a certificate for $CV_DOMAIN and add its validation record"
    cv_write "the certificate" "$f" acm request-certificate --domain-name "$CV_DOMAIN" --validation-method DNS \
        --idempotency-token "$INSTALL_ID" --tags "Key=$CV_TAG_KEY,Value=$INSTALL_ID" --query CertificateArn
    if ! CERTIFICATE_ARN=$(cv_string "$f"); then
        msg_tool_failed "aws acm request-certificate" "1" "it named no certificate"
    fi
    case "$CERTIFICATE_ARN" in
        "arn:aws:acm:$CV_REGION:$CV_ACCOUNT:certificate/"*) ;;
        *) msg_tool_failed "aws acm request-certificate" "1" "the certificate is not in this account and us-west-2" ;;
    esac
    cv_put_fact CERTIFICATE_ARN "$CERTIFICATE_ARN" || msg_tool_failed "the record" "1" "CERTIFICATE_ARN"
    while :; do
        i=$((i + 1))
        if ! cv_read "the validation record" "$f" acm describe-certificate --certificate-arn "$CERTIFICATE_ARN" \
            --query 'Certificate.DomainValidationOptions[0].ResourceRecord'; then
            msg_tool_failed "aws acm describe-certificate" "1" "${CV_CODE:-no answer}"
        fi
        if jq -e -s 'length == 1 and (.[0] | type == "object" and .Type == "CNAME"
            and (.Name | type == "string") and (.Value | type == "string"))' "$f" > /dev/null 2>&1; then
            break
        fi
        if [ "$i" -ge "$CV_TRIES" ]; then
            msg_tool_failed "aws acm describe-certificate" "1" "no validation record after $CV_TRIES reads"
        fi
        cv_sleep "$CV_POLL_SECONDS"
    done
    name=$(jq -r '.Name' "$f")
    type=$(jq -r '.Type' "$f")
    value=$(jq -r '.Value' "$f")
    if ! cv_read "the records at $name" "$CV_RECORD/tmp/at-name.json" route53 list-resource-record-sets \
        --hosted-zone-id "$CV_ZONE_ID" --query "ResourceRecordSets[?Name=='$name'].[Type, ResourceRecords[0].Value]" ||
        ! cv_list "$CV_RECORD/tmp/at-name.json" 'all(type == "array")'; then
        msg_tool_failed "aws route53 list-resource-record-sets" "1" "the records at the validation name could not be read"
    fi
    before=$(jq -c '.' "$CV_RECORD/tmp/at-name.json")
    # shellcheck disable=SC2016  # $value is jq's own variable, given to it with --arg
    if cv_list "$CV_RECORD/tmp/at-name.json" 'length == 1 and .[0] == ["CNAME", $value]' --arg value "$value"; then
        cv_out "  The validation record $name was already there with ACM's value; it is used and kept."
    elif cv_list "$CV_RECORD/tmp/at-name.json" 'length == 0'; then
        change=$(jq -n -c --arg name "$name" --arg type "$type" --arg value "$value" '{
            Comment: "Controlvar installer: the certificate validation record",
            Changes: [{Action: "CREATE", ResourceRecordSet: {Name: $name, Type: $type, TTL: 300,
                ResourceRecords: [{Value: $value}]}}]}')
        cv_out "  Add the validation record $name"
        cv_record_change 1 "$name" "$type" "$change" "$before"
        cv_put_fact VALIDATION_RECORD "1" || msg_tool_failed "the record" "1" "VALIDATION_RECORD"
    else
        msg_validation_record "$name" "$(jq -r '.[0][0]' "$CV_RECORD/tmp/at-name.json")"
    fi
    cv_ensure_service_role
    cv_step_done 4
}

# The ECS service-linked role: no stack creates it, and a new account may have none when the
# services start. Created only when the checks read NoSuchEntity; "has been taken" means it
# is there.
cv_ensure_service_role() {
    local f="$CV_RECORD/tmp/slr.json" rc=0
    if ! grep -qx 'AWSServiceRoleForECS absent' "$CV_RECORD/before/service-linked-roles.txt"; then
        return 0
    fi
    cv_out "  Create the ECS service-linked role AWSServiceRoleForECS (free; AWS keeps it for the account)"
    cv_aws iam create-service-linked-role --aws-service-name ecs.amazonaws.com --query Role.RoleName --output json \
        > "$f" 2> "$f.err" || rc=$?
    if [ "$rc" -eq 0 ] && jq -e -s 'length == 1 and .[0] == "AWSServiceRoleForECS"' "$f" > /dev/null 2>&1; then
        cv_put_fact SERVICE_ROLE_CREATED "1" || :
        return 0
    fi
    if [ "$(cv_error_code "$f.err")" = InvalidInput ] && grep -q 'has been taken' "$f.err"; then
        cv_out "  AWSServiceRoleForECS is already there (AWS made it meanwhile)."
        return 0
    fi
    msg_service_role "$(cv_error_code "$f.err")"
}

# [5/9] Each verified image's linux/amd64 entry, by digest, into its repository as
# :bootstrap, read back equal; then the certificate, ISSUED.
cv_step_images() {
    local registry="$CV_ACCOUNT.dkr.ecr.$CV_REGION.amazonaws.com" pair repo image digest rc=0 f="$CV_RECORD/tmp/image.json"
    cv_step 5 "Copy the release images into the repositories, 253 MiB, and wait for the certificate"
    # The token goes from the CLI to crane through the pipe only, never an argument.
    if ! cv_aws ecr get-login-password 2> "$CV_RECORD/tmp/token.err" |
        cv_ecr auth login "$registry" -u AWS --password-stdin > "$CV_RECORD/tmp/login.out" 2>&1; then
        CV_CODE=$(cv_error_code "$CV_RECORD/tmp/token.err")
        msg_tool_failed "crane auth login" "1" "the registry login failed (${CV_CODE:-crane could not log in})"
    fi
    for pair in "$NAMES_BACKEND_REPOSITORY=$CV_IMAGE_API=$API_AMD64" "$NAMES_WEB_REPOSITORY=$CV_IMAGE_WEB=$WEB_AMD64"; do
        repo=${pair%%=*}
        image=${pair#*=}
        digest=${image#*=}
        image=${image%%=*}
        cv_out "  Copy $image@$digest to $repo:bootstrap"
        cv_run_long "$CV_RECORD/logs/crane-copy.log" "$CV_RECORD/logs/crane-copy.log" \
            cv_ecr copy "$image@$digest" "$registry/$repo:bootstrap" || rc=$?
        if [ "$rc" -ne 0 ]; then
            msg_tool_failed "crane copy" "$rc" "$(tail -n 1 "$CV_RECORD/logs/crane-copy.log" 2> /dev/null)"
        fi
    done
    cv_ecr auth logout "$registry" > /dev/null 2>&1 || :
    rm -rf "$CV_RECORD/docker-config"
    mkdir -p "$CV_RECORD/docker-config"
    for pair in "$NAMES_BACKEND_REPOSITORY=$API_AMD64" "$NAMES_WEB_REPOSITORY=$WEB_AMD64"; do
        repo=${pair%%=*}
        digest=${pair#*=}
        if ! cv_read "the image in $repo" "$f" ecr describe-images --repository-name "$repo" \
            --image-ids imageTag=bootstrap --query 'imageDetails[0].imageDigest' ||
            ! jq -e -s --arg d "$digest" 'length == 1 and .[0] == $d' "$f" > /dev/null 2>&1; then
            msg_image_copied_wrong "$repo"
        fi
    done
    cv_put_fact IMAGES_COPIED "1" || msg_tool_failed "the record" "1" "IMAGES_COPIED"
    cv_wait_certificate
    cv_step_done 5
}

# The certificate, ISSUED within 45 minutes; FAILED, VALIDATION_TIMED_OUT or REVOKED stop the
# install with ACM's reason (a CAA record that does not allow Amazon shows here).
cv_wait_certificate() {
    local f="$CV_RECORD/tmp/status.json" deadline status reason
    deadline=$(($(date +%s) + CV_CERT_MINUTES * 60))
    while :; do
        if ! cv_read "the certificate" "$f" acm describe-certificate --certificate-arn "$CERTIFICATE_ARN" \
            --query '[Certificate.Status, Certificate.FailureReason]'; then
            msg_tool_failed "aws acm describe-certificate" "1" "${CV_CODE:-no answer}"
        fi
        if cv_list "$f" 'length == 2 and (.[0] | type == "string")'; then
            status=$(jq -r '.[0]' "$f")
            case "$status" in
                ISSUED) return 0 ;;
                FAILED | VALIDATION_TIMED_OUT | REVOKED)
                    reason=$(jq -r '.[1] // "no reason given"' "$f")
                    msg_certificate_failed "$CV_DOMAIN" "$status" "$reason"
                    ;;
            esac
        fi
        if [ "$(date +%s)" -ge "$deadline" ]; then
            msg_certificate_timeout "$CV_DOMAIN" "${status:-unread}" "$(jq -r '.name' "$CV_RECORD/records/1.json" 2> /dev/null || printf 'its validation name')"
        fi
        cv_sleep "$CV_POLL_SECONDS"
    done
}

# --- deploy ----------------------------------------------------------------------------------

# [6/9] cdk list in a fresh output folder, the target asserted on THAT folder's manifest; the
# session check (no time window); DEPLOY_STARTED; then cdk deploy of that very assembly
# (--app names the folder, so nothing is synthesised again), with the service watcher
# beside it and a line a minute.
cv_step_deploy() {
    local rc=0 list="$CV_RECORD/tmp/cdk-list.txt" err="$CV_RECORD/tmp/cdk-list.err" deploy_pid watch_pid why app
    cv_step 6 "Create the nine stacks, about 24 minutes"
    cd "$CV_RECORD/src/infrastructure/cdk" || msg_tool_failed "cd" "1" "the release's CDK folder"
    cv_next_out
    app="$CV_OUT"
    : > "$list"
    : > "$err"
    cv_run_long "$list" "$err" cv_cdk list --profile "$CV_COPY_PROFILE" --output "$app" || rc=$?
    cv_log "$(cat "$list" "$err" 2> /dev/null)"
    if [ "$rc" -ne 0 ]; then
        msg_tool_failed "cdk list" "$rc" "$(tail -n 1 "$err" 2> /dev/null)"
    fi
    if ! grep -Fqx -- "[cdk] $NAMES_PROFILE_LINE" "$list" "$err"; then
        msg_target "the app did not say it builds the core profile"
    fi
    if ! why=$(assert_cdk_target "$list" "$app" "$CV_ACCOUNT" 2>&1); then
        msg_target "$why"
    fi
    cv_session_check
    cv_put_fact DEPLOY_STARTED "$(date -u +%Y-%m-%dT%H:%M:%SZ)" || msg_tool_failed "the record" "1" "DEPLOY_STARTED"
    cv_next_out
    : > "$CV_RECORD/logs/cdk-deploy.log"
    : > "$CV_RECORD/tmp/watch.out"
    cv_bg "$CV_RECORD/logs/cdk-deploy.log" "$CV_RECORD/logs/cdk-deploy.log" \
        cv_cdk deploy --all --require-approval never --app "$app" --profile "$CV_COPY_PROFILE" --output "$CV_OUT"
    deploy_pid=$CV_CHILD
    cv_bg "$CV_RECORD/tmp/watch.out" "$CV_RECORD/tmp/watch.out" cv_watch
    watch_pid=$CV_CHILD
    cv_deploy_wait "$deploy_pid" "$watch_pid"
    cd "$CV_RECORD" || :
    cv_put_fact DEPLOY_DONE "$(date -u +%Y-%m-%dT%H:%M:%SZ)" || msg_tool_failed "the record" "1" "DEPLOY_DONE"
    cv_check_nine_stacks
    cv_step_done 6
}

# The session check before cdk deploy: the profile still signs in (the CLI renews if it must)
# and the token cache still holds a refresh token. No time window: the cache's time stays the
# login's for most of an hour.
cv_session_check() {
    local f="$CV_RECORD/tmp/session.json" code
    if ! cv_read "the sign-in" "$f" sts get-caller-identity --query Account ||
        ! jq -e -s --arg a "$CV_ACCOUNT" 'length == 1 and .[0] == $a' "$f" > /dev/null 2>&1; then
        code=$(cv_error_code "$f.err")
        msg_session_ended "$CV_PROFILE" "${code:-the sign-in could not be read}"
    fi
    if ! cv_refresh_token "$CV_SESSION"; then
        msg_session_ended "$CV_PROFILE" "no refresh token in its cache"
    fi
}

# cv_deploy_wait <deploy pid> <watcher pid>: until cdk deploy ends; a watcher that gives up
# stops the deploy (CloudFormation carries on by itself).
cv_deploy_wait() {
    local deploy_pid="$1" watch_pid="$2" rc=0 last=0 now exit_line
    while :; do
        if ! kill -0 "$deploy_pid" 2> /dev/null; then
            cv_wait "$deploy_pid" || rc=$?
            break
        fi
        if [ -n "$watch_pid" ] && ! kill -0 "$watch_pid" 2> /dev/null; then
            cv_wait "$watch_pid" || :
            watch_pid=""
            exit_line=$(grep '^EXIT ' "$CV_RECORD/tmp/watch.out" | sed -n 1p)
            cv_log "watcher: $exit_line"
            case "$exit_line" in
                "EXIT complete"*) ;;
                *)
                    cv_kill "$deploy_pid"
                    cv_wait "$deploy_pid" || :
                    cv_watch_failed "$exit_line"
                    ;;
            esac
        fi
        now=$(date +%s)
        if [ $((now - last)) -ge "$CV_HEARTBEAT_SECONDS" ]; then
            last=$now
            cv_heartbeat
        fi
        cv_sleep "$CV_TICK_SECONDS"
    done
    if [ -n "$watch_pid" ]; then
        cv_kill "$watch_pid"
        cv_wait "$watch_pid" || :
    fi
    if [ "$rc" -ne 0 ]; then
        cv_deploy_failed "$rc"
    fi
}

# cv_heartbeat: "10:31 UTC  4 of 9 done; creating fargate, migrations". A read that fails
# prints nothing: it decides nothing.
cv_heartbeat() {
    local f="$CV_RECORD/tmp/heartbeat.json" done_n creating
    if ! cv_read "the stacks" "$f" cloudformation list-stacks \
        --query "StackSummaries[?starts_with(StackName, '$NAMES_STACK_PREFIX')].[StackName,StackStatus]" ||
        ! cv_list "$f" 'all(type == "array")'; then
        return 0
    fi
    done_n=$(jq '[.[] | select(.[1] == "CREATE_COMPLETE" or .[1] == "UPDATE_COMPLETE")] | length' "$f") || return 0
    creating=$(jq -r --arg p "$NAMES_STACK_PREFIX" --arg s "-$CV_ENVIRONMENT" \
        '[.[] | select(.[1] | endswith("_IN_PROGRESS")) | .[0] | ltrimstr($p) | rtrimstr($s)] | join(", ")' "$f")
    cv_out "$(date -u +%H:%M) UTC  $done_n of 9 done${creating:+; creating $creating}"
}

# cv_watch_failed <EXIT line>: the watcher's reason, as the message that ends the install.
cv_watch_failed() {
    case "$1" in
        "EXIT rule: "*) msg_api_cannot_start "${1#EXIT rule: }" ;;
        "EXIT cap: "*) msg_watch_cap "$NAMES_FARGATE_STACK" "$CV_WATCH_CAP_MINUTES" ;;
        "EXIT read: "*) msg_watch_read "$NAMES_FARGATE_STACK" "${1#EXIT read: }" ;;
        "EXIT status: "*) msg_stack_failed "$NAMES_FARGATE_STACK" "${1#EXIT status: }" ;;
        "EXIT wait: "*) msg_watch_cap "$NAMES_FARGATE_STACK" "$CV_WATCH_WAIT_MINUTES" ;;
        *) msg_watch_read "$NAMES_FARGATE_STACK" "the watcher stopped without a reason" ;;
    esac
}

# cv_deploy_failed <exit status>: cdk deploy failed; the first stack AWS stopped creating,
# with its first failed resource and AWS's own reason.
cv_deploy_failed() {
    local f="$CV_RECORD/tmp/failed.json" stack reason
    if cv_read "the stacks" "$f" cloudformation list-stacks \
        --query "StackSummaries[?starts_with(StackName, '$NAMES_STACK_PREFIX') && (ends_with(StackStatus, 'FAILED') || contains(StackStatus, 'ROLLBACK'))].StackName" &&
        cv_list "$f" 'length > 0 and all(type == "string")'; then
        stack=$(jq -r '.[0]' "$f")
        if cv_read "the events of $stack" "$f" cloudformation describe-stack-events --stack-name "$stack" \
            --query "StackEvents[?ends_with(ResourceStatus, 'FAILED')] | [0].[LogicalResourceId, ResourceStatusReason]" &&
            cv_list "$f" 'length == 2'; then
            reason=$(jq -r '"\(.[0]) failed with: \(.[1] // "no reason given" | .[0:300])"' "$f")
        fi
        msg_stack_failed "$stack" "${reason:-the log has its events}"
    fi
    msg_tool_failed "cdk deploy" "$1" "$(tail -n 1 "$CV_RECORD/logs/cdk-deploy.log" 2> /dev/null)"
}

# cv_watch: the AWS guide's run's service watcher, read-only, beside cdk deploy. It exits on
# the first of: two stopped API tasks with the same reason, or a service event about the
# service-linked role (EXIT rule); the stack leaving CREATE_IN_PROGRESS (EXIT complete, or EXIT
# status); the cap after it entered CREATE_IN_PROGRESS (EXIT cap); the stack still absent at
# the wait limit (EXIT wait); a read of the stack that fails for any reason but "does not
# exist" (EXIT read).
cv_watch() {
    local started now since="" status f="$CV_RECORD/tmp/watch-stack.json" tasks reasons dup events sleeper=""
    trap 'if [ -n "$sleeper" ]; then kill "$sleeper" 2> /dev/null; fi; exit 143' TERM
    started=$(date +%s)
    while :; do
        now=$(date +%s)
        if cv_aws cloudformation describe-stacks --stack-name "$NAMES_FARGATE_STACK" --query 'Stacks[0].StackStatus' \
            --output json > "$f" 2> "$f.err"; then
            status=$(cv_string "$f") || status="UNREADABLE"
        elif [ "$(cv_error_code "$f.err")" = ValidationError ] && grep -q 'does not exist' "$f.err"; then
            status="ABSENT"
        else
            printf 'EXIT read: %s\n' "$(cv_error_code "$f.err")"
            exit 1
        fi
        case "$status" in
            ABSENT)
                if [ $((now - started)) -ge $((CV_WATCH_WAIT_MINUTES * 60)) ]; then
                    printf 'EXIT wait: %s not created after %s minutes\n' "$NAMES_FARGATE_STACK" "$CV_WATCH_WAIT_MINUTES"
                    exit 1
                fi
                ;;
            CREATE_IN_PROGRESS)
                if [ -z "$since" ]; then since=$now; fi
                reasons="$CV_RECORD/tmp/watch-reasons.json"
                : > "$reasons"
                if cv_aws ecs list-tasks --cluster "$NAMES_CLUSTER" --desired-status STOPPED --query taskArns \
                    --output json > "$CV_RECORD/tmp/watch-tasks.json" 2> /dev/null &&
                    tasks=$(jq -r 'if type == "array" then .[] else empty end' "$CV_RECORD/tmp/watch-tasks.json" 2> /dev/null) &&
                    [ -n "$tasks" ]; then
                    # shellcheck disable=SC2086  # one argument per task
                    cv_aws ecs describe-tasks --cluster "$NAMES_CLUSTER" --tasks $tasks \
                        --query "tasks[?contains(taskDefinitionArn, ':task-definition/${NAMES_FAMILIES[0]}:')].stoppedReason" \
                        --output json > "$reasons" 2> /dev/null || :
                fi
                dup=$(jq -r 'if type == "array" then (group_by(.) | map(select(length > 1)) | .[0][0] // empty) else empty end' "$reasons" 2> /dev/null) || dup=""
                if [ -n "$dup" ]; then
                    printf 'EXIT rule: two stopped API tasks give the same reason: %s\n' "$dup"
                    exit 1
                fi
                events=$(cv_aws ecs describe-services --cluster "$NAMES_CLUSTER" --services "$NAMES_API_SERVICE" \
                    --query 'services[0].events[0:5].message' --output json 2> /dev/null) || events=""
                case "$(printf '%s' "$events" | tr '[:upper:]' '[:lower:]')" in
                    *service?linked?role*)
                        printf 'EXIT rule: a service event names the service-linked role\n'
                        exit 1
                        ;;
                esac
                if [ $((now - since)) -ge $((CV_WATCH_CAP_MINUTES * 60)) ]; then
                    printf 'EXIT cap: %s still CREATE_IN_PROGRESS %s minutes after it started\n' "$NAMES_FARGATE_STACK" "$CV_WATCH_CAP_MINUTES"
                    exit 1
                fi
                ;;
            CREATE_COMPLETE)
                printf 'EXIT complete: %s CREATE_COMPLETE\n' "$NAMES_FARGATE_STACK"
                exit 0
                ;;
            *)
                printf 'EXIT status: %s is %s\n' "$NAMES_FARGATE_STACK" "$status"
                exit 1
                ;;
        esac
        sleep "$CV_WATCH_POLL_SECONDS" &
        sleeper=$!
        wait "$sleeper" || :
        sleeper=""
    done
}

# The nine stacks are complete, exactly, and the fargate stack has its outputs.
cv_check_nine_stacks() {
    local f="$CV_RECORD/tmp/nine.json" expected
    expected=$(printf '%s\n' ${NAMES_STACKS[@]+"${NAMES_STACKS[@]}"} | jq -R -s -c 'split("\n") | map(select(length > 0)) | sort')
    if ! cv_read "the stacks" "$f" cloudformation list-stacks \
        --query "StackSummaries[?starts_with(StackName, '$NAMES_STACK_PREFIX') && StackStatus!='DELETE_COMPLETE'].[StackName,StackStatus]" ||
        ! cv_list "$f" 'all(type == "array")'; then
        msg_stacks_not_complete "their list could not be read (${CV_CODE:-no list})"
    fi
    if ! jq -e --argjson want "$expected" '([.[][0]] | sort) == $want
        and all(.[]; .[1] == "CREATE_COMPLETE" or .[1] == "UPDATE_COMPLETE")' "$f" > /dev/null 2>&1; then
        msg_stacks_not_complete "$(jq -r '[.[] | "\(.[0]) \(.[1])"] | join(", ")' "$f" 2> /dev/null)"
    fi
    f="$CV_RECORD/tmp/outputs.json"
    if ! cv_read "the outputs" "$f" cloudformation describe-stacks --stack-name "$NAMES_FARGATE_STACK" --query 'Stacks[0].Outputs' ||
        ! cv_list "$f" 'all(type == "object")'; then
        msg_stacks_not_complete "the outputs of $NAMES_FARGATE_STACK could not be read"
    fi
    if ! jq -e --arg want "$(printf '%s\n' ${NAMES_FARGATE_OUTPUTS[@]+"${NAMES_FARGATE_OUTPUTS[@]}"})" '
        ([.[].OutputKey]) as $have | ($want | split("\n") | map(select(length > 0))) | all(. as $k | $have | index($k) != null)' \
        "$f" > /dev/null 2>&1; then
        msg_stacks_not_complete "$NAMES_FARGATE_STACK does not have every output it should"
    fi
    CV_TASK_SUBNETS=$(jq -r '.[] | select(.OutputKey == "TaskSubnets") | .OutputValue' "$f")
    CV_TASK_GROUP=$(jq -r '.[] | select(.OutputKey == "TaskSecurityGroup") | .OutputValue' "$f")
    CV_ALB_OUTPUT=$(jq -r '.[] | select(.OutputKey == "ALBDnsName") | .OutputValue' "$f")
    f="$CV_RECORD/tmp/topic.json"
    CV_SUBSCRIPTION="not read"
    if cv_read "the alarm topic" "$f" sns list-topics --query "Topics[?ends_with(TopicArn, ':$NAMES_ALARM_TOPIC')].TopicArn" &&
        cv_list "$f" 'length == 1' &&
        cv_read "the subscription" "$CV_RECORD/tmp/subscription.json" sns list-subscriptions-by-topic \
            --topic-arn "$(jq -r '.[0]' "$f")" --query 'Subscriptions[].SubscriptionArn' &&
        cv_list "$CV_RECORD/tmp/subscription.json" 'all(type == "string")'; then
        if cv_list "$CV_RECORD/tmp/subscription.json" 'any(.[]; . == "PendingConfirmation")'; then
            CV_SUBSCRIPTION="waiting for its confirmation"
        else
            CV_SUBSCRIPTION="confirmed"
        fi
    fi
}

# --- finish ----------------------------------------------------------------------------------

# cv_http <name> <path> [<curl arguments>...]: one HTTPS request to the install, through an
# address the zone's own name servers gave (never this computer's resolver). The status is
# CV_HTTP; the body is tmp/<name>.out.
cv_http() {
    local name="$1" path="$2"
    shift 2
    CV_HTTP=$(curl --resolve "$CV_DOMAIN:443:$CV_IP" -sS -o "$CV_RECORD/tmp/$name.out" -w '%{http_code}' \
        --max-time 30 "$@" "https://$CV_DOMAIN$path" 2> "$CV_RECORD/tmp/$name.err") || CV_HTTP="000"
}

# [7/9] The one alias record (CREATE only, the name read again first), then the name answering
# from the zone's own name servers.
cv_step_alias() {
    local f="$CV_RECORD/tmp/alb.json" change before ns i=0
    cv_step 7 "Point $CV_DOMAIN at the load balancer"
    if ! cv_read "the load balancer" "$f" elbv2 describe-load-balancers --names "$NAMES_LOAD_BALANCER" \
        --query 'LoadBalancers[0].[DNSName,CanonicalHostedZoneId]' ||
        ! cv_list "$f" 'length == 2 and all(type == "string")'; then
        msg_tool_failed "aws elbv2 describe-load-balancers" "1" "the load balancer could not be read (${CV_CODE:-no list})"
    fi
    CV_ALB_DNS=$(jq -r '.[0]' "$f")
    CV_ALB_ZONE=$(jq -r '.[1]' "$f")
    if [ "$CV_ALB_DNS" != "$CV_ALB_OUTPUT" ]; then
        msg_tool_failed "aws elbv2 describe-load-balancers" "1" "the load balancer is not the one the fargate stack names"
    fi
    cv_put_fact LOAD_BALANCER "$CV_ALB_DNS" || msg_tool_failed "the record" "1" "LOAD_BALANCER"
    if ! cv_read "the records at $CV_DOMAIN" "$CV_RECORD/tmp/host.json" route53 list-resource-record-sets \
        --hosted-zone-id "$CV_ZONE_ID" --query "ResourceRecordSets[?Name=='$CV_DOMAIN.'].Type" ||
        ! cv_list "$CV_RECORD/tmp/host.json" 'all(type == "string")'; then
        msg_tool_failed "aws route53 list-resource-record-sets" "1" "the records at $CV_DOMAIN could not be read"
    fi
    if ! cv_list "$CV_RECORD/tmp/host.json" 'length == 0'; then
        msg_alias_record "$CV_DOMAIN" "$(jq -r '.[0]' "$CV_RECORD/tmp/host.json")"
    fi
    before=$(jq -c '.' "$CV_RECORD/tmp/host.json")
    change=$(jq -n -c --arg name "$CV_DOMAIN." --arg zone "$CV_ALB_ZONE" --arg dns "$CV_ALB_DNS" '{
        Comment: "Controlvar installer: the address",
        Changes: [{Action: "CREATE", ResourceRecordSet: {Name: $name, Type: "A",
            AliasTarget: {HostedZoneId: $zone, DNSName: $dns, EvaluateTargetHealth: false}}}]}')
    cv_out "  Add the record $CV_DOMAIN (an alias to the load balancer)"
    cv_record_change 2 "$CV_DOMAIN." "A" "$change" "$before"
    cv_put_fact ALIAS_RECORD "2" || msg_tool_failed "the record" "1" "ALIAS_RECORD"
    ns=$(sed -n 1p "$CV_RECORD/before/zone-ns.txt")
    CV_IP=""
    while [ "$i" -lt "$CV_TRIES" ]; do
        i=$((i + 1))
        CV_IP=$(dig @"$ns" "$CV_DOMAIN" A +short 2> /dev/null | grep -E '^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$' | sed -n 1p) || CV_IP=""
        if [ -n "$CV_IP" ]; then
            cv_http health /health
            if [ "$CV_HTTP" = 200 ]; then
                break
            fi
        fi
        cv_sleep "$CV_POLL_SECONDS"
    done
    if [ -z "$CV_IP" ] || [ "$CV_HTTP" != 200 ]; then
        msg_check_failed "https://$CV_DOMAIN/health, through the zone's own name server $ns," "${CV_HTTP:-no address}"
    fi
    cv_step_done 7
}

# [8/9] The migration revision, by the verified digest; the migration task, which names the
# first administrator (FIRST_SUPERUSER); the decisive check is the line the bootstrap logs.
cv_step_schema() {
    local registry="$CV_ACCOUNT.dkr.ecr.$CV_REGION.amazonaws.com" arn rc=0 network overrides admin
    cv_step 8 "Create the database schema and the first administrator"
    if ! assert_first_admin_email; then
        msg_tool_failed "the first administrator's address" "1" "$CV_ADMIN_EMAIL is not one address"
    fi
    if ! arn=$(cv_script "$CV_RECORD/src/scripts/register_task_definition.sh" "$NAMES_MIGRATE_FAMILY" \
        "$registry/$NAMES_BACKEND_REPOSITORY@$API_AMD64" 2> "$CV_RECORD/tmp/register.err"); then
        cv_log "$(cat "$CV_RECORD/tmp/register.err")"
        msg_tool_failed "register_task_definition.sh" "1" "$(sed -n 1p "$CV_RECORD/tmp/register.err")"
    fi
    case "$arn" in
        "arn:aws:ecs:$CV_REGION:$CV_ACCOUNT:task-definition/$NAMES_MIGRATE_FAMILY:"[0-9]*) ;;
        *) msg_tool_failed "register_task_definition.sh" "1" "it named no revision of $NAMES_MIGRATE_FAMILY" ;;
    esac
    cv_put_fact MIGRATE_TD_ARN "$arn" || msg_tool_failed "the record" "1" "MIGRATE_TD_ARN"
    network=$(jq -c -n --arg subnets "$CV_TASK_SUBNETS" --arg sg "$CV_TASK_GROUP" \
        '{awsvpcConfiguration: {subnets: ($subnets | split(",")), securityGroups: [$sg], assignPublicIp: "DISABLED"}}')
    overrides=$(jq -c -n --arg admin "$CV_ADMIN_EMAIL" --arg name "$NAMES_BACKEND_CONTAINER" \
        '{containerOverrides: [{name: $name, command: ["python", "-m", "backend.app.db.bootstrap"],
          environment: [{name: "FIRST_SUPERUSER", value: $admin}]}]}')
    cv_out "  Run the database setup task (it names the first administrator, $CV_ADMIN_EMAIL)"
    : > "$CV_RECORD/tmp/migration.out"
    cv_run_long "$CV_RECORD/tmp/migration.out" "$CV_RECORD/tmp/migration.out" \
        cv_script "$CV_RECORD/src/scripts/run_migration_task.sh" "$NAMES_CLUSTER" "$arn" "$network" \
        "$overrides" "$NAMES_MIGRATE_LOG_GROUP" || rc=$?
    cv_log "$(cat "$CV_RECORD/tmp/migration.out")"
    admin=$(printf '%s' "$CV_ADMIN_EMAIL" | tr '[:upper:]' '[:lower:]')
    if [ "$rc" -ne 0 ]; then
        msg_migration_failed "$admin" "the task did not exit 0"
    fi
    if ! grep -Fq "created the first administrator $admin (role ADMIN)" "$CV_RECORD/tmp/migration.out"; then
        msg_migration_failed "$admin" "its log does not say it created that administrator"
    fi
    if ! grep -Eq 'Database bootstrap complete \((created|upgraded)\)' "$CV_RECORD/tmp/migration.out"; then
        msg_migration_failed "$admin" "its log does not say the schema is complete"
    fi
    cv_put_fact ADMIN_CREATED "$admin" || msg_tool_failed "the record" "1" "ADMIN_CREATED"
    cv_step_done 8
}

# [9/9] From the internet, through the zone's own name server: the release and profile, the
# health of the database and the cache, the administrator's sign-in, an API key, a flag, the
# flag evaluated with the key (which needs the schema) and refused without it; the key revoked.
cv_step_smoke() {
    local admin key_id
    cv_step 9 "Check the install from the internet"
    admin=$(printf '%s' "$CV_ADMIN_EMAIL" | tr '[:upper:]' '[:lower:]')
    cv_http modules /api/v1/modules
    if [ "$CV_HTTP" != 200 ] || ! jq -e --arg v "$CV_VERSION" '.profile == "core" and .modules == [] and .version == $v' \
        "$CV_RECORD/tmp/modules.out" > /dev/null 2>&1; then
        msg_check_failed "GET /api/v1/modules" "$CV_HTTP"
    fi
    cv_http health /health
    if [ "$CV_HTTP" != 200 ] || ! jq -e --arg v "$CV_VERSION" '.status == "healthy" and .profile == "core"
        and .version == $v and .environment == "staging" and .checks.database.status == "healthy"
        and .checks.redis.status == "healthy"' "$CV_RECORD/tmp/health.out" > /dev/null 2>&1; then
        msg_check_failed "GET /health" "$CV_HTTP"
    fi
    if ! cv_read "the password" "$CV_RECORD/tmp/password.json" secretsmanager get-secret-value \
        --secret-id "$FIRST_SUPERUSER_PASSWORD_SECRET_ARN" --query SecretString ||
        ! jq -e -s 'length == 1 and (.[0] | type == "string")' "$CV_RECORD/tmp/password.json" > /dev/null 2>&1 ||
        ! jq -n --arg email "$admin" --slurpfile secret "$CV_RECORD/tmp/password.json" \
            '{email: $email, password: $secret[0]}' > "$CV_RECORD/tmp/login.json"; then
        msg_check_failed "reading the first administrator's password back from Secrets Manager" "${CV_CODE:-no answer}"
    fi
    rm -f "$CV_RECORD/tmp/password.json"
    cv_http login /api/v1/auth/login -H 'Content-Type: application/json' --data-binary "@$CV_RECORD/tmp/login.json"
    rm -f "$CV_RECORD/tmp/login.json"
    if [ "$CV_HTTP" != 200 ] || ! jq -e --arg e "$admin" '.user | .email == $e and .role == "ADMIN" and .is_superuser == true' \
        "$CV_RECORD/tmp/login.out" > /dev/null 2>&1 ||
        ! jq -r '"Authorization: Bearer " + .access_token' "$CV_RECORD/tmp/login.out" > "$CV_RECORD/tmp/auth.hdr" 2> /dev/null; then
        msg_check_failed "the first administrator's sign-in (POST /api/v1/auth/login)" "$CV_HTTP"
    fi
    rm -f "$CV_RECORD/tmp/login.out"
    cv_http me /api/v1/auth/me -H "@$CV_RECORD/tmp/auth.hdr"
    if [ "$CV_HTTP" != 200 ]; then
        msg_check_failed "GET /api/v1/auth/me" "$CV_HTTP"
    fi
    cv_http apikey /api/v1/api-keys -H "@$CV_RECORD/tmp/auth.hdr" -H 'Content-Type: application/json' \
        --data-binary '{"name":"controlvar-install smoke"}'
    if [ "$CV_HTTP" != 201 ] || ! jq -r '"X-API-Key: " + .key' "$CV_RECORD/tmp/apikey.out" > "$CV_RECORD/tmp/apikey.hdr" 2> /dev/null ||
        ! key_id=$(jq -e -r '.id' "$CV_RECORD/tmp/apikey.out" 2> /dev/null); then
        msg_check_failed "POST /api/v1/api-keys" "$CV_HTTP"
    fi
    rm -f "$CV_RECORD/tmp/apikey.out"
    cv_http flag /api/v1/feature-flags/ -H "@$CV_RECORD/tmp/auth.hdr" -H 'Content-Type: application/json' \
        --data-binary '{"key":"controlvar-install-smoke","name":"Controlvar install smoke","is_active":true,"rollout_percentage":100}'
    if [ "$CV_HTTP" != 201 ]; then
        msg_check_failed "POST /api/v1/feature-flags/" "$CV_HTTP"
    fi
    cv_http evaluate "/api/v1/feature-flags/evaluate/controlvar-install-smoke?user_id=controlvar-install-user" \
        -H "@$CV_RECORD/tmp/apikey.hdr"
    if [ "$CV_HTTP" != 200 ] || ! jq -e '.key == "controlvar-install-smoke" and .enabled == true and .reason == "rollout"' \
        "$CV_RECORD/tmp/evaluate.out" > /dev/null 2>&1; then
        msg_check_failed "GET /api/v1/feature-flags/evaluate/controlvar-install-smoke with the API key" "$CV_HTTP"
    fi
    cv_http unkeyed "/api/v1/feature-flags/evaluate/controlvar-install-smoke?user_id=controlvar-install-user"
    if [ "$CV_HTTP" != 401 ]; then
        msg_check_failed "the same evaluation without a key" "$CV_HTTP"
    fi
    cv_http revoke "/api/v1/api-keys/$key_id" -X DELETE -H "@$CV_RECORD/tmp/auth.hdr"
    if [ "$CV_HTTP" != 204 ]; then
        msg_check_failed "DELETE /api/v1/api-keys/{id} (revoking the check's key)" "$CV_HTTP"
    fi
    rm -f "$CV_RECORD/tmp/auth.hdr" "$CV_RECORD/tmp/apikey.hdr"
    cv_put_fact SMOKE_DONE "$(date -u +%Y-%m-%dT%H:%M:%SZ)" || msg_tool_failed "the record" "1" "SMOKE_DONE"
    cv_step_done 9
}

# The summary. The password is written once to this computer's terminal (/dev/tty), never to
# stdout or the log; with no terminal the summary gives the command that reads it.
cv_summary() {
    local admin page="$CV_PAGE"
    admin=$(printf '%s' "$CV_ADMIN_EMAIL" | tr '[:upper:]' '[:lower:]')
    cv_out "Controlvar $CV_VERSION (beta) is installed and answering at:"
    cv_out "  https://$CV_DOMAIN"
    cv_out "  Sign in   $admin"
    if { : > /dev/tty; } 2> /dev/null; then
        cv_password_to_terminal
        cv_out "            Shown once, on this computer's terminal. Also in Secrets Manager: $NAMES_PASSWORD_SECRET"
    else
        cv_out "  Password  in Secrets Manager; to read it:"
        cv_out "    aws secretsmanager get-secret-value --secret-id $NAMES_PASSWORD_SECRET --query SecretString --output text --profile $CV_PROFILE --region $CV_REGION"
    fi
    cv_out "  Cost      about \$10 a day until you uninstall it, used or not"
    cv_out "  Alarms    email $CV_ALARM_EMAIL, $CV_SUBSCRIPTION"
    cv_out "Next: change the password (More > Change password); add a second administrator (Admin > Users > Invite User);"
    cv_out "  confirm the alarm email, or alarms reach nobody:"
    cv_out "  ${page}#confirm-the-alarm-email"
    cv_out "  If the address does not open yet, wait up to 15 minutes: your computer may hold an earlier \"not found\"."
    cv_out "To remove it (keep this folder and the record folder until then; uninstall works from both):"
    cv_out "  $(cv_uninstall_command)"
    cv_out "Log: $CV_LOG (it holds your account ID and email: remove them before you share it)"
}

# cv_password_to_terminal: the first administrator's password, once, on /dev/tty only.
cv_password_to_terminal() {
    local f="$CV_RECORD/tmp/shown.json"
    if cv_read "the password" "$f" secretsmanager get-secret-value --secret-id "$FIRST_SUPERUSER_PASSWORD_SECRET_ARN" \
        --query SecretString && jq -e -s 'length == 1 and (.[0] | type == "string")' "$f" > /dev/null 2>&1; then
        jq -r '"  Password  " + .' "$f" > /dev/tty 2> /dev/null || :
    fi
    rm -f "$f"
}
