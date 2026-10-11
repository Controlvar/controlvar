#!/usr/bin/env bash
# Install the API fuzzer's pins into a virtualenv of its own, then check them:
#
#   scripts/check_fuzz_install.sh [VENV_DIR]
#
# The venv holds exactly what tests/fuzz/requirements.txt and
# tests/fuzz/constraints.txt pin (--no-deps), and `pip check` fails when a
# package one of them needs is missing, or is pinned to a version another
# package refuses.
#
# This is the one copy of those commands. Three callers run it, so that the
# install a pull request is judged on and the install a fuzz run uses cannot
# differ:
#
#   * .github/workflows/pr-qa-gate.yml, the "Base Requirements Only" job, on
#     every pull request (no argument: a throwaway venv, removed afterwards);
#   * .github/workflows/_platform.yml, the fuzz arm's "Install the fuzzer"
#     step, into $RUNNER_TEMP/fuzz-venv, which the run then uses;
#   * scripts/fuzz_local.sh (`make fuzz`), into its working directory.
#
# VENV_DIR must not exist yet: a venv left over from an earlier install could
# hold packages neither file pins, and `pip check` would pass over them.
# PYTHON names the interpreter that makes the venv (default python3); the
# callers use Python 3.11.
set -euo pipefail

python=${PYTHON:-python3}

if [ "$#" -gt 1 ]; then
  echo "usage: scripts/check_fuzz_install.sh [VENV_DIR]" >&2
  exit 2
fi

# The paths in the pip commands are relative to the repository root, so move
# there -- after the argument has been made absolute.
if [ "$#" -eq 1 ]; then
  case $1 in
    /*) venv=$1 ;;
    *) venv=$PWD/$1 ;;
  esac
  if [ -e "$venv" ]; then
    echo "check_fuzz_install: $venv already exists; give it a directory that does not" >&2
    exit 2
  fi
else
  work=$(mktemp -d "${RUNNER_TEMP:-${TMPDIR:-/tmp}}/fuzz-install.XXXXXX")
  trap 'rm -rf "$work"' EXIT
  venv=$work/venv
fi

cd "$(dirname "${BASH_SOURCE[0]}")/.."

"$python" -m venv "$venv"
"$venv/bin/pip" install --quiet --disable-pip-version-check --no-deps -r tests/fuzz/requirements.txt -r tests/fuzz/constraints.txt
"$venv/bin/pip" check --disable-pip-version-check
echo "check_fuzz_install: tests/fuzz/requirements.txt and constraints.txt install together and pass pip check ($("$venv/bin/python" --version))"
