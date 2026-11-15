"""``.gitleaksignore`` pins no line of the generated SDK contract vectors (#1497).

``tests/sdk-contract/ruleset-vectors.json`` is written by
``backend/scripts/generate_ruleset_vectors.py``. A ``path:rule:line`` line for
it in ``.gitleaksignore`` -- the form a tree scan such as ``core_build.sh``'s
secrets step matches -- silences whatever finding the next regeneration puts
on that line, real or not. Four such lines stood there until #1497; the
generator now names its flags so that none reads as a credential, and a
future false positive is fixed the same way, at the generator.

``core_build.sh``'s secrets step proves the vectors scan clean with no ignore
file at all. This test is the cheap half, in every unit job: the pin itself
cannot come back.

The ``commit:path:rule:line`` lines are not touched: they pin findings in
history, which the Secret Detection and Release Gate jobs rescan, and a
commit's content never moves.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import List

import pytest

REPO_ROOT = Path(__file__).resolve().parents[4]
IGNORE_FILE = REPO_ROOT / ".gitleaksignore"
GENERATED = "tests/sdk-contract/ruleset-vectors.json"

# `regression` as well as `unit`: the four line pins this refuses were in the
# file until #1497.
pytestmark = [pytest.mark.unit, pytest.mark.regression]

_COMMIT = re.compile(r"[0-9a-f]{40}")


def tree_pins(text: str) -> List[str]:
    """Every line of an ignore file that pins the vectors without a commit.

    gitleaks reads a line as ``commit:path:rule:line`` or ``path:rule:line``
    and skips blank lines and ``#`` comments. A line whose first field is not
    a commit is the second form; it counts here when its path is the vectors
    file under any root (``./tests/...``, an absolute copy path), so a respelt
    pin is refused as well as the exact one.
    """
    name = GENERATED.rsplit("/", 1)[-1]
    pins = []
    for number, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        path = line.split(":", 1)[0]
        if _COMMIT.fullmatch(path):
            continue
        if path.rsplit("/", 1)[-1] == name:
            pins.append(f"line {number}: {line}")
    return pins


def test_the_vectors_file_is_where_this_test_looks():
    """A moved file would make the test below pass over nothing."""
    assert (REPO_ROOT / GENERATED).is_file(), f"{GENERATED} moved: update this test"


def test_no_tree_pin_for_the_generated_vectors():
    pins = tree_pins(IGNORE_FILE.read_text(encoding="utf-8"))
    assert not pins, (
        f".gitleaksignore pins {GENERATED} by line number, which hides "
        "whatever the next regeneration puts on that line: "
        + "; ".join(pins)
        + ". Rename the flag key in backend/scripts/generate_ruleset_vectors.py, "
        "regenerate, and delete the pin."
    )


_SHA = "0" * 40


@pytest.mark.parametrize(
    "line",
    [
        f"{GENERATED}:generic-api-key:153",
        f"./{GENERATED}:generic-api-key:153",
        f"  {GENERATED}:generic-api-key:304  ",
        f"/tmp/controlvar-core-build.x/{GENERATED}:generic-api-key:305",
    ],
)
def test_a_tree_pin_is_found(line):
    text = f"# a comment\n{_SHA}:scripts/x.py:generic-api-key:2\n{line}\n"
    assert tree_pins(text) == [f"line 3: {line.strip()}"]


@pytest.mark.parametrize(
    "line",
    [
        f"{_SHA}:{GENERATED}:generic-api-key:148",
        f"# {GENERATED}:generic-api-key:153",
        "scripts/check_installer_cli.py:generic-api-key:402",
        "",
    ],
)
def test_a_commit_pin_a_comment_or_another_file_is_not(line):
    assert tree_pins(f"{line}\n") == []
