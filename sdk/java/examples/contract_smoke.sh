#!/usr/bin/env bash
# Contract smoke for the Java SDK.
#
# Builds the core SDK (core/target/classes) plus its runtime dependencies
# (core/target/lib) when they are missing or older than the sources, then runs
# com.controlvar.sdk.examples.ContractSmoke. Only the smoke's JSON line is
# written to stdout; Maven output goes to stderr. Environment variables
# (CONTROLVAR_API_URL, CONTROLVAR_API_KEY, CONTRACT_*) are passed through and the
# exit status is the smoke's (non-zero on failure).
#
#   CONTROLVAR_API_KEY=... bash sdk/java/examples/contract_smoke.sh
#   FORCE_BUILD=1 bash sdk/java/examples/contract_smoke.sh   # always rebuild
set -euo pipefail

SDK_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$SDK_DIR"

SMOKE_CLASS="core/target/classes/com/controlvar/sdk/examples/ContractSmoke.class"

needs_build() {
    [[ "${FORCE_BUILD:-0}" == "1" ]] && return 0
    [[ -f "$SMOKE_CLASS" && -d core/target/lib ]] || return 0
    # Rebuild when any source or POM is newer than the compiled smoke class.
    [[ -n "$(find core/src/main core/pom.xml pom.xml -type f -newer "$SMOKE_CLASS" -print -quit)" ]]
}

if needs_build; then
    ./mvnw -q -B -pl core -am package -DskipTests 1>&2
fi

exec java -cp "core/target/classes:core/target/lib/*" \
    com.controlvar.sdk.examples.ContractSmoke "$@"
