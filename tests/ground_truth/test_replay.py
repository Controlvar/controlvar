"""The live replay (#1544): seeded, realistic traffic sent through the public
API to a running Controlvar, and its results page checked against the event
contract and an independent reference computed on the same sample.

Each scenario is a ``ground_truth.generator`` plan, sent by ``driver.Driver``,
which writes the ledger of every answer (in pytest's temporary directory:
synthetic ids only, never uploaded). What ``GET /api/v1/results/{id}`` must
show is ``contract.valid_events`` of that ledger, and the statistics are
``reference``'s on the same per-user arrays. The planted truth only sets each
scenario's direction; nothing is compared with it.

====  ============================================================  =====================
Id    Arms, users (pull-request scale; nightly sends four times)    Truth (direction only)
====  ============================================================  =====================
S1    2, 50/50, 4,000                                                A/A, 10% conversion
S2    2, 50/50, 10,000                                               8% against 9.6%
S3    2, 30/70, 6,000: about 5% buyers, repeat purchases, a          the treatment's
      lognormal(3, 1) value quantised to 2^-6; a revenue metric      values are higher
      and a count metric on the same events
S4    3, 34/33/33, 6,000, Benjamini-Hochberg                         one arm lifted
====  ============================================================  =====================

The gates, per scenario (SPEC #1544 v1.2, section 3.5):

* **G0** every assignment answer, single and batch, is O-ASSIGN's variant,
  from the allocation and order the test sent (never from a GET of the
  experiment), and no user is answered two variants.
* **G1** per variant and metric: ``sample_size``, ``conversions`` and, for a
  revenue or count metric, ``mean`` exactly (``math.fsum`` of the ledger's
  per-user values over n, exact under the 2^-6 quantisation); a proportion's
  ``mean`` within ``PROPORTION_MEAN_REL``; ``std_dev`` within ``REL``; the
  summary's ``total_users``, ``total_events`` and ``total_conversions``
  exactly; and the plan stored: every user assigned, every event accepted and
  tagged, one view event per ``/assign`` call.
* **G2** ``p_value``, ``adjusted_p_value``, ``absolute_difference``, a mean's
  ``absolute_difference_ci`` and the sample-ratio check's ``p_value`` within
  ``REL`` (a difference also within 1e-9 times the larger of the two means; a
  p-value also within ``P_FLOOR``); ``is_significant`` exactly.
* **Z** no answer is a 5xx.

And once, without a server:

* **G6** this copy of the tree cannot import ``modules``: the replay runs the
  core profile (``_platform.yml``'s "Make the core profile real").

A failure names the scenario and the seed, the variant, metric and field, the
two values and the tolerance, and the command that reproduces it.

Environment: ``CONTROLVAR_API_URL`` (default ``http://127.0.0.1:8000``);
``GROUND_TRUTH_SEED`` (default: a new seed each run, printed);
``GROUND_TRUTH_SCALE`` (``pr``, the default, or ``nightly``);
``GROUND_TRUTH_ADMIN_EMAIL`` and ``GROUND_TRUTH_ADMIN_PASSWORD`` (default
``FIRST_SUPERUSER`` and ``FIRST_SUPERUSER_PASSWORD``, else the first
administrator the bootstrap creates on an empty development database).
"""

from __future__ import annotations

import importlib.util
import math
import os
import random
import statistics
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import driver
import httpx
import pytest
from ground_truth import contract, generator, ledger, reference
from ground_truth.generator import Arm, Metric, Scenario, Values

#: Users per scenario multiply by this (``GROUND_TRUTH_SCALE``).
SCALES = {"pr": 1, "nightly": 4}
#: The first administrator ``python -m backend.app.db.bootstrap`` creates on
#: an empty database with ENVIRONMENT=development: the ``FIRST_SUPERUSER``
#: and ``FIRST_SUPERUSER_PASSWORD`` defaults of ``backend/app/core/config.py``.
DEV_ADMIN = ("admin@example.com", "admin")
#: The server stores an event sent again with the same ``event_id`` once
#: (#1543). Every id of a plan is used once, so this changes no number.
DECLARED = {"event_dedup": True}

