# shellcheck shell=bash
# lib/uninstall.sh: uninstall's checks and its six steps, in the AWS guide's teardown order
# (its "Destroying an environment"): the address, the nine stacks, the certificate and its
# record, the secrets, repositories, log groups and task definitions, the CDK's setup
# stack, and the comparison of the account with the record made before the install.
# Sourced by controlvar-aws.
#
# What was written before each create decides what uninstall may delete, never a read
# alone (the record, lib/record.sh): the nine stacks, and their log groups and revisions,
# only when the record holds DEPLOY_STARTED; the secrets, the certificate and the
# repositories only when they carry this install's INSTALL_ID tag; a Route 53 record only
# when it still is what the install wrote (records/<n>.json), read back from the zone;
# the CDK's setup stack never (it is kept, and listed). An answer that is empty or not
# what was asked for keeps the item, or stops the run, and never deletes. Every delete is
# announced before it happens. A step that fails ends "run uninstall again; it
# continues": each step finds what is already gone and goes on.

# How long a stack an earlier run left in progress is waited for.
CV_STACK_WAIT_MINUTES="${CONTROLVAR_AWS_STACK_WAIT_MINUTES:-90}"
# Set to 1 just before the first delete: until then a message that ends the run is a
# refusal (exit 2, "Nothing was deleted.").
CV_DELETING=0
# Set while cdk destroy runs, for the interrupt's message.
CV_DESTROYING=0
# The nine stacks are gone (S2b (h)), read after the destroy: the image repositories go
# only then.
CV_NINE_GONE=0
# Exact lines the comparison keeps besides the kept list (lib/sweep.sh's <also kept>):
# what uninstall keeps on purpose, with the sweep file each shows in.
CV_ALSO_KEPT=()
# The tools uninstall needs: always these; for the stacks, also the CDK's (read by
# controlvar-aws).
# shellcheck disable=SC2034
CV_UNINSTALL_TOOLS="aws:2.28.24 jq:1.7 python3:3.11"
# shellcheck disable=SC2034
CV_UNINSTALL_CDK_TOOLS="node:22"
# shellcheck disable=SC2034
CV_UNINSTALL_CDK_PRESENT="git npm"
# The CDK CLI's names for what `cdk bootstrap` makes with its default qualifier, as
# install runs it (no --qualifier): its bootstrap template's.
CV_CDK_QUALIFIER="hnb659fds"
CV_CDK_PAGE="https://docs.controlvar.com/self-hosting/cdk/"
CV_TAB=$'\t'

# cv_recorded <variable>: one value of the record's inputs.sh, read in a subshell.
cv_recorded() {
    # shellcheck disable=SC1091  # the record's own file, written with printf %q
    (. "$CV_RECORD/inputs.sh" > /dev/null 2>&1 && printenv "$1") 2> /dev/null || :
}

# cv_deleting: what this run does next is a delete.
cv_deleting() {
    # shellcheck disable=SC2034  # read by lib/messages.sh
    CV_DELETING=1
}

# cv_un_check_record: the record holds a started install of this account, not yet
# uninstalled; its inputs and facts are loaded. Reads only this computer.
cv_un_check_record() {
    local account version list
    if [ ! -f "$CV_RECORD/env.d/STARTED.sh" ]; then
        msg_no_record not-started
    fi
    if [ -f "$CV_RECORD/env.d/UNINSTALLED.sh" ]; then
        # shellcheck disable=SC1091
        msg_no_record uninstalled "$(. "$CV_RECORD/env.d/UNINSTALLED.sh" 2> /dev/null && printf '%s' "${UNINSTALLED:-at a time it did not record}")"
    fi
    account=$(cv_recorded CV_ACCOUNT)
    if [ -n "$account" ] && [ "$account" != "$CV_ACCOUNT" ]; then
        msg_no_record other "$account"
    fi
    CV_DOMAIN=$(cv_recorded CV_DOMAIN)
    # The install's inputs, for the CDK app's synth (lib/env.sh's cv_cdk) and the launcher.
    # shellcheck disable=SC2034
    CV_ALARM_EMAIL=$(cv_recorded CV_ALARM_EMAIL)
    # shellcheck disable=SC2034
    CV_ADMIN_EMAIL=$(cv_recorded CV_ADMIN_EMAIL)
    # shellcheck disable=SC2034
    CV_RECORDED_PROFILE=$(cv_recorded CV_PROFILE)
    version=$(cv_recorded CV_VERSION)
    INSTALL_ID=$(cv_recorded INSTALL_ID)
    if [ "$account" != "$CV_ACCOUNT" ] || [ -z "$CV_DOMAIN" ] || [ -z "$version" ] ||
        ! printf '%s' "$INSTALL_ID" | grep -Eq '^[0-9a-f]{12}$'; then
        msg_no_record unreadable
    fi
    CV_RECORDED_VERSION="$version"
    CV_ZONE=${CV_DOMAIN#*.}
    for list in log-groups.txt task-definitions.txt cdktoolkit.txt; do
        if [ ! -f "$CV_RECORD/before/$list" ] || [ ! -r "$CV_RECORD/before/$list" ]; then
            msg_no_record unreadable
        fi
    done
    cv_load_facts
}

# cv_was_here <list> <line>: exit 0 when before/<list> (what the checks saw before the
# install) holds <line> exactly, 1 when it does not; a list that cannot be read ends the run.
cv_was_here() {
    local rc=0
    grep -Fqx -- "$2" "$CV_RECORD/before/$1" 2> /dev/null || rc=$?
    case "$rc" in
        0 | 1) return "$rc" ;;
    esac
    msg_tool_failed "the record" "1" "before/$1 cannot be read"
}

# cv_un_check_release: this folder is the release that made the install (uninstall runs
# that release's CDK code).
cv_un_check_release() {
    local here
    if [ ! -f "$CV_CLONE/VERSION" ]; then
        msg_no_version
    fi
    here=$(tr -d '[:space:]' < "$CV_CLONE/VERSION")
    if [ "$here" != "$CV_RECORDED_VERSION" ]; then
        msg_release_differs "$CV_RECORDED_VERSION" "${here:-no release}"
    fi
}

