# shellcheck shell=bash
# lib/record.sh: the install's record on this computer, its lock, the children the installer
# starts, and the signals. Sourced by controlvar-aws.
#
# The record is ~/.controlvar-aws/<account>-us-west-2/, mode 0700, written under umask 077:
#   inputs.sh       the options and INSTALL_ID, written with STARTED
#   aws-config      the installer's copy of one IAM Identity Center profile (lib/env.sh)
#   env.d/<NAME>.sh one fact per file, written to a temporary file and moved
#   records/<n>.json each Route 53 change: the exact change, its zone and the read of the
#                   name before it, "pending" before its call, then "created" (uninstall:
#                   "deleted", "absent" or "kept"; records/<n>-delete.json its delete)
#   before/         what the checks saw; sweep/ the sweeps (before the install, after the
#                   uninstall); verdict/ and kept-also.txt the comparison; logs/ one log
#                   per run; history/ an earlier install's facts and records
#   src/            the release's CDK and its two deploy scripts (git archive of the tag)
#   cdk-out/<n>/    each cdk call's own, new output folder; tools/ the pinned CDK CLI and venv
#   tmp/            secret files until their create call, emptied on every exit and signal
#   docker-config/  the registry login, only while the images copy; lock/ the run's pid
#   home/           the HOME every aws, cdk, crane and cosign child runs with, made again on
#                   each run: only .aws/sso/cache, a link to the reader's (lib/env.sh)
# The lifecycle: no folder; planned (checks only, no STARTED); started (STARTED and a new
# INSTALL_ID, written immediately before the first change in AWS); uninstalled
# (UNINSTALLED, after the comparison); started again (the next install moves the earlier
# facts and records to history/ and writes a new INSTALL_ID).

CV_CHILDREN=()
CV_STEP="the checks"
CV_LOCKED=0
# 1 once a message has ended the run (lib/messages.sh's cv_stop sets it).
CV_SAID=0

# cv_record_init: makes the record's folders and opens the run's log.
cv_record_init() {
    local d
    umask 077
    for d in "" env.d records before sweep logs tmp docker-config no-login lock cdk-out tools; do
        if ! mkdir -p "$CV_RECORD/$d" 2> /dev/null; then
            msg_record_folder "$CV_RECORD/$d"
        fi
    done
    chmod 700 "$CV_RECORD" 2> /dev/null || :
    CV_LOG="$CV_RECORD/logs/$(date -u +%Y%m%dT%H%M%SZ)-$CV_VERB.log"
    : > "$CV_LOG" 2> /dev/null || CV_LOG=""
    # From here on the shell's own stderr is the log. Bash reports there a child in the
    # foreground that a signal ended ("Terminated", when a TERM reaches the whole process
    # group), and a child's error that no redirection sends elsewhere goes there too; the
    # terminal's stderr (fd 9, lib/messages.sh) carries the installer's messages only.
    if [ -n "$CV_LOG" ]; then
        exec 2>> "$CV_LOG"
    fi
}

# cv_lock: one run per record. A lock left by a process that is gone is taken over. A lock
# with no process number yet (its pid file missing or empty) is held: the run that made it
# writes the number just after, so it may well be working.
cv_lock() {
    local held="$CV_RECORD/lock/held" pid
    if mkdir "$held" 2> /dev/null; then
        printf '%s\n' "$$" > "$held/pid"
        CV_LOCKED=1
        return 0
    fi
    pid=$(cat "$held/pid" 2> /dev/null) || pid=""
    case "$pid" in
        '') msg_lock "unknown" ;;
        *[!0-9]*) ;;
        *)
            if kill -0 "$pid" 2> /dev/null; then
                msg_lock "$pid"
            fi
            ;;
    esac
    rm -rf "$held"
    if mkdir "$held" 2> /dev/null; then
        printf '%s\n' "$$" > "$held/pid"
        CV_LOCKED=1
        return 0
    fi
    msg_lock "unknown"
}

