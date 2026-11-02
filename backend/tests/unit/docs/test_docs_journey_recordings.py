"""The launch walkthroughs R1-R8: the registry, the journeys that record them,
and what keeps a credential off them (QA plan UX D11.4, V12 and V13).

``tests/acceptance/docs/recordings.toml`` names each walkthrough a counting
docs-journeys run must keep (T138: "recordings R1-R8 in that run's
artifacts"); the journeys under ``journeys/`` declare which of them they
record and over which steps; the runner records them with ``page.screencast``
(``execute.Walkthrough``), checks every page it records for the values the run
keeps (``redaction.on_screen``), and reports each with its guide's verdict
(``report.walkthrough_rows``). The presence check that fails a run missing one
is ``scripts/docs_journeys_report.py recordings``
(``backend/tests/unit/scripts/test_docs_journeys_recordings.py``).

Pure parts only, as in ``test_docs_journey_runner.py``: no browser, no network,
temporary files.
"""

from __future__ import annotations

import datetime
import sys
import tomllib
from pathlib import Path
from typing import Any, Dict, List

import pytest
import yaml

pytestmark = [pytest.mark.unit]

REPO_ROOT = Path(__file__).resolve().parents[4]
RUNNER_ROOT = REPO_ROOT / "tests" / "acceptance" / "docs"
if str(RUNNER_ROOT) not in sys.path:
    sys.path.insert(0, str(RUNNER_ROOT))

from docs_runner import loader, redaction, report
from docs_runner.log import Record
from docs_runner.model import Journey

REGISTRY_PATH = RUNNER_ROOT / "recordings.toml"
JOURNEYS = RUNNER_ROOT / "journeys"
TODAY = datetime.date(2026, 10, 7)

#: QA plan UX D11.4: each walkthrough, its guide and its longest length. R5 is
#: two halves of a minute each (its two minutes), two segments of the
#: user-guide journey (the sample-ratio half on NEW-2's dataset, EM v1.3
#: condition 3).
EXPECTED = {
    "R1-quick-start": ("R1", "getting-started/quick-start.md", 180),
    "R2-docker-guide": ("R2", "getting-started/docker-guide.md", 90),
    "R3-experiment-wizard": ("R3", "guides/experiment-wizard.md", 120),
    "R4-power-analysis": ("R4", "statistics/power-analysis.md", 60),
    "R5-reading-results": ("R5", "guides/user-guide.md", 60),
    "R5-srm-warning": ("R5", "guides/user-guide.md", 60),
    "R6-feature-flag": ("R6", "feature-flags/create.md", 120),
    "R7-docs-site": ("R7", "README.md", 60),
    "R8-onboarding": ("R8", "auth/auth-user-guide.md", 90),
}
PENDING: set = set()


