"""demo-shoplab's products step checks the arm the API gave the visitor (#1375).

The products sort (``shoplab_plp_sort``) is a Thompson-sampling bandit, and the
tracking API routes a new visitor by the bandit's current weights, so the
journey's fresh visitor can land in any arm: on 2026-10-22 and 10-23 it landed
in ``relevance``, the page said "Sorted by Relevance", and a step that expected
"Recommended for you" failed. A fixed visitor id would not help, because the
weights move.

So the step lists one ARIA snapshot per arm (``expect.aria_one_of``), each
pairing the arm the storefront's panel shows (the ``variant_name`` the API
answered) with that arm's sort label, and the screen must match one of them.
A label paired with the wrong arm matches none, whichever arm the visitor got.

A visitor in the demo stack's 2% global holdout is in no arm: the API answers
the control without enrolling them, the list is sorted by the control's label,
and the panel's row says so instead of naming an arm. Once the panel stopped
showing the control's name for that visitor, the step failed for one, so it
has a fourth snapshot for them.

Held here:

* the journey: one template per arm of the seed's bandit, each with the label
  ``demo/shoplab/src/lib/sorting.ts`` gives that arm's algorithm (a rotated
  label, a missing arm and a repeated arm are each refused, below), and one
  for a held-out visitor: the control's label with the row the panel draws
  for them, from ``demo/shoplab/src/lib/labels.ts`` in the order
  ``ControlvarPanel.tsx`` renders it (another arm's label, changed panel
  words and a missing template are each refused);
* the model and the loader: what ``aria_one_of`` accepts and refuses;
* the runner: it passes on the template the screen matches and says which,
  tries again until the step's timeout, and fails naming none; the expected
  snapshot file holds every template.
"""

from __future__ import annotations

import re
import sys
import types
from pathlib import Path
from typing import Dict, List, Optional

import pytest
import yaml
from pydantic import ValidationError

REPO_ROOT = Path(__file__).resolve().parents[4]
RUNNER_ROOT = REPO_ROOT / "tests" / "acceptance" / "docs"
if str(RUNNER_ROOT) not in sys.path:
    sys.path.insert(0, str(RUNNER_ROOT))

from docs_runner import checks, loader, log
from docs_runner.model import Expect, Step

from backend.scripts import seed_shoplab

JOURNEY = RUNNER_ROOT / "journeys" / "demo-shoplab.yaml"
SORTING = REPO_ROOT / "demo" / "shoplab" / "src" / "lib" / "sorting.ts"
LABELS = REPO_ROOT / "demo" / "shoplab" / "src" / "lib" / "labels.ts"
PANEL_SOURCE = (
    REPO_ROOT / "demo" / "shoplab" / "src" / "components" / "ControlvarPanel.tsx"
)
BANDIT = "shoplab_plp_sort"
PANEL = 'complementary "Controlvar panel"'
LABEL = re.compile(r"^Sorted by (.+)$")
#: How a problem with the held-out visitor's template names it.
HELD_OUT = "a visitor in the global holdout"


def _template(arm: str, label: str) -> str:
    """The products step's template for one arm, as the journey writes it."""
    return yaml.safe_dump(
        [
            {
                "main": [
                    'heading "All products" [level=1]',
                    {"paragraph": "12 items"},
                    {"paragraph": f"Sorted by {label}"},
                ]
            },
            {
                PANEL: [
                    {
                        "list": [
                            {
                                "listitem": [
                                    {"text": "Product sort (bandit)"},
                                    {"code": BANDIT},
                                    {"text": arm},
                                ]
                            }
                        ]
                    }
                ]
            },
        ],
        sort_keys=False,
    )


# ---------------------------------------------------------------------------
# The journey
# ---------------------------------------------------------------------------
def _products() -> Dict:
    steps = yaml.safe_load(JOURNEY.read_text(encoding="utf-8"))["steps"]
    return next(step for step in steps if step["id"] == "products")


def _variants() -> List[Dict]:
    spec = next(e for e in seed_shoplab.EXPERIMENTS if e["key"] == BANDIT)
    return spec["variants"]


