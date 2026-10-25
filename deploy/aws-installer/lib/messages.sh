# shellcheck shell=bash
# lib/messages.sh: everything the installer says, and how. Sourced by controlvar-aws.
#
# Every message that ends a run has three parts, in this order: what happened; what exists
# and what it bills (cv_state, from the record, never typed by hand); the exact next step.
# Its first word names the exit code: REFUSED (2, nothing changed), FAILED (1, something
# exists), STOPPED (130, interrupted). Refusals and failures go to stderr, everything else
# to stdout; every line also goes to the log once one exists. No password, key, token,
# secret value or ARN is printed. ASCII only: no colour, no carriage return, no redraw.
# Each message is a function msg_<key>; test_installer_messages.py makes each one happen.

CV_PAGE="https://docs.controlvar.com/self-hosting/aws-install/"
CV_LOG=""
# Where lines go: the launcher's kept copies of the terminal's stdout and stderr, or 1 and 2
# where the launcher did not set them.
CV_OUT_FD="${CV_OUT_FD:-1}"
CV_ERR_FD="${CV_ERR_FD:-2}"

# cv_log <line>: the line, appended to the log.
cv_log() {
    if [ -n "$CV_LOG" ]; then
        printf '%s\n' "$1" >> "$CV_LOG" 2> /dev/null || :
    fi
}

# cv_print <fd> <line>: the line in printable ASCII and newlines only, with no child
# process: a tool's own line, which a message can quote (the CDK's last line, say), can
# carry colour codes, carriage returns or symbols.
cv_print() {
    local LC_ALL=C nl='
'
    printf '%s\n' "${2//[!${nl}[:print:]]/}" >&"$1"
}

# cv_out <line>: a line to stdout and the log.
cv_out() {
    cv_print "$CV_OUT_FD" "$1"
    cv_log "$1"
}

# cv_note <line>: a line to stderr and the log.
cv_note() {
    cv_print "$CV_ERR_FD" "$1"
    cv_log "$1"
}

# cv_account_end <account>: "the account ending in 4444".
cv_account_end() {
    printf 'the account ending in %s' "${1: -4}"
}

# cv_uninstall_command: the command that removes this install, to paste.
cv_uninstall_command() {
    printf '%s uninstall --account %s --aws-profile %s' "$CV_LAUNCHER" "${CV_ACCOUNT:-ACCOUNT}" "${CV_PROFILE:-NAME}"
}

# cv_fact <name>: exit 0 when the record holds the fact <name> (env.d), set or empty.
cv_fact() {
    [ -n "${CV_RECORD:-}" ] && [ -f "$CV_RECORD/env.d/$1.sh" ]
}

# cv_started: an install is under way in this record: STARTED stands without UNINSTALLED.
# (An earlier install's record, uninstalled, is history until the next start moves it.)
cv_started() {
    cv_fact STARTED && ! cv_fact UNINSTALLED
}

# cv_changed: this run has changed something in the account: install once STARTED stands,
# uninstall once it has asked for its first delete (CV_DELETING, lib/uninstall.sh). A
# message that ends a run before then is a refusal: exit 2, nothing changed.
cv_changed() {
    if [ "${CV_VERB:-}" = uninstall ]; then
        [ "${CV_DELETING:-0}" -eq 1 ]
        return
    fi
    cv_started
}

# cv_state: the sentence for what exists now, from the record's facts.
cv_state() {
    local list
    if [ "${CV_VERB:-}" = uninstall ]; then
        cv_uninstall_state
        return 0
    fi
    if ! cv_started; then
        printf 'Nothing was created.'
        return 0
    fi
    list=$(cv_made_list install)
    if cv_fact DEPLOY_STARTED; then
        printf "Now in your account: %s. These bill about \$10 a day until you run uninstall." "$list"
    else
        printf 'Created so far: %s. Nothing that bills by the hour exists yet; what does exist costs cents a month.' "$list"
    fi
}