#: Relative tolerance of every statistic that is not exact (PE-GT-11).
REL = 1e-9
#: Two p-values below this are equal.
P_FLOOR = 1e-300
#: A proportion's ``mean`` is computed as ``((c / n) * 100) / 100``, which
#: differs from ``c / n`` by one unit in the last place for about one pair in
#: seven (measured: 5,367 of 38,143 pairs, at most 1.8e-16 relative). So it is
#: compared within this; ``conversions`` and ``sample_size`` stay exact.
PROPORTION_MEAN_REL = 1e-12
#: Findings shown per failure; the count says how many there were.
SHOWN = 25


def resolve_seed(environ: Mapping[str, str]) -> int:
    """``GROUND_TRUTH_SEED``, or a new seed for this run."""
    text = environ.get("GROUND_TRUTH_SEED", "").strip()
    if not text:
        return random.SystemRandom().randrange(1, 2**31)
    if not text.isdigit():
        raise ValueError("GROUND_TRUTH_SEED must be a non-negative integer")
    return int(text)


def resolve_scale(environ: Mapping[str, str]) -> str:
    """``GROUND_TRUTH_SCALE``: ``pr`` (the default) or ``nightly``."""
    name = environ.get("GROUND_TRUTH_SCALE", "").strip() or "pr"
    if name not in SCALES:
        raise ValueError(f"GROUND_TRUTH_SCALE must be one of {sorted(SCALES)}")
    return name


def credentials(environ: Mapping[str, str]) -> tuple[str, str]:
    """The administrator the run signs in as: email and password."""
    email = (
        environ.get("GROUND_TRUTH_ADMIN_EMAIL")
        or environ.get("FIRST_SUPERUSER")
        or DEV_ADMIN[0]
    )
    password = (
        environ.get("GROUND_TRUTH_ADMIN_PASSWORD")
        or environ.get("FIRST_SUPERUSER_PASSWORD")
        or DEV_ADMIN[1]
    )
    return email, password


def scenarios(seed: int, scale: int) -> dict[str, Scenario]:
    """S1 to S4 (the table above), each under its own experiment key."""

    def key(name: str) -> str:
        return f"gt-{name.lower()}-{seed}"

    return {
        "S1": Scenario(
            name="S1",
            experiment_key=key("S1"),
            arms=(Arm("control", 50, 0.10), Arm("treatment", 50, 0.10)),
            users=4000 * scale,
            event_name="signup",
            metrics=(Metric("signups", "signup", "conversion"),),
        ),
        "S2": Scenario(
            name="S2",
            experiment_key=key("S2"),
            arms=(Arm("control", 50, 0.08), Arm("treatment", 50, 0.096)),
            users=10000 * scale,
            event_name="checkout",
            metrics=(Metric("checkouts", "checkout", "conversion"),),
        ),
        "S3": Scenario(
            name="S3",
            experiment_key=key("S3"),
            arms=(
                Arm("control", 30, 0.05, repeat=0.3, values=Values(3.0, 1.0)),
                Arm("treatment", 70, 0.05, repeat=0.3, values=Values(3.2, 1.0)),
            ),
            users=6000 * scale,
            event_name="purchase",
            metrics=(
                Metric("revenue", "purchase", "revenue"),
                Metric("purchases", "purchase", "count"),
            ),
        ),
        "S4": Scenario(
            name="S4",
            experiment_key=key("S4"),
            arms=(
                Arm("control", 34, 0.10),
                Arm("b", 33, 0.10),
                Arm("c", 33, 0.13),
            ),
            users=6000 * scale,
            event_name="click",
            metrics=(Metric("clicks", "click", "conversion"),),
            correction_method="benjamini_hochberg",
        ),
    }


