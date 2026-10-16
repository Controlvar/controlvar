"""
Domain-specific synthetic data engine for the Controlvar platform.

Generates statistically realistic experimentation scenarios — not just valid
shapes but data that behaves like the real world: baseline conversion rates,
novelty effects, outliers, day-of-week patterns, and intentional edge cases.

Reproducible: a scenario's seed decides every draw (the users, their ids, dates
and conversions) and the key of the experiment it is seeded into, and its dates
start on a fixed day, so one scenario and seed give the same data on every run.

The platform picks each user's variant. The seeder converts a user by the
variant ``POST /api/v1/tracking/assign`` answered, so each variant's configured
rate is the rate of the users the platform counts in it. The dry run plans the
split with the hash the server assigns by, on the same experiment key, so the
counts and the p-value it prints are the ones the platform computes when every
user is assigned (no global holdout).

Usage, from the repository root (standalone dry-run):
    python -m backend.tests.realistic.data_generator --scenario ab_test_lifecycle --dry-run

Usage (seed into running API; .claude/agents/data-generator.md shows how to get
the token and the API key):
    python -m backend.tests.realistic.data_generator --scenario ab_test_lifecycle
        --api-url http://localhost:8000 --token <JWT> --api-key <API key>
"""

from __future__ import annotations

import argparse
import math
import random
import sys
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Optional

import requests

from backend.app.core.consistent_hash import bucket_of

#: Day 0 of a scenario unless it is given another: Monday 2026-01-05, 00:00
#: UTC. A fixed date rather than one counted back from the moment of the run:
#: the day-of-week effect reads each user's weekday, so with a start that moved
#: with the run date one seed gave each user another weekday, and other
#: conversions, on another day. A Monday start puts a 14- or 21-day scenario on
#: whole calendar weeks. The platform does not filter conversions by date, so
#: dates in the past are counted like any other.
DEFAULT_START_DATE = datetime(2026, 1, 5, tzinfo=timezone.utc)

#: The event the seeded experiment's primary metric counts.
CONVERSION_EVENT = "purchase"

#: The share of late control users the ``metric_drift`` edge case gives an
#: extra conversion.
DRIFT_RATE = 0.10

#: The significance level of the platform's default analysis (confidence 0.95).
ALPHA = 0.05

#: The two-sided normal critical value at ``ALPHA``.
Z_CRITICAL = 1.959963984540054

# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------


@dataclass
class UserEvent:
    user_id: str
    variant_name: str
    event_name: str
    value: float
    # None: the server stamps the event with its own clock.
    timestamp: Optional[datetime]
    properties: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class UserDraws:
    """One user's random draws, made once from the scenario's seed.

    Whichever variant the platform puts the user in, these decide their events
    (``DataScenario.events_for``). The draws never depend on the platform's
    answer, so the same seed gives the same draws, and a user's outcome in
    each variant is fixed before the platform assigns them.
    """

    conversion: float  # converts when below the variant's effective rate
    outlier: float  # an outlier when below the scenario's outlier_rate
    outlier_value: float  # the purchase value of an outlier who converts
    event_hours: float  # hours from assignment to the purchase
    drift: float  # the metric_drift edge case's draw


@dataclass
class SimulatedUser:
    user_id: str
    # The planned variant: the platform's assignment for a generated scenario
    # (see DataScenario.planned_variant); the seeder uses the server's answer.
    variant_name: str
    assigned_at: datetime
    properties: dict[str, Any] = field(default_factory=dict)
    # None for a row that fires no events of its own (the zero_events_user
    # ghost; the multi_assignment repeat, which the seeder skips for the
    # user's first row) and for a user built by hand.
    draws: Optional[UserDraws] = None