# cv_made_list <install|uninstall>: what the record says the install made, as one list
# ("the install's record (on this computer only)" when nothing yet). With "uninstall",
# what uninstall deletes of it: the CDK's setup stack left out (uninstall keeps it), and
# nothing printed when the record names nothing in AWS. Every call names its mode: the
# CI runner's shellcheck reports a function whose $1 no call in its own file passes.
cv_made_list() {
    local made=() mode="${1:-}"
    if [ "$mode" != uninstall ] && cv_fact CDKTOOLKIT_STACK_ID; then made+=("the CDK's setup stack"); fi
    if cv_fact REPOSITORIES_CREATED; then
        if cv_fact IMAGES_COPIED; then
            made+=("2 image repositories holding the release images")
        else
            made+=("2 image repositories")
        fi
    fi
    if cv_fact JWT_SECRET_ARN; then made+=("2 secrets"); fi
    if cv_fact CERTIFICATE_ARN; then
        if cv_fact VALIDATION_RECORD; then
            made+=("the certificate and its validation record")
        else
            made+=("the certificate")
        fi
    fi
    if cv_fact DEPLOY_STARTED; then
        made+=("the nine stacks (the network, the database, the cache, the services, the load balancer and the alarms)")
    fi
    if cv_fact ALIAS_RECORD; then made+=("the record ${CV_DOMAIN:-}"); fi
    if cv_fact MIGRATE_TD_ARN; then made+=("a task-definition revision for the database setup"); fi
    if [ "${#made[@]}" -gt 0 ]; then
        cv_join ${made[@]+"${made[@]}"}
    elif [ "$mode" != uninstall ]; then
        printf '%s' "the install's record (on this computer only)"
    fi
}

# cv_stacks_destroyed: how many of the nine stacks uninstall's cdk destroy has deleted, from
# the record's UN_STACK_<name> facts (one per stack, written as its destroy ends), so a run
# again counts what an earlier one deleted.
cv_stacks_destroyed() {
    local stack n=0
    for stack in ${NAMES_STACKS[@]+"${NAMES_STACKS[@]}"}; do
        if cv_fact "UN_STACK_${stack//-/_}"; then n=$((n + 1)); fi
    done
    printf '%s' "$n"
}

# cv_uninstall_state: uninstall's sentence, from the record's facts: what it has deleted
# (the UN_ facts lib/uninstall.sh writes as each part goes) and what of the install is
# still there. Before its first delete, "Nothing was deleted." The CDK's setup stack is
# never among either: uninstall keeps it, and says so when this install made it.
cv_uninstall_state() {
    local deleted=() still=() entry made gone label bill done_list still_list n kept=""
    if [ "${CV_DELETING:-0}" -ne 1 ]; then
        printf 'Nothing was deleted.'
        return 0
    fi
    for entry in \
        "ALIAS_RECORD:UN_ALIAS:the record ${CV_DOMAIN:-}" \
        "DEPLOY_STARTED:UN_STACKS:the nine stacks" \
        "CERTIFICATE_ARN:UN_CERTIFICATE:the certificate" \
        "VALIDATION_RECORD:UN_VALIDATION:its validation record" \
        "JWT_SECRET_ARN:UN_SECRETS:2 secrets" \
        "REPOSITORIES_CREATED:UN_REPOSITORIES:2 image repositories" \
        "MIGRATE_TD_ARN:UN_TASK_DEFINITIONS:the task-definition revisions"; do
        made=${entry%%:*}
        gone=${entry#*:}
        label=${gone#*:}
        gone=${gone%%:*}
        if ! cv_fact "$made"; then continue; fi
        if cv_fact "$gone"; then
            deleted+=("$label")
            continue
        fi
        # Part of the nine deleted (a later destroy failed): how many, from their facts. All
        # nine destroyed but not yet read back as gone stays "the nine stacks".
        if [ "$made" = DEPLOY_STARTED ]; then
            n=$(cv_stacks_destroyed)
            if [ "$n" -gt 0 ] && [ "$n" -lt "${#NAMES_STACKS[@]}" ]; then
                deleted+=("$n of the nine stacks")
                still+=("$((${#NAMES_STACKS[@]} - n)) of the nine stacks")
                continue
            fi
        fi
        still+=("$label")
    done
    done_list="nothing yet"
    if [ "${#deleted[@]}" -gt 0 ]; then done_list=$(cv_join ${deleted[@]+"${deleted[@]}"}); fi
    if [ "${#still[@]}" -eq 0 ]; then
        if cv_fact CDKTOOLKIT_STACK_ID; then
            printf "Deleted so far: %s. Nothing else the install made is in your account but the CDK's setup stack, which uninstall keeps." "$done_list"
        else
            printf 'Deleted so far: %s. Nothing else the install made is in your account.' "$done_list"
        fi
        return 0
    fi
    still_list=$(cv_join ${still[@]+"${still[@]}"})
    bill="these cost cents a month until they are deleted"
    if cv_fact DEPLOY_STARTED && ! cv_fact UN_STACKS; then
        bill="these bill about \$10 a day until they are deleted"
    fi
    if cv_fact CDKTOOLKIT_STACK_ID; then
        kept=" Uninstall keeps the CDK's setup stack this install made (other CDK apps may use it)."
    fi
    printf 'Deleted so far: %s. Still in your account: %s; %s.%s' "$done_list" "$still_list" "$bill" "$kept"
}

# cv_join <item...>: "a", "a and b", "a, b and c".
cv_join() {
    local out="" n=$# i=0 item
    for item in "$@"; do
        i=$((i + 1))
        if [ "$i" -eq 1 ]; then
            out="$item"
        elif [ "$i" -eq "$n" ]; then
            out="$out and $item"
        else
            out="$out, $item"
        fi
    done
    printf '%s' "$out"
}

# cv_stop <code> <what> <next> [<state>]: the three parts, the log line, then exit <code>.
cv_stop() {
    local code="$1" what="$2" next="$3" state="${4:-}" word
    case "$code" in
        1) word="FAILED" ;;
        2) word="REFUSED" ;;
        130) word="STOPPED" ;;
        *) word="FAILED" ;;
    esac
    if [ -z "$state" ]; then
        state=$(cv_state)
    fi
    cv_log "message: ${FUNCNAME[1]:-cv_stop}, exit $code"
    cv_note "$word: $what"
    cv_note "$state"
    cv_note "Next: $next"
    if [ -n "$CV_LOG" ]; then
        cv_note "Log: $CV_LOG (it can hold your account ID and email address: remove them before you share it)"
    fi
    exit "$code"
}