def _sort_label(variant: Dict) -> str:
    """The label the storefront shows for *variant*'s algorithm (``SORT_LABELS``)."""
    block = SORTING.read_text(encoding="utf-8").split("SORT_LABELS", 1)[1]
    labels = dict(re.findall(r"^\s+(\w+): '([^']+)',$", block.split("};")[0], re.M))
    return labels[variant["configuration"]["algorithm"]]


def _expected() -> Dict[str, str]:
    """Each arm of the seeded bandit, with the label the storefront shows for it."""
    return {v["name"]: _sort_label(v) for v in _variants()}


def _control_label() -> str:
    """The list's label for a visitor the API did not enrol: the API answers the
    control's variant and configuration, and the page sorts by its algorithm."""
    (control,) = [v for v in _variants() if v["is_control"]]
    return _sort_label(control)


def _found(pattern: str, text: str, where: Path) -> str:
    match = re.search(pattern, text)
    assert match, f"{where.name} no longer has {pattern!r}"
    return match.group(1)


def _holdout_row() -> str:
    """The sort row's text after its key for a visitor in the global holdout.

    ``assignmentNote`` gives the line under the key for reason ``holdout`` and
    ``assignmentPill`` the pill for an answer that did not enrol the visitor
    (``labels.ts``); the panel draws both in the row, in the order its source
    renders them, and the snapshot reads them as one text joined by a space."""
    labels = LABELS.read_text(encoding="utf-8")
    note = _found(r"case 'holdout':\s*return '([^']+)';", labels, LABELS)
    pill = _found(
        r"if \(answer\.assigned === false\) return [^;]*: '([^']+)';", labels, LABELS
    )
    panel = PANEL_SOURCE.read_text(encoding="utf-8")
    drawn = [(panel.find("{note}"), note), (panel.find("{assignmentPill(a)}"), pill)]
    assert all(at >= 0 for at, _ in drawn), (
        "the panel no longer draws the note and pill"
    )
    return " ".join(text for _, text in sorted(drawn))


def _pair(template: str) -> tuple:
    """(the arm the panel shows, the label the list shows) in one template."""
    nodes = yaml.safe_load(template)
    main = next(n["main"] for n in nodes if isinstance(n, dict) and "main" in n)
    panel = next(n[PANEL] for n in nodes if isinstance(n, dict) and PANEL in n)
    paragraphs = [
        c["paragraph"] for c in main if isinstance(c, dict) and "paragraph" in c
    ]
    labels = [m.group(1) for p in paragraphs if (m := LABEL.match(p))]
    assert len(labels) == 1, paragraphs
    item = panel[0]["list"][0]["listitem"]
    assert item[:2] == [{"text": "Product sort (bandit)"}, {"code": BANDIT}], item
    return item[2]["text"], labels[0]


def _problems(templates: List[str]) -> List[str]:
    """What is wrong with the step's templates against the seed and the storefront."""
    found = []
    expected = _expected()
    pairs = [_pair(t) for t in templates]
    arms = [arm for arm, _ in pairs if arm in expected]
    others = [pair for pair in pairs if pair[0] not in expected]
    if sorted(arms) != sorted(expected):
        found.append(
            f"the templates' arms {arms} are not the bandit's {sorted(expected)}"
        )
    for arm, label in pairs:
        if arm in expected and label != expected[arm]:
            found.append(f"{arm} is paired with {label!r}, not {expected[arm]!r}")
    held_out = [(_holdout_row(), _control_label())]
    if others != held_out:
        found.append(
            f"the templates for no arm {others} are not {HELD_OUT}'s {held_out}"
        )
    return found


def test_the_seed_and_the_storefront_give_three_arms_and_three_labels():
    """The pairing the step must hold, read from its two sources (not typed here)."""
    assert _expected() == {
        "relevance": "Relevance",
        "price_low_high": "Price: low to high",
        "ml_personalized": "Recommended for you",
    }


