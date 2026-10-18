"""The docs-journey runner's pure parts: the model, the loader, the log, the reports.

The runner lives in ``tests/acceptance/docs/`` (#939) and drives a browser,
which does not belong in the unit job. Its schema, its load-time refusals, its
results log and its report renderer import neither Playwright nor ``backend``,
so they are tested here, on every pull request, including one that changes only
documentation: a guide edit that removes an anchor or a label a journey uses
fails ``test_every_journey_file_loads`` in the "Docs content tests" step,
through this directory.

Each refusal is shown firing on a small journey with one planted defect, in a
temporary docs tree. The compose stack runs against a fake ``docker``, and the
runner's own pytest session (no skip passes, no retry plugin loads) runs in a
subprocess on a copy of its conftest. Temporary files only; no git, no network.
"""

from __future__ import annotations

import copy
import datetime
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import textwrap
import tomllib
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

import pytest
import yaml

pytestmark = [pytest.mark.unit]

REPO_ROOT = Path(__file__).resolve().parents[4]
RUNNER_ROOT = REPO_ROOT / "tests" / "acceptance" / "docs"
if str(RUNNER_ROOT) not in sys.path:
    sys.path.insert(0, str(RUNNER_ROOT))

from docs_runner import checks, loader, log, registry, report, site
from docs_runner import guide as guides
from docs_runner.model import Journey, Named

TODAY = datetime.date(2026, 10, 6)

GUIDE = """# Sample guide

Some words about the guide.

## Sign in

Open the dashboard and click **Sign in**. Fill **Email** and **Password** with
the account the seed creates, then choose **Production** in **Environment**.
Its settings live in `settings.json` and `.env`; the **.NET** tab is for C#.

```bash
# Not a heading: a comment in a shell block
ls docs
```

## Create an experiment {#create}

Click **New experiment**. The page shows the heading "Experiments" and the
total sample size.

```{.bash exec}
curl -fsS http://localhost:8000/health
```

## Create an experiment

The same title again: the first one has an id of its own, so this one keeps its slug.
"""

INVENTORY: Dict[str, Any] = {
    "pages": {
        "guides/sample.md": {
            "class": "journey",
            "journey": "sample",
            "reason": "walked",
        },
        "guides/other.md": {"class": "reference", "reason": "nothing to walk"},
        "guides/later.md": {
            "class": "journey",
            "journey": "later",
            "pending": True,
            "planned": "D3a",
            "reason": "to be walked",
        },
    },
    "flow": [
        {
            "text": "Quick Start, end to end",
            "journeys": ["sample", "later"],
            "note": "walked in a browser",
        }
    ],
}

GOOD: Dict[str, Any] = {
    "guide": "guides/sample.md",
    "stack": "compose-dev",
    "profile": "core",
    "video": False,
    "written": TODAY,
    "steps": [
        {
            "id": "open",
            "doc": "sign-in",
            "do": {"goto": "/login"},
            "expect": {
                "status": 200,
                "visible": [{"role": "button", "name": "Sign in"}],
                "aria": '- button "Sign in"',
            },
            "fail": "the sign-in page does not answer",
        },
        {
            "id": "email",
            "doc": "sign-in",
            "do": {"fill": {"label": "Email", "value": "admin@demo.com"}},
            "expect": {"aria": '- textbox "Email"'},
            "fail": "there is no Email field",
        },
        {
            "id": "environment",
            "doc": "sign-in",
            "do": {"select": {"label": "Environment", "option": "Production"}},
            "expect": {"text": "Production"},
            "snapshot": False,
            "snapshot_reason": "the select is checked by its chosen text",
            "fail": "the environment cannot be chosen",
        },
        {
            "id": "sign-in",
            "doc": "sign-in",
            "do": {"click": {"role": "button", "name": "Sign in"}},
            "expect": {
                "url": "/experiments",
                "aria": '- heading "Experiments" [level=1]',
            },
            "fail": "the sign-in form is shown again",
            "snapshot": True,
        },
        {
            "id": "health",
            "doc": "create",
            "do": {"ref": "doc-examples"},
            "fail": "Doc Examples reports the block red",
        },
        {
            "id": "list",
            "doc": "create",
            "do": {
                "api": {"method": "GET", "path": "/api/v1/experiments/", "as": "admin"}
            },
            "expect": {"status": 200, "json": {"total": 3}},
            "fail": "the list does not answer 200",
        },
        {
            "id": "size",
            "doc": "create-an-experiment",
            "do": {"click": {"role": "button", "name": "New experiment"}},
            "expect": {
                "number": {
                    "locator": {
                        "role": "status",
                        "name": "Total",
                        "quote": False,
                        "reason": "the guide calls it the total sample size",
                    },
                    "oracle": {"name": "double", "args": {"x": 2}},
                },
                "aria": '- status "Total"',
            },
            "fail": "the number differs from the oracle",
            "not_run": "waived #123",
        },
    ],
}


def _context(tmp_path: Path, **overrides: Any) -> loader.Context:
    docs = tmp_path / "docs"
    (docs / "guides").mkdir(parents=True, exist_ok=True)
    (docs / "guides" / "sample.md").write_text(GUIDE, encoding="utf-8")
    values: Dict[str, Any] = {
        "docs_root": docs,
        "inventory": INVENTORY,
        "doc_examples": {"guides/sample.md": 1},
        "oracles": {"double": lambda x: 2 * x},
        "today": TODAY,
    }
    values.update(overrides)
    return loader.Context(**values)


def _load(tmp_path: Path, data: Any, name: str = "sample", **overrides: Any) -> Journey:
    path = tmp_path / "journeys" / f"{name}.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    return loader.load(path, _context(tmp_path, **overrides))


def _refusals(
    tmp_path: Path, data: Any, name: str = "sample", **overrides: Any
) -> List[str]:
    with pytest.raises(loader.Refused) as refused:
        _load(tmp_path, data, name, **overrides)
    return refused.value.problems


def _step(data: Dict[str, Any], step_id: str) -> Dict[str, Any]:
    return next(step for step in data["steps"] if step["id"] == step_id)


def _with(change: Callable[[Dict[str, Any]], None]) -> Dict[str, Any]:
    data = copy.deepcopy(GOOD)
    change(data)
    return data


# ---------------------------------------------------------------------------
# The good journey loads
# ---------------------------------------------------------------------------
def test_the_good_journey_loads(tmp_path):
    journey = _load(tmp_path, GOOD)
    assert [step.do.kind for step in journey.steps] == [
        "goto",
        "fill",
        "select",
        "click",
        "ref",
        "api",
        "click",
    ]
    assert journey.steps[5].do.api.as_ == "admin"
    assert journey.steps[5].expect.json_ == {"total": 3}


# ---------------------------------------------------------------------------
# Each refusal, on one planted defect
# ---------------------------------------------------------------------------
def _on_step(step_id: str, change: Callable[[Dict[str, Any]], None]):
    return lambda data: change(_step(data, step_id))


def _drop(step_id: str, key: str):
    return _on_step(step_id, lambda step: step.pop(key))


PLANTS = [
    pytest.param(
        _on_step("sign-in", lambda s: s["do"]["click"].update(name="css=#submit")),
        {},
        "'css=#submit' is a CSS or XPath selector",
        id="css-selector-as-a-name",
    ),
    pytest.param(
        _on_step(
            "open",
            lambda s: s["expect"].update(
                visible=[{"role": "button", "name": "//button[1]"}]
            ),
        ),
        {},
        "'//button[1]' is a CSS or XPath selector",
        id="xpath-as-a-name",
    ),
    pytest.param(
        _on_step(
            "sign-in", lambda s: s["do"].update(click={"selector": "button.primary"})
        ),
        {},
        "selector is a CSS or XPath selector",
        id="selector-key",
    ),
    pytest.param(
        _on_step(
            "size",
            lambda s: s["expect"]["number"].update(locator="css=.total"),
        ),
        {},
        "locator is a CSS or XPath selector",
        id="string-locator",
    ),
    pytest.param(
        _on_step("email", lambda s: s["do"]["fill"].update(label="form >> Email")),
        {},
        "'form >> Email' is a CSS or XPath selector",
        id="chained-selector-as-a-label",
    ),
    pytest.param(
        _on_step("sign-in", lambda s: s.update(do={"sleep": 2})),
        {},
        "sleep is a sleep",
        id="sleep-action",
    ),
    pytest.param(
        _on_step("sign-in", lambda s: s["expect"].update(wait_for_timeout=500)),
        {},
        "wait_for_timeout is a sleep",
        id="wait-in-expect",
    ),
    pytest.param(
        _drop("sign-in", "fail"),
        {},
        "step 4 (sign-in): no fail; write what failure looks like before the run",
        id="step-without-fail",
    ),
    pytest.param(
        _on_step("sign-in", lambda s: s.update(fail="  ")),
        {},
        "fail must be one non-empty line",
        id="empty-fail",
    ),
    pytest.param(
        lambda data: data.update(stack="staging"),
        {},
        "unknown stack 'staging'",
        id="unknown-stack",
    ),
    pytest.param(
        _on_step("sign-in", lambda s: s.update(doc="step-9")),
        {},
        "the guide has no anchor #step-9",
        id="anchor-the-page-lacks",
    ),
    pytest.param(
        _on_step("sign-in", lambda s: s["do"]["click"].update(name="Log in")),
        {},
        "click.name 'Log in' is not in the guide's text",
        id="name-the-guide-does-not-say",
    ),
    pytest.param(
        _on_step("email", lambda s: s["do"]["fill"].update(label="E-mail")),
        {},
        "fill.label 'E-mail' is not in the guide's text",
        id="label-the-guide-does-not-say",
    ),
    pytest.param(
        _on_step(
            "size",
            lambda s: s["expect"]["number"]["locator"].pop("reason"),
        ),
        {},
        "quote: false needs a one-line reason",
        id="unquoted-without-reason",
    ),
    pytest.param(
        lambda data: data.update(guide="guides/later.md"),
        {},
        "names 'later' as the journey of 'guides/later.md', not 'sample'",
        id="another-journeys-guide",
    ),
    pytest.param(
        lambda data: None,
        {
            "inventory": {
                "pages": {
                    "guides/sample.md": {
                        "class": "journey",
                        "journey": "sample",
                        "pending": True,
                        "reason": "x",
                    }
                }
            }
        },
        "journey 'sample' is pending = true in inventory.toml",
        id="pending-journey",
    ),
    pytest.param(
        lambda data: data.update(guide="guides/other.md"),
        {},
        "is classified 'reference' in inventory.toml, not journey",
        id="inventory-class-not-journey",
    ),
    pytest.param(
        lambda data: data.update(guide="guides/missing.md"),
        {},
        "is not a page in inventory.toml",
        id="guide-not-in-inventory",
    ),
    pytest.param(
        _on_step("sign-in", lambda s: s.update(expect={"json": {"a": 1}})),
        {},
        "step 4 (sign-in): a step on one screen needs expect.aria",
        id="screen-step-with-only-json",
    ),
    pytest.param(
        _drop("environment", "expect"),
        {},
        "step 3 (environment): a step with snapshot: false needs a structural expect",
        id="screen-step-with-no-expect",
    ),
    pytest.param(
        _on_step("open", lambda s: s["expect"].pop("aria")),
        {},
        "step 1 (open): a step on one screen needs expect.aria, its ARIA snapshot"
        " written before the run, beside any structural expect",
        id="screen-step-without-aria",
    ),
    pytest.param(
        _drop("environment", "snapshot_reason"),
        {},
        "step 3 (environment): snapshot: false needs a one-line snapshot_reason",
        id="no-snapshot-without-a-reason",
    ),
    pytest.param(
        _on_step("sign-in", lambda s: s.update(snapshot_reason="because")),
        {},
        "snapshot_reason is only for snapshot: false",
        id="snapshot-reason-with-a-snapshot",
    ),
    pytest.param(
        _on_step("sign-in", lambda s: s["expect"].update(aria="heading: [unclosed")),
        {},
        "step 4 (sign-in).expect.aria: aria must be an ARIA snapshot template",
        id="aria-that-is-not-yaml",
    ),
    pytest.param(
        _on_step("sign-in", lambda s: s["expect"].update(aria="Experiments")),
        {},
        "step 4 (sign-in).expect.aria: aria must be an ARIA snapshot template",
        id="aria-that-is-not-a-list",
    ),
    pytest.param(
        _on_step("sign-in", lambda s: s["do"]["click"].update(name="Sign\nin")),
        {},
        "step 4 (sign-in).do.click.name: must be one line",
        id="multi-line-name",
    ),
    pytest.param(
        _on_step("email", lambda s: s["do"]["fill"].update(label="Email\naddress")),
        {},
        "step 2 (email).do.fill.label: must be one line",
        id="multi-line-label",
    ),
    pytest.param(
        _on_step("environment", lambda s: s["expect"].update(text="Pro\nduction")),
        {},
        "step 3 (environment).expect.text: must be one line",
        id="multi-line-text",
    ),
    pytest.param(
        _on_step("open", lambda s: s["do"].update(goto="/login\n/other")),
        {},
        "step 1 (open).do.goto: must be one line",
        id="multi-line-goto",
    ),
    pytest.param(
        lambda data: data.update(
            steps=[
                _step(data, "health"),
                {**_step(data, "list"), "not_run": "needs-aws"},
            ]
        ),
        {},
        "no step is run by this runner: every step is ref: doc-examples or"
        " declares not_run",
        id="no-step-run-here",
    ),
    pytest.param(
        _on_step("sign-in", lambda s: s["expect"].update(status=200)),
        {},
        "status is the answer to a goto or an open; click has none",
        id="status-on-a-click",
    ),
    pytest.param(
        _on_step("list", lambda s: s.update(expect={"text": "Experiments"})),
        {},
        "an api step needs status or json in expect",
        id="api-step-without-status-or-json",
    ),
    pytest.param(
        lambda data: data.update(stack="docs-local"),
        {},
        "an api step needs a stack with an API; docs-local has none",
        id="api-step-on-a-docs-stack",
    ),
    pytest.param(
        _on_step("health", lambda s: s.update(doc="sign-in")),
        {},
        "the section #sign-in has no {.bash exec} block",
        id="ref-without-exec-block",
    ),
    pytest.param(
        lambda data: None,
        {"doc_examples": {}},
        "enrols no exec block of guides/sample.md",
        id="ref-on-a-guide-not-enrolled",
    ),
    pytest.param(
        _on_step(
            "size", lambda s: s["expect"]["number"]["oracle"].update(name="triple")
        ),
        {},
        "no oracle named 'triple'",
        id="unknown-oracle",
    ),
    pytest.param(
        _on_step("size", lambda s: s.update(not_run="earlier-step-failed")),
        {},
        "not_run 'earlier-step-failed' is not a reason a journey may give",
        id="runtime-reason-declared",
    ),
    pytest.param(
        _on_step("size", lambda s: s.update(not_run="flaky")),
        {},
        "not_run 'flaky' is not a reason a journey may give",
        id="unregistered-reason",
    ),
    pytest.param(
        lambda data: data.update(written=TODAY + datetime.timedelta(days=1)),
        {},
        "is after today",
        id="written-in-the-future",
    ),
    pytest.param(
        _on_step("sign-in", lambda s: s.update(retries=3)),
        {},
        "step 4 (sign-in).retries: Extra inputs are not permitted",
        id="unknown-field",
    ),
    pytest.param(
        _on_step("sign-in", lambda s: s["do"].update(goto="/login")),
        {},
        "do must be exactly one of",
        id="two-actions",
    ),
    pytest.param(
        _on_step("open", lambda s: s["do"].update(goto="https://example.com/login")),
        {},
        "goto is a path on the stack",
        id="goto-a-url",
    ),
    pytest.param(
        _on_step("sign-in", lambda s: s["do"]["click"].update(role="btn")),
        {},
        "step 4 (sign-in).do.click.role: 'btn' is not one of the values allowed here",
        id="unknown-role",
    ),
    pytest.param(
        _on_step("health", lambda s: s.update(expect={"status": 200})),
        {},
        "ref: doc-examples is checked by Doc Examples; it has no expect here",
        id="ref-with-expect",
    ),
    pytest.param(
        _on_step("list", lambda s: s.update(snapshot=True)),
        {},
        "an api step has no screen to snapshot",
        id="api-step-snapshot",
    ),
    pytest.param(
        lambda data: data["steps"].append(copy.deepcopy(data["steps"][0])),
        {},
        "step id 'open' is used twice",
        id="duplicate-step-id",
    ),
    pytest.param(
        lambda data: data.update(video="yes"),
        {},
        "video: Input should be a valid boolean",
        id="quoted-bool",
    ),
    pytest.param(
        _on_step(
            "environment",
            lambda s: s["expect"].update(aria='- combobox "Environment"'),
        ),
        {},
        "step 3 (environment): snapshot: false drops the ARIA snapshot, but"
        " expect.aria gives one",
        id="snapshot-false-with-aria",
    ),
    pytest.param(
        _on_step(
            "environment",
            lambda s: s.update(expect={"aria": '- combobox "Environment"'}),
        ),
        {},
        "step 3 (environment): a step with snapshot: false needs a structural"
        " expect (url, status, visible, text, number, cells)",
        id="snapshot-false-with-only-aria",
    ),
    pytest.param(
        _on_step("open", lambda s: s["expect"].update(found="/guides/sample/")),
        {},
        "step 1 (open): found is the page a search finds; goto has none",
        id="found-on-a-goto",
    ),
    pytest.param(
        _on_step("open", lambda s: (s.pop("expect"), s.update(do={"crawl": "nav"}))),
        {},
        "step 1 (open): crawl is for the documentation site's stacks"
        " (docs-local, docs-published); compose-dev serves no site",
        id="crawl-on-compose-dev",
    ),
    pytest.param(
        _on_step(
            "open",
            lambda s: s.update(
                do={"search": "sign in"}, expect={"found": "/guides/sample/"}
            ),
        ),
        {},
        "step 1 (open): search is for the documentation site's stacks",
        id="search-on-compose-dev",
    ),
]