def registry() -> Dict[str, Dict[str, Any]]:
    return tomllib.loads(REGISTRY_PATH.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# The registry
# ---------------------------------------------------------------------------
def test_the_registry_is_the_eight_walkthroughs():
    found = registry()
    assert {
        name: (e["walkthrough"], e["guide"], e["max_seconds"])
        for name, e in found.items()
    } == EXPECTED
    assert {name for name, e in found.items() if e.get("pending")} == PENDING
    for name, entry in found.items():
        assert set(entry) <= {"walkthrough", "guide", "max_seconds", "shows", "pending"}
        assert entry["shows"].strip(), f"{name} says nothing about what it shows"
        assert name.startswith(entry["walkthrough"] + "-")


def test_the_registry_and_the_redaction_notes_name_the_journey_field():
    """Both say where a journey names its walkthroughs: the ``recordings``
    list of the journey file (``Journey.recordings``), not ``recording``."""
    assert "recordings" in Journey.model_fields
    assert "recording" not in Journey.model_fields
    header = "\n".join(
        line
        for line in REGISTRY_PATH.read_text(encoding="utf-8").splitlines()
        if line.startswith("#")
    )
    assert "in its `recordings` list" in header
    assert "`recording." not in header
    notes = redaction.__doc__ or ""
    assert "(``recordings``)" in notes
    assert "(``recording``)" not in notes


def test_every_walkthrough_guide_is_a_journey_in_the_inventory():
    pages = loader.read_inventory(RUNNER_ROOT / "inventory.toml")["pages"]
    for name, entry in registry().items():
        page = pages.get(entry["guide"])
        assert page is not None, f"{name}: {entry['guide']} is not a nav page"
        assert page.get("class") == "journey", f"{name}: {entry['guide']}"


def test_the_presence_check_asks_for_the_same_walkthroughs():
    scripts = REPO_ROOT / "scripts"
    sys.path.insert(0, str(scripts))
    try:
        import docs_journeys_report as check
    finally:
        sys.path.remove(str(scripts))
    assert check.WALKTHROUGHS == tuple(f"R{n}" for n in range(1, 9))
    assert check.RECORDINGS_TOML == REGISTRY_PATH
    assert check.FRAME == (1280, 720)


# ---------------------------------------------------------------------------
# The real journeys record exactly the registry's walkthroughs
# ---------------------------------------------------------------------------
def _real_journeys() -> Dict[str, Journey]:
    context = loader.context_for(REPO_ROOT)
    return {path.stem: loader.load(path, context) for path in JOURNEYS.glob("*.yaml")}


def test_each_walkthrough_is_recorded_by_exactly_one_journey_of_its_guide():
    declared: Dict[str, List[str]] = {}
    journeys = _real_journeys()
    for stem, journey in journeys.items():
        for recording in journey.recordings:
            declared.setdefault(recording.name, []).append(stem)
    entries = registry()
    required = {name for name, e in entries.items() if not e.get("pending")}
    assert set(declared) == required, (
        "a walkthrough no journey records, or one too many"
    )
    for name, stems in declared.items():
        assert len(stems) == 1, f"{name} is recorded by {stems}"
        assert journeys[stems[0]].guide == entries[name]["guide"]


def test_the_walkthrough_segments_are_the_ones_planned():
    journeys = _real_journeys()
    segments = {
        recording.name: (j.steps[first].id, j.steps[last].id)
        for j in journeys.values()
        for recording, first, last in j.segments
    }
    whole = {
        recording.name
        for j in journeys.values()
        for recording, first, last in j.segments
        if (first, last) == (0, len(j.steps) - 1)
    }
    # R7 stops before the crawls; R8 starts after the last one-time password;
    # R5's A/B-lift half ends at the results API, before the sample-ratio and
    # minimum-sample experiments, and its sample-ratio half is that
    # experiment's results page (its population arrives through the API).
    assert segments["R7-docs-site"] == ("home", "api-page")
    assert segments["R8-onboarding"] == ("demo-admin-log-out", "viewer-new-accepted")
    assert segments["R5-reading-results"] == ("sign-in-page", "results-api")
    assert segments["R5-srm-warning"] == ("ratio-results-page", "ratio-results-page")
    # R6 is the boolean flag, from sign-in to its users answered off; the
    # string flag the journey makes after it (#1497) is not in the walkthrough.
    assert segments["R6-feature-flag"] == ("sign-in-page", "off-users")
    assert whole == {
        "R1-quick-start",
        "R2-docker-guide",
        "R3-experiment-wizard",
        "R4-power-analysis",
    }


def test_no_journey_is_recorded_whole_and_for_a_walkthrough():
    for stem, journey in _real_journeys().items():
        assert not (journey.video and journey.recordings), stem


# ---------------------------------------------------------------------------
# The loader's refusals, each on one planted defect
# ---------------------------------------------------------------------------
GUIDE = """# Sample guide

## Sign in

Open the dashboard and click **Sign in**. Fill **Email** and **Password**, then
click **New experiment** and copy the one-time password from the **Sign in**
dialog.
"""
INVENTORY = {
    "pages": {
        "guides/sample.md": {"class": "journey", "journey": "sample", "reason": "x"},
        "guides/other.md": {"class": "journey", "journey": "other", "reason": "x"},
    }
}
REGISTRY = {
    "R1-sample": {"walkthrough": "R1", "guide": "guides/sample.md", "max_seconds": 60},
    "R2-other": {"walkthrough": "R2", "guide": "guides/other.md", "max_seconds": 60},
    "R3-later": {
        "walkthrough": "R3",
        "guide": "guides/sample.md",
        "max_seconds": 60,
        "pending": "not yet",
    },
    "R2-sample-later": {
        "walkthrough": "R2",
        "guide": "guides/sample.md",
        "max_seconds": 60,
    },
}
STEPS: List[Dict[str, Any]] = [
    {
        "id": "open",
        "doc": "sign-in",
        "do": {"goto": "/login"},
        "expect": {"status": 200, "aria": '- button "Sign in"'},
        "fail": "the sign-in page does not answer",
    },
    {
        "id": "password",
        "doc": "sign-in",
        "do": {"fill": {"label": "Password", "secret": "new-password"}},
        "expect": {"visible": [{"role": "button", "name": "Sign in"}]},
        "snapshot": False,
        "snapshot_reason": "it types a password the run makes up",
        "fail": "there is no Password field",
    },
    {
        "id": "invite",
        "doc": "sign-in",
        "do": {"click": {"role": "button", "name": "New experiment"}},
        "expect": {"visible": [{"role": "dialog", "name": "Sign in"}]},
        "snapshot": False,
        "snapshot_reason": "its screen shows the one-time password",
        "fail": "no dialog opens",
    },
    {
        "id": "one-time",
        "doc": "sign-in",
        "do": {
            "keep": {
                "secret": "one-time",
                "role": "code",
                "within": {"role": "dialog", "name": "Sign in"},
            }
        },
        "fail": "the dialog shows no one-time password",
    },
    {
        "id": "close",
        "doc": "sign-in",
        "do": {"click": {"role": "button", "name": "Sign in"}},
        "expect": {"aria": '- heading "Experiments" [level=1]'},
        "fail": "the dialog does not close",
    },
    {
        "id": "key",
        "doc": "sign-in",
        "do": {
            "api": {
                "method": "POST",
                "path": "/api/v1/api-keys",
                "as": "admin",
                "body": {"name": "sample"},
            }
        },
        "expect": {"status": 201},
        "save": {"sdk-key": {"path": "key", "secret": True}},
        "fail": "no key is made",
    },
    {
        "id": "after",
        "doc": "sign-in",
        "do": {"goto": "/experiments"},
        "expect": {"aria": '- heading "Experiments" [level=1]'},
        "fail": "the experiments page does not answer",
    },
]


def _journey(recording: Any, steps: List[Dict[str, Any]] = STEPS, **top: Any):
    """A journey recording *recording* (one segment, or a list of them)."""
    data: Dict[str, Any] = {
        "guide": "guides/sample.md",
        "stack": "compose-dev",
        "profile": "core",
        "video": False,
        "written": TODAY,
        "passwords": ["new-password"],
    }
    data.update(top)
    if recording is not None:
        data["recordings"] = recording if isinstance(recording, list) else [recording]
    data["steps"] = [dict(step) for step in steps]
    return data


def _load(tmp_path: Path, data: Dict[str, Any], **context: Any) -> Journey:
    docs = tmp_path / "docs" / "guides"
    docs.mkdir(parents=True, exist_ok=True)
    (docs / "sample.md").write_text(GUIDE, encoding="utf-8")
    (docs / "other.md").write_text(GUIDE, encoding="utf-8")
    path = tmp_path / "journeys" / "sample.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    values: Dict[str, Any] = {
        "docs_root": tmp_path / "docs",
        "inventory": INVENTORY,
        "doc_examples": {},
        "oracles": {},
        "today": TODAY,
        "recordings": REGISTRY,
    }
    values.update(context)
    return loader.load(path, loader.Context(**values))


