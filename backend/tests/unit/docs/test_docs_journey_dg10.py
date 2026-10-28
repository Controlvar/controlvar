"""demo-shoplab's order check: an order placed in the storefront lands in the
visitor's arm, exactly, and in no arm while the experiment is paused.

The journey's last steps (``tests/acceptance/docs/journeys/demo-shoplab.yaml``)
run only in the nightly demo job, on the stack ``demo/setup-local.sh`` starts.
What decides whether they can pass for the wrong reason is written in the file
and in the sources it reads, and is held here:

* the visitor: the chooser's answer, outside the demo's global holdout, and in
  the arm every count is read for (the checkout flow's ``standard``), by the
  documented assignment hash and by the API's own;
* the arm the API holds for the visitor is asked before anything is counted,
  and every count is read for that arm and the other by name (a variant-order
  change fails rather than passing for the other arm);
* the simulator ``demo/setup-local.sh`` started is stopped by the README's own
  block, first, is not restarted, and the block prints only once the process
  has gone (run here for real against a stand-in process);
* the "before" counts are read after the visitor's assignment, with
  ``use_cache=false`` on every results read;
* the exact amounts: the order placed while paused is one more event for the
  experiment and +0 users and conversions in each arm; the order placed while
  it runs is +1 conversion in the visitor's arm and +0 elsewhere;
* the paused order is placed on the page that was assigned while the
  experiment ran (no reload between), so it reaches the API tagged with the
  experiment and only the API's status check keeps it out of the arm; and it
  comes before any counted order, since a user is counted once;
* the results page is read for the numbers the API answered last;
* every new step runs: none declares ``not_run``;
* the words the steps expect are the storefront's and the seed's own.
"""

from __future__ import annotations

import functools
import os
import re
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Dict, List, Optional

import pytest
import yaml

pytestmark = [pytest.mark.unit]

REPO_ROOT = Path(__file__).resolve().parents[4]
RUNNER_ROOT = REPO_ROOT / "tests" / "acceptance" / "docs"
if str(RUNNER_ROOT) not in sys.path:
    sys.path.insert(0, str(RUNNER_ROOT))

from docs_runner import commands, guide, loader, registry, traffic, values

from backend.scripts import seed_shoplab

JOURNEY = RUNNER_ROOT / "journeys" / "demo-shoplab.yaml"
README = REPO_ROOT / "demo" / "shoplab" / "README.md"
SETUP = REPO_ROOT / "demo" / "setup-local.sh"
SHOPLAB = REPO_ROOT / "demo" / "shoplab" / "src"
CHECKOUT_PAGE = SHOPLAB / "pages" / "checkout.tsx"
PRODUCT_PAGE = SHOPLAB / "pages" / "products" / "[id].tsx"
PRODUCTS = SHOPLAB / "data" / "products.ts"
LABELS = SHOPLAB / "lib" / "labels.ts"
PANEL = SHOPLAB / "components" / "ControlvarPanel.tsx"

CHECKOUT = "shoplab_checkout_flow"
BUY_BUTTON = "shoplab_pdp_buy_button"
#: The steps this check added, in order: the simulator stopped, then every
#: step of the measured window, the last ShopLab steps.
NEW_STEPS = [
    "stop-simulator",
    "product",
    "add-to-cart",
    "checkout",
    "checkout-arm",
    "checkout-arm-api",
    "pause-checkout",
    "before-order",
    "paused-name",
    "paused-email",
    "paused-address",
    "paused-city",
    "paused-postal",
    "paused-shipping",
    "paused-payment",
    "paused-card",
    "paused-order",
    "paused-order-in-no-arm",
    "paused-checkout-page",
    "paused-panel",
    "resume-checkout",
    "resumed-checkout-page",
    "resumed-panel",
    "product-again",
    "add-to-cart-again",
    "checkout-again",
    "order-name",
    "order-email",
    "order-address",
    "order-city",
    "order-postal",
    "order-shipping",
    "order-payment",
    "order-card",
    "order-placed",
    "order-in-its-arm",
    "results-page",
    "complete-checkout",
    "completed-checkout-page",
    "completed-panel",
]
#: Steps that reload a storefront page (a new SDK client, which asks again).
RELOADS = ("open",)


