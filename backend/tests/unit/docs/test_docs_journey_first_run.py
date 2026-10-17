"""The first-run journey: the dashboard's first-run card walked on an empty workspace (#1142).

``tests/acceptance/docs/journeys/first-run.yaml`` walks the card on
``/experiments`` (``frontend/src/components/experiments/FirstRunChecklist.tsx``)
on ``compose-empty``: compose-dev's stack with ``SEED=``, so the list is empty
and the card shows. What decides whether that walk can pass for the wrong
reason is held here, in the unit job:

* the stack differs from compose-dev by ``SEED=`` alone, and docker-compose.yml
  applies no seed for an empty ``SEED`` and creates the administrator the
  journey signs in as;
* compose-empty and compose-dev share one project, so whichever comes up
  brings the other down, and compose-empty starts docker with ``SEED`` empty;
* the journey's guide is the card, whose anchors are its ``data-testid``
  values: a step naming another anchor, or a link name the card does not say,
  is refused;
* the experiment key the assignment sends is the one the journey reads on the
  experiment's page, and the user is the card's sample user.

``docker`` is a fake that records its calls. Temporary files only; no network,
no git, nothing from ``modules/``, so it runs the same in
``scripts/core_build.sh``'s copy.
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path
from typing import List

import pytest
import yaml

pytestmark = [pytest.mark.unit]

REPO_ROOT = Path(__file__).resolve().parents[4]
RUNNER_ROOT = REPO_ROOT / "tests" / "acceptance" / "docs"
if str(RUNNER_ROOT) not in sys.path:
    sys.path.insert(0, str(RUNNER_ROOT))

from docs_runner import guide as guides
from docs_runner import loader, stacks
from docs_runner.stacks import ComposeDev, ComposeEmpty

JOURNEY = RUNNER_ROOT / "journeys" / "first-run.yaml"
CARD = "frontend/src/components/experiments/FirstRunChecklist.tsx"


def _journey_data():
    return yaml.safe_load(JOURNEY.read_text(encoding="utf-8"))


def _load(path: Path = JOURNEY):
    return loader.load(path, loader.context_for(REPO_ROOT))


# ---------------------------------------------------------------------------
# The stack
# ---------------------------------------------------------------------------
def test_compose_empty_differs_from_compose_dev_by_an_empty_seed_alone(tmp_path):
    environ = {"PATH": "/usr/bin"}
    dev = ComposeDev(tmp_path, tmp_path / "run", environ).env("core")
    empty = ComposeEmpty(tmp_path, tmp_path / "run", environ).env("core")
    assert {k: v for k, v in empty.items() if dev.get(k) != v} == {"SEED": ""}
    assert set(empty) - set(dev) == {"SEED"}


def test_the_compose_file_seeds_nothing_for_an_empty_seed_and_makes_the_admin():
    api = yaml.safe_load((REPO_ROOT / "docker-compose.yml").read_text())["services"][
        "api"
    ]
    environment = api["environment"]
    # ${SEED-demo}: the default only when SEED is unset; set and empty, no seed.
    assert environment["SEED"] == "${SEED-demo}"
    email, password = ComposeEmpty.ACCOUNTS["admin"]
    assert environment["FIRST_SUPERUSER"] == "${FIRST_SUPERUSER:-" + email + "}"
    assert environment["FIRST_SUPERUSER_PASSWORD"] == (
        "${FIRST_SUPERUSER_PASSWORD:-" + password + "}"
    )
    assert set(ComposeEmpty.ACCOUNTS) == {"admin"}


FAKE_DOCKER = """#!/bin/sh
here="$(dirname "$0")"
printf '%s SEED=%s\\n' "$*" "${SEED-unset}" >> "$here/calls.log"
exit 0
"""
COMPOSE_FILE = """services:
  api:
    ports:
      - "${API_HOST_PORT:-8000}:8000"
  frontend:
    ports:
      - "${FRONTEND_HOST_PORT:-3000}:8080"
