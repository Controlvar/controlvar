# shellcheck shell=bash
# lib/sweep.sh: the closed, read-only inventory of an account, and the verdict that compares
# two of them. Sourced by controlvar-aws after lib/names.sh; it has no verb of its own.
# Install records the account before its first write (sweep_take); uninstall records it again
# and sorts the difference against the kept list (sweep_verdict).
#
# The inventory is RB Appendix A's closed list for us-west-2, us-east-1 and global: one file per
# type, one item per line, sorted, 58 types in each region and 10 global, 126 files. The kept
# list is RB 10.2's, matched by exact name.
#
# Every aws call goes through cv_aws, the wrapper lib/env.sh defines, and is a list, describe
# or get operation: backend/tests/unit/infrastructure/test_installer_sweep_static.py fails on
# any other.
#
# Where this departs from Appendix A, it fails closed where the runbook's block could not:
# - every read is --output json, the per-item lists included, and an answer that is empty, or
#   is not one JSON list (or null, for a list the service leaves out), is a failed read.
#   Appendix A read the per-item lists as text, where an empty answer reads as "none";
# - an item goes into its per-item command as a whole argument ({}), never through pattern
#   substitution, whose replacement text bash 5.2 can rewrite;
# - the caller passes the folder and the account; nothing is sourced.
#
# Both public functions check every status themselves and never rely on set -e, so a caller
# may run them inside a condition. Bash 3.2: no associative arrays, every array expansion is
# guarded for set -u.

_SWEEP_REGIONS=(us-west-2)

# Appendix A's filter: one line per item, the columns of a row joined by tabs.
_SWEEP_FLAT='(. // []) | if type == "array" then .[] else . end | if type == "array" then map(tostring) | join("\t") else tostring end'

_SWEEP_TAB=$'\t'

# One read: _sweep_json <answer> <errors> <aws arguments...>. Exit 0 only when the call exited 0
# and printed exactly one JSON list or null; otherwise the reason is added to <errors>.
_sweep_json() {
    local out="$1" err="$2" rc=0
    shift 2
    cv_aws "$@" --output json > "$out" 2> "$err" || rc=$?
    if [ "$rc" -ne 0 ]; then
        printf 'exit %s\n' "$rc" >> "$err"
        return 1
    fi
    if [ ! -s "$out" ]; then
        printf 'an empty answer (exit 0, nothing printed)\n' >> "$err"
        return 1
    fi
    if ! jq -e 'type == "array" or type == "null"' "$out" > /dev/null 2>> "$err"; then
        printf 'not one JSON list\n' >> "$err"
        return 1
    fi
    return 0
}

# A failed read's file: the marker line, then the reasons.
_sweep_failed() {  # _sweep_failed <file> <errors> <aws arguments...>
    local f="$1" err="$2"
    shift 2
    _SWEEP_FAILED=$((_SWEEP_FAILED + 1))
    {
        printf 'READ FAILED: aws %s\n' "$*"
        if [ -f "$err" ]; then cat "$err"; fi
    } > "$f"
}

# _sweep_q <dir> <type> <aws arguments...>: one file from one read.
_sweep_q() {
    local f="$_SWEEP_OUT/$1/$2.txt"
    shift 2
    _SWEEP_READS=$((_SWEEP_READS + 1))
    if _sweep_json "$f.json" "$f.err" "$@" &&
        jq -r "$_SWEEP_FLAT" "$f.json" > "$f.rows" 2>> "$f.err" &&
        LC_ALL=C sort "$f.rows" > "$f" 2>> "$f.err"; then
        rm -f "$f.json" "$f.err" "$f.rows"
        return 0
    fi
    _sweep_failed "$f" "$f.err" "$@"
    rm -f "$f.json" "$f.err" "$f.rows"
    return 0
}