@pytest.mark.parametrize("change, overrides, expected", PLANTS)
def test_planted_defect_is_refused(tmp_path, change, overrides, expected):
    problems = _refusals(tmp_path, _with(change), **overrides)
    assert any(expected in problem for problem in problems), problems


def test_a_file_name_that_is_not_a_slug_is_refused(tmp_path):
    problems = _refusals(tmp_path, GOOD, name="Sample_Journey")
    assert any("is not a lower-case slug" in problem for problem in problems), problems


def test_the_inventory_names_the_journey_by_file(tmp_path):
    inventory = copy.deepcopy(INVENTORY)
    inventory["pages"]["guides/sample.md"]["journey"] = "another"
    problems = _refusals(tmp_path, GOOD, inventory=inventory)
    assert any(
        "names 'another' as the journey of 'guides/sample.md', not 'sample'" in problem
        for problem in problems
    ), problems


def test_an_unquoted_name_with_a_reason_is_accepted(tmp_path):
    data = _with(
        _on_step(
            "sign-in",
            lambda s: s["do"]["click"].update(
                name="Log in", quote=False, reason="the button says Log in"
            ),
        )
    )
    assert _load(tmp_path, data).steps[3].do.click.name == "Log in"


def test_a_waiver_names_its_issue(tmp_path):
    assert _load(tmp_path, GOOD).steps[6].not_run == "waived #123"
    for reason in ("waived", "waived #", "waived #0", "waived 123"):
        data = _with(_on_step("size", lambda s, r=reason: s.update(not_run=r)))
        problems = _refusals(tmp_path, data)
        assert any("is not a reason a journey may give" in p for p in problems), reason


def test_a_raw_problem_is_not_repeated_by_the_model(tmp_path):
    """A missing fail, a sleep and an unknown stack, each said once."""
    data = _with(_drop("sign-in", "fail"))
    _step(data, "open")["do"] = {"wait": 1}
    data["stack"] = "nope"
    problems = _refusals(tmp_path, data)
    assert len(problems) == 3, problems


def test_every_stage_reports_in_one_file(tmp_path):
    """A raw problem, a model problem and a semantic one, all three at once."""
    data = _with(_on_step("open", lambda s: s["expect"].update(pause=1)))
    _step(data, "email")["retries"] = 2
    _step(data, "sign-in")["doc"] = "step-9"
    problems = _refusals(tmp_path, data)
    assert problems == [
        "step 1 (open).expect: pause is a sleep; a step waits for what it expects,"
        " never for a time",
        "step 2 (email).retries: Extra inputs are not permitted",
        "step 4 (sign-in): the guide has no anchor #step-9",
    ], problems


def test_names_that_look_like_files_are_names(tmp_path):
    """settings.json, .env and .NET are what a reader sees, not selectors."""
    visible = [
        {"role": "link", "name": "settings.json"},
        {"role": "link", "name": ".env"},
        {"role": "tab", "name": ".NET"},
    ]
    data = _with(_on_step("open", lambda s: s["expect"].update(visible=visible)))
    names = [named.name for named in _load(tmp_path, data).steps[0].expect.visible]
    assert names == ["settings.json", ".env", ".NET"]


def test_the_model_docstring_example_loads():
    """The example in model.py's docstring is a journey the loader accepts."""
    from docs_runner import model

    doc = model.__doc__
    block = doc[doc.index("without ``pending``)::") :].split("\n\n", 2)[1]
    data = yaml.safe_load(textwrap.dedent(block))
    real = loader.context_for(REPO_ROOT)
    inventory = copy.deepcopy(real.inventory)
    inventory["pages"]["README.md"].pop("pending", None)
    context = loader.Context(
        docs_root=real.docs_root,
        inventory=inventory,
        doc_examples=real.doc_examples,
        oracles=real.oracles,
        today=real.today,
    )
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "docs-site.yaml"
        path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
        journey = loader.load(path, context)
    assert [step.id for step in journey.steps] == [
        "home",
        "open-quick-start",
        "deploy-guide",
    ]


# ---------------------------------------------------------------------------
# What a step's log line says, and how it compares
# ---------------------------------------------------------------------------
def test_actions_and_expectations_are_described_for_the_log(tmp_path):
    steps = _load(tmp_path, GOOD).steps
    assert [checks.describe_action(step) for step in steps] == [
        "open /login",
        'fill "Email"',
        'choose "Production" in "Environment"',
        'click the button "Sign in"',
        "its shell blocks, run by Doc Examples",
        "GET /api/v1/experiments/ as admin",
        'click the button "New experiment"',
    ]
    assert checks.describe_expect(steps[0]) == (
        'status 200; button "Sign in" visible; the screen matches its ARIA snapshot'
        " (1 lines)"
    )
    assert checks.describe_expect(steps[3]) == (
        "at /experiments; the screen matches its ARIA snapshot (1 lines)"
    )
    assert checks.describe_expect(steps[4]) == "Doc Examples runs this section's blocks"
    assert checks.describe_expect(steps[5]) == "status 200; json total = 3"
    assert checks.describe_expect(steps[6]) == (
        'the number in status "Total" equals double(x=2); the screen matches its'
        " ARIA snapshot (1 lines)"
    )


def test_a_fill_value_is_never_described():
    """A password typed into a form must not reach the public log."""
    data = _with(_on_step("email", lambda s: s["do"]["fill"].update(value="Demo1234!")))
    step = Journey.model_validate(data).steps[1]
    assert "Demo1234!" not in checks.describe_action(step)
    assert "Demo1234!" not in checks.describe_expect(step)


def test_json_paths_and_values():
    document = {"items": [{"key": "a", "on": True}], "total": 1}
    assert checks.json_at(document, "items.0.key") == "a"
    assert checks.json_at(document, "total") == 1
    for missing in ("items.1.key", "items.x", "nope", "total.x"):
        with pytest.raises(KeyError):
            checks.json_at(document, missing)
    assert checks.same_json(1, 1.0)
    assert checks.same_json(True, True)
    assert not checks.same_json(True, 1)
    assert not checks.same_json(0, False)


def test_numbers_shown_on_a_screen():
    assert checks.parse_number("Sample size: 31,234 per variant") == 31234
    assert checks.parse_number("-0.5 pp") == -0.5
    with pytest.raises(checks.StepFailed):
        checks.parse_number("no number here")


def test_an_error_is_cut_to_one_line_before_the_call_log():
    error = "Timeout 10000ms exceeded.\n  waiting for x\nCall log:\n  - secret detail"
    assert checks.one_line(error) == "Timeout 10000ms exceeded.; waiting for x"
    assert len(checks.one_line("x" * 1000)) == checks.OBSERVED_CHARS


# ---------------------------------------------------------------------------
# The guide's anchors
# ---------------------------------------------------------------------------
def test_anchors_follow_toc():
    guide = guides.parse("guides/sample.md", GUIDE)
    assert [(h.level, h.text, h.anchor) for h in guide.headings] == [
        (1, "Sample guide", "sample-guide"),
        (2, "Sign in", "sign-in"),
        (2, "Create an experiment", "create"),
        (2, "Create an experiment", "create-an-experiment"),
    ]
    assert guide.title == "Sample guide"
    assert guide.has_exec_block("create")
    assert not guide.has_exec_block("sign-in")


@pytest.mark.parametrize(
    "heading, anchor",
    [
        ("Step 1: Start Services", "step-1-start-services"),
        ("`docker compose up` -- the stack", "docker-compose-up-the-stack"),
        ("**Bold** and [a link](x.md)", "bold-and-a-link"),
        ("Café — résumé", "cafe-resume"),
        ("What's new? (v2)", "whats-new-v2"),
        ("Explicit {: #chosen }", "chosen"),
        ("GET /api/v1/drafts/{draft_id}", "get-apiv1draftsdraft_id"),
        (
            "Every EXPERIMENT_SCHEDULER_INTERVAL_MINUTES",
            "every-experiment_scheduler_interval_minutes",
        ),
        ("`trackBatch(events: List<TrackEvent>)`", "trackbatchevents-listtrackevent"),
        ("`platform-<env>` (metrics)", "platform-env-metrics"),
        ("Developers &amp; *Integration* _Guides_", "developers-integration-guides"),
    ],
)
def test_slugs(heading, anchor):
    assert guides.parse("x.md", f"## {heading}\n").headings[0].anchor == anchor


def test_repeated_and_taken_slugs_are_numbered_as_toc_numbers_them():
    text = "## A\n\n## A\n\n## A_1 {#a_1}\n\n## A\n"
    anchors = [h.anchor for h in guides.parse("x.md", text).headings]
    assert anchors == ["a", "a_2", "a_1", "a_3"]


def test_html_ids_are_anchors_and_fenced_hashes_are_not():
    text = '<a id="top"></a>\n\n~~~\n# not a heading\n~~~\n\n## Real\n'
    guide = guides.parse("x.md", text)
    assert set(guide.anchors) == {"top", "real"}


# ---------------------------------------------------------------------------
# The results log
# ---------------------------------------------------------------------------
def _record(**changes: Any) -> log.Record:
    values: Dict[str, Any] = {
        "run": "1",
        "sha": "abc",
        "stack": "compose-dev",
        "guide": "guides/sample.md",
        "step": 1,
        "step_id": "open",
        "heading": "Sign in",
        "action": "open /login",
        "expected": "status 200",
        "failure_signature": "the sign-in page does not answer",
        "result": "PASS",
        "reason": "",
        "observed": "at /login (status 200)",
        "snapshot": "",
        "expected_snapshot": "structural only",
        "ms": 12,
    }
    values.update(changes)
    return log.Record(**values)


def test_the_log_keys_are_fixed():
    """The keys every line carries, in order; a change here is a change of contract."""
    assert log.KEYS == (
        "run",
        "sha",
        "stack",
        "guide",
        "step",
        "step_id",
        "heading",
        "action",
        "expected",
        "failure_signature",
        "result",
        "reason",
        "observed",
        "snapshot",
        "expected_snapshot",
        "ms",
    )
    assert tuple(json.loads(_record().line())) == log.KEYS


def test_the_log_round_trips_and_refuses_other_shapes(tmp_path):
    path = tmp_path / "results.jsonl"
    writer = log.Log(path)
    writer.write(_record())
    writer.write(_record(step=2, result="NOT RUN", reason="earlier-step-failed"))
    assert [r.step for r in log.read(path)] == [1, 2]

    line = json.loads(_record().line())
    del line["reason"]
    with pytest.raises(log.BadRecord, match="are not the log's keys"):
        log.parse_line(json.dumps(line))
    line = json.loads(_record().line())
    line["extra"] = 1
    with pytest.raises(log.BadRecord):
        log.parse_line(json.dumps(line))


@pytest.mark.parametrize(
    "changes, message",
    [
        ({"result": "SKIPPED"}, "is not one of"),
        ({"result": "NOT RUN", "reason": ""}, "is not in the registry"),
        ({"result": "NOT RUN", "reason": "flaky"}, "is not in the registry"),
        ({"result": "PASS", "reason": "needs-aws"}, "has no reason"),
        ({"observed": "two\nlines"}, "more than one line"),
        ({"step": 0}, "is not a step number"),
    ],
)
def test_a_bad_record_is_refused(changes, message):
    with pytest.raises(log.BadRecord, match=message):
        _record(**changes)


def test_every_registry_reason_is_accepted():
    for reason in (*registry.DECLARED, *registry.RUNTIME, "waived #939"):
        assert _record(result="NOT RUN", reason=reason).reason == reason
    assert not registry.is_declarable(registry.EARLIER_STEP_FAILED)
    assert not registry.is_declarable(registry.DOC_EXAMPLES)


def test_the_run_directory_is_never_in_the_tree(tmp_path):
    with pytest.raises(ValueError, match="inside the repository"):
        log.run_directory({"DOCS_JOURNEY_RUN_DIR": str(REPO_ROOT / "out")}, REPO_ROOT)
    with pytest.raises(ValueError, match="inside the repository"):
        log.run_directory({"DOCS_JOURNEY_RUN_DIR": str(REPO_ROOT)}, REPO_ROOT)
    chosen = tmp_path / "run"
    assert log.run_directory({"DOCS_JOURNEY_RUN_DIR": str(chosen)}, REPO_ROOT) == chosen
    assert chosen.is_dir()


# ---------------------------------------------------------------------------
# The reports
# ---------------------------------------------------------------------------
EXPECTED_ARIA = '- heading "Experiments" [level=1]'
OBSERVED_ARIA = '- heading "Sign in" [level=1]\n- textbox "Email"'


def _failed_run(run_dir: Path) -> report.GuideRun:
    """A run whose second step failed, with both its snapshots on disk."""
    (run_dir / "sample").mkdir(parents=True)
    (run_dir / "sample" / "02-sign-in.expected.aria.yml").write_text(
        EXPECTED_ARIA + "\n"
    )
    (run_dir / "sample" / "02-sign-in.png").write_bytes(b"\x89PNG\r\n")
    (run_dir / "sample" / "02-sign-in.aria.yml").write_text(OBSERVED_ARIA + "\n")
    return report.GuideRun(
        journey="sample",
        guide="guides/sample.md",
        title="Sample guide",
        stack="compose-dev",
        records=(
            _record(),
            _record(
                step=2,
                step_id="sign-in",
                action='click the button "Sign in"',
                expected="at /experiments; the screen matches its ARIA snapshot (1 lines)",
                failure_signature="the sign-in form is shown again",
                result="FAIL",
                observed="Locator expected to match the ARIA snapshot",
                snapshot="sample/02-sign-in.png",
                expected_snapshot="sample/02-sign-in.expected.aria.yml",
            ),
            _record(
                step=3, step_id="size", result="NOT RUN", reason="earlier-step-failed"
            ),
        ),
    )