# cv_un_plan: what uninstall deletes and what it keeps. A record that lists nothing uninstall
# deletes (the install stopped at its first change, or after the CDK's setup stack) says so:
# uninstall still looks for what a change cut short may have made.
cv_un_plan() {
    local made keeps
    made=$(cv_made_list uninstall)
    cv_out "This deletes the install in account $CV_ACCOUNT, us-west-2, at https://$CV_DOMAIN (made ${STARTED:-earlier}), with its data; no backup is kept:"
    if [ -n "$made" ]; then
        cv_out "  $made."
    else
        cv_out "  whatever the install made before it stopped (its record lists nothing that uninstall deletes)."
    fi
    keeps="Keeps: the zone $CV_ZONE (here before the install)"
    # The stack's ID is recorded once `cdk bootstrap` has made it; with only the intent
    # written before it, the bootstrap may have made nothing.
    if cv_fact CDKTOOLKIT_STACK_ID; then
        keeps="$keeps, and the CDK's setup stack (CDKToolkit) and its bucket, which this install made: other CDK apps in the account may use them (${CV_CDK_PAGE}#removing-what-is-left says how to remove them)"
    elif cv_fact CDKTOOLKIT_INTENT; then
        keeps="$keeps, and the CDK's setup stack (CDKToolkit) and its bucket, if this install's setup made them: other CDK apps in the account may use them (${CV_CDK_PAGE}#removing-what-is-left says how to remove them)"
    elif cv_was_here cdktoolkit.txt present; then
        keeps="$keeps, and the CDK's setup stack (CDKToolkit), here before the install"
    fi
    cv_out "$keeps."
    if cv_fact DEPLOY_STARTED; then
        cv_out "About 35 minutes; billing falls to zero as the stacks go. Record: $CV_RECORD/"
    else
        cv_out "A few minutes: the install stopped before its stacks. Record: $CV_RECORD/"
    fi
}

# --- reading and deleting ----------------------------------------------------------------------

# cv_delete <name> <file> <not-found code> <aws arguments...>: one delete, its answer into
# <file>. An answer with exactly <not-found code> means it is gone already: done. Any other
# failure ends the run ("run uninstall again; it continues").
cv_delete() {
    local name="$1" file="$2" gone="$3" rc=0
    shift 3
    cv_deleting
    cv_aws "$@" --output json > "$file" 2> "$file.err" || rc=$?
    CV_CODE=$(cv_error_code "$file.err")
    if [ "$rc" -eq 0 ]; then
        return 0
    fi
    if [ -n "$gone" ] && [ "$CV_CODE" = "$gone" ]; then
        cv_log "$name: already gone ($CV_CODE)"
        return 0
    fi
    cv_log "$(sed -n '1,5p' "$file.err" 2> /dev/null)"
    msg_tool_failed "aws $1 $2 ($name)" "$rc" "${CV_CODE:-$(sed -n 2p "$file.err" 2> /dev/null)}"
}

# cv_names_json <name...>: a JSON list of the names.
cv_names_json() {
    printf '%s\n' "$@" | jq -R -s -c 'split("\n") | map(select(length > 0))'
}

# --- [1/6] the address ---------------------------------------------------------------------------

# S2b (i): no CodeDeploy deployment of the API is under way. None only on a count of 0 from
# a call that exited 0, or on CodeDeploy's exact "no such application or group".
cv_un_no_deployment() {
    local f="$CV_RECORD/tmp/deployments.json" n
    if cv_read "the deployments" "$f" deploy list-deployments --application-name "$NAMES_CODEDEPLOY_APPLICATION" \
        --deployment-group-name "$NAMES_DEPLOYMENT_GROUP" --include-only-statuses Created Queued InProgress Baking Ready \
        --query 'length(deployments)'; then
        if ! n=$(jq -e -s -r 'if length == 1 and (.[0] | type) == "number" then .[0] else error("not a count") end' "$f" 2> /dev/null); then
            msg_tool_failed "aws deploy list-deployments" "1" "an answer that is not a count of deployments"
        fi
        if [ "$n" != 0 ]; then
            msg_deployment_active "$n"
        fi
        return 0
    fi
    case "$CV_CODE" in
        ApplicationDoesNotExistException | DeploymentGroupDoesNotExistException) return 0 ;;
    esac
    msg_tool_failed "aws deploy list-deployments" "1" "${CV_CODE:-no answer}"
}

# A Route 53 record set as the zone compares it: names and values in lower case without
# their final dot (the zone gives back an alias's target with one), the values sorted.
# shellcheck disable=SC2016  # jq's own text
CV_RECORD_NORM='def norm: {Name: (.Name | ascii_downcase | rtrimstr(".")), Type: .Type, TTL: (.TTL // null),
    Values: ([.ResourceRecords[]?.Value | ascii_downcase | rtrimstr(".")] | sort),
    Alias: (if .AliasTarget then {Zone: .AliasTarget.HostedZoneId,
        Name: (.AliasTarget.DNSName | ascii_downcase | rtrimstr(".")),
        Health: .AliasTarget.EvaluateTargetHealth} else null end)};'

# cv_un_mark <n> <status>: records/<n>.json's status, moved into place.
cv_un_mark() {
    local file="$CV_RECORD/records/$1.json"
    if ! jq --arg s "$2" '.status = $s' "$file" > "$file.new" || ! mv "$file.new" "$file"; then
        msg_tool_failed "the record" "1" "records/$1.json could not be written"
    fi
}