def api_url(environ: Mapping[str, str]) -> str:
    """``CONTROLVAR_API_URL``, the address ``_platform.yml`` sets, or the
    development server's."""
    return environ.get("CONTROLVAR_API_URL", "").strip() or "http://127.0.0.1:8000"


SEED = resolve_seed(os.environ)
SCALE = resolve_scale(os.environ)
SCENARIOS = scenarios(SEED, SCALES[SCALE])
API_URL = api_url(os.environ)


def reproduce(name: str = "", *, seed: int = SEED, scale: str = SCALE) -> str:
    """The one command that runs scenario *name* (every scenario when empty)
    again with *seed*."""
    env = f"GROUND_TRUTH_SEED={seed}"
    if scale != "pr":
        env += f" GROUND_TRUTH_SCALE={scale}"
    command = f"{env} python -m pytest tests/ground_truth/test_replay.py"
    return f"{command} -k {name}" if name else command


# ---------------------------------------------------------------------------
# What a gate found
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Finding:
    """One value that disagrees: where, both values, and the tolerance."""

    where: str
    got: Any
    want: Any
    tolerance: str
    got_label: str = "/results"
    want_label: str = "reference"

    def line(self) -> str:
        return (
            f"{self.where}: {self.got_label} {self.got!r},"
            f" {self.want_label} {self.want!r} ({self.tolerance})"
        )


def close(got: Any, want: Any, *, rel: float = REL, floor: float = 0.0) -> bool:
    """*got* equals *want* within *rel* of the larger, or within *floor*."""
    if got is None or want is None:
        return got is None and want is None
    if isinstance(got, bool) or not isinstance(got, (int, float)):
        return False
    if not (math.isfinite(got) and math.isfinite(want)):
        return got == want
    return abs(got - want) <= max(rel * max(abs(got), abs(want)), floor)


def _exact(found: list[Finding], where: str, got: Any, want: Any, **labels) -> None:
    if type(got) is bool or type(want) is bool:
        same = type(got) is type(want) and got == want
    else:
        same = got == want
    if not same:
        found.append(Finding(where, got, want, "exact", **labels))


def _near(
    found: list[Finding],
    where: str,
    got: Any,
    want: Any,
    *,
    rel: float = REL,
    floor: float = 0.0,
) -> None:
    if not close(got, want, rel=rel, floor=floor):
        tolerance = f"rel {rel:g}" + (f", abs {floor:.3g}" if floor else "")
        found.append(Finding(where, got, want, tolerance))


def _by(items: Any, field: str) -> dict[Any, Mapping[str, Any]]:
    return {
        item.get(field): item
        for item in (items if isinstance(items, list) else [])
        if isinstance(item, Mapping)
    }


def expected_std_dev(values: Sequence[float], analysis: str) -> float | None:
    """The documented ``std_dev`` of one user's outcome (``VariantResult`` in
    the OpenAPI snapshots): ``sqrt(mean * (1 - mean))`` for a proportion, the
    sample standard deviation of the per-user values for a mean; None with no
    user, and for a mean with fewer than two."""
    n = len(values)
    if n == 0:
        return None
    if analysis == "proportion":
        share = math.fsum(values) / n
        return math.sqrt(share * (1 - share))
    if n < 2:
        return None
    return statistics.stdev(values)


def _mean(values: Sequence[float]) -> float:
    return math.fsum(values) / len(values) if values else 0.0


def assignment_answers(records: Sequence[Mapping[str, Any]], key: str) -> int:
    """The assignment answers of experiment *key* that assigned a user."""
    return sum(
        1
        for r in records
        if r["kind"] == "assign"
        and r["exp"] == key
        and 200 <= r["status"] < 300
        and r["assigned"] is True
    )