def test_the_storefront_shows_a_held_out_visitor_the_control_and_says_why():
    """What the readers above take from the sources, as a held-out visitor's
    screen showed it in a journey run (the step's snapshot is checked against
    the readers, not against these words)."""
    assert _control_label() == "Relevance"
    assert _holdout_row() == "in the global holdout (control shown) not enrolled"


@pytest.mark.regression
def test_the_products_step_pairs_each_arm_with_its_own_label():
    """#1375: the step expected one arm's label and failed for a visitor in another."""
    step = _products()
    assert set(step["expect"]) == {"aria_one_of"}, step["expect"]
    templates = step["expect"]["aria_one_of"]
    assert _problems(templates) == []
    assert len(templates) == len(_expected()) + 1


@pytest.mark.regression
def test_the_products_step_expects_a_visitor_in_the_global_holdout():
    """The step had a snapshot per arm only, and failed for a held-out visitor:
    the list said "Sorted by Relevance" and the panel's row said the visitor was
    in the global holdout, not enrolled, which no template expected."""
    templates = _products()["expect"]["aria_one_of"]
    others = [pair for pair in map(_pair, templates) if pair[0] not in _expected()]
    assert others == [(_holdout_row(), _control_label())]


def _arms(templates: List[str]) -> List[str]:
    return [t for t in templates if _pair(t)[0] in _expected()]


def _held_out(templates: List[str]) -> List[str]:
    return [t for t in templates if _pair(t)[0] not in _expected()]


def _rotated(templates: List[str]) -> List[str]:
    pairs = [_pair(t) for t in _arms(templates)]
    labels = [label for _, label in pairs]
    return [
        _template(arm, labels[(i + 1) % len(labels)])
        for i, (arm, _) in enumerate(pairs)
    ] + _held_out(templates)


def _held_out_with_another_arms_label(templates: List[str]) -> List[str]:
    other = next(v for v in _expected().values() if v != _control_label())
    return _arms(templates) + [_template(_holdout_row(), other)]


def _arm_twice(templates: List[str]) -> List[str]:
    arms = _arms(templates)
    twice = _template(_pair(arms[0])[0], _pair(arms[-1])[1])
    return [*arms[:-1], twice] + _held_out(templates)


@pytest.mark.parametrize(
    "plant, message",
    [
        pytest.param(_rotated, "is paired with", id="labels-rotated"),
        pytest.param(
            lambda t: _arms(t)[1:] + _held_out(t),
            "are not the bandit's",
            id="arm-missing",
        ),
        pytest.param(_arm_twice, "are not the bandit's", id="arm-twice"),
        pytest.param(_arms, HELD_OUT, id="held-out-missing"),
        pytest.param(
            _held_out_with_another_arms_label, HELD_OUT, id="held-out-another-label"
        ),
    ],
)
def test_a_planted_pairing_is_refused(plant, message):
    planted = plant(_products()["expect"]["aria_one_of"])
    problems = _problems(planted)
    assert any(message in p for p in problems), problems


def _plant_source(name: str, old: str, new: str, monkeypatch, tmp_path) -> None:
    """Point the reader of source *name* at a copy with *old* replaced by *new*."""
    original = globals()[name]
    text = original.read_text(encoding="utf-8")
    assert text.count(old) == 1, old
    planted = tmp_path / original.name
    planted.write_text(text.replace(old, new), encoding="utf-8")
    monkeypatch.setitem(globals(), name, planted)


@pytest.mark.parametrize(
    "name, old, new",
    [
        pytest.param(
            "LABELS",
            "'in the global holdout (control shown)'",
            "'held out of every experiment (control shown)'",
            id="note-reworded",
        ),
        pytest.param(
            "LABELS", ": 'not enrolled';", ": 'held out';", id="pill-reworded"
        ),
        pytest.param(
            "PANEL_SOURCE",
            "{note && (",
            "{assignmentPill(a)}{note && (",
            id="pill-drawn-first",
        ),
    ],
)
def test_the_held_out_snapshot_follows_the_panel_source(
    name, old, new, monkeypatch, tmp_path
):
    """The panel drawing a held-out visitor's row differently fails the pin."""
    _plant_source(name, old, new, monkeypatch, tmp_path)
    problems = _problems(_products()["expect"]["aria_one_of"])
    assert any(HELD_OUT in p for p in problems), problems


