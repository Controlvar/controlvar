"""The replay's gates (G0, G1, G2, Z), with no server: each scenario sent to
the fake server that answers as the docs say a correct one does, and a
``/results`` answer built from the reference, which every gate accepts; then
each defect SPEC #1544 section 3.5 plants in the product, applied to that
answer instead, which its gate refuses. The live run (tests/ground_truth/
test_replay.py, in ground-truth.yml) applies the same checks to the real
server.

Also: the scenarios as section 3.4 sizes them, the seed and scale a run reads,
the account it signs in as, and what a failure message carries.
"""

from __future__ import annotations

import importlib
import io
import math
import os
import statistics
import struct
import subprocess
import sys

import httpx
import pytest
from scipy import stats

from backend.tests.unit.ground_truth.library import (
    REPO_ROOT,
    contract,
    generator,
    hashing,
    ledger,
    reference,
    replay,
)

# tests/ground_truth is on sys.path once library is imported.
suite = importlib.import_module("test_replay")
driver = importlib.import_module("driver")

pytestmark = pytest.mark.unit

SEED = 20261103
SCENARIOS = suite.scenarios(SEED, 1)


def _replayed(scenario, *, answer=None):
    plan = generator.simulate(SEED, scenario)
    text = replay(plan, declared=suite.DECLARED, answer=answer)
    records = ledger.read(io.StringIO(text))
    expected = contract.valid_events(records)
    return plan, records, expected.experiments[scenario.experiment_key]


def honest_results(scenario, experiment, *, compare=reference.compare):
    """What a correct ``GET /api/v1/results/{id}`` answers, in its shape."""
    metrics = []
    for metric in scenario.metrics:
        analysis = reference.ANALYSIS[metric.type]
        per_user = {
            name: experiment.per_user(name, metric.event_name, metric.type)
            for name in experiment.variants
        }
        comparisons = {
            c.variant: c
            for c in compare(
                per_user,
                analysis,
                scenario.correction_method,
                scenario.confidence_level,
            )
        }
        variants = []
        for index, name in enumerate(experiment.variants):
            values = per_user[name]
            c = comparisons.get(name)
            variants.append(
                {
                    "variant_name": name,
                    "is_control": index == 0,
                    "sample_size": len(values),
                    "conversions": experiment.converting_users(name, metric.event_name),
                    "mean": math.fsum(values) / len(values),
                    "std_dev": suite.expected_std_dev(values, analysis),
                    "p_value": None if c is None else c.p_value,
                    "adjusted_p_value": None if c is None else c.adjusted_p_value,
                    "absolute_difference": None if c is None else c.absolute_difference,
                    "absolute_difference_ci": (
                        list(c.absolute_difference_ci)
                        if c is not None and c.absolute_difference_ci
                        else None
                    ),
                    "is_significant": bool(c is not None and c.is_significant),
                }
            )
        metrics.append(
            {"metric_name": metric.name, "analysis": analysis, "variants": variants}
        )
    return {
        "correction_method": scenario.correction_method,
        "confidence_level": scenario.confidence_level,
        "metrics": metrics,
        "summary": {
            "total_users": experiment.total_users,
            "total_events": experiment.total_events,
            "total_conversions": experiment.total_converting_users(
                m.event_name for m in scenario.metrics
            ),
        },
        "srm": {
            "p_value": reference.srm_p(
                [experiment.sample_size(v) for v in experiment.variants],
                experiment.allocations,
            )
        },
    }


@pytest.fixture(scope="module", params=sorted(SCENARIOS))
def honest(request):
    scenario = SCENARIOS[request.param]
    plan, records, experiment = _replayed(scenario)
    return scenario, plan, records, experiment, honest_results(scenario, experiment)


def _variant(results, metric, variant):
    (found,) = [m for m in results["metrics"] if m["metric_name"] == metric]
    (entry,) = [v for v in found["variants"] if v["variant_name"] == variant]
    return entry


# ---------------------------------------------------------------------------
# A correct server passes every gate
# ---------------------------------------------------------------------------
def test_a_correct_server_passes_every_gate(honest):
    scenario, plan, _, experiment, results = honest
    assert suite.g0_findings(experiment) == []
    assert suite.plan_findings(plan, experiment) == []
    assert suite.g1_findings(scenario, experiment, results) == []
    assert suite.g2_findings(scenario, experiment, results) == []


