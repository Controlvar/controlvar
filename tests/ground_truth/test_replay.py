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
S5    2, 30/70, 6,000, balanced within ``country`` (US, DE, FR)      A/A in every group
      (#448): every user assigned one at a time with a context, about
      one in ten again (US 2,000, DE 1,500, FR 1,400, ``other`` 500,
      ``unknown`` 600); about 10% buyers, repeat purchases
====  ============================================================  =====================

The gates, per scenario (SPEC #1544 v1.2, section 3.5):

* **G0** (S1 to S4) every assignment answer, single and batch, is O-ASSIGN's
  variant, from the allocation and order the test sent (never from a GET of
  the experiment), and no user is answered two variants.
* **G0b** (S5, #1897) the split within each group, from the ledger,
  with each user's group the one the context the test sent puts them in
  (``docs/guides/balance-within-groups.md``):

  1. in each group, at every count n of its users in the order the server
     answered them, each variant's count is within ``B * w * (1 - w)`` of
     ``n * w`` (2.1 at 30/70, whose block ``B`` is 10 places), and exactly
     ``n * w`` when n is a multiple of ``B``; each group's line at its last
     multiple of ``B`` is printed with its first breach;
  2. every returning user is answered their first variant, and no user two;
  3. ``GET /api/v1/results/{id}/balanced-groups``, read once after the sends:
     each group's users are the ledger's, and ``users_outside_blocks`` is 0.

  The driver is serial (one request at a time), so the server takes each
  group's users in the ledger's order, one counter step at a time, and (1)
  is the blocks' worst case at every n, never a probability. It proves
  nothing about concurrency.
* **G1g** (S5, #1897) the results by group, from the ledger: every count
  exact, and the overall estimate against the reference computed on the same
  counts (S5 is A/A in every group, which the group sizes alone make
  sensitive to the weights):

  1. ``GET /api/v1/results/{id}?breakdown=country``: per (value, variant), the
     users and converting users of the primary metric equal the ledger's, each
     user in the value their first context gives (``US``, ``DE``, ``FR``,
     ``BR`` and ``us`` are rows of their own; a nested or dotted key is
     ``unknown``), and per variant the rows add up to the answer's headline;
  2. ``GET /api/v1/results/{id}/balanced-groups``: per (group, variant), the
     users and converting users equal the ledger's, each user in the group
     they were balanced in;
  3. its ``overall``, per treatment, equals ``reference.stratified`` on those
     counts within 1e-9, ``is_significant`` exactly, and no group is merged.
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
import statistics
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import driver
import pytest
from ground_truth import contract, generator, ledger, reference
from ground_truth.generator import Arm, Balance, Cohort, Metric, Scenario, Values
from ground_truth.seed import SEED

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
#: S5's share of its users who come back for a second assign, which G0b (2)
#: reads: about one in ten. Every one took the pull-request Replay job past
#: 12 minutes (12.1, its first run), the 12-minute limit set for S5's
#: pull-request Replay.
S5_RETURNING = 0.1


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


def s5_balance(scale: int) -> Balance:
    """S5's ``balance_within`` and its users' contexts. Every group's size is
    a multiple of 10, so at its last user each variant has exactly its share.
    ``other`` and ``unknown`` hold the documented traps: a value matched
    exactly (``us`` is not ``US``), and a nested or dotted key, which is not
    read (docs/guides/balance-within-groups.md)."""
    return Balance(
        attribute="country",
        values=("US", "DE", "FR"),
        cohorts=(
            Cohort("US", {"country": "US"}, 2000 * scale),
            Cohort("DE", {"country": "DE"}, 1500 * scale),
            Cohort("FR", {"country": "FR"}, 1400 * scale),
            Cohort("other", {"country": "BR"}, 400 * scale),
            Cohort("other", {"country": "us"}, 100 * scale),
            Cohort("unknown", {"plan": "free"}, 300 * scale),
            Cohort("unknown", {"user": {"country": "US"}}, 150 * scale),
            Cohort("unknown", {"user.country": "US"}, 150 * scale),
        ),
    )


def scenarios(seed: int, scale: int) -> dict[str, Scenario]:
    """S1 to S5 (the table above), each under its own experiment key."""

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
        # The same conversion in both arms: the plan decides each user's
        # events before the server answers, and a balanced variant depends on
        # a key the server never gives, so only an arm-blind rate is honest.
        "S5": Scenario(
            name="S5",
            experiment_key=key("S5"),
            arms=(
                Arm("control", 30, 0.10, repeat=0.3),
                Arm("treatment", 70, 0.10, repeat=0.3),
            ),
            users=6000 * scale,
            event_name="purchase",
            metrics=(Metric("buyers", "purchase", "conversion"),),
            single_share=1.0,
            sticky_share=S5_RETURNING,
            balance=s5_balance(scale),
        ),
    }


#: Every scenario (G1, G2, Z), those assigned by the hash (G0) and the one
#: balanced within groups (G0b).
ALL = sorted(scenarios(1, 1))
HASHED = sorted(name for name, s in scenarios(1, 1).items() if s.balance is None)
BALANCED = sorted(name for name, s in scenarios(1, 1).items() if s.balance is not None)


def api_url(environ: Mapping[str, str]) -> str:
    """``CONTROLVAR_API_URL``, the address ``_platform.yml`` sets, or the
    development server's."""
    return environ.get("CONTROLVAR_API_URL", "").strip() or "http://127.0.0.1:8000"


SCALE = resolve_scale(os.environ)
SCENARIOS = scenarios(SEED, SCALES[SCALE])
API_URL = api_url(os.environ)


def reproduce(
    name: str = "",
    *,
    seed: int = SEED,
    scale: str = SCALE,
    path: str = "tests/ground_truth/test_replay.py",
) -> str:
    """The one command that runs scenario *name* (every scenario when empty)
    of the test file *path* again with *seed*."""
    env = f"GROUND_TRUTH_SEED={seed}"
    if scale != "pr":
        env += f" GROUND_TRUTH_SCALE={scale}"
    command = f"{env} python -m pytest {path}"
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
# G0b: the split within groups (S5)
# ---------------------------------------------------------------------------
def _n(value: Any) -> str:
    """A count as a G0b line prints it (``1,431``); anything else as itself."""
    if isinstance(value, int) and not isinstance(value, bool):
        return f"{value:,}"
    return repr(value)


def _hundredths(value: int) -> str:
    """*value* hundredths as a decimal: 210 is ``2.1``, 300 is ``3``."""
    whole, part = divmod(abs(value), 100)
    text = f"{whole:,}" + (f".{part:02d}".rstrip("0") if part else "")
    return f"-{text}" if value < 0 else text


def block_size(shares: Sequence[int]) -> int:
    """The places of one block: ``100 / gcd`` of the shares (10 at 30/70)."""
    return 100 // math.gcd(100, *shares)


def arrivals(
    records: Sequence[Mapping[str, Any]],
    key: str,
    groups: Mapping[str, str],
    order: Sequence[str] = (),
) -> dict[str, list[str]]:
    """Per group, the variant of each user's first assignment answer, in the
    ledger's order, which is the order the server answered them: *groups*
    gives each user's group (``generator.user_groups``); a user it does not
    name is in ``"(not sent)"``. Every group of *order* is present, first."""
    found: dict[str, list[str]] = {group: [] for group in order}
    seen: set[str] = set()
    for r in records:
        if r["kind"] != "assign" or r["exp"] != key:
            continue
        if not 200 <= r["status"] < 300 or r["assigned"] is not True:
            continue
        if r["user"] in seen:
            continue
        seen.add(r["user"])
        group = groups.get(r["user"], "(not sent)")
        found.setdefault(group, []).append(r["variant"])
    return found


def _off_their_share(
    name: str,
    group: str,
    n: int,
    counts: Mapping[str, int],
    allocations: Sequence[tuple[str, int]],
    block: int,
) -> list[str]:
    """The variants whose count after the group's *n*-th user is off: not
    exactly ``n * w`` at a multiple of *block*, else more than
    ``block * w * (1 - w)`` from it. In hundredths, so exact."""
    lines = []
    for arm, share in allocations:
        got = counts.get(arm, 0)
        off = 100 * got - n * share
        if n % block == 0:
            if off:
                lines.append(
                    f"{name} group {group}: {arm} {_n(got)} != {_n(n * share // 100)}"
                    f" at n={_n(n)}"
                )
            continue
        bound = (block * share // 100) * (100 - share)
        if abs(off) > bound:
            lines.append(
                f"{name} group {group}: {arm} {_n(got)} at n={_n(n)} is"
                f" {_hundredths(abs(off))} from {_hundredths(n * share)}"
                f" (at most {_hundredths(bound)})"
            )
    return lines


def g0b_balance_lines(
    name: str,
    found: Mapping[str, Sequence[str]],
    allocations: Sequence[tuple[str, int]],
    planned: Mapping[str, int],
) -> list[str]:
    """G0b (1): per group (*found*, from ``arrivals``), its users answered
    against the users the plan sends it (*planned*), then each variant's
    count at every n. A group stops at its first breach, and its line at its
    last multiple of the block is printed too (n = 2,000 for S5's US)."""
    block = block_size([share for _, share in allocations])
    lines: list[str] = []
    for group in [*planned, *(g for g in found if g not in planned)]:
        sequence = found.get(group, [])
        if len(sequence) != planned.get(group, 0):
            lines.append(
                f"{name} group {group}: {_n(len(sequence))} users answered,"
                f" the plan sends {_n(planned.get(group, 0))}"
            )
        counts: Counter[str] = Counter()
        first: list[str] = []
        last = len(sequence) // block * block
        for n, variant in enumerate(sequence, 1):
            counts[variant] += 1
            if not first:
                first = _off_their_share(name, group, n, counts, allocations, block)
                lines += first
            if n == last and last > 0:
                lines += [
                    line
                    for line in _off_their_share(
                        name, group, n, counts, allocations, block
                    )
                    if line not in first
                ]
    return lines


def returning_answers(records: Sequence[Mapping[str, Any]], key: str) -> int:
    """Assignment answers to a user already answered: the returning users."""
    seen: set[str] = set()
    again = 0
    for r in records:
        if r["kind"] != "assign" or r["exp"] != key:
            continue
        if not 200 <= r["status"] < 300 or r["assigned"] is not True:
            continue
        if r["user"] in seen:
            again += 1
        seen.add(r["user"])
    return again


def returning_sends(plan: generator.Plan) -> int:
    """Assigns of the plan to a user it assigned before."""
    seen: set[str] = set()
    again = 0
    for op in plan.ops:
        if op.kind == "assign":
            users = [op.body["user_id"]]
        elif op.kind == "assign_batch":
            users = [entry["user_id"] for entry in op.body["users"]]
        else:
            continue
        for user in users:
            again += user in seen
            seen.add(user)
    return again


def g0b_returning_lines(
    name: str,
    experiment: contract.ExperimentExpected,
    answered: int,
    sent: int,
) -> list[str]:
    """G0b (2): every returning user the plan sends is answered (*answered*
    of *sent*), and with their first variant; no user is answered two."""
    lines = []
    if answered != sent:
        lines.append(
            f"{name}: {_n(answered)} returning users answered, the plan sends {_n(sent)}"
        )
    lines += [
        f"{name}: user {c.user} (ledger line {c.record}) answered {c.answered!r},"
        f" first {c.expected!r}"
        for c in experiment.variant_conflicts
    ]
    return lines


def g0b_groups_lines(
    name: str, balanced: Any, found: Mapping[str, Sequence[str]]
) -> list[str]:
    """G0b (3): ``GET .../balanced-groups``'s users per group against the
    ledger's (*found*, from ``arrivals``), and its ``users_outside_blocks``
    against 0."""
    body = balanced if isinstance(balanced, Mapping) else {}
    rows = body.get("groups")
    segments = rows.get("segments") if isinstance(rows, Mapping) else None
    lines = []
    if not isinstance(segments, list):
        lines.append(f"{name}: balanced-groups answered no group rows")
        segments = []
    served = {
        row.get("segment_value"): row.get("sample_size")
        for row in segments
        if isinstance(row, Mapping)
    }
    want = {group: len(sequence) for group, sequence in found.items()}
    for group in [*want, *(g for g in served if g not in want)]:
        got, expected = served.get(group, 0), want.get(group, 0)
        if got != expected:
            lines.append(
                f"{name}: {group} has {_n(got)} users, expected {_n(expected)}"
            )
    outside = body.get("users_outside_blocks")
    if isinstance(outside, bool) or outside != 0:
        lines.append(f"{name}: users_outside_blocks {_n(outside)}, expected 0")
    return lines


# ---------------------------------------------------------------------------
# G1g: the results by group (S5): ?breakdown= and /balanced-groups
# ---------------------------------------------------------------------------
#: ``overall``'s tolerance against ``reference.stratified`` on the same counts.
OVERALL_ABS = 1e-9
#: How many values a breakdown lists by themselves; the rest are ``other``
#: (``docs/api/dimensional-analysis.md``, "Where the segments come from").
BREAKDOWN_LISTED = 20
OTHER, UNKNOWN = "other", "unknown"

#: One group's users and converting users per variant.
GroupCounts = dict[str, dict[str, tuple[int, int]]]


def first_context_values(plan: generator.Plan, attribute: str) -> dict[str, str | None]:
    """Each user's value of *attribute* as ``?breakdown=`` reads it: the
    top-level key of the context of the user's first assign (a nested or
    dotted key is not read), when it is text that is not empty; else None.
    ``/assign`` stores that context as the user's exposure, which a breakdown
    reads before any other event (``docs/api/dimensional-analysis.md``). The
    product reads the first event that carries a value; the two agree for S5,
    because a returning user sends the same context (``generator.user_groups``
    raises on a user sent in two groups)."""
    values: dict[str, str | None] = {}
    for op in plan.ops:
        if op.kind == "assign":
            sent = [op.body]
        elif op.kind == "assign_batch":
            sent = list(op.body["users"])
        else:
            continue
        for entry in sent:
            if entry["user_id"] in values:
                continue
            context = entry.get("context")
            value = context.get(attribute) if isinstance(context, Mapping) else None
            if value is not None and not isinstance(value, str):
                raise ValueError(
                    f"user {entry['user_id']}: {attribute} is not text; the"
                    " breakdown's reading of it is not modelled"
                )
            values[entry["user_id"]] = value or None
    return values


def breakdown_rows(values: Mapping[str, str | None]) -> dict[str, str]:
    """Each user's row of a breakdown: no value is ``unknown``; the
    ``BREAKDOWN_LISTED`` values with the most users (then by value) are their
    own rows; every other value is ``other``."""
    users = Counter(v for v in values.values() if v not in (None, UNKNOWN, OTHER))
    ranked = sorted(users.items(), key=lambda item: (-item[1], item[0]))
    listed = {value for value, _ in ranked[:BREAKDOWN_LISTED]}
    return {
        user: UNKNOWN
        if value is None or value == UNKNOWN
        else value
        if value in listed
        else OTHER
        for user, value in values.items()
    }


def group_counts(
    experiment: contract.ExperimentExpected,
    event_name: str,
    row_of: Mapping[str, str],
) -> GroupCounts:
    """Per row, per variant, from the ledger: the assigned users, and those of
    them with at least one counting event of *event_name*."""
    counts: GroupCounts = {}
    for variant in experiment.variants:
        converting = experiment.counting.get((variant, event_name), {})
        for user in experiment.users[variant]:
            row = counts.setdefault(row_of.get(user, "(not sent)"), {})
            users, converted = row.get(variant, (0, 0))
            row[variant] = (
                users + 1,
                converted + (1 if converting.get(user, (0, ()))[0] > 0 else 0),
            )
    return counts


def _served_rows(body: Any) -> dict[tuple[str, str], tuple[Any, Any]] | None:
    """(group, variant) -> (sample_size, conversions) of a breakdown's
    ``segments``; None without rows."""
    segments = body.get("segments") if isinstance(body, Mapping) else None
    if not isinstance(segments, list):
        return None
    served: dict[tuple[str, str], tuple[Any, Any]] = {}
    for segment in segments:
        if not isinstance(segment, Mapping):
            continue
        for row in segment.get("variants") or []:
            if isinstance(row, Mapping):
                served[(segment.get("segment_value"), row.get("variant_name"))] = (
                    row.get("sample_size"),
                    row.get("conversions"),
                )
    return served


def _count_lines(
    name: str,
    what: str,
    served: Mapping[tuple[str, str], tuple[Any, Any]],
    want: GroupCounts,
    variants: Sequence[str],
) -> list[str]:
    """One line per (group, variant) whose users or converting users differ
    from the ledger's, the groups the ledger lacks included."""
    lines = []
    groups = [*want, *dict.fromkeys(g for g, _ in served if g not in want)]
    for group in groups:
        for variant in variants:
            users, converted = want.get(group, {}).get(variant, (0, 0))
            got_users, got_converted = served.get((group, variant), (0, None))
            if got_users != users:
                lines.append(
                    f"{name} {what} {group}/{variant} users {_n(got_users)}"
                    f" != {_n(users)}"
                )
            if got_converted != converted:
                lines.append(
                    f"{name} {what} {group}/{variant} conversions"
                    f" {_n(got_converted)} != {_n(converted)}"
                )
    return lines


def g1g_breakdown_lines(
    scenario: Scenario,
    experiment: contract.ExperimentExpected,
    plan: generator.Plan,
    results: Any,
) -> list[str]:
    """G1g (1): ``GET /results/{id}?breakdown=<attribute>``: per (value,
    variant), the users and converting users of the primary metric equal the
    ledger's, with each user in the value their first context gives
    (``first_context_values``); and per variant the rows add up to the
    headline of the same answer."""
    assert scenario.balance is not None
    name = scenario.name
    body = results.get("breakdown") if isinstance(results, Mapping) else None
    found = _served_rows(body)
    if found is None:
        return [f"{name} breakdown: the results answered no breakdown rows"]
    served = found
    metric = scenario.metrics[0]
    row_of = breakdown_rows(first_context_values(plan, scenario.balance.attribute))
    lines = _count_lines(
        name,
        "breakdown",
        served,
        group_counts(experiment, metric.event_name, row_of),
        experiment.variants,
    )
    headline = _by(
        _by(results.get("metrics"), "metric_name").get(metric.name, {}).get("variants"),
        "variant_name",
    )
    for variant in experiment.variants:
        rows = [v for (_, vname), v in served.items() if vname == variant]
        for index, label in ((0, "sample_size"), (1, "conversions")):
            added = sum(v[index] for v in rows if isinstance(v[index], int))
            top = headline.get(variant, {}).get(label)
            if added != top:
                lines.append(
                    f"{name} breakdown {variant}: rows add up to {_n(added)}"
                    f" {label}, the headline {_n(top)}"
                )
    return lines


def g1g_balanced_lines(
    scenario: Scenario,
    experiment: contract.ExperimentExpected,
    plan: generator.Plan,
    balanced: Any,
) -> list[str]:
    """G1g (2): ``GET /results/{id}/balanced-groups``: per (group, variant),
    the users and converting users of the primary metric equal the ledger's,
    with each user in the group the context they were sent puts them in."""
    assert scenario.balance is not None
    name = scenario.name
    found = _served_rows(
        balanced.get("groups") if isinstance(balanced, Mapping) else None
    )
    if found is None:
        return [f"{name}: balanced-groups answered no group rows"]
    return _count_lines(
        name,
        "balanced-groups",
        found,
        group_counts(
            experiment, scenario.metrics[0].event_name, generator.user_groups(plan)
        ),
        experiment.variants,
    )


def _within(got: Any, want: float | None, tolerance: float) -> bool:
    if want is None or got is None:
        return want is None and got is None
    if isinstance(got, bool) or not isinstance(got, (int, float)):
        return False
    return abs(got - want) <= tolerance


def g1g_overall_lines(
    scenario: Scenario,
    experiment: contract.ExperimentExpected,
    plan: generator.Plan,
    balanced: Any,
) -> list[str]:
    """G1g (3): ``overall`` of ``GET /results/{id}/balanced-groups`` against
    ``reference.stratified`` on the same counts (the ledger's, per group), each
    field within ``OVERALL_ABS`` (``is_significant`` exactly), and no group
    merged (the reference does not model a merge)."""
    name = scenario.name
    body = balanced if isinstance(balanced, Mapping) else {}
    overall = body.get("overall")
    if not isinstance(overall, list):
        return [
            f"{name} overall: {overall!r} (reason {body.get('reason')!r}),"
            " expected one estimate per treatment"
        ]
    lines = []
    if body.get("merged_groups"):
        lines.append(f"{name} overall: merged_groups {body.get('merged_groups')!r}")
    counts = group_counts(
        experiment, scenario.metrics[0].event_name, generator.user_groups(plan)
    )
    control, *treatments = experiment.variants
    wanted = {
        t: reference.stratified(
            list(counts.values()), control, t, scenario.confidence_level
        )
        for t in treatments
    }
    adjusted = reference.adjust(
        [wanted[t].p_value for t in treatments], scenario.correction_method
    )
    served = _by(overall, "variant_name")
    alpha = 1 - scenario.confidence_level
    for treatment, q in zip(treatments, adjusted, strict=True):
        want, got = wanted[treatment], served.get(treatment)
        tag = f"{name} overall" + (f" {treatment}" if len(treatments) > 1 else "")
        if got is None:
            lines.append(f"{tag}: no estimate for {treatment}")
            continue
        interval = got.get("absolute_difference_ci")
        ends = (
            [None, None]
            if interval is None
            else list(interval)
            if isinstance(interval, list) and len(interval) == 2
            else ["?", "?"]
        )
        decisive = q if q is not None else want.p_value
        checks = [
            (
                "absolute_difference",
                got.get("absolute_difference"),
                want.absolute_difference,
            ),
            ("standard_error", got.get("standard_error"), want.standard_error),
            (
                "relative_improvement_pct",
                got.get("relative_improvement_pct"),
                want.relative_improvement_pct,
            ),
            ("p_value", got.get("p_value"), want.p_value),
            ("adjusted_p_value", got.get("adjusted_p_value"), q),
            (
                "absolute_difference_ci lower",
                ends[0],
                want.absolute_difference_ci and want.absolute_difference_ci[0],
            ),
            (
                "absolute_difference_ci upper",
                ends[1],
                want.absolute_difference_ci and want.absolute_difference_ci[1],
            ),
        ]
        for field_name, value, expected in checks:
            if not _within(value, expected, OVERALL_ABS):
                lines.append(f"{tag} {field_name} {value!r} != reference {expected!r}")
        significant = decisive is not None and decisive < alpha
        if got.get("is_significant") is not significant:
            lines.append(
                f"{tag} is_significant {got.get('is_significant')!r}"
                f" != reference {significant!r}"
            )
    return lines


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
    #: ``GET .../balanced-groups``, for a scenario that balances within groups.
    balanced: Any = None
    #: ``GET .../results/{id}?breakdown=<attribute>``, for the same scenarios.
    broken_down: Any = None

    @property
    def experiment(self) -> contract.ExperimentExpected:
        assert self.expected is not None
        return self.expected.experiments[self.scenario.experiment_key]


def report(
    gate: str,
    replayed: Replayed,
    findings: Sequence[Finding | str],
    *,
    headline: str = "",
    seed: int = SEED,
    scale: str = SCALE,
) -> str:
    """A failure: the gate, scenario and seed, what disagreed (a ``Finding``,
    or a G0b line as it is), and how to reproduce it."""
    name = replayed.scenario.name
    lines = [
        f"{gate} failed: scenario {name}, seed {seed}, scale {scale}"
        + (f": {headline}" if headline else "")
    ]
    lines += [
        f"  {finding if isinstance(finding, str) else finding.line()}"
        for finding in findings[:SHOWN]
    ]
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


class Replays:
    """Each scenario, sent once, when a test first asks for it: G0 and G0b
    take different scenarios, so a module-scoped parametrised fixture would
    be torn down and sent again between them."""

    def __init__(self, live: driver.Driver, factory: pytest.TempPathFactory) -> None:
        self._live = live
        self._factory = factory
        self._done: dict[str, Replayed] = {}

    def get(self, name: str) -> Replayed:
        if name not in self._done:
            self._done[name] = replay_scenario(
                SCENARIOS[name], self._live, self._factory
            )
        return self._done[name]


@pytest.fixture(scope="module")
def replays(live: driver.Driver, tmp_path_factory: pytest.TempPathFactory) -> Replays:
    return Replays(live, tmp_path_factory)


@pytest.fixture
def replayed(request: pytest.FixtureRequest, replays: Replays) -> Replayed:
    """The scenario the test is parametrised with, sent once per run."""
    return replays.get(request.param)


def replay_scenario(
    scenario: Scenario,
    live: driver.Driver,
    tmp_path_factory: pytest.TempPathFactory,
) -> Replayed:
    """One scenario sent, its ledger read back, its results fetched (and, for
    a scenario that balances within groups, its balanced groups)."""
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
            if scenario.balance is not None:
                replayed.balanced = live.balanced_groups(
                    experiment_id, label=scenario.name
                )
                replayed.broken_down = live.breakdown(
                    experiment_id, scenario.balance.attribute, label=scenario.name
                )
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


@pytest.mark.parametrize("replayed", HASHED, indirect=True)
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


def _arrivals(replayed: Replayed) -> dict[str, list[str]]:
    assert replayed.records is not None and replayed.scenario.balance is not None
    return arrivals(
        replayed.records,
        replayed.scenario.experiment_key,
        generator.user_groups(replayed.plan),
        replayed.scenario.balance.groups(),
    )


@pytest.mark.parametrize("replayed", BALANCED, indirect=True)
def test_g0b1_each_group_is_balanced_at_every_prefix(replayed: Replayed) -> None:
    _completed("G0b", replayed)
    scenario = replayed.scenario
    assert scenario.balance is not None
    planned = Counter(generator.user_groups(replayed.plan).values())
    lines = g0b_balance_lines(
        scenario.name,
        _arrivals(replayed),
        replayed.plan.allocations(),
        {group: planned[group] for group in scenario.balance.groups()},
    )
    if lines:
        pytest.fail(
            report("G0b (1)", replayed, lines, headline="a group is off its blocks"),
            pytrace=False,
        )


@pytest.mark.parametrize("replayed", BALANCED, indirect=True)
def test_g0b2_a_returning_user_keeps_their_variant(replayed: Replayed) -> None:
    _completed("G0b", replayed)
    assert replayed.records is not None
    lines = g0b_returning_lines(
        replayed.scenario.name,
        replayed.experiment,
        returning_answers(replayed.records, replayed.scenario.experiment_key),
        returning_sends(replayed.plan),
    )
    if lines:
        pytest.fail(report("G0b (2)", replayed, lines), pytrace=False)


@pytest.mark.parametrize("replayed", BALANCED, indirect=True)
def test_g0b3_the_server_s_groups_are_the_ledger_s(replayed: Replayed) -> None:
    _completed("G0b", replayed)
    lines = g0b_groups_lines(
        replayed.scenario.name, replayed.balanced, _arrivals(replayed)
    )
    if lines:
        pytest.fail(report("G0b (3)", replayed, lines), pytrace=False)


@pytest.mark.parametrize("replayed", BALANCED, indirect=True)
def test_g1g1_the_breakdown_rows_equal_the_ledger(replayed: Replayed) -> None:
    _completed("G1g", replayed)
    lines = g1g_breakdown_lines(
        replayed.scenario, replayed.experiment, replayed.plan, replayed.broken_down
    )
    if lines:
        pytest.fail(
            report("G1g (1)", replayed, lines, headline="?breakdown= rows"),
            pytrace=False,
        )


@pytest.mark.parametrize("replayed", BALANCED, indirect=True)
def test_g1g2_the_balanced_groups_rows_equal_the_ledger(replayed: Replayed) -> None:
    _completed("G1g", replayed)
    lines = g1g_balanced_lines(
        replayed.scenario, replayed.experiment, replayed.plan, replayed.balanced
    )
    if lines:
        pytest.fail(
            report("G1g (2)", replayed, lines, headline="balanced-groups rows"),
            pytrace=False,
        )


@pytest.mark.parametrize("replayed", BALANCED, indirect=True)
def test_g1g3_the_overall_estimate_equals_the_stratified_reference(
    replayed: Replayed,
) -> None:
    _completed("G1g", replayed)
    lines = g1g_overall_lines(
        replayed.scenario, replayed.experiment, replayed.plan, replayed.balanced
    )
    if lines:
        pytest.fail(
            report("G1g (3)", replayed, lines, headline="balanced-groups overall"),
            pytrace=False,
        )


@pytest.mark.parametrize("replayed", ALL, indirect=True)
def test_g1_counts_and_means_equal_the_ledger(replayed: Replayed) -> None:
    _completed("G1", replayed)
    findings = plan_findings(replayed.plan, replayed.experiment)
    findings += g1_findings(replayed.scenario, replayed.experiment, replayed.results)
    if findings:
        pytest.fail(report("G1", replayed, findings), pytrace=False)


@pytest.mark.parametrize("replayed", ALL, indirect=True)
def test_g2_statistics_equal_the_reference(replayed: Replayed) -> None:
    _completed("G2", replayed)
    findings = g2_findings(replayed.scenario, replayed.experiment, replayed.results)
    if findings:
        pytest.fail(report("G2", replayed, findings), pytrace=False)


@pytest.mark.parametrize("replayed", ALL, indirect=True)
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