def _refused(tmp_path: Path, data: Dict[str, Any]) -> str:
    with pytest.raises(loader.Refused) as refused:
        _load(tmp_path, data)
    return "\n".join(refused.value.problems)


def test_a_walkthrough_after_the_last_kept_value_loads(tmp_path):
    journey = _load(tmp_path, _journey({"name": "R1-sample", "start": "key"}))
    assert [(r.name, a, b) for r, a, b in journey.segments] == [("R1-sample", 5, 6)]
    # The api step that makes a key is in the segment: the key never reaches
    # the browser, and the runner checks the page after it all the same.
    assert journey.has_secrets


def test_a_password_typed_into_a_password_field_may_be_recorded(tmp_path):
    steps = [STEPS[0], STEPS[1], STEPS[4]]
    journey = _load(tmp_path, _journey({"name": "R1-sample"}, steps))
    assert [(r.name, a, b) for r, a, b in journey.segments] == [("R1-sample", 0, 2)]


def test_two_segments_of_one_journey_load_in_step_order(tmp_path):
    data = _journey(
        [
            {"name": "R2-sample-later", "start": "after"},
            {"name": "R1-sample", "start": "open", "end": "password"},
        ]
    )
    journey = _load(tmp_path, data)
    assert [(r.name, a, b) for r, a, b in journey.segments] == [
        ("R1-sample", 0, 1),
        ("R2-sample-later", 6, 6),
    ]


PLANTS = [
    pytest.param(
        _journey({"name": "R9-nothing", "start": "key"}),
        "recording 'R9-nothing' is not a table of recordings.toml",
        id="unknown-walkthrough",
    ),
    pytest.param(
        _journey({"name": "R3-later", "start": "key"}),
        "recording 'R3-later' is pending in recordings.toml",
        id="pending-walkthrough",
    ),
    pytest.param(
        _journey({"name": "R2-other", "start": "key"}),
        "recording 'R2-other' is recorded from 'guides/other.md' in"
        " recordings.toml, not from 'guides/sample.md'",
        id="another-guides-walkthrough",
    ),
    pytest.param(
        _journey({"name": "R1-sample", "start": "key"}, video=True),
        "a journey is recorded whole (video) or for its walkthroughs (recordings)",
        id="video-and-recording",
    ),
    pytest.param(
        _journey({"name": "R1-sample", "start": "nope"}),
        "recording.start 'nope' is not a step of the journey",
        id="start-names-no-step",
    ),
    pytest.param(
        _journey({"name": "R1-sample", "start": "after", "end": "key"}),
        "recording.end 'key' comes before recording.start 'after'",
        id="end-before-start",
    ),
    pytest.param(
        _journey({"name": "R1-sample"}),
        "step 4 (one-time): recording 'R1-sample' holds a keep step",
        id="segment-holds-a-keep",
    ),
    pytest.param(
        _journey({"name": "R1-sample", "end": "invite"}),
        "step 3 (invite): recording 'R1-sample' holds the screen the next step keeps",
        id="segment-holds-the-screen-before-a-keep",
    ),
    pytest.param(
        _journey({"name": "R1-sample", "start": "close"}),
        "step 5 (close): recording 'R1-sample' starts right after a keep step",
        id="segment-starts-on-the-kept-screen",
    ),
    pytest.param(
        _journey(
            {"name": "R1-sample", "end": "password"},
            [
                STEPS[0],
                {
                    **STEPS[1],
                    "do": {"fill": {"label": "Email", "secret": "new-password"}},
                },
            ],
        ),
        "recording 'R1-sample' types the secret 'new-password' into 'Email', which is"
        " not a password field",
        id="secret-typed-where-it-is-drawn",
    ),
    pytest.param(
        _journey({"name": "r1-sample", "start": "key"}),
        "recordings.0.name: String should match pattern",
        id="name-not-a-walkthrough-name",
    ),
]