def test_a_fail_shows_both_snapshots_side_by_side(tmp_path):
    """For each FAIL, the expected and the observed snapshot, side by side."""
    run = _failed_run(tmp_path)
    for text in (
        report.guide_markdown(run, tmp_path, date="2026-10-06", sha="abc"),
        report.guide_html(run, tmp_path, date="2026-10-06", sha="abc"),
    ):
        assert "Expected (written before the run)" in text
        assert "heading &quot;Experiments&quot; [level=1]" in text
        assert 'src="sample/02-sign-in.png"' in text
        assert "heading &quot;Sign in&quot; [level=1]" in text
        assert "the sign-in form is shown again" in text
        expected_at = text.index("heading &quot;Experiments&quot;")
        observed_at = text.index("heading &quot;Sign in&quot;")
        assert expected_at < observed_at


def test_the_guide_report_opens_with_its_verdict(tmp_path):
    text = report.guide_markdown(
        _failed_run(tmp_path), tmp_path, date="2026-10-06", sha="abc"
    )
    lines = text.splitlines()
    assert lines[0] == (
        "# Sample guide (guides/sample.md), 2026-10-06, commit abc: FAIL at step 2"
    )
    assert lines[2] == (
        'A reader following this guide cannot finish it. First failing step: 2, "Sign in".'
    )
    assert "| 3 | Sign in: open /login |" in text
    assert "NOT RUN (earlier-step-failed)" in text


@pytest.mark.parametrize(
    "plant, message",
    [
        (
            lambda d: (d / "sample" / "02-sign-in.expected.aria.yml").unlink(),
            "expected snapshot",
        ),
        (lambda d: (d / "sample" / "02-sign-in.png").unlink(), "observed snapshot"),
    ],
    ids=["expected-snapshot-file-dropped", "observed-snapshot-file-dropped"],
)
def test_a_fail_without_both_snapshots_is_refused(tmp_path, plant, message):
    run = _failed_run(tmp_path)
    plant(tmp_path)
    with pytest.raises(report.ReportError, match=message):
        report.guide_markdown(run, tmp_path, date="2026-10-06", sha="abc")
    with pytest.raises(report.ReportError, match=message):
        report.guide_html(run, tmp_path, date="2026-10-06", sha="abc")


@pytest.mark.parametrize("field", ["expected_snapshot", "snapshot"])
def test_a_fail_record_naming_no_snapshot_is_refused(tmp_path, field):
    run = _failed_run(tmp_path)
    records = list(run.records)
    records[1] = log.Record(**{**json.loads(records[1].line()), field: ""})
    broken = report.GuideRun(**{**run.__dict__, "records": tuple(records)})
    with pytest.raises(report.ReportError, match="FAIL with no"):
        report.guide_markdown(broken, tmp_path, date="2026-10-06", sha="abc")


def test_a_structural_fail_shows_its_expectation_and_what_was_seen(tmp_path):
    (tmp_path / "s").mkdir()
    (tmp_path / "s" / "01-list.api.json").write_text('{"status": 500}\n')
    run = report.GuideRun(
        journey="s",
        guide="guides/sample.md",
        title="Sample guide",
        stack="compose-dev",
        records=(
            _record(
                result="FAIL",
                observed="status 500, expected 200",
                snapshot="s/01-list.api.json",
                expected_snapshot="structural only",
            ),
        ),
    )
    text = report.guide_markdown(run, tmp_path, date="2026-10-06", sha="abc")
    assert "Expected (structural only): status 200" in text
    assert "Seen: status 500, expected 200" in text
    assert "&quot;status&quot;: 500" in text


def test_verdicts():
    def run(*records):
        return report.GuideRun("s", "g.md", "G", "docs-local", records=records)

    assert report.verdict(run(_record())).line == "PASS"
    assert (
        report.verdict(
            run(_record(), _record(step=2, result="NOT RUN", reason="doc-examples"))
        ).line
        == "PASS"
    )
    partial = report.verdict(
        run(_record(), _record(step=2, result="NOT RUN", reason="needs-aws"))
    )
    assert partial.line == "PARTIAL: 1 not run"
    assert "needs-aws" in partial.sentence
    for reasons in (["doc-examples"], ["doc-examples", "needs-aws"], ["waived #9"]):
        nothing_ran = report.verdict(
            run(
                *(
                    _record(step=n, result="NOT RUN", reason=reason)
                    for n, reason in enumerate(reasons, 1)
                )
            )
        )
        assert nothing_ran.word == "PARTIAL", reasons
        assert nothing_ran.line == f"PARTIAL: {len(reasons)} not run"
        assert nothing_ran.sentence.startswith("No step of this guide ran here")
    assert report.verdict(run()).word == "FAIL"
    refused = report.GuideRun("s", "g.md", "G", "docs-local", refused=("x",))
    assert report.verdict(refused).line == "FAIL: the journey file was refused"
    down = report.GuideRun(
        "s", "g.md", "G", "docs-local", error="mkdocs build exited 1"
    )
    assert report.verdict(down).word == "FAIL"


def test_the_summary_lists_every_page_not_covered_and_every_flow(tmp_path):
    passed = report.GuideRun(
        "sample",
        "guides/sample.md",
        "Sample guide",
        "compose-dev",
        records=(_record(),),
    )
    text = report.summary_markdown([passed], INVENTORY, date="2026-10-06", sha="abc")
    lines = text.splitlines()
    assert lines[0] == (
        "Docs journeys, 2026-10-06, commit abc: GREEN (a run outside this"
        " repository's Actions, posted nowhere)"
    )
    assert lines[2] == (
        "Guides: 1 pass, 0 fail, 0 partial, of 3 pages in the nav. Not covered: 2"
        " pages, each listed below with its reason."
    )
    assert "| guides/other.md | reference | nothing to walk |" in text
    assert (
        "| guides/later.md | journey | journey later: pending, expectations not"
        " written yet; to be walked |"
    ) in text
    assert "guides/sample.md | journey |" not in text
    assert (
        "| Quick Start, end to end | sample: PASS; later: planned in D3a |  |"
        " partly: sample ran; not verified yet: waits on later (planned in D3a) |"
    ) in text
    assert "0 of 1 flows are verified by journeys that ran in this run." in text
    assert "- planned in D3a: later" in text


def test_the_summary_of_an_empty_run_is_red():
    text = report.summary_markdown([], INVENTORY, date="2026-10-06", sha="abc")
    assert text.splitlines()[0].startswith("Docs journeys, 2026-10-06, commit abc: RED")
    assert "No journey ran, so nothing was verified." in text


# ---------------------------------------------------------------------------
# The compose stack, against a fake docker
# ---------------------------------------------------------------------------
FAKE_DOCKER = """#!/bin/sh
here="$(dirname "$0")"
printf '%s\\n' "$*" >> "$here/calls.log"
case " $* " in
  *" up "*) exit "$(cat "$here/up_exit")" ;;
esac
exit 0
"""
DOWN = "compose -p docs-journeys down -v --remove-orphans"
UP = "compose -p docs-journeys up -d --wait --build"


def _fake_docker(tmp_path: Path, up_exit: int) -> Path:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake = bin_dir / "docker"
    fake.write_text(FAKE_DOCKER)
    fake.chmod(fake.stat().st_mode | stat.S_IXUSR)
    (bin_dir / "up_exit").write_text(str(up_exit))
    return bin_dir


COMPOSE_FILE = """services:
  api:
    ports:
      - "${API_HOST_PORT:-8000}:8000"
  frontend:
    ports:
      - "${FRONTEND_HOST_PORT:-3000}:8080"
  postgres:
    ports:
      - "${POSTGRES_HOST_PORT:-5432}:5432"
"""


def _compose(tmp_path: Path, bin_dir: Path, compose_file: str = COMPOSE_FILE):
    from docs_runner import stacks

    (tmp_path / "docker-compose.yml").write_text(compose_file)
    environ = {"PATH": f"{bin_dir}{os.pathsep}/usr/bin{os.pathsep}/bin"}
    return stacks, stacks.ComposeDev(tmp_path, tmp_path / "logs", environ)


def _calls(bin_dir: Path) -> List[str]:
    return (bin_dir / "calls.log").read_text().splitlines()


def test_compose_dev_clears_its_project_then_builds(tmp_path, monkeypatch):
    bin_dir = _fake_docker(tmp_path, 0)
    stacks, stack = _compose(tmp_path, bin_dir)
    monkeypatch.setattr(stack, "_served_profile", lambda api_url: "core")
    running = stack.up("core")
    assert running.base_url == "http://127.0.0.1:23000/"
    assert running.published == {
        8000: "http://127.0.0.1:28000",
        3000: "http://127.0.0.1:23000",
    }
    assert _calls(bin_dir) == [DOWN, UP]
    stack.down()
    assert _calls(bin_dir) == [DOWN, UP, DOWN]


def test_a_failed_compose_up_clears_its_project(tmp_path):
    bin_dir = _fake_docker(tmp_path, 3)
    stacks, stack = _compose(tmp_path, bin_dir)
    with pytest.raises(stacks.StackError, match=r"docker compose up \(core\) exited 3"):
        stack.up("core")
    assert _calls(bin_dir) == [DOWN, UP, DOWN]


def test_the_wrong_profile_clears_the_project(tmp_path, monkeypatch):
    bin_dir = _fake_docker(tmp_path, 0)
    stacks, stack = _compose(tmp_path, bin_dir)
    monkeypatch.setattr(stack, "_served_profile", lambda api_url: "core")
    with pytest.raises(stacks.StackError, match="serves the core profile, not full"):
        stack.up("full")
    assert _calls(bin_dir) == [DOWN, UP, DOWN]
    assert stack.running is None


def test_a_program_that_cannot_start_is_a_stack_error(tmp_path):
    """No docker, no npm: the journey gets a FAIL before step 1, not a crash."""
    from docs_runner import stacks

    empty = tmp_path / "empty"
    empty.mkdir()
    (tmp_path / "frontend").mkdir()
    (tmp_path / "docker-compose.yml").write_text(COMPOSE_FILE)
    environ = {"PATH": str(empty)}
    compose = stacks.ComposeDev(tmp_path, tmp_path / "logs", environ)
    with pytest.raises(stacks.StackError, match="docker could not be started"):
        compose.up("core")
    marketing = stacks.MarketingLocal(tmp_path, tmp_path / "logs", environ)
    with pytest.raises(stacks.StackError, match="npm could not be started"):
        marketing.up()


# ---------------------------------------------------------------------------
# The runner's own session: no skip passes, no retry plugin loads
# ---------------------------------------------------------------------------
def _runner_copy(tmp_path: Path, test_body: str) -> Path:
    """A tree holding the runner's conftest, ini and package, with one test of ours."""
    root = tmp_path / "repo"
    here = root / "tests" / "acceptance" / "docs"
    here.mkdir(parents=True)
    for name in ("conftest.py", "pytest.ini", "inventory.toml", "recordings.toml"):
        shutil.copy(RUNNER_ROOT / name, here / name)
    shutil.copytree(
        RUNNER_ROOT / "docs_runner",
        here / "docs_runner",
        ignore=shutil.ignore_patterns("__pycache__"),
    )
    (here / "test_journeys.py").write_text(test_body)
    (root / "scripts").mkdir()
    shutil.copy(REPO_ROOT / "scripts" / "doc_examples.toml", root / "scripts")
    shutil.copy(REPO_ROOT / "scripts" / "qa_render.py", root / "scripts")
    shutil.copytree(
        REPO_ROOT / ".github" / "qa-templates", root / ".github" / "qa-templates"
    )
    (root / "backend" / "tests").mkdir(parents=True)
    (root / "backend" / "__init__.py").write_text("")
    (root / "backend" / "tests" / "__init__.py").write_text("")
    shutil.copy(
        REPO_ROOT / "backend" / "tests" / "no_real_aws.py", root / "backend" / "tests"
    )
    return root


def _run_runner(
    root: Path, tmp_path: Path, *args: str, **extra_env: str
) -> subprocess.CompletedProcess:
    env = {**os.environ, "DOCS_JOURNEY_RUN_DIR": str(tmp_path / "run"), **extra_env}
    env.pop("PYTEST_ADDOPTS", None)
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-c",
            "tests/acceptance/docs/pytest.ini",
            "tests/acceptance/docs",
            *args,
        ],
        cwd=root,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )


PASSING = "def test_planted():\n    assert True\n"
SKIPPING = (
    "import pytest\n\n\ndef test_planted():\n"
    '    getattr(pytest, "skip")("planted past the lint")\n'
)