# ---------------------------------------------------------------------------
# The defects SPEC section 3.5 plants, at the scenarios' real sizes
# ---------------------------------------------------------------------------
def test_g0_a_server_whose_bucket_moved_answers_other_variants():
    """The plant changes ``bucket_of``: here every answer is shifted by 50
    buckets, as the CI tamper does, and G0 names the users."""
    scenario = SCENARIOS["S2"]
    allocations = [(arm.name, arm.allocation) for arm in scenario.arms]

    def moved(op, default):
        status, body = default
        if op.kind == "assign":
            name = _shifted(op.body["user_id"], scenario.experiment_key, allocations)
            return status, {**body, "variant_name": name}
        if op.kind == "assign_batch":
            rows = [
                {
                    **row,
                    "variant_id": f"id-{_shifted(row['user_id'], scenario.experiment_key, allocations)}",
                }
                for row in body["assignments"]
            ]
            return status, {**body, "assignments": rows}
        return default

    _, records, experiment = _replayed(scenario, answer=moved)
    findings = suite.g0_findings(experiment)
    answers = suite.assignment_answers(records, scenario.experiment_key)
    # Every user at least once (sticky /assign calls answer again).
    assert answers >= scenario.users
    # N of N at 50/50: every answer is the other arm, and each is a finding.
    assert len(findings) == answers
    assert {(f.got_label, f.want_label) for f in findings} == {("answered", "O-ASSIGN")}


def test_g0_a_server_that_assigned_nobody_is_refused():
    """No answer says ``assigned: true``: there is no wrong variant to find, and
    G0 alone must not pass on that."""
    scenario = SCENARIOS["S2"]

    def nobody(op, default):
        status, body = default
        if op.kind == "assign":
            return status, {**body, "assigned": False, "reason": "targeting"}
        if op.kind == "assign_batch":
            rows = [{**r, "assigned": False} for r in body["assignments"]]
            return status, {**body, "assignments": rows}
        return default

    _, records, experiment = _replayed(scenario, answer=nobody)
    answers = suite.assignment_answers(records, scenario.experiment_key)
    assert answers == 0
    assert suite.g0_findings(experiment) == []  # what G0 saw before the check
    findings = suite.g0_findings(experiment, answers=answers, users=scenario.users)
    assert [f.line() for f in findings] == [
        f"assignment answers: answered 0, users {scenario.users}"
        " (at least one per user)"
    ]


def test_g0_every_user_answered_is_not_a_finding():
    scenario = SCENARIOS["S2"]
    _, records, experiment = _replayed(scenario)
    answers = suite.assignment_answers(records, scenario.experiment_key)
    assert suite.g0_findings(experiment, answers=answers, users=scenario.users) == []


def _shifted(user, key, allocations):
    bucket = (hashing.bucket(user, key) + 50) % 100
    cumulative = 0
    for name, allocation in allocations:
        cumulative += allocation
        if bucket < cumulative:
            return name
    return allocations[-1][0]


def test_g1_conversions_counted_as_events_are_refused():
    """``count_converting_users`` counting events: repeat buyers (S3) make
    conversions exceed the converting users."""
    scenario = SCENARIOS["S3"]
    _, _, experiment = _replayed(scenario)
    results = honest_results(scenario, experiment)
    for name in experiment.variants:
        entry = _variant(results, "revenue", name)
        entry["conversions"] = sum(experiment.counts(name, "purchase"))
    findings = suite.g1_findings(scenario, experiment, results)
    assert any("'conversions'" in f.where and f.tolerance == "exact" for f in findings)


def test_g1_a_rounded_value_total_is_refused():
    """A ``round()`` in ``value_totals``: the revenue means move."""
    scenario = SCENARIOS["S3"]
    _, _, experiment = _replayed(scenario)
    results = honest_results(scenario, experiment)
    for name in experiment.variants:
        totals = experiment.value_totals(name, "purchase")
        _variant(results, "revenue", name)["mean"] = math.fsum(
            round(t) for t in totals
        ) / len(totals)
    findings = suite.g1_findings(scenario, experiment, results)
    assert [f.where for f in findings] == [
        "variant 'control', metric 'revenue', field 'mean'",
        "variant 'treatment', metric 'revenue', field 'mean'",
    ]