# ---------------------------------------------------------------------------
# Readers
# ---------------------------------------------------------------------------
@functools.lru_cache(maxsize=None)
def _journey() -> Dict:
    """The journey file as YAML, read once per run (no test changes it)."""
    return yaml.safe_load(JOURNEY.read_text(encoding="utf-8"))


def _steps() -> List[Dict]:
    return _journey()["steps"]


def _step(step_id: str) -> Dict:
    return next(step for step in _steps() if step["id"] == step_id)


def _index(step_id: str) -> int:
    return [step["id"] for step in _steps()].index(step_id)


def _experiment(key: str) -> Dict:
    return next(e for e in seed_shoplab.EXPERIMENTS if e["key"] == key)


def _allocations(key: str):
    return [(v["name"], v["traffic_allocation"]) for v in _experiment(key)["variants"]]


def _visitor() -> str:
    return _journey()["visitor"]


def _arm(key: str) -> str:
    return traffic.variant_for(_visitor(), key, _allocations(key))


def _other_arm() -> str:
    (other,) = [name for name, _ in _allocations(CHECKOUT) if name != _arm(CHECKOUT)]
    return other


def _arm_index(name: str) -> int:
    """The arm's place in the results: the seed's order (the API orders an
    experiment's variants as they were created)."""
    return [n for n, _ in _allocations(CHECKOUT)].index(name)


def _results_reads() -> List[Dict]:
    return [
        step
        for step in _steps()
        if step["do"].get("api", {}).get("path", "").startswith("/api/v1/results/")
    ]


def _found(pattern: str, text: str, where: Path) -> str:
    match = re.search(pattern, text)
    assert match, f"{where.name} no longer has {pattern!r}"
    return match.group(1)


def _panel_row(reason: str) -> str:
    """The checkout row's text after its key, as the panel draws it for an
    answer that did not enrol the visitor, for *reason* (labels.ts: the note,
    then the pill, read as one text)."""
    labels = LABELS.read_text(encoding="utf-8")
    pill_completed = _found(
        r"answer\.reason === 'completed' \? '([^']+)'", labels, LABELS
    )
    pill_other = _found(
        r"answer\.reason === 'completed' \? '[^']+' : '([^']+)'", labels, LABELS
    )
    if reason == "completed":
        note = _found(r"case 'completed':\s*return '([^']+)';", labels, LABELS)
        pill = pill_completed
    else:
        template = _found(r"default:\s*return `([^`]+)`;", labels, LABELS)
        note = template.replace("${answer.reason ?? 'unknown'}", reason)
        pill = pill_other
    return f"{note} {pill}"


def _aria(step_id: str) -> List:
    return yaml.safe_load(_step(step_id)["expect"]["aria"])


def _checkout_row(step_id: str) -> str:
    """The text the step expects after the checkout flow's key in the panel."""
    panel = _aria(step_id)[0]['complementary "Controlvar panel"']
    lists = [
        node["list"] for node in panel if isinstance(node, dict) and "list" in node
    ]
    for items in lists:
        for item in items:
            row = item["listitem"]
            if row[1] == {"code": CHECKOUT}:
                assert row[0] == {"text": "Checkout flow"}, row
                return row[2]["text"]
    raise AssertionError(f"{step_id} expects no checkout flow row")


# ---------------------------------------------------------------------------
# A known visitor, in a known arm, outside the holdout
# ---------------------------------------------------------------------------
def test_the_visitor_is_the_choosers_and_outside_the_demo_holdout():
    assert _visitor() == values.choose_visitor()
    assert values.is_uuid_v4(_visitor())
    assert not values.in_demo_holdout(_visitor())


def test_the_arm_counted_is_the_visitors_arm_by_the_documented_hash():
    """The arm the steps expect the API to hold, and count +1 in, is the one the
    documented assignment hash gives the visitor; the other arm counts +0."""
    arm = _arm(CHECKOUT)
    assert arm == "standard"
    answer = _step("checkout-arm-api")["expect"]["json"]
    assert answer["variant_name"] == arm
    assert answer["assigned"] is True
    assert answer["user_id"] == _visitor()
    assert answer["experiment_key"] == CHECKOUT
    assert _step("checkout-arm-api")["do"]["api"]["body"] == {
        "experiment_key": CHECKOUT,
        "user_id": _visitor(),
    }
    assert _checkout_row("checkout-arm") == arm
    assert _checkout_row("resumed-panel") == arm