#: Two segments of one journey: each refusal holds for each, and they may not
#: overlap or share a name.
PLANTS += [
    pytest.param(
        _journey(
            [
                {"name": "R1-sample", "start": "key"},
                {"name": "R2-sample-later", "start": "after"},
            ]
        ),
        "recordings 'R1-sample' and 'R2-sample-later' overlap",
        id="two-segments-overlap",
    ),
    pytest.param(
        _journey(
            [
                {"name": "R1-sample", "start": "key", "end": "key"},
                {"name": "R1-sample", "start": "after"},
            ]
        ),
        "recording 'R1-sample' is named twice",
        id="one-name-twice",
    ),
    pytest.param(
        _journey(
            [
                {"name": "R1-sample", "start": "open", "end": "password"},
                {"name": "R2-sample-later", "start": "one-time", "end": "close"},
            ]
        ),
        "step 4 (one-time): recording 'R2-sample-later' holds a keep step",
        id="the-second-segment-holds-a-keep",
    ),
    pytest.param(
        _journey(
            [
                {"name": "R1-sample", "start": "open", "end": "open"},
                {"name": "R2-sample-later", "start": "close", "end": "close"},
            ]
        ),
        "step 5 (close): recording 'R2-sample-later' starts right after a keep step",
        id="the-second-segment-starts-on-the-kept-screen",
    ),
    pytest.param(
        _journey(
            [
                {"name": "R1-sample", "start": "key", "end": "key"},
                {"name": "R9-nothing", "start": "after"},
            ]
        ),
        "recording 'R9-nothing' is not a table of recordings.toml",
        id="the-second-segment-is-unknown",
    ),
]


@pytest.mark.parametrize("data, fragment", PLANTS)
def test_each_recording_refusal_fires(tmp_path, data, fragment):
    assert fragment in _refused(tmp_path, data)


# ---------------------------------------------------------------------------
# The page check: what the runner reads after every recorded step (V13)
# ---------------------------------------------------------------------------
KEY = "eptk_Planted0Key9xyz"


@pytest.mark.parametrize(
    "text, fields, where",
    [
        pytest.param(f"Your key: {KEY}", [], "the page's text", id="in-the-text"),
        pytest.param(
            "Planted key\nshown", [], "the page's text", id="split-by-a-line-break"
        ),
        pytest.param(
            "", [("text", f"x {KEY} y")], "a field of type text", id="in-a-text-field"
        ),
        pytest.param(
            "", [("textarea", KEY)], "a field of type textarea", id="in-a-text-area"
        ),
        pytest.param("", [("password", KEY)], None, id="dots-in-a-password-field"),
        pytest.param("Nothing kept here", [("text", "hello")], None, id="clean"),
    ],
)
def test_the_page_check_finds_a_kept_value_wherever_it_is_drawn(text, fields, where):
    assert redaction.on_screen(text, fields, {KEY, "Planted key shown"}) == where


def test_the_page_check_names_the_place_never_the_value():
    where = redaction.on_screen(KEY, [], {KEY})
    assert where is not None and KEY not in where


# ---------------------------------------------------------------------------
# The end-of-run scan removes a journey's walkthrough with its files
# ---------------------------------------------------------------------------
PLANT = "Planted0Value9xyz"


def _run_dir(tmp_path: Path) -> Path:
    run = tmp_path / "run"
    (run / "recordings").mkdir(parents=True)
    (run / "invite").mkdir()
    (run / "recordings" / "R8-onboarding.webm").write_bytes(b"webm")
    (run / "recordings" / "R7-docs-site.webm").write_bytes(b"webm")
    return run


OWNERS = {
    "recordings/R8-onboarding.webm": "invite",
    "recordings/R7-docs-site.webm": "docs-site",
}


def test_a_journey_file_holding_a_value_takes_its_walkthrough_with_it(tmp_path):
    run = _run_dir(tmp_path)
    (run / "invite" / "03-dialog.aria.yml").write_text(f"- code: {PLANT}\n")
    record = redaction.clear(run, {PLANT}, owners=OWNERS)
    assert record["removed"] == ["invite/03-dialog.aria.yml"]
    assert record["screens_removed"] == ["recordings/R8-onboarding.webm"]
    assert (run / "recordings" / "R7-docs-site.webm").is_file()
    removed = list(record["removed"])
    assert redaction.journeys_of(removed, ["invite", "docs-site"], owners=OWNERS) == {
        "invite"
    }


def test_a_shared_file_holding_a_value_takes_every_walkthrough(tmp_path):
    run = _run_dir(tmp_path)
    (run / "results.jsonl").write_text(f'{{"observed": "{PLANT}"}}\n')
    record = redaction.clear(run, {PLANT}, owners=OWNERS)
    assert record["screens_removed"] == [
        "recordings/R7-docs-site.webm",
        "recordings/R8-onboarding.webm",
    ]


def test_a_walkthrough_holding_a_value_fails_its_own_journey(tmp_path):
    run = _run_dir(tmp_path)
    (run / "recordings" / "R8-onboarding.webm").write_bytes(PLANT.encode())
    record = redaction.clear(run, {PLANT}, owners=OWNERS)
    assert record["removed"] == ["recordings/R8-onboarding.webm"]
    removed = list(record["removed"])
    assert redaction.journeys_of(removed, ["invite", "docs-site"], owners=OWNERS) == {
        "invite"
    }
    assert redaction.files_of(removed, "invite", owners=OWNERS) == tuple(removed)
    assert redaction.files_of(removed, "docs-site", owners=OWNERS) == ()


