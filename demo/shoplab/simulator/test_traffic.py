"""Offline tests for the ShopLab traffic simulator (no network: ApiClient is replaced)."""
from __future__ import annotations

import io
import json
import random
import sys
import urllib.error
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
# The repository root, for the seed script the history test reads.
REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) not in sys.path:
    sys.path.append(str(REPO_ROOT))

import traffic  # noqa: E402

TOL = 0.03  # ±3 percentage points


class FakeClient:
    """Stands in for ApiClient: server-side variant split + flag rollout, records batches."""

    def __init__(self, api_url: str = "", api_key: str = "", timeout: float = 5.0, *, seed: int = 1, new_search_p: float = 0.5):
        self.rng = random.Random(seed)
        self.new_search_p = new_search_p
        self.batches: list[list[dict]] = []
        self.assign_calls: list[tuple[str, str, dict]] = []
        self.evaluate_calls: list[tuple[str, str]] = []

    def assign(self, experiment_key: str, user_id: str, context: dict) -> dict:
        self.assign_calls.append((experiment_key, user_id, context))
        split = traffic.VARIANTS[experiment_key]
        variant = self.rng.choices(list(split), weights=list(split.values()), k=1)[0]
        return {
            "experiment_key": experiment_key,
            "user_id": user_id,
            "variant_id": f"{experiment_key}:{variant}",
            "variant_name": variant,
            "is_control": variant in ("control", "relevance", "standard"),
            "configuration": {},
        }

    def evaluate_flag(self, flag_key: str, user_id: str) -> dict:
        self.evaluate_calls.append((flag_key, user_id))
        enabled = self.rng.random() < self.new_search_p if flag_key == traffic.NEW_SEARCH_FLAG else True
        return {"key": flag_key, "enabled": enabled, "config": None}

    def batch(self, events: list[dict]) -> dict:
        assert len(events) <= traffic.BATCH_LIMIT
        self.batches.append(list(events))
        return {"success_count": len(events), "failure_count": 0}


class FailingClient(FakeClient):
    def __init__(self, status: int, **kw):
        super().__init__(**kw)
        self.status = status

    def assign(self, experiment_key, user_id, context):
        raise traffic.ApiError(self.status, "nope", f"http://x/api/v1/tracking/assign")


def run_visitors(n: int, seed: int = 42, **client_kw) -> tuple[FakeClient, traffic.Stats, list[traffic.VisitorPlan]]:
    rng = random.Random(seed)
    client = FakeClient(seed=seed + 1, **client_kw)
    stats = traffic.Stats()
    plans = [traffic.run_visitor(client, rng, stats) for _ in range(n)]
    return client, stats, plans


def share(plans, predicate, given=lambda p: True) -> float:
    pool = [p for p in plans if given(p)]
    assert pool, "empty conditioning set"
    return sum(1 for p in pool if predicate(p)) / len(pool)


# --------------------------------------------------------------------------------------


def test_persona_mix_is_calibrated_to_spec_rates():
    assert sum(p.weight for p in traffic.PERSONAS) == pytest.approx(1.0)
    assert sum(p.weight * p.intent for p in traffic.PERSONAS) == pytest.approx(1.0)
    # Purchase shift is zero on average over visitors who reach checkout.
    checkout_weighted = sum(p.weight * traffic.begin_checkout_share(p.intent) * p.purchase_shift for p in traffic.PERSONAS)
    assert checkout_weighted == pytest.approx(0.0, abs=1e-9)
    names = [p.name for p in traffic.PERSONAS]
    assert names == ["casual", "shopper", "power"]
    assert traffic.PERSONAS[0].intent < traffic.PERSONAS[1].intent < traffic.PERSONAS[2].intent
    assert traffic.PERSONAS[0].purchase_shift < 0 < traffic.PERSONAS[1].purchase_shift <= traffic.PERSONAS[2].purchase_shift
    for p in traffic.PERSONAS:
        assert 0 < traffic.begin_checkout_share(p.intent) < 0.5


