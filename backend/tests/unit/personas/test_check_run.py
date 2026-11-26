"""``tests/personas/check_run.py`` on recorded Playwright reports (no browser).

``recorded/`` holds one green run of the persona days, as Playwright 1.65
wrote it (its ``config`` block, which names this machine's paths, removed):
``list.json`` from ``--list --project=personas --reporter=json``,
``report.json`` from the run and ``oracle-log.jsonl`` from the oracles (only
id, kind and pass kept). ``report-caught-step-error.json`` is a second run
whose day 5 was wrapped in ``.catch(() => {})`` with ``[object Object]`` on the
page: the test passed, day 5's step carries the watcher's error (its stack
and snippet removed, its file made relative). Each test below breaks one
thing in a copy and checks the count it moves, the reason the details file
gets, and that the one public line still names no test, step or persona.

The cards are parsed for their shape only (no anchor is read), so nothing
here reads docs/.
"""

from __future__ import annotations

import copy
import json
import re
import sys
from pathlib import Path
from typing import Any

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[4]
PERSONAS_ROOT = REPO_ROOT / "tests" / "personas"
RECORDED = Path(__file__).resolve().parent / "recorded"

if str(PERSONAS_ROOT) not in sys.path:
    sys.path.insert(0, str(PERSONAS_ROOT))

import catalogue  # the path is set first
import check_run

pytestmark = pytest.mark.unit

LINE = re.compile(
    r"^persona-days: listed \d+ pinned \d+ passed \d+ failed \d+ flaky \d+ "
    r"skipped \d+ checks \d+$"
)
GREEN = "persona-days: listed 1 pinned 1 passed 1 failed 0 flaky 0 skipped 0 checks 0"


def _json(name: str) -> dict[str, Any]:
    return json.loads((RECORDED / name).read_text("utf-8"))


def _log() -> list[dict[str, Any]]:
    return check_run.read_oracle_log(RECORDED / "oracle-log.jsonl")


def _card() -> dict[str, Any]:
    return yaml.safe_load(
        (PERSONAS_ROOT / "personas" / "u-dev.yaml").read_text("utf-8")
    )


def _test(report: dict[str, Any]) -> dict[str, Any]:
    (test,) = [t for _, t in check_run.project_tests(report)]
    return test


def _evaluate(
    *,
    list_report: dict[str, Any] | None = None,
    report: dict[str, Any] | None = None,
    cards: list[dict[str, Any]] | None = None,
    oracle_log: list[dict[str, Any]] | None = None,
    known_noise: list[dict[str, Any]] | None = None,
    pinned: int = 1,
    list_exit: int = 0,
    run_exit: int = 0,
) -> check_run.Outcome:
    return check_run.evaluate(
        list_report=_json("list.json") if list_report is None else list_report,
        report=_json("report.json") if report is None else report,
        personas=[catalogue.parse_persona(c) for c in (cards or [_card()])],
        oracle_log=_log() if oracle_log is None else oracle_log,
        known_noise=known_noise or [],
        pinned=pinned,
        list_exit=list_exit,
        run_exit=run_exit,
    )


def _titles() -> list[str]:
    """Every title in the recorded run: the persona's, the test's, each step's."""
    test = _test(_json("report.json"))
    steps = list(check_run.step_titles(test["results"][-1]["steps"]))
    specs = [title for title, _ in check_run.project_tests(_json("report.json"))]
    return ["U-DEV", *specs, *steps]


def _assert_neutral(line: str) -> None:
    for title in _titles():
        assert title not in line, f"summary line names a test: {title!r}"
    assert LINE.match(line), f"not the count line: {line!r}"


# ------------------------------------------------------------------ green


def test_the_recorded_green_run_is_green_and_its_line_is_exact():
    outcome = _evaluate()
    assert outcome.problems == {}
    assert outcome.ok
    assert check_run.summary_line(outcome) == GREEN


def test_the_recorded_run_binds_every_step_of_the_card():
    test = _test(_json("report.json"))
    steps = list(check_run.step_titles(test["results"][-1]["steps"]))
    card = catalogue.parse_persona(_card())
    assert steps == [f"U-DEV day {s.n}: {s.intent}" for s in card.day]


# ---------------------------------------------------------- each count moves


def _with_status(status: str, final: str) -> dict[str, Any]:
    report = _json("report.json")
    test = _test(report)
    test["status"] = status
    test["results"][-1]["status"] = final
    return report


