#!/usr/bin/env python3
"""ShopLab traffic simulator.

Drives synthetic visitors through the ShopLab storefront funnel using only the public
Controlvar API and the storefront API key — no database access, stdlib only.

Per visitor:
  1. POST /api/v1/tracking/assign for the four ShopLab experiments (context = persona attributes)
  2. GET  /api/v1/feature-flags/evaluate/{key}?user_id=… for the two flags
  3. Walk the funnel with the true conversion rates from the shared spec (§3), scaled by persona
  4. POST /api/v1/tracking/batch (≤100 events per call) with an explicit experiment_key for
     every assigned experiment (fan-out, like the React SDK) and feature_flag_key for search.

Usage:
  python demo/shoplab/simulator/traffic.py --rate 3 --duration 600
  python demo/shoplab/simulator/traffic.py --dry-run --seed 1
"""
from __future__ import annotations

import argparse
import json
import math
import random
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator, TextIO

# --------------------------------------------------------------------------------------
# Catalogue of experiments / flags (mirrors the seed script and src/lib/env.ts)
# --------------------------------------------------------------------------------------

DEFAULT_API_URL = "http://localhost:8000"
DEFAULT_API_KEY_FILE = Path(__file__).resolve().parent.parent / ".api_key"
BATCH_LIMIT = 100
REPORT_INTERVAL_S = 10.0

HERO_KEY = "shoplab_hero_banner"
PLP_KEY = "shoplab_plp_sort"
PDP_KEY = "shoplab_pdp_buy_button"
CHECKOUT_KEY = "shoplab_checkout_flow"
EXPERIMENT_KEYS: tuple[str, ...] = (HERO_KEY, PLP_KEY, PDP_KEY, CHECKOUT_KEY)

NEW_SEARCH_FLAG = "shoplab_new_search"
FREE_SHIPPING_FLAG = "shoplab_free_shipping_banner"
FLAG_KEYS: tuple[str, ...] = (NEW_SEARCH_FLAG, FREE_SHIPPING_FLAG)

# Variant traffic split. Only used offline (--dry-run / tests); live, the server decides.
VARIANTS: dict[str, dict[str, int]] = {
    HERO_KEY: {"control": 50, "video_hero": 50},
    PLP_KEY: {"relevance": 34, "price_low_high": 33, "ml_personalized": 33},
    PDP_KEY: {"control": 25, "green_add": 25, "orange_buy_now": 25, "green_buy_now": 25},
    CHECKOUT_KEY: {"standard": 50, "one_page": 50},
}

PRIMARY_METRIC: dict[str, str] = {
    HERO_KEY: "hero_cta_click",
    PLP_KEY: "product_click",
    PDP_KEY: "add_to_cart",
    CHECKOUT_KEY: "purchase",
}

# Variant configuration the storefront would render (used for event metadata).
HERO_MEDIA = {"control": "image", "video_hero": "video"}
PLP_ALGORITHM = {"relevance": "relevance", "price_low_high": "price_asc", "ml_personalized": "ml"}
PDP_BUTTON_TEXT = {"control": "Add to cart", "green_add": "Add to cart", "orange_buy_now": "Buy now", "green_buy_now": "Buy now"}
CHECKOUT_FLOW = {"standard": "standard", "one_page": "one_page"}

# --------------------------------------------------------------------------------------
# True conversion rates (spec §3) — the history seeder uses the same numbers
# --------------------------------------------------------------------------------------

HERO_CTA_RATE = {"control": 0.12, "video_hero": 0.15}
HERO_PURCHASE_AFTER_CLICK = 0.25
PLP_CLICK_RATE = {"relevance": 0.22, "price_low_high": 0.19, "ml_personalized": 0.28}
ADD_TO_CART_AFTER_CLICK = 0.30
PDP_ADD_RATE = {"control": 0.080, "green_add": 0.085, "orange_buy_now": 0.105, "green_buy_now": 0.095}
CHECKOUT_PURCHASE_RATE = {"standard": 0.62, "one_page": 0.68}
ORDER_VALUE_MEDIAN = 85.0
ORDER_VALUE_SIGMA = 0.5
SEARCH_CLICK_RATE = {True: 0.35, False: 0.25}  # keyed by "new search flag on?"
DIRECT_PDP_RATE = 0.10  # visitors who open a product page without clicking in the list


