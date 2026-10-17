#!/usr/bin/env python3
"""
Live event simulator for Controlvar demo.

Streams synthetic events directly to the database at a configurable rate,
targeting the active A/B test and MAB experiment so charts update
in real time during the demo.

The simulator writes directly to the DB (bypassing the API) for reliability,
since the tracking API endpoint uses Experiment.key which may not be present
in all deployment configurations.

Usage:
    cd /path/to/controlvar
    source venv/bin/activate
    python backend/scripts/simulate_live_events.py \\
        --rate 5 \\
        --duration 0

Arguments:
    --api-url   Base URL of the API (kept for script interface compatibility, unused)
    --api-key   API key (kept for script interface compatibility, unused)
    --rate      Events per second (default: 5)
    --duration  Run for this many seconds; 0 = infinite (default: 0)
"""

import argparse
import os
import random
import signal
import sys
import time
import uuid
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

# Add project root to Python path
PROJECT_ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

os.environ.setdefault("APP_ENV", "development")
os.environ.setdefault("POSTGRES_SERVER", "localhost")
os.environ.setdefault("POSTGRES_USER", "postgres")
os.environ.setdefault("POSTGRES_PASSWORD", "postgres")
os.environ.setdefault("POSTGRES_DB", "experimentation")
os.environ.setdefault("POSTGRES_SCHEMA", "experimentation")

from backend.app.db.session import SessionLocal
from backend.app.models.assignment import Assignment
from backend.app.models.event import Event
from backend.app.models.experiment import Experiment, ExperimentStatus, Variant

# ---------------------------------------------------------------------------
# Configuration: per-variant conversion rates
# ---------------------------------------------------------------------------

EXPERIMENT_CONFIG = {
    "Checkout Button Color": {
        "variant_rates": {
            "blue_button": 0.08,  # 8% conversion
            "green_button": 0.11,  # 11% — green winning slowly
        },
        "event_name": "checkout_completed",
        "weight": 0.6,
    },
    "Recommendation Algorithm MAB": {
        "variant_rates": {
            "algo_v1": 0.05,  # 5%
            "algo_v2": 0.09,  # 9% — v2 pulling ahead
            "algo_v3": 0.06,  # 6%
        },
        "event_name": "item_clicked",
        "weight": 0.4,
    },
}


# ---------------------------------------------------------------------------
# Statistics tracker
# ---------------------------------------------------------------------------


class Stats:
    def __init__(self):
        self.assigned: dict = defaultdict(int)
        self.converted: dict = defaultdict(int)
        self.variant_assigned: dict = defaultdict(int)
        self.variant_converted: dict = defaultdict(int)
        self.errors: int = 0
        self.total_events: int = 0
        self.start_time: float = time.time()

    def record_assignment(self, exp_name: str, variant: str):
        self.assigned[exp_name] += 1
        self.variant_assigned[(exp_name, variant)] += 1
        self.total_events += 1

    def record_conversion(self, exp_name: str, variant: str):
        self.converted[exp_name] += 1
        self.variant_converted[(exp_name, variant)] += 1

    def record_error(self):
        self.errors += 1

    def print_summary(self):
        elapsed = time.time() - self.start_time
        ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
        lines = [
            f"[{ts}] Live stats (elapsed: {int(elapsed)}s | total events: {self.total_events:,})"
        ]

        for exp_name, exp_config in EXPERIMENT_CONFIG.items():
            assigned_total = self.assigned.get(exp_name, 0)
            converted_total = self.converted.get(exp_name, 0)
            cvr = (
                (converted_total / assigned_total * 100) if assigned_total > 0 else 0.0
            )

            variant_parts = []
            for variant_name in exp_config["variant_rates"]:
                va = self.variant_assigned.get((exp_name, variant_name), 0)
                vc = self.variant_converted.get((exp_name, variant_name), 0)
                v_cvr = (vc / va * 100) if va > 0 else 0.0
                variant_parts.append(f"{variant_name}={va}/{vc}({v_cvr:.1f}%)")

            lines.append(
                f"  {exp_name}: {assigned_total} assigned, "
                f"{converted_total} converted ({cvr:.1f}%) | " + " ".join(variant_parts)
            )

        if self.errors > 0:
            lines.append(f"  [!] {self.errors} errors (DB connection issue?)")

        print("\n".join(lines))
        print()


# ---------------------------------------------------------------------------
# Simulator core
# ---------------------------------------------------------------------------