# _sweep_each <dir> <type> <list arguments...> -- <per-item arguments, {} for the item...>:
# one file from a list read and one read per item. A failed item read leaves its marker line
# among the good items, and its reason after them.
_sweep_each() {
    local f="$_SWEEP_OUT/$1/$2.txt" item a bad=0
    local list=() cmd=()
    shift 2
    while [ "$#" -gt 0 ] && [ "$1" != "--" ]; do
        list+=("$1")
        shift
    done
    if [ "$#" -eq 0 ]; then
        printf 'sweep: %s has no per-item command\n' "$f" >&2
        return 2
    fi
    shift
    _SWEEP_READS=$((_SWEEP_READS + 1))
    if ! _sweep_json "$f.list" "$f.err" ${list[@]+"${list[@]}"} ||
        ! jq -r '(. // [])[] | tostring' "$f.list" > "$f.items" 2>> "$f.err"; then
        _sweep_failed "$f" "$f.err" ${list[@]+"${list[@]}"}
        rm -f "$f.list" "$f.items" "$f.err"
        return 0
    fi
    : > "$f.rows"
    : > "$f.why"
    while IFS= read -r item; do
        cmd=()
        for a in "$@"; do
            if [ "$a" = "{}" ]; then cmd+=("$item"); else cmd+=("$a"); fi
        done
        _SWEEP_READS=$((_SWEEP_READS + 1))
        if _sweep_json "$f.one" "$f.one.err" ${cmd[@]+"${cmd[@]}"} &&
            jq -r "$_SWEEP_FLAT" "$f.one" >> "$f.rows" 2>> "$f.one.err"; then
            continue
        fi
        _SWEEP_FAILED=$((_SWEEP_FAILED + 1))
        bad=1
        printf 'READ FAILED: aws %s\n' "${cmd[*]+"${cmd[*]}"}" >> "$f.rows"
        if [ -f "$f.one.err" ]; then cat "$f.one.err" >> "$f.why"; fi
    done < "$f.items"
    if ! LC_ALL=C sort "$f.rows" > "$f"; then
        _sweep_failed "$f" "$f.why" ${list[@]+"${list[@]}"}
    elif [ "$bad" -eq 1 ]; then
        cat "$f.why" >> "$f"
    fi
    rm -f "$f.list" "$f.items" "$f.err" "$f.rows" "$f.why" "$f.one" "$f.one.err"
    return 0
}