def test_an_edit_elsewhere_in_the_panel_source_keeps_the_pin(monkeypatch, tmp_path):
    """Positive control for the plants above: a copy that differs only in words a
    held-out visitor's sort row does not show reads as the step expects."""
    _plant_source(
        "LABELS",
        "'excluded: targeting rules (control shown)'",
        "'excluded: targeting (control shown)'",
        monkeypatch,
        tmp_path,
    )
    _plant_source(
        "PANEL_SOURCE",
        "'Free-shipping banner'",
        "'Free shipping'",
        monkeypatch,
        tmp_path,
    )
    assert _problems(_products()["expect"]["aria_one_of"]) == []


def test_the_journey_loads_with_the_step_as_written():
    journey = loader.load(JOURNEY, loader.context_for(REPO_ROOT))
    step = next(s for s in journey.steps if s.id == "products")
    assert (
        checks.describe_expect(step) == "the screen matches one of its 4 ARIA snapshots"
    )


# ---------------------------------------------------------------------------
# The model and the loader
# ---------------------------------------------------------------------------
A, B, C = (_template(arm, "x") for arm in ("a", "b", "c"))


@pytest.mark.parametrize(
    "expect, message",
    [
        pytest.param({"aria_one_of": [A]}, "at least 2 items", id="one-template"),
        pytest.param(
            {"aria_one_of": [A, B, C, _template("d", "x"), _template("e", "x")]},
            "at most 4 items",
            id="five-templates",
        ),
        pytest.param(
            {"aria_one_of": [A, "just text"]},
            "aria must be an ARIA snapshot template",
            id="not-a-template",
        ),
        pytest.param({"aria_one_of": [A, A]}, "gives one template twice", id="twice"),
        pytest.param(
            {"aria": A, "aria_one_of": [A, B]},
            "give aria or aria_one_of, not both",
            id="with-aria",
        ),
    ],
)
def test_aria_one_of_refuses(expect, message):
    with pytest.raises(ValidationError, match=message):
        Expect.model_validate(expect)


def _screen_step(expect: Dict, **more) -> Step:
    return Step.model_validate(
        {"id": "s", "doc": "x", "fail": "x", "do": {"goto": "/products"}, **more}
        | {"expect": expect}
    )


def test_a_screen_step_with_aria_one_of_has_its_snapshot():
    problems = loader._expect_problems(
        "step 1 (s)", _screen_step({"aria_one_of": [A, B]}), "demo-local"
    )
    assert problems == []
    # Positive control: the same step with no snapshot at all is refused.
    problems = loader._expect_problems(
        "step 1 (s)", _screen_step({"url": "/products"}), "demo-local"
    )
    assert any("needs expect.aria" in p for p in problems), problems


def test_snapshot_false_refuses_aria_one_of():
    step = _screen_step(
        {"url": "/products", "aria_one_of": [A, B]},
        snapshot=False,
        snapshot_reason="x",
    )
    problems = loader._expect_problems("step 1 (s)", step, "demo-local")
    assert any("expect.aria_one_of gives one" in p for p in problems), problems


# ---------------------------------------------------------------------------
# The runner (Playwright stood in for, as the unit job lacks it)
# ---------------------------------------------------------------------------
@pytest.fixture
def execute_module(monkeypatch):
    import importlib

    sync_api = types.ModuleType("playwright.sync_api")
    sync_api.Error = type("Error", (Exception,), {})
    sync_api.Browser = sync_api.Page = sync_api.Playwright = object
    sync_api.expect = types.SimpleNamespace(set_options=lambda **kwargs: None)
    package = types.ModuleType("playwright")
    package.sync_api = sync_api
    monkeypatch.setitem(sys.modules, "playwright", package)
    monkeypatch.setitem(sys.modules, "playwright.sync_api", sync_api)
    monkeypatch.delitem(sys.modules, "docs_runner.execute", raising=False)
    module = importlib.import_module("docs_runner.execute")
    yield module
    sys.modules.pop("docs_runner.execute", None)