def _mean(values: Iterable[float]) -> float:
    vals = list(values)
    return sum(vals) / len(vals)


def rate_for(table: dict[str, float], variant: str) -> float:
    """Rate for a variant; unknown names (renamed on the server) fall back to the table mean."""
    return table.get(variant, _mean(table.values()))


def clamp(p: float) -> float:
    return max(0.0, min(1.0, p))


# --------------------------------------------------------------------------------------
# Personas
# --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Persona:
    name: str
    weight: float  # share of traffic
    intent: float  # multiplier on click / add-to-cart probabilities
    purchase_shift: float  # additive shift on P(purchase | begin_checkout)
    search_rate: float  # probability of using the search page


# (name, traffic share, intent, search rate). The traffic-weighted mean of `intent` is
# exactly 1.0, so population-level click / add-to-cart rates equal the spec while personas
# still differ. Purchase shifts are calibrated below.
_PERSONA_BASE: tuple[tuple[str, float, float, float], ...] = (
    ("casual", 0.60, 0.80, 0.30),
    ("shopper", 0.30, 1.20, 0.45),
    ("power", 0.10, 1.60, 0.65),
)

# add_to_cart is coupled to product_click so that P(add | click) ≈ ADD_TO_CART_AFTER_CLICK
# while the marginal P(add) per buy-button variant stays at PDP_ADD_RATE.
_MEAN_HERO_CTA = _mean(HERO_CTA_RATE.values())
_MEAN_PLP_CLICK = _mean(PLP_CLICK_RATE.values())
_MEAN_PDP_ADD = _mean(PDP_ADD_RATE.values())
_MEAN_CHECKOUT = _mean(CHECKOUT_PURCHASE_RATE.values())
_BOOST_CLICK = ADD_TO_CART_AFTER_CLICK / _MEAN_PDP_ADD
_BOOST_NOCLICK = (1.0 - _MEAN_PLP_CLICK * _BOOST_CLICK) / (1.0 - _MEAN_PLP_CLICK)
_ADD_NORM = sum(
    weight * intent * ((_MEAN_PLP_CLICK * intent) * _BOOST_CLICK + (1.0 - _MEAN_PLP_CLICK * intent) * _BOOST_NOCLICK)
    for _, weight, intent, _ in _PERSONA_BASE
)
# Hero clickers sometimes go straight to checkout so that P(purchase | hero click) ≈ 25%.
_HERO_QUICK_CHECKOUT = (HERO_PURCHASE_AFTER_CLICK / _MEAN_CHECKOUT - _MEAN_PDP_ADD) / (1.0 - _MEAN_PDP_ADD)


def begin_checkout_share(intent: float) -> float:
    """Expected P(begin_checkout) for a persona with this intent (averaged over variants)."""
    click = _MEAN_PLP_CLICK * intent
    boost = click * _BOOST_CLICK + (1.0 - click) * _BOOST_NOCLICK
    p_add = _MEAN_PDP_ADD * intent * boost / _ADD_NORM
    p_quick = _MEAN_HERO_CTA * intent * _HERO_QUICK_CHECKOUT
    return p_add + p_quick - p_add * p_quick


def _build_personas() -> tuple[Persona, ...]:
    """Purchase shifts: casual/shopper are chosen; power's is solved so that the shift
    averaged over visitors who *reach checkout* is exactly zero (high-intent personas reach
    checkout more often, so a plain traffic-weighted zero would bias P(purchase | begin))."""
    shifts = {"casual": -0.07, "shopper": +0.05}
    weighted = {name: weight * begin_checkout_share(intent) for name, weight, intent, _ in _PERSONA_BASE}
    shifts["power"] = -sum(weighted[n] * s for n, s in shifts.items()) / weighted["power"]
    return tuple(Persona(name, weight, intent, shifts[name], search) for name, weight, intent, search in _PERSONA_BASE)