def g0_findings(
    experiment: contract.ExperimentExpected,
    *,
    answers: int | None = None,
    users: int | None = None,
) -> list[Finding]:
    """G0: answers that are not O-ASSIGN's variant, or not the user's first.
    With *answers* and *users*, also fewer assignment answers than users: a
    server that assigned nobody has no wrong answer to find."""
    found = []
    if answers is not None and users is not None and answers < users:
        found.append(
            Finding(
                "assignment answers",
                answers,
                users,
                "at least one per user",
                got_label="answered",
                want_label="users",
            )
        )
    found += [
        Finding(
            f"user {m.user} (ledger line {m.record})",
            m.answered,
            m.expected,
            "exact",
            got_label="answered",
            want_label="O-ASSIGN",
        )
        for m in experiment.variant_mismatches
    ]
    found += [
        Finding(
            f"user {c.user} (ledger line {c.record}), a later answer",
            c.answered,
            c.expected,
            "exact",
            got_label="answered",
            want_label="their first answer",
        )
        for c in experiment.variant_conflicts
    ]
    return found


def plan_findings(
    plan: generator.Plan, experiment: contract.ExperimentExpected
) -> list[Finding]:
    """G1's precondition: the server stored what the plan sent, so no count
    can agree because nothing arrived."""
    sent = sum(1 for op in plan.ops if op.kind == "track") + sum(
        len(op.body["events"]) for op in plan.ops if op.kind == "batch"
    )
    views = sum(1 for op in plan.ops if op.kind == "assign")
    labels = {"got_label": "ledger", "want_label": "plan"}
    found: list[Finding] = []
    _exact(
        found, "users assigned", experiment.total_users, plan.scenario.users, **labels
    )
    _exact(
        found,
        "events stored",
        experiment.total_events - experiment.exposures,
        sent,
        **labels,
    )
    _exact(
        found, "events stored with no variant", experiment.untagged_events, 0, **labels
    )
    _exact(
        found, "view events stored by /assign", experiment.exposures, views, **labels
    )
    return found


def g1_findings(
    scenario: Scenario, experiment: contract.ExperimentExpected, results: Any
) -> list[Finding]:
    """G1: the counts, means and standard deviations against the ledger."""
    found: list[Finding] = []
    results = results if isinstance(results, Mapping) else {}
    metrics = _by(results.get("metrics"), "metric_name")
    for metric in scenario.metrics:
        result = metrics.get(metric.name)
        if result is None:
            found.append(Finding(f"metric {metric.name!r}", None, "present", "exact"))
            continue
        analysis = reference.ANALYSIS[metric.type]
        _exact(
            found,
            f"metric {metric.name!r}, field 'analysis'",
            result.get("analysis"),
            analysis,
        )
        variants = _by(result.get("variants"), "variant_name")
        for index, name in enumerate(experiment.variants):
            where = f"variant {name!r}, metric {metric.name!r}, field"
            got = variants.get(name)
            if got is None:
                found.append(
                    Finding(f"{where} (the variant)", None, "present", "exact")
                )
                continue
            values = experiment.per_user(name, metric.event_name, metric.type)
            _exact(found, f"{where} 'is_control'", got.get("is_control"), index == 0)
            _exact(found, f"{where} 'sample_size'", got.get("sample_size"), len(values))
            _exact(
                found,
                f"{where} 'conversions'",
                got.get("conversions"),
                experiment.converting_users(name, metric.event_name),
            )
            if analysis == "mean":
                _exact(found, f"{where} 'mean'", got.get("mean"), _mean(values))
            else:
                _near(
                    found,
                    f"{where} 'mean'",
                    got.get("mean"),
                    _mean(values),
                    rel=PROPORTION_MEAN_REL,
                )
            _near(
                found,
                f"{where} 'std_dev'",
                got.get("std_dev"),
                expected_std_dev(values, analysis),
            )
    summary = results.get("summary")
    summary = summary if isinstance(summary, Mapping) else {}
    _exact(
        found,
        "summary 'total_users'",
        summary.get("total_users"),
        experiment.total_users,
    )
    _exact(
        found,
        "summary 'total_events'",
        summary.get("total_events"),
        experiment.total_events,
    )
    _exact(
        found,
        "summary 'total_conversions'",
        summary.get("total_conversions"),
        experiment.total_converting_users(m.event_name for m in scenario.metrics),
    )
    return found


