#!/bin/bash
# install_jq.sh: jq 1.7.1's release binary into /usr/local/bin, checked against the
# SHA-256 the release publishes (its sha256sum.txt), for the CI jobs that run the AWS
# installer beta's tests (deploy/aws-installer, backend/tests/installer_sweeps).
#
# The installer refuses a jq that reports a version older than 1.7.1, and its tests run
# the real jq. The ubuntu-24.04 runner's jq is Ubuntu's 1.7.1 package, which reports
# "jq-1.7"; the release binary reports "jq-1.7.1". Linux x86-64 only.
set -euo pipefail

url="https://github.com/jqlang/jq/releases/download/jq-1.7.1/jq-linux-amd64"
sha256="5942c9b0934e510ee61eb3e30273f1b3fe2590df93933a93d7c58b81d19c8ff5"
tmp=$(mktemp)
trap 'rm -f "$tmp"' EXIT

curl -fsSL -o "$tmp" "$url"
printf '%s  %s\n' "$sha256" "$tmp" | sha256sum -c -
install -m 0755 "$tmp" /usr/local/bin/jq
got=$(/usr/local/bin/jq --version)
if [ "$got" != "jq-1.7.1" ]; then
    echo "install_jq.sh: /usr/local/bin/jq reports '$got', not jq-1.7.1" >&2
    exit 1
fi
if [ "$(command -v jq)" != /usr/local/bin/jq ]; then
    echo "install_jq.sh: the jq first on PATH is $(command -v jq), not /usr/local/bin/jq" >&2
    exit 1
fi
echo "jq: $got at /usr/local/bin/jq"