# cv_put_fact <name> <value>: one fact in env.d, moved into place.
cv_put_fact() {
    local file="$CV_RECORD/env.d/$1.sh"
    if ! printf 'export %s=%q\n' "$1" "$2" > "$file.new" || ! mv "$file.new" "$file"; then
        cv_note "The installer could not write $file."
        return 1
    fi
    export "$1=$2"
}

# cv_load_facts: every fact in env.d, into this shell.
cv_load_facts() {
    local f
    for f in "$CV_RECORD"/env.d/*.sh; do
        if [ -f "$f" ]; then
            # shellcheck disable=SC1090  # the record's own facts, written by cv_put_fact
            . "$f"
        fi
    done
}

# cv_empty_tmp: nothing secret stays on disk after a run.
cv_empty_tmp() {
    if [ -n "${CV_RECORD:-}" ] && [ -d "$CV_RECORD/tmp" ]; then
        rm -rf "$CV_RECORD/tmp" 2> /dev/null || :
        mkdir -p "$CV_RECORD/tmp" 2> /dev/null || :
    fi
    if [ -n "${CV_RECORD:-}" ] && [ -d "$CV_RECORD/docker-config" ]; then
        rm -rf "$CV_RECORD/docker-config" 2> /dev/null || :
        mkdir -p "$CV_RECORD/docker-config" 2> /dev/null || :
    fi
}

# --- children --------------------------------------------------------------------------------

# cv_bg <stdout file> <stderr file> <command...>: starts a long child in the background, so
# a signal reaches the installer at once (a trapped signal interrupts `wait`); its pid is
# CV_CHILD. Its output is appended to the two files (they may be the same one), and only
# its: the installer's messages go on to the terminal (fds 8 and 9, lib/messages.sh, which
# the child does not get), so a message the signal trap prints is seen. The child runs in
# a process group of its own (job control on for that one fork only), so that stopping it
# stops what it started too: cv_cdk's cdk, a deploy script's aws calls and sleeps. Its
# stdin is /dev/null: a child outside the terminal's process group must never read the
# terminal.
cv_bg() {
    local out="$1" err="$2"
    shift 2
    set -m
    # The fork's own stderr goes nowhere: macOS's /bin/bash 3.2 has printed "child setpgid
    # (<pid> to <pid>): Operation not permitted" there (2 runs in about 550 on a macOS
    # runner), and the child ran all the same. The child's own stderr is still <err>.
    { "$@" < /dev/null >> "$out" 2>> "$err" 8>&- 9>&- & } 2> /dev/null
    CV_CHILD=$!
    set +m
    CV_CHILDREN+=("$CV_CHILD")
}

# cv_kill <pid>: TERM to the child's process group, or to the child if it has none.
cv_kill() {
    kill -TERM -- "-$1" 2> /dev/null || kill -TERM "$1" 2> /dev/null || :
}

# cv_wait <pid>: its exit status; the child is forgotten once it has ended. (Bash 3.2
# writes a line about a job a signal ended; it goes nowhere.)
cv_wait() {
    local pid="$1" rc=0 kept=() p
    wait "$pid" 2> /dev/null || rc=$?
    for p in ${CV_CHILDREN[@]+"${CV_CHILDREN[@]}"}; do
        if [ "$p" != "$pid" ]; then kept+=("$p"); fi
    done
    CV_CHILDREN=(${kept[@]+"${kept[@]}"})
    return "$rc"
}

# cv_run_long <stdout file> <stderr file> <command...>: a long child, waited for; its exit
# status.
cv_run_long() {
    cv_bg "$@"
    cv_wait "$CV_CHILD"
}

# cv_sleep <seconds>: a pause a signal interrupts.
cv_sleep() {
    cv_bg /dev/null /dev/null sleep "$1"
    cv_wait "$CV_CHILD" || :
}

# cv_stop_children: every child still running is stopped, with what it started, and
# waited for.
cv_stop_children() {
    local p
    for p in ${CV_CHILDREN[@]+"${CV_CHILDREN[@]}"}; do
        cv_kill "$p"
    done
    for p in ${CV_CHILDREN[@]+"${CV_CHILDREN[@]}"}; do
        wait "$p" 2> /dev/null || :
    done
    CV_CHILDREN=()
}

# --- signals and the end of a run ------------------------------------------------------------

# cv_on_signal: INT or TERM, to the process group or to the installer alone. The record
# says where it stopped, every child is stopped, nothing secret is left, exit 130. A second
# signal meanwhile is caught and does nothing: `trap ':'`, not `trap ''`, so the signals
# stay trapped. Bash's own line about a child a group's TERM ended ("Terminated") goes to
# the log, the shell's stderr once cv_record_init has opened it.
cv_on_signal() {
    trap ':' INT TERM
    cv_stop_children
    if [ -n "${CV_RECORD:-}" ] && [ -d "$CV_RECORD/env.d" ]; then
        cv_put_fact INTERRUPTED_AT "$CV_STEP" || :
    fi
    cv_empty_tmp
    msg_interrupted "$CV_STEP"
}

# cv_on_exit: on every exit. A run that a message ended has said why (cv_stop). A run that
# ends with an error status and no message, on an error of the installer's own (bash
# reports it in the log), gets a FAILED line and the log's path on the terminal, so that it
# never ends with nothing said. The role credentials the CLI cached in the children's HOME
# go too: nothing of a sign-in but the shared token cache outlives the run.
cv_on_exit() {
    local rc=$? next
    trap ':' INT TERM
    if [ "$rc" -ne 0 ] && [ "$CV_SAID" -ne 1 ] && [ -n "${CV_LOG:-}" ]; then
        next="Run the same command again"
        if cv_changed && [ "${CV_VERB:-}" = uninstall ]; then
            next="Run uninstall again; it continues: $(cv_uninstall_command)"
        elif cv_changed; then
            next="Remove what exists, then install again: $(cv_uninstall_command)"
        fi
        cv_note "FAILED: The installer stopped on an error of its own (exit $rc), which the log names. $next. If it fails the same way, please report it at https://github.com/Controlvar/controlvar/issues, with the log (account ID and email address removed)."
        cv_note "Log: $CV_LOG (it can hold your account ID and email address: remove them before you share it)"
    fi
    cv_stop_children
    cv_empty_tmp
    if [ -n "${CV_CHILD_HOME:-}" ] && [ "$CV_CHILD_HOME" = "${CV_RECORD:-}/home" ]; then
        rm -rf "$CV_CHILD_HOME/.aws/cli/cache" 2> /dev/null || :
        rmdir "$CV_CHILD_HOME/.aws/cli" 2> /dev/null || :
    fi
    if [ "$CV_LOCKED" -eq 1 ]; then
        rm -rf "$CV_RECORD/lock/held" 2> /dev/null || :
    fi
}

cv_traps() {
    trap cv_on_exit EXIT
}

# --- progress --------------------------------------------------------------------------------

CV_STEP_STARTED=0

# The number of steps the verb has: install's nine, uninstall's six.
CV_STEPS=9

# cv_step <n> <label>: announces step n of the verb's, before anything in it is changed.
cv_step() {
    CV_STEP="$2"
    CV_STEP_STARTED=$(date +%s)
    cv_out "[$1/$CV_STEPS] $2 ..."
    if cv_started; then
        cv_put_fact STEP "$2" || :
    fi
}

# cv_step_done <n>: the step's line with its time.
cv_step_done() {
    local took=$(($(date +%s) - CV_STEP_STARTED))
    cv_out "[$1/$CV_STEPS] $CV_STEP .... done  $(cv_duration "$took")"
}

# cv_duration <seconds>: "53 s", "1 min 02 s".
cv_duration() {
    if [ "$1" -lt 60 ]; then
        printf '%d s' "$1"
    else
        printf '%d min %02d s' $(($1 / 60)) $(($1 % 60))
    fi
}
