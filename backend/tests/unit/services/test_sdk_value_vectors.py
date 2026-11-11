"""
``tests/sdk-contract/value-vectors.json``: current, counted, and the server's (#1810).

* **Drift.** The file is regenerated in-process
  (``backend/scripts/generate_value_vectors.py``, the same comparison as
  ``--check``) and must match byte for byte. It reads the file by path and
  never shells out to git, so it runs the same in a checkout, in CI and in
  ``scripts/core_build.sh``'s copy (no ``.git``, no ``modules/``).
* **Counts.** 35 served vectors and 14 malformed ones, pinned here. Every SDK
  that consumes the file asserts it answered exactly 49, so a vector lost from
  the file fails here and in each of them.
* **The contract's fields.** Every vector has exactly the seven fields, a
  known getter and error code, the getter's default, and ``reported`` true
  exactly when ``expect`` is ``"default"``.
* **Coherence.** Each served body, as the server builds it now, carries what
  its case expects: the value itself when a value is expected, a value of
  another type for TYPE_MISMATCH, no value for INVALID_RESPONSE, a 404 for an
  unknown key. So a change in what the route serves fails here, at the
  source, rather than only in the SDK jobs.
* **The server's, not an SDK's.** The generator, run in a fresh interpreter,
  loads no module from under ``sdk/``.
"""

from __future__ import annotations

import difflib
import json
import os
import subprocess
import sys
from typing import Any, Dict, List

import pytest

from backend.scripts import generate_value_vectors as generator

pytestmark = pytest.mark.unit

#: Pinned. A change here is a change to what every consuming SDK answers:
#: regenerate the file and say why in the pull request. Each consumer asserts
#: the total, 49 (sdk/js/__tests__/value-vectors.test.ts,
#: sdk/python/tests/test_value_vectors.py).
EXPECTED_COUNTS = {"served": 35, "malformed": 14}

FIELDS = ["name", "response", "getter", "default", "expect", "error_code", "reported"]
GETTERS = ("string", "number", "json")
CODES = ("TYPE_MISMATCH", "INVALID_RESPONSE", None)


@pytest.fixture(scope="module")
def committed_text() -> str:
    assert generator.VECTORS_PATH.exists(), (
        "tests/sdk-contract/value-vectors.json is missing; run "
        "`python -m backend.scripts.generate_value_vectors`"
    )
    return generator.VECTORS_PATH.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def vectors(committed_text) -> Dict[str, Any]:
    return json.loads(committed_text)


@pytest.fixture(scope="module")
def fresh() -> Dict[str, Any]:
    """The document as the server builds it now."""
    return generator.build_vectors()


def test_the_file_is_what_the_generator_writes(committed_text, fresh):
    """The ``--check`` comparison, in-process: the committed file is exactly
    what the generator writes from the server's code as it is now."""
    current = generator.render(fresh)
    if committed_text != current:
        diff = "".join(
            list(
                difflib.unified_diff(
                    committed_text.splitlines(keepends=True),
                    current.splitlines(keepends=True),
                    fromfile="committed",
                    tofile="regenerated",
                )
            )[:80]
        )
        pytest.fail(
            "tests/sdk-contract/value-vectors.json is stale: run "
            "`python -m backend.scripts.generate_value_vectors` and say in the pull "
            "request why the answers moved.\n" + diff
        )


def test_check_exits_1_on_a_stale_file_and_0_on_the_current_one(tmp_path, fresh):
    stale = tmp_path / "value-vectors.json"
    stale.write_text("{}\n", encoding="utf-8")
    assert generator.main(["--check", "--output", str(stale)]) == 1
    stale.write_text(generator.render(fresh), encoding="utf-8")
    assert generator.main(["--check", "--output", str(stale)]) == 0


@pytest.mark.parametrize("half", sorted(EXPECTED_COUNTS))
def test_each_half_holds_exactly_its_count(vectors, half):
    found = len(vectors[half])
    assert found == EXPECTED_COUNTS[half], (
        f"{EXPECTED_COUNTS[half]} {half} vectors, found {found}"
    )


def test_the_file_holds_the_two_halves_and_nothing_else(vectors):
    assert list(vectors) == ["about", "served", "malformed"]


def _all(vectors: Dict[str, Any]) -> List[Dict[str, Any]]:
    return [*vectors["served"], *vectors["malformed"]]