# sweep_take <folder> <account>: records the account into <folder>, which must not exist yet.
# Prints "sweep <name>: <n> reads, <m> failed -> <folder>". Exit 0 when every read succeeded;
# 1 when any failed (each such file holds READ FAILED and the reason, never an empty list);
# 2 when it could not start (nothing written).
sweep_take() {
    local account="$2" got r
    _SWEEP_OUT="$1"
    _SWEEP_READS=0
    _SWEEP_FAILED=0
    if ! declare -F cv_aws > /dev/null; then
        printf 'sweep: cv_aws is not defined: source lib/env.sh first\n' >&2
        return 2
    fi
    case "$account" in
        *[!0-9]* | '')
            printf 'sweep: %s is not a 12-digit account ID\n' "$account" >&2
            return 2
            ;;
    esac
    if [ "${#account}" -ne 12 ]; then
        printf 'sweep: %s is not a 12-digit account ID\n' "$account" >&2
        return 2
    fi
    if [ -e "$_SWEEP_OUT" ]; then
        printf 'sweep: %s exists: use a new folder\n' "$_SWEEP_OUT" >&2
        return 2
    fi
    if ! got=$(cv_aws sts get-caller-identity --query Account --output text); then
        printf 'sweep: the sign-in could not be read, so nothing was recorded\n' >&2
        return 2
    fi
    if [ "$got" != "$account" ]; then
        printf 'STOP: the profile signs in to account %s, not %s; nothing was recorded\n' \
            "$got" "$account" >&2
        return 2
    fi
    if ! mkdir -p "$_SWEEP_OUT/global" "$_SWEEP_OUT/us-west-2" "$_SWEEP_OUT/us-east-1"; then
        return 2
    fi

    for r in ${_SWEEP_REGIONS[@]+"${_SWEEP_REGIONS[@]}"}; do
        _sweep_q "$r" cfn-stacks cloudformation list-stacks --region "$r" --query "StackSummaries[?StackStatus!='DELETE_COMPLETE'].[StackName,StackStatus]"
        _sweep_q "$r" ec2-vpcs ec2 describe-vpcs --region "$r" --query "Vpcs[].[VpcId,CidrBlock,IsDefault]"
        _sweep_q "$r" ec2-subnets ec2 describe-subnets --region "$r" --query "Subnets[].[SubnetId,VpcId,CidrBlock]"
        _sweep_q "$r" ec2-route-tables ec2 describe-route-tables --region "$r" --query "RouteTables[].[RouteTableId,VpcId]"
        _sweep_q "$r" ec2-internet-gateways ec2 describe-internet-gateways --region "$r" --query "InternetGateways[].[InternetGatewayId,Attachments[0].VpcId]"
        _sweep_q "$r" ec2-nat-gateways ec2 describe-nat-gateways --region "$r" --query "NatGateways[?State!='deleted'].[NatGatewayId,State,VpcId]"
        _sweep_q "$r" ec2-addresses ec2 describe-addresses --region "$r" --query "Addresses[].[AllocationId,PublicIp]"
        _sweep_q "$r" ec2-network-interfaces ec2 describe-network-interfaces --region "$r" --query "NetworkInterfaces[].[NetworkInterfaceId,InterfaceType,Status,VpcId]"
        _sweep_q "$r" ec2-security-groups ec2 describe-security-groups --region "$r" --query "SecurityGroups[].[GroupId,GroupName,VpcId]"
        _sweep_q "$r" ec2-vpc-endpoints ec2 describe-vpc-endpoints --region "$r" --query "VpcEndpoints[?State!='deleted'].[VpcEndpointId,ServiceName,State]"
        _sweep_q "$r" ec2-network-acls ec2 describe-network-acls --region "$r" --query "NetworkAcls[].[NetworkAclId,VpcId,IsDefault]"
        _sweep_q "$r" ec2-instances ec2 describe-instances --region "$r" --query "Reservations[].Instances[?State.Name!='terminated'][].[InstanceId,State.Name]"
        _sweep_q "$r" ec2-volumes ec2 describe-volumes --region "$r" --query "Volumes[].[VolumeId,State]"
        _sweep_q "$r" ec2-flow-logs ec2 describe-flow-logs --region "$r" --query "FlowLogs[].[FlowLogId,ResourceId]"
        _sweep_q "$r" elbv2-load-balancers elbv2 describe-load-balancers --region "$r" --query "LoadBalancers[].[LoadBalancerName,Type,State.Code]"
        _sweep_q "$r" elbv2-target-groups elbv2 describe-target-groups --region "$r" --query "TargetGroups[].[TargetGroupName,Port]"
        _sweep_q "$r" ecs-clusters ecs list-clusters --region "$r" --query "clusterArns"
        _sweep_each "$r" ecs-services ecs list-clusters --region "$r" --query "clusterArns" -- ecs list-services --region "$r" --cluster "{}" --query "serviceArns"
        _sweep_q "$r" ecs-td-families-active ecs list-task-definition-families --region "$r" --status ACTIVE --query "families"
        _sweep_q "$r" ecs-td-families-inactive ecs list-task-definition-families --region "$r" --status INACTIVE --query "families"
        _sweep_q "$r" ecs-td-inactive ecs list-task-definitions --region "$r" --status INACTIVE --query "taskDefinitionArns"
        _sweep_q "$r" ecs-td-delete-in-progress ecs list-task-definitions --region "$r" --status DELETE_IN_PROGRESS --query "taskDefinitionArns"
        _sweep_q "$r" ecr-repositories ecr describe-repositories --region "$r" --query "repositories[].repositoryName"
        _sweep_q "$r" rds-clusters rds describe-db-clusters --region "$r" --query "DBClusters[].[DBClusterIdentifier,Status]"
        _sweep_q "$r" rds-instances rds describe-db-instances --region "$r" --query "DBInstances[].[DBInstanceIdentifier,DBInstanceStatus]"
        _sweep_q "$r" rds-cluster-snapshots rds describe-db-cluster-snapshots --region "$r" --query "DBClusterSnapshots[].[DBClusterSnapshotIdentifier,SnapshotType]"
        _sweep_q "$r" rds-instance-snapshots rds describe-db-snapshots --region "$r" --query "DBSnapshots[].[DBSnapshotIdentifier,SnapshotType]"
        _sweep_q "$r" rds-cluster-automated-backups rds describe-db-cluster-automated-backups --region "$r" --query "DBClusterAutomatedBackups[].[DBClusterIdentifier,Status]"
        _sweep_q "$r" rds-subnet-groups rds describe-db-subnet-groups --region "$r" --query "DBSubnetGroups[].DBSubnetGroupName"
        _sweep_q "$r" rds-cluster-parameter-groups rds describe-db-cluster-parameter-groups --region "$r" --query "DBClusterParameterGroups[?!starts_with(DBClusterParameterGroupName, 'default.')].DBClusterParameterGroupName"
        _sweep_q "$r" rds-parameter-groups rds describe-db-parameter-groups --region "$r" --query "DBParameterGroups[?!starts_with(DBParameterGroupName, 'default.')].DBParameterGroupName"
        _sweep_q "$r" elasticache-replication-groups elasticache describe-replication-groups --region "$r" --query "ReplicationGroups[].[ReplicationGroupId,Status]"
        _sweep_q "$r" elasticache-cache-clusters elasticache describe-cache-clusters --region "$r" --query "CacheClusters[].[CacheClusterId,CacheClusterStatus]"
        _sweep_q "$r" elasticache-snapshots elasticache describe-snapshots --region "$r" --query "Snapshots[].[SnapshotName,SnapshotSource]"
        _sweep_q "$r" elasticache-subnet-groups elasticache describe-cache-subnet-groups --region "$r" --query "CacheSubnetGroups[].CacheSubnetGroupName"
        _sweep_q "$r" secrets secretsmanager list-secrets --region "$r" --include-planned-deletion --query "SecretList[].[Name,DeletedDate]"
        _sweep_q "$r" kms-aliases kms list-aliases --region "$r" --query "Aliases[].[AliasName,TargetKeyId]"
        _sweep_each "$r" kms-keys kms list-keys --region "$r" --query "Keys[].KeyId" -- kms describe-key --region "$r" --key-id "{}" --query "[KeyMetadata.[KeyId,KeyManager,KeyState,Description]]"
        _sweep_q "$r" logs-groups logs describe-log-groups --region "$r" --query "logGroups[].logGroupName"
        _sweep_q "$r" cloudwatch-alarms cloudwatch describe-alarms --region "$r" --query "[MetricAlarms[].AlarmName, CompositeAlarms[].AlarmName][]"
        _sweep_q "$r" cloudwatch-dashboards cloudwatch list-dashboards --region "$r" --query "DashboardEntries[].DashboardName"
        _sweep_q "$r" sns-topics sns list-topics --region "$r" --query "Topics[].TopicArn"
        _sweep_q "$r" sns-subscriptions sns list-subscriptions --region "$r" --query "Subscriptions[].[TopicArn,Protocol,SubscriptionArn]"
        _sweep_q "$r" ssm-parameters ssm describe-parameters --region "$r" --query "Parameters[].Name"
        _sweep_q "$r" dynamodb-tables dynamodb list-tables --region "$r" --query "TableNames"
        _sweep_q "$r" dynamodb-backups dynamodb list-backups --region "$r" --backup-type ALL --query "BackupSummaries[].[TableName,BackupType,BackupStatus]"
        _sweep_q "$r" cognito-user-pools cognito-idp list-user-pools --region "$r" --max-results 60 --query "UserPools[].Name"
        _sweep_q "$r" lambda-functions lambda list-functions --region "$r" --query "Functions[].FunctionName"
        _sweep_q "$r" codedeploy-applications deploy list-applications --region "$r" --query "applications"
        _sweep_q "$r" autoscaling-ecs application-autoscaling describe-scalable-targets --region "$r" --service-namespace ecs --query "ScalableTargets[].ResourceId"
        _sweep_q "$r" autoscaling-dynamodb application-autoscaling describe-scalable-targets --region "$r" --service-namespace dynamodb --query "ScalableTargets[].ResourceId"
        _sweep_q "$r" acm-certificates acm list-certificates --region "$r" --includes "keyTypes=RSA_1024,RSA_2048,RSA_3072,RSA_4096,EC_prime256v1,EC_secp384r1,EC_secp521r1" --query "CertificateSummaryList[].[DomainName,Status]"
        _sweep_q "$r" events-rules events list-rules --region "$r" --query "Rules[].Name"
        _sweep_q "$r" backup-vaults backup list-backup-vaults --region "$r" --query "BackupVaultList[].BackupVaultName"
        _sweep_each "$r" backup-recovery-points backup list-backup-vaults --region "$r" --query "BackupVaultList[].BackupVaultName" -- backup list-recovery-points-by-backup-vault --region "$r" --backup-vault-name "{}" --query "RecoveryPoints[].ResourceArn"
        _sweep_q "$r" guardduty-detectors guardduty list-detectors --region "$r" --query "DetectorIds"
        _sweep_q "$r" config-recorders configservice describe-configuration-recorders --region "$r" --query "ConfigurationRecorders[].name"
        _sweep_q "$r" tagged-resources resourcegroupstaggingapi get-resources --region "$r" --query "ResourceTagMappingList[].ResourceARN"
    done

    _sweep_q global iam-roles iam list-roles --query "Roles[].[Path,RoleName]"
    _sweep_q global iam-policies-local iam list-policies --scope Local --query "Policies[].PolicyName"
    _sweep_q global iam-instance-profiles iam list-instance-profiles --query "InstanceProfiles[].InstanceProfileName"
    _sweep_q global iam-users iam list-users --query "Users[].UserName"
    _sweep_q global iam-oidc-providers iam list-open-id-connect-providers --query "OpenIDConnectProviderList[].Arn"
    _sweep_q global iam-saml-providers iam list-saml-providers --query "SAMLProviderList[].Arn"
    _sweep_q global route53-hosted-zones route53 list-hosted-zones --query "HostedZones[].[Name,Config.PrivateZone,ResourceRecordSetCount]"
    _sweep_q global route53-health-checks route53 list-health-checks --query "HealthChecks[].Id"
    _sweep_q global s3-buckets s3api list-buckets --query "Buckets[].Name"
    _sweep_q global cloudfront-distributions cloudfront list-distributions --query "DistributionList.Items[].Id"

    if [ "${SWEEP_TIDY:-}" = yes ]; then
        cv_aws ssm delete-parameter --region us-west-2 --name /docs-run/sweep-canary
    fi
    printf 'sweep %s: %s reads, %s failed -> %s\n' \
        "${_SWEEP_OUT##*/}" "$_SWEEP_READS" "$_SWEEP_FAILED" "$_SWEEP_OUT"
    if [ "$_SWEEP_FAILED" -eq 0 ]; then
        return 0
    fi
    return 1
}