def test_the_arm_counted_is_the_apis_own_assignment():
    """The API's own hash, over the seed's variants in the seed's order, gives
    the visitor the arm the steps count in."""
    from types import SimpleNamespace

    from backend.app.services.assignment_service import AssignmentService

    api = AssignmentService.__new__(AssignmentService)  # the hash reads no session
    created = [
        SimpleNamespace(id=name, traffic_allocation=share)
        for name, share in _allocations(CHECKOUT)
    ]
    experiment = SimpleNamespace(id=CHECKOUT, key=CHECKOUT, variants=created)
    assert api._hash_user_to_variant(_visitor(), experiment) == _arm(CHECKOUT)


def test_the_buy_button_arm_keeps_the_words_the_steps_click():
    (variant,) = [
        v for v in _experiment(BUY_BUTTON)["variants"] if v["name"] == _arm(BUY_BUTTON)
    ]
    for step_id in ("add-to-cart", "add-to-cart-again"):
        assert _step(step_id)["do"]["click"]["name"] == variant["configuration"]["text"]
    for step_id in ("product", "product-again"):
        assert (
            f'button "{variant["configuration"]["text"]}"'
            in _step(step_id)["expect"]["aria"]
        )


# ---------------------------------------------------------------------------
# The arm the API returned is asked before anything is counted
# ---------------------------------------------------------------------------
def test_the_api_is_asked_for_the_arm_before_anything_is_counted():
    first_count = _index(_results_reads()[0]["id"])
    assert _index("checkout") < _index("checkout-arm") < _index("checkout-arm-api")
    assert _index("checkout-arm-api") < first_count


def test_every_count_is_read_for_an_arm_named_in_the_same_answer():
    """Each results read names the variant at each index it counts, so an arm
    in another order fails the read instead of counting the other arm."""
    arm, other = _arm(CHECKOUT), _other_arm()
    for step in _results_reads():
        expected = step["expect"]["json"]
        assert expected[f"metrics.0.variants.{_arm_index(arm)}.variant_name"] == arm
        assert expected[f"metrics.0.variants.{_arm_index(other)}.variant_name"] == other
        if "metrics.0.metric_name" in expected:
            assert expected["metrics.0.metric_name"] == "Purchase"


# ---------------------------------------------------------------------------
# The simulator stopped, the "before" read after the assignment
# ---------------------------------------------------------------------------
def test_the_simulator_is_stopped_by_the_readmes_block_and_stays_stopped():
    ids = [step["id"] for step in _steps()]
    start = ids.index("stop-simulator")
    assert ids[start:] == NEW_STEPS, "the new steps are the journey's last, in order"
    assert ids[start - 1] == "banner-back"
    stop = _step("stop-simulator")
    assert stop["doc"] == "traffic-simulator"
    assert stop["expect"] == {"exit": 0, "output": ["ShopLab simulator stopped"]}
    assert "background" not in stop["do"]["run"]
    page = guide.parse("demo/shoplab/README.md", README.read_text(encoding="utf-8"))
    section = page.section("traffic-simulator")
    assert commands.block_problem(section, stop["do"]["run"]["command"]) is None
    # Nothing after it starts the simulator again: no other block runs.
    later = [
        s["id"]
        for s in _steps()[start + 1 :]
        if "run" in s["do"] or "finish" in s["do"]
    ]
    assert later == []


def test_the_stop_block_names_the_pid_file_setup_local_writes():
    setup = SETUP.read_text(encoding="utf-8")
    assert '> "$DEMO_DIR/.pids/shoplab-simulator.pid"' in setup
    started = _found(
        r"nohup (python demo/shoplab/simulator/traffic\.py[^\n>]*)", setup, SETUP
    )
    assert "--duration" not in started, "it runs until stopped"
    command = _step("stop-simulator")["do"]["run"]["command"]
    assert 'pid="$(cat demo/.pids/shoplab-simulator.pid)"' in command


