#!/bin/bash
# The harness's driver for deploy/aws-installer/lib/sweep.sh: sources lib/names.sh and
# lib/sweep.sh as controlvar-aws does, under the launcher's shell options, with cv_aws running
# the aws first on PATH (the harness's fake), then runs one function with its arguments:
#   drive_sweep.sh sweep_take <folder> <account>
#   drive_sweep.sh sweep_verdict <before> <after> <account> <report> [<also kept>]
# shellcheck source-path=SCRIPTDIR
set -euo pipefail
lib="${CV_LIB:?CV_LIB names deploy/aws-installer/lib}"
# shellcheck source=../../../../deploy/aws-installer/lib/names.sh
. "$lib/names.sh"
# shellcheck source=../../../../deploy/aws-installer/lib/sweep.sh
. "$lib/sweep.sh"
cv_aws() { aws "$@"; }
"$@"