# --- the verdict (RB 10.2; S6) ----------------------------------------------------------------

# What AWS makes on first use and keeps: the service-linked roles the stacks need, as
# iam-roles.txt prints them (path, then name). Kept by exact line.
_SWEEP_KEPT_ROLES=(
    "/aws-service-role/ecs.amazonaws.com/ AWSServiceRoleForECS"
    "/aws-service-role/elasticloadbalancing.amazonaws.com/ AWSServiceRoleForElasticLoadBalancing"
    "/aws-service-role/rds.amazonaws.com/ AWSServiceRoleForRDS"
    "/aws-service-role/elasticache.amazonaws.com/ AWSServiceRoleForElastiCache"
    "/aws-service-role/ecs.application-autoscaling.amazonaws.com/ AWSServiceRoleForApplicationAutoScaling_ECSService"
)

# A DynamoDB backup's status: the three values the service has (BackupStatus).
_SWEEP_BACKUP_STATUSES=(AVAILABLE CREATING DELETED)

# _sweep_in <value> <list...>: exit 0 when <value> equals one of the list exactly.
_sweep_in() {
    local value="$1" item
    shift
    for item in "$@"; do
        if [ "$item" = "$value" ]; then
            return 0
        fi
    done
    return 1
}

# _sweep_kept <file> <sign> <line> <account>: exit 0 when the line is on the kept list. Names
# are compared whole; the only exceptions are the two kinds no account can make, AWS's own
# KMS keys (KeyManager AWS) and its aliases (alias/aws/...).
_sweep_kept() {
    local file="$1" sign="$2" line="$3" account="$4" tab="$_SWEEP_TAB"
    local id rest entry family revision table
    case "$file" in
        */kms-aliases.txt)
            case "$line" in
                alias/aws/*) return 0 ;;
            esac
            return 1
            ;;
        */kms-keys.txt)
            id=${line%%"$tab"*}
            rest=${line#*"$tab"}
            case "$rest" in
                "AWS${tab}"*) return 0 ;;
            esac
            # The database key, which CloudFormation can only schedule for deletion. Its ID
            # is AWS's: 36 characters of hex and dashes.
            if [ "$file" = us-west-2/kms-keys.txt ] && [ "$sign" = + ] && [ "${#id}" -eq 36 ] &&
                [ "$rest" = "CUSTOMER${tab}PendingDeletion${tab}${NAMES_DB_KEY_DESCRIPTION}" ]; then
                case "$id" in
                    *[!0-9a-f-]*) return 1 ;;
                esac
                return 0
            fi
            return 1
            ;;
    esac
    if [ "$sign" != + ]; then
        return 1
    fi
    case "$file" in
        global/iam-roles.txt)
            for entry in ${_SWEEP_KEPT_ROLES[@]+"${_SWEEP_KEPT_ROLES[@]}"}; do
                if [ "$line" = "${entry% *}${tab}${entry#* }" ]; then
                    return 0
                fi
            done
            ;;
        us-west-2/dynamodb-backups.txt)
            # The tables' SYSTEM backups, made when CloudFormation deletes a table with point-in-
            # time recovery on; AWS removes them after 35 days.
            for table in ${NAMES_TABLES[@]+"${NAMES_TABLES[@]}"}; do
                rest=${line#"${table}${tab}SYSTEM${tab}"}
                if [ "$rest" != "$line" ] &&
                    _sweep_in "$rest" ${_SWEEP_BACKUP_STATUSES[@]+"${_SWEEP_BACKUP_STATUSES[@]}"}; then
                    return 0
                fi
            done
            ;;
        us-west-2/ecs-td-delete-in-progress.txt)
            # The families' revisions ECS is still deleting.
            for family in ${NAMES_FAMILIES[@]+"${NAMES_FAMILIES[@]}"}; do
                revision=${line#"arn:aws:ecs:us-west-2:${account}:task-definition/${family}:"}
                if [ "$revision" != "$line" ] && [ -n "$revision" ]; then
                    case "$revision" in
                        *[!0-9]*) ;;
                        *) return 0 ;;
                    esac
                fi
            done
            ;;
        us-west-2/ecs-td-families-inactive.txt)
            # A family stays listed until its last revision is gone.
            if _sweep_in "$line" ${NAMES_FAMILIES[@]+"${NAMES_FAMILIES[@]}"}; then
                return 0
            fi
            ;;
        us-west-2/sns-subscriptions.txt)
            # An email subscription never confirmed outlives its topic, with no ARN (N3).
            if [ "$line" = "arn:aws:sns:us-west-2:${account}:${NAMES_ALARM_TOPIC}${tab}email${tab}PendingConfirmation" ]; then
                return 0
            fi
            ;;
    esac
    return 1
}