def test_an_unowned_walkthrough_holding_a_value_fails_every_journey(tmp_path):
    run = _run_dir(tmp_path)
    (run / "recordings" / "R7-docs-site.webm").write_bytes(PLANT.encode())
    record = redaction.clear(run, {PLANT}, owners={})
    removed = list(record["removed"])
    assert redaction.journeys_of(removed, ["invite", "docs-site"], owners={}) == {
        "invite",
        "docs-site",
    }


# ---------------------------------------------------------------------------
# The summary: a walkthrough reads PASS only when its guide passed (V12)
# ---------------------------------------------------------------------------
def _step_record(result: str, step: int = 1) -> Record:
    return Record(
        run="1",
        sha="abc",
        stack="compose-dev",
        guide="statistics/power-analysis.md",
        step=step,
        step_id=f"s{step}",
        heading="The power calculator",
        action="open /power-calculator",
        expected="status 200",
        failure_signature="it does not answer",
        result=result,
        reason="",
        observed="seen",
        snapshot="",
        expected_snapshot="structural only",
        ms=5,
    )


def _guide_run(*results: str) -> report.GuideRun:
    return report.GuideRun(
        journey="power-calculator",
        guide="statistics/power-analysis.md",
        title="Statistical Power Analysis",
        stack="compose-dev",
        records=tuple(_step_record(r, n) for n, r in enumerate(results, 1)),
    )


RECORDED = {
    "R4-power-analysis": {
        "journey": "power-calculator",
        "file": "recordings/R4-power-analysis.webm",
        "seconds": 9.5,
        "dropped": "",
    }
}


def _rows(run: report.GuideRun, present: bool = True, recorded=RECORDED):
    rows = report.walkthrough_rows(
        registry(),
        recorded,
        {run.journey: report.verdict(run)},
        lambda name: present,
    )
    return {name: (length, result) for name, _, length, result in rows}


def test_a_walkthrough_reads_pass_when_its_guide_passed():
    rows = _rows(_guide_run("PASS", "PASS"))
    assert rows["R4-power-analysis"] == (
        "9.5 s, at most 60 s",
        "PASS (power-calculator)",
    )


def test_a_walkthrough_reads_fail_when_its_guide_failed():
    """V12's planted defect: R4's guide fails, so its recording reads FAIL."""
    rows = _rows(_guide_run("PASS", "FAIL"))
    assert rows["R4-power-analysis"][1] == "FAIL at step 2 (power-calculator)"


def test_the_other_walkthrough_states_are_said():
    run = _guide_run("PASS")
    later = {
        **registry(),
        "R9-later": {"guide": "x.md", "max_seconds": 60, "pending": "a reason"},
    }
    pending = report.walkthrough_rows(
        later, RECORDED, {run.journey: report.verdict(run)}, lambda name: True
    )
    assert [row[3] for row in pending if row[0] == "R9-later"] == ["pending: a reason"]
    rows = _rows(run)
    assert rows["R1-quick-start"][1] == "not recorded in this run"
    gone = _rows(_guide_run("PASS"), present=False)
    assert gone["R4-power-analysis"][1].startswith("removed by the end-of-run scan")
    dropped = _rows(
        _guide_run("PASS"),
        recorded={
            "R4-power-analysis": {
                **RECORDED["R4-power-analysis"],
                "file": "",
                "dropped": "a value the run keeps is drawn in the page's text",
            }
        },
    )
    assert dropped["R4-power-analysis"][1] == (
        "dropped: a value the run keeps is drawn in the page's text"
    )


def test_the_summary_has_a_walkthroughs_table():
    run = _guide_run("PASS")
    rows = report.walkthrough_rows(
        registry(), RECORDED, {run.journey: report.verdict(run)}, lambda name: True
    )
    text = report.summary_markdown(
        [run], {"pages": {}}, date="2026-10-07", sha="abc", walkthroughs=rows
    )
    assert "## Launch walkthroughs" in text
    assert (
        "| R4-power-analysis | statistics/power-analysis.md | 9.5 s, at most 60 s |"
        " PASS (power-calculator) |" in text
    )
    plain = report.summary_markdown([run], {"pages": {}}, date="2026-10-07", sha="abc")
    assert "Launch walkthroughs" not in plain


# ---------------------------------------------------------------------------
# The runner's loop, against a stand-in page (V13 at run time)
# ---------------------------------------------------------------------------
# A walkthrough is recorded by JourneyRunner.run: the page is read for every
# kept value before the segment starts and after each of its steps, and a value
# drawn there fails the step and deletes the recording. These tests run that
# loop with a stand-in for Playwright (the unit job has none) and a stand-in
# for one step, which changes what the page draws; the steps' own machinery is
# test_docs_journey_runner.py's.
KEPT = "eptk_Kept0Value9abcdef"


@pytest.fixture
def execute_module(monkeypatch):
    """docs_runner.execute imported against a stand-in ``playwright.sync_api``."""
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


class _Overlay:
    def __init__(self, cast: "_Screencast", html: str):
        self.cast = cast
        self.html = html
        cast.overlays.append(html)
        cast.drawn.append(html)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.cast.overlays.remove(self.html)


class _Screencast:
    """Writes its file on stop, as Playwright's does."""

    def __init__(self):
        self.path: Any = None
        self.starts = 0
        self.overlays: List[str] = []
        self.drawn: List[str] = []
        self.chapters: List[tuple] = []
        self.hidden = False
        self.hidden_at_screenshot: List[bool] = []

    def start(self, path, size):
        assert size == {"width": 1280, "height": 720}
        self.path = Path(path)
        self.starts += 1

    def show_chapter(self, title, description=None, duration=None):
        self.chapters.append((title, description))

    def show_overlay(self, html):
        return _Overlay(self, html)

    def hide_overlays(self):
        self.hidden = True

    def show_overlays(self):
        self.hidden = False

    def stop(self):
        if self.path is not None:
            self.path.write_bytes(b"\x1a\x45\xdf\xa3 a recording")
            self.path = None