@pytest.mark.parametrize(
    ("status", "final", "counts"),
    [
        ("unexpected", "failed", {"failed": 1}),
        ("flaky", "passed", {"flaky": 1}),
        ("skipped", "skipped", {"skipped": 1}),
        # test.fail(): "expected", yet the attempt failed. Not a pass.
        ("expected", "failed", {"failed": 1}),
    ],
)
def test_a_day_that_did_not_pass_fails_the_run_and_moves_its_count(
    status, final, counts
):
    outcome = _evaluate(report=_with_status(status, final))
    assert not outcome.ok
    assert outcome.passed == 0
    for name in ("failed", "flaky", "skipped"):
        assert getattr(outcome, name) == counts.get(name, 0), name
    assert outcome.checks == 0, outcome.problems
    _assert_neutral(check_run.summary_line(outcome))


def test_a_failed_day_is_one_failure_not_a_check_per_step_it_never_reached():
    report = _with_status("unexpected", "failed")
    test = _test(report)
    test["results"][-1]["steps"] = test["results"][-1]["steps"][:3]
    log = [e for e in _log() if not e["id"].startswith(("u-dev.4", "u-dev.5"))]
    outcome = _evaluate(report=report, oracle_log=log)
    assert (outcome.failed, outcome.checks) == (1, 0), outcome.problems


def test_the_failing_step_s_own_error_is_not_a_second_check():
    report = _with_status("unexpected", "failed")
    test = _test(report)
    test["results"][-1]["steps"] = test["results"][-1]["steps"][:5]
    test["results"][-1]["steps"][4]["error"] = {"message": "Error: watcher: ..."}
    log = [e for e in _log() if not e["id"].startswith("u-dev.5")]
    outcome = _evaluate(report=report, oracle_log=log)
    assert (outcome.failed, outcome.checks) == (1, 0), outcome.problems


@pytest.mark.regression
def test_a_step_error_the_day_caught_is_a_check_not_a_pass():
    """The recorded run: day 5's error was caught, the test passed. The run
    is red, and the details name the day step."""
    outcome = _evaluate(report=_json("report-caught-step-error.json"))
    assert outcome.passed == 1 and outcome.failed == 0
    assert _reasons(outcome) == [
        "U-DEV day 5: a test.step error was caught and the day passed"
    ]
    assert outcome.checks == 1 and not outcome.ok
    _assert_neutral(check_run.summary_line(outcome))


def test_a_nested_step_error_counts_against_its_day():
    report = _json("report.json")
    steps = _test(report)["results"][-1]["steps"]
    steps[2]["steps"] = [
        {"title": "read the badge", "duration": 1, "error": {"message": "x"}}
    ]
    outcome = _evaluate(report=report)
    assert "U-DEV day 3: a test.step error was caught and the day passed" in (
        _reasons(outcome)
    )


def test_a_caught_error_in_the_flaky_attempt_that_passed_is_a_check_too():
    report = _with_status("flaky", "passed")
    _test(report)["results"][-1]["steps"][0]["error"] = {"message": "x"}
    outcome = _evaluate(report=report)
    assert outcome.flaky == 1
    assert _reasons(outcome) == [
        "U-DEV day 1: a test.step error was caught and the day passed"
    ]


def test_a_day_renamed_out_of_the_project_is_listed_0_pinned_1():
    """The spec renamed to `.spec.ts`: the personas project lists nothing."""
    empty = {"suites": [], "errors": [], "stats": {}}
    outcome = _evaluate(list_report=empty, report=empty)
    assert not outcome.ok
    line = check_run.summary_line(outcome)
    assert line.startswith("persona-days: listed 0 pinned 1 "), line


def test_a_pinned_count_of_0_is_refused():
    outcome = _evaluate(pinned=0)
    assert not outcome.ok
    assert check_run.summary_line(outcome) == "persona-days: pinned 0: refused"


@pytest.mark.parametrize(("list_exit", "run_exit"), [(1, 0), (0, 1)])
def test_a_playwright_command_that_exited_non_zero_fails_the_run(list_exit, run_exit):
    outcome = _evaluate(list_exit=list_exit, run_exit=run_exit)
    assert not outcome.ok
    assert check_run.summary_line(outcome) == GREEN  # the counts alone are clean


# ---------------------------------------------------------------- the checks


def _reasons(outcome: check_run.Outcome) -> list[str]:
    return sorted(outcome.problems.values())