# sweep_verdict <before> <after> <account> <report> [<also kept>]: sorts every line that differs
# between two sweeps into one class, and writes <report>/{kept,lag,not-checked,not-explained}.txt,
# one "<file><TAB><+ or -><TAB><line>" per line (+ only in after, - only in before). <report>
# must not exist. <also kept> lists further exact lines in the same form, to be kept as well.
#   KEPT           on the kept list (_sweep_kept), or in <also kept>
#   LAG            any line of a tagged-resources.txt: the tagging index trails the services
#   NOT CHECKED    a file either sweep could not read, or that only one sweep has
#   NOT EXPLAINED  everything else, which uninstall leaves alone
# The verdict is CLEAN (kept only), PENDING (kept and lag), INCOMPLETE (a file not checked) or
# RESIDUE (anything not explained, which outranks INCOMPLETE). Prints "verdict <word>: <k> kept,
# <l> lag, <n> not checked, <o> not explained"; exit 0 for CLEAN or PENDING, 1 for RESIDUE or
# INCOMPLETE, 2 when it could not start.
sweep_verdict() {
    local before="$1" after="$2" account="$3" report="$4" also="${5:-}"
    local file kept lag unchecked other word
    if [ ! -d "$before" ] || [ ! -d "$after" ]; then
        printf 'verdict: %s and %s must both be sweeps\n' "$before" "$after" >&2
        return 2
    fi
    if [ -e "$report" ]; then
        printf 'verdict: %s exists: use a new folder\n' "$report" >&2
        return 2
    fi
    if [ -n "$also" ] && [ ! -f "$also" ]; then
        printf 'verdict: %s does not exist\n' "$also" >&2
        return 2
    fi
    if ! mkdir -p "$report"; then
        return 2
    fi
    : > "$report/kept.txt"
    : > "$report/lag.txt"
    : > "$report/not-checked.txt"
    : > "$report/not-explained.txt"
    if ! _sweep_files "$before" "$report/.before" ||
        ! _sweep_files "$after" "$report/.after" ||
        ! LC_ALL=C comm -23 "$report/.before" "$report/.after" > "$report/.before-only" ||
        ! LC_ALL=C comm -13 "$report/.before" "$report/.after" > "$report/.after-only" ||
        ! LC_ALL=C comm -12 "$report/.before" "$report/.after" > "$report/.both"; then
        printf 'verdict: the two sweeps could not be listed\n' >&2
        return 2
    fi
    while IFS= read -r file; do
        printf '%s\t!\tonly in the before sweep\n' "$file" >> "$report/not-checked.txt"
    done < "$report/.before-only"
    while IFS= read -r file; do
        printf '%s\t!\tonly in the after sweep\n' "$file" >> "$report/not-checked.txt"
    done < "$report/.after-only"
    while IFS= read -r file; do
        _sweep_compare "$before" "$after" "$file" "$report" "$account" "$also"
    done < "$report/.both"
    rm -f "$report/.before" "$report/.after" "$report/.before-only" "$report/.after-only" \
        "$report/.both" "$report/.minus" "$report/.plus"

    kept=$(_sweep_count "$report/kept.txt")
    lag=$(_sweep_count "$report/lag.txt")
    unchecked=$(_sweep_count "$report/not-checked.txt")
    other=$(_sweep_count "$report/not-explained.txt")
    if [ "$other" -gt 0 ]; then
        word=RESIDUE
    elif [ "$unchecked" -gt 0 ]; then
        word=INCOMPLETE
    elif [ "$lag" -gt 0 ]; then
        word=PENDING
    else
        word=CLEAN
    fi
    printf 'verdict %s: %s kept, %s lag, %s not checked, %s not explained\n' \
        "$word" "$kept" "$lag" "$unchecked" "$other"
    case "$word" in
        CLEAN | PENDING) return 0 ;;
    esac
    return 1
}

