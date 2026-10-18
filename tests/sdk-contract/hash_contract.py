#!/usr/bin/env python3
"""Run each SDK's own exported hash function against golden-vectors.json.

    python tests/sdk-contract/hash_contract.py --list     # classify every sdk/ dir
    python tests/sdk-contract/hash_contract.py python     # one SDK
    python tests/sdk-contract/hash_contract.py js edge    # several

Each SDK has a thin harness under ``harness/`` that imports the SDK's real,
exported hash function (from the SDK's source or its built package), reads the
``[user_id, flag_key]`` inputs as JSON on stdin and prints what the SDK returned
as JSON on stdout. This script is the only place the SDK's answers are compared
with the vectors, so every SDK is judged by the same rule:

* the hash is within 1e-10 of ``expected_hash`` (one bucket is 2**-32, about
  2.3e-10, so a hash that is off by one bucket fails);
* the MD5 hex digest equals ``md5_hex``, for an SDK that exports one;
* the 3 / 5 / 50 / 100 % rollout inclusions of ``rollout_vectors`` hold;
* exactly ``EXPECTED_HASH_VECTORS`` hash vectors and ``EXPECTED_ROLLOUT_VECTORS``
  rollout vectors were checked. A vector file that lost entries, or a harness
  that returned fewer answers than it was asked for, fails rather than passing
  on fewer checks.

An SDK directory under ``sdk/`` is either in ``RUNNERS`` or in ``NOT_RUN`` with
the reason; ``--list`` fails on a directory that is in neither, so a new SDK
cannot drop out of this job unnoticed.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
HERE = Path(__file__).resolve().parent
VECTORS_PATH = HERE / "golden-vectors.json"
HARNESS = HERE / "harness"

# The number of vectors in golden-vectors.json. Change these together with the
# file: a harness is required to have checked exactly this many.
EXPECTED_HASH_VECTORS = 9
EXPECTED_ROLLOUT_VECTORS = 2

TOLERANCE = 1e-10
ROLLOUT_THRESHOLDS = {
    "rollout_3_percent": 0.03,
    "rollout_5_percent": 0.05,
    "rollout_50_percent": 0.50,
    "rollout_100_percent": 1.0,
}

NPM_CI = ["npm", "ci", "--no-audit", "--no-fund", "--ignore-scripts"]


@dataclass(frozen=True)
class Runner:
    """How to run one SDK's hash under the vectors."""

    function: str  # what is under test, for the log
    command: list[str]
    prepare: list[tuple[str, list[str]]] = field(default_factory=list)  # (cwd, argv)


RUNNERS: dict[str, Runner] = {
    "python": Runner(
        function="controlvar.consistent_hash / md5_hex (sdk/python source)",
        command=[sys.executable, str(HARNESS / "python_sdk.py")],
    ),
    "js": Runner(
        function="consistentHash / md5Hex from the built package (sdk/js/dist)",
        prepare=[("sdk/js", NPM_CI), ("sdk/js", ["npm", "run", "build"])],
        command=["node", str(HARNESS / "js_sdk.cjs")],
    ),
    "edge": Runner(
        function="hashUser / md5Hex from the built package (sdk/edge/dist)",
        prepare=[("sdk/edge", NPM_CI), ("sdk/edge", ["npm", "run", "build"])],
        command=["node", str(HARNESS / "edge_sdk.mjs")],
    ),
    "react-native": Runner(
        # The package ships TypeScript source (main: src/index.ts) and no build;
        # node's type stripping loads that source as it is.
        function="hashUser from sdk/react-native/src/hash.ts (the package ships source)",
        # A full install, not --omit=dev: npm ci replaces node_modules, and a
        # developer running this locally keeps their test dependencies.
        prepare=[("sdk/react-native", NPM_CI)],
        command=[
            "node",
            "--experimental-strip-types",
            "--no-warnings",
            str(HARNESS / "react_native_sdk.mjs"),
        ],
    ),
    "go": Runner(
        function="experimentation.ConsistentHash (sdk/go, via a replace directive)",
        command=["go", "run", "."],
    ),
}

# Where each SDK's harness runs from (default: the repository root).
RUNNER_CWD = {"go": HARNESS / "go"}