PERSONAS: tuple[Persona, ...] = _build_personas()


def pick_persona(rng: random.Random) -> Persona:
    return rng.choices(PERSONAS, weights=[p.weight for p in PERSONAS], k=1)[0]


DEVICES = (("desktop", 0.55), ("mobile", 0.38), ("tablet", 0.07))
COUNTRIES = (("US", 0.55), ("GB", 0.12), ("DE", 0.10), ("CA", 0.08), ("FR", 0.06), ("AU", 0.05), ("NL", 0.04))


def make_attributes(rng: random.Random, persona: Persona) -> dict[str, Any]:
    device = rng.choices([d for d, _ in DEVICES], weights=[w for _, w in DEVICES], k=1)[0]
    country = rng.choices([c for c, _ in COUNTRIES], weights=[w for _, w in COUNTRIES], k=1)[0]
    returning = rng.random() < {"casual": 0.25, "shopper": 0.55, "power": 0.85}[persona.name]
    return {"device": device, "country": country, "returning": returning, "persona": persona.name}


# --------------------------------------------------------------------------------------
# Catalogue (mirrors demo/shoplab/src/data/products.ts)
# --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Product:
    id: str
    name: str
    category: str
    price: float


PRODUCTS: tuple[Product, ...] = (
    Product("p001", "Trailhead Backpack 45L", "Outdoor", 129),
    Product("p002", "Summit Tent 2P", "Outdoor", 249),
    Product("p003", "Ember Camp Stove", "Outdoor", 59),
    Product("p004", "Ridge Runner Trail Shoes", "Footwear", 139),
    Product("p005", "Harbor Canvas Sneakers", "Footwear", 69),
    Product("p006", "Alpine Insulated Boots", "Footwear", 189),
    Product("p007", "Merino Base Layer", "Apparel", 79),
    Product("p008", "Stormshell Rain Jacket", "Apparel", 159),
    Product("p009", "Everyday Chino Pants", "Apparel", 64),
    Product("p010", "Solstice Sunglasses", "Accessories", 89),
    Product("p011", "Field Watch", "Accessories", 199),
    Product("p012", "Wool Beanie", "Accessories", 29),
)

SEARCH_QUERIES = (
    "jacket", "rain jaket", "boots", "tent", "watch", "backpack", "sneakers",
    "beanie", "stove", "sunglasses", "warm layer", "trail shoes",
)

# --------------------------------------------------------------------------------------
# Visitor funnel
# --------------------------------------------------------------------------------------


@dataclass
class VisitorPlan:
    user_id: str
    persona: Persona
    attributes: dict[str, Any]
    assignments: dict[str, str]  # experiment key -> variant name
    flags: dict[str, bool]  # flag key -> enabled
    events: list[dict[str, Any]] = field(default_factory=list)  # /tracking/track bodies
    outcomes: dict[str, bool] = field(default_factory=dict)  # named funnel steps hit
    order_value: float = 0.0

    def converted(self, experiment_key: str) -> bool:
        """Did the primary metric fire with this experiment's key (what the server sees)?"""
        metric = PRIMARY_METRIC[experiment_key]
        return any(e.get("experiment_key") == experiment_key and e["event_name"] == metric for e in self.events)