def test_the_runner_session_passes_a_passing_test(tmp_path):
    """The control: the copy itself runs green, so a red below is the plant."""
    result = _run_runner(_runner_copy(tmp_path, PASSING), tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "1 passed" in result.stdout


def test_a_skip_in_the_runner_fails_the_run(tmp_path):
    result = _run_runner(_runner_copy(tmp_path, SKIPPING), tmp_path)
    assert result.returncode == 1, result.stdout + result.stderr
    assert "1 skipped" in result.stdout
    assert "was skipped; nothing here may skip, so the run fails" in result.stdout


def test_a_retry_plugin_stops_the_runner(tmp_path):
    root = _runner_copy(tmp_path, PASSING)
    (tmp_path / "plugins").mkdir()
    (tmp_path / "plugins" / "rerunfailures.py").write_text("")
    result = _run_runner(
        root, tmp_path, "-p", "rerunfailures", PYTHONPATH=str(tmp_path / "plugins")
    )
    assert result.returncode == 4, result.stdout + result.stderr
    assert "the rerunfailures plugin is loaded" in result.stderr


# ---------------------------------------------------------------------------
# The real tree
# ---------------------------------------------------------------------------
def test_every_journey_file_loads():
    """Every journey file in the tree loads against today's docs and inventory."""
    context = loader.context_for(REPO_ROOT)
    problems = []
    for path in sorted((RUNNER_ROOT / "journeys").glob("*.yaml")):
        try:
            loader.load(path, context)
        except loader.Refused as refused:
            problems.append(str(refused))
    assert not problems, "\n".join(problems)


def test_the_runner_bans_skips_and_writes_nothing_into_the_tree():
    """The lint ban and the pytest settings the runner's no-skip rule rests on."""
    ruff = tomllib.loads((RUNNER_ROOT / "ruff.toml").read_text(encoding="utf-8"))
    assert ruff["extend"] == "../../../pyproject.toml"
    assert "TID251" in ruff["lint"]["extend-select"]
    assert set(ruff["lint"]["flake8-tidy-imports"]["banned-api"]) == {
        "pytest.skip",
        "pytest.xfail",
        "pytest.importorskip",
        "pytest.mark.skip",
        "pytest.mark.skipif",
        "pytest.mark.xfail",
    }
    ini = (RUNNER_ROOT / "pytest.ini").read_text(encoding="utf-8")
    assert "empty_parameter_set_mark = fail_at_collect" in ini
    assert "-p no:cacheprovider" in ini


# ---------------------------------------------------------------------------
# The documentation site's own steps: crawl and search
# ---------------------------------------------------------------------------
SITE: Dict[str, Any] = {
    "guide": "guides/sample.md",
    "stack": "docs-published",
    "profile": "core",
    "video": True,
    "written": TODAY,
    "steps": [
        {
            "id": "nav",
            "doc": "sign-in",
            "do": {"crawl": "nav"},
            "fail": "a nav page differs from its source",
        },
        {
            "id": "links",
            "doc": "sign-in",
            "do": {"crawl": "links"},
            "fail": "a link to the site is broken",
        },
        {
            "id": "find",
            "doc": "sign-in",
            "do": {"search": "sign in"},
            "expect": {"found": "/guides/sample/"},
            "fail": "search does not find the guide",
        },
    ],
}


def _site(change: Callable[[Dict[str, Any]], None]) -> Dict[str, Any]:
    data = copy.deepcopy(SITE)
    change(data)
    return data


def test_a_site_journey_loads(tmp_path):
    journey = _load(tmp_path, SITE)
    assert [step.do.kind for step in journey.steps] == ["crawl", "crawl", "search"]
    assert not journey.steps[0].in_browser
    assert journey.steps[2].takes_screenshot
    assert checks.describe_expect(journey.steps[2]) == (
        "/guides/sample/ among the first 3 results"
    )
    assert checks.describe_action(journey.steps[1]) == (
        "follow every link to the site from the nav pages and their source"
    )
    assert "first # heading" in checks.describe_expect(journey.steps[0])


SITE_PLANTS = [
    pytest.param(
        lambda d: d["steps"][0].update(expect={"status": 200}),
        "crawl: nav checks what the action says; it has no expect",
        id="crawl-with-expect",
    ),
    pytest.param(
        lambda d: d["steps"][1].update(snapshot=False, snapshot_reason="none"),
        "a crawl step takes no ARIA snapshot; leave snapshot out",
        id="crawl-with-snapshot",
    ),
    pytest.param(
        lambda d: d["steps"][0]["do"].update(crawl="everything"),
        "'everything' is not one of the values allowed here",
        id="unknown-crawl",
    ),
    pytest.param(
        lambda d: d["steps"][2].pop("expect"),
        "a search step needs expect.found, the page it must find",
        id="search-without-found",
    ),
    pytest.param(
        lambda d: d["steps"][2]["expect"].update(aria="- heading"),
        "a search step expects only found, not ['aria']",
        id="search-with-aria",
    ),
    pytest.param(
        lambda d: d["steps"][2]["expect"].update(found="guides/sample/"),
        "String should match pattern",
        id="found-not-a-path",
    ),
    pytest.param(
        lambda d: d["steps"][2]["do"].update(search="sign\nin"),
        "must be one line",
        id="multi-line-search",
    ),
    pytest.param(
        lambda d: d.update(stack="marketing-local"),
        "crawl is for the documentation site's stacks",
        id="crawl-on-marketing",
    ),
]


@pytest.mark.parametrize("change, expected", SITE_PLANTS)
def test_a_planted_site_step_is_refused(tmp_path, change, expected):
    problems = _refusals(tmp_path, _site(change))
    assert any(expected in problem for problem in problems), problems


MKDOCS = """site_name: Sample
site_url: https://example.github.io/sample/
docs_dir: docs
nav:
  - Home: README.md
  - Guides:
    - Sample: guides/sample.md
    - Twice: guides/sample.md
    - Index: guides/index.md
  - Outside: https://example.com/
"""


def _source(
    root: Path, sample_heading: str = "Sample guide", extra: str = ""
) -> site.Source:
    (root / "docs" / "guides").mkdir(parents=True, exist_ok=True)
    (root / "mkdocs.yml").write_text(MKDOCS)
    (root / "docs" / "README.md").write_text("# Home *page*\n\nWords.\n")
    (root / "docs" / "guides" / "sample.md").write_text(
        f"```bash\n# not a heading\n```\n\n# {sample_heading}\n\n{extra}"
    )
    (root / "docs" / "guides" / "index.md").write_text("No heading at all.\n")
    return site.Source(root)


def test_the_nav_pages_their_urls_and_their_source_headings(tmp_path):
    pages = site.nav_pages(_source(tmp_path))
    assert pages == [
        site.NavPage("README.md", "", "Home page"),
        site.NavPage("guides/sample.md", "guides/sample/", "Sample guide"),
        site.NavPage("guides/index.md", "guides/", ""),
    ]
    assert site.page_url("rbac/README.md") == "rbac/"
    assert site.page_url("a/b.md") == "a/b/"
    assert site.site_url(_source(tmp_path)) == "https://example.github.io/sample/"


def test_a_source_without_a_nav_or_a_config_is_refused(tmp_path):
    with pytest.raises(site.SiteError, match="cannot read mkdocs.yml"):
        site.nav_pages(site.Source(tmp_path))
    (tmp_path / "mkdocs.yml").write_text("site_name: x\n")
    with pytest.raises(site.SiteError, match="no nav pages"):
        site.nav_pages(site.Source(tmp_path))


def test_absolute_links_to_the_site_written_in_the_source(tmp_path):
    source = _source(
        tmp_path,
        extra=(
            "See [power](https://example.github.io/sample/statistics/power/#top).\n"
            "Again: https://example.github.io/sample/statistics/power/, and\n"
            "`curl https://example.github.io/sample/api/x.json` and\n"
            "<https://example.github.io/sample/a/> but not https://example.com/b.\n"
        ),
    )
    assert site.absolute_links(source, ["README.md", "guides/sample.md"]) == [
        ("guides/sample.md", "https://example.github.io/sample/statistics/power/"),
        ("guides/sample.md", "https://example.github.io/sample/api/x.json"),
        ("guides/sample.md", "https://example.github.io/sample/a/"),
    ]


def test_only_links_to_the_site_are_its_links():
    base = "https://example.github.io/sample/"
    hrefs = [
        base,
        base + "a/#x",
        base + "a/",
        "https://example.github.io/sample-other/",
        "https://github.com/x",
        "mailto:x@example.com",
        "https://example.github.io/sample",
    ]
    assert site.site_links(hrefs, base) == [
        base,
        base + "a/",
        "https://example.github.io/sample",
    ]


def _fetcher(answers: Dict[str, Any]):
    def fetch(url: str):
        answer = answers[url]
        if isinstance(answer, Exception):
            raise answer
        return answer

    return fetch


BASE = "https://example.github.io/sample/"


@pytest.mark.parametrize(
    "answers, problem",
    [
        ({BASE + "a/": (200, "")}, ""),
        ({BASE + "a": (301, BASE + "a/"), BASE + "a/": (200, "")}, ""),
        ({BASE + "a": (301, "/sample/a/"), BASE + "a/": (200, "")}, ""),
        ({BASE + "a": (404, "")}, "answered 404"),
        ({BASE + "a": (302, "https://other.example/a/")}, "outside the site"),
        ({BASE + "a": (301, "")}, "301 with no Location"),
        ({BASE + "a": (301, BASE + "a")}, "more than 5 redirects"),
        ({BASE + "a": OSError("down")}, "no answer (OSError)"),
        ({BASE + "a": (301, BASE + "../other/")}, "outside the site"),
        ({BASE + "a": (301, BASE + "b/../c/"), BASE + "b/../c/": (200, "")}, ""),
    ],
    ids=[
        "ok",
        "redirect",
        "relative-redirect",
        "404",
        "out",
        "no-location",
        "loop",
        "down",
        "dot-segments-climb-out",
        "dot-segments-stay-in",
    ],
)
def test_a_link_resolves_only_within_the_site(answers, problem):
    link = next(iter(answers))
    resolved = site.resolve(link, BASE, _fetcher(answers), pause=lambda s: None)
    if problem:
        assert problem in resolved.problem, resolved
    else:
        assert resolved.problem == "", resolved


def _sequence(*answers):
    """A fetch that gives *answers* in turn, whatever the URL."""
    queue = list(answers)

    def fetch(url: str):
        answer = queue.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer

    return fetch


@pytest.mark.parametrize(
    "answers, problem, retried, pauses",
    [
        ([(503, ""), (200, "")], "", True, 1),
        ([OSError("reset"), (200, "")], "", True, 1),
        ([(503, ""), (503, "")], "answered 503", True, 1),
        ([OSError("reset"), OSError("reset")], "no answer (OSError)", True, 1),
        ([(404, "")], "answered 404", False, 0),
        ([(200, "")], "", False, 0),
    ],
    ids=["503-then-ok", "none-then-ok", "503-twice", "none-twice", "404-once", "ok"],
)
def test_a_5xx_or_no_answer_is_asked_once_more(answers, problem, retried, pauses):
    """GitHub Pages answers 503 now and then; a second 5xx is the finding, and
    a 4xx is never asked again."""
    waited = []
    resolved = site.resolve(BASE + "a/", BASE, _sequence(*answers), pause=waited.append)
    assert resolved.problem.startswith(problem) if problem else resolved.problem == ""
    assert resolved.retried is retried
    assert waited == [site.RETRY_SECONDS] * pauses


def _opener(*answers):
    """An ``open`` that gives *answers* in turn, (status or None, error); it
    records the URLs it was asked for."""
    queue = list(answers)
    asked: List[str] = []

    def open(url: str):
        asked.append(url)
        return queue.pop(0)

    open.asked = asked
    return open


@pytest.mark.parametrize(
    "answers, status, error, retried",
    [
        ([(503, ""), (200, "")], 200, "", True),
        ([(None, "net::ERR_RESET"), (200, "")], 200, "", True),
        ([(503, ""), (503, "")], 503, "", True),
        ([(None, "first"), (None, "second")], None, "second", True),
        ([(500, ""), (404, "")], 404, "", True),
        ([(404, "")], 404, "", False),
        ([(200, "")], 200, "", False),
    ],
    ids=[
        "503-then-ok",
        "none-then-ok",
        "503-twice",
        "none-twice",
        "500-then-404",
        "404-once",
        "ok",
    ],
)
def test_a_page_that_answers_5xx_or_nothing_is_opened_once_more(
    answers, status, error, retried
):
    """The same rule as a link's: the second answer is the one judged, and a
    4xx or a 2xx on the first is final, with no pause."""
    waited: List[float] = []
    opener = _opener(*answers)
    opened = site.open_page(BASE + "a/", opener, pause=waited.append)
    assert (opened.status, opened.error, opened.retried) == (status, error, retried)
    asks = 2 if retried else 1
    assert opener.asked == [BASE + "a/"] * asks
    assert waited == [site.RETRY_SECONDS] * (asks - 1)


def test_the_second_answer_is_the_one_a_nav_page_is_judged_by():
    """503 then 200 is green; 503 then 503 is red; a 404 is red and not asked again."""
    page = site.NavPage("a.md", "a/", "A")

    def problems(*answers):
        opener = _opener(*answers)
        opened = site.open_page(BASE + "a/", opener, pause=lambda seconds: None)
        seen = {"a.md": {"status": opened.status, "heading": "A", "on_site": True}}
        return site.compare_headings([page], seen), len(opener.asked)

    assert problems((503, ""), (200, "")) == ([], 2)
    assert problems((503, ""), (503, "")) == (["a.md (/a/): answered 503"], 2)
    assert problems((None, "x"), (None, "x")) == (["a.md (/a/): answered nothing"], 2)
    assert problems((404, "")) == (["a.md (/a/): answered 404"], 1)


@pytest.mark.parametrize(
    "url, inside",
    [
        (BASE, True),
        (BASE + "a/", True),
        ("https://example.github.io/sample", True),
        (BASE + "./a/", True),
        (BASE + "a/../b/", True),
        ("https://example.github.io/../sample/a/", True),
        (BASE + "../other/", False),
        (BASE + "a/../../other/", False),
        (BASE + "..", False),
        (BASE + "a/%2e%2e/%2E%2E/other/", False),
        ("https://example.github.io/sample-other/", False),
        ("http://example.github.io/sample/a/", False),
        ("https://other.example/sample/a/", False),
    ],
)
def test_a_url_with_dot_segments_is_judged_by_the_page_it_reaches(url, inside):
    """``.../sample/../other/`` starts with the site's address and is not a page
    of the site: a browser or a request resolves the dots first."""
    assert site.within(url, BASE) is inside


def test_a_link_with_dot_segments_that_leaves_the_site_is_not_the_sites():
    hrefs = [BASE + "a/", BASE + "../other/", BASE + "b/../c/"]
    assert site.site_links(hrefs, BASE) == [BASE + "a/", BASE + "b/../c/"]


@pytest.mark.parametrize(
    "checked, what, problems, twice, against, text",
    [
        (7, "nav pages", [], 0, "", "7 nav pages, every one as expected"),
        (
            7,
            "nav pages",
            [],
            0,
            "v0.25.1",
            "7 nav pages, every one as expected against v0.25.1",
        ),
        (
            7,
            "nav pages",
            [],
            2,
            "v0.25.1",
            "7 nav pages, every one as expected against v0.25.1 (2 asked twice)",
        ),
        (
            5,
            "links to the site",
            [],
            1,
            "",
            "5 links to the site, every one as expected (1 asked twice)",
        ),
        (
            7,
            "nav pages",
            ["a.md (/a/): answered 503", "b.md (/b/): answered 404"],
            1,
            "v0.25.1",
            "2 of 7 nav pages against v0.25.1 (1 asked twice):"
            " a.md (/a/): answered 503; b.md (/b/): answered 404",
        ),
        (
            7,
            "nav pages",
            ["a.md (/a/): answered 404"],
            0,
            "",
            "1 of 7 nav pages: a.md (/a/): answered 404",
        ),
    ],
)
def test_the_crawl_says_what_it_compared_with_and_how_many_were_asked_twice(
    checked, what, problems, twice, against, text
):
    assert site.crawl_text(checked, what, problems, twice, against) == text


def test_each_way_a_nav_page_can_differ_from_its_source(tmp_path):
    pages = [
        site.NavPage("ok.md", "ok/", "Ok"),
        site.NavPage("renamed.md", "renamed/", "Renamed"),
        site.NavPage("moved.md", "moved/", "Moved"),
        site.NavPage("retitled.md", "retitled/", "New title"),
        site.NavPage("bare.md", "bare/", "Bare"),
        site.NavPage("untitled.md", "untitled/", ""),
        site.NavPage("unvisited.md", "unvisited/", "Unvisited"),
    ]
    seen = {
        "ok.md": {"status": 200, "heading": "Ok", "on_site": True},
        "renamed.md": {"status": 404, "heading": None, "on_site": True},
        "moved.md": {"status": 200, "heading": "Moved", "on_site": False, "url": "x"},
        "retitled.md": {"status": 200, "heading": "Old  title", "on_site": True},
        "bare.md": {"status": 200, "heading": None, "on_site": True},
        "untitled.md": {"status": 200, "heading": "Something", "on_site": True},
    }
    problems = site.compare_headings(pages, seen)
    assert problems == [
        "renamed.md (/renamed/): answered 404",
        "moved.md (/moved/): left the site, to x",
        "retitled.md (/retitled/): the page's heading is 'Old title', the source's"
        " first heading is 'New title'",
        "bare.md (/bare/): no first-level heading on the page",
        "untitled.md (/untitled/): the source page has no # heading",
        "unvisited.md (/unvisited/): not visited",
    ]


def test_the_crawl_compares_with_the_source_it_is_given_not_main(tmp_path):
    """The published site is built from a release tag. A heading changed on
    main since then must not turn the crawl red, and one changed in the tag's
    source must: the crawl reads the source the stack names."""
    tag = _source(tmp_path / "tag", sample_heading="Sample guide")
    main = _source(tmp_path / "main", sample_heading="Sample guide, renamed on main")
    published = {  # what the site built from the tag shows
        "README.md": {"status": 200, "heading": "Home page", "on_site": True},
        "guides/sample.md": {"status": 200, "heading": "Sample guide", "on_site": True},
        "guides/index.md": {"status": 200, "heading": "Index", "on_site": True},
    }
    against_tag = site.compare_headings(site.nav_pages(tag), published)
    against_main = site.compare_headings(site.nav_pages(main), published)
    assert [p for p in against_tag if "sample" in p] == []
    assert [p for p in against_main if "sample" in p] == [
        "guides/sample.md (/guides/sample/): the page's heading is 'Sample guide',"
        " the source's first heading is 'Sample guide, renamed on main'"
    ]


def test_the_published_stack_takes_its_source_from_the_environment(tmp_path):
    from docs_runner import stacks

    tag = tmp_path / "tag"
    _source(tag)
    running = stacks.DocsPublished(
        tmp_path / "repo", tmp_path, {"DOCS_JOURNEY_PUBLISHED_SOURCE": str(tag)}
    ).up()
    assert running.source == tag
    assert running.source_ref == ""
    assert running.base_url == stacks.PUBLISHED_URL
    named = stacks.DocsPublished(
        tmp_path / "repo",
        tmp_path,
        {
            "DOCS_JOURNEY_PUBLISHED_SOURCE": str(tag),
            "DOCS_JOURNEY_PUBLISHED_REF": "v0.25.1",
        },
    ).up()
    assert named.source_ref == "v0.25.1"
    for odd in ("v1 && x", "-v1", "v1\nv2", "x" * 101, "$(id)"):
        with pytest.raises(stacks.StackError, match="not a git ref") as refused:
            stacks.DocsPublished(
                tmp_path / "repo", tmp_path, {"DOCS_JOURNEY_PUBLISHED_REF": odd}
            )
        assert odd not in str(refused.value)
    unset = stacks.DocsPublished(REPO_ROOT, tmp_path, {}).up()
    assert unset.source == REPO_ROOT
    with pytest.raises(stacks.StackError, match="holds no mkdocs.yml"):
        stacks.DocsPublished(
            REPO_ROOT, tmp_path, {"DOCS_JOURNEY_PUBLISHED_SOURCE": str(tmp_path / "x")}
        ).up()


def test_a_search_result_is_found_by_its_page():
    results = [
        BASE + "llm/quickstart/?h=quick+start",
        BASE + "getting-started/quick-start/?h=quick+start#top",
        BASE + "faq/?h=",
    ]
    assert site.rank(results, BASE, "/getting-started/quick-start/") == 2
    assert site.rank(results, BASE, "/getting-started/") is None
    assert site.rank([], BASE, "/faq/") is None


# ---------------------------------------------------------------------------
# The headers: QA templates through scripts/qa_render.py
# ---------------------------------------------------------------------------
SHA = "c6e0f081d82f1a858e111c86db62aa5d8b8a71d4"
LINK = "https://github.com/Controlvar/controlvar/actions/runs/18000000003"


@pytest.fixture(autouse=True)
def _run_repository(monkeypatch):
    """qa_render accepts a run link of ``GITHUB_REPOSITORY`` only: LINK's."""
    monkeypatch.setenv("GITHUB_REPOSITORY", "Controlvar/controlvar")


def _renderer():
    return report.qa_render()


def test_a_ci_run_renders_its_guide_header_from_the_fail_template(tmp_path):
    run = _failed_run(tmp_path)
    text = report.guide_markdown(
        run, tmp_path, date="2026-10-06", sha=SHA, run_link=LINK
    )
    expected = (
        _renderer()
        .render(
            "docs-guide-fail.tmpl",
            {
                "heading_guide": "Sample guide",
                "guide_path": "guides/sample.md",
                "date": "2026-10-06",
                "sha": SHA,
                "step_number": "2",
                "heading_step": "Sign in (sign-in)",
                "run_link": LINK,
                "count_steps_pass": "1",
                "count_steps": "3",
            },
        )
        .body
    )
    assert text.startswith(expected.rstrip("\n") + "\n")
    html_text = report.guide_html(
        run, tmp_path, date="2026-10-06", sha=SHA, run_link=LINK
    )
    assert "<h1>Sample guide (guides/sample.md), 2026-10-06, commit " in html_text
    assert "What to do: compare the expected and seen snapshots of step 2" in html_text


def test_a_ci_run_renders_pass_and_partial_headers():
    def run(*records):
        return report.GuideRun(
            "s", "README.md", "Rollback — Controlvar", "x", records=records
        )

    info = report.RunInfo("2026-10-06", SHA, LINK)
    passed = run(_record(), _record(step=2, result="NOT RUN", reason="doc-examples"))
    name, values = report.header_template(passed, info)
    assert name == "docs-guide-pass.tmpl"
    assert values["count_steps"] == "1"
    assert values["heading_guide"] == "Rollback - Controlvar"
    assert report.guide_header(passed, info).startswith(
        "# Rollback - Controlvar (README.md), 2026-10-06, commit "
    )
    partial = run(_record(), _record(step=2, result="NOT RUN", reason="needs-aws"))
    name, values = report.header_template(partial, info)
    assert name == "docs-guide-partial.tmpl"
    assert (values["count_not_run"], values["count_steps_pass"]) == ("1", "1")


@pytest.mark.parametrize(
    "text, folded",
    [
        ("Rollback Runbook — Controlvar", "Rollback Runbook - Controlvar"),
        ("Café “quoted”", 'Cafe "quoted"'),
        ("Costs $5 <b>", "Costs 5 b"),
        ("See https://example.org now", "See example.org now"),
        ("x" * 130, "x" * 120),
        ("☃", "untitled"),
    ],
)
def test_a_heading_is_folded_to_what_a_template_takes(text, folded):
    assert report.heading_value(text) == folded
    _renderer().check_value("heading_step", report.heading_value(text))


def test_a_failing_step_is_named_by_its_own_id_when_steps_share_a_heading():
    """The docs-site journey's steps all point at the home page's heading, so
    the heading alone says nothing about which of them failed."""
    heading = "Controlvar Documentation"

    def run(failing_id: str, failing_step: int):
        steps = ["home", "nav-pages", "links", "search-quick-start"]
        return report.GuideRun(
            "docs-site",
            "README.md",
            heading,
            "docs-published",
            records=tuple(
                _record(
                    guide="README.md",
                    step=number,
                    step_id=step_id,
                    heading=heading,
                    result="FAIL" if step_id == failing_id else "PASS",
                )
                for number, step_id in enumerate(steps, 1)
            ),
        )

    info = report.RunInfo("2026-10-06", SHA, LINK)
    issues = []
    for failing_id, number in (("nav-pages", 2), ("links", 3)):
        name, values = report.header_template(run(failing_id, number), info)
        assert name == "docs-guide-fail.tmpl"
        assert values["heading_step"] == f"{heading} ({failing_id})"
        issue = _renderer().render("docs-journey-issue.tmpl", values).body
        assert f'step {values["step_number"]}, "{heading} ({failing_id})"' in issue
        issues.append(issue)
    assert issues[0] != issues[1]


@pytest.mark.parametrize(
    "heading, step_id, named",
    [
        ("Sign in", "sign-in", "Sign in (sign-in)"),
        ("Rollback \u2014 Controlvar", "size", "Rollback - Controlvar (size)"),
        ("\u2603", "size", "untitled (size)"),
        ("x" * 130, "nav-pages", "x" * 108 + " (nav-pages)"),
        ("Home", "a" * 118, "(" + "a" * 118),
        ("Home", "a" * 119, "(" + "a" * 118),
    ],
    ids=["plain", "folded", "untitled", "heading-cut-id-whole", "id-118", "id-119"],
)
def test_a_step_is_named_within_what_a_template_takes(heading, step_id, named):
    assert report.step_heading(heading, step_id) == named
    _renderer().check_value("heading_step", report.step_heading(heading, step_id))


def test_a_header_the_renderer_refuses_fails_the_report(tmp_path):
    run = report.GuideRun("s", "guides/../x.md", "T", "x", records=(_record(),))
    with pytest.raises(report.ReportError, match="docs-guide-pass.tmpl: guide_path"):
        report.guide_markdown(run, tmp_path, date="2026-10-06", sha=SHA, run_link=LINK)


def test_a_run_outside_actions_keeps_a_plain_header(tmp_path):
    assert not report.RunInfo("2026-10-06", "unknown", "").published
    assert not report.RunInfo("2026-10-06", SHA, "https://example.com/1").published
    assert report.RunInfo("2026-10-06", SHA, LINK).published


def test_a_ci_summary_opens_with_the_run_template():
    failed = report.GuideRun(
        "sample", "guides/sample.md", "Sample guide", "x", error="down"
    )
    text = report.summary_markdown(
        [failed], INVENTORY, date="2026-10-06", sha=SHA, run_link=LINK
    )
    expected = (
        _renderer()
        .render(
            "docs-run-red.tmpl",
            {
                "date": "2026-10-06",
                "sha": SHA,
                "run_link": LINK,
                "count_pass": "0",
                "count_fail": "1",
                "count_partial": "0",
                "count_nav": "3",
                "count_not_covered": "2",
            },
        )
        .body
    )
    assert text.startswith(expected.rstrip("\n") + "\n")
    assert (
        "| Quick Start, end to end | sample: FAIL before step 1; later: planned in D3a"
        in text
    )
    assert "| FAIL: sample |" in text


def test_the_verdicts_record_carries_each_header_template_and_its_values(tmp_path):
    run = _failed_run(tmp_path)
    record = report.verdicts_record([run], report.RunInfo("2026-10-06", SHA, LINK))
    entry = record["journeys"]["sample"]
    assert (entry["word"], entry["step"], entry["template"]) == (
        "FAIL",
        2,
        "docs-guide-fail.tmpl",
    )
    rendered = _renderer().render("docs-journey-issue.tmpl", entry["values"])
    assert rendered.title == "Docs journey (guides/sample.md) is red"
    local = report.verdicts_record([run], report.RunInfo("2026-10-06", "unknown", ""))
    assert local["journeys"]["sample"]["values"] == {}


def test_the_verdicts_record_names_the_published_site_it_read(tmp_path):
    """verdicts.json says which ref and commit the published site was built
    from, as the workflow named them; scripts/rc_evidence.py compares them with
    the release tag. A value that is not a ref or a commit is recorded empty."""
    run = _failed_run(tmp_path)
    info = report.RunInfo("2026-10-06", SHA, LINK)
    named = report.published_source(
        {"DOCS_JOURNEY_PUBLISHED_REF": "v0.30.0", "DOCS_JOURNEY_PUBLISHED_SHA": SHA}
    )
    assert report.verdicts_record([run], info, named)["published"] == {
        "ref": "v0.30.0",
        "sha": SHA,
    }
    assert report.verdicts_record([run], info)["published"] == {"ref": "", "sha": ""}
    odd = report.published_source(
        {
            "DOCS_JOURNEY_PUBLISHED_REF": "v0.30.0 main",
            "DOCS_JOURNEY_PUBLISHED_SHA": "abc",
        }
    )
    assert odd == {"ref": "", "sha": ""}


def test_the_real_inventory_maps_every_flow_and_names_where_each_journey_waits():
    inventory = loader.context_for(REPO_ROOT).inventory
    rows, waiting = report.flow_rows(inventory, {})
    assert len(rows) == 9
    planned = {journey for journeys in waiting.values() for journey in journeys}
    pending = {
        entry["journey"]
        for entry in inventory["pages"].values()
        if entry.get("pending") is True
    }
    flows = {j for flow in inventory["flow"] for j in flow.get("journeys", [])}
    assert planned == pending & flows
    assert set(waiting) <= {
        "planned in D2",
        "planned in D3a",
        "planned in D3b",
        "planned, no pull request assigned yet",
    }
    assert not any(row[3].startswith("verified by") for row in rows)


def test_the_run_summary_reads_pages_and_flows_and_ignores_documents():
    """inventory.toml's [documents] and [meta] (#1142) change nothing in the
    run summary: it lists nav pages and flows only."""
    inventory = loader.context_for(REPO_ROOT).inventory
    assert inventory["documents"] and inventory["meta"]
    pages_only = {key: inventory[key] for key in ("pages", "flow")}
    assert report.summary_markdown(
        [], inventory, date="2026-10-11", sha="abc"
    ) == report.summary_markdown([], pages_only, date="2026-10-11", sha="abc")
    assert [row[0] for row in report.not_covered(inventory, [])] == sorted(
        inventory["pages"]
    )


@pytest.mark.regression
def test_a_partial_journey_does_not_verify_its_flow():
    """A journey that ran with steps not run (PARTIAL) leaves its flow partly
    verified, and the summary's count of verified flows leaves that flow out."""
    inventory = copy.deepcopy(INVENTORY)
    inventory["pages"]["guides/third.md"] = {
        "class": "journey",
        "journey": "third",
        "reason": "walked",
    }
    inventory["flow"] = [
        {"text": "Sample alone", "journeys": ["sample"]},
        {"text": "Sample and third", "journeys": ["sample", "third"]},
    ]

    def ran(journey: str, *records: log.Record) -> report.GuideRun:
        return report.GuideRun(
            journey, f"guides/{journey}.md", journey, "compose-dev", records=records
        )

    partial = ran(
        "sample", _record(), _record(step=2, result="NOT RUN", reason="needs-aws")
    )
    passed = ran("third", _record(guide="guides/third.md"))
    verdicts = {run.journey: report.verdict(run) for run in (partial, passed)}
    assert verdicts["sample"].word == "PARTIAL"
    rows, _ = report.flow_rows(inventory, verdicts)
    assert [row[3] for row in rows] == [
        "partly verified: sample has steps not run",
        "partly verified: sample has steps not run; verified by third",
    ]
    text = report.summary_markdown(
        [partial, passed], inventory, date="2026-10-06", sha="abc"
    )
    assert "0 of 2 flows are verified by journeys that ran in this run." in text
    assert "| verified by" not in text


# ---------------------------------------------------------------------------
# Carried over from #966's review: the session's edges
# ---------------------------------------------------------------------------
def test_a_session_with_no_journeys_directory_stops_at_collection(tmp_path):
    """The real test module with no journeys/: pytest's exit 2, never green."""
    root = _runner_copy(
        tmp_path, (RUNNER_ROOT / "test_journeys.py").read_text(encoding="utf-8")
    )
    assert not (root / "tests" / "acceptance" / "docs" / "journeys").exists()
    result = _run_runner(root, tmp_path)
    assert result.returncode == 2, result.stdout + result.stderr
    assert "Empty parameter set" in result.stdout + result.stderr


def test_a_session_that_runs_no_journey_writes_nothing(tmp_path):
    """No DOCS_JOURNEY_RUN_DIR and no journey run: no run directory is made."""
    root = _runner_copy(tmp_path, PASSING)
    scratch = tmp_path / "tmp"
    scratch.mkdir()
    env = {**os.environ, "TMPDIR": str(scratch)}
    env.pop("DOCS_JOURNEY_RUN_DIR", None)
    env.pop("PYTEST_ADDOPTS", None)
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-c",
            "tests/acceptance/docs/pytest.ini",
            "tests/acceptance/docs",
        ],
        cwd=root,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "nothing written" in result.stdout
    assert [p.name for p in scratch.iterdir() if "docs-journeys" in p.name] == []