class _Locator:
    def __init__(self, page: "_Page", what: tuple):
        self.page = page
        self.what = what

    def aria_snapshot(self):
        return "- main"


class _Page:
    def __init__(self, execute_module):
        self.module = execute_module
        self.screencast = _Screencast()
        self.text = ""
        self.fields: List[tuple] = []
        self.video = None
        self.evaluated: List[tuple] = []
        self.masks: List[list] = []

    def on(self, event, handler):
        return None

    def evaluate(self, script, arg=None):
        if script == self.module.DRAWN_JS:
            return [self.text, [list(field) for field in self.fields]]
        self.evaluated.append((script, arg))
        return 0

    def wait_for_load_state(self, state):
        return None

    def get_by_text(self, value):
        return _Locator(self, ("text", value))

    def locator(self, selector):
        return _Locator(self, ("css", selector))

    def screenshot(self, path, mask):
        self.masks.append([locator.what for locator in mask])
        self.screencast.hidden_at_screenshot.append(self.screencast.hidden)
        Path(path).write_bytes(b"png")


class _Context:
    def __init__(self, page):
        self.page = page

    def set_default_timeout(self, ms):
        return None

    def new_page(self):
        return self.page

    def close(self):
        return None


class _Browser:
    def __init__(self, page):
        self.page = page

    def new_context(self, **options):
        assert "record_video_dir" not in options, "a walkthrough is not a video"
        return _Context(self.page)


def _walk_journey(recording: Any, gotos: List[str]) -> Journey:
    """A journey of goto steps recording *recording* (one segment or a list)."""
    return Journey.model_validate(
        {
            "guide": "guides/sample.md",
            "stack": "docs-local",
            "profile": "core",
            "video": False,
            "written": TODAY,
            "recordings": recording if isinstance(recording, list) else [recording],
            "steps": [
                {
                    "id": f"step-{n}",
                    "doc": "sign-in",
                    "do": {"goto": goto},
                    "expect": {"status": 200, "aria": "- main"},
                    "fail": "the page does not answer",
                }
                for n, goto in enumerate(gotos, 1)
            ],
        }
    )


def _walk(execute_module, tmp_path, journey, screens, kept=(KEPT,), fails=()):
    """Run *journey*; *screens* maps a step number to what the page then draws,
    and a step numbered in *fails* fails."""
    from docs_runner import log as runlog
    from docs_runner.stacks import Running

    page = _Page(execute_module)
    runner = execute_module.JourneyRunner(
        None,
        _Browser(page),
        runlog.Log(tmp_path / "results.jsonl"),
        execute_module.Settings(run_dir=tmp_path, run_id="r", sha="abc123"),
    )
    for value in kept:
        runner.redactor.add(value)

    def step(journey, journey_id, number, step, running, page_, api, req, a, failed):
        if failed:
            result, observed = "NOT RUN", ""
        else:
            page_.text, page_.fields = screens.get(number, (page_.text, page_.fields))
            result, observed = "PASS", f"at {step.do.goto}"
            if number in fails:
                result, observed = "FAIL", "the page does not answer"
        return Record(
            run="r",
            sha="abc123",
            stack="docs-local",
            guide=journey.guide,
            step=number,
            step_id=step.id,
            heading="Sign in",
            action=f"open {step.do.goto}",
            expected="status 200",
            failure_signature=step.fail,
            result=result,
            reason="earlier-step-failed" if failed else "",
            observed=observed,
            snapshot="",
            expected_snapshot="structural only",
            ms=1,
        )

    runner._step = step
    guide = type("Guide", (), {"title": "Sample guide", "anchors": {}})()
    running = Running(name="docs-local", base_url="http://site/")
    records = runner.run(journey, "sample", running, guide)
    return runner, page, records


RECORDING = Path("recordings") / "R1-sample.webm"


def test_a_clean_walkthrough_is_recorded_with_its_title_and_captions(
    execute_module, tmp_path
):
    journey = _walk_journey({"name": "R1-sample"}, ["/a", "/b", "/c"])
    runner, page, records = _walk(
        execute_module, tmp_path, journey, {2: ("Nothing kept here", [])}
    )
    assert [r.result for r in records] == ["PASS", "PASS", "PASS"]
    assert (tmp_path / RECORDING).is_file()
    assert runner.recorded["R1-sample"]["file"] == RECORDING.as_posix()
    assert runner.recorded["R1-sample"]["dropped"] == ""
    assert page.screencast.chapters == [
        ("R1: Sample guide", "guides/sample.md, commit abc123")
    ]
    assert any("Step 3 of 3" in html for html in page.screencast.drawn)