# The next step once something exists: install never continues, so remove and start again;
# uninstall continues where it stopped.
cv_next_after_write() {
    if [ "${CV_VERB:-}" = uninstall ]; then
        printf 'run uninstall again; it continues: %s' "$(cv_uninstall_command)"
        return 0
    fi
    printf 'remove what exists, then install again: %s' "$(cv_uninstall_command)"
}

# --- the command and its options -------------------------------------------------------------

msg_usage() {
    cv_stop 2 "controlvar-aws takes a verb, install or uninstall, then its options (beta)." \
        "run: $CV_LAUNCHER --help"
}

msg_not_an_option() {
    local flag="$1" next="run it again without $1"
    if [ "$flag" = "--profile" ]; then
        next="$next; for your AWS CLI profile, use --aws-profile NAME"
    fi
    cv_stop 2 "$flag is not a beta option: the beta installs the core profile, in staging's shape, in us-west-2 only." "$next"
}

# msg_setting <variable> <value>: one of the installer's CONTROLVAR_AWS_* settings, from the
# environment, is not a whole number (its value shown at most 40 characters long).
msg_setting() {
    cv_stop 2 "The setting $1 is \"${2:0:40}\", and it takes a whole number of at most five digits, with no leading zero (for example 10), so the installer does not start." \
        "unset it (unset $1), or set it to a whole number, then run this command again."
}

msg_option_value() {
    cv_stop 2 "$1 needs a value." "run it again with a value after $1, as $CV_LAUNCHER --help shows."
}

msg_account_missing() {
    cv_stop 2 "--account takes the 12-digit ID of the AWS account to install into, the one you are signed in to. It is missing." \
        "add it, for example --account 111122223333, and run this command again."
}

msg_account_digits() {
    cv_stop 2 "--account takes the 12-digit ID of the AWS account to install into, the one you are signed in to. It is \"$1\", which is not 12 digits." \
        "add it, for example --account 111122223333, and run this command again."
}

msg_domain_missing() {
    cv_stop 2 "--domain is required: the name the install will answer at, directly under a public Route 53 zone of the account." \
        "add it, for example --domain app.example.com, and run this command again."
}

msg_domain_unusable() {
    cv_stop 2 "--domain $1 cannot be used: $2." \
        "give one name under your zone, for example --domain app.your-company.com."
}

msg_domain_example() {
    cv_stop 2 "--domain $1 is in $2, the documentation's example." "use a name in your own zone."
}

msg_alarm_missing() {
    cv_stop 2 "--alarm-email is required: the address AWS emails when an alarm fires. It is also the first administrator's sign-in unless you pass --admin-email." \
        "add it, for example --alarm-email you@your-company.com, and run this command again."
}

msg_email_unusable() {
    cv_stop 2 "$1 $2 cannot be used: $3." "use another address$4."
}