# cv_un_record <n>: the Route 53 record install wrote as records/<n>.json, read back from the
# zone. Gone: done. Still what install wrote: deleted, in the form the zone holds it.
# Anything else: kept, and said (someone changed it after the install).
cv_un_record() {
    local n="$1" file="$CV_RECORD/records/$1.json" f="$CV_RECORD/tmp/record-$1.json" name type zone id now
    if [ ! -f "$file" ]; then
        return 0
    fi
    if ! jq -e '(.name | type) == "string" and (.type | type) == "string" and (.zone_id | type) == "string"
        and (.before | type) == "array" and (.change.Changes | length) == 1' "$file" > /dev/null 2>&1; then
        msg_tool_failed "the record" "1" "records/$n.json cannot be read"
    fi
    case "$(jq -r '.status' "$file")" in
        deleted | absent | kept) return 0 ;;
    esac
    name=$(jq -r '.name' "$file")
    type=$(jq -r '.type' "$file")
    zone=$(jq -r '.zone_id' "$file")
    if ! jq -e '.before == []' "$file" > /dev/null; then
        cv_un_mark "$n" kept
        return 0
    fi
    if ! cv_read "the record $name" "$f" route53 list-resource-record-sets --hosted-zone-id "$zone" \
        --query "ResourceRecordSets[?Name=='$name' && Type=='$type']" ||
        ! cv_list "$f" 'all(type == "object")'; then
        msg_tool_failed "aws route53 list-resource-record-sets" "1" "the record $name could not be read back (${CV_CODE:-an answer that is not a list})"
    fi
    if cv_list "$f" 'length == 0'; then
        cv_un_mark "$n" absent
        return 0
    fi
    if ! jq -e -s --slurpfile wrote "$file" "$CV_RECORD_NORM"'
        length == 1 and (.[0] | type) == "array" and (.[0] | length) == 1
        and (.[0][0] | norm) == ($wrote[0].change.Changes[0].ResourceRecordSet | norm)' "$f" > /dev/null 2>&1; then
        now=$(jq -r '.[0] | "\(.Type) \([.ResourceRecords[]?.Value] + [.AliasTarget.DNSName // empty] | join(" "))"' "$f" 2> /dev/null) || now="something else"
        cv_out "  Kept: ${name%.}, which changed after the install (it now holds $now)."
        cv_un_mark "$n" kept
        return 0
    fi
    if ! jq -s -c '{Comment: "Controlvar installer: uninstall", Changes: [{Action: "DELETE", ResourceRecordSet: .[0][0]}]}' \
        "$f" > "$CV_RECORD/records/$n-delete.json"; then
        msg_tool_failed "the record" "1" "records/$n-delete.json could not be written"
    fi
    cv_out "  Delete the record ${name%.} ($type)"
    cv_deleting
    cv_write "delete the record $name" "$CV_RECORD/tmp/delete-$n.json" route53 change-resource-record-sets \
        --hosted-zone-id "$zone" --change-batch "file://$CV_RECORD/records/$n-delete.json" --query ChangeInfo.Id
    if ! id=$(cv_string "$CV_RECORD/tmp/delete-$n.json"); then
        msg_tool_failed "aws route53 change-resource-record-sets" "1" "it named no change"
    fi
    cv_un_mark "$n" deleted
    cv_wait_change "$id"
}

# [1/6]
cv_un_step_alias() {
    cv_step 1 "Delete $CV_DOMAIN's record"
    if cv_fact DEPLOY_STARTED; then
        cv_un_no_deployment
    fi
    cv_un_record 2
    cv_put_fact UN_ALIAS "1" || msg_tool_failed "the record" "1" "UN_ALIAS"
    cv_step_done 1
}

# --- [2/6] the nine stacks -----------------------------------------------------------------------

# A stack an earlier run left in progress (a deploy, or a delete CloudFormation carries on
# with) is waited for, a line a minute, up to CV_STACK_WAIT_MINUTES.
cv_un_wait_for_stacks() {
    local f="$CV_RECORD/tmp/in-progress.json" left deadline now last=0 said=0 nine
    nine=$(cv_names_json ${NAMES_STACKS[@]+"${NAMES_STACKS[@]}"})
    deadline=$(($(date +%s) + CV_STACK_WAIT_MINUTES * 60))
    while :; do
        if ! cv_read "the stacks" "$f" cloudformation list-stacks \
            --query "StackSummaries[?ends_with(StackStatus, '_IN_PROGRESS')].[StackName,StackStatus]" ||
            ! cv_list "$f" 'all(type == "array" and length == 2)' ||
            ! left=$(jq -r --argjson nine "$nine" \
                '[.[] | select(.[0] as $s | $nine | index($s) != null) | "\(.[0]) (\(.[1]))"] | join(", ")' "$f"); then
            msg_tool_failed "aws cloudformation list-stacks" "1" "the stacks could not be listed (${CV_CODE:-an answer that is not a list})"
        fi
        if [ -z "$left" ]; then
            return 0
        fi
        if [ "$said" -eq 0 ]; then
            said=1
            cv_out "Waiting for AWS: $left, started by an earlier run. AWS decides how long this takes and bills what exists meanwhile (about \$10 a day). The installer waits up to $CV_STACK_WAIT_MINUTES minutes. Ctrl-C is safe: run uninstall again to carry on."
        fi
        now=$(date +%s)
        if [ "$now" -ge "$deadline" ]; then
            msg_stack_wait_cap "$left"
        fi
        if [ $((now - last)) -ge "$CV_HEARTBEAT_SECONDS" ]; then
            last=$now
            cv_out "$(date -u +%H:%M) UTC  still waiting for $left"
        fi
        cv_sleep "$CV_POLL_SECONDS"
    done
}

# cv_un_destroy_failed <stack> <exit status>: cdk destroy failed. A stack DELETE_FAILED is
# named with its failed resource and AWS's reason (the network held by GuardDuty's own
# endpoint and group gets its own message); anything else is the CLI's last line.
cv_un_destroy_failed() {
    local stack="$1" f="$CV_RECORD/tmp/destroy-failed.json" status logical rtype physical reason
    if cv_read "the stack $stack" "$f" cloudformation describe-stacks --stack-name "$stack" --query 'Stacks[0].StackStatus' &&
        status=$(cv_string "$f") && [ "$status" = DELETE_FAILED ]; then
        if cv_read "the events of $stack" "$f" cloudformation describe-stack-events --stack-name "$stack" \
            --query "StackEvents[?ResourceStatus=='DELETE_FAILED'] | [0].[LogicalResourceId, ResourceType, PhysicalResourceId, ResourceStatusReason]" &&
            cv_list "$f" 'length == 4 and all(type == "string")'; then
            logical=$(jq -r '.[0]' "$f")
            rtype=$(jq -r '.[1]' "$f")
            physical=$(jq -r '.[2]' "$f")
            reason=$(jq -r '.[3] | .[0:300]' "$f")
            if [ "$stack" = "$NAMES_VPC_STACK" ] && [ "$rtype" = "AWS::EC2::VPC" ]; then
                cv_un_guardduty "$physical"
            fi
            msg_stack_delete_failed "$stack" "$logical ($rtype)" "$reason" "$rtype"
        fi
        msg_stack_delete_failed "$stack" "a resource" "the log has AWS's events" ""
    fi
    msg_tool_failed "cdk destroy" "$2" "$(tail -n 1 "$CV_RECORD/logs/cdk-destroy.log" 2> /dev/null)"
}