def test_g1_a_float32_value_total_moves_nothing_under_quantisation():
    """Why the G1 tamper is ``round()``: values are multiples of 2^-6 and
    well under 2^17, so a float32 cast of a user's total is exact, and that
    plant would change no number (measured here)."""
    scenario = SCENARIOS["S3"]
    _, _, experiment = _replayed(scenario)
    for name in experiment.variants:
        for total in experiment.value_totals(name, "purchase"):
            assert struct.unpack("f", struct.pack("f", total))[0] == total


def test_g2_a_pooled_t_test_at_30_70_is_refused():
    """Welch with ``equal_var=True`` on S3: the p-values move."""
    scenario = SCENARIOS["S3"]
    _, _, experiment = _replayed(scenario)

    def pooled(per_user, analysis, method, level):
        found = reference.compare(per_user, analysis, method, level)
        if analysis != "mean":
            return found
        (control, values), *_ = per_user.items()
        out = []
        for c in found:
            p = float(
                stats.ttest_ind(per_user[c.variant], values, equal_var=True).pvalue
            )
            out.append(
                reference.Comparison(
                    c.variant,
                    p,
                    p,
                    c.absolute_difference,
                    c.absolute_difference_ci,
                    p < 0.05,
                )
            )
        return out

    results = honest_results(scenario, experiment, compare=pooled)
    findings = suite.g2_findings(scenario, experiment, results)
    assert any("'p_value'" in f.where and "revenue" in f.where for f in findings)


def test_g2_benjamini_hochberg_dropped_in_s4_is_refused():
    scenario = SCENARIOS["S4"]
    _, _, experiment = _replayed(scenario)
    results = honest_results(scenario, experiment)
    for name in experiment.variants[1:]:
        entry = _variant(results, "clicks", name)
        entry["adjusted_p_value"] = entry["p_value"]
    findings = suite.g2_findings(scenario, experiment, results)
    assert any("'adjusted_p_value'" in f.where for f in findings)


# ---------------------------------------------------------------------------
# Each check, one field at a time
# ---------------------------------------------------------------------------
@pytest.fixture(scope="module")
def s3():
    scenario = SCENARIOS["S3"]
    _, _, experiment = _replayed(scenario)
    return scenario, experiment


def _g1(s3, mutate):
    scenario, experiment = s3
    results = honest_results(scenario, experiment)
    mutate(results)
    return suite.g1_findings(scenario, experiment, results)


def _g2(s3, mutate):
    scenario, experiment = s3
    results = honest_results(scenario, experiment)
    mutate(results)
    return suite.g2_findings(scenario, experiment, results)


def test_g1_a_mean_is_exact(s3):
    def one_ulp(results):
        entry = _variant(results, "revenue", "treatment")
        entry["mean"] = math.nextafter(entry["mean"], math.inf)

    (finding,) = _g1(s3, one_ulp)
    assert finding.where == "variant 'treatment', metric 'revenue', field 'mean'"
    assert finding.tolerance == "exact"


def test_g1_a_proportion_s_mean_is_within_its_tolerance():
    scenario = SCENARIOS["S1"]
    _, _, experiment = _replayed(scenario)
    results = honest_results(scenario, experiment)
    entry = _variant(results, "signups", "control")
    entry["mean"] = math.nextafter(entry["mean"], math.inf)
    assert suite.g1_findings(scenario, experiment, results) == []
    entry["mean"] *= 1 + 1e-9
    (finding,) = suite.g1_findings(scenario, experiment, results)
    assert finding.tolerance == "rel 1e-12"


def test_g1_std_dev_at_rel_1e_9(s3):
    def within(results):
        _variant(results, "purchases", "control")["std_dev"] *= 1 + 1e-10

    def beyond(results):
        _variant(results, "purchases", "control")["std_dev"] *= 1 + 1e-8

    assert _g1(s3, within) == []
    (finding,) = _g1(s3, beyond)
    assert finding.where.endswith("field 'std_dev'")