NOT_RUN: dict[str, str] = {
    "react": "exports no hash function (flags and assignments are server-decided); "
    "nothing to check",
    "android": "NOT RUN: compiling the Kotlin source needs Maven and the Kotlin "
    "compiler, which this job does not install; the SDK's own HashCompatibilityTest "
    "runs in sdk-unit-tests.yml's android job (nightly, and when sdk/android changes)",
    "ios": "NOT RUN: the hash uses CommonCrypto, which exists only on Apple "
    "platforms; the SDK's own HashCompatibilityTests run in sdk-unit-tests.yml's "
    "ios job (macOS; nightly, and when sdk/ios changes)",
    "java": "NOT RUN by this job (no harness yet); its own unit suite runs in "
    "sdk-unit-tests.yml",
    "dotnet": "NOT RUN by this job (no harness yet); its own unit suite runs in "
    "sdk-unit-tests.yml",
    "ruby": "NOT RUN by this job (no harness yet); its own unit suite runs in "
    "sdk-unit-tests.yml",
    "php": "NOT RUN by this job (no harness yet); its own unit suite runs in "
    "sdk-unit-tests.yml",
    "elixir": "NOT RUN by this job (no harness yet); its own unit suite runs in "
    "sdk-unit-tests.yml (heavy tier)",
    "flutter": "NOT RUN by this job (no harness yet); its own unit suite runs in "
    "sdk-unit-tests.yml (heavy tier)",
    "openfeature": "NOT RUN separately: re-exports sdk/js's consistentHash, "
    "which the js entry checks",
    "openfeature-python": "NOT RUN separately: imports sdk/python's "
    "consistent_hash, which the python entry checks",
}


class ContractError(Exception):
    """An SDK disagreed with the vectors, or could not be run."""


def load_vectors(path: Path = VECTORS_PATH) -> tuple[list[dict], list[dict]]:
    data = json.loads(path.read_text())
    hash_vectors = data["hash_vectors"]
    rollout_vectors = data["rollout_vectors"]
    if len(hash_vectors) != EXPECTED_HASH_VECTORS:
        raise ContractError(
            f"{path.name} has {len(hash_vectors)} hash_vectors, expected "
            f"{EXPECTED_HASH_VECTORS}; update EXPECTED_HASH_VECTORS with the file"
        )
    if len(rollout_vectors) != EXPECTED_ROLLOUT_VECTORS:
        raise ContractError(
            f"{path.name} has {len(rollout_vectors)} rollout_vectors, expected "
            f"{EXPECTED_ROLLOUT_VECTORS}; update EXPECTED_ROLLOUT_VECTORS with the file"
        )
    return hash_vectors, rollout_vectors


def check_results(
    sdk: str,
    hash_vectors: list[dict],
    rollout_vectors: list[dict],
    result: dict,
) -> list[str]:
    """Compare one SDK's answers with the vectors; return the summary lines.

    ``result`` is ``{"hashes": [...], "md5_hex": [...] | None}`` answering the
    hash vectors followed by the rollout vectors, in order.
    """
    vectors = hash_vectors + rollout_vectors
    hashes = result.get("hashes")
    md5s = result.get("md5_hex")
    if not isinstance(hashes, list) or len(hashes) != len(vectors):
        got = len(hashes) if isinstance(hashes, list) else hashes
        raise ContractError(
            f"{sdk}: the harness returned {got} hashes for {len(vectors)} inputs"
        )
    if md5s is not None and (not isinstance(md5s, list) or len(md5s) != len(vectors)):
        raise ContractError(f"{sdk}: the harness returned a malformed md5_hex list")

    failures: list[str] = []
    hash_checked = md5_checked = rollout_checked = 0

    for i, v in enumerate(hash_vectors):
        label = f"{v['user_id']}:{v['flag_key']}"
        h = float(hashes[i])
        if not (0.0 <= h < 1.0) or abs(h - v["expected_hash"]) >= TOLERANCE:
            failures.append(
                f"hash {label!r}: expected {v['expected_hash']!r}, got {h!r}"
            )
        hash_checked += 1
        if md5s is not None:
            if md5s[i] != v["md5_hex"]:
                failures.append(
                    f"md5 {label!r}: expected {v['md5_hex']}, got {md5s[i]}"
                )
            md5_checked += 1

    offset = len(hash_vectors)
    for j, v in enumerate(rollout_vectors):
        label = f"{v['user_id']}:{v['flag_key']}"
        h = float(hashes[offset + j])
        if abs(h - v["expected_hash"]) >= TOLERANCE:
            failures.append(
                f"rollout {label!r}: expected hash {v['expected_hash']!r}, got {h!r}"
            )
        for name, threshold in ROLLOUT_THRESHOLDS.items():
            if name in v and (h < threshold) != v[name]:
                failures.append(
                    f"rollout {label!r} {name}: expected included={v[name]}, hash={h!r}"
                )
        rollout_checked += 1

    if failures:
        raise ContractError(f"{sdk}: " + "; ".join(failures))
    if (
        hash_checked != EXPECTED_HASH_VECTORS
        or rollout_checked != EXPECTED_ROLLOUT_VECTORS
    ):
        raise ContractError(
            f"{sdk}: checked {hash_checked} hash / {rollout_checked} rollout vectors, "
            f"expected {EXPECTED_HASH_VECTORS} / {EXPECTED_ROLLOUT_VECTORS}"
        )
    md5_note = (
        f", {md5_checked}/{EXPECTED_HASH_VECTORS} md5 digests"
        if md5s is not None
        else ", md5: the SDK exports no digest function"
    )
    return [
        f"{sdk}: OK -- {hash_checked}/{EXPECTED_HASH_VECTORS} hash vectors, "
        f"{rollout_checked}/{EXPECTED_ROLLOUT_VECTORS} rollout vectors{md5_note}"
    ]