msg_profile_missing() {
    cv_stop 2 "No AWS CLI profile was named, and the beta signs in only with an IAM Identity Center profile." \
        "pass --aws-profile NAME (a profile made with aws configure sso), or set AWS_PROFILE, and run this command again."
}

# --- this computer -------------------------------------------------------------------------

# msg_tools <line...>: one line per missing or old tool.
msg_tools() {
    local what="The installer needs tools that are missing or too old:" line
    for line in "$@"; do
        what="$what
  $line"
    done
    cv_stop 2 "$what" "install or update them and run this command again."
}

msg_no_version() {
    cv_stop 2 "This folder is neither a clone of Controlvar nor a release download (it has no VERSION file), so the installer cannot tell which release it holds." \
        "git clone https://github.com/Controlvar/controlvar.git, then in it git switch --detach \"v\$(cat VERSION)\", and run the installer from there."
}

msg_no_tag() {
    cv_stop 2 "This clone has no tag v$1, the release its VERSION file names, so the installer cannot copy that release's CDK." \
        "run git fetch --tags, then git switch --detach \"v\$(cat VERSION)\", and run this command again."
}

msg_old_release() {
    cv_stop 2 "This folder is release $1, older than 0.36.1, the first release the beta installs." \
        "run git switch --detach \"v\$(cat VERSION)\" in a clone of a newer release, and run this command again."
}

msg_archive() {
    cv_stop 2 "The installer could not copy release v$1's CDK out of this clone (git archive: $2)." \
        "check the clone with: git status, then run this command again."
}

msg_cdk_json() {
    cv_stop 2 "Your ~/.cdk.json $1, and the CDK would apply what it holds to this install as defaults: it can change what is built, run or deployed." \
        "move it aside while the installer runs (mv ~/.cdk.json ~/.cdk.json.aside), run this command again, then move it back."
}

msg_record_folder() {
    cv_stop 2 "The installer could not make the folder $1, where it keeps its record of the install." \
        "check that your home folder is writable (ls -ld ~/.controlvar-aws), then run this command again." \
        "Nothing was changed."
}

msg_lock() {
    local which="process $1" next="wait for it to finish, then run this command again."
    if [ "$1" = unknown ]; then
        which="it has not written its process number yet"
        next="$next If no other run of the installer is open on this computer, one was stopped before it wrote that number: remove the folder $CV_RECORD/lock/held, then run this command again."
    fi
    cv_stop 2 "Another run of the installer is working on $(cv_account_end "$CV_ACCOUNT") on this computer ($which)." "$next"
}

# --- the sign-in ---------------------------------------------------------------------------

# The probe reads only the installer's own environment: its copy of the profile, under its
# own name and with no [default], a HOME holding only the sign-in cache, and none of the
# shell's AWS variables. The reader's own AWS files are not what it read, so the next step
# points at the aws command itself.
msg_probe() {
    cv_stop 2 "With no profile named, an AWS call from the installer's environment did not stop for want of credentials ($1). That environment holds only the installer's copy of your profile, under a name of its own, and your sign-in cache: none of your shell's AWS variables and no other AWS file of yours. The beta signs in only with the profile you name, so it does not start." \
        "check that aws is the AWS CLI itself and not a script that adds credentials (command -v aws; aws --version), then run this command again."
}

msg_no_profile() {
    cv_stop 2 "The AWS CLI config file ($2) has no profile $1." \
        "make one (aws configure sso), sign in, and pass --aws-profile NAME."
}

msg_not_identity_center() {
    cv_stop 2 "The AWS CLI profile $1 does not sign in through IAM Identity Center, and the beta signs in no other way: it never reads access keys." \
        "make one (aws configure sso), sign in, and pass --aws-profile NAME."
}

msg_signin_expired() {
    cv_stop 2 "Your AWS sign-in for profile $1 has expired or was never made." \
        "sign in, then run this command again: aws sso login --profile $1"
}

msg_identity_unreadable() {
    cv_stop 2 "The installer could not read which account the profile $1 signs in to ($2)." \
        "check it with: aws sts get-caller-identity --profile $1, then run this command again."
}

msg_account_mismatch() {
    cv_stop 2 "--account says $(cv_account_end "$1"), but you are signed in to $(cv_account_end "$2") ($3). The installer acts only in the account you name." \
        "run this command again with --account for the account you are signed in to, or sign in to the account you meant."
}