def test_the_stop_block_prints_last_after_waiting_for_the_process():
    """The order the test below cannot see: it checks the process has gone
    when the block ends, and this that nothing is printed before the wait."""
    lines = _step("stop-simulator")["do"]["run"]["command"].strip("\n").splitlines()
    assert lines[0] == 'pid="$(cat demo/.pids/shoplab-simulator.pid)"'
    assert lines[1] == 'kill "$pid"'
    assert lines[2].startswith('while kill -0 "$pid"'), lines
    assert lines[3:] == ["printf 'ShopLab simulator stopped\\n'"], lines


def test_every_results_read_skips_the_cache():
    reads = _results_reads()
    assert len(reads) == 3
    for step in reads:
        assert step["do"]["api"]["path"].endswith("?use_cache=false"), step["id"]


def test_the_before_read_follows_the_assignment_and_the_pause():
    assert _index("checkout") < _index("pause-checkout") < _index("before-order")
    assert _index("checkout-arm-api") < _index("before-order")
    assert _step("before-order")["expect"]["json"]["status"] == "paused"
    assert _step("pause-checkout")["expect"]["json"]["status"] == "paused"


# ---------------------------------------------------------------------------
# Exact amounts
# ---------------------------------------------------------------------------
def _saved(step_id: str) -> Dict[str, str]:
    return {name: save["path"] for name, save in _step(step_id)["save"].items()}


def _deltas(step_id: str) -> Dict[str, tuple]:
    return {
        path: (delta["since"], delta["by"])
        for path, delta in _step(step_id)["expect"]["delta"].items()
    }


def test_the_paused_order_is_one_more_event_and_in_no_arm():
    saved = _saved("before-order")
    arm, other = _arm_index(_arm(CHECKOUT)), _arm_index(_other_arm())
    wanted = {"summary.total_events": 1}
    for index in (arm, other):
        wanted[f"metrics.0.variants.{index}.conversions"] = 0
        wanted[f"metrics.0.variants.{index}.sample_size"] = 0
    deltas = _deltas("paused-order-in-no-arm")
    assert {path: by for path, (_, by) in deltas.items()} == wanted
    for path, (since, _) in deltas.items():
        assert saved[since] == path, (path, since)
    assert _step("paused-order-in-no-arm")["poll"] == {
        "up_to_seconds": 60,
        "every_seconds": 5,
    }


def test_the_running_order_counts_once_in_the_visitors_arm():
    saved = _saved("before-order")
    arm, other = _arm_index(_arm(CHECKOUT)), _arm_index(_other_arm())
    wanted = {
        f"metrics.0.variants.{arm}.conversions": 1,
        f"metrics.0.variants.{other}.conversions": 0,
        f"metrics.0.variants.{arm}.sample_size": 0,
        f"metrics.0.variants.{other}.sample_size": 0,
    }
    deltas = _deltas("order-in-its-arm")
    assert {path: by for path, (_, by) in deltas.items()} == wanted
    for path, (since, _) in deltas.items():
        assert saved[since] == path, (path, since)
    assert _step("order-in-its-arm")["expect"]["json"]["status"] == "active"
    assert "poll" in _step("order-in-its-arm")


def test_the_paused_order_comes_first_and_on_the_page_assigned_while_running():
    """A user converts once: the paused order must come before the counted one.
    And it is placed on the checkout page opened before the pause, with no
    reload between (a reload asks the API again, which answers "paused", and
    the SDK then sends the order to no experiment at all, so the API's status
    check would go untested)."""
    assert (
        _index("paused-order")
        < _index("paused-order-in-no-arm")
        < _index("order-placed")
    )
    between = _steps()[_index("checkout") + 1 : _index("paused-order")]
    assert [s["id"] for s in between if any(k in s["do"] for k in RELOADS)] == []
    assert _index("pause-checkout") < _index("paused-order") < _index("resume-checkout")
    assert (
        _index("resume-checkout") < _index("order-placed") < _index("complete-checkout")
    )