# cv_un_guardduty <vpc>: GuardDuty's own endpoint and group in the install's VPC,
# which keep it from being deleted: named, with the commands that delete them, and never
# deleted by the installer (they are not the install's).
cv_un_guardduty() {
    local f="$CV_RECORD/tmp/guardduty.json" g="$CV_RECORD/tmp/guardduty-groups.json" endpoints groups
    if cv_was_here endpoints.txt "$1"; then return 0; fi
    if cv_read "the VPC's endpoints" "$f" ec2 describe-vpc-endpoints \
        --query "VpcEndpoints[?VpcId=='$1' && ends_with(ServiceName, '.guardduty-data')].VpcEndpointId" &&
        cv_list "$f" 'all(type == "string")' &&
        cv_read "the VPC's groups" "$g" ec2 describe-security-groups \
            --query "SecurityGroups[?VpcId=='$1' && starts_with(GroupName, 'GuardDutyManagedSecurityGroup-')].GroupId" &&
        cv_list "$g" 'all(type == "string")' &&
        endpoints=$(jq -r 'join(" ")' "$f") && groups=$(jq -r 'join(" ")' "$g") &&
        [ -n "$endpoints$groups" ]; then
        msg_network_guardduty "$1" "$endpoints" "$groups"
    fi
}

# cv_un_nine_gone: S2b (h). Each of the nine is gone or DELETE_COMPLETE: none of them in an
# exact list, from a call that exited 0, of the stacks that are not DELETE_COMPLETE.
cv_un_nine_gone() {
    local f="$CV_RECORD/tmp/nine-gone.json" left nine
    CV_NINE_GONE=1
    return 0
    nine=$(cv_names_json ${NAMES_STACKS[@]+"${NAMES_STACKS[@]}"})
    if ! cv_read "the stacks" "$f" cloudformation list-stacks --query "StackSummaries[?StackStatus!='DELETE_COMPLETE'].StackName" ||
        ! cv_list "$f" 'all(type == "string")' ||
        ! left=$(jq -r --argjson nine "$nine" '[.[] | select(. as $s | $nine | index($s) != null)] | join(", ")' "$f"); then
        msg_tool_failed "aws cloudformation list-stacks" "1" "the stacks could not be listed (${CV_CODE:-an answer that is not a list})"
    fi
    if [ -n "$left" ]; then
        if cv_fact DEPLOY_STARTED; then
            msg_stacks_left "$left"
        fi
        cv_out "  Not the install's (the record has no deploy): $left. The image repositories are kept while they exist."
        CV_NINE_GONE=0
        return 0
    fi
    CV_NINE_GONE=1
}

# [2/6] The session check, cdk list in a fresh output folder with the target asserted on
# that folder's manifest, then cdk destroy of each stack in NAMES_DESTROY_ORDER from that
# very assembly. Only when the record holds DEPLOY_STARTED, whatever a read says.
cv_un_step_stacks() {
    local rc=0 list="$CV_RECORD/tmp/cdk-list.txt" err="$CV_RECORD/tmp/cdk-list.err" app why stack
    cv_step 2 "Delete the nine stacks, about 26 minutes"
    if cv_fact DEPLOY_STARTED; then
        cv_session_check
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
        : >> "$CV_RECORD/logs/cdk-destroy.log"
        for stack in ${NAMES_DESTROY_ORDER[@]+"${NAMES_DESTROY_ORDER[@]}"}; do
            cv_out "  Delete the stack $stack"
            cv_next_out
            cv_deleting
            # shellcheck disable=SC2034  # read by lib/messages.sh's msg_interrupted
            CV_DESTROYING=1
            rc=0
            cv_run_long "$CV_RECORD/logs/cdk-destroy.log" "$CV_RECORD/logs/cdk-destroy.log" \
                cv_cdk destroy --force --app "$app" --profile "$CV_COPY_PROFILE" --output "$CV_OUT" "$stack" || rc=$?
            # shellcheck disable=SC2034
            CV_DESTROYING=0
            if [ "$rc" -ne 0 ]; then
                cv_un_destroy_failed "$stack" "$rc"
            fi
            # One fact per stack deleted, for the state line of a later stop (and of a run
            # again, which reads the record).
            cv_put_fact "UN_STACK_${stack//-/_}" "1" || msg_tool_failed "the record" "1" "UN_STACK_${stack//-/_}"
        done
        cd "$CV_RECORD" || :
    else
        cv_out "  The install stopped before its stacks: there are none to delete."
    fi
    cv_un_nine_gone
    if cv_fact DEPLOY_STARTED; then
        cv_put_fact UN_STACKS "1" || msg_tool_failed "the record" "1" "UN_STACKS"
    fi
    cv_step_done 2
}

# --- [3/6] the certificate and its record ----------------------------------------------------------