def simulate_visitor(
    rng: random.Random,
    user_id: str,
    persona: Persona,
    attributes: dict[str, Any],
    assignments: dict[str, str],
    flags: dict[str, bool],
    start_time: datetime | None = None,
) -> VisitorPlan:
    """Pure funnel walk: returns the visitor's events and outcomes. No network."""
    plan = VisitorPlan(user_id, persona, attributes, dict(assignments), dict(flags))
    clock = start_time or datetime.now(timezone.utc)

    def emit(
        name: str,
        metadata: dict[str, Any] | None = None,
        value: float | None = None,
        *,
        flag: str | None = None,
    ) -> None:
        nonlocal clock
        clock = clock + timedelta(seconds=rng.uniform(2.0, 20.0))
        base: dict[str, Any] = {
            "event_type": name,
            "event_name": name,
            "user_id": user_id,
            "metadata": metadata or {},
            "timestamp": clock.isoformat(timespec="seconds"),
        }
        if value is not None:
            base["value"] = round(float(value), 2)
        if flag is not None:
            plan.events.append({**base, "feature_flag_key": flag})
            return
        # Fan out to every experiment the visitor is assigned to (SDK behaviour).
        for key in EXPERIMENT_KEYS:
            if key in plan.assignments:
                plan.events.append({**base, "experiment_key": key})

    hero_variant = assignments[HERO_KEY]
    plp_variant = assignments[PLP_KEY]
    pdp_variant = assignments[PDP_KEY]
    checkout_variant = assignments[CHECKOUT_KEY]
    new_search_on = bool(flags.get(NEW_SEARCH_FLAG, False))

    # --- Home / hero ---------------------------------------------------------------
    emit("page_view", {"page": "home"})
    hero_click = rng.random() < clamp(rate_for(HERO_CTA_RATE, hero_variant) * persona.intent)
    if hero_click:
        emit("hero_cta_click", {"variant": hero_variant, "media": HERO_MEDIA.get(hero_variant, "image")})
    hero_quick = hero_click and rng.random() < _HERO_QUICK_CHECKOUT

    # --- Product list --------------------------------------------------------------
    emit("page_view", {"page": "products"})
    algorithm = PLP_ALGORITHM.get(plp_variant, "relevance")
    product_click = rng.random() < clamp(rate_for(PLP_CLICK_RATE, plp_variant) * persona.intent)
    product = rng.choice(PRODUCTS)
    if product_click:
        emit("product_click", {"product_id": product.id, "position": rng.randint(1, len(PRODUCTS)), "sort": algorithm})

    # --- Search (flag-attributed only, so the sort bandit's product_click stays clean) ---
    searched = rng.random() < persona.search_rate
    search_click = False
    if searched:
        emit("page_view", {"page": "search"})
        engine = "fuzzy" if new_search_on else "exact"
        results = rng.randint(1, 5) if new_search_on else rng.randint(1, 3)
        emit("search", {"query": rng.choice(SEARCH_QUERIES), "results": results, "engine": engine}, flag=NEW_SEARCH_FLAG)
        search_click = rng.random() < SEARCH_CLICK_RATE[new_search_on]
        if search_click:
            emit("product_click", {"product_id": product.id, "position": rng.randint(1, results), "sort": "search"}, flag=NEW_SEARCH_FLAG)

    # --- Product detail / add to cart ----------------------------------------------
    boost = _BOOST_CLICK if product_click else _BOOST_NOCLICK
    add_to_cart = rng.random() < clamp(rate_for(PDP_ADD_RATE, pdp_variant) * persona.intent * boost / _ADD_NORM)
    reach_pdp = product_click or search_click or hero_quick or add_to_cart or rng.random() < DIRECT_PDP_RATE
    quantity = rng.choices([1, 2, 3], weights=[80, 15, 5], k=1)[0]
    if reach_pdp:
        emit("page_view", {"page": "product"})
    if add_to_cart:
        emit(
            "add_to_cart",
            {"product_id": product.id, "quantity": quantity, "button": PDP_BUTTON_TEXT.get(pdp_variant, "Add to cart")},
            value=product.price,
        )

    # --- Checkout ------------------------------------------------------------------
    begin_checkout = add_to_cart or hero_quick
    purchase = False
    if begin_checkout:
        flow = CHECKOUT_FLOW.get(checkout_variant, "standard")
        items = quantity if add_to_cart else 1
        cart_total = product.price * items
        emit("page_view", {"page": "checkout"})
        emit("begin_checkout", {"items": items, "flow": flow}, value=cart_total)
        purchase = rng.random() < clamp(rate_for(CHECKOUT_PURCHASE_RATE, checkout_variant) + persona.purchase_shift)
        if purchase:
            plan.order_value = round(rng.lognormvariate(math.log(ORDER_VALUE_MEDIAN), ORDER_VALUE_SIGMA), 2)
            order_id = "SL-" + uuid.UUID(int=rng.getrandbits(128)).hex[:10].upper()
            emit("purchase", {"items": items, "flow": flow, "order_id": order_id}, value=plan.order_value)

    plan.outcomes = {
        "hero_click": hero_click,
        "hero_quick": hero_quick,
        "product_click": product_click,
        "searched": searched,
        "search_click": search_click,
        "add_to_cart": add_to_cart,
        "begin_checkout": begin_checkout,
        "purchase": purchase,
    }
    return plan


