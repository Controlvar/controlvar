"""The browser script's limit cases get the same answer from the server (#1545).

``sdk/visual/__tests__/limit-cases.json`` describes page-change documents at
the server's limits (2000 characters of text, 100 changes, 64 KiB), built
with characters that take more than one byte or more than one UTF-16 unit.
The script's unit test builds each one and checks the script's answer; this
test builds the same document and checks that the server gives the answer
the file states, so the two agree at every boundary the script mirrors.
"""

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from backend.app.schemas.visual import VisualChanges, stored_size

CASES_FILE = (
    Path(__file__).resolve().parents[4]
    / "sdk"
    / "visual"
    / "__tests__"
    / "limit-cases.json"
)
CASES = json.loads(CASES_FILE.read_text(encoding="utf-8"))["cases"]


def _build(case):
    """The recipe validate.test.ts follows, step for step."""
    if case["build"] == "text":
        text = case["unit"] * case["count"]
        return {
            "version": 1,
            "changes": [{"op": "set_text", "selector": "#t", "text": text}],
        }
    if case["build"] == "changes":
        changes = [{"op": "hide", "selector": f"#c{i}"} for i in range(case["count"])]
        return {"version": 1, "changes": changes}
    unit = case["unit"]
    width = len(unit.encode("utf-8"))
    per = 2000 // width
    changes = []
    while True:
        following = changes + [{"op": "set_text", "selector": "#t", "text": unit * per}]
        if stored_size({"version": 1, "changes": following}) > case["count"] - 2100:
            break
        changes = following
    last = {"op": "set_text", "selector": "#t", "text": ""}
    changes.append(last)
    rest = case["count"] - stored_size({"version": 1, "changes": changes})
    last["text"] = unit * (rest // width) + "a" * (rest % width)
    return {"version": 1, "changes": changes}


def _accepted(document) -> bool:
    try:
        VisualChanges.model_validate(document)
    except ValidationError:
        return False
    return True


@pytest.mark.parametrize("case", CASES, ids=[case["id"] for case in CASES])
def test_the_server_gives_the_stated_answer(case):
    document = _build(case)
    if case["build"] == "bytes":
        assert stored_size(document) == case["count"]
    assert _accepted(document) is case["accepted"]


def test_there_are_exactly_ten_cases():
    assert len(CASES) == 10
    assert {case["accepted"] for case in CASES} == {True, False}