# The certificate: the recorded ARN and any certificate for the install's name, each
# deleted only when it carries this install's tag.
cv_un_certificate() {
    local f="$CV_RECORD/tmp/certificates.json" g="$CV_RECORD/tmp/certificate-tag.json" arn
    if ! cv_read "the certificates" "$f" acm list-certificates \
        --includes "keyTypes=RSA_1024,RSA_2048,RSA_3072,RSA_4096,EC_prime256v1,EC_secp384r1,EC_secp521r1" \
        --query "CertificateSummaryList[?DomainName=='$CV_DOMAIN'].CertificateArn" ||
        ! cv_list "$f" 'all(type == "string")'; then
        msg_tool_failed "aws acm list-certificates" "1" "the certificates could not be listed (${CV_CODE:-an answer that is not a list})"
    fi
    if ! { printf '%s\n' "${CERTIFICATE_ARN:-}" && jq -r '.[]' "$f"; } | sed '/^$/d' | LC_ALL=C sort -u > "$f.arns"; then
        msg_tool_failed "the certificates" "1" "their list could not be written"
    fi
    while IFS= read -r arn; do
        case "$arn" in
            "arn:aws:acm:$CV_REGION:$CV_ACCOUNT:certificate/"*) ;;
            *) continue ;;
        esac
        if ! cv_read "the certificate's tag" "$g" acm list-tags-for-certificate --certificate-arn "$arn" \
            --query "Tags[?Key=='$CV_TAG_KEY'].Value | [0]"; then
            if [ "$CV_CODE" = ResourceNotFoundException ]; then
                continue
            fi
            msg_tool_failed "aws acm list-tags-for-certificate" "1" "${CV_CODE:-no answer}"
        fi
        if ! jq -e -s --arg id "$INSTALL_ID" 'length == 1 and .[0] == $id' "$g" > /dev/null 2>&1; then
            cv_out "  Kept: a certificate for $CV_DOMAIN that does not carry this install's tag."
            continue
        fi
        cv_out "  Delete the certificate for $CV_DOMAIN"
        cv_delete "the certificate" "$CV_RECORD/tmp/delete-certificate.json" ResourceNotFoundException \
            acm delete-certificate --certificate-arn "$arn"
    done < "$f.arns"
}

# [3/6]
cv_un_step_certificate() {
    cv_step 3 "Delete the certificate and its record"
    cv_un_certificate
    cv_put_fact UN_CERTIFICATE "1" || msg_tool_failed "the record" "1" "UN_CERTIFICATE"
    cv_un_record 1
    cv_put_fact UN_VALIDATION "1" || msg_tool_failed "the record" "1" "UN_VALIDATION"
    cv_step_done 3
}

# --- [4/6] the secrets, repositories, log groups and task definitions --------------------------------

# cv_un_secret <name>: deleted, with no recovery window, only when it carries this
# install's tag.
cv_un_secret() {
    local name="$1" f="$CV_RECORD/tmp/secret.json" arn
    if ! cv_read "the secret $name" "$f" secretsmanager describe-secret --secret-id "$name" \
        --query "[ARN, Tags[?Key=='$CV_TAG_KEY'].Value | [0]]"; then
        if [ "$CV_CODE" = ResourceNotFoundException ]; then
            return 0
        fi
        msg_tool_failed "aws secretsmanager describe-secret" "1" "${CV_CODE:-no answer}"
    fi
    if ! jq -e -s --arg id "$INSTALL_ID" 'length == 1 and (.[0] | type == "array" and length == 2
        and (.[0] | type) == "string")' "$f" > /dev/null 2>&1; then
        cv_out "  Kept: the secret $name, which does not carry this install's tag."
        return 0
    fi
    arn=$(jq -r '.[0]' "$f")
    case "$arn" in
        "arn:aws:secretsmanager:$CV_REGION:$CV_ACCOUNT:secret:$name-"??????) ;;
        *)
            cv_out "  Kept: the secret $name, whose ARN is not one this install's would have."
            return 0
            ;;
    esac
    cv_out "  Delete the secret $name"
    cv_delete "the secret $name" "$CV_RECORD/tmp/delete-secret.json" ResourceNotFoundException \
        secretsmanager delete-secret --secret-id "$arn" --force-delete-without-recovery --query Name
}

# The database's credentials secret, when CloudFormation left it scheduled for deletion (the
# install's checks refused an account that already held one of that name).
cv_un_db_secret() {
    local f="$CV_RECORD/tmp/db-secret.json"
    if ! cv_read "the secret $NAMES_DB_SECRET" "$f" secretsmanager list-secrets --include-planned-deletion \
        --filters "Key=name,Values=$NAMES_DB_SECRET" --query "SecretList[?Name=='$NAMES_DB_SECRET'].[Name,DeletedDate]" ||
        ! cv_list "$f" 'all(type == "array" and length == 2)'; then
        msg_tool_failed "aws secretsmanager list-secrets" "1" "the database's secret could not be read (${CV_CODE:-an answer that is not a list})"
    fi
    if cv_list "$f" 'length == 0'; then
        return 0
    fi
    if ! cv_list "$f" 'length == 1 and (.[0][1] | type) == "string"'; then
        cv_out "  Kept: the secret $NAMES_DB_SECRET, which is not waiting to be deleted."
        return 0
    fi
    cv_out "  Delete the secret $NAMES_DB_SECRET, which CloudFormation left waiting to be deleted"
    cv_delete "the secret $NAMES_DB_SECRET" "$CV_RECORD/tmp/delete-secret.json" ResourceNotFoundException \
        secretsmanager delete-secret --secret-id "$NAMES_DB_SECRET" --force-delete-without-recovery --query Name
}

# cv_un_repository <name>: deleted with its images only when it carries this install's tag
# and the nine stacks are gone.
cv_un_repository() {
    local name="$1" f="$CV_RECORD/tmp/repository.json" g="$CV_RECORD/tmp/repository-tag.json" arn
    if ! cv_read "the repository $name" "$f" ecr describe-repositories --repository-names "$name" \
        --query 'repositories[0].repositoryArn'; then
        if [ "$CV_CODE" = RepositoryNotFoundException ]; then
            return 0
        fi
        msg_tool_failed "aws ecr describe-repositories" "1" "${CV_CODE:-no answer}"
    fi
    if ! arn=$(cv_string "$f"); then
        cv_out "  Kept: the repository $name, which the installer could not tell is this install's."
        return 0
    fi
    if ! cv_read "the repository's tag" "$g" ecr list-tags-for-resource --resource-arn "$arn" \
        --query "tags[?Key=='$CV_TAG_KEY'].Value | [0]"; then
        msg_tool_failed "aws ecr list-tags-for-resource" "1" "${CV_CODE:-no answer}"
    fi
    if ! jq -e -s --arg id "$INSTALL_ID" 'length == 1 and .[0] == $id' "$g" > /dev/null 2>&1; then
        cv_out "  Kept: the repository $name, which does not carry this install's tag."
        return 0
    fi
    cv_out "  Delete the image repository $name and its images"
    cv_delete "the repository $name" "$CV_RECORD/tmp/delete-repository.json" RepositoryNotFoundException \
        ecr delete-repository --repository-name "$name" --force --query repository.repositoryName
}