"""
DOWN = "compose -p docs-journeys down -v --remove-orphans"
UP = "compose -p docs-journeys up -d --wait --build"


@pytest.fixture
def tree(tmp_path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    (root / "docker-compose.yml").write_text(COMPOSE_FILE)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "docker").write_text(FAKE_DOCKER)
    (bin_dir / "docker").chmod(0o755)
    return root


@pytest.fixture(autouse=True)
def _no_stack_left_up():
    yield
    stacks._COMPOSE_UP.clear()


def _stack(cls, tree: Path, monkeypatch):
    environ = {"PATH": f"{tree.parent / 'bin'}{os.pathsep}/usr/bin{os.pathsep}/bin"}
    stack = cls(tree, tree.parent / "run" / "stacks", environ)
    monkeypatch.setattr(stack, "_served_profile", lambda api_url: "core")
    return stack


def _calls(tree: Path) -> List[str]:
    return (tree.parent / "bin" / "calls.log").read_text().splitlines()


def test_compose_empty_starts_docker_with_an_empty_seed_and_brings_compose_dev_down(
    tree, monkeypatch
):
    dev = _stack(ComposeDev, tree, monkeypatch)
    empty = _stack(ComposeEmpty, tree, monkeypatch)
    dev.up("core")
    assert _calls(tree) == [f"{DOWN} SEED=unset", f"{UP} SEED=unset"]
    running = empty.up("core")
    assert dev.running is None and empty.running is running
    # compose-dev's own down (volumes too), then compose-empty clears and starts.
    assert _calls(tree)[2:] == [f"{DOWN} SEED=unset", f"{DOWN} SEED=", f"{UP} SEED="]
    assert running.name == "compose-empty"
    assert dict(running.accounts) == {"admin": stacks.DEMO_ACCOUNTS["admin"]}


# ---------------------------------------------------------------------------
# The journey and its guide, the card
# ---------------------------------------------------------------------------
def test_a_component_guides_anchors_are_its_test_ids():
    guide = guides.parse(
        "x.tsx",
        "<div data-testid=\"card\">\n  <h2>Title</h2>\n  <p data-testid='step-1'>a</p>\n</div>\n",
    )
    assert guide.anchors == {"card": "", "step-1": ""}
    assert guide.headings == []


def test_the_journey_walks_the_card_on_an_empty_workspace():
    journey = _load()
    assert journey.guide == CARD
    assert journey.stack == "compose-empty"
    entry = loader.read_inventory(RUNNER_ROOT / "inventory.toml")["documents"][CARD]
    assert entry["class"] == "journey" and entry["journey"] == "first-run"
    assert "pending" not in entry
    card = guides.load(REPO_ROOT, CARD)
    assert {step.doc for step in journey.steps} <= set(card.anchors)


def test_the_assignment_sends_the_key_the_experiment_page_shows_and_the_cards_user():
    steps = {step["id"]: step for step in _journey_data()["steps"]}
    shown = re.search(
        r"- term: Key\n\s+- definition:\n\s+- text: (\S+)\n",
        steps["create-experiment"]["expect"]["aria"],
    )
    assert shown, "the create step reads the key on the experiment's page"
    body = steps["assign"]["do"]["api"]["body"]
    assert body["experiment_key"] == shown.group(1)
    card = (REPO_ROOT / CARD).read_text(encoding="utf-8")
    assert f'"user_id": "{body["user_id"]}"' in card
    assert (
        steps["assign"]["do"]["api"]["key"] == steps["keep-key"]["do"]["keep"]["secret"]
    )


@pytest.mark.parametrize(
    "change, refused",
    [
        pytest.param(
            ("doc: checklist-start", "doc: checklist-begin"),
            "the guide has no anchor #checklist-begin",
            id="an anchor the card does not have",
        ),
        pytest.param(
            ("name: Open Admin → API Keys", "name: Open Settings → API Keys"),
            "'Open Settings → API Keys' is not in the guide's text",
            id="a link name the card does not say",
        ),
    ],
)
def test_a_journey_that_strays_from_the_card_is_refused(tmp_path, change, refused):
    text = JOURNEY.read_text(encoding="utf-8")
    assert change[0] in text
    planted = tmp_path / "first-run.yaml"
    planted.write_text(text.replace(change[0], change[1], 1), encoding="utf-8")
    with pytest.raises(loader.Refused) as caught:
        _load(planted)
    assert refused in str(caught.value)


@pytest.mark.regression
def test_the_report_names_the_card_as_the_journeys_guide(tmp_path):
    """The report's header and issue templates take the card's path as the
    guide's: a run of this journey that passed every step once failed its
    report ("guide_path: not a valid guide_path value")."""
    from docs_runner import log, report

    record = log.Record(
        run="1",
        sha="abc",
        stack="compose-empty",
        guide=CARD,
        step=1,
        step_id="sign-in-page",
        heading="first-run-checklist",
        action="goto /login",
        expected="status 200",
        failure_signature="the sign-in page does not answer",
        result="PASS",
        reason="",
        observed="at /login (status 200)",
        snapshot="",
        expected_snapshot="structural only",
        ms=12,
    )
    run = report.GuideRun("first-run", CARD, CARD, "x", records=(record,))
    sha = "c6e0f081d82f1a858e111c86db62aa5d8b8a71d4"
    link = "https://github.com/Controlvar/controlvar/actions/runs/18000000003"
    written = report.guide_markdown(
        run, tmp_path, date="2026-10-13", sha=sha, run_link=link
    )
    assert f"({CARD})" in str(written)