def run_sdk(sdk: str) -> list[str]:
    runner = RUNNERS[sdk]
    hash_vectors, rollout_vectors = load_vectors()
    print(f"{sdk}: under test: {runner.function}", flush=True)
    for cwd, argv in runner.prepare:
        print(f"{sdk}: $ (cd {cwd} && {' '.join(argv)})", flush=True)
        done = subprocess.run(argv, cwd=REPO_ROOT / cwd, check=False)
        if done.returncode != 0:
            raise ContractError(f"{sdk}: {' '.join(argv)} exited {done.returncode}")

    inputs = [[v["user_id"], v["flag_key"]] for v in hash_vectors + rollout_vectors]
    done = subprocess.run(
        runner.command,
        cwd=RUNNER_CWD.get(sdk, REPO_ROOT),
        input=json.dumps({"inputs": inputs}),
        capture_output=True,
        text=True,
        check=False,
    )
    if done.returncode != 0:
        raise ContractError(
            f"{sdk}: the harness exited {done.returncode}\n{done.stdout}{done.stderr}"
        )
    try:
        result = json.loads(done.stdout)
    except json.JSONDecodeError as exc:
        raise ContractError(
            f"{sdk}: the harness printed no JSON ({exc})\n{done.stdout}{done.stderr}"
        ) from exc
    return check_results(sdk, hash_vectors, rollout_vectors, result)


def classify(sdk_dirs: list[str]) -> list[str]:
    """Return one line per SDK directory; raise on one that is unclassified."""
    both = sorted(set(RUNNERS) & set(NOT_RUN))
    if both:
        raise ContractError(f"in both RUNNERS and NOT_RUN: {both}")
    known = set(RUNNERS) | set(NOT_RUN)
    unknown = sorted(set(sdk_dirs) - known)
    stale = sorted(known - set(sdk_dirs))
    if unknown:
        raise ContractError(
            f"sdk/ has directories this job does not classify: {unknown}; add each "
            "to RUNNERS or to NOT_RUN (with the reason) in hash_contract.py"
        )
    if stale:
        raise ContractError(f"classified SDKs with no directory under sdk/: {stale}")
    lines = [f"{s}: checked by this job" for s in sorted(RUNNERS)]
    lines += [f"{s}: {NOT_RUN[s]}" for s in sorted(NOT_RUN)]
    return lines


def main(argv: list[str]) -> int:
    if not argv:
        print(__doc__)
        return 2
    try:
        if argv == ["--list"]:
            dirs = [p.name for p in (REPO_ROOT / "sdk").iterdir() if p.is_dir()]
            for line in classify(dirs):
                print(line)
                if line.split(": ", 1)[1].startswith("NOT RUN") and os.environ.get(
                    "GITHUB_ACTIONS"
                ):
                    print(f"::notice title=SDK hash not checked here::{line}")
            return 0
        unknown = [a for a in argv if a not in RUNNERS]
        if unknown:
            raise ContractError(f"no runner for {unknown}; runners: {sorted(RUNNERS)}")
        for sdk in argv:
            for line in run_sdk(sdk):
                print(line)
    except ContractError as exc:
        print(f"FAIL {exc}", file=sys.stderr)
        if os.environ.get("GITHUB_ACTIONS"):
            print(f"::error title=SDK hash contract::{str(exc).splitlines()[0]}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