def test_a_kept_value_drawn_after_a_step_fails_it_and_leaves_no_recording(
    execute_module, tmp_path
):
    journey = _walk_journey({"name": "R1-sample"}, ["/a", "/b", "/c"])
    runner, page, records = _walk(
        execute_module, tmp_path, journey, {2: (f"Your key: {KEPT}", [])}
    )
    assert [r.result for r in records] == ["PASS", "FAIL", "NOT RUN"]
    assert records[1].observed == (
        "a value the run keeps is drawn in the page's text while it is recorded;"
        " recording R1-sample was dropped"
    )
    assert not (tmp_path / RECORDING).exists()
    assert runner.recorded["R1-sample"] == {
        "journey": "sample",
        "file": "",
        "seconds": runner.recorded["R1-sample"]["seconds"],
        "dropped": "a value the run keeps is drawn in the page's text",
    }
    logged = (tmp_path / "results.jsonl").read_text()
    assert KEPT not in logged and '"result": "FAIL"' in logged


def test_a_kept_value_on_the_page_before_the_segment_is_never_recorded(
    execute_module, tmp_path
):
    """The page already draws the value when the segment starts; the first
    recorded step moves away from it. Nothing is recorded, and that step fails."""
    journey = _walk_journey({"name": "R1-sample", "start": "step-2"}, ["/a", "/b"])
    runner, page, records = _walk(
        execute_module,
        tmp_path,
        journey,
        {1: (f"one-time password {KEPT}", []), 2: ("Signed in", [])},
    )
    assert [r.result for r in records] == ["PASS", "FAIL"]
    assert "drawn in the page's text while it is recorded" in records[1].observed
    assert page.screencast.starts == 0
    assert not (tmp_path / RECORDING).exists()
    assert runner.recorded["R1-sample"]["file"] == ""


@pytest.mark.parametrize(
    "kind, verdict",
    [("password", "PASS"), ("text", "FAIL"), ("email", "FAIL")],
)
def test_a_kept_value_typed_into_a_field_fails_unless_it_draws_dots(
    execute_module, tmp_path, kind, verdict
):
    journey = _walk_journey({"name": "R1-sample"}, ["/a", "/b"])
    runner, page, records = _walk(
        execute_module, tmp_path, journey, {1: ("Sign in", [(kind, KEPT)])}
    )
    assert records[0].result == verdict
    assert (tmp_path / RECORDING).exists() is (verdict == "PASS")


def test_the_captions_are_redacted_like_the_log(execute_module, tmp_path):
    journey = _walk_journey({"name": "R1-sample"}, ["/a", f"/keys/{KEPT}"])
    runner, page, records = _walk(execute_module, tmp_path, journey, {})
    assert [r.result for r in records] == ["PASS", "PASS"]
    shown = "\n".join(page.screencast.drawn)
    assert "open /keys/(redacted)" in shown
    assert KEPT not in shown


def test_a_recorded_screen_masks_only_a_field_holding_a_kept_value(
    execute_module, tmp_path
):
    """Playwright draws a mask into the page, so a recording would show one
    over every field. While recording, only fields the page check would fail
    on are marked and masked; outside a recording, every field is."""
    from docs_runner.model import Step

    journey = _walk_journey({"name": "R1-sample"}, ["/a"])
    runner, page, _ = _walk(execute_module, tmp_path, journey, {})
    runner.secrets = {"new-password": KEPT}
    step = Step.model_validate(journey.steps[0].model_dump(by_alias=True))
    walkthrough = execute_module.Walkthrough(
        journey,
        "sample",
        journey.segments[0],
        type("Guide", (), {"title": "Sample guide", "anchors": {}})(),
        __import__("docs_runner.stacks", fromlist=["Running"]).Running(
            name="docs-local", base_url="http://site/"
        ),
        runner.settings,
    )
    walkthrough.active = True
    runner.walkthrough = walkthrough
    runner._screen(page, "sample", 1, step)
    assert page.masks[-1] == [("text", KEPT), ("css", "[data-docs-journey-mask]")]
    assert page.screencast.hidden_at_screenshot[-1] is True
    scripts = [script for script, _ in page.evaluated]
    assert scripts == [execute_module.MARK_JS, execute_module.UNMARK_JS]
    assert page.evaluated[0][1] == [[KEPT], "data-docs-journey-mask"]
    runner.walkthrough = None
    runner._screen(page, "sample", 1, step)
    assert page.masks[-1] == [
        ("text", KEPT),
        ("css", "input, textarea, [contenteditable]"),
    ]
    assert page.screencast.hidden_at_screenshot[-1] is False


class _MarkingPage(_Page):
    """``MARK_JS`` and ``UNMARK_JS`` as the page runs them, on ``fields``:
    each screenshot records the value of every field its masks cover."""

    def __init__(self, execute_module):
        super().__init__(execute_module)
        self.marked: List[int] = []
        self.masked_values: List[List[str]] = []

    def evaluate(self, script, arg=None):
        result = super().evaluate(script, arg)
        if script == self.module.MARK_JS:
            values, _ = arg
            self.marked = [
                n
                for n, (kind, typed) in enumerate(self.fields)
                if kind != "password" and any(v and v in typed for v in values)
            ]
            return len(self.marked)
        if script == self.module.UNMARK_JS:
            self.marked = []
        return result

    def screenshot(self, path, mask):
        super().screenshot(path, mask)
        covered = []
        if ("css", f"[{self.module.MASK_MARK}]") in self.masks[-1]:
            covered = [self.fields[n][1] for n in self.marked]
        self.masked_values.append(covered)


