"""Journeys every step of which the runner runs: no ``not_run``, no ``ref:``.

A step that declares ``not_run`` (``waived #<issue>`` among the reasons a
journey may give) is reported NOT RUN, and a journey with a NOT RUN step and no
FAIL is PARTIAL, which does not turn the run red. So a journey that a release
counts on passing could be waived into PARTIAL by one reviewed line, and the
run would stay green. ``registry.EVERY_STEP_RUN`` names the journeys that may
not: the loader refuses a ``not_run`` or a ``ref: doc-examples`` on any of
their steps. It is a list per journey, not a rule for all: every other journey
keeps the waiver.

Held here: the list itself, each journey on it existing (a rename would leave
the list naming nothing), each of them loading as it is, and the refusals on a
copy with ``not_run: waived #1368`` or ``ref: doc-examples`` planted.
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path
from typing import Any, Dict, List

import pytest
import yaml

pytestmark = [pytest.mark.unit]

REPO_ROOT = Path(__file__).resolve().parents[4]
RUNNER_ROOT = REPO_ROOT / "tests" / "acceptance" / "docs"
if str(RUNNER_ROOT) not in sys.path:
    sys.path.insert(0, str(RUNNER_ROOT))

from docs_runner import loader, registry

JOURNEYS = RUNNER_ROOT / "journeys"
#: The journeys whose verdict must be PASS or FAIL: the list is held here, so
#: a change to it is a change to this file too.
EVERY_STEP_RUN = ("demo-shoplab",)
#: One that is not on the list, for the control: its waiver still loads.
WAIVABLE = "demo-streampulse"


def missing(journeys: Path) -> List[str]:
    """The names on ``registry.EVERY_STEP_RUN`` with no journey file in *journeys*."""
    return [
        name
        for name in registry.EVERY_STEP_RUN
        if not (journeys / f"{name}.yaml").is_file()
    ]


def test_the_list_is_the_one_held_here():
    assert registry.EVERY_STEP_RUN == EVERY_STEP_RUN


def test_each_journey_on_the_list_exists():
    assert missing(JOURNEYS) == []


def test_a_renamed_journey_leaves_the_list_naming_nothing(tmp_path):
    copied = tmp_path / "journeys"
    shutil.copytree(JOURNEYS, copied)
    (copied / "demo-shoplab.yaml").rename(copied / "demo-shoplab-store.yaml")
    assert missing(copied) == ["demo-shoplab"]


@pytest.mark.parametrize("name", EVERY_STEP_RUN)
def test_each_journey_on_the_list_loads_and_runs_every_step(name):
    journey = loader.load(JOURNEYS / f"{name}.yaml", loader.context_for(REPO_ROOT))
    assert all(step.not_run is None for step in journey.steps)
    assert all(step.do.kind != "ref" for step in journey.steps)


def _planted(
    tmp_path: Path, name: str, index: int, change: Dict[str, Any]
) -> List[str]:
    """The loader's refusals of a copy of *name* with *change* on its step *index*."""
    data = yaml.safe_load((JOURNEYS / f"{name}.yaml").read_text(encoding="utf-8"))
    data["steps"][index].update(change)
    copy = tmp_path / f"{name}.yaml"
    copy.write_text(yaml.safe_dump(data, sort_keys=False, allow_unicode=True))
    try:
        loader.load(copy, loader.context_for(REPO_ROOT))
    except loader.Refused as refused:
        return refused.problems
    return []


def _rule(name: str) -> str:
    return (
        f"{name} is in registry.EVERY_STEP_RUN, so this runner runs every step of"
        " it and its verdict is PASS or FAIL, never PARTIAL"
    )


@pytest.mark.parametrize("name", EVERY_STEP_RUN)
@pytest.mark.parametrize("where", ["first", "last"])
def test_a_waived_step_is_refused(tmp_path, name, where):
    steps = yaml.safe_load((JOURNEYS / f"{name}.yaml").read_text(encoding="utf-8"))[
        "steps"
    ]
    index = 0 if where == "first" else len(steps) - 1
    problems = _planted(tmp_path, name, index, {"not_run": "waived #1368"})
    label = f"step {index + 1} ({steps[index]['id']})"
    assert problems == [f"{label}: not_run 'waived #1368', but {_rule(name)}"]


@pytest.mark.parametrize("name", EVERY_STEP_RUN)
def test_a_step_needing_aws_is_refused(tmp_path, name):
    problems = _planted(tmp_path, name, 0, {"not_run": "needs-aws"})
    assert any(p.endswith(f"not_run 'needs-aws', but {_rule(name)}") for p in problems)


@pytest.mark.parametrize("name", EVERY_STEP_RUN)
def test_a_step_handed_to_doc_examples_is_refused(tmp_path, name):
    step = yaml.safe_load((JOURNEYS / f"{name}.yaml").read_text(encoding="utf-8"))[
        "steps"
    ][0]
    problems = _planted(
        tmp_path, name, 0, {"do": {"ref": "doc-examples"}, "expect": None}
    )
    assert f"step 1 ({step['id']}): ref: doc-examples, but {_rule(name)}" in problems


def test_a_journey_not_on_the_list_keeps_the_waiver(tmp_path):
    """The control: the same plant in a journey off the list loads."""
    assert WAIVABLE not in registry.EVERY_STEP_RUN
    assert _planted(tmp_path, WAIVABLE, 0, {"not_run": "waived #1368"}) == []