@pytest.mark.parametrize("field", ["total_users", "total_events", "total_conversions"])
def test_g1_the_summary_is_exact(s3, field):
    def plus_one(results):
        results["summary"][field] += 1

    (finding,) = _g1(s3, plus_one)
    assert finding.where == f"summary {field!r}"


def test_g1_a_missing_metric_is_named(s3):
    def drop(results):
        results["metrics"] = results["metrics"][:1]

    (finding,) = _g1(s3, drop)
    assert finding.where == "metric 'purchases'"


@pytest.mark.parametrize("field", ["p_value", "adjusted_p_value"])
def test_g2_a_p_value_at_rel_1e_9(s3, field):
    def within(results):
        _variant(results, "revenue", "treatment")[field] *= 1 + 1e-10

    def beyond(results):
        _variant(results, "revenue", "treatment")[field] *= 1 + 1e-7

    assert _g2(s3, within) == []
    (finding,) = _g2(s3, beyond)
    assert finding.where == f"variant 'treatment', metric 'revenue', field {field!r}"


def test_g2_a_difference_near_0_has_an_absolute_floor(s3):
    scenario, experiment = s3
    floor = 1e-9 * max(
        abs(experiment.mean("control", "purchase", "revenue")),
        abs(experiment.mean("treatment", "purchase", "revenue")),
    )

    def nudge(by):
        def mutate(results):
            _variant(results, "revenue", "treatment")["absolute_difference_ci"][0] += by

        return mutate

    assert _g2(s3, nudge(floor / 2)) == []
    assert len(_g2(s3, nudge(floor * 2 + 1e-6))) == 1


def test_g2_is_significant_is_exact(s3):
    def flip(results):
        entry = _variant(results, "purchases", "treatment")
        entry["is_significant"] = not entry["is_significant"]

    (finding,) = _g2(s3, flip)
    assert finding.where.endswith("field 'is_significant'")


def test_g2_the_sample_ratio_check(s3):
    def moved(results):
        results["srm"]["p_value"] *= 1.01

    def missing(results):
        results["srm"] = None

    assert [f.where for f in _g2(s3, moved)] == ["srm 'p_value'"]
    assert [f.where for f in _g2(s3, missing)] == ["srm 'p_value'"]


def test_g2_the_stored_settings_are_the_scenario_s(s3):
    def other(results):
        results["correction_method"] = "none"

    assert [f.where for f in _g2(s3, other)] == ["field 'correction_method'"]


def test_the_plan_stored_is_checked(s3):
    """An event batch refused (a 422) leaves events unstored: G1 says so
    before any count could agree for the wrong reason."""
    scenario = SCENARIOS["S3"]

    def refuse_first_batch(op, default):
        if op.kind == "batch" and not refuse_first_batch.done:
            refuse_first_batch.done = True
            return 422, {"detail": "invalid"}
        return default

    refuse_first_batch.done = False
    plan, _, experiment = _replayed(scenario, answer=refuse_first_batch)
    findings = suite.plan_findings(plan, experiment)
    assert [f.where for f in findings] == ["events stored"]
    assert (findings[0].got_label, findings[0].want_label) == ("ledger", "plan")


# ---------------------------------------------------------------------------
# The documented std_dev, the scenarios, the run's settings, the messages
# ---------------------------------------------------------------------------
def test_expected_std_dev():
    assert suite.expected_std_dev([1.0, 0.0, 0.0, 0.0], "proportion") == math.sqrt(
        0.25 * 0.75
    )
    values = [1.5, 0.0, 2.25, 0.0, 7.0]
    assert suite.expected_std_dev(values, "mean") == pytest.approx(
        statistics.stdev(values), rel=1e-15
    )
    assert suite.expected_std_dev(values, "mean") != pytest.approx(
        statistics.pstdev(values), rel=1e-3
    )
    assert suite.expected_std_dev([], "proportion") is None
    assert suite.expected_std_dev([3.0], "mean") is None