@dataclass
class ScenarioResult:
    scenario_name: str
    users: list[SimulatedUser]
    events: list[UserEvent]
    control_cvr: float
    treatment_cvr: float
    expected_significant: bool
    edge_cases_injected: list[str]
    metadata: dict[str, Any] = field(default_factory=dict)
    # The key of the experiment the seeder creates; None for a result built by
    # hand, which the seeder gives a random one.
    experiment_key: Optional[str] = None
    seed: Optional[int] = None
    # The events a user fires in a given variant. The seeder calls it with the
    # variant the platform assigned. None for a result built by hand: the
    # seeder then sends the listed events as they are.
    events_for: Optional[Callable[[SimulatedUser, str], list[UserEvent]]] = field(
        default=None, repr=False, compare=False
    )

    def split(self) -> dict[str, dict[str, int]]:
        """Users and converting users per planned variant, as the platform counts.

        Each user counts once, in the variant of their first row (the platform's
        assignment is sticky); a converting user has at least one
        ``CONVERSION_EVENT``.
        """
        converting = {
            e.user_id for e in self.events if e.event_name == CONVERSION_EVENT
        }
        counts: dict[str, dict[str, int]] = {}
        seen: set[str] = set()
        for user in self.users:
            if user.user_id in seen:
                continue
            seen.add(user.user_id)
            arm = counts.setdefault(
                user.variant_name, {"users": 0, "converting_users": 0}
            )
            arm["users"] += 1
            if user.user_id in converting:
                arm["converting_users"] += 1
        return counts

    def summary(self) -> str:
        split = self.split()
        control = split.get("control", {"users": 0, "converting_users": 0})
        treatment = split.get("treatment", {"users": 0, "converting_users": 0})
        n_users = sum(arm["users"] for arm in split.values())
        repeated = len(self.users) - n_users

        def rate(arm: dict[str, int]) -> float:
            return arm["converting_users"] / arm["users"] if arm["users"] else 0.0

        header = f"Scenario: {self.scenario_name}"
        if self.seed is not None or self.experiment_key:
            header += f" (seed {self.seed}, experiment key {self.experiment_key})"
        lines = [
            header,
            f"  Users: {n_users} total "
            f"({control['users']} control, {treatment['users']} treatment)"
            + (f"; {repeated} listed again in the other variant" if repeated else ""),
            f"  Events: {len(self.events)} total",
            f"  Control CVR: {rate(control):.3f} "
            f"({control['converting_users']} converting; base rate {self.control_cvr:.3f})",
            f"  Treatment CVR: {rate(treatment):.3f} "
            f"({treatment['converting_users']} converting; base rate {self.treatment_cvr:.3f})",
        ]
        predicted = self.metadata.get("predicted_p_value")
        if predicted is not None:
            lines.append(
                f"  Predicted p-value: {predicted:.4g} "
                "(Fisher's exact, two-sided, on these counts: the platform's test)"
            )
        lines.append(f"  Expected significant: {self.expected_significant}")
        power = self.metadata.get("power")
        if power is not None:
            lines.append(
                f"  Power at the generated rates: {power:.2f} "
                "(normal approximation, a 50/50 split, alpha 0.05)"
            )
        lines.append(f"  Edge cases: {self.edge_cases_injected or 'none'}")
        return "\n".join(lines) + "\n"


def fisher_p_value(split: dict[str, dict[str, int]]) -> Optional[float]:
    """Fisher's exact two-sided p-value of treatment against control.

    The test the platform's results API reports for a conversion metric
    (``binomial_variant_results``), on converting users out of assigned users.
    None unless both variants have users.
    """
    control = split.get("control")
    treatment = split.get("treatment")
    if not control or not treatment or not control["users"] or not treatment["users"]:
        return None
    from scipy import stats

    table = [
        [
            treatment["converting_users"],
            treatment["users"] - treatment["converting_users"],
        ],
        [control["converting_users"], control["users"] - control["converting_users"]],
    ]
    return float(stats.fisher_exact(table)[1])


def _normal_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


# ---------------------------------------------------------------------------
# Core data generator
# ---------------------------------------------------------------------------