def test_primary_metric_rates_match_spec_over_5000_visitors():
    _, stats, _ = run_visitors(5000)
    assert stats.visitors == 5000
    expectations = {
        traffic.HERO_KEY: traffic.HERO_CTA_RATE,
        traffic.PLP_KEY: traffic.PLP_CLICK_RATE,
        traffic.PDP_KEY: traffic.PDP_ADD_RATE,
    }
    for key, table in expectations.items():
        for variant, expected in table.items():
            n = stats.variant_visitors[key][variant]
            assert n > 500, f"{key}/{variant} only got {n} visitors"
            observed = stats.rate(key, variant)
            assert observed == pytest.approx(expected, abs=TOL), f"{key}/{variant}: {observed:.3f} vs {expected}"
    # Sanity: the better variants really are better in the sample.
    assert stats.rate(traffic.HERO_KEY, "video_hero") > stats.rate(traffic.HERO_KEY, "control")
    assert stats.rate(traffic.PLP_KEY, "ml_personalized") > stats.rate(traffic.PLP_KEY, "price_low_high")


def test_conditional_funnel_rates_match_spec():
    # Conditionals have far smaller denominators, so use a bigger sample for ±3 pts.
    _, _, plans = run_visitors(30000, seed=7)

    for variant, expected in traffic.CHECKOUT_PURCHASE_RATE.items():
        observed = share(
            plans,
            lambda p: p.outcomes["purchase"],
            given=lambda p, v=variant: p.outcomes["begin_checkout"] and p.assignments[traffic.CHECKOUT_KEY] == v,
        )
        assert observed == pytest.approx(expected, abs=TOL), f"purchase|begin_checkout {variant}: {observed:.3f}"

    for flag_on, expected in traffic.SEARCH_CLICK_RATE.items():
        observed = share(
            plans,
            lambda p: p.outcomes["search_click"],
            given=lambda p, f=flag_on: p.outcomes["searched"] and p.flags[traffic.NEW_SEARCH_FLAG] is f,
        )
        assert observed == pytest.approx(expected, abs=TOL), f"product_click|search flag={flag_on}: {observed:.3f}"

    add_after_click = share(plans, lambda p: p.outcomes["add_to_cart"], given=lambda p: p.outcomes["product_click"])
    assert add_after_click == pytest.approx(traffic.ADD_TO_CART_AFTER_CLICK, abs=TOL)

    purchase_after_hero = share(plans, lambda p: p.outcomes["purchase"], given=lambda p: p.outcomes["hero_click"])
    assert purchase_after_hero == pytest.approx(traffic.HERO_PURCHASE_AFTER_CLICK, abs=TOL)

    # Order values: lognormal with median ≈ $85.
    values = sorted(p.order_value for p in plans if p.outcomes["purchase"])
    assert values[len(values) // 2] == pytest.approx(traffic.ORDER_VALUE_MEDIAN, rel=0.1)


def test_assignment_and_flag_calls_carry_context():
    client, _, plans = run_visitors(20)
    reached_checkout = sum(p.outcomes["begin_checkout"] for p in plans)
    # Three experiments per visitor, and the checkout flow for those who began checkout.
    assert len(client.assign_calls) == 20 * 3 + reached_checkout
    assert len(client.evaluate_calls) == 20 * len(traffic.FLAG_KEYS)
    key, user_id, context = client.assign_calls[0]
    assert key in traffic.EXPERIMENT_KEYS
    assert set(context) == {"device", "country", "returning", "persona"}
    assert context["persona"] in {"casual", "shopper", "power"}
    assert plans[0].user_id == user_id
    # sticky: the same user id was used for the visitor's assign calls and both flag evaluations
    first = len(plans[0].assignments)
    assert {c[1] for c in client.assign_calls[:first]} == {user_id}
    assert {c[1] for c in client.evaluate_calls[:2]} == {user_id}


@pytest.mark.regression
def test_each_experiment_is_assigned_where_the_storefront_assigns_it():
    """Every visitor was assigned to all four experiments before the walk, so the
    checkout flow counted visitors who never reached checkout. It is now assigned only
    to those who begin checkout, as the checkout page does; the other three as the
    visit starts, in the order the walk reaches them."""
    client, _, plans = run_visitors(300, seed=3)
    start = [traffic.HERO_KEY, traffic.PLP_KEY, traffic.PDP_KEY]
    calls: dict[str, list[str]] = {}
    for key, user_id, _ in client.assign_calls:
        calls.setdefault(user_id, []).append(key)
    for plan in plans:
        expected = start + ([traffic.CHECKOUT_KEY] if plan.outcomes["begin_checkout"] else [])
        assert calls[plan.user_id] == expected
        assert list(plan.assignments) == expected
    reached = sum(p.outcomes["begin_checkout"] for p in plans)
    assert 0 < reached < len(plans) / 2


def test_batch_payloads_have_the_right_keys():
    client, _, plans = run_visitors(300, seed=3)
    assert client.batches, "no batches sent"
    required = {"event_type", "event_name", "user_id", "metadata"}
    metadata_keys = {
        "page_view": {"page"},
        "hero_cta_click": {"variant", "media"},
        "product_click": {"product_id", "position", "sort"},
        "add_to_cart": {"product_id", "quantity", "button"},
        "begin_checkout": {"items", "flow"},
        "purchase": {"items", "flow", "order_id"},
        "search": {"query", "results", "engine"},
    }
    valued = {"add_to_cart", "begin_checkout", "purchase"}
    seen: set[str] = set()
    for batch in client.batches:
        assert 1 <= len(batch) <= traffic.BATCH_LIMIT
        for e in batch:
            assert required <= set(e), e
            assert e["event_type"] == e["event_name"]
            assert ("experiment_key" in e) ^ ("feature_flag_key" in e), e
            assert set(e["metadata"]) == metadata_keys[e["event_name"]], e
            assert (e["event_name"] in valued) == ("value" in e), e
            if "value" in e:
                assert e["value"] > 0
            if "feature_flag_key" in e:
                assert e["feature_flag_key"] == traffic.NEW_SEARCH_FLAG
                assert e["event_name"] in ("search", "product_click")
                if e["event_name"] == "product_click":
                    assert e["metadata"]["sort"] == "search"
            else:
                assert e["experiment_key"] in traffic.EXPERIMENT_KEYS
            seen.add(e["event_name"])
    assert seen == set(metadata_keys), f"missing event types: {set(metadata_keys) - seen}"

    # Fan-out: every experiment-attributed event is sent once per experiment assigned so
    # far, the copies one after another: the checkout flow only from the checkout page on.
    for plan in plans:
        copies = []
        for e in plan.events:
            if "experiment_key" not in e:
                continue
            body = {k: v for k, v in e.items() if k != "experiment_key"}
            if not copies or copies[-1][0] != body or e["experiment_key"] in copies[-1][1]:
                copies.append((body, []))
            copies[-1][1].append(e["experiment_key"])
        assert copies
        for body, keys in copies:
            at_checkout = body["event_name"] in ("begin_checkout", "purchase") or body["metadata"].get("page") == "checkout"
            assert keys == [k for k in traffic.EXPERIMENT_KEYS if at_checkout or k != traffic.CHECKOUT_KEY], body
        # A purchase always follows a begin_checkout, which follows either add_to_cart or a hero click.
        names = [e["event_name"] for e in plan.events]
        if "purchase" in names:
            assert names.index("begin_checkout") < names.index("purchase")
            assert plan.outcomes["add_to_cart"] or plan.outcomes["hero_click"]


def test_chunking_never_exceeds_batch_limit():
    events = [{"i": i} for i in range(250)]
    chunks = list(traffic.chunked(events))
    assert [len(c) for c in chunks] == [100, 100, 50]
    assert sum(len(c) for c in chunks) == 250


def test_dry_run_prints_plan_without_network():
    out = io.StringIO()

    def boom(*a, **k):
        raise AssertionError("dry run must not construct an API client")

    rc = traffic.main(["--dry-run", "--seed", "11"], client_factory=boom, out=out)
    assert rc == 0
    text = out.getvalue()
    assert "dry run" in text
    lines = [ln for ln in text.splitlines() if ln and not ln.startswith("#")]
    events = [json.loads(ln) for ln in lines]
    assert events and events[0]["event_name"] == "page_view"
    prefix = "# assignments="
    assigned = json.loads(next(ln for ln in text.splitlines() if ln.startswith(prefix))[len(prefix) :])
    assert {traffic.HERO_KEY, traffic.PLP_KEY, traffic.PDP_KEY} <= set(assigned)
    assert {e["experiment_key"] for e in events if "experiment_key" in e} == set(assigned)

    # Deterministic for a given seed.
    out2 = io.StringIO()
    traffic.main(["--dry-run", "--seed", "11"], client_factory=boom, out=out2)
    assert out2.getvalue() == text


def test_help_lists_documented_arguments(capsys):
    with pytest.raises(SystemExit) as exc:
        traffic.main(["--help"])
    assert exc.value.code == 0
    text = capsys.readouterr().out
    for flag in ("--api-url", "--api-key", "--rate", "--duration", "--seed", "--dry-run"):
        assert flag in text


@pytest.mark.parametrize("status,expected_rc,needle", [(401, 2, "API key"), (404, 3, "seed"), (0, 4, "Cannot reach")])
def test_fatal_api_errors_exit_non_zero_with_message(status, expected_rc, needle, capsys):
    out = io.StringIO()
    rc = traffic.main(
        ["--api-key", "k", "--max-visitors", "1", "--rate", "1000"],
        client_factory=lambda url, key: FailingClient(status),
        sleep=lambda s: None,
        out=out,
    )
    assert rc == expected_rc
    assert needle in capsys.readouterr().err


def test_run_stops_after_max_visitors_and_reports(capsys):
    out = io.StringIO()
    made = []

    def factory(url, key):
        c = FakeClient(seed=5)
        made.append(c)
        return c

    rc = traffic.main(["--api-key", "k", "--max-visitors", "25", "--rate", "1000", "--seed", "1"], client_factory=factory, sleep=lambda s: None, out=out)
    assert rc == 0
    assert len(made[0].batches) >= 25
    text = out.getvalue()
    assert "visitors=25" in text
    for key in traffic.EXPERIMENT_KEYS:
        assert key in text


def test_missing_api_key_is_an_error(monkeypatch, capsys):
    monkeypatch.setattr(traffic, "DEFAULT_API_KEY_FILE", Path("/nonexistent/.api_key"))
    rc = traffic.main(["--max-visitors", "1"], client_factory=lambda *a: FakeClient())
    assert rc == 2
    assert "API key" in capsys.readouterr().err


def test_api_client_request_shape_and_error_mapping(monkeypatch):
    captured = {}

    class Resp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return b'{"success_count": 2, "failure_count": 0}'

    def fake_urlopen(req, timeout):
        captured["url"] = req.full_url
        captured["method"] = req.get_method()
        captured["headers"] = {k.lower(): v for k, v in req.header_items()}
        captured["body"] = json.loads(req.data)
        captured["timeout"] = timeout
        return Resp()

    monkeypatch.setattr(traffic.urllib.request, "urlopen", fake_urlopen)
    client = traffic.ApiClient("http://localhost:8000/", "secret")
    resp = client.batch([{"event_type": "page_view"}, {"event_type": "purchase"}])
    assert resp == {"success_count": 2, "failure_count": 0}
    assert captured["url"] == "http://localhost:8000/api/v1/tracking/batch"
    assert captured["method"] == "POST"
    assert captured["headers"]["x-api-key"] == "secret"
    assert captured["headers"]["content-type"] == "application/json"
    assert captured["body"] == {"events": [{"event_type": "page_view"}, {"event_type": "purchase"}]}
    assert captured["timeout"] == 5.0

    def fake_get(req, timeout):
        captured["url"] = req.full_url
        return Resp()

    monkeypatch.setattr(traffic.urllib.request, "urlopen", fake_get)
    client.evaluate_flag("shoplab_new_search", "u-1")
    assert captured["url"] == "http://localhost:8000/api/v1/feature-flags/evaluate/shoplab_new_search?user_id=u-1"

    def raise_401(req, timeout):
        raise urllib.error.HTTPError(req.full_url, 401, "Unauthorized", {}, io.BytesIO(b'{"detail":"bad key"}'))

    monkeypatch.setattr(traffic.urllib.request, "urlopen", raise_401)
    with pytest.raises(traffic.ApiError) as exc:
        client.assign("shoplab_hero_banner", "u-1", {})
    assert exc.value.status == 401
    assert "bad key" in exc.value.body

    def raise_conn(req, timeout):
        raise urllib.error.URLError("Connection refused")

    monkeypatch.setattr(traffic.urllib.request, "urlopen", raise_conn)
    with pytest.raises(traffic.ApiError) as exc:
        client.assign("shoplab_hero_banner", "u-1", {})
    assert exc.value.status == 0

    with pytest.raises(ValueError):
        client.batch([{}] * 101)


class ClockedClient(FakeClient):
    """Notes the wall-clock time each batch is sent at, beside each event it carried."""

    def __init__(self, **kw):
        super().__init__(**kw)
        self.sent: list[tuple[datetime, dict]] = []

    def batch(self, events: list[dict]) -> dict:
        now = datetime.now(timezone.utc)
        self.sent.extend((now, e) for e in events)
        return super().batch(events)


@pytest.mark.regression
def test_no_event_is_dated_after_it_is_sent():
    """The simulator dated each event 2-20 s after the one before, from the moment the visit
    was planned, and sent them all at once, so they arrived dated up to minutes ahead (the
    server keeps a client timestamp). It now sends none: the server dates each on receipt."""
    rng = random.Random(4)
    client, stats = ClockedClient(seed=5), traffic.Stats()
    for _ in range(300):
        traffic.run_visitor(client, rng, stats)
    assert any(e["event_name"] == "purchase" for _, e in client.sent), "expected whole funnels"
    ahead = [
        (sent.isoformat(), e)
        for sent, e in client.sent
        if "timestamp" in e and datetime.fromisoformat(e["timestamp"]) > sent
    ]
    assert ahead == []
    assert not [e for _, e in client.sent if "timestamp" in e]


#: The checkout experiment's purchase rate per assigned user, history against live
#: traffic: at most this far apart, per variant.
HISTORY_TOL = 0.015
#: Sample sizes. With 200,000 simulated visitors (about 13,000 reach checkout per arm)
#: the gap stayed under 0.9 points on each of seeds 1-30; with 60,000, 3 of the 30 seeds
#: went past 1.5 points from noise alone.
LIVE_VISITORS = 200_000
HISTORY_USERS = 40_000


def history_checkout_rates(users: int) -> dict[str, float]:
    """What the seed writes for the checkout experiment: purchases per assigned user,
    drawn by its own history generator (backend/scripts/seed_shoplab.py) with the seed
    it uses. No database: the generator only builds the event objects."""
    from backend.scripts import seed_shoplab

    spec = next(e for e in seed_shoplab.EXPERIMENTS if e["key"] == traffic.CHECKOUT_KEY)
    variants = sorted(
        (SimpleNamespace(name=v["name"], traffic_allocation=v["traffic_allocation"]) for v in spec["variants"]),
        key=lambda v: v.name,
    )
    rng = random.Random(f"shoplab-history:{traffic.CHECKOUT_KEY}")
    exposed_at = datetime(2026, 1, 1, tzinfo=timezone.utc)
    assigned: dict[str, int] = {}
    bought: dict[str, int] = {}
    for i in range(users):
        variant = seed_shoplab._pick_variant(rng, variants)
        events = seed_shoplab._funnel_events(traffic.CHECKOUT_KEY, variant.name, f"u{i}", None, None, exposed_at, rng)
        assigned[variant.name] = assigned.get(variant.name, 0) + 1
        bought[variant.name] = bought.get(variant.name, 0) + any(e.event_type == "purchase" for e in events)
    return {name: bought[name] / n for name, n in assigned.items()}


def live_checkout_rates(visitors: int, seed: int) -> dict[str, float]:
    """What the simulator sends for the checkout experiment: visitors whose purchase
    carries its key, per visitor assigned to it (Stats.rate, the server's count)."""
    rng = random.Random(seed)
    stats = traffic.Stats()
    for _ in range(visitors):
        persona = traffic.pick_persona(rng)
        user_id = traffic.new_user_id(rng)
        attributes = traffic.make_attributes(rng, persona)
        variants, flags = traffic.offline_assignments(rng)
        stats.record(traffic.simulate_visitor(rng, user_id, persona, attributes, variants.__getitem__, flags))
    return {variant: stats.rate(traffic.CHECKOUT_KEY, variant) for variant in traffic.CHECKOUT_PURCHASE_RATE}


@pytest.mark.regression
def test_live_checkout_converts_like_the_seeded_history():
    """The seeded history assigns the checkout experiment to visitors who begin checkout,
    so about 62% / 68% of its users buy. The simulator assigned every visitor, so about
    8% / 9% of its users bought, and the results page mixed the two populations. It now
    assigns at begin_checkout, as the storefront's checkout page does."""
    history = history_checkout_rates(HISTORY_USERS)
    live = live_checkout_rates(LIVE_VISITORS, seed=1)
    assert set(history) == set(live) == set(traffic.CHECKOUT_PURCHASE_RATE)
    gaps = {variant: round(abs(live[variant] - history[variant]), 4) for variant in history}
    assert all(gap <= HISTORY_TOL for gap in gaps.values()), (gaps, live, history)