def test_a_card_step_edited_alone_is_one_check():
    card = _card()
    card["day"][2]["intent"] = "start the experiment at once"
    outcome = _evaluate(cards=[card])
    assert _reasons(outcome) == ["U-DEV day 3: test.step title != card intent"]
    assert outcome.checks == 1 and not outcome.ok
    _assert_neutral(check_run.summary_line(outcome))


def test_a_step_whose_oracle_never_ran_is_one_check():
    log = [e for e in _log() if e["id"] != "u-dev.3.active"]
    outcome = _evaluate(oracle_log=log)
    assert _reasons(outcome) == [
        "declared oracle u-dev.3.active (api_state) absent from the oracle log"
    ]
    assert outcome.checks == 1


def test_an_oracle_decided_with_another_kind_is_absent():
    log = [
        {**e, "kind": "text_exact"} if e["id"] == "u-dev.1.key-listed-once" else e
        for e in _log()
    ]
    outcome = _evaluate(oracle_log=log)
    assert _reasons(outcome) == [
        "declared oracle u-dev.1.key-listed-once (api_state) absent from the oracle log"
    ]


def test_an_oracle_no_card_names_is_one_check():
    outcome = _evaluate(
        oracle_log=[*_log(), {"id": "u-dev.9.extra", "kind": "api_state", "pass": True}]
    )
    assert _reasons(outcome) == ["oracle u-dev.9.extra is on no card"]


def test_an_oracle_that_failed_inside_a_passing_day_is_one_check():
    log = [*_log(), {"id": "u-dev.3.active", "kind": "api_state", "pass": False}]
    outcome = _evaluate(oracle_log=log)
    assert _reasons(outcome) == ["oracle u-dev.3.active failed in a day that passed"]


@pytest.mark.parametrize(
    ("title", "reason"),
    [
        ("open the dashboard", "test.step 'open the dashboard': no card line"),
        ("U-DEV day 9: rest", "U-DEV day 9: no layer-A step on the card"),
        ("U-ANALYST day 1: read", "U-ANALYST day 1: a step of another persona's test"),
    ],
)
def test_a_step_with_no_card_line_is_one_check(title, reason):
    report = _json("report.json")
    _test(report)["results"][-1]["steps"].append({"title": title, "duration": 1})
    outcome = _evaluate(report=report)
    assert _reasons(outcome) == [reason]


def test_a_nested_step_is_bound_too():
    report = _json("report.json")
    steps = _test(report)["results"][-1]["steps"]
    steps[0]["steps"] = [{"title": "fill the form", "duration": 1}]
    outcome = _evaluate(report=report)
    assert _reasons(outcome) == ["test.step 'fill the form': no card line"]


def test_a_card_step_run_twice_is_one_check():
    report = _json("report.json")
    steps = _test(report)["results"][-1]["steps"]
    steps.append(copy.deepcopy(steps[2]))
    outcome = _evaluate(report=report)
    assert _reasons(outcome) == ["U-DEV day 3: more than one test.step"]


def test_a_card_step_with_no_test_step_is_one_check():
    report = _json("report.json")
    steps = _test(report)["results"][-1]["steps"]
    del steps[6]
    outcome = _evaluate(report=report)
    assert _reasons(outcome) == ["U-DEV day 7: no test.step for this card step"]


def test_a_card_with_no_test_fails_each_of_its_steps():
    other = _card()
    other["id"] = "U-QA"
    for step in other["day"]:
        for oracle in step["oracles"]:
            oracle["id"] = oracle["id"].replace("u-dev", "u-qa")
    outcome = _evaluate(cards=[_card(), other])
    assert outcome.checks == len(other["day"])
    assert all(r.startswith("U-QA day ") for r in _reasons(outcome))


def test_a_test_that_is_not_a_persona_s_day_is_one_check():
    report = _json("report.json")
    report["suites"][0]["specs"][0]["title"] = "the developer's day"
    outcome = _evaluate(report=report)
    assert "test \"the developer's day\": not a persona's day" in _reasons(outcome)
    # ...and U-DEV's card then has no test: each of its eight steps is a check.
    assert outcome.checks == 1 + 8


def test_a_run_that_ran_fewer_tests_than_it_listed_is_a_check():
    empty = {"suites": [], "errors": [], "stats": {}}
    outcome = _evaluate(report=empty)
    assert "ran 0 tests, listed 1" in _reasons(outcome)


def test_a_run_that_reported_errors_of_its_own_is_a_check():
    report = _json("report.json")
    report["errors"] = [{"message": "config error"}]
    outcome = _evaluate(report=report)
    assert _reasons(outcome) == ["the run reported errors of its own"]