class DataScenario:
    """
    Generates statistically realistic experiment data.

    Parameters
    ----------
    name : str
        Scenario identifier used for seeding and reporting.
    users : int
        Total simulated user population. The platform splits it 50/50 by its
        assignment hash, so each variant gets about half.
    control_cvr : float
        Baseline conversion rate for the control group (0.0–1.0), before the
        modifiers below.
    treatment_cvr : float
        Conversion rate for the treatment group.  If > control_cvr, the
        experiment should show a positive lift.
    novelty_decay_days : int
        Number of days over which the treatment CVR exhibits a novelty spike
        (day-1 boost that decays to steady-state).  0 = no novelty effect.
    outlier_rate : float
        Fraction of users who produce extreme values (bots, whales).
    inject_edge_cases : list[str]
        Named edge cases to inject into the dataset.  Supported:
          - "zero_events_user": a user who never fires any event
          - "multi_assignment": same user assigned to both variants
          - "metric_drift": gradual baseline shift over time
          - "simpsons_paradox": subgroup reversal of aggregate trend
    day_of_week_effect : bool
        If True, apply weekday (1.2×) vs weekend (0.7×) multiplier.
    session_decay : bool
        If True, engagement drops 15% per week into the experiment.
    seed : int | None
        Seed for every random draw, and part of the default experiment key.
        None draws from the operating system: a different population each run.
    start_date : datetime | None
        Day 0 of the experiment; ``DEFAULT_START_DATE`` when None.
    """

    EDGE_CASES = (
        "zero_events_user",
        "multi_assignment",
        "metric_drift",
        "simpsons_paradox",
    )

    def __init__(
        self,
        name: str = "default",
        users: int = 1_000,
        control_cvr: float = 0.08,
        treatment_cvr: float = 0.095,
        novelty_decay_days: int = 0,
        outlier_rate: float = 0.02,
        inject_edge_cases: Optional[list[str]] = None,
        day_of_week_effect: bool = False,
        session_decay: bool = False,
        experiment_duration_days: int = 14,
        seed: Optional[int] = None,
        start_date: Optional[datetime] = None,
    ):
        self.name = name
        self.users = users
        self.control_cvr = control_cvr
        self.treatment_cvr = treatment_cvr
        self.novelty_decay_days = novelty_decay_days
        self.outlier_rate = outlier_rate
        self.inject_edge_cases = inject_edge_cases or []
        self.day_of_week_effect = day_of_week_effect
        self.session_decay = session_decay
        self.experiment_duration_days = experiment_duration_days
        self.seed = seed
        self.start_date = start_date or DEFAULT_START_DATE

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def generate(self, experiment_key: Optional[str] = None) -> ScenarioResult:
        """Run the full simulation and return a ScenarioResult.

        Every call starts a fresh random stream from ``seed``, so a scenario
        generates the same result each time it is asked. ``experiment_key`` is
        the key the seeder creates the experiment under (default
        ``realistic-<name>-seed<seed>``); the planned split depends on it.
        """
        rng = random.Random(self.seed)
        if experiment_key is None:
            suffix = (
                f"seed{self.seed}"
                if self.seed is not None
                else f"{rng.getrandbits(24):06x}"
            )
            experiment_key = f"realistic-{self.name}-{suffix}"

        users = self._make_users(rng, experiment_key)
        events = [e for user in users for e in self.events_for(user, user.variant_name)]

        injected: list[str] = []
        for ec in self.inject_edge_cases:
            if ec == "zero_events_user":
                self._inject_zero_events_user(rng, users, experiment_key)
            elif ec == "multi_assignment":
                self._inject_multi_assignment(rng, users)
            elif ec not in self.EDGE_CASES:
                continue
            # metric_drift and simpsons_paradox are rules of events_for, so
            # they follow whichever variant the platform assigns.
            injected.append(ec)

        result = ScenarioResult(
            scenario_name=self.name,
            users=users,
            events=events,
            control_cvr=self.control_cvr,
            treatment_cvr=self.treatment_cvr,
            expected_significant=False,
            edge_cases_injected=injected,
            experiment_key=experiment_key,
            seed=self.seed,
            events_for=self.events_for,
        )

        # The prediction: the platform's test on the planned split, which is
        # the platform's own split for this experiment key.
        predicted_p = fisher_p_value(result.split())
        result.expected_significant = predicted_p is not None and predicted_p < ALPHA

        # How much rides on this seed: the power of the platform's test at the
        # rates these users generate, for a 50/50 split of them.
        rate_control, rate_treatment = self._generated_rates(users)
        n_per_arm = len({u.user_id for u in users}) / 2
        pooled = (rate_control + rate_treatment) / 2
        se = (
            math.sqrt(pooled * (1 - pooled) * 2 / n_per_arm)
            if n_per_arm > 0 and 0 < pooled < 1
            else 0.0
        )
        z = abs(rate_treatment - rate_control) / se if se > 0 else 0.0
        power = _normal_cdf(z - Z_CRITICAL) + _normal_cdf(-z - Z_CRITICAL)

        result.metadata = {
            "experiment_duration_days": self.experiment_duration_days,
            "novelty_decay_days": self.novelty_decay_days,
            "day_of_week_effect": self.day_of_week_effect,
            "session_decay": self.session_decay,
            "start_date": self.start_date.isoformat(),
            "generated_control_rate": round(rate_control, 4),
            "generated_treatment_rate": round(rate_treatment, 4),
            "z_score": round(z, 3),
            "power": round(power, 3),
            "predicted_p_value": predicted_p,
        }
        return result

    def events_for(self, user: SimulatedUser, variant: str) -> list[UserEvent]:
        """The events *user* fires in *variant*: a function of their draws.

        The seeder calls this with the variant the platform assigned, so each
        variant's configured rate applies to the users the platform counts in
        it. It draws nothing: the same user in the same variant fires the
        same events every time.
        """
        draws = user.draws
        if draws is None:
            return []
        events: list[UserEvent] = []
        is_outlier = draws.outlier < self.outlier_rate

        if (
            variant == "treatment"
            and "simpsons_paradox" in self.inject_edge_cases
            and user.properties.get("device") == "mobile"
        ):
            # The subgroup that reverses the aggregate: mobile treatment users
            # convert at half the control base rate.
            if draws.conversion < self.control_cvr * 0.5:
                events.append(
                    UserEvent(
                        user_id=user.user_id,
                        variant_name=variant,
                        event_name=CONVERSION_EVENT,
                        value=1.0,
                        timestamp=user.assigned_at + timedelta(hours=1),
                        properties={"injected": "simpsons_paradox", "device": "mobile"},
                    )
                )
        else:
            base_cvr = self.control_cvr if variant == "control" else self.treatment_cvr
            effective = self._effective_cvr(user, variant, base_cvr)
            # Outlier: rare users produce very high-value events
            if is_outlier:
                effective = min(effective * 5, 1.0)
            if draws.conversion < effective:
                value = draws.outlier_value if is_outlier else 1.0
                events.append(
                    UserEvent(
                        user_id=user.user_id,
                        variant_name=variant,
                        event_name=CONVERSION_EVENT,
                        value=round(value, 2),
                        timestamp=user.assigned_at + timedelta(hours=draws.event_hours),
                        properties={"outlier": is_outlier},
                    )
                )

        if (
            variant == "control"
            and "metric_drift" in self.inject_edge_cases
            and user.assigned_at > self._midpoint()
            and draws.drift < DRIFT_RATE
        ):
            # Baseline drift: extra conversions for late control users.
            events.append(
                UserEvent(
                    user_id=user.user_id,
                    variant_name=variant,
                    event_name=CONVERSION_EVENT,
                    value=1.0,
                    timestamp=user.assigned_at + timedelta(hours=2),
                    properties={"injected": "metric_drift"},
                )
            )
        return events

    @staticmethod
    def planned_variant(user_id: str, experiment_key: str) -> str:
        """The variant the platform assigns *user_id* in the seeded experiment.

        The seeder creates control, then treatment, at 50% each, and the server
        assigns by ``bucket_of(user_id, experiment_key)`` over the variants in
        that order (``AssignmentService._hash_user_to_variant``): buckets 0-49
        are control. This is the dry run's plan only. The seeder converts each
        user by the server's answer and counts disagreements as
        ``variant_mismatches``.
        """
        return "control" if bucket_of(user_id, experiment_key) < 50 else "treatment"

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _make_users(
        self, rng: random.Random, experiment_key: str
    ) -> list[SimulatedUser]:
        users: list[SimulatedUser] = []
        for _ in range(self.users):
            user_id = f"user-{rng.getrandbits(48):012x}"
            day_offset = rng.uniform(0, self.experiment_duration_days)
            props: dict[str, Any] = {
                "country": rng.choice(["US", "UK", "CA", "AU", "DE"]),
                "device": rng.choice(["desktop", "mobile", "tablet"]),
                "segment": rng.choice(["new", "returning"]),
            }
            # Drawn for every user, converting or not, so that one user's
            # outcome never shifts the stream for the users after them.
            draws = UserDraws(
                conversion=rng.random(),
                outlier=rng.random(),
                outlier_value=rng.lognormvariate(0, 0.5),
                event_hours=rng.uniform(0.1, 48),
                drift=rng.random(),
            )
            users.append(
                SimulatedUser(
                    user_id=user_id,
                    variant_name=self.planned_variant(user_id, experiment_key),
                    assigned_at=self.start_date + timedelta(days=day_offset),
                    properties=props,
                    draws=draws,
                )
            )
        return users

    def _midpoint(self) -> datetime:
        return self.start_date + timedelta(days=self.experiment_duration_days / 2)

    def _effective_cvr(
        self, user: SimulatedUser, variant: str, base_cvr: float
    ) -> float:
        """Compute per-user effective CVR applying all modifiers."""
        cvr = base_cvr
        days_since_start = (user.assigned_at - self.start_date).total_seconds() / 86400

        # Novelty spike: treatment gets a burst on day 1 that decays
        if self.novelty_decay_days > 0 and variant == "treatment":
            decay_factor = max(
                0,
                1 - days_since_start / self.novelty_decay_days,
            )
            novelty_boost = 0.3 * decay_factor  # up to +30% on day 1
            cvr = min(cvr + novelty_boost, 1.0)

        # Day-of-week effect
        if self.day_of_week_effect:
            weekday = user.assigned_at.weekday()
            multiplier = 1.2 if weekday < 5 else 0.7
            cvr *= multiplier

        # Session decay: engagement drops 15% per week
        if self.session_decay:
            weeks = days_since_start / 7.0
            cvr *= max(0.1, 1.0 - 0.15 * weeks)

        return min(max(cvr, 0.0), 1.0)

    def _generated_rates(self, users: list[SimulatedUser]) -> tuple[float, float]:
        """The share of users who convert in control, and in treatment.

        Every user's outcome in both variants is fixed by their draws, so these
        are the rates a 50/50 split of them estimates, whichever users the
        platform puts where.
        """
        seen: set[str] = set()
        in_control = in_treatment = 0
        for user in users:
            if user.user_id in seen:
                continue
            seen.add(user.user_id)
            in_control += any(
                e.event_name == CONVERSION_EVENT
                for e in self.events_for(user, "control")
            )
            in_treatment += any(
                e.event_name == CONVERSION_EVENT
                for e in self.events_for(user, "treatment")
            )
        n = len(seen)
        return (in_control / n, in_treatment / n) if n else (0.0, 0.0)

    # ------------------------------------------------------------------
    # Edge case injectors
    # ------------------------------------------------------------------

    def _inject_zero_events_user(
        self, rng: random.Random, users: list[SimulatedUser], experiment_key: str
    ) -> None:
        """Add a user who is assigned but never fires any event."""
        ghost_id = f"ghost-{rng.getrandbits(32):08x}"
        users.append(
            SimulatedUser(
                user_id=ghost_id,
                variant_name=self.planned_variant(ghost_id, experiment_key),
                assigned_at=self.start_date + timedelta(hours=1),
                properties={"segment": "ghost"},
            )
        )

    def _inject_multi_assignment(
        self, rng: random.Random, users: list[SimulatedUser]
    ) -> None:
        """List one user again in the other variant (an assignment bug).

        The platform's assignment is sticky, so the seeder assigns the user
        once and the second row changes nothing there.
        """
        if not users:
            return
        victim = rng.choice(users)
        users.append(
            SimulatedUser(
                user_id=victim.user_id,
                variant_name=(
                    "treatment" if victim.variant_name == "control" else "control"
                ),
                assigned_at=victim.assigned_at + timedelta(minutes=5),
                properties={**victim.properties, "duplicate_assignment": True},
            )
        )