msg_token() {
    cv_stop 2 "The installer needs a fresh sign-in: the one for $1 was last renewed $2." \
        "sign in again (aws sso login --profile $1), then run this command again."
}

msg_session_ended() {
    if [ "${CV_VERB:-}" = uninstall ]; then
        local code=2
        if cv_changed; then code=1; fi
        cv_stop "$code" "The sign-in for $1 stopped working ($2), so the installer stopped before deleting the nine stacks." \
            "sign in again (aws sso login --profile $1), then run uninstall again; it continues: $(cv_uninstall_command)"
    fi
    cv_stop 1 "The sign-in for $1 stopped working ($2), so the installer stopped before creating the nine stacks." \
        "sign in again (aws sso login --profile $1), then remove what exists and install again: $(cv_uninstall_command)"
}

# --- the account ---------------------------------------------------------------------------

msg_read_failed() {
    cv_stop 2 "The installer could not read $1 ($2), and it does not guess." \
        "sign in with an administrator permission set (AdministratorAccess), then run this command again. The full error is in the log."
}

msg_region_missing() {
    cv_stop 2 "This account does not have $1 enabled, and the beta needs it: it installs in us-west-2 and records us-east-1 before and after." \
        "enable it (AWS console > Account > AWS Regions), wait until it shows as enabled, and run this command again."
}

msg_engine() {
    cv_stop 2 "us-west-2 cannot run this install for this account: $1 is not offered there." \
        "the beta installs only in us-west-2, so it cannot install in this account yet."
}

msg_quota() {
    cv_stop 2 "The limit \"$1\" in us-west-2 is $2 for this account. The install needs $3." \
        "ask AWS to raise it (it is free): Service Quotas > $4 > \"$1\" in us-west-2. Then run this command again."
}

msg_quota_unreadable() {
    cv_stop 2 "The installer could not read the limit \"$1\" in us-west-2 ($2)." \
        "sign in with an administrator permission set (AdministratorAccess), then run this command again."
}

msg_permission_check() {
    cv_stop 2 "The installer could not check what your sign-in may do ($1: $2), and it will not guess." \
        "sign in with an administrator permission set (AdministratorAccess), or ask your administrator to allow iam:GetRole and iam:SimulatePrincipalPolicy for this role, then run this command again."
}

msg_permission_denied() {
    cv_stop 2 "Your sign-in ($1) is not allowed to do $2 things the installer needs, for example $3. The installer creates IAM roles and sets up the CDK in your account, so it needs to run as an administrator of the account." \
        "sign in with an administrator permission set (AdministratorAccess) and run this command again. The full list is in the log."
}

msg_stacks_present() {
    cv_stop 2 "This account holds Controlvar stacks that this installer did not create: $1. The installer only installs into an account with no Controlvar environment it did not make, in any region, so that it cannot affect one." \
        "install in another account (a new account is best). To manage that environment, use the CDK guide: https://docs.controlvar.com/self-hosting/cdk/"
}

msg_cdktoolkit_state() {
    cv_stop 2 "This account's CDK setup (the CDKToolkit stack in us-west-2) is $1, so the installer will not use it. Other CDK apps in the account may share it." \
        "wait until it is CREATE_COMPLETE or UPDATE_COMPLETE (CloudFormation > CDKToolkit > Events shows why it is not), then run this command again."
}

msg_repository_present() {
    cv_stop 2 "An image repository named $1 already exists in us-west-2 and was not made by this install, so the installer will not use or replace it. It may belong to another Controlvar environment." \
        "install in another account (a new account is best)."
}

msg_secret_present() {
    cv_stop 2 "A secret named $1 already exists in us-west-2$2 and was not made by this install, so the installer will not use or replace it. It may belong to another Controlvar environment." \
        "install in another account (a new account is best)."
}

# --- the zone ------------------------------------------------------------------------------

msg_no_zone() {
    cv_stop 2 "This account has no public Route 53 zone for $1 (it looked for $2); the beta writes only into a zone in this account." \
        "use a name in $3, or make a zone first: ${CV_PAGE}#if-you-do-not-have-a-zone-yet"
}

msg_zone_private() {
    cv_stop 2 "This account's zone $1 is private, so AWS could not issue the certificate." \
        "use a name in a public zone, or make one first: ${CV_PAGE}#if-you-do-not-have-a-zone-yet"
}

msg_zone_twice() {
    cv_stop 2 "This account holds $2 public zones named $1, and the installer cannot tell which one the internet uses." \
        "keep the one the internet uses (dig +short NS $1 names its servers) and run this command again."
}