def test_the_run_directory_is_made_in_one_place():
    """conftest makes no temporary directory of its own: log.run_directory does."""
    conftest = (RUNNER_ROOT / "conftest.py").read_text(encoding="utf-8")
    assert "mkdtemp" not in conftest
    assert "run_directory(os.environ, REPO_ROOT)" in conftest
    made = log.run_directory({}, REPO_ROOT)
    try:
        assert made.is_dir() and made.name.startswith("docs-journeys-run-")
    finally:
        made.rmdir()


def test_the_static_server_refuses_what_it_cannot_serve(tmp_path, monkeypatch):
    from docs_runner import stacks

    with pytest.raises(stacks.StackError, match="nothing to serve"):
        stacks._StaticServer(tmp_path / "absent", 1).start()
    a_file = tmp_path / "file"
    a_file.write_text("x")
    with pytest.raises(stacks.StackError, match="nothing to serve"):
        stacks._StaticServer(a_file, 1).start()

    def cannot(self):
        raise OSError("no python")

    monkeypatch.setattr(stacks._StaticServer, "_spawn", cannot)
    with pytest.raises(
        stacks.StackError, match=r"the static server could not be started \(OSError\)"
    ):
        stacks._StaticServer(tmp_path, 1).start()


def test_a_refused_journey_still_names_a_written_date_after_today(tmp_path):
    """The model refuses the journey (an unknown field), yet the date check
    still runs on what parsed; a date-and-time is not a date."""
    data = _with(lambda d: d.update(written=TODAY + datetime.timedelta(days=2)))
    data["steps"][0]["retries"] = 3
    problems = _refusals(tmp_path, data)
    assert any("retries: Extra inputs are not permitted" in p for p in problems)
    assert any("is after today" in p for p in problems), problems
    timed = _with(lambda d: d.update(written=datetime.datetime(2026, 10, 9, 10, 0)))
    timed["steps"][0]["retries"] = 3
    problems = _refusals(tmp_path, timed)
    assert not any("is after today" in p for p in problems), problems
    assert any(p.startswith("written:") for p in problems), problems