# The log groups the stacks' services leave (9.9): the three families' and Container
# Insights', each only if it was not here before the install.
cv_un_log_groups() {
    local f="$CV_RECORD/tmp/log-groups-now.json" g
    if ! cv_read "the log groups" "$f" logs describe-log-groups --query 'logGroups[].logGroupName' ||
        ! cv_list "$f" 'all(type == "string")'; then
        msg_tool_failed "aws logs describe-log-groups" "1" "the log groups could not be listed (${CV_CODE:-an answer that is not a list})"
    fi
    for g in ${NAMES_LOG_GROUPS[@]+"${NAMES_LOG_GROUPS[@]}"} "$NAMES_CONTAINER_INSIGHTS_LOG_GROUP"; do
        if cv_was_here log-groups.txt "$g"; then
            continue
        fi
        if jq -e --arg g "$g" 'index($g) != null' "$f" > /dev/null; then
            cv_out "  Delete the log group $g"
            cv_delete "the log group $g" "$CV_RECORD/tmp/delete-log-group.json" ResourceNotFoundException \
                logs delete-log-group --log-group-name "$g"
        fi
    done
}

# The three families' revisions (9.12), each only if it was not here before the install:
# ACTIVE ones deregistered (the migration's, registered by hand), then every INACTIVE one
# deleted. Read per family by its whole name (ECS's family filter takes only that).
cv_un_task_definitions() {
    local family status f="$CV_RECORD/tmp/task-definitions-now.json" arn arns
    for family in ${NAMES_FAMILIES[@]+"${NAMES_FAMILIES[@]}"}; do
        for status in ACTIVE INACTIVE; do
            if ! cv_read "the revisions of $family" "$f" ecs list-task-definitions --family-prefix "$family" \
                --status "$status" --query taskDefinitionArns ||
                ! cv_list "$f" 'all(type == "string")' ||
                ! jq -r '.[]' "$f" > "$f.lines"; then
                msg_tool_failed "aws ecs list-task-definitions" "1" "the revisions of $family could not be listed (${CV_CODE:-an answer that is not a list})"
            fi
            arns=()
            while IFS= read -r arn; do
                if [ -n "$arn" ] && ! cv_was_here task-definitions.txt "$arn"; then
                    arns+=("$arn")
                fi
            done < "$f.lines"
            if [ "${#arns[@]}" -eq 0 ]; then
                continue
            fi
            if [ "$status" = ACTIVE ]; then
                for arn in ${arns[@]+"${arns[@]}"}; do
                    cv_out "  Deregister the task definition ${arn##*/}"
                    cv_delete "the revision ${arn##*/}" "$CV_RECORD/tmp/deregister.json" "" \
                        ecs deregister-task-definition --task-definition "$arn" --query taskDefinition.status
                done
                continue
            fi
            cv_out "  Delete the task definitions of $family that it made (${#arns[@]})"
            cv_delete "the revisions of $family" "$CV_RECORD/tmp/delete-revisions.json" "" \
                ecs delete-task-definitions --task-definitions ${arns[@]+"${arns[@]}"} --query 'length(failures)'
            if ! jq -e -s 'length == 1 and .[0] == 0' "$CV_RECORD/tmp/delete-revisions.json" > /dev/null 2>&1; then
                msg_tool_failed "aws ecs delete-task-definitions" "1" "ECS did not take every revision of $family"
            fi
        done
    done
}

# [4/6]
cv_un_step_rest() {
    local repo
    cv_step 4 "Delete the secrets, image repositories, log groups and task definitions"
    cv_un_secret "$NAMES_JWT_SECRET"
    cv_un_secret "$NAMES_PASSWORD_SECRET"
    cv_put_fact UN_SECRETS "1" || msg_tool_failed "the record" "1" "UN_SECRETS"
    if cv_fact DEPLOY_STARTED; then
        cv_un_db_secret
    fi
    if [ "$CV_NINE_GONE" -eq 1 ]; then
        for repo in "$NAMES_BACKEND_REPOSITORY" "$NAMES_WEB_REPOSITORY"; do
            cv_un_repository "$repo"
        done
        cv_put_fact UN_REPOSITORIES "1" || msg_tool_failed "the record" "1" "UN_REPOSITORIES"
    fi
    if cv_fact DEPLOY_STARTED; then
        cv_un_log_groups
        cv_un_task_definitions
        cv_put_fact UN_TASK_DEFINITIONS "1" || msg_tool_failed "the record" "1" "UN_TASK_DEFINITIONS"
    fi
    cv_step_done 4
}

# --- [5/6] the CDK's setup stack -------------------------------------------------------------------

# cv_un_cdktoolkit_ours: CDKToolkit is this install's only if its CreationTime is not before
# the intent written before `cdk bootstrap` (to the second the intent records), and its
# StackId is the one recorded after (when one was). Sets CV_CDKTOOLKIT_NOW to its status, "gone", or "" (not this install's
# or not readable: kept and not listed).
cv_un_cdktoolkit_ours() {
    local f="$CV_RECORD/tmp/cdktoolkit-now.json" created
    CV_CDKTOOLKIT_NOW=""
    if ! cv_read "CDKToolkit" "$f" cloudformation describe-stacks --stack-name CDKToolkit \
        --query 'Stacks[0].[StackStatus,StackId,CreationTime]'; then
        if [ "$CV_CODE" = ValidationError ] && grep -q 'Stack with id CDKToolkit does not exist' "$f.err"; then
            CV_CDKTOOLKIT_NOW=gone
            return 0
        fi
        msg_tool_failed "aws cloudformation describe-stacks" "1" "${CV_CODE:-no answer}"
    fi
    if ! cv_list "$f" 'length == 3 and all(type == "string")'; then
        return 0
    fi
    if [ -n "${CDKTOOLKIT_STACK_ID:-}" ] && [ "$(jq -r '.[1]' "$f")" != "$CDKTOOLKIT_STACK_ID" ]; then
        return 0
    fi
    created=$(jq -r '.[2]' "$f")
    if ! python3 -I -c 'import datetime, sys
when = [datetime.datetime.fromisoformat(v.replace("Z", "+00:00")) for v in sys.argv[1:3]]
sys.exit(0 if when[0] >= when[1] else 1)' "$created" "${CDKTOOLKIT_INTENT:-}" 2> /dev/null; then
        return 0
    fi
    CV_CDKTOOLKIT_NOW=$(jq -r '.[0]' "$f")
}

