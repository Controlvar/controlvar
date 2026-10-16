#!/usr/bin/env bash
# Contract smoke for the Android SDK, run on a plain JVM.
#
# The SDK's Kotlin sources touch no android.* API, so `sdk/android/jvm/pom.xml`
# compiles them (plus this smoke's main()) for a normal JVM — no Android SDK and
# no emulator. Builds jvm/target/classes and jvm/target/lib when they are missing
# or older than the sources, then runs
# com.controlvar.android.examples.ContractSmoke. Only the smoke's JSON
# line goes to stdout; Maven output goes to stderr. Environment variables
# (EXPERIMENTLY_API_URL, EXPERIMENTLY_API_KEY, CONTRACT_*) are passed through and
# the exit status is the smoke's (non-zero on failure).
#
#   EXPERIMENTLY_API_KEY=... bash sdk/android/examples/contract_smoke.sh
#   FORCE_BUILD=1 bash sdk/android/examples/contract_smoke.sh   # always rebuild
set -euo pipefail

SDK_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$SDK_DIR/jvm"

SMOKE_CLASS="target/classes/com/controlvar/android/examples/ContractSmoke.class"

needs_build() {
    [[ "${FORCE_BUILD:-0}" == "1" ]] && return 0
    [[ -f "$SMOKE_CLASS" && -d target/lib ]] || return 0
    # Rebuild when any source or the POM is newer than the compiled smoke class.
    [[ -n "$(find ../sdk/src/main src/main pom.xml -type f -newer "$SMOKE_CLASS" -print -quit)" ]]
}

if needs_build; then
    ./mvnw -q -B package -DskipTests 1>&2
fi

exec java -cp "target/classes:target/lib/*" \
    com.controlvar.android.examples.ContractSmoke "$@"