def g2_findings(
    scenario: Scenario, experiment: contract.ExperimentExpected, results: Any
) -> list[Finding]:
    """G2: the closed-form statistics against ``reference``."""
    found: list[Finding] = []
    results = results if isinstance(results, Mapping) else {}
    _exact(
        found,
        "field 'correction_method'",
        results.get("correction_method"),
        scenario.correction_method,
    )
    _exact(
        found,
        "field 'confidence_level'",
        results.get("confidence_level"),
        scenario.confidence_level,
    )
    metrics = _by(results.get("metrics"), "metric_name")
    control = experiment.variants[0]
    for metric in scenario.metrics:
        result = metrics.get(metric.name)
        if result is None:
            found.append(Finding(f"metric {metric.name!r}", None, "present", "exact"))
            continue
        analysis = reference.ANALYSIS[metric.type]
        per_user = {
            name: experiment.per_user(name, metric.event_name, metric.type)
            for name in experiment.variants
        }
        variants = _by(result.get("variants"), "variant_name")
        comparisons = reference.compare(
            per_user, analysis, scenario.correction_method, scenario.confidence_level
        )
        for comparison in comparisons:
            name = comparison.variant
            where = f"variant {name!r}, metric {metric.name!r}, field"
            got = variants.get(name)
            if got is None:
                found.append(
                    Finding(f"{where} (the variant)", None, "present", "exact")
                )
                continue
            floor = 1e-9 * max(
                abs(_mean(per_user[control])), abs(_mean(per_user[name]))
            )
            _near(
                found,
                f"{where} 'p_value'",
                got.get("p_value"),
                comparison.p_value,
                floor=P_FLOOR,
            )
            _near(
                found,
                f"{where} 'adjusted_p_value'",
                got.get("adjusted_p_value"),
                comparison.adjusted_p_value,
                floor=P_FLOOR,
            )
            _near(
                found,
                f"{where} 'absolute_difference'",
                got.get("absolute_difference"),
                comparison.absolute_difference,
                floor=floor,
            )
            if analysis == "mean":
                interval = got.get("absolute_difference_ci")
                want = comparison.absolute_difference_ci
                if want is None or not isinstance(interval, list) or len(interval) != 2:
                    _exact(found, f"{where} 'absolute_difference_ci'", interval, want)
                else:
                    for end, got_end, want_end in zip(
                        ("lower", "upper"), interval, want, strict=True
                    ):
                        _near(
                            found,
                            f"{where} 'absolute_difference_ci' ({end})",
                            got_end,
                            want_end,
                            floor=floor,
                        )
            _exact(
                found,
                f"{where} 'is_significant'",
                got.get("is_significant"),
                comparison.is_significant,
            )
    srm = results.get("srm")
    srm = srm if isinstance(srm, Mapping) else {}
    _near(
        found,
        "srm 'p_value'",
        srm.get("p_value"),
        reference.srm_p(
            [experiment.sample_size(name) for name in experiment.variants],
            experiment.allocations,
        ),
        floor=P_FLOOR,
    )
    return found


# ---------------------------------------------------------------------------
# A replayed scenario
# ---------------------------------------------------------------------------
@dataclass
class Replayed:
    scenario: Scenario
    plan: generator.Plan
    ledger_path: Path
    failure: str | None = None
    server_errors: int = 0
    records: list[dict[str, Any]] | None = None
    expected: contract.Expected | None = None
    results: Any = None

    @property
    def experiment(self) -> contract.ExperimentExpected:
        assert self.expected is not None
        return self.expected.experiments[self.scenario.experiment_key]