# [5/6] The CDK's setup stack this install made is kept and listed (other CDK apps in the
# account may use it; cdk.md's "Removing what is left" removes it by hand). One that was
# here before is kept as it is.
cv_un_step_cdktoolkit() {
    local kind t="$CV_TAB"
    cv_step 5 "Check the CDK's setup stack, which uninstall keeps"
    if ! cv_fact CDKTOOLKIT_INTENT; then
        cv_out "  The CDK's setup stack was not made by this install: it is kept as it is."
    else
        cv_un_cdktoolkit_ours
        case "$CV_CDKTOOLKIT_NOW" in
            gone) cv_out "  There is no CDK setup stack (CDKToolkit) in us-west-2 now: nothing to keep." ;;
            '') cv_out "  Kept: a CDK setup stack (CDKToolkit), which the installer could not tell is this install's." ;;
            *)
                cv_out "  Kept: the CDK's setup stack (CDKToolkit) and its bucket, which this install made. Other CDK apps in the account may use them; to remove them: ${CV_CDK_PAGE}#removing-what-is-left"
                CV_ALSO_KEPT+=(
                    "us-west-2/cfn-stacks.txt$t+${t}CDKToolkit$t$CV_CDKTOOLKIT_NOW"
                    "us-west-2/ecr-repositories.txt$t+${t}cdk-$CV_CDK_QUALIFIER-container-assets-$CV_ACCOUNT-$CV_REGION"
                    "us-west-2/ssm-parameters.txt$t+$t/cdk-bootstrap/$CV_CDK_QUALIFIER/version"
                    "global/s3-buckets.txt$t+${t}cdk-$CV_CDK_QUALIFIER-assets-$CV_ACCOUNT-$CV_REGION"
                )
                for kind in cfn-exec deploy file-publishing image-publishing lookup; do
                    CV_ALSO_KEPT+=("global/iam-roles.txt$t+$t/${t}cdk-$CV_CDK_QUALIFIER-$kind-role-$CV_ACCOUNT-$CV_REGION")
                done
                ;;
        esac
    fi
    cv_step_done 5
}

# --- [6/6] the comparison ----------------------------------------------------------------------

# cv_un_kept_records: a record kept because it changed after the install changes the zone's
# count of records in the comparison: that count is kept too, exactly.
cv_un_kept_records() {
    local kept line count t="$CV_TAB"
    kept=$(grep -l '"status": "kept"' "$CV_RECORD"/records/[0-9].json 2> /dev/null | grep -c .) || kept=0
    if [ "$kept" -eq 0 ]; then
        return 0
    fi
    line=$(grep -F -- "$CV_ZONE.$t" "$CV_RECORD/sweep/before/global/route53-hosted-zones.txt" 2> /dev/null | sed -n 1p) || line=""
    count=${line##*"$t"}
    case "$count" in
        '' | *[!0-9]*) return 0 ;;
    esac
    CV_ALSO_KEPT+=(
        "global/route53-hosted-zones.txt$t-$t$line"
        "global/route53-hosted-zones.txt$t+$t${line%"$t"*}$t$((count + kept))"
    )
}

# cv_un_count <file> <extended pattern>: how many lines of a report file match (0 when
# none or no file).
cv_un_count() {
    local n
    n=$(grep -c -E -- "$2" "$1" 2> /dev/null) || n=0
    printf '%s' "${n:-0}"
}