# ---------------------------------------------------------------------------
# The results page shows the API's numbers
# ---------------------------------------------------------------------------
def test_the_results_page_is_read_for_the_numbers_the_api_answered_last():
    saved = _saved("order-in-its-arm")
    arm, other = _arm(CHECKOUT), _other_arm()
    arm_row = f"{arm}(control)" if _arm_index(arm) == 0 else arm
    cells = _step("results-page")["expect"]["cells"]
    seen = [
        (
            c["row"],
            c.get("also"),
            c["column"],
            saved[c["delta"]["since"]],
            c["delta"]["by"],
        )
        for c in cells
    ]
    assert seen == [
        (
            arm_row,
            "Purchaseprimary",
            "Sample Size",
            f"metrics.0.variants.{_arm_index(arm)}.sample_size",
            0,
        ),
        (
            other,
            "Purchaseprimary",
            "Sample Size",
            f"metrics.0.variants.{_arm_index(other)}.sample_size",
            0,
        ),
        (
            arm_row,
            "Order value",
            "Value",
            f"metrics.1.variants.{_arm_index(arm)}.mean",
            0,
        ),
    ]
    assert (
        _index("order-in-its-arm")
        < _index("results-page")
        < _index("complete-checkout")
    )
    name = _experiment(CHECKOUT)["name"]
    assert f'heading "{name}" [level=2]' in _step("results-page")["expect"]["aria"]


def test_the_metric_names_are_the_seeds():
    metrics = _experiment(CHECKOUT)["metrics"]
    primary = [m["name"] for m in metrics if m["is_primary"]]
    assert primary == ["Purchase"]
    assert [m["name"] for m in metrics] == ["Purchase", "Order value"]
    controls = [v["name"] for v in _experiment(CHECKOUT)["variants"] if v["is_control"]]
    assert controls == ["standard"]


# ---------------------------------------------------------------------------
# The new steps run
# ---------------------------------------------------------------------------
def test_the_new_steps_run():
    assert "demo-shoplab" in registry.EVERY_STEP_RUN
    assert set(NEW_STEPS) <= {step["id"] for step in _steps()}
    for step in _steps():
        if step["id"] in NEW_STEPS:
            assert "not_run" not in step, step["id"]
            assert step["do"].get("ref") is None, step["id"]


def test_the_journey_loads_as_written():
    journey = loader.load(JOURNEY, loader.context_for(REPO_ROOT))
    assert journey.visitor == _visitor()
    assert [s.id for s in journey.steps][-len(NEW_STEPS) :] == NEW_STEPS


# ---------------------------------------------------------------------------
# The words the steps expect are the storefront's own
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "source, words",
    [
        (CHECKOUT_PAGE, "Thanks for your order!"),
        (CHECKOUT_PAGE, "Your cart is empty"),
        (CHECKOUT_PAGE, ">Checkout</h1>"),
        (CHECKOUT_PAGE, "const STEPS = ['Address', 'Shipping', 'Payment'] as const;"),
        (CHECKOUT_PAGE, "aria-label={`Step ${step + 1}: ${STEPS[step]}`}"),
        (CHECKOUT_PAGE, 'aria-label="Step 3: Payment"'),
        (CHECKOUT_PAGE, "Continue"),
        (CHECKOUT_PAGE, "Place order · {formatPrice(orderTotal)}"),
        (CHECKOUT_PAGE, "{ id: 'standard', label: 'Standard (3–5 days)', price: 0 }"),
        (CHECKOUT_PAGE, "shipping: 'standard', card: ''"),
        (CHECKOUT_PAGE, "Full name"),
        (CHECKOUT_PAGE, ">\n          Email\n"),
        (CHECKOUT_PAGE, "Street address"),
        (CHECKOUT_PAGE, ">\n            City\n"),
        (CHECKOUT_PAGE, "Postal code"),
        (CHECKOUT_PAGE, "Card number (any digits — demo only)"),
        (
            CHECKOUT_PAGE,
            "track('purchase', { items, flow, order_id: orderId }, { value: orderTotal });",
        ),
        (
            PRODUCT_PAGE,
            "Added to your cart ({cart.count} {cart.count === 1 ? 'item' : 'items'}).",
        ),
        (PRODUCT_PAGE, "Go to checkout"),
        (PRODUCTS, "name: 'Field Watch',"),
        (PANEL, "label: 'Checkout flow'"),
        (PANEL, 'data-testid="panel-visitor-id"'),
    ],
)
def test_the_words_the_steps_expect_are_in_the_storefront(source, words):
    assert words in source.read_text(encoding="utf-8"), f"{source.name}: {words!r}"