@pytest.mark.regression
def test_a_recorded_screen_masks_a_field_holding_any_value_the_run_keeps(
    execute_module, tmp_path
):
    """The fields marked for a recorded step's screenshot are found by the
    values the page check reads (the run's redactor), not only this journey's
    secrets: a field holding a credential an api step was answered is masked
    in the step's PNG too."""
    from docs_runner.model import Step
    from docs_runner.stacks import Running

    other = "eptk_Answered0Key9ghijkl"
    journey = _walk_journey({"name": "R1-sample"}, ["/a"])
    runner, _, _ = _walk(execute_module, tmp_path, journey, {})
    runner.secrets = {"new-password": KEPT}
    runner.redactor.add(other)
    page = _MarkingPage(execute_module)
    page.fields = [("text", f"key {other}"), ("text", "hello"), ("password", KEPT)]
    walkthrough = execute_module.Walkthrough(
        journey,
        "sample",
        journey.segments[0],
        type("Guide", (), {"title": "Sample guide", "anchors": {}})(),
        Running(name="docs-local", base_url="http://site/"),
        runner.settings,
    )
    walkthrough.active = True
    runner.walkthrough = walkthrough
    step = Step.model_validate(journey.steps[0].model_dump(by_alias=True))
    runner._screen(page, "sample", 1, step)
    assert page.masked_values == [[f"key {other}"]]
    values, mark = page.evaluated[0][1]
    assert set(values) == {KEPT, other} and mark == execute_module.MASK_MARK


def _js_lines(script: str) -> List[str]:
    return [" ".join(line.split()) for line in script.splitlines()]


def test_the_page_check_reads_placeholders_and_open_shadow_roots(execute_module):
    """The stand-in page answers ``DRAWN_JS`` with canned text, so no walk
    above can see what the script itself reads; its lines are pinned instead
    (no browser runs in the unit job). A value drawn as a field's placeholder,
    or inside an open shadow root, must reach ``redaction.on_screen``."""
    drawn = _js_lines(execute_module.DRAWN_JS)
    assert "if (el.placeholder) texts.push(String(el.placeholder));" in drawn
    assert "texts.push(String(el.shadowRoot.textContent || ''));" in drawn
    assert "visit(el.shadowRoot);" in drawn
    assert "const texts = [document.body ? document.body.innerText : ''];" in drawn
    for script in (execute_module.MARK_JS, execute_module.UNMARK_JS):
        assert "if (el.shadowRoot) visit(el.shadowRoot);" in _js_lines(script)


# -- two segments of one journey ----------------------------------------------
TWO = [
    {"name": "R1-sample", "start": "step-1", "end": "step-2"},
    {"name": "R2-sample-later", "start": "step-4", "end": "step-4"},
]
LATER = Path("recordings") / "R2-sample-later.webm"


def test_two_segments_record_two_files(execute_module, tmp_path):
    journey = _walk_journey(TWO, ["/a", "/b", "/c", "/d"])
    runner, page, records = _walk(execute_module, tmp_path, journey, {})
    assert [r.result for r in records] == ["PASS"] * 4
    assert (tmp_path / RECORDING).is_file() and (tmp_path / LATER).is_file()
    assert page.screencast.starts == 2
    assert {name: o["file"] for name, o in runner.recorded.items()} == {
        "R1-sample": RECORDING.as_posix(),
        "R2-sample-later": LATER.as_posix(),
    }
    assert [title for title, _ in page.screencast.chapters] == [
        "R1: Sample guide",
        "R2: Sample guide",
    ]


def test_a_value_drawn_in_the_second_segment_drops_it_alone(execute_module, tmp_path):
    journey = _walk_journey(TWO, ["/a", "/b", "/c", "/d"])
    runner, page, records = _walk(
        execute_module, tmp_path, journey, {4: (f"key {KEPT}", [])}
    )
    assert [r.result for r in records] == ["PASS", "PASS", "PASS", "FAIL"]
    assert "recording R2-sample-later was dropped" in records[3].observed
    assert (tmp_path / RECORDING).is_file()
    assert not (tmp_path / LATER).exists()
    assert runner.recorded["R2-sample-later"]["file"] == ""


def test_a_value_drawn_between_two_segments_stops_the_second_before_it_starts(
    execute_module, tmp_path
):
    """Step 3 is recorded by neither segment and is not checked; the page it
    leaves draws the value when the second segment would start."""
    journey = _walk_journey(TWO, ["/a", "/b", "/c", "/d"])
    runner, page, records = _walk(
        execute_module,
        tmp_path,
        journey,
        {3: (f"one-time password {KEPT}", []), 4: ("Signed in", [])},
    )
    assert [r.result for r in records] == ["PASS", "PASS", "PASS", "FAIL"]
    assert page.screencast.starts == 1
    assert (tmp_path / RECORDING).is_file()
    assert not (tmp_path / LATER).exists()


def test_a_segment_after_a_failed_step_is_not_recorded(execute_module, tmp_path):
    journey = _walk_journey(TWO, ["/a", "/b", "/c", "/d"])
    runner, page, records = _walk(execute_module, tmp_path, journey, {}, fails=(2,))
    assert [r.result for r in records] == ["PASS", "FAIL", "NOT RUN", "NOT RUN"]
    assert page.screencast.starts == 1
    assert (tmp_path / RECORDING).is_file()
    assert not (tmp_path / LATER).exists()
    assert runner.recorded["R2-sample-later"]["dropped"] == (
        "an earlier step failed, so none of it ran"
    )