class _Expect:
    """Playwright's expect: the screen matches *template* from try *after* on."""

    def __init__(self, template: Optional[str], after: int = 0):
        self.template, self.after = template, after
        self.tries: List[tuple] = []

    def __call__(self, target):
        def to_match_aria_snapshot(template, timeout=None):
            self.tries.append((target, template, timeout))
            if template != self.template or len(self.tries) <= self.after:
                raise AssertionError("no match")

        return types.SimpleNamespace(to_match_aria_snapshot=to_match_aria_snapshot)

    def set_options(self, **kwargs):
        return None


class _Page:
    url = "http://localhost:3200/products"

    def goto(self, url):
        return None

    def evaluate(self, script):
        return "/products"

    def locator(self, selector):
        return ("locator", selector)


def _run(execute_module, monkeypatch, tmp_path, fake: _Expect, timeout_ms: int):
    from docs_runner.stacks import Running

    monkeypatch.setattr(execute_module, "expect", fake)
    runner = execute_module.JourneyRunner(
        None,
        None,
        log.Log(tmp_path / "results.jsonl"),
        execute_module.Settings(
            run_dir=tmp_path, run_id="r", sha="s", timeout_ms=timeout_ms
        ),
    )
    step = _screen_step({"aria_one_of": [A, B, C]})
    running = Running(name="demo-local", base_url="http://dash/", api_url="http://api")
    return runner._browser_step(step, _Page(), running)


def test_the_runner_passes_on_the_template_the_screen_matches_and_names_it(
    execute_module, monkeypatch, tmp_path
):
    fake = _Expect(B)
    observed = _run(execute_module, monkeypatch, tmp_path, fake, 10_000)
    assert observed.startswith("at /products (its ARIA snapshot 2 of 3)"), observed
    assert [t[1] for t in fake.tries] == [A, B]
    assert {t[0] for t in fake.tries} == {("locator", "body")}
    assert {t[2] for t in fake.tries} == {execute_module.ONE_OF_MS}


def test_the_runner_tries_again_until_the_screen_settles(
    execute_module, monkeypatch, tmp_path
):
    """The panel says "loading…" before the API answers: a later round matches."""
    fake = _Expect(C, after=7)
    observed = _run(execute_module, monkeypatch, tmp_path, fake, 10_000)
    assert "its ARIA snapshot 3 of 3" in observed
    assert [t[1] for t in fake.tries] == [A, B, C] * 3


def test_the_runner_fails_when_the_screen_matches_none(
    execute_module, monkeypatch, tmp_path
):
    from docs_runner.checks import StepFailed

    fake = _Expect(None)
    with pytest.raises(StepFailed, match="matches none of its 3 ARIA snapshots"):
        _run(execute_module, monkeypatch, tmp_path, fake, 1)
    assert [t[1] for t in fake.tries][:3] == [A, B, C]


def test_the_expected_snapshot_file_holds_every_template(execute_module, tmp_path):
    from docs_runner.stacks import Running

    runner = execute_module.JourneyRunner(
        None,
        None,
        log.Log(tmp_path / "results.jsonl"),
        execute_module.Settings(run_dir=tmp_path, run_id="r", sha="s"),
    )
    journey = types.SimpleNamespace(stack="demo-local", guide="demo/shoplab/README.md")
    running = Running(name="demo-local", base_url="http://dash/", api_url="http://api")
    step = _screen_step({"aria_one_of": [A, B, C]})
    record = runner._step(journey, "j", 3, step, running, None, None, None, {}, True)
    assert record.expected_snapshot == "j/03-s.expected.aria.yml"
    written = (tmp_path / record.expected_snapshot).read_text(encoding="utf-8")
    assert written.split("\n# or\n") == [
        A.rstrip("\n"),
        B.rstrip("\n"),
        C.rstrip("\n") + "\n",
    ]