def test_a_known_noise_entry_that_did_not_occur_is_a_check_and_one_that_did_is_not():
    entry = {"issue": 123, "kind": "console", "match": "x"}
    outcome = _evaluate(known_noise=[entry])
    assert _reasons(outcome) == ["known-noise #123 did not occur"]
    outcome = _evaluate(
        known_noise=[entry], oracle_log=[*_log(), {"noise": 123, "kind": "console"}]
    )
    assert outcome.problems == {}


# ----------------------------------------------------- the public line


def _every_red_outcome() -> list[check_run.Outcome]:
    card = _card()
    card["day"][0]["intent"] = "edited"
    return [
        _evaluate(report=_with_status("unexpected", "failed")),
        _evaluate(report=_with_status("flaky", "passed")),
        _evaluate(cards=[card]),
        _evaluate(oracle_log=[]),
        _evaluate(known_noise=[{"issue": 9, "kind": "text", "match": "null"}]),
    ]


def test_the_line_names_no_test_step_or_persona_on_any_red():
    for outcome in _every_red_outcome():
        assert outcome.problems or not outcome.ok
        _assert_neutral(check_run.summary_line(outcome))


# ------------------------------------------------------------- the command


def _files(tmp_path: Path, report: Any = None) -> list[str]:
    (tmp_path / "list.json").write_text((RECORDED / "list.json").read_text("utf-8"))
    text = (
        (RECORDED / "report.json").read_text("utf-8")
        if report is None
        else json.dumps(report)
    )
    (tmp_path / "report.json").write_text(text)
    (tmp_path / "log.jsonl").write_text(
        (RECORDED / "oracle-log.jsonl").read_text("utf-8")
    )
    # The recording is of U-DEV's day alone: the command reads that card and
    # a pinned count of 1, not the live ones, which grow with each day.
    cards = tmp_path / "personas"
    cards.mkdir()
    (cards / "u-dev.yaml").write_text(
        (PERSONAS_ROOT / "personas" / "u-dev.yaml").read_text("utf-8")
    )
    (tmp_path / "pinned.yaml").write_text("tests: 1\n")
    return [
        "--list", str(tmp_path / "list.json"), "--list-exit", "0",
        "--report", str(tmp_path / "report.json"), "--run-exit", "0",
        "--oracle-log", str(tmp_path / "log.jsonl"),
        "--details", str(tmp_path / "details.txt"),
        "--personas", str(cards), "--pinned", str(tmp_path / "pinned.yaml"),
    ]  # fmt: skip


def test_the_command_prints_one_line_and_writes_it_to_the_run_summary(
    tmp_path, capsys, monkeypatch
):
    summary = tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    assert check_run.main(_files(tmp_path)) == 0
    out = capsys.readouterr()
    assert out.out == GREEN + "\n" and out.err == ""
    assert summary.read_text("utf-8") == GREEN + "\n"
    assert (tmp_path / "details.txt").read_text("utf-8") == ""


def test_the_command_puts_the_reasons_in_the_details_file_only(
    tmp_path, capsys, monkeypatch
):
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)
    args = _files(tmp_path)
    (tmp_path / "log.jsonl").write_text("")
    assert check_run.main(args) == 1
    out = capsys.readouterr()
    _assert_neutral(out.out.strip())
    assert out.err == "" and out.out.count("\n") == 1
    details = (tmp_path / "details.txt").read_text("utf-8")
    assert "declared oracle u-dev.1.key-listed-once (api_state)" in details


@pytest.mark.regression
def test_the_command_is_red_on_a_caught_step_error_and_names_it_in_the_details_only(
    tmp_path, capsys, monkeypatch
):
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)
    args = _files(tmp_path, _json("report-caught-step-error.json"))
    assert check_run.main(args) == 1
    out = capsys.readouterr()
    assert out.out == (
        "persona-days: listed 1 pinned 1 passed 1 failed 0 flaky 0 skipped 0 checks 1\n"
    )
    assert out.err == ""
    details = (tmp_path / "details.txt").read_text("utf-8")
    assert details == "U-DEV day 5: a test.step error was caught and the day passed\n"


def test_a_malformed_report_prints_the_fixed_line_and_never_its_text(
    tmp_path, capsys, monkeypatch
):
    """A title inside a malformed field: the error would quote it."""
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)
    report = _json("report.json")
    title = "U-DEV day 3: start the experiment"
    _test(report)["status"] = {"title": title}
    assert check_run.main(_files(tmp_path, report)) == 2
    out = capsys.readouterr()
    assert out.out == "persona-days: check_run failed\n"
    assert out.err == ""
    assert title not in out.out + out.err