# _sweep_compare <before> <after> <file> <report> <account> <also kept>: one file both sweeps
# hold, its differing lines sorted into the report's classes.
_sweep_compare() {
    local before="$1" after="$2" file="$3" report="$4" account="$5" also="$6"
    local tab="$_SWEEP_TAB" sign src line class
    if grep -q '^READ FAILED: ' "$before/$file" || grep -q '^READ FAILED: ' "$after/$file"; then
        printf '%s\t!\tREAD FAILED\n' "$file" >> "$report/not-checked.txt"
        return 0
    fi
    if ! LC_ALL=C comm -23 "$before/$file" "$after/$file" > "$report/.minus" ||
        ! LC_ALL=C comm -13 "$before/$file" "$after/$file" > "$report/.plus"; then
        printf '%s\t!\tcould not be compared\n' "$file" >> "$report/not-checked.txt"
        return 0
    fi
    for sign in - +; do
        if [ "$sign" = - ]; then src="$report/.minus"; else src="$report/.plus"; fi
        while IFS= read -r line; do
            case "$file" in
                */tagged-resources.txt) class=lag ;;
                *)
                    if _sweep_kept "$file" "$sign" "$line" "$account"; then
                        class=kept
                    elif [ -n "$also" ] &&
                        grep -Fxq -- "${file}${tab}${sign}${tab}${line}" "$also"; then
                        class=kept
                    else
                        class=not-explained
                    fi
                    ;;
            esac
            printf '%s\t%s\t%s\n' "$file" "$sign" "$line" >> "$report/$class.txt"
        done < "$src"
    done
    return 0
}

# _sweep_files <sweep> <list>: writes the sweep's files to <list>, relative and sorted.
_sweep_files() {
    local p
    if ! (cd "$1" && find . -type f -name '*.txt') > "$2.found"; then
        return 1
    fi
    : > "$2.relative"
    while IFS= read -r p; do
        printf '%s\n' "${p#./}" >> "$2.relative"
    done < "$2.found"
    if ! LC_ALL=C sort "$2.relative" > "$2"; then
        return 1
    fi
    rm -f "$2.found" "$2.relative"
}

_sweep_count() {
    local n
    n=$(wc -l < "$1")
    printf '%s\n' "$((n + 0))"
}