# --------------------------------------------------------------------------------------
# API client (urllib only)
# --------------------------------------------------------------------------------------


class ApiError(Exception):
    def __init__(self, status: int, body: str, url: str = ""):
        super().__init__(f"HTTP {status} {url}: {body[:300]}")
        self.status = status
        self.body = body
        self.url = url


class ApiClient:
    def __init__(self, api_url: str = DEFAULT_API_URL, api_key: str = "", timeout: float = 5.0):
        self.api_url = api_url.rstrip("/")
        self.api_key = api_key
        self.timeout = timeout

    def request(
        self,
        method: str,
        path: str,
        body: Any = None,
        params: dict[str, str] | None = None,
    ) -> Any:
        url = f"{self.api_url}/api/v1{path}"
        if params:
            url += "?" + urllib.parse.urlencode(params)
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(
            url,
            data=data,
            method=method,
            headers={"X-API-Key": self.api_key, "Content-Type": "application/json", "Accept": "application/json"},
        )
        raw = b""
        for attempt in range(3):
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    raw = resp.read()
                break
            except urllib.error.HTTPError as e:
                if e.code == 429 and attempt < 2:
                    # Back off for the server's Retry-After (capped) and retry
                    # instead of counting a burst of rate-limit rejections as errors.
                    try:
                        wait = float(e.headers.get("Retry-After", "1"))
                    except (TypeError, ValueError):
                        wait = 1.0
                    time.sleep(min(max(wait, 0.5), 5.0))
                    continue
                raise ApiError(e.code, e.read().decode("utf-8", "replace"), url) from None
            except urllib.error.URLError as e:
                raise ApiError(0, str(e.reason), url) from None
            except (TimeoutError, OSError) as e:
                raise ApiError(0, str(e), url) from None
        if not raw:
            return None
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            raise ApiError(0, f"non-JSON response: {raw[:100]!r}", url) from None

    def assign(self, experiment_key: str, user_id: str, context: dict[str, Any]) -> dict[str, Any]:
        return self.request("POST", "/tracking/assign", {"experiment_key": experiment_key, "user_id": user_id, "context": context})

    def evaluate_flag(self, flag_key: str, user_id: str) -> dict[str, Any]:
        return self.request("GET", f"/feature-flags/evaluate/{flag_key}", params={"user_id": user_id})

    def batch(self, events: list[dict[str, Any]]) -> dict[str, Any]:
        if len(events) > BATCH_LIMIT:
            raise ValueError(f"batch too large: {len(events)} > {BATCH_LIMIT}")
        return self.request("POST", "/tracking/batch", {"events": events}) or {}


def chunked(items: list[dict[str, Any]], size: int = BATCH_LIMIT) -> Iterator[list[dict[str, Any]]]:
    for i in range(0, len(items), size):
        yield items[i : i + size]


# --------------------------------------------------------------------------------------
# Stats
# --------------------------------------------------------------------------------------