def test_a_missing_input_prints_the_fixed_line(tmp_path, capsys, monkeypatch):
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)
    args = _files(tmp_path)
    (tmp_path / "report.json").unlink()
    assert check_run.main(args) == 2
    assert capsys.readouterr().out == "persona-days: check_run failed\n"


# --------------------------------------------------------- the real inputs


def test_the_pinned_count_is_at_least_one_and_matches_the_persona_specs():
    pinned = check_run.read_pinned(check_run.PINNED)
    assert pinned >= 1
    specs = sorted(
        (REPO_ROOT / "frontend/tests/e2e/personas").glob("*.persona.spec.ts")
    )
    tests = sum(
        len(re.findall(r"^test\(", p.read_text("utf-8"), flags=re.M)) for p in specs
    )
    assert tests == pinned, (
        f"tests/personas/pinned.yaml pins {pinned}; the persona specs hold {tests} "
        "top-level test() calls"
    )


def test_the_known_noise_file_is_valid_and_every_entry_names_an_issue():
    entries = check_run.read_known_noise(check_run.KNOWN_NOISE)
    for entry in entries:
        assert set(entry) == {"issue", "kind", "match"}, entry


def test_the_known_noise_reader_refuses_an_entry_without_an_issue(tmp_path):
    bad = tmp_path / "noise.json"
    bad.write_text(
        json.dumps({"entries": [{"issue": 0, "kind": "text", "match": "x"}]})
    )
    with pytest.raises(ValueError, match="issue number"):
        check_run.read_known_noise(bad)


# ------------------------------------------------------------------ sign-ins

SIGNIN_TS = REPO_ROOT / "frontend" / "tests" / "e2e" / "oracles" / "signin.ts"


def _signins(*seconds: float) -> list[dict[str, Any]]:
    start = 1_800_000_000_000
    return [{"signin": "developer", "at": start + s * 1000} for s in seconds]


@pytest.mark.regression
def test_more_sign_ins_in_a_minute_than_the_budget_is_a_check():
    """The days together signing in ten times in a minute: one more and the
    API's limit answers 429 to the next. Red, naming the count."""
    outcome = _evaluate(oracle_log=[*_log(), *_signins(*range(0, 50, 5))])
    assert _reasons(outcome) == [
        "sign-ins: 10 within 60 s, more than the 9 the limit of 10 leaves"
    ]
    assert outcome.checks == 1 and not outcome.ok
    _assert_neutral(check_run.summary_line(outcome))


@pytest.mark.parametrize(
    "seconds",
    [
        range(0, 45, 5),  # nine in 45 s: the budget, met
        range(0, 600, 20),  # thirty, three a minute
        [*range(9), *range(60, 69)],  # nine, then nine more 60 s later
    ],
)
def test_sign_ins_within_the_budget_are_no_check(seconds):
    outcome = _evaluate(oracle_log=[*_log(), *_signins(*seconds)])
    assert outcome.problems == {}, outcome.problems


def test_the_busiest_minute_counts_the_api_s_window():
    """A sign-in exactly 60 s after another is outside its window, as the
    API's sliding window counts (it drops what is 60 s old)."""
    assert check_run.busiest_minute([0, 60_000]) == 1
    assert check_run.busiest_minute([0, 59_999]) == 2
    assert check_run.busiest_minute([]) == 0


def test_the_budget_is_one_under_the_api_s_sign_in_limit_here_and_in_the_pacer():
    from backend.app.middleware.rate_limiter import RATE_LIMIT_CONFIG

    limit, window = RATE_LIMIT_CONFIG["/api/v1/auth/login"]
    assert (check_run.SIGN_IN_LIMIT, check_run.SIGN_IN_WINDOW_S) == (limit, window)
    assert check_run.SIGN_IN_BUDGET == limit - 1
    pacer = SIGNIN_TS.read_text("utf-8")
    for line in (
        'export const LOGIN_PATH = "/api/v1/auth/login";',
        f"export const SIGN_IN_LIMIT = {limit};",
        f"export const SIGN_IN_WINDOW_MS = {window * 1000:_};",
        "export const SIGN_IN_BUDGET = SIGN_IN_LIMIT - 1;",
    ):
        assert line in pacer, line