def test_the_runner_loads_the_aws_plugin_in_configure_not_by_pytest_plugins():
    conftest = (RUNNER_ROOT / "conftest.py").read_text(encoding="utf-8")
    assert "pytest_plugins" not in conftest.split('"""', 2)[2]
    assert 'config.pluginmanager.import_plugin("backend.tests.no_real_aws")' in conftest


def test_a_run_started_from_tests_acceptance_collects_the_journeys():
    """From tests/acceptance the runner's conftest is not the run's root
    conftest; it must still load (pytest refuses pytest_plugins there)."""
    env = {**os.environ}
    env.pop("PYTEST_ADDOPTS", None)
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "tests/acceptance",
            "--collect-only",
            "-q",
            "-p",
            "no:cacheprovider",
            "-o",
            "addopts=",
        ],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "test_journey[docs-site]" in result.stdout


# ---------------------------------------------------------------------------
# D3a: a guide's addresses on the compose stack, and what a run must not write
# ---------------------------------------------------------------------------
#: A journey on the compose stack that opens the guide's address, makes up one
#: password, keeps one value from the screen and types both.
SECRETS: Dict[str, Any] = {
    "guide": "guides/sample.md",
    "stack": "compose-dev",
    "profile": "core",
    "video": False,
    "written": TODAY,
    "passwords": ["chosen"],
    "steps": [
        {
            "id": "api-docs",
            "doc": "create",
            "do": {"open": "http://localhost:8000/api/v1/docs"},
            "expect": {"status": 200},
            "snapshot": False,
            "snapshot_reason": "only that the address answers matters here",
            "fail": "nothing answers at the guide's address",
        },
        {
            "id": "temporary",
            "doc": "sign-in",
            "do": {
                "keep": {
                    "secret": "temporary",
                    "role": "code",
                    "within": {
                        "role": "dialog",
                        "name": "User created",
                        "quote": False,
                        "reason": "the dialog the guide's Sign in leads to",
                    },
                }
            },
            "fail": "no one-time password is shown",
        },
        {
            "id": "current",
            "doc": "sign-in",
            "do": {"fill": {"label": "Password", "secret": "temporary"}},
            "expect": {"visible": [{"role": "button", "name": "Sign in"}]},
            "snapshot": False,
            "snapshot_reason": "the field holds the kept password",
            "fail": "there is no Password field",
        },
        {
            "id": "new",
            "doc": "sign-in",
            "do": {"fill": {"label": "Password", "secret": "chosen"}},
            "expect": {"visible": [{"role": "button", "name": "Sign in"}]},
            "snapshot": False,
            "snapshot_reason": "the field holds the made-up password",
            "fail": "there is no Password field",
        },
    ],
}


def _secrets(change: Callable[[Dict[str, Any]], None]) -> Dict[str, Any]:
    data = copy.deepcopy(SECRETS)
    change(data)
    return data


def test_a_journey_with_secrets_loads(tmp_path):
    journey = _load(tmp_path, SECRETS)
    assert [step.do.kind for step in journey.steps] == ["open", "keep", "fill", "fill"]
    assert journey.has_secrets
    assert [step.secret for step in journey.steps] == [
        None,
        "temporary",
        "temporary",
        "chosen",
    ]
    assert not any(step.takes_screenshot for step in journey.steps)


SECRET_PLANTS = [
    pytest.param(
        _on_step("api-docs", lambda s: s["do"].update(open="http://localhost:3001")),
        "open 'http://localhost:3001' is not in the guide's text",
        id="open-an-address-the-guide-does-not-give",
    ),
    pytest.param(
        _on_step(
            "api-docs", lambda s: s["do"].update(open="https://example.com:8000/")
        ),
        "open is an address the guide gives on this machine",
        id="open-a-remote-url",
    ),
    pytest.param(
        _on_step("api-docs", lambda s: s["do"].update(goto="http://localhost:8000")),
        "goto is a path on the stack",
        id="goto-a-url",
    ),
    pytest.param(
        lambda data: data.update(stack="docs-local"),
        "open is for an address the guide gives for the compose stack",
        id="open-on-another-stack",
    ),
    pytest.param(
        _on_step("temporary", lambda s: s.update(expect={"text": "Done"})),
        "keep checks what the action says",
        id="keep-with-an-expect",
    ),
    pytest.param(
        _on_step("temporary", lambda s: s.update(snapshot=True)),
        "a keep step keeps no screen; leave snapshot out",
        id="keep-with-a-snapshot",
    ),
    pytest.param(
        _on_step(
            "temporary",
            lambda s: s["do"]["keep"]["within"].update(quote=True, reason=None),
        ),
        "keep.within.name 'User created' is not in the guide's text",
        id="keep-within-a-name-the-guide-does-not-say",
    ),
    pytest.param(
        _on_step(
            "current",
            lambda s: [s.pop(key) for key in ("snapshot", "snapshot_reason")],
        ),
        "a fill that types the secret 'temporary' keeps no screen",
        id="secret-fill-with-a-screen",
    ),
    pytest.param(
        _on_step("new", lambda s: s["do"]["fill"].update(secret="unknown")),
        "fill types the secret 'unknown', which no passwords entry makes up",
        id="secret-nobody-makes",
    ),
    pytest.param(
        lambda data: data["steps"].insert(1, data["steps"].pop(2)),
        "fill types the secret 'temporary', which no passwords entry makes up",
        id="secret-typed-before-it-is-kept",
    ),
    pytest.param(
        lambda data: data.update(video=True),
        "video: true, but the journey makes up or keeps a secret",
        id="recorded-with-a-secret",
    ),
    pytest.param(
        lambda data: data.update(passwords=["chosen", "temporary"]),
        "secret 'temporary' is made up or kept twice",
        id="secret-named-twice",
    ),
    pytest.param(
        _on_step("new", lambda s: s["do"]["fill"].update(value="typed")),
        "a fill types exactly one of value or secret",
        id="fill-value-and-secret",
    ),
    pytest.param(
        _on_step("new", lambda s: s["do"]["fill"].pop("secret")),
        "a fill types exactly one of value or secret",
        id="fill-neither",
    ),
]


@pytest.mark.parametrize("change, expected", SECRET_PLANTS)
def test_a_planted_secret_step_is_refused(tmp_path, change, expected):
    problems = _refusals(tmp_path, _secrets(change))
    assert any(expected in problem for problem in problems), problems


def test_a_secret_step_is_described_by_name_never_by_value(tmp_path):
    journey = _load(tmp_path, SECRETS)
    lines = [checks.describe_action(step) for step in journey.steps]
    assert lines == [
        "open http://localhost:8000/api/v1/docs, the guide's address",
        'keep the text of the code in the dialog "User created" as temporary',
        'fill "Password" with the password temporary',
        'fill "Password" with the password chosen',
    ]
    assert checks.describe_expect(journey.steps[1]) == (
        "exactly one code there, holding at least 8 characters (kept, never written)"
    )


def test_the_compose_files_default_ports_are_read_by_variable(tmp_path):
    from docs_runner import stacks

    (tmp_path / "docker-compose.yml").write_text(COMPOSE_FILE)
    assert stacks.compose_default_ports(tmp_path / "docker-compose.yml") == {
        "API_HOST_PORT": 8000,
        "FRONTEND_HOST_PORT": 3000,
        "POSTGRES_HOST_PORT": 5432,
    }
    real = stacks.compose_default_ports(REPO_ROOT / "docker-compose.yml")
    assert set(stacks.HTTP_PORT_VARIABLES) <= set(real)


def test_the_default_ports_are_read_through_a_loopback_binding(tmp_path):
    """`"127.0.0.1:${VAR:-port}:target"` is how docker-compose.yml publishes them."""
    from docs_runner import stacks

    (tmp_path / "docker-compose.yml").write_text(
        COMPOSE_FILE.replace('- "$', '- "127.0.0.1:$')
    )
    assert stacks.compose_default_ports(tmp_path / "docker-compose.yml") == {
        "API_HOST_PORT": 8000,
        "FRONTEND_HOST_PORT": 3000,
        "POSTGRES_HOST_PORT": 5432,
    }


def test_a_compose_file_without_a_default_port_stops_before_docker(tmp_path):
    bin_dir = _fake_docker(tmp_path, 0)
    stacks, stack = _compose(
        tmp_path, bin_dir, COMPOSE_FILE.replace("${FRONTEND_HOST_PORT:-3000}", "3000")
    )
    with pytest.raises(stacks.StackError, match="no default host port for FRONTEND"):
        stack.up("core")
    assert not (bin_dir / "calls.log").exists()


def test_a_made_up_password_meets_the_account_rules():
    from docs_runner import redaction

    made = {redaction.make_password() for _ in range(200)}
    assert len(made) == 200
    for value in made:
        assert len(value) == redaction.PASSWORD_LENGTH
        assert any(c.isupper() for c in value)
        assert any(c.islower() for c in value)
        assert any(c.isdigit() for c in value)
        assert value.isascii() and value.isalnum()


def test_the_redactor_takes_every_kept_value_out():
    from docs_runner import redaction

    redactor = redaction.Redactor()
    redactor.add("Kept0Value9abc")
    redactor.add("Kept0Value9abcdef")
    text = '- textbox "Password": Kept0Value9abcdef\n- code: Kept0Value9abc\n'
    assert redactor.redact(text) == (
        '- textbox "Password": (redacted)\n- code: (redacted)\n'
    )
    with pytest.raises(ValueError, match="shorter than 8"):
        redactor.add("short")
    assert redaction.redacted_path("access_token")
    assert redaction.redacted_path("items.0.key")
    assert not redaction.redacted_path("token_type")
    assert not redaction.redacted_path("keys.0.name")
    # An experiment's or a flag's key is written when it is the expected one,
    # and is never looked for in the run's files: it is no credential.
    assert redaction.credential_path("access_token")
    assert not redaction.credential_path("key")


def test_the_scan_removes_every_file_holding_a_value_and_records_only_names(
    tmp_path,
):
    from docs_runner import redaction

    run = tmp_path / "run"
    (run / "journey").mkdir(parents=True)
    (run / "journey" / "01-sign-in.aria.yml").write_text(
        '- textbox "Password": Planted0Value9xyz\n'
    )
    (run / "journey" / "02-done.png").write_bytes(b"\x89PNG..Planted0Value9xyz..")
    (run / "results.jsonl").write_text('{"observed": "Planted0Value9xyz"}\n')
    (run / "journey" / "03-clean.aria.yml").write_text('- heading "Experiments"\n')
    (run / "summary.md").write_text("all clean\n")
    record = redaction.clear(run, {"Planted0Value9xyz"}, owners={})
    assert record == {
        "files_read": 5,
        "values": 1,
        "removed": [
            "journey/01-sign-in.aria.yml",
            "journey/02-done.png",
            "results.jsonl",
        ],
        "screens_removed": [],
    }
    assert sorted(p.name for p in run.rglob("*") if p.is_file()) == [
        "03-clean.aria.yml",
        redaction.SCAN_RECORD,
        "summary.md",
    ]
    assert "Planted0Value9xyz" not in (run / redaction.SCAN_RECORD).read_text()
    again = redaction.clear(run, {"Planted0Value9xyz"}, owners={})
    assert again["removed"] == [] and again["files_read"] == 2


def test_the_scan_finds_a_value_written_json_escaped(tmp_path):
    from docs_runner import redaction

    value = 'Quote"Back\\slash9'
    (tmp_path / "a.json").write_text(json.dumps({"seen": value}))
    assert redaction.scan(tmp_path, [value]) == (1, ["a.json"])


PLANTED_VALUE = (
    "import conftest\n\n\ndef test_planted(request):\n"
    "    run_dir = conftest.run_dir_of(request.config)\n"
    '    request.config.stash[conftest.SECRET_VALUES].add("Planted0Value9xyz")\n'
    '    (run_dir / "journey").mkdir()\n'
    '    (run_dir / "journey" / "01-x.aria.yml").write_text(\n'
    "        '- textbox \"Password\": Planted0Value9xyz\\n'\n"
    "    )\n"
)


def test_a_value_left_in_the_run_directory_fails_the_run_and_is_removed(tmp_path):
    from docs_runner import redaction

    result = _run_runner(_runner_copy(tmp_path, PLANTED_VALUE), tmp_path)
    assert result.returncode == 1, result.stdout + result.stderr
    assert "1 passed" in result.stdout
    assert (
        "journey/01-x.aria.yml held a value the run made up, kept or signed in for;"
        " it was removed, so the run fails"
    ) in result.stdout
    run = tmp_path / "run"
    assert not (run / "journey" / "01-x.aria.yml").exists()
    record = json.loads((run / redaction.SCAN_RECORD).read_text())
    assert record["removed"] == ["journey/01-x.aria.yml"]
    assert "Planted0Value9xyz" not in result.stdout + result.stderr