class Simulator:
    def __init__(self, rate: float, duration: int, rng_seed: Optional[int] = None):
        self.rate = rate
        self.duration = duration
        self.rng = random.Random(rng_seed)
        self.stats = Stats()
        self._running = True
        self._next_stats_print = time.time() + 10
        # Cache: exp_name -> {variant_name: (experiment_id, variant_id)}
        self._experiment_cache: dict = {}

    def _stop(self, *_):
        print("\n\nStopping simulator (SIGINT)...")
        self._running = False

    def _load_experiments(self):
        """Load active experiment + variant IDs from DB."""
        self._experiment_cache = {}
        try:
            with SessionLocal() as db:
                for exp_name in EXPERIMENT_CONFIG:
                    exp = (
                        db.query(Experiment)
                        .filter(
                            Experiment.name == exp_name,
                            Experiment.status == ExperimentStatus.ACTIVE,
                        )
                        .first()
                    )
                    if not exp:
                        print(
                            f"  [warn] Experiment '{exp_name}' not found or not ACTIVE — will skip."
                        )
                        continue

                    variants = (
                        db.query(Variant).filter(Variant.experiment_id == exp.id).all()
                    )

                    self._experiment_cache[exp_name] = {
                        "id": exp.id,
                        "variants": {v.name: v.id for v in variants},
                    }
                    print(
                        f"  Loaded '{exp_name}' (id={exp.id}): "
                        f"{[v.name for v in variants]}"
                    )
        except Exception as e:
            print(f"  [error] Could not load experiments from DB: {e}")

    def _pick_experiment(self) -> Optional[tuple]:
        """Return (exp_name, exp_config) based on weights. Returns None if no experiments loaded."""
        available = [
            name for name in EXPERIMENT_CONFIG if name in self._experiment_cache
        ]
        if not available:
            return None, None
        weights = [EXPERIMENT_CONFIG[name]["weight"] for name in available]
        chosen = self.rng.choices(available, weights=weights, k=1)[0]
        return chosen, EXPERIMENT_CONFIG[chosen]

    def _simulate_user(self, exp_name: str, exp_config: dict):
        """Simulate one user: assign + maybe convert. Writes directly to DB."""
        cached = self._experiment_cache.get(exp_name)
        if not cached:
            return

        exp_id = cached["id"]
        variant_map = cached["variants"]

        # Pick a variant based on configured rates (weighted by rates for MAB realism)
        variant_names = list(variant_map.keys())
        if not variant_names:
            return

        # For MAB: assign variant weighted by the conversion rates to simulate exploration
        # (real Thompson Sampling would update weights; we use fixed weights for demo)
        weights = [exp_config["variant_rates"].get(v, 0.05) for v in variant_names]
        variant_name = self.rng.choices(variant_names, weights=weights, k=1)[0]
        variant_id = variant_map[variant_name]
        user_id = f"sim_{uuid.uuid4().hex[:12]}"

        try:
            with SessionLocal() as db:
                now = datetime.now(timezone.utc)

                # Check for existing assignment (idempotency)
                existing = (
                    db.query(Assignment)
                    .filter(
                        Assignment.experiment_id == exp_id,
                        Assignment.user_id == user_id,
                    )
                    .first()
                )

                if not existing:
                    assignment = Assignment(
                        experiment_id=exp_id,
                        variant_id=variant_id,
                        user_id=user_id,
                        context={"source": "simulator"},
                        created_at=now,
                    )
                    db.add(assignment)

                # Exposure event
                exposure = Event(
                    event_type="experiment_exposure",
                    event_name="experiment_exposure",
                    user_id=user_id,
                    experiment_id=exp_id,
                    variant_id=variant_id,
                    value=1.0,
                    created_at=now.isoformat(),
                )
                db.add(exposure)
                db.flush()

                self.stats.record_assignment(exp_name, variant_name)

                # Maybe convert
                cvr = exp_config["variant_rates"].get(variant_name, 0.05)
                if self.rng.random() < cvr:
                    conv_time = now
                    conversion = Event(
                        event_type=exp_config["event_name"],
                        event_name=exp_config["event_name"],
                        user_id=user_id,
                        experiment_id=exp_id,
                        variant_id=variant_id,
                        value=1.0,
                        created_at=conv_time.isoformat(),
                    )
                    db.add(conversion)
                    db.commit()
                    self.stats.record_conversion(exp_name, variant_name)
                else:
                    db.commit()

        except Exception:
            self.stats.record_error()
            # Don't print every error — just increment counter

    def run(self):
        signal.signal(signal.SIGINT, self._stop)
        signal.signal(signal.SIGTERM, self._stop)

        print("Starting live event simulator")
        print(f"  Rate:    {self.rate} events/sec")
        print(f"  Duration:{self.duration}s (0=infinite)")
        print("  Writing directly to database")
        print()

        # Load experiment data
        print("Loading experiments from database...")
        self._load_experiments()

        if not self._experiment_cache:
            print("[warn] No active experiments found. Is the database seeded?")
            print("       Run: python backend/scripts/seed_demo_data.py")
            return

        print()
        sleep_interval = 1.0 / self.rate if self.rate > 0 else 0.2
        start = time.time()

        while self._running:
            if self.duration > 0 and (time.time() - start) >= self.duration:
                print(f"\nDuration ({self.duration}s) reached. Stopping.")
                break

            loop_start = time.time()

            exp_name, exp_config = self._pick_experiment()
            if exp_name:
                self._simulate_user(exp_name, exp_config)

            # Print stats every 10s
            if time.time() >= self._next_stats_print:
                self.stats.print_summary()
                self._next_stats_print = time.time() + 10

            # Rate limiting
            elapsed = time.time() - loop_start
            remaining = sleep_interval - elapsed
            if remaining > 0:
                time.sleep(remaining)

        print("Final statistics:")
        self.stats.print_summary()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main():
    parser = argparse.ArgumentParser(
        description="Stream live events for Controlvar demo (writes directly to DB)"
    )
    parser.add_argument(
        "--api-url",
        default="http://localhost:8000",
        help="API URL (kept for interface compatibility — not used)",
    )
    parser.add_argument(
        "--api-key",
        default="",
        help="API key (kept for interface compatibility — not used)",
    )
    parser.add_argument(
        "--rate",
        type=float,
        default=5.0,
        help="Events per second (default: 5)",
    )
    parser.add_argument(
        "--duration",
        type=int,
        default=0,
        help="Run for this many seconds; 0 = infinite (default: 0)",
    )
    parser.add_argument(
        "--seed",
        type=int,
        default=None,
        help="Random seed for reproducibility",
    )
    args = parser.parse_args()

    simulator = Simulator(
        rate=args.rate,
        duration=args.duration,
        rng_seed=args.seed,
    )
    simulator.run()


if __name__ == "__main__":
    main()