msg_zone_not_delegated() {
    cv_stop 2 "The zone $1 is in this account, but the internet does not use it: public DNS names $2." \
        "set its NS records at the parent to $3, check with dig +short NS $1, run this command again."
}

msg_zone_top() {
    cv_stop 2 "$1 is the top of its zone; the beta installs at a name under it." \
        "use, for example, app.$1."
}

msg_record_present() {
    cv_stop 2 "$1 already has a $2 record, and the installer will not overwrite it." \
        "remove that record, or choose another name with --domain, then run this command again."
}

# --- the record on this computer -----------------------------------------------------------

msg_started() {
    cv_stop 2 "This computer holds the record of an install in $(cv_account_end "$CV_ACCOUNT"), us-west-2, made $1, that $2. The beta does not change or continue an install." \
        "remove it ($(cv_uninstall_command)), then install again." "Nothing was changed."
}

msg_sweep_failed() {
    cv_stop 2 "The installer could not read $1 while recording the account, so uninstall could not show what it left; it does not start without a complete record." \
        "sign in with an administrator permission set (AdministratorAccess), then run this command again. Each failed read is in $2."
}

msg_no_terminal() {
    if [ "${CV_VERB:-}" = uninstall ]; then
        cv_stop 2 "This would delete the install and everything in it, and there is no terminal to type the account ID on." \
            "run the command in a terminal, or add --yes to confirm without typing (the plan is printed above)."
    fi
    cv_stop 2 "This would create resources that cost about \$10 a day, and there is no terminal to type the account ID on." \
        "run the command in a terminal, or add --yes to confirm without typing (the plan is printed above)."
}

msg_not_confirmed() {
    if [ "${CV_VERB:-}" = uninstall ]; then
        cv_stop 2 "Not confirmed. What was typed is not the account ID shown above." \
            "run uninstall again when you are ready, and type the account ID exactly as shown."
    fi
    cv_stop 2 "Not confirmed. What was typed is not the account ID shown above." \
        "run the command again when you are ready, and type the account ID exactly as shown."
}

# msg_before_unwritten <what> <file>: a list of what was here, which uninstall reads to
# leave alone what it holds, could not be written.
msg_before_unwritten() {
    cv_stop 2 "The installer could not write its list of $1 ($2 in the record), which uninstall reads to leave alone what was here before the install, so it does not start." \
        "check that the record's folder can be written (ls -ld $CV_RECORD/before), then run this command again."
}

# --- the release images --------------------------------------------------------------------

msg_image() {
    cv_stop 2 "The image $1 $2, so the installer will not copy it into your account. Expected signer: release.yml in github.com/Controlvar/controlvar (the name is case-sensitive)." \
        "do not work around this: the image may not be the one the release published. Report it privately to the maintainers (the policy file at the top of this repository says how), with the image name above. To check an image by hand: https://docs.controlvar.com/self-hosting/releases/#verifying-what-you-pulled"
}

msg_image_missing() {
    cv_stop 2 "The image $1 is not on ghcr.io (there is no tag $2)." \
        "if release v$3 was published in the last hour, its images may still be uploading: wait a few minutes and run this command again."
}

msg_image_copied_wrong() {
    cv_stop 1 "The image stored in your repository $1 under the tag bootstrap is not the one that was verified (it has a different digest)." \
        "$(cv_next_after_write). If it happens again, report it privately to the maintainers (the policy file at the top of this repository says how)."
}

msg_unreachable() {
    local code=2
    if cv_changed; then code=1; fi
    cv_stop "$code" "The installer could not reach $1 ($2)." \
        "check your network, proxy and firewall settings, then run this command again. The hosts the installer needs are: AWS, ghcr.io, Sigstore, npm, PyPI and 1.1.1.1."
}

# --- after the first write -----------------------------------------------------------------

# msg_tool_failed <tool> <exit code> <last line>: anything that failed after the first write
# (a refusal before it).
msg_tool_failed() {
    local code=2 next="run the same command again. If it fails the same way, please report it at https://github.com/Controlvar/controlvar/issues, with the log (account ID and email address removed)."
    if cv_changed; then
        code=1
        next="$(cv_next_after_write). If it fails the same way, please report it at https://github.com/Controlvar/controlvar/issues, with the log (account ID and email address removed)."
    fi
    cv_stop "$code" "$1 stopped with an error (exit code $2): $3. The full output is in the log." "$next"
}