class Stats:
    def __init__(self) -> None:
        self.started = time.monotonic()
        self.visitors = 0
        self.events_sent = 0
        self.errors = 0
        self.rejected = 0  # failure_count reported by /tracking/batch
        self.purchases = 0
        self.revenue = 0.0
        self.variant_visitors: dict[str, Counter[str]] = {k: Counter() for k in EXPERIMENT_KEYS}
        self.variant_conversions: dict[str, Counter[str]] = {k: Counter() for k in EXPERIMENT_KEYS}
        self.flag_on: Counter[str] = Counter()
        self.flag_evaluated: Counter[str] = Counter()
        self.personas: Counter[str] = Counter()

    def record(self, plan: VisitorPlan) -> None:
        self.visitors += 1
        self.events_sent += len(plan.events)
        self.personas[plan.persona.name] += 1
        for key in EXPERIMENT_KEYS:
            variant = plan.assignments.get(key)
            if variant is None:
                continue
            self.variant_visitors[key][variant] += 1
            if plan.converted(key):
                self.variant_conversions[key][variant] += 1
        for key, enabled in plan.flags.items():
            self.flag_evaluated[key] += 1
            if enabled:
                self.flag_on[key] += 1
        if plan.outcomes.get("purchase"):
            self.purchases += 1
            self.revenue += plan.order_value

    def rate(self, experiment_key: str, variant: str) -> float:
        n = self.variant_visitors[experiment_key][variant]
        return self.variant_conversions[experiment_key][variant] / n if n else 0.0

    def render(self) -> str:
        elapsed = max(time.monotonic() - self.started, 1e-9)
        mm, ss = divmod(int(elapsed), 60)
        lines = [
            f"[{mm:02d}:{ss:02d}] visitors={self.visitors} ({self.visitors / elapsed:.1f}/s) "
            f"events={self.events_sent} errors={self.errors} rejected={self.rejected} "
            f"purchases={self.purchases} revenue=${self.revenue:,.2f}"
        ]
        for key in EXPERIMENT_KEYS:
            parts = []
            for variant, n in sorted(self.variant_visitors[key].items()):
                parts.append(f"{variant}={n} {100 * self.rate(key, variant):.1f}%")
            lines.append(f"  {key:<26} {PRIMARY_METRIC[key]:<15} " + "  ".join(parts))
        flags = "  ".join(
            f"{key} on={self.flag_on[key]}/{self.flag_evaluated[key]}"
            for key in FLAG_KEYS
            if self.flag_evaluated[key]
        )
        if flags:
            lines.append(f"  flags: {flags}")
        if self.personas:
            lines.append("  personas: " + "  ".join(f"{p}={n}" for p, n in sorted(self.personas.items())))
        return "\n".join(lines)


# --------------------------------------------------------------------------------------
# Driver
# --------------------------------------------------------------------------------------


def new_user_id(rng: random.Random) -> str:
    return str(uuid.UUID(int=rng.getrandbits(128), version=4))


def run_visitor(client: Any, rng: random.Random, stats: Stats) -> VisitorPlan:
    """Assign, evaluate flags, walk the funnel and send the events for one visitor."""
    persona = pick_persona(rng)
    user_id = new_user_id(rng)
    attributes = make_attributes(rng, persona)
    assignments: dict[str, str] = {}
    for key in EXPERIMENT_KEYS:
        resp = client.assign(key, user_id, attributes)
        assignments[key] = str(resp["variant_name"])
    flags = {key: bool(client.evaluate_flag(key, user_id).get("enabled")) for key in FLAG_KEYS}
    plan = simulate_visitor(rng, user_id, persona, attributes, assignments, flags)
    for chunk in chunked(plan.events):
        resp = client.batch(chunk)
        stats.rejected += int(resp.get("failure_count", 0) or 0)
    stats.record(plan)
    return plan


def offline_assignments(rng: random.Random) -> tuple[dict[str, str], dict[str, bool]]:
    """Pick variants/flags locally (dry-run and tests only)."""
    assignments = {
        key: rng.choices(list(split.keys()), weights=list(split.values()), k=1)[0] for key, split in VARIANTS.items()
    }
    flags = {NEW_SEARCH_FLAG: rng.random() < 0.10, FREE_SHIPPING_FLAG: True}
    return assignments, flags


def dry_run(rng: random.Random, out: TextIO) -> VisitorPlan:
    persona = pick_persona(rng)
    user_id = new_user_id(rng)
    attributes = make_attributes(rng, persona)
    assignments, flags = offline_assignments(rng)
    plan = simulate_visitor(rng, user_id, persona, attributes, assignments, flags)
    print(f"# dry run — no network. visitor={user_id} persona={persona.name}", file=out)
    print(f"# attributes={json.dumps(attributes)}", file=out)
    print(f"# assignments={json.dumps(assignments)}", file=out)
    print(f"# flags={json.dumps(flags)}", file=out)
    print(f"# outcomes={json.dumps(plan.outcomes)}", file=out)
    print(f"# {len(plan.events)} track bodies would be sent in {math.ceil(len(plan.events) / BATCH_LIMIT)} batch call(s):", file=out)
    for event in plan.events:
        print(json.dumps(event, sort_keys=True), file=out)
    return plan