def test_every_vector_has_the_contract_fields(vectors):
    problems = []
    names = [vector.get("name") for vector in _all(vectors)]
    if len(set(names)) != len(names):
        problems.append("names are not unique")
    for vector in _all(vectors):
        name = vector.get("name")
        if list(vector) != FIELDS:
            problems.append(f"{name}: fields {list(vector)}")
            continue
        response = vector["response"]
        if list(response) != ["status", "body"] or type(response["status"]) is not int:
            problems.append(f"{name}: response {response!r}")
        if vector["getter"] not in GETTERS:
            problems.append(f"{name}: getter {vector['getter']!r}")
            continue
        if vector["default"] != generator.DEFAULTS[vector["getter"]]:
            problems.append(f"{name}: default {vector['default']!r}")
        if vector["error_code"] not in CODES:
            problems.append(f"{name}: error_code {vector['error_code']!r}")
        if type(vector["reported"]) is not bool:
            problems.append(f"{name}: reported {vector['reported']!r}")
        # A getter reports every time it returns the default, and only then. So
        # no served value can ever be the string "default" unnoticed.
        if (vector["expect"] == "default") != vector["reported"]:
            problems.append(
                f"{name}: expect {vector['expect']!r}, reported {vector['reported']!r}"
            )
        if vector["expect"] != "default" and vector["error_code"] is not None:
            problems.append(f"{name}: a value with the code {vector['error_code']!r}")
    assert problems == []


def _coherence_problems(vector: Dict[str, Any]) -> List[str]:
    """What is wrong between a served vector's body and what its case expects."""
    name = vector["name"]
    status = vector["response"]["status"]
    body = vector["response"]["body"]
    getter, expect, code = vector["getter"], vector["expect"], vector["error_code"]
    if status == 404:
        if (expect, code, vector["reported"]) != ("default", None, True):
            return [f"{name}: a 404 is the default, reported, with no code compared"]
        if list(body) != ["detail"]:
            return [f"{name}: a 404 body is {body!r}"]
        return []
    if status != 200 or not isinstance(body, dict):
        return [f"{name}: status {status} body {body!r}"]
    if not isinstance(body.get("value_type"), str):
        return [f"{name}: no value_type in {body!r}"]
    if expect != "default":
        same = generator.ordered_json(body.get("value")) == generator.ordered_json(
            expect
        )
        if "value" not in body or not same or body["value_type"] != getter:
            return [f"{name}: expects {expect!r}, the server serves {body!r}"]
        return []
    if code == "TYPE_MISMATCH":
        if "value" not in body or body["value_type"] == getter:
            return [f"{name}: TYPE_MISMATCH, but the server serves {body!r}"]
        return []
    if code == "INVALID_RESPONSE":
        if "value" in body:
            return [f"{name}: INVALID_RESPONSE, but the server serves {body!r}"]
        return []
    return [f"{name}: a 200 default with the code {code!r}"]


def test_each_served_body_carries_what_its_case_expects(fresh):
    """Run on the document as the server builds it now, not on the committed
    file: serving the on value to a user the flag is off for fails here."""
    problems = [p for vector in fresh["served"] for p in _coherence_problems(vector)]
    assert problems == []


def test_the_served_half_covers_each_outcome(vectors):
    """Each outcome a getter has is in the served half: values (falsy ones
    included), TYPE_MISMATCH, INVALID_RESPONSE, the off value, and a 404."""
    served = vectors["served"]
    values = [v["expect"] for v in served if v["expect"] != "default"]
    for falsy in ("", 0, {}, []):
        assert any(
            generator.ordered_json(v) == generator.ordered_json(falsy) for v in values
        ), falsy
    codes = [v["error_code"] for v in served]
    assert codes.count("TYPE_MISMATCH") == 18
    assert codes.count("INVALID_RESPONSE") == 3
    assert [v["response"]["status"] for v in served].count(404) == 3
    assert (
        sum(1 for v in served if v["response"]["body"].get("reason") == "inactive") == 1
    )


#: Run in a fresh interpreter from the repository root: build the vectors,
#: list every loaded module whose file is under sdk/, then import the Python
#: SDK and list again (the probe's own control: it can see one).
_PROBE = """
import json, sys
from pathlib import Path
sdk = (Path.cwd() / "sdk").resolve()

def under_sdk():
    found = []
    for name, module in list(sys.modules.items()):
        path = getattr(module, "__file__", None)
        if path and sdk in Path(path).resolve().parents:
            found.append(name)
    return sorted(found)

from backend.scripts import generate_value_vectors
generate_value_vectors.build_vectors()
before = under_sdk()
sys.path.insert(0, str(sdk / "python"))
import controlvar
print(json.dumps({"generator": before, "control": under_sdk()}))
"""


def test_the_generator_loads_nothing_from_under_sdk():
    """The expectations come from the case table and the bodies from the
    server; no SDK's code takes part (an SDK cannot grade its own answers)."""
    env = dict(os.environ, APP_ENV="test", TESTING="true", PYTHONDONTWRITEBYTECODE="1")
    run = subprocess.run(
        [sys.executable, "-B", "-c", _PROBE],
        cwd=generator.REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert run.returncode == 0, run.stderr[-2000:]
    report = json.loads(run.stdout.strip().splitlines()[-1])
    assert report["generator"] == []
    assert "controlvar" in report["control"]