def test_the_scenarios_are_section_3_4_s():
    shape = {
        name: (
            [(a.name, a.allocation) for a in s.arms],
            s.users,
            [(m.event_name, m.type) for m in s.metrics],
            s.correction_method,
        )
        for name, s in SCENARIOS.items()
    }
    assert shape == {
        "S1": (
            [("control", 50), ("treatment", 50)],
            4000,
            [("signup", "conversion")],
            "benjamini_hochberg",
        ),
        "S2": (
            [("control", 50), ("treatment", 50)],
            10000,
            [("checkout", "conversion")],
            "benjamini_hochberg",
        ),
        "S3": (
            [("control", 30), ("treatment", 70)],
            6000,
            [("purchase", "revenue"), ("purchase", "count")],
            "benjamini_hochberg",
        ),
        "S4": (
            [("control", 34), ("b", 33), ("c", 33)],
            6000,
            [("click", "conversion")],
            "benjamini_hochberg",
        ),
    }
    s1 = SCENARIOS["S1"]
    assert s1.arms[0].conversion == s1.arms[1].conversion  # A/A
    s3 = SCENARIOS["S3"]
    assert all(a.repeat > 0 and a.values is not None for a in s3.arms)
    assert {
        k: v.users for k, v in suite.scenarios(SEED, suite.SCALES["nightly"]).items()
    } == {name: 4 * s.users for name, s in SCENARIOS.items()}
    assert len({s.experiment_key for s in SCENARIOS.values()}) == 4
    assert all(str(SEED) in s.experiment_key for s in SCENARIOS.values())


def test_the_seed_is_given_or_new():
    assert suite.resolve_seed({"GROUND_TRUTH_SEED": "4242"}) == 4242
    drawn = {suite.resolve_seed({}) for _ in range(5)}
    assert len(drawn) > 1  # a new one each run
    assert all(1 <= seed < 2**31 for seed in drawn)
    with pytest.raises(ValueError):
        suite.resolve_seed({"GROUND_TRUTH_SEED": "-3"})


def test_the_scale_is_pr_or_nightly():
    assert suite.resolve_scale({}) == "pr"
    assert suite.resolve_scale({"GROUND_TRUTH_SCALE": "nightly"}) == "nightly"
    assert suite.SCALES == {"pr": 1, "nightly": 4}
    with pytest.raises(ValueError):
        suite.resolve_scale({"GROUND_TRUTH_SCALE": "huge"})


def test_the_run_signs_in_as_the_development_first_administrator():
    from backend.app.core.config import Settings

    fields = Settings.model_fields
    assert suite.DEV_ADMIN == (
        fields["FIRST_SUPERUSER"].default,
        fields["FIRST_SUPERUSER_PASSWORD"].default,
    )
    assert suite.credentials({}) == suite.DEV_ADMIN
    assert suite.credentials(
        {"FIRST_SUPERUSER": "a@x.io", "FIRST_SUPERUSER_PASSWORD": "pw"}
    ) == ("a@x.io", "pw")
    assert suite.credentials(
        {
            "GROUND_TRUTH_ADMIN_EMAIL": "b@x.io",
            "GROUND_TRUTH_ADMIN_PASSWORD": "pw2",
            "FIRST_SUPERUSER": "a@x.io",
        }
    ) == ("b@x.io", "pw2")


def test_the_api_address():
    assert suite.api_url({}) == "http://127.0.0.1:8000"
    assert (
        suite.api_url({"CONTROLVAR_API_URL": "http://10.0.0.5:9000"})
        == "http://10.0.0.5:9000"
    )


def test_the_suite_prints_its_seed_first_and_g6_reads_the_tree():
    """The live suite's G6 alone, with no server: the seed is printed before
    any test, and G6 fails exactly where ``modules/`` is present ("modules
    importable", as in the Unit Tests job's full checkout) and passes where it
    is not (core-build's copy, and the replay job's)."""
    result = subprocess.run(
        [
            sys.executable,
            "-B",
            "-m",
            "pytest",
            "tests/ground_truth/test_replay.py",
            "-k",
            "g6",
            "-p",
            "no:cov",
            "-q",
        ],
        cwd=REPO_ROOT,
        env={**os.environ, "GROUND_TRUTH_SEED": "4242", "GROUND_TRUTH_SCALE": "pr"},
        capture_output=True,
        text=True,
        timeout=300,
    )
    out = result.stdout + result.stderr
    assert "ground-truth replay: seed 4242, scale pr" in out, out
    if (REPO_ROOT / "modules").exists():
        assert result.returncode == 1, out
        assert "G6 failed: seed 4242, scale pr: modules importable" in out, out
        assert "GROUND_TRUTH_SEED=4242 python -m pytest" in out, out
    else:
        assert result.returncode == 0, out
        assert "1 passed" in out, out