def read_api_key_file(path: Path) -> str | None:
    try:
        key = path.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return key or None


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="traffic.py",
        description="Generate realistic ShopLab visitor traffic against the Controlvar public API.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--api-url", default=DEFAULT_API_URL, help="Controlvar API origin")
    parser.add_argument("--api-key", default=None, help=f"X-API-Key value (default: contents of {DEFAULT_API_KEY_FILE})")
    parser.add_argument("--rate", type=float, default=3.0, help="visitors per second")
    parser.add_argument("--duration", type=float, default=0.0, help="seconds to run (0 = forever)")
    parser.add_argument("--seed", type=int, default=None, help="random seed for reproducible traffic")
    parser.add_argument("--max-visitors", type=int, default=0, help="stop after this many visitors (0 = no limit)")
    parser.add_argument("--dry-run", action="store_true", help="print the first visitor's planned events and exit (no network)")
    return parser


FATAL_EXIT_CODES = {401: 2, 403: 2, 404: 3, 0: 4}


def fatal_message(err: ApiError, api_url: str) -> str | None:
    if err.status in (401, 403):
        return (
            f"Unauthorized ({err.status}) from {api_url}: the API key was rejected. "
            "Re-run the ShopLab seed script (it writes demo/shoplab/.api_key) or pass --api-key."
        )
    if err.status == 404:
        return (
            f"Not found (404) from {err.url}: the ShopLab experiments are not seeded or not ACTIVE, or its flags are not seeded. "
            "Run the ShopLab seed script first."
        )
    if err.status == 0:
        return f"Cannot reach {api_url}: {err.body}. Is the backend running (uvicorn app.main:app --port 8000)?"
    return None


def main(
    argv: list[str] | None = None,
    client_factory: Callable[..., Any] = ApiClient,
    sleep: Callable[[float], None] = time.sleep,
    out: TextIO = sys.stdout,
) -> int:
    args = build_parser().parse_args(argv)
    if args.rate <= 0:
        print("error: --rate must be > 0", file=sys.stderr)
        return 2
    rng = random.Random(args.seed)

    if args.dry_run:
        dry_run(rng, out)
        return 0

    api_key = args.api_key or read_api_key_file(DEFAULT_API_KEY_FILE)
    if not api_key:
        print(
            f"error: no API key. Pass --api-key or run the ShopLab seed script to create {DEFAULT_API_KEY_FILE}.",
            file=sys.stderr,
        )
        return 2

    client = client_factory(args.api_url, api_key)
    stats = Stats()
    interval = 1.0 / args.rate
    start = time.monotonic()
    next_report = start + REPORT_INTERVAL_S
    print(
        f"ShopLab traffic → {args.api_url}  key={api_key[:4]}…  rate={args.rate}/s  "
        f"duration={'∞' if not args.duration else f'{args.duration:g}s'}  seed={args.seed}",
        file=out,
    )
    exit_code = 0
    try:
        while True:
            now = time.monotonic()
            if args.duration and now - start >= args.duration:
                break
            if args.max_visitors and stats.visitors >= args.max_visitors:
                break
            try:
                run_visitor(client, rng, stats)
            except ApiError as err:
                message = fatal_message(err, args.api_url)
                if message:
                    print(f"\nerror: {message}", file=sys.stderr)
                    exit_code = FATAL_EXIT_CODES.get(err.status, 1)
                    break
                stats.errors += 1
                print(f"warning: {err}", file=sys.stderr)
            if time.monotonic() >= next_report:
                print(stats.render(), file=out)
                next_report += REPORT_INTERVAL_S
            target = start + stats.visitors * interval
            delay = target - time.monotonic()
            if delay > 0:
                sleep(delay)
    except KeyboardInterrupt:
        print("\ninterrupted", file=out)
    print(stats.render(), file=out)
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