# ---------------------------------------------------------------------------
# API seeder
# ---------------------------------------------------------------------------


class SeedingError(RuntimeError):
    """The platform refused a seeding call; nothing after it was sent."""


class PlatformSeeder:
    """Seeds a running platform instance with scenario data via REST API.

    Every call must succeed: any non-2xx response raises :class:`SeedingError`
    naming the call and the status, so a missing ``--api-key`` (401) stops the
    run instead of reporting events that were never stored.

    For each user the seeder first calls ``POST /api/v1/tracking/assign`` and
    only then sends that user's events: the events the user fires in the
    variant the *server* assigned (``ScenarioResult.events_for``), so each
    variant's configured rate holds among the users the results engine counts
    in it. Events carry their generated ``timestamp`` when it is set, so
    pre-period events can be seeded.

    The counts returned are what the platform confirmed -- an assignment
    answered with ``assigned: true``, an event answered with a stored ``id`` --
    never the number of attempts. ``by_variant`` and ``expected_p_value`` are
    computed from the server's answers: the users and converting users the
    results API should count in each variant, and the p-value it should report.
    """

    def __init__(self, api_url: str, token: str, api_key: Optional[str] = None):
        self.api_url = api_url.rstrip("/")
        self.api_key = api_key
        self.session = requests.Session()
        self.session.headers.update(
            {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
        )

    def seed_scenario(
        self, result: ScenarioResult, experiment_key: Optional[str] = None
    ) -> dict[str, Any]:
        """Create and start an experiment, assign every user, then seed events."""
        exp_key = (
            experiment_key
            or result.experiment_key
            or f"realistic-{result.scenario_name}-{uuid.uuid4().hex[:6]}"
        )
        experiment = self._create_experiment(result, exp_key)
        exp_id = experiment["id"]
        self._start_experiment(exp_id)

        listed_events: dict[str, list[UserEvent]] = {}
        for event in result.events:
            listed_events.setdefault(event.user_id, []).append(event)

        first_row: dict[str, SimulatedUser] = {}
        for user in result.users:
            # A user listed twice (the multi_assignment edge case) is assigned
            # once: assignment is sticky, so a second call would only return
            # the first.
            first_row.setdefault(user.user_id, user)

        users_assigned = 0
        users_not_assigned = 0
        variant_mismatches = 0
        events_seeded = 0
        events_skipped_unassigned = 0
        by_variant: dict[str, dict[str, int]] = {}
        for user_id, user in first_row.items():
            assignment = self._assign_user(exp_key, user_id)
            planned_events = listed_events.pop(user_id, [])
            if not assignment.get("assigned", False):
                # Holdout / exclusion / targeting: the platform records no
                # assignment, so their events would be conversions with no
                # assigned user behind them. They are not sent.
                users_not_assigned += 1
                events_skipped_unassigned += len(planned_events)
                continue
            users_assigned += 1
            variant = assignment.get("variant_name")
            if variant != user.variant_name:
                variant_mismatches += 1
            # The user converts (or not) as a member of the server's variant.
            user_events = (
                result.events_for(user, variant)
                if result.events_for is not None and isinstance(variant, str)
                else planned_events
            )
            for event in user_events:
                self._track_event(event, exp_id, assignment["variant_id"])
                events_seeded += 1
            arm = by_variant.setdefault(
                str(variant), {"users": 0, "converting_users": 0}
            )
            arm["users"] += 1
            if any(e.event_name == CONVERSION_EVENT for e in user_events):
                arm["converting_users"] += 1

        # Events for a user the scenario never listed have no assignment.
        events_skipped_unassigned += sum(len(v) for v in listed_events.values())

        return {
            "experiment_id": exp_id,
            "experiment_key": exp_key,
            "users_generated": len(first_row),
            "users_seeded": users_assigned,
            "users_not_assigned": users_not_assigned,
            "variant_mismatches": variant_mismatches,
            "events_generated": len(result.events),
            "events_seeded": events_seeded,
            "events_skipped_unassigned": events_skipped_unassigned,
            "by_variant": by_variant,
            "expected_p_value": fisher_p_value(by_variant),
        }

    # -- HTTP -----------------------------------------------------------------

    def _post(
        self,
        what: str,
        path: str,
        payload: Optional[dict[str, Any]] = None,
        headers: Optional[dict[str, str]] = None,
    ) -> Any:
        """POST and return the JSON body; raise on anything but 2xx."""
        resp = self.session.post(
            f"{self.api_url}{path}", json=payload, headers=headers or {}
        )
        if not 200 <= resp.status_code < 300:
            hint = ""
            if resp.status_code in (401, 403) and path.startswith("/api/v1/tracking"):
                hint = " -- the tracking endpoints need --api-key (X-API-Key header)"
            elif resp.status_code == 409 and path == "/api/v1/experiments":
                hint = (
                    " -- the key comes from the scenario and the seed; pass "
                    "--experiment-key to seed this scenario again on this platform"
                )
            raise SeedingError(
                f"{what} failed: POST {path} returned HTTP {resp.status_code}"
                f"{hint}: {resp.text[:500]}"
            )
        if resp.status_code == 204 or not resp.content:
            return {}
        return resp.json()

    def _tracking_headers(self) -> dict[str, str]:
        return {"X-API-Key": self.api_key} if self.api_key else {}

    def _create_experiment(self, result: ScenarioResult, key: str) -> dict[str, Any]:
        payload = {
            "name": f"[Realistic] {result.scenario_name}",
            "key": key,
            "description": f"Auto-generated scenario: {result.scenario_name}",
            "hypothesis": "Treatment will outperform control",
            "experiment_type": "a_b",
            # In this order, at these allocations: DataScenario.planned_variant
            # relies on it.
            "variants": [
                {"name": "control", "is_control": True, "traffic_allocation": 50},
                {"name": "treatment", "is_control": False, "traffic_allocation": 50},
            ],
            "metrics": [
                {
                    "name": "Conversion Rate",
                    "event_name": CONVERSION_EVENT,
                    "metric_type": "conversion",
                    "is_primary": True,
                }
            ],
        }
        experiment = self._post("create experiment", "/api/v1/experiments", payload)
        if not isinstance(experiment, dict) or "id" not in experiment:
            raise SeedingError(f"create experiment returned no id: {experiment!r}")
        return experiment

    def _start_experiment(self, exp_id: str) -> None:
        # /tracking/assign only assigns into an ACTIVE experiment.
        self._post("start experiment", f"/api/v1/experiments/{exp_id}/start")

    def _assign_user(self, exp_key: str, user_id: str) -> dict[str, Any]:
        body = self._post(
            f"assign {user_id}",
            "/api/v1/tracking/assign",
            {"experiment_key": exp_key, "user_id": user_id},
            self._tracking_headers(),
        )
        if not isinstance(body, dict) or "variant_id" not in body:
            raise SeedingError(f"assign {user_id} returned no variant_id: {body!r}")
        return body

    def _track_event(self, event: UserEvent, exp_id: str, variant_id: str) -> None:
        payload: dict[str, Any] = {
            "event_type": "track",
            "event_name": event.event_name,
            "user_id": event.user_id,
            "value": event.value,
            "experiment_id": exp_id,
            "variant_id": variant_id,
            "properties": event.properties,
        }
        if event.timestamp is not None:
            payload["timestamp"] = event.timestamp.isoformat()
        body = self._post(
            f"track event for {event.user_id}",
            "/api/v1/tracking/events",
            payload,
            self._tracking_headers(),
        )
        if not isinstance(body, dict) or not body.get("id"):
            raise SeedingError(
                f"track event for {event.user_id} returned no stored id: {body!r}"
            )


# ---------------------------------------------------------------------------
# Pre-built scenario factories
# ---------------------------------------------------------------------------


def make_ab_test_scenario(seed: int = 42) -> DataScenario:
    """
    Standard A/B test: detectable 18.75% relative lift over 14 days.

    Sample size rationale: detecting 8% → 9.5% CVR (delta=1.5pp) at α=0.05
    with 80% power takes about 5,570 users per arm. The day-of-week effect and
    the outliers raise both rates (to about 9.1% and 10.8%), which brings that
    down to about 4,800 per arm, so 12,000 users (about 6,000 per arm) give a
    power of about 0.88. At 6,000 users it was about 0.6: half the seeds were
    not significant. The dry run prints the exact p-value for a seed.
    """
    return DataScenario(
        name="ab_test_lifecycle",
        users=12_000,
        control_cvr=0.08,
        treatment_cvr=0.095,
        day_of_week_effect=True,
        session_decay=False,
        seed=seed,
    )


def make_rollout_scenario(seed: int = 99) -> DataScenario:
    """
    Feature flag gradual rollout: negligible lift (rollout mechanics validation).

    Not designed to produce statistical significance — the goal is to test
    rollout percentage transitions, not detect an effect.
    """
    return DataScenario(
        name="feature_flag_rollout",
        users=4_000,
        control_cvr=0.05,
        treatment_cvr=0.051,
        day_of_week_effect=False,
        session_decay=True,
        experiment_duration_days=21,
        seed=seed,
    )


def make_novelty_scenario(seed: int = 7) -> DataScenario:
    """
    Novelty effect: day-1 spike that fades to a detectable steady-state lift.

    6,000 users. The novelty boost (up to +30 points for the first users,
    gone by day 3) makes the aggregate lift large (about 11% → 19%), so the
    experiment as a whole is significant whatever the seed. The steady-state
    lift alone (10% → 12%) would take about 3,850 users per arm at 80% power.
    """
    return DataScenario(
        name="novelty_effect",
        users=6_000,
        control_cvr=0.10,
        treatment_cvr=0.12,
        novelty_decay_days=3,
        outlier_rate=0.01,
        day_of_week_effect=True,
        seed=seed,
    )


def make_edge_case_scenario(seed: int = 13) -> DataScenario:
    """
    Stress scenario: outliers, drift, multi-assignment bugs, Simpson's paradox.

    8,000 users at a base 6% → 7.2%, with all four edge cases, which dominate
    the aggregate: metric_drift gives about 10% of the late control users an
    extra conversion and simpsons_paradox halves the mobile treatment users'
    rate, so control comes out well ahead (about 11% against 5%).
    """
    return DataScenario(
        name="statistical_edge_cases",
        users=8_000,
        control_cvr=0.06,
        treatment_cvr=0.072,
        outlier_rate=0.03,
        inject_edge_cases=[
            "zero_events_user",
            "multi_assignment",
            "metric_drift",
            "simpsons_paradox",
        ],
        day_of_week_effect=True,
        session_decay=True,
        experiment_duration_days=21,
        seed=seed,
    )


def make_concurrent_scenario(seed: int = 21) -> DataScenario:
    """
    Concurrent experiments: overlapping audiences for interaction testing.

    8,000 users; detecting 7% → 8.2% (delta=1.2pp) at α=0.05 with 80% power
    takes about 7,650 users per arm (about 6,600 once the day-of-week effect
    and the outliers raise both rates), so at about 4,000 per arm the power is
    about 0.6: significant on some seeds and not on others. The dry run says
    which.
    """
    return DataScenario(
        name="concurrent_experiments",
        users=8_000,
        control_cvr=0.07,
        treatment_cvr=0.082,
        day_of_week_effect=True,
        seed=seed,
    )


#: Each scenario's factory; it takes ``seed`` and defaults to the scenario's
#: own. "all" runs every scenario, one after another.
SCENARIOS: dict[str, Optional[Callable[..., DataScenario]]] = {
    "ab_test_lifecycle": make_ab_test_scenario,
    "feature_flag_rollout": make_rollout_scenario,
    "novelty_effect": make_novelty_scenario,
    "statistical_edge_cases": make_edge_case_scenario,
    "concurrent_experiments": make_concurrent_scenario,
    "all": None,  # special keyword
}


# ---------------------------------------------------------------------------
# CLI entry-point
# ---------------------------------------------------------------------------


def main(argv: Optional[list[str]] = None) -> None:
    parser = argparse.ArgumentParser(
        description="Generate realistic experiment data for the Controlvar platform."
    )
    parser.add_argument(
        "--scenario",
        choices=list(SCENARIOS.keys()),
        default="ab_test_lifecycle",
        help="Which scenario to generate (default: ab_test_lifecycle)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print summary without seeding the API",
    )
    parser.add_argument(
        "--api-url",
        default="http://localhost:8000",
        help="Base URL of the running platform API",
    )
    parser.add_argument(
        "--token",
        default="",
        help="JWT bearer token for the API",
    )
    parser.add_argument(
        "--api-key",
        default=None,
        help=(
            "API key for the tracking endpoints (X-API-Key header). "
            "Required: /tracking/assign and /tracking/events answer 401 without it, "
            "and the seeder stops on the first non-2xx response."
        ),
    )
    parser.add_argument(
        "--seed",
        type=int,
        default=None,
        help=(
            "Seed for every random draw (the users, their ids, dates and "
            "conversions) and for the default experiment key. Default: the "
            "scenario's own seed. The same scenario and seed give the same data."
        ),
    )
    parser.add_argument(
        "--experiment-key",
        default=None,
        help=(
            "Key of the experiment to create (default: realistic-<scenario>-seed"
            "<seed>). Keys are unique on a platform: give another one to seed a "
            "scenario and seed again. The platform's split depends on the key, so "
            "pass the same one to the dry run to see the counts it will show."
        ),
    )
    args = parser.parse_args(argv)
    if args.scenario == "all" and args.experiment_key:
        parser.error("--experiment-key names one experiment: give one --scenario")

    names = (
        [k for k, f in SCENARIOS.items() if f is not None]
        if args.scenario == "all"
        else [args.scenario]
    )
    for name in names:
        factory = SCENARIOS[name]
        if factory is None:
            print("Error: unexpected None scenario", file=sys.stderr)
            sys.exit(1)
        scenario = factory() if args.seed is None else factory(seed=args.seed)
        print(f"\nGenerating scenario: {scenario.name} ...")
        result = scenario.generate(experiment_key=args.experiment_key)
        print(result.summary())

        if not args.dry_run:
            if not args.token:
                print("  --token required to seed API.  Skipping.", file=sys.stderr)
                continue
            seeder = PlatformSeeder(args.api_url, args.token, api_key=args.api_key)
            seed_result = seeder.seed_scenario(result)
            print(f"  Seeded: {seed_result}")


if __name__ == "__main__":
    main()