msg_certificate_failed() {
    cv_stop 1 "AWS could not issue the certificate for $1: it is $2 ($3)." \
        "$(cv_next_after_write). A CAA record that does not allow Amazon (amazon.com) shows here as CAA_ERROR."
}

msg_certificate_timeout() {
    cv_stop 1 "After 45 minutes AWS had still not issued the certificate for $1 (it is $2)." \
        "check the validation record with dig +short CNAME $3, then $(cv_next_after_write)"
}

msg_validation_record() {
    cv_stop 1 "The certificate's validation record $1 already holds something else ($2), and the installer will not overwrite it." \
        "$(cv_next_after_write)"
}

msg_service_role() {
    cv_stop 1 "AWS did not create the ECS service-linked role AWSServiceRoleForECS ($1)." \
        "$(cv_next_after_write)"
}

msg_target() {
    cv_stop 1 "The CDK's list of what it would deploy is not the nine staging stacks in this account and us-west-2 ($1), so the installer stopped before creating any of them." \
        "$(cv_next_after_write). A release whose stack list differs needs the installer of that release."
}

msg_stack_failed() {
    cv_stop 1 "AWS stopped creating the stack $1: $2." \
        "the log has AWS's events for each stack; $(cv_next_after_write)"
}

msg_api_cannot_start() {
    cv_stop 1 "The API service cannot start its tasks: $1. The installer stopped waiting instead of letting this run on; AWS keeps working on the stack until its own time limit." \
        "$(cv_next_after_write)"
}

msg_watch_cap() {
    cv_stop 1 "AWS was still creating $1 $2 minutes after it started, longer than the installer waits; AWS keeps working on it until its own time limit." \
        "$(cv_next_after_write)"
}

msg_watch_read() {
    cv_stop 1 "The installer could not read the stack $1 while AWS created it ($2)." \
        "$(cv_next_after_write)"
}

msg_stacks_not_complete() {
    cv_stop 1 "The deploy ended, but not every one of the nine stacks is complete: $1." \
        "the log has AWS's events for each stack; $(cv_next_after_write)"
}

msg_alias_record() {
    cv_stop 1 "$1 now has a $2 record that was not there when the installer checked, so it did not point $1 at the load balancer." \
        "$(cv_next_after_write)"
}

msg_migration_failed() {
    cv_stop 1 "The database setup task stopped without creating the first administrator $1: $2. Its last log lines are in the log; they name the administrator's email and never the password." \
        "$(cv_next_after_write)"
}

msg_check_failed() {
    cv_stop 1 "The install is created, but a check from the internet failed: $1 answered $2." \
        "look at the API's logs (CloudWatch Logs, group /ecs/${NAMES_API_SERVICE:-} in us-west-2). To stop the bill: $(cv_uninstall_command)"
}

msg_interrupted() {
    local what="Stopped by Ctrl-C or a signal at \"$1\"."
    if [ "${CV_VERB:-}" = uninstall ]; then
        if [ "${CV_DESTROYING:-0}" -eq 1 ]; then
            what="$what AWS carries on with the stack it was deleting, on its own."
        fi
    elif cv_fact DEPLOY_STARTED; then
        what="$what AWS carries on creating the stacks on its own."
    fi
    if cv_started; then
        cv_stop 130 "$what" "$(cv_next_after_write)"
    fi
    cv_stop 130 "$what" "run the command again when you are ready."
}

# --- uninstall -------------------------------------------------------------------------------

# cv_code_now: 1 once this run has changed something, 2 before (a refusal).
cv_code_now() {
    if cv_changed; then printf '1'; else printf '2'; fi
}

msg_install_only() {
    cv_stop 2 "$1 is an option of install, not of uninstall: uninstall reads the install's name and addresses from its record." \
        "run it again without $1."
}

# msg_no_record <why> [<value>]: uninstall finds no started install of this account in
# this computer's record (none, planned only, uninstalled, another account's, unreadable).
msg_no_record() {
    local what
    case "$1" in
        none) what="This computer has no record of an install in $(cv_account_end "$CV_ACCOUNT"), us-west-2 (there is no folder $CV_RECORD)." ;;
        not-started) what="This computer's record for $(cv_account_end "$CV_ACCOUNT"), us-west-2, holds no install: its checks ran, and nothing was created." ;;
        uninstalled) what="The install in $(cv_account_end "$CV_ACCOUNT"), us-west-2, was uninstalled ($2), and the record holds no install made since." ;;
        other) what="The record in $CV_RECORD is of $(cv_account_end "$2"), not of $(cv_account_end "$CV_ACCOUNT"), so it is not this account's." ;;
        *) what="The record in $CV_RECORD cannot be read (its inputs.sh is missing or incomplete)." ;;
    esac
    cv_stop 2 "$what The installer deletes only what its own record says it made." \
        "if the install was made on another computer, run uninstall there (its record is in ~/.controlvar-aws/). To take down an environment made another way: https://docs.controlvar.com/self-hosting/cdk/#destroying-an-environment"
}