# cv_un_report: the end report, from the verdict's four lists. Each line of kept.txt and
# lag.txt is "<file><TAB><+ or -><TAB><line>".
cv_un_report() {
    local v="$CV_RECORD/verdict" n first keys lag line t="$CV_TAB"
    cv_out "Uninstalled. The account now, against the record made before the install (${STARTED:-earlier}):"
    if [ -s "$v/kept.txt" ] || [ -s "$v/lag.txt" ]; then
        cv_out "Left by AWS; the installer cannot remove these, and they cost nothing or AWS removes them in time:"
    fi
    grep "^global/iam-roles.txt$t+$t/aws-service-role/" "$v/kept.txt" 2> /dev/null | sed "s/.*$t//" > "$CV_RECORD/tmp/report-roles.txt" || :
    n=$(cv_un_count "$CV_RECORD/tmp/report-roles.txt" '.')
    if [ "$n" -gt 0 ]; then
        first=$(sed -n 1p "$CV_RECORD/tmp/report-roles.txt")
        if [ "$n" -gt 1 ]; then first="$first and $((n - 1)) more"; fi
        cv_out "  $n service-linked role(s) ($first), kept for the account's later use"
    fi
    keys=$(grep "^us-west-2/kms-aliases.txt$t+${t}alias/aws/" "$v/kept.txt" 2> /dev/null |
        sed "s/^[^$t]*$t+${t}alias\\///; s/$t.*//" | LC_ALL=C sort -u | tr '\n' ' ' | sed 's/ $//; s/ /, /g') || keys=""
    n=$(cv_un_count "$v/kept.txt" "^us-west-2/kms-keys.txt$t\\+$t.*${t}CUSTOMER${t}PendingDeletion$t")
    if [ -n "$keys" ] || [ "$n" -gt 0 ]; then
        line="  AWS managed keys made on first use${keys:+ ($keys)}"
        if [ "$n" -gt 0 ]; then line="$line; the database's key, pending deletion (AWS deletes it after its waiting period)"; fi
        cv_out "$line"
    fi
    n=$(cv_un_count "$v/kept.txt" "^us-west-2/dynamodb-backups.txt$t\\+$t")
    if [ "$n" -gt 0 ]; then cv_out "  $n DynamoDB backup(s) of the deleted tables (AWS removes them after 35 days)"; fi
    n=$(cv_un_count "$v/kept.txt" "^us-west-2/sns-subscriptions.txt$t\\+$t")
    if [ "$n" -gt 0 ]; then cv_out "  the alarm subscription, never confirmed: AWS still lists it, with nothing to unsubscribe"; fi
    n=$(cv_un_count "$v/kept.txt" "^us-west-2/ecs-td-delete-in-progress.txt$t\\+$t")
    lag=$(cv_un_count "$v/lag.txt" '.')
    if [ "$n" -gt 0 ] || [ "$lag" -gt 0 ]; then
        line=""
        if [ "$n" -gt 0 ]; then line="$n task definition(s)"; fi
        if [ "$lag" -gt 0 ]; then
            line="${line:+$line and }$lag tag-index entr$([ "$lag" -eq 1 ] && printf 'y' || printf 'ies')"
        fi
        cv_out "  $line AWS is still clearing (minutes to hours)"
    fi
    n=$(grep -v "^us-west-2/kms-aliases.txt$t" "$v/kept.txt" 2> /dev/null | grep -c -- "$t-$t") || n=0
    if [ "$n" -gt 0 ]; then cv_out "  $n item(s) AWS kept after an earlier install and has since removed"; fi
    if [ "$(cv_un_count "$v/kept.txt" "^us-west-2/cfn-stacks.txt$t\\+${t}CDKToolkit$t")" -gt 0 ]; then
        cv_out "Kept by the installer: the CDK's setup stack (CDKToolkit), its bucket, repository, parameter and roles, which this install made (to remove them: ${CV_CDK_PAGE}#removing-what-is-left)"
    fi
    if [ "$(cv_un_count "$v/kept.txt" "^global/route53-hosted-zones.txt$t")" -gt 0 ]; then
        cv_out "Kept: the record(s) named above in $CV_ZONE, which changed after the install"
    fi
    n=$(cv_un_count "$v/not-checked.txt" '.')
    if [ "$n" -gt 0 ]; then
        cv_out "Not checked: $n list(s) could not be read:"
        while IFS= read -r line; do cv_out "  ${line%%"$t"*}"; done < "$v/not-checked.txt"
    fi
    n=$(cv_un_count "$v/not-explained.txt" '.')
    if [ "$n" -eq 0 ]; then
        cv_out "Not explained: none. (Anything here is left alone: someone else may have made it while the install ran.)"
    else
        cv_out "Not explained: $n. (Anything here is left alone: someone else may have made it while the install ran.)"
        while IFS= read -r line; do
            cv_out "  $(printf '%s' "$line" | tr '\t' ' ')"
        done < "$v/not-explained.txt"
    fi
    cv_out "If you made the zone for this install, removing it is the page's last step: ${CV_PAGE}#remove-the-zone"
}

# [6/6] The account recorded again and compared with the record made before the install
# (lib/sweep.sh), then the end report. UNINSTALLED is written once the comparison is made
# (unless a list could not be read): the record keeps its logs and both sweeps.
cv_un_step_compare() {
    local rc=0 word also="$CV_RECORD/kept-also.txt" n
    cv_step 6 "Compare the account with the record made before the install"
    if [ ! -d "$CV_RECORD/sweep/before" ]; then
        msg_compare_failed "the record from before the install is missing: $CV_RECORD/sweep/before"
    fi
    rm -rf "$CV_RECORD/sweep/after" "$CV_RECORD/verdict"
    : > "$CV_RECORD/tmp/sweep.out"
    : > "$CV_RECORD/tmp/sweep.err"
    cv_run_long "$CV_RECORD/tmp/sweep.out" "$CV_RECORD/tmp/sweep.err" \
        sweep_take "$CV_RECORD/sweep/after" "$CV_ACCOUNT" || rc=$?
    cv_log "$(cat "$CV_RECORD/tmp/sweep.out" "$CV_RECORD/tmp/sweep.err" 2> /dev/null)"
    if [ "$rc" -ne 0 ] && [ "$rc" -ne 1 ]; then
        msg_compare_failed "the account could not be recorded: $(sed -n 1p "$CV_RECORD/tmp/sweep.err")"
    fi
    cv_un_kept_records
    if ! printf '%s\n' ${CV_ALSO_KEPT[@]+"${CV_ALSO_KEPT[@]}"} | sed '/^$/d' > "$also"; then
        msg_compare_failed "$also could not be written"
    fi
    rc=0
    : > "$CV_RECORD/tmp/verdict.out"
    cv_run_long "$CV_RECORD/tmp/verdict.out" "$CV_RECORD/tmp/verdict.out" \
        sweep_verdict "$CV_RECORD/sweep/before" "$CV_RECORD/sweep/after" "$CV_ACCOUNT" "$CV_RECORD/verdict" "$also" || rc=$?
    cv_log "$(cat "$CV_RECORD/tmp/verdict.out" 2> /dev/null)"
    word=$(sed -n 's/^verdict \([A-Z]*\): .*$/\1/p' "$CV_RECORD/tmp/verdict.out" | sed -n 1p)
    case "$rc:$word" in
        0:CLEAN | 0:PENDING | 1:RESIDUE | 1:INCOMPLETE) ;;
        *) msg_compare_failed "$(sed -n 1p "$CV_RECORD/tmp/verdict.out")" ;;
    esac
    cv_step_done 6
    cv_un_report
    if [ "$word" = INCOMPLETE ]; then
        msg_incomplete "$(cv_un_count "$CV_RECORD/verdict/not-checked.txt" '.') list(s)"
    fi
    cv_put_fact UNINSTALLED "$(date -u +%Y-%m-%dT%H:%M:%SZ)" || msg_tool_failed "the record" "1" "UNINSTALLED"
    if [ "$word" = RESIDUE ]; then
        n=$(cv_un_count "$CV_RECORD/verdict/not-explained.txt" '.')
        msg_residue "$n thing(s)"
    fi
    cv_out "Controlvar is uninstalled from account $CV_ACCOUNT, us-west-2. The record stays in $CV_RECORD/ (its logs and both records of the account)."
}