def test_a_clean_run_directory_is_scanned_and_recorded(tmp_path):
    """The control for the plant above: the scan runs, finds nothing, the run passes."""
    from docs_runner import redaction

    result = _run_runner(_runner_copy(tmp_path, PASSING), tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr
    record = json.loads((tmp_path / "run" / redaction.SCAN_RECORD).read_text())
    assert record["removed"] == [] and record["values"] == 0
    assert "0 held one" in result.stdout


# ---------------------------------------------------------------------------
# #1004 review: a hit stops the upload and fails the journey; screens; deep
# redaction; error lines; written passwords; the sign-in limit
# ---------------------------------------------------------------------------
PLANT = "Planted0Value9xyz"


def test_a_removed_file_takes_its_screenshot_and_its_recording_with_it(tmp_path):
    from docs_runner import redaction

    run = tmp_path / "run"
    (run / "invite").mkdir(parents=True)
    (run / "videos").mkdir()
    (run / "invite" / "02-reveal.aria.yml").write_text(f"- code: {PLANT}\n")
    (run / "invite" / "02-reveal.png").write_bytes(b"\x89PNG pixels, not bytes")
    (run / "invite" / "03-done.png").write_bytes(b"\x89PNG")
    (run / "videos" / "invite.webm").write_bytes(b"webm")
    (run / "videos" / "other.webm").write_bytes(b"webm")
    record = redaction.clear(run, {PLANT}, owners={})
    assert record["removed"] == ["invite/02-reveal.aria.yml"]
    assert record["screens_removed"] == [
        "invite/02-reveal.png",
        "videos/invite.webm",
    ]
    assert (run / "invite" / "03-done.png").is_file()
    assert (run / "videos" / "other.webm").is_file()


def test_a_shared_file_holding_a_value_takes_every_recording_and_fails_every_journey(
    tmp_path,
):
    from docs_runner import redaction

    run = tmp_path / "run"
    (run / "videos").mkdir(parents=True)
    (run / "results.jsonl").write_text(f'{{"observed": "{PLANT}"}}\n')
    for name in ("a", "b"):
        (run / "videos" / f"{name}.webm").write_bytes(b"webm")
    record = redaction.clear(run, {PLANT}, owners={})
    assert record["screens_removed"] == ["videos/a.webm", "videos/b.webm"]
    assert redaction.journeys_of(["results.jsonl"], ["a", "b"], owners={}) == {"a", "b"}
    assert redaction.journeys_of(
        ["a/01-x.aria.yml", "a.html"], ["a", "b"], owners={}
    ) == {"a"}
    assert redaction.files_of(
        ["a/01-x.aria.yml", "b.md", "summary.md"], "a", owners={}
    ) == (
        "a/01-x.aria.yml",
        "summary.md",
    )


def test_a_journey_whose_files_the_scan_removed_is_fail_in_verdicts(tmp_path):
    passed = _record(result=log.PASS, observed="at /experiments")
    clean = report.GuideRun(
        journey="sample",
        guide="guides/sample.md",
        title="Sample",
        stack="compose-dev",
        records=(passed,),
    )
    assert report.verdict(clean).word == "PASS"
    hit = report.GuideRun(**{**clean.__dict__, "scrubbed": ("sample/01-x.aria.yml",)})
    result = report.verdict(hit)
    assert result.word == "FAIL"
    assert result.line == "FAIL: its files held a value the run kept"
    info = report.RunInfo("2026-10-06", "a" * 40, "")
    assert report.verdicts_record([hit], info)["journeys"]["sample"]["word"] == "FAIL"


SCRUBBED_RUN = (
    "import conftest\n"
    "from docs_runner import log, report\n\n\n"
    "def test_planted(request):\n"
    "    run_dir = conftest.run_dir_of(request.config)\n"
    f'    request.config.stash[conftest.SECRET_VALUES].add("{PLANT}")\n'
    '    (run_dir / "sample").mkdir()\n'
    '    (run_dir / "videos").mkdir()\n'
    '    (run_dir / "sample" / "02-reveal.aria.yml").write_text(\n'
    f"        '- code: {PLANT}\\n'\n"
    "    )\n"
    '    (run_dir / "sample" / "02-reveal.png").write_bytes(b"pixels")\n'
    '    (run_dir / "videos" / "sample.webm").write_bytes(b"webm")\n'
    "    record = log.Record(\n"
    '        run="r", sha="s", stack="compose-dev", guide="README.md", step=1,\n'
    '        step_id="home", heading="Home", action="open /", expected="x",\n'
    '        failure_signature="x", result="PASS", reason="", observed="at /",\n'
    '        snapshot="", expected_snapshot="structural only", ms=1,\n'
    "    )\n"
    "    request.config.stash[conftest.RUNS].append(\n"
    "        report.GuideRun(\n"
    '            journey="sample", guide="README.md", title="Home",\n'
    '            stack="compose-dev", records=(record,),\n'
    "        )\n"
    "    )\n"
)


def test_a_scan_hit_fails_the_journey_in_verdicts_json_and_removes_its_screens(
    tmp_path,
):
    """The journey passed every step; its files held a value: FAIL, and its
    screenshot and recording are gone with the file."""
    from docs_runner import redaction

    result = _run_runner(_runner_copy(tmp_path, SCRUBBED_RUN), tmp_path)
    assert result.returncode == 1, result.stdout + result.stderr
    run = tmp_path / "run"
    verdicts = json.loads((run / "verdicts.json").read_text())
    assert verdicts["journeys"]["sample"]["word"] == "FAIL"
    record = json.loads((run / redaction.SCAN_RECORD).read_text())
    assert record["removed"] == ["sample/02-reveal.aria.yml"]
    assert record["screens_removed"] == ["sample/02-reveal.png", "videos/sample.webm"]
    assert not (run / "sample" / "02-reveal.png").exists()
    assert not (run / "videos" / "sample.webm").exists()
    assert "1 failed" in result.stdout.split("docs journeys:")[1]
    assert PLANT not in result.stdout + result.stderr


def test_the_step_that_reveals_a_kept_value_keeps_no_screen(tmp_path):
    """The reviewer's case: the step before a keep took a screenshot of the
    dialog, password in clear. Now that journey does not load."""
    data = _secrets(
        lambda d: d["steps"].__setitem__(
            0,
            {
                "id": "invite",
                "doc": "sign-in",
                "do": {"click": {"role": "button", "name": "Sign in"}},
                "expect": {"aria": '- dialog "User created"'},
                "fail": "no dialog",
            },
        )
    )
    problems = _refusals(tmp_path, data)
    assert any(
        "step 1 (invite): the next step keeps a value this step's screen shows"
        in problem
        for problem in problems
    ), problems


@pytest.mark.parametrize(
    "value, refused",
    [("Hunter2Hunter2", True), ("Demo1234!", False)],
    ids=["a-written-password", "the-documented-demo-password"],
)
def test_a_written_password_is_typed_only_where_no_screen_is_kept(
    tmp_path, value, refused
):
    data = _with(
        lambda d: d["steps"].insert(
            2,
            {
                "id": "typed",
                "doc": "sign-in",
                "do": {"fill": {"label": "Password", "value": value}},
                "expect": {"aria": '- textbox "Password"'},
                "fail": "no Password field",
            },
        )
    )
    if refused:
        problems = _refusals(tmp_path, data)
        assert any(
            "fill types a written password into 'Password'" in p for p in problems
        )
    else:
        _load(tmp_path, data)
    with_no_screen = copy.deepcopy(data)
    typed = _step(with_no_screen, "typed")
    typed.update(
        snapshot=False,
        snapshot_reason="the field holds a password",
        expect={"visible": [{"role": "button", "name": "Sign in"}]},
    )
    _load(tmp_path, with_no_screen)


@pytest.mark.parametrize(
    "api, recorded_ok",
    [
        ({"method": "POST", "path": "/api/v1/auth/login", "as": "anonymous"}, False),
        ({"method": "POST", "path": "/api/v1/api-keys", "as": "admin"}, False),
        ({"method": "GET", "path": "/api/v1/experiments/", "as": "admin"}, True),
    ],
    ids=["sign-in-route", "api-key-route", "no-credential"],
)
def test_an_api_step_that_reveals_a_credential_keeps_the_journey_unrecorded(
    tmp_path, api, recorded_ok
):
    data = _with(
        lambda d: (
            d.update(video=True),
            _step(d, "list").update(do={"api": api}, expect={"status": 200}),
        )
    )
    if recorded_ok:
        assert not _load(tmp_path, data).has_secrets
    else:
        problems = _refusals(tmp_path, data)
        assert any("an api step reveals one" in p for p in problems), problems
    expects_a_token = _with(
        lambda d: (
            d.update(video=True),
            _step(d, "list").update(
                expect={"status": 200, "json": {"user": {"access_token": "x"}}}
            ),
        )
    )
    assert any(
        "an api step reveals one" in p for p in _refusals(tmp_path, expects_a_token)
    )


# -- deep redaction, through the pure function and through _api_step ---------
def test_scrub_redacts_every_credential_at_any_depth_and_registers_it():
    from docs_runner import redaction

    redactor = redaction.Redactor()
    value = {
        "user": {"name": "ada", "access_token": "Tok0nestedValue1"},
        "items": [{"token": "Tok0inListValue2", "id": 1}],
        "key": "homepage_button_colour",
    }
    shown = redaction.scrub("answer", value, redactor=redactor)
    assert shown == {
        "user": {"name": "ada", "access_token": redaction.REDACTED},
        "items": [{"token": redaction.REDACTED, "id": 1}],
        "key": redaction.REDACTED,
    }
    assert redactor.values == {"Tok0nestedValue1", "Tok0inListValue2"}
    expected = {"key": "homepage_button_colour"}
    assert redaction.scrub("x", {"key": "homepage_button_colour"}, expected) == expected
    redactor = redaction.Redactor()
    redaction.register_credentials(
        {"a": [{"refresh_token": "Ref0reshValue3"}], "key": "eptk_notregistered"},
        redactor,
    )
    assert redactor.values == {"Ref0reshValue3"}


class _FakeAnswer:
    def __init__(self, status: int, body: Any):
        self.status = status
        self._body = body

    def json(self):
        return self._body


class _FakeApi:
    def __init__(self, answer: _FakeAnswer):
        self.answer = answer
        self.calls: List[tuple] = []

    def fetch(self, path, method, headers, data):
        self.calls.append((method, path, headers))
        return self.answer


@pytest.fixture
def execute_module(monkeypatch):
    """docs_runner.execute with a stand-in for Playwright, which the unit job lacks.

    Only what the module touches at import and in JourneyRunner's __init__ is
    stood in for; _api_step and _line use none of it.
    """
    import importlib
    import types

    sync_api = types.ModuleType("playwright.sync_api")

    class _Expect:
        def __call__(self, *args, **kwargs):
            raise AssertionError("no browser here")

        def set_options(self, **kwargs):
            return None

    sync_api.Error = type("Error", (Exception,), {})
    sync_api.Browser = sync_api.Page = sync_api.Playwright = object
    sync_api.expect = _Expect()
    package = types.ModuleType("playwright")
    package.sync_api = sync_api
    monkeypatch.setitem(sys.modules, "playwright", package)
    monkeypatch.setitem(sys.modules, "playwright.sync_api", sync_api)
    monkeypatch.delitem(sys.modules, "docs_runner.execute", raising=False)
    module = importlib.import_module("docs_runner.execute")
    yield module
    sys.modules.pop("docs_runner.execute", None)


def _runner(execute_module, run_dir: Path):
    return execute_module.JourneyRunner(
        None,
        None,
        log.Log(run_dir / "results.jsonl"),
        execute_module.Settings(run_dir=run_dir, run_id="r", sha="s"),
    )


def test_an_api_step_writes_no_nested_credential_and_the_scan_knows_them(
    tmp_path, execute_module
):
    from docs_runner import redaction
    from docs_runner.model import Step
    from docs_runner.stacks import Running

    step = Step.model_validate(
        {
            "id": "me",
            "doc": "x",
            "do": {"api": {"method": "GET", "path": "/me", "as": "anonymous"}},
            "expect": {
                "status": 200,
                "json": {
                    "nested": {"access_token": "whatever"},
                    "items": [{"token": "whatever", "id": 1}],
                },
            },
            "fail": "x",
        }
    )
    body = {
        "nested": {"access_token": "Acc0essNested1"},
        "items": [{"token": "Tok0enInList2", "id": 1}],
        "elsewhere": {"refresh_token": "Ref0reshUnwritten3"},
    }
    runner = _runner(execute_module, tmp_path)
    running = Running(name="compose-dev", base_url="http://dash/", api_url="http://api")
    with pytest.raises(execute_module._Failed) as failed:
        runner._api_step(step, running, _FakeApi(_FakeAnswer(200, body)), "j", 1)
    written = (tmp_path / "j" / "01-me.api.json").read_text()
    for value in ("Acc0essNested1", "Tok0enInList2", "Ref0reshUnwritten3"):
        assert value not in written
        assert value not in failed.value.observed
        assert value in runner.redactor.values
    assert json.loads(written)["nested"] == {"access_token": redaction.REDACTED}
    assert json.loads(written)["items"] == [{"token": redaction.REDACTED, "id": 1}]
    (tmp_path / "later.aria.yml").write_text("- text: Tok0enInList2\n")
    assert redaction.scan(tmp_path, runner.redactor.values)[1] == ["later.aria.yml"]


def test_an_error_line_is_redacted_before_it_is_cut(tmp_path, execute_module):
    runner = _runner(execute_module, tmp_path)
    value = "Kept0Value9abcdefgh"
    runner.redactor.add(value)
    runner.redactor.add("two words kept here")
    line = runner._line("x" * 290 + value + "y" * 50)
    assert len(line) <= checks.OBSERVED_CHARS
    assert value[:7] not in line
    assert runner._line("refused: two\nwords   kept\there!") == "refused: (redacted)!"


# -- the sign-in limit --------------------------------------------------------
def _compose_journeys() -> List[Journey]:
    context = loader.context_for(REPO_ROOT)
    found = []
    for path in sorted((RUNNER_ROOT / "journeys").glob("*.yaml")):
        journey = loader.load(path, context)
        if journey.stack == "compose-dev":
            found.append(journey)
    return found


def sign_in_counts(journeys: List[Journey]) -> Dict[str, int]:
    """The sign-ins and password changes a session of these journeys sends.

    A click on the dashboard's "Sign in" button is one sign-in; the runner
    signs in once per caller its api steps name, for the whole session.
    """
    callers = set()
    sign_ins = changes = 0
    for journey in journeys:
        for step in journey.steps:
            click = step.do.click
            if click is not None and click.role == "button":
                sign_ins += click.name == "Sign in"
                changes += click.name == "Change password"
            if step.do.api is not None and step.do.api.as_ != "anonymous":
                callers.add(step.do.api.as_)
    return {"sign_ins": sign_ins + len(callers), "password_changes": changes}


def test_the_runner_paces_sign_ins_to_the_stacks_limit():
    """The compose journeys together sign in more often than the stack allows a
    minute, so the runner paces them (``docs_runner/pacer.py``) at exactly the
    stack's own limit; password changes have no pacer and stay under theirs."""
    from docs_runner import pacer

    from backend.app.middleware.rate_limiter import RATE_LIMIT_CONFIG

    limit, window = RATE_LIMIT_CONFIG["/api/v1/auth/login"]
    change_limit, _ = RATE_LIMIT_CONFIG["/api/v1/users/me/password"]
    assert (pacer.LIMIT, pacer.WINDOW_SECONDS) == (limit, float(window))
    counts = sign_in_counts(_compose_journeys())
    assert counts["password_changes"] <= change_limit, counts


def test_a_click_on_sign_in_is_counted():
    """The plant for the count above: one more Sign in click counts once more."""
    journeys = _compose_journeys()
    extra = (
        journeys[0]
        .steps[0]
        .model_copy(
            update={
                "do": journeys[0]
                .steps[0]
                .do.model_copy(update={"click": Named(role="button", name="Sign in")})
            }
        )
    )
    planted = [journeys[0].model_copy(update={"steps": [*journeys[0].steps, extra]})]
    counts = sign_in_counts(planted + journeys[1:])
    assert counts["sign_ins"] == sign_in_counts(journeys)["sign_ins"] + 1


# -- screenshots in a journey with secrets, and the plaintext key -------------
class _FakeLocator:
    def __init__(self, what):
        self.what = what

    def aria_snapshot(self):
        return "- main"


class _FakePage:
    def __init__(self):
        self.masks: Optional[list] = None

    def get_by_text(self, value):
        return _FakeLocator(("text", value))

    def locator(self, selector):
        return _FakeLocator(("locator", selector))

    def screenshot(self, path, mask):
        self.masks = [m.what for m in mask]
        Path(path).write_bytes(b"png")


FIELDS = ("locator", "input, textarea, [contenteditable]")


@pytest.mark.parametrize("secrets", [{}, {"pw": "S3cretValue9xyz"}])
def test_a_screenshot_masks_every_field_exactly_when_the_journey_has_secrets(
    tmp_path, execute_module, secrets
):
    from docs_runner.model import Step

    step = Step.model_validate(
        {
            "id": "screen",
            "doc": "x",
            "do": {"api": {"method": "GET", "path": "/me", "as": "anonymous"}},
            "fail": "x",
        }
    )
    runner = _runner(execute_module, tmp_path)
    runner.secrets = dict(secrets)
    page = _FakePage()
    runner._screen(page, "j", 1, step)
    assert page.masks is not None, "no screenshot was taken"
    assert (FIELDS in page.masks) == bool(secrets)
    for value in secrets.values():
        assert ("text", value) in page.masks


def test_the_plaintext_key_a_credential_route_answers_is_looked_for(
    tmp_path, execute_module
):
    from docs_runner import redaction
    from docs_runner.model import Step
    from docs_runner.stacks import Running

    step = Step.model_validate(
        {
            "id": "new-key",
            "doc": "x",
            "do": {
                "api": {"method": "POST", "path": "/api/v1/api-keys", "as": "anonymous"}
            },
            "expect": {"status": 201},
            "fail": "x",
        }
    )
    secret = "eptk_live_PLAINTEXT_SECRET_9999"
    runner = _runner(execute_module, tmp_path)
    running = Running(name="compose-dev", base_url="http://dash/", api_url="http://api")
    runner._api_step(
        step, running, _FakeApi(_FakeAnswer(201, {"key": secret, "name": "k"})), "j", 1
    )
    assert secret in runner.redactor.values
    (tmp_path / "later.aria.yml").write_text(f"- text: {secret}\n")
    assert redaction.scan(tmp_path, runner.redactor.values)[1] == ["later.aria.yml"]


# -- D3b: what a journey saves or keeps reaches no file; its checks can fail --
SAVED = "Sav0edSecretValue1abcd"


def _running():
    from docs_runner.stacks import Running

    return Running(name="compose-dev", base_url="http://dash/", api_url="http://api")


def _step_of(spec: Dict[str, Any]):
    from docs_runner.model import Step

    return Step.model_validate({"id": "s", "doc": "x", "fail": "x", **spec})


def _passing_expect(execute_module, monkeypatch):
    """Playwright's expect, as if every element the step waits for were there."""
    import types

    class _Expect:
        def __call__(self, target):
            return types.SimpleNamespace(to_have_count=lambda count: None)

        def set_options(self, **kwargs):
            return None

    monkeypatch.setattr(execute_module, "expect", _Expect())


def test_a_secret_saved_from_any_path_reaches_the_redactor_and_no_file(
    tmp_path, execute_module
):
    step = _step_of(
        {
            "do": {
                "api": {"method": "GET", "path": "/api/v1/things", "as": "anonymous"}
            },
            "expect": {"status": 200},
            "save": {"sdk-key": {"path": "data.value", "secret": True}},
        }
    )
    runner = _runner(execute_module, tmp_path)
    answer = _FakeAnswer(200, {"data": {"value": SAVED}})
    runner._api_step(step, _running(), _FakeApi(answer), "j", 1)
    assert runner.secrets["sdk-key"] == SAVED
    assert SAVED in runner.redactor.values
    written = (tmp_path / "j" / "01-s.api.json").read_text()
    assert SAVED not in written
    assert json.loads(written)["data.value"] == "(redacted)"
    # Playwright puts a request's headers in its error text: the key in one is
    # taken out of the line the log keeps.
    assert SAVED not in runner._line(f"x-api-key: {SAVED}")


def test_a_token_saved_for_a_later_path_is_never_written(tmp_path, execute_module):
    token = "Inv1teTokenValue2abcd"
    step = _step_of(
        {
            "do": {
                "api": {
                    "method": "POST",
                    "path": "/api/v1/workspaces/w/invites",
                    "as": "anonymous",
                }
            },
            "expect": {"status": 201},
            "save": {"invite-token": {"path": "token"}},
        }
    )
    runner = _runner(execute_module, tmp_path)
    answer = _FakeAnswer(201, {"token": token, "email": "dev@demo.com"})
    runner._api_step(step, _running(), _FakeApi(answer), "j", 1)
    assert runner.values["invite-token"] == token
    assert token in runner.redactor.values
    assert token not in runner._line(f"POST /invites/{token}/accept answered 500")


class _KeepElement:
    def __init__(self, text: str):
        self.text = text
        self.has_text = None

    def get_by_role(self, role, **kwargs):
        return self

    def filter(self, has_text=None, **kwargs):
        self.has_text = has_text
        return self

    def inner_text(self):
        return self.text


class _KeepPage:
    def __init__(self, text: str):
        self.element = _KeepElement(text)

    def get_by_role(self, role, **kwargs):
        return self.element


@pytest.mark.parametrize(
    "keep",
    [
        {"secret": "sdk-key", "role": "code", "prefix": "eptk_"},
        {
            "secret": "sdk-key",
            "role": "code",
            "within": {"role": "dialog", "name": "User created"},
        },
    ],
    ids=["by-prefix", "within-a-named-element"],
)
def test_a_kept_value_reaches_the_redactor(tmp_path, execute_module, monkeypatch, keep):
    _passing_expect(execute_module, monkeypatch)
    value = "eptk_" + "0123456789abcdef" * 2
    step = _step_of({"do": {"keep": keep}})
    runner = _runner(execute_module, tmp_path)
    page = _KeepPage(f"  {value}\n")
    runner._keep_step(step, page)
    assert runner.secrets["sdk-key"] == value
    assert value in runner.redactor.values
    assert value not in runner._line(f"could not read: {value}")
    if "prefix" in keep:
        assert page.element.has_text.pattern == "^eptk_"


def test_every_file_the_runner_writes_has_kept_values_taken_out(
    tmp_path, execute_module
):
    runner = _runner(execute_module, tmp_path)
    runner.redactor.add(SAVED)
    name = runner._write("j/01-x.aria.yml", f"- code: {SAVED}\n")
    text = (tmp_path / name).read_text()
    assert SAVED not in text
    assert "(redacted)" in text


FISHER_ARGS = {
    "control_users": 400,
    "control_converted": 40,
    "treatment_users": 400,
    "treatment_converted": 64,
}


def _results_step():
    return _step_of(
        {
            "do": {
                "api": {
                    "method": "GET",
                    "path": "/api/v1/results/{{experiment-id}}",
                    "as": "anonymous",
                }
            },
            "expect": {
                "status": 200,
                "computed": {
                    "metrics.0.variants.1.p_value": {
                        "oracle": {"name": "fisher_exact_p", "args": FISHER_ARGS},
                        "rel": 1e-9,
                    }
                },
            },
        }
    )


def _results(p_value: float) -> _FakeAnswer:
    return _FakeAnswer(200, {"metrics": [{"variants": [{}, {"p_value": p_value}]}]})


def test_a_saved_value_is_put_into_the_path(tmp_path, execute_module):
    from docs_runner.oracles import fisher_exact_p

    runner = _runner(execute_module, tmp_path)
    runner.values = {"experiment-id": "e1"}
    api = _FakeApi(_results(fisher_exact_p(**FISHER_ARGS)))
    runner._api_step(_results_step(), _running(), api, "j", 1)
    assert api.calls[0][1] == "/api/v1/results/e1"


def test_a_computed_number_holds_within_its_tolerance_and_fails_outside(
    tmp_path, execute_module
):
    from docs_runner.oracles import fisher_exact_p

    p_value = fisher_exact_p(**FISHER_ARGS)
    runner = _runner(execute_module, tmp_path)
    runner.values = {"experiment-id": "e1"}
    observed, _ = runner._api_step(
        _results_step(), _running(), _FakeApi(_results(p_value * (1 + 5e-10))), "j", 1
    )
    assert observed == "status 200"
    with pytest.raises(execute_module._Failed) as failed:
        runner._api_step(
            _results_step(),
            _running(),
            _FakeApi(_results(p_value * (1 + 2e-9))),
            "j",
            2,
        )
    assert "fisher_exact_p gives" in failed.value.observed
    written = json.loads((tmp_path / "j" / "02-s.api.json").read_text())
    assert written["metrics.0.variants.1.p_value"]["oracle"] == p_value


class _TableChain:
    def __init__(self, page):
        self.page = page

    def get_by_role(self, role, **kwargs):
        return self

    def filter(self, **kwargs):
        return self

    def evaluate(self, script, argument=None):
        return 5 if "cellIndex" in script else self.page.cell_text


class _TablePage:
    def __init__(self, cell_text: str):
        self.cell_text = cell_text
        self.url = "http://dash/results/e1"

    def goto(self, url):
        return None

    def evaluate(self, script):
        return "/results/e1"

    def get_by_role(self, role, **kwargs):
        return _TableChain(self)


def _cells_step():
    return _step_of(
        {
            "do": {"goto": "/results/e1"},
            "expect": {
                "cells": [
                    {
                        "row": "Treatment",
                        "column": "p-value",
                        "oracle": {"name": "fisher_exact_p", "args": FISHER_ARGS},
                    }
                ]
            },
        }
    )


def test_a_cell_shows_its_oracle_at_the_precision_shown_or_the_step_fails(
    tmp_path, execute_module, monkeypatch
):
    from docs_runner.checks import StepFailed

    _passing_expect(execute_module, monkeypatch)
    runner = _runner(execute_module, tmp_path)
    observed = runner._browser_step(_cells_step(), _TablePage("0.0153"), _running())
    assert "p-value 0.0153, each its oracle's" in observed
    with pytest.raises(StepFailed, match="shows '0.0156'; fisher_exact_p gives"):
        runner._browser_step(_cells_step(), _TablePage("0.0156"), _running())


class _TrafficApi:
    """The experiment read, and the tracking API assigning by the documented hash."""

    def __init__(self, wrong: Optional[str] = None):
        self.wrong = wrong
        self.allocations = [("Control", 50), ("Treatment", 50)]

    def get(self, path, headers):
        variants = [{"name": n, "traffic_allocation": a} for n, a in self.allocations]
        return _FakeAnswer(200, {"key": "exp_key", "variants": variants})

    def fetch(self, path, method, headers, data):
        from docs_runner import traffic

        if path.endswith("/assign"):
            user = data["user_id"]
            name = traffic.variant_for(user, "exp_key", self.allocations)
            if user == self.wrong:
                name = "Control" if name == "Treatment" else "Treatment"
            return _FakeAnswer(200, {"variant_name": name, "assigned": True})
        return _FakeAnswer(200, {"event_type": data["event_type"]})


def _traffic_step():
    return _step_of(
        {
            "do": {
                "traffic": {
                    "experiment": "experiment-id",
                    "key": "sdk-key",
                    "as": "admin",
                    "event": "checkout_completed",
                    "users": "u",
                    "variants": {
                        "Control": {"assigned": 5, "converted": 1},
                        "Treatment": {"assigned": 5, "converted": 2},
                    },
                }
            }
        }
    )


def test_a_traffic_step_fails_on_an_assignment_other_than_chosen(
    tmp_path, execute_module
):
    from docs_runner import traffic

    runner = _runner(execute_module, tmp_path)
    runner.values = {"experiment-id": "e1"}
    runner.secrets = {"sdk-key": SAVED}
    runner.tokens[("http://api", "admin")] = "already-signed-in"
    observed, _ = runner._traffic_step(
        _traffic_step(), _running(), _TrafficApi(), "j", 1
    )
    assert observed.startswith("13 requests: Control 5 assigned as chosen, 1 converted")
    chosen = traffic.choose(
        "exp_key", [("Control", 50), ("Treatment", 50)], {"Treatment": 5}, "u"
    )
    with pytest.raises(execute_module._Failed) as failed:
        runner._traffic_step(
            _traffic_step(),
            _running(),
            _TrafficApi(wrong=chosen["Treatment"][0]),
            "j",
            2,
        )
    assert "the documented hash puts it in 'Treatment'" in failed.value.observed
    assert SAVED not in (tmp_path / "j" / "02-s.traffic.json").read_text()


#: Two journeys, each with a walkthrough; one walkthrough's own bytes, or one
#: of its journey's files, hold a value the run kept.
WALKTHROUGH_RUN = (
    "import conftest\n"
    "from docs_runner import log, report\n\n\n"
    "def test_planted(request):\n"
    "    run_dir = conftest.run_dir_of(request.config)\n"
    f'    request.config.stash[conftest.SECRET_VALUES].add("{PLANT}")\n'
    '    (run_dir / "recordings").mkdir()\n'
    '    (run_dir / "sample").mkdir()\n'
    "    PLANTED\n"
    '    (run_dir / "recordings" / "R2-other.webm").write_bytes(b"webm")\n'
    '    (run_dir / "recordings" / "R1-sample-two.webm").write_bytes(b"webm")\n'
    "    request.config.stash[conftest.RECORDED].update(\n"
    "        {\n"
    '            "R1-sample": {"journey": "sample", "seconds": 1.0, "dropped": "",\n'
    '                          "file": "recordings/R1-sample.webm"},\n'
    '            "R1-sample-two": {"journey": "sample", "seconds": 1.0,\n'
    '                              "dropped": "",\n'
    '                              "file": "recordings/R1-sample-two.webm"},\n'
    '            "R2-other": {"journey": "other", "seconds": 1.0, "dropped": "",\n'
    '                         "file": "recordings/R2-other.webm"},\n'
    "        }\n"
    "    )\n"
    '    for name in ("sample", "other"):\n'
    "        record = log.Record(\n"
    '            run="r", sha="s", stack="compose-dev", guide="README.md", step=1,\n'
    '            step_id="home", heading="Home", action="open /", expected="x",\n'
    '            failure_signature="x", result="PASS", reason="", observed="at /",\n'
    '            snapshot="", expected_snapshot="structural only", ms=1,\n'
    "        )\n"
    "        request.config.stash[conftest.RUNS].append(\n"
    "            report.GuideRun(\n"
    '                journey=name, guide="README.md", title="Home",\n'
    '                stack="compose-dev", records=(record,),\n'
    "            )\n"
    "        )\n"
)


@pytest.mark.parametrize(
    "planted, removed, screens",
    [
        pytest.param(
            f'(run_dir / "recordings" / "R1-sample.webm").write_bytes(b"webm {PLANT}")',
            ["recordings/R1-sample.webm"],
            ["recordings/R1-sample-two.webm"],
            id="the-walkthrough-holds-it",
        ),
        pytest.param(
            '(run_dir / "recordings" / "R1-sample.webm").write_bytes(b"webm"); '
            '(run_dir / "sample" / "03-x.aria.yml").write_text('
            f'"- code: {PLANT}")',
            ["sample/03-x.aria.yml"],
            ["recordings/R1-sample-two.webm", "recordings/R1-sample.webm"],
            id="a-file-of-its-journey-holds-it",
        ),
    ],
)
def test_a_walkthrough_is_its_own_journeys_in_the_end_of_run_scan(
    tmp_path, planted, removed, screens
):
    """conftest passes each walkthrough's owner to the scan: only the journey
    that recorded it fails, and only that journey's walkthroughs (two here)
    go."""
    from docs_runner import redaction

    body = WALKTHROUGH_RUN.replace("PLANTED", planted)
    result = _run_runner(_runner_copy(tmp_path, body), tmp_path)
    assert result.returncode == 1, result.stdout + result.stderr
    # Both passes of the scan ran to the end (a hook that raised would leave
    # the first pass's record and still exit 1).
    assert "Traceback" not in result.stdout + result.stderr
    assert "docs journeys: 2 run, 1 failed" in result.stdout
    run = tmp_path / "run"
    words = {
        name: entry["word"]
        for name, entry in json.loads((run / "verdicts.json").read_text())[
            "journeys"
        ].items()
    }
    assert words == {"sample": "FAIL", "other": "PASS"}
    record = json.loads((run / redaction.SCAN_RECORD).read_text())
    assert record["removed"] == removed
    assert record["screens_removed"] == screens
    assert not (run / "recordings" / "R1-sample.webm").exists()
    assert not (run / "recordings" / "R1-sample-two.webm").exists()
    assert (run / "recordings" / "R2-other.webm").is_file()