def test_a_failure_names_the_scenario_seed_values_tolerance_and_the_command(
    s3, tmp_path
):
    scenario, _ = s3
    replayed = suite.Replayed(
        scenario=scenario,
        plan=generator.simulate(SEED, scenario),
        ledger_path=tmp_path / "S3.jsonl",
    )
    finding = suite.Finding(
        "variant 'treatment', metric 'revenue', field 'p_value'",
        0.41894,
        0.40094,
        "rel 1e-09",
    )
    text = suite.report("G2", replayed, [finding], seed=SEED, scale="pr")
    assert f"scenario S3, seed {SEED}" in text
    assert (
        "variant 'treatment', metric 'revenue', field 'p_value': /results 0.41894, reference 0.40094 (rel 1e-09)"
        in text
    )
    assert (
        f"GROUND_TRUTH_SEED={SEED} python -m pytest tests/ground_truth/test_replay.py -k S3"
        in text
    )
    assert str(tmp_path / "S3.jsonl") in text
    nightly = suite.report("G2", replayed, [finding], seed=SEED, scale="nightly")
    assert (
        f"GROUND_TRUTH_SEED={SEED} GROUND_TRUTH_SCALE=nightly python -m pytest"
        in nightly
    )


def test_g6_names_the_seed_and_the_command():
    text = suite.g6_message("/x/modules/__init__.py", seed=SEED, scale="nightly")
    assert text.startswith(f"G6 failed: seed {SEED}, scale nightly: modules importable")
    assert (
        f"GROUND_TRUTH_SEED={SEED} GROUND_TRUTH_SCALE=nightly python -m pytest"
        " tests/ground_truth/test_replay.py -k g6"
    ) in text


def test_a_refused_sign_in_or_key_fails_with_the_seed_and_the_command():
    """The ``live`` fixture's setup: a 401 from sign-in, then a 403 from key
    creation, each fails (not a bare ReplayError) naming seed and command."""

    def sign_in_refused(request):
        return httpx.Response(401, json={"detail": "no"})

    def key_refused(request):
        if request.url.path == driver.SIGN_IN:
            return httpx.Response(200, json={"access_token": "T" * 40})
        return httpx.Response(403, json={"detail": "no"})

    for handler, route in (
        (sign_in_refused, driver.SIGN_IN),
        (key_refused, driver.API_KEYS),
    ):
        with httpx.Client(
            transport=httpx.MockTransport(handler), base_url="http://api.invalid"
        ) as client:
            session = driver.Driver(client)
            with pytest.raises(pytest.fail.Exception) as raised:
                suite.start_session(
                    session,
                    "a@example.com",
                    "pw",
                    mask=lambda value: None,
                    seed=SEED,
                    scale="pr",
                )
        message = str(raised.value)
        assert route in message, message
        assert f"seed {SEED}, scale pr" in message, message
        assert (
            f"GROUND_TRUTH_SEED={SEED} python -m pytest tests/ground_truth/test_replay.py"
            in message
        ), message


def test_the_step_summary_carries_the_seed_and_counts_only():
    transport = httpx.MockTransport(lambda request: httpx.Response(200))
    with httpx.Client(transport=transport) as client:
        session = driver.Driver(client)
        session.routes[ledger.TRACK] = driver.RouteStats(requests=10, seconds=0.5)
        text = suite.summary_text(session, seed=SEED, scale="pr")
    assert f"Seed {SEED}, scale pr." in text
    assert (
        f"GROUND_TRUTH_SEED={SEED} python -m pytest tests/ground_truth/test_replay.py -k S2"
        in text
    )
    assert f"| `{ledger.TRACK}` | 10 | 0 | 0 | 0.5 | 20.0 |" in text