def test_the_order_button_names_the_products_price():
    products = PRODUCTS.read_text(encoding="utf-8")
    block = products.split("id: 'p011',", 1)[1].split("}", 1)[0]
    price = int(_found(r"price: (\d+),", block, PRODUCTS))
    name = _found(r"name: '([^']+)',", block, PRODUCTS)
    shown = f"${price:,.2f}"
    for step_id in ("paused-order", "order-placed"):
        assert _step(step_id)["do"]["click"]["name"] == f"Place order · {shown}"
    for step_id in ("product",):
        assert _step(step_id)["do"]["click"]["name"] == f"{name}, {shown}"
    for step_id in ("product", "product-again"):
        assert f'heading "{name}" [level=1]' in _step(step_id)["expect"]["aria"]
    assert _step("product-again")["do"]["open"] == "http://localhost:3200/products/p011"


def test_the_panel_rows_are_the_panels_words_for_each_status():
    assert _checkout_row("paused-panel") == _panel_row("paused")
    assert _checkout_row("completed-panel") == _panel_row("completed")
    assert _panel_row("paused") == "not enrolled: paused (control shown) not enrolled"
    assert _panel_row("completed") == "experiment completed (control shown) completed"


def test_the_visitor_is_the_one_the_panel_steps_expect():
    for step_id in ("panel", "checkout-arm"):
        assert f"- code: {_visitor()}" in _step(step_id)["expect"]["aria"]


# ---------------------------------------------------------------------------
# The README's stop block, run for real against a stand-in simulator
# ---------------------------------------------------------------------------
def _stand_in(tmp_path: Path, delay: float = 0.5):
    """A process that takes *delay* seconds to exit after SIGTERM, its pid in
    the file setup-local.sh writes, reaped as soon as it exits (so a check that
    it has gone does not wait on a zombie)."""
    (tmp_path / "venv" / "bin").mkdir(parents=True)
    pids = tmp_path / "demo" / ".pids"
    pids.mkdir(parents=True)
    child = subprocess.Popen(
        [
            "/bin/sh",
            "-c",
            f"trap 'sleep {delay}; exit 0' TERM; while :; do sleep 0.1; done",
        ]
    )
    threading.Thread(target=child.wait, daemon=True).start()
    (pids / "shoplab-simulator.pid").write_text(f"{child.pid}\n", encoding="utf-8")
    return child


def _stop_block() -> str:
    return _step("stop-simulator")["do"]["run"]["command"]


def _finish(child) -> None:
    if child.poll() is None:
        child.kill()


def test_the_stop_block_prints_its_line_only_once_the_process_has_gone(tmp_path):
    child = _stand_in(tmp_path)
    try:
        finished = commands.run(_stop_block(), tmp_path, 20, os.environ)
        gone = child.poll() is not None
    finally:
        _finish(child)
    assert finished.status == 0, finished.output
    assert "ShopLab simulator stopped" in finished.output
    assert gone, "the line was printed while the simulator still ran"


def test_the_stop_block_fails_when_the_simulator_is_not_running(tmp_path):
    child = _stand_in(tmp_path)
    child.kill()
    deadline = time.monotonic() + 5
    while child.poll() is None and time.monotonic() < deadline:
        time.sleep(0.05)
    finished = commands.run(_stop_block(), tmp_path, 20, os.environ)
    assert finished.status != 0
    assert "ShopLab simulator stopped" not in finished.output


def test_a_block_that_does_not_wait_is_caught(tmp_path):
    """Positive control for the test above: the same block without its wait
    prints while the stand-in still runs, and the check says so."""
    planted = "\n".join(
        line for line in _stop_block().splitlines() if not line.startswith("while ")
    )
    assert planted != _stop_block()
    child = _stand_in(tmp_path, delay=1.0)
    try:
        finished = commands.run(planted, tmp_path, 20, os.environ)
        gone: Optional[bool] = child.poll() is not None
    finally:
        _finish(child)
    assert finished.status == 0
    assert gone is False