def report(
    gate: str,
    replayed: Replayed,
    findings: Sequence[Finding],
    *,
    headline: str = "",
    seed: int = SEED,
    scale: str = SCALE,
) -> str:
    """A failure: the gate, scenario and seed, what disagreed, and how to
    reproduce it."""
    name = replayed.scenario.name
    lines = [
        f"{gate} failed: scenario {name}, seed {seed}, scale {scale}"
        + (f": {headline}" if headline else "")
    ]
    lines += [f"  {finding.line()}" for finding in findings[:SHOWN]]
    if len(findings) > SHOWN:
        lines.append(f"  ... and {len(findings) - SHOWN} more")
    lines.append(
        f"  ledger (synthetic ids only, never uploaded): {replayed.ledger_path}"
    )
    lines.append(
        f"  reproduce, from the repository root against a server started with"
        f" ENVIRONMENT=development on an empty database: {reproduce(name, seed=seed, scale=scale)}"
    )
    return "\n".join(lines)


def g6_message(origin: str | None, *, seed: int = SEED, scale: str = SCALE) -> str:
    """G6's failure: the tree the replay runs from has ``modules/``."""
    return (
        f"G6 failed: seed {seed}, scale {scale}: modules importable (from {origin}):"
        " the replay must run the core profile, a tree with no modules/"
        " (_platform.yml with profile: core removes it)\n"
        f"  reproduce: {reproduce('g6', seed=seed, scale=scale)}"
    )


def setup_message(exc: Exception, *, seed: int = SEED, scale: str = SCALE) -> str:
    """The replay could not start (sign-in or key creation refused)."""
    return (
        f"setup failed: seed {seed}, scale {scale}: {exc}\n"
        f"  reproduce, from the repository root against a server started with"
        f" ENVIRONMENT=development on an empty database: {reproduce(seed=seed, scale=scale)}"
    )


def start_session(
    session: driver.Driver,
    email: str,
    password: str,
    *,
    mask: Callable[[str | None], None],
    seed: int = SEED,
    scale: str = SCALE,
) -> None:
    """Sign in and create the API key; a refusal fails with the seed and the
    command, not a bare ``ReplayError``."""
    try:
        session.sign_in(email, password)
        mask(session.token)
        session.create_api_key()
        mask(session.api_key)
    except driver.ReplayError as exc:
        pytest.fail(setup_message(exc, seed=seed, scale=scale), pytrace=False)


def _completed(gate: str, replayed: Replayed) -> None:
    if replayed.failure is not None:
        pytest.fail(
            report(
                gate,
                replayed,
                [],
                headline=f"the replay did not complete: {replayed.failure}",
            ),
            pytrace=False,
        )


def _say(config: pytest.Config, line: str) -> None:
    """A line in the run's output, past pytest's capture."""
    reporter = config.pluginmanager.get_plugin("terminalreporter")
    if reporter is not None:
        reporter.write_line(line)


def _mask(config: pytest.Config, value: str | None) -> None:
    """Keep *value* out of a GitHub Actions log."""
    if value and os.environ.get("GITHUB_ACTIONS") == "true":
        _say(config, f"::add-mask::{value}")


def summary_text(
    session: driver.Driver, *, seed: int = SEED, scale: str = SCALE
) -> str:
    """The step summary: the seed, then counts and durations only."""
    lines = [
        "### Ground-truth replay",
        "",
        f"Seed {seed}, scale {scale}. Reproduce one scenario: `{reproduce('S2', seed=seed, scale=scale)}`",
        "",
        *session.summary(),
    ]
    return "\n".join(lines) + "\n"


@pytest.fixture(scope="session", autouse=True)
def _announce_the_seed(pytestconfig: pytest.Config) -> None:
    _say(
        pytestconfig,
        f"ground-truth replay: seed {SEED}, scale {SCALE}; GROUND_TRUTH_SEED={SEED} reproduces it",
    )