msg_release_differs() {
    cv_stop 2 "This install was made by Controlvar $1, and this folder holds $2. Uninstall runs the same CDK code that made the install, so it needs the $1 files." \
        "run git switch --detach \"v$1\" in this clone (git fetch --tags first if it has no such tag), then run uninstall again."
}

msg_deployment_active() {
    cv_stop "$(cv_code_now)" "A CodeDeploy deployment of the API is still under way ($1 active in $NAMES_CODEDEPLOY_APPLICATION), and deleting the stacks under it would leave it half done." \
        "wait for it to end (CodeDeploy > Deployments in the AWS console shows it), then run uninstall again; it continues."
}

msg_stack_wait_cap() {
    cv_stop "$(cv_code_now)" "AWS is still working on $1 after $CV_STACK_WAIT_MINUTES minutes, longer than the installer waits." \
        "run uninstall again to keep waiting, or look at the stack in the AWS console (CloudFormation > the stack > Events)."
}

# msg_stack_delete_failed <stack> <resource> <reason> <resource type>: E26d.
msg_stack_delete_failed() {
    local next
    next="fix the cause and run uninstall again; it continues where it stopped: $(cv_uninstall_command)"
    if [ "$4" = "AWS::ECS::Service" ]; then
        next="the API's service is what stops it: delete it (aws ecs delete-service --cluster $NAMES_CLUSTER --service $NAMES_API_SERVICE --force --region us-west-2 --profile ${CV_PROFILE:-NAME}), then run uninstall again; it continues where it stopped."
    fi
    cv_stop 1 "AWS could not delete $2 in the stack $1: $3." "$next"
}

# msg_network_guardduty <vpc> <endpoint IDs> <group IDs>: the IDs space-separated, either
# list possibly empty; the next step prints the commands that list the network's interfaces
# and delete them.
msg_network_guardduty() {
    local where="--region us-west-2 --profile ${CV_PROFILE:-NAME}" deletes="" then="" g
    if [ -n "$2" ]; then
        deletes="aws ec2 delete-vpc-endpoints --vpc-endpoint-ids $2 $where"
        then=", then, once the endpoint is gone, "
    fi
    for g in $3; do
        deletes="$deletes${then}aws ec2 delete-security-group --group-id $g $where"
        then=", then "
    done
    cv_stop 1 "AWS could not delete the network: GuardDuty put its own endpoint in it (${2:-none}), with the group GuardDuty made for it (${3:-none}). The installer did not make them, so it does not delete them." \
        "check that they are all that is left in the network (aws ec2 describe-network-interfaces --filters Name=vpc-id,Values=$1 --query 'NetworkInterfaces[].[NetworkInterfaceId,InterfaceType,Description]' --output text $where), then run uninstall again; it continues with the network. ${CV_PAGE}#if-uninstall-stops-at-the-network says more."
}

msg_stacks_left() {
    cv_stop "$(cv_code_now)" "Not every one of the nine stacks is gone ($1), so the installer keeps the image repositories, which they use." \
        "look at those stacks in the AWS console (CloudFormation > Stacks), then run uninstall again; it continues."
}

msg_compare_failed() {
    cv_stop 1 "The installer deleted what the install made, but could not compare the account with its record from before the install ($1)." \
        "run uninstall again; it continues with the comparison: $(cv_uninstall_command)"
}

msg_residue() {
    cv_stop 1 "The install is removed, but the account holds $1 that the record from before the install does not, and that the installer could not tell are this install's: the report above lists them under \"Not explained\". The installer leaves them alone." \
        "look at each one (someone else may have made it while the install ran). Both records of the account are in $CV_RECORD/sweep/."
}

msg_incomplete() {
    cv_stop 1 "The install is removed, but $1 of the account could not be read afterwards, so the comparison is not complete (the report above names them)." \
        "run uninstall again to compare again; what is gone stays gone: $(cv_uninstall_command)"
}