@pytest.fixture(scope="session")
def live(pytestconfig: pytest.Config) -> Iterator[driver.Driver]:
    """The signed-in driver, with its API key; the step summary at the end."""
    email, password = credentials(os.environ)
    with httpx.Client(base_url=API_URL, timeout=driver.TIMEOUT) as client:
        session = driver.Driver(client)
        try:
            start_session(
                session,
                email,
                password,
                mask=lambda value: _mask(pytestconfig, value),
            )
            yield session
        finally:
            text = summary_text(session)
            for line in text.splitlines():
                _say(pytestconfig, line)
            target = os.environ.get("GITHUB_STEP_SUMMARY")
            if target:
                with open(target, "a", encoding="utf-8") as stream:
                    stream.write(text)


@pytest.fixture(scope="module", params=sorted(SCENARIOS))
def replayed(
    request: pytest.FixtureRequest,
    live: driver.Driver,
    tmp_path_factory: pytest.TempPathFactory,
) -> Replayed:
    """One scenario sent, its ledger read back, its results fetched."""
    scenario = SCENARIOS[request.param]
    plan = generator.simulate(SEED, scenario)
    path = tmp_path_factory.mktemp("ground-truth") / f"{scenario.name}.jsonl"
    replayed = Replayed(scenario=scenario, plan=plan, ledger_path=path)
    before = live.server_errors
    experiment_id = None
    with open(path, "w", encoding="utf-8", newline="\n") as stream:
        try:
            experiment_id = live.replay(
                plan, stream, declared=DECLARED, label=scenario.name
            )
        except (ledger.LedgerRefused, driver.ReplayError) as exc:
            replayed.failure = str(exc)
    if replayed.failure is None and experiment_id is not None:
        try:
            replayed.records = ledger.read_path(path, secrets=live.secrets)
            replayed.expected = contract.valid_events(replayed.records)
            replayed.results = live.results(experiment_id, label=scenario.name)
        except (
            ledger.LedgerRefused,
            contract.ContractRefused,
            driver.ReplayError,
        ) as exc:
            replayed.failure = str(exc)
    replayed.server_errors = live.server_errors - before
    return replayed


# ---------------------------------------------------------------------------
# The gates
# ---------------------------------------------------------------------------
def test_g6_the_replay_runs_the_core_profile() -> None:
    """G6: ``import modules`` fails in the copy the replay runs from."""
    spec = importlib.util.find_spec("modules")
    if spec is not None:
        pytest.fail(g6_message(spec.origin), pytrace=False)


def test_g0_every_assignment_answer_is_o_assign(replayed: Replayed) -> None:
    _completed("G0", replayed)
    assert replayed.records is not None
    answers = assignment_answers(replayed.records, replayed.scenario.experiment_key)
    findings = g0_findings(
        replayed.experiment, answers=answers, users=replayed.scenario.users
    )
    if findings:
        pytest.fail(
            report(
                "G0",
                replayed,
                findings,
                headline=f"variant mismatches: {len(replayed.experiment.variant_mismatches)}"
                f" of {answers} answers",
            ),
            pytrace=False,
        )


def test_g1_counts_and_means_equal_the_ledger(replayed: Replayed) -> None:
    _completed("G1", replayed)
    findings = plan_findings(replayed.plan, replayed.experiment)
    findings += g1_findings(replayed.scenario, replayed.experiment, replayed.results)
    if findings:
        pytest.fail(report("G1", replayed, findings), pytrace=False)


def test_g2_statistics_equal_the_reference(replayed: Replayed) -> None:
    _completed("G2", replayed)
    findings = g2_findings(replayed.scenario, replayed.experiment, replayed.results)
    if findings:
        pytest.fail(report("G2", replayed, findings), pytrace=False)


def test_z_no_answer_is_a_5xx(replayed: Replayed) -> None:
    if replayed.server_errors:
        pytest.fail(
            report(
                "Z",
                replayed,
                [],
                headline=f"5xx seen: {replayed.server_errors}"
                + (f" ({replayed.failure})" if replayed.failure else ""),
            ),
            pytrace=False,
        )
