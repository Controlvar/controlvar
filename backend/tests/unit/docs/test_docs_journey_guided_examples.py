"""The guided-examples page and its journey are the same steps, in the same order.

``docs/guides/guided-examples.md`` promises that every step on it is walked,
in order, by one journey (``tests/acceptance/docs/journeys/guided-examples.yaml``)
on the stack ``demo/setup-local.sh`` starts. The loader checks only the
journey's side (each ``doc`` anchor exists on the page), so a step added to the
page with no journey step, a step dropped from the journey, or the two put in
different orders would all load and pass. This lint holds the page's side:

* a page step is a level-3 heading ``### <n>. <title> {#<anchor>}`` under one of
  the page's walkthroughs (its level-2 sections), numbered 1, 2, 3, ... in each,
  with an explicit anchor; a numbered heading at another level is refused, and
  so is a level-3 heading that is not a numbered step;
* the page's step anchors, in page order, equal the journey's ``doc`` values in
  file order with each run of one anchor read once: the same set, the same
  order, every page step walked and no journey step outside a page step;
* each page step says "You will see" exactly once, and at least one of its
  journey steps checks what is seen: an ARIA snapshot, JSON, a delta, a table
  cell, a text or a command's output;
* the page's last section is "What these examples do not cover"
  (``#not-covered``), and it has no steps: no numbered heading, no numbered
  list and no shell block, so what is not walked is never written as a step.

Also held: the journey's visitor is the chooser's, outside the demo's global
holdout and in the arms the steps expect; the only counts that move are one
conversion in each of the visitor's arms the page names; no ARIA template node
is split at a colon in its name; and the page's simulator block, run
for real against stand-in processes, stops only the ShopLab simulator and is a
no-op once it has gone (the journey runs after demo-shoplab, which stops it).
"""

from __future__ import annotations

import copy
import functools
import os
import re
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Tuple

import pytest
import yaml

pytestmark = [pytest.mark.unit]

REPO_ROOT = Path(__file__).resolve().parents[4]
RUNNER_ROOT = REPO_ROOT / "tests" / "acceptance" / "docs"
if str(RUNNER_ROOT) not in sys.path:
    sys.path.insert(0, str(RUNNER_ROOT))

from docs_runner import commands, guide, loader, registry, traffic, values

from backend.scripts import seed_shoplab

PAGE = "guides/guided-examples.md"
PAGE_PATH = REPO_ROOT / "docs" / PAGE
JOURNEY = RUNNER_ROOT / "journeys" / "guided-examples.yaml"
#: The page's last section: what the examples do not walk, with no steps.
FINAL = "not-covered"
#: A page step's heading: its number, a dot, then its title.
NUMBERED = re.compile(r"^([0-9]+)\.\s+\S")
#: A numbered list item or a fence, which the final section may not hold.
LIST_ITEM = re.compile(r"^\s{0,3}[0-9]+[.)]\s")
FENCE = re.compile(r"^\s{0,3}(`{3,}|~{3,})")
SEEN = "You will see"
#: The expectations that check what a step's "You will see" says.
CHECKS = ("aria", "aria_one_of", "json", "delta", "cells", "text", "output")
#: values.choose_visitor's name for this journey's visitor.
VISITOR_NAME = "docs-guided-examples"
#: The arms the steps expect the visitor in, by experiment key.
ARMS = {
    "shoplab_hero_banner": "video_hero",
    "shoplab_pdp_buy_button": "control",
    "shoplab_checkout_flow": "standard",
}


# ---------------------------------------------------------------------------
# The lint
# ---------------------------------------------------------------------------
def page_steps(text: str) -> Tuple[List[Tuple[str, str]], List[str]]:
    """The page's steps, ``(walkthrough anchor, step anchor)`` in page order,
    and what is wrong with the page's structure."""
    page = guide.parse(PAGE, text)
    lines = text.splitlines()
    steps: List[Tuple[str, str]] = []
    problems: List[str] = []
    walkthrough = ""
    number = 0
    for heading in page.headings:
        numbered = NUMBERED.match(heading.text)
        if heading.level == 2:
            walkthrough, number = heading.anchor, 0
            continue
        if heading.level != 3:
            if numbered:
                problems.append(
                    f"line {heading.line + 1}: {heading.text!r} is numbered at level"
                    f" {heading.level}; a step is a level-3 heading"
                )
            continue
        if not numbered:
            problems.append(
                f"line {heading.line + 1}: {heading.text!r} is a level-3 heading that is"
                " not a numbered step"
            )
            continue
        number += 1
        if int(numbered.group(1)) != number:
            problems.append(
                f"line {heading.line + 1}: step {numbered.group(1)} of #{walkthrough}"
                f" should be step {number}"
            )
        if f"{{#{heading.anchor}}}" not in lines[heading.line]:
            problems.append(
                f"line {heading.line + 1}: step {heading.text!r} has no explicit"
                " {#anchor}"
            )
        if walkthrough == FINAL:
            problems.append(
                f"line {heading.line + 1}: step {heading.text!r} is in #{FINAL}, which"
                " lists what the examples do not walk and has no steps"
            )
            continue
        seen = guide.normalise_space(page.section(heading.anchor)).count(SEEN)
        if seen != 1:
            problems.append(
                f"step #{heading.anchor} says {SEEN!r} {seen} times; a step says it once"
            )
        steps.append((walkthrough, heading.anchor))
    level_two = [h.anchor for h in page.headings if h.level == 2]
    if not level_two or level_two[-1] != FINAL:
        problems.append(f"the page's last section is not #{FINAL}")
    for line in page.section(FINAL).splitlines()[1:]:
        if LIST_ITEM.match(line) or FENCE.match(line):
            problems.append(
                f"#{FINAL} holds {line.strip()!r}: a numbered item or a block is a"
                " step, and that section has none"
            )
    return steps, problems


def journey_anchors(data: Dict[str, Any]) -> List[str]:
    """The journey's ``doc`` values in file order, each run of one anchor once."""
    found: List[str] = []
    for step in data.get("steps") or []:
        if not found or found[-1] != step.get("doc"):
            found.append(step.get("doc"))
    return found


def problems(text: str, data: Dict[str, Any]) -> List[str]:
    """Everything that keeps the page's steps and the journey's from being one
    list in one order; empty when they are."""
    steps, found = page_steps(text)
    page = [anchor for _, anchor in steps]
    walked = journey_anchors(data)
    for anchor in page:
        if anchor not in walked:
            found.append(f"page step #{anchor} has no journey step")
    for anchor in walked:
        if anchor not in page:
            found.append(f"journey doc {anchor!r} is not a numbered step of the page")
    repeated = sorted({a for a in walked if walked.count(a) > 1})
    for anchor in repeated:
        found.append(
            f"journey doc {anchor!r} comes back after other steps; each page step's"
            " journey steps are together"
        )
    common = [a for a in page if a in walked]
    if not repeated and common != [a for a in walked if a in page]:
        found.append(
            "the journey walks the page's steps in another order: page "
            f"{common}, journey {[a for a in walked if a in page]}"
        )
    for anchor in page:
        checked = [
            step
            for step in data.get("steps") or []
            if step.get("doc") == anchor
            and any((step.get("expect") or {}).get(key) for key in CHECKS)
        ]
        if anchor in walked and not checked:
            found.append(
                f"page step #{anchor}: no journey step checks what it says you will see"
            )
    return found


@functools.lru_cache(maxsize=None)
def _page() -> str:
    return PAGE_PATH.read_text(encoding="utf-8")


@functools.lru_cache(maxsize=None)
def _journey_text() -> str:
    return JOURNEY.read_text(encoding="utf-8")


def _journey() -> Dict[str, Any]:
    """A fresh copy each time: the plants below change it."""
    return copy.deepcopy(_journey_cached())


@functools.lru_cache(maxsize=None)
def _journey_cached() -> Dict[str, Any]:
    return yaml.safe_load(_journey_text())


def test_the_page_and_its_journey_are_one_list_in_one_order():
    assert problems(_page(), _journey()) == []


def test_the_page_has_its_four_walkthroughs_and_the_final_section():
    page = guide.parse(PAGE, _page())
    assert [h.anchor for h in page.headings if h.level == 2] == [
        "before-you-start",
        "ab-test",
        "pause",
        "device",
        "rollout",
        FINAL,
    ]
    steps, _ = page_steps(_page())
    assert {walkthrough for walkthrough, _ in steps} == {"ab-test", "pause", "device"}


# ---------------------------------------------------------------------------
# Plants: each defect the lint exists for, red
# ---------------------------------------------------------------------------
EXTRA_STEP = (
    "### 10. Open the dashboard {#ab-extra}\n\n"
    "Open http://localhost:3100.\n\nYou will see the dashboard.\n\n"
)


def _before(text: str, marker: str, insert: str) -> str:
    assert marker in text
    return text.replace(marker, insert + marker, 1)


def test_a_page_step_with_no_journey_step_is_red():
    planted = _before(_page(), "## Pause the test", EXTRA_STEP)
    assert "page step #ab-extra has no journey step" in problems(planted, _journey())


def test_a_dropped_journey_step_is_red():
    data = _journey()
    data["steps"] = [s for s in data["steps"] if s["doc"] != "ab-open-the-panel"]
    assert "page step #ab-open-the-panel has no journey step" in problems(_page(), data)


def test_two_steps_swapped_are_red():
    data = _journey()
    first = [s for s in data["steps"] if s["doc"] == "device-premium-iphone"]
    second = [s for s in data["steps"] if s["doc"] == "device-search"]
    rest = [s for s in data["steps"] if s not in first + second]
    at = data["steps"].index(first[0])
    data["steps"] = rest[:at] + second + first + rest[at:]
    found = problems(_page(), data)
    assert any(
        p.startswith("the journey walks the page's steps in another order")
        for p in found
    )


def test_two_page_steps_swapped_are_red():
    text = _page()
    one = text.index("### 2. Pick a premium iPhone")
    two = text.index("### 3. Open Search")
    end = text.index("### 4. Pick a free iPhone")
    planted = (
        text[:one]
        + text[two:end].replace("### 3.", "### 2.", 1)
        + text[one:two].replace("### 2.", "### 3.", 1)
        + text[end:]
    )
    found = problems(planted, _journey())
    assert any(
        p.startswith("the journey walks the page's steps in another order")
        for p in found
    )


def test_a_step_in_the_final_section_is_red():
    planted = (
        _page()
        + "\n### 1. Complete the test {#complete-the-test}\n\nPress Complete.\n\nYou will see Completed.\n"
    )
    found = problems(planted, _journey())
    assert any(f"is in #{FINAL}" in p for p in found)


def test_a_numbered_list_in_the_final_section_is_red():
    planted = _page() + "\n1. Press Complete on the test's page.\n"
    found = problems(planted, _journey())
    assert any(
        p.startswith(f"#{FINAL} holds") and "1. Press Complete" in p for p in found
    )


def test_a_step_without_an_explicit_anchor_is_red():
    planted = _page().replace(
        "### 3. Open the panel {#ab-open-the-panel}", "### 3. Open the panel", 1
    )
    assert any("has no explicit {#anchor}" in p for p in problems(planted, _journey()))


def test_a_step_misnumbered_is_red():
    planted = _page().replace("### 3. Open the panel", "### 4. Open the panel", 1)
    assert any("should be step 3" in p for p in problems(planted, _journey()))


def test_a_step_that_says_you_will_see_twice_is_red():
    planted = _page().replace(
        "You will see the product list.",
        "You will see the product list. You will see its sort.",
        1,
    )
    assert any("You will see' 2 times" in p for p in problems(planted, _journey()))


def test_a_step_whose_journey_steps_check_nothing_is_red():
    data = _journey()
    for step in data["steps"]:
        if step["doc"] == "ab-stop-the-simulator":
            step["expect"] = {"exit": 0}
    assert (
        "page step #ab-stop-the-simulator: no journey step checks what it says you will see"
        in problems(_page(), data)
    )


# ---------------------------------------------------------------------------
# The journey as the runner reads it
# ---------------------------------------------------------------------------
def test_the_journey_loads_runs_every_step_and_is_last_in_the_demo_job():
    journey = loader.load(JOURNEY, loader.context_for(REPO_ROOT))
    assert "guided-examples" in registry.EVERY_STEP_RUN
    assert all(step.not_run is None and step.do.kind != "ref" for step in journey.steps)
    demo = sorted(
        path.stem
        for path in (RUNNER_ROOT / "journeys").glob("*.yaml")
        if yaml.safe_load(path.read_text(encoding="utf-8")).get("stack") == "demo-local"
    )
    assert demo[-1] == "guided-examples"


def _allocations(key: str) -> List[Tuple[str, int]]:
    spec = next(s for s in seed_shoplab.EXPERIMENTS if s["key"] == key)
    return [(v["name"], v["traffic_allocation"]) for v in spec["variants"]]


def test_the_visitor_is_the_choosers_outside_the_holdout_and_in_the_expected_arms():
    visitor = _journey()["visitor"]
    arms = {key: (_allocations(key), arm) for key, arm in ARMS.items()}
    assert visitor == values.choose_visitor(arms, name=VISITOR_NAME)
    assert not values.in_demo_holdout(visitor)
    for key, arm in ARMS.items():
        assert traffic.variant_for(visitor, key, _allocations(key)) == arm


def test_the_visitor_is_not_demo_shoplabs():
    """demo-shoplab's visitor has bought in the hero banner before this runs."""
    shoplab = yaml.safe_load(
        (RUNNER_ROOT / "journeys" / "demo-shoplab.yaml").read_text(encoding="utf-8")
    )
    assert shoplab.get("visitor") != _journey()["visitor"]


def test_the_steps_count_in_the_arms_the_visitor_is_in():
    """Every results read pins the arm at each index it counts; the only +1s
    are one conversion each in the visitor's hero arm (index 1) and buy-button
    arm (index 0), and every other count, users included, moves by 0."""
    hero = [name for name, _ in _allocations("shoplab_hero_banner")]
    buy_button = [name for name, _ in _allocations("shoplab_pdp_buy_button")]
    moved = []
    for step in _journey()["steps"]:
        expect = step.get("expect") or {}
        for path, delta in (expect.get("delta") or {}).items():
            match = re.fullmatch(
                r"metrics\.([01])\.variants\.([0-9])\.(conversions|sample_size)", path
            )
            assert match, (step["id"], path)
            pinned = f"metrics.{match.group(1)}.variants.{match.group(2)}.variant_name"
            assert pinned in expect["json"], (step["id"], pinned)
            if delta["by"] != 0:
                moved.append(
                    (step["id"], expect["json"][pinned], match.group(3), delta["by"])
                )
    assert moved == [
        ("press-counted", ARMS["shoplab_hero_banner"], "conversions", 1),
        ("paused-order-reached", ARMS["shoplab_pdp_buy_button"], "conversions", 1),
        ("order-counted", ARMS["shoplab_hero_banner"], "conversions", 1),
    ]
    assert hero.index(ARMS["shoplab_hero_banner"]) == 1
    assert buy_button.index(ARMS["shoplab_pdp_buy_button"]) == 0


def _split_nodes(node: Any, found: List[str]) -> List[str]:
    """Each name or text of a parsed ARIA template with an unbalanced quote."""
    if isinstance(node, list):
        for item in node:
            _split_nodes(item, found)
    elif isinstance(node, dict):
        for key, value in node.items():
            if isinstance(key, str) and key.count('"') % 2:
                found.append(key)
            _split_nodes(value, found)
    elif isinstance(node, str) and node.count('"') % 2:
        found.append(node)
    return found


def test_no_aria_node_is_split_at_a_colon_in_its_name():
    """A node such as ``form "Step 1: Address"`` read as YAML is a mapping from
    ``form "Step 1`` to ``Address"`` and matches no screen; such a node is
    quoted whole (demo-shoplab's first run failed on exactly this)."""
    split = []
    for step in _journey()["steps"]:
        expected = step.get("expect") or {}
        templates = [expected["aria"]] if expected.get("aria") else []
        for template in templates + (expected.get("aria_one_of") or []):
            split += [
                (step["id"], n) for n in _split_nodes(yaml.safe_load(template), [])
            ]
    assert split == []


HOME = "http://localhost:3200"
PANEL = "Powered by Controlvar"
PANEL_CLOSED = f'button "{PANEL}" [expanded=false]'


def _clicked(step: Dict[str, Any]) -> str:
    click = (step.get("do") or {}).get("click")
    return click.get("name", "") if isinstance(click, dict) else ""


def add_to_cart_problems(steps: List[Dict[str, Any]]) -> List[str]:
    """Each Add to cart pressed while the panel may be open, or after a page
    load other than the home page's.

    A press of the panel's button counts as closing it only when its snapshot
    says ``[expanded=false]``; any other press is taken to open it. A page
    load (``open`` or ``goto``) closes it.
    """
    found = []
    panel_open = False
    last_load = None
    for step in steps:
        do = step.get("do") or {}
        if "open" in do or "goto" in do:
            panel_open = False
            last_load = do.get("open") or do.get("goto")
        elif _clicked(step) == PANEL:
            aria = (step.get("expect") or {}).get("aria") or ""
            panel_open = PANEL_CLOSED not in aria
        elif _clicked(step) == "Add to cart":
            if panel_open:
                found.append(f"{step['id']}: the panel is open")
            if last_load != HOME:
                found.append(f"{step['id']}: the last page loaded is {last_load}")
    return found


def test_add_to_cart_is_pressed_with_the_panel_closed_after_the_home_page():
    """Two things the browser runs of this journey showed.

    The storefront's Controlvar panel stays open across the storefront's own
    links, and at the runner's 1280x720 window it covers the product page's
    Add to cart: the first run timed out pressing it. So the panel is closed
    first, and the step says so in its snapshot.

    The React SDK sends a purchase to the experiments it has answered for the
    visitor in the page's lifetime (sdk/react/src/client/attribution.ts keeps
    them in memory), so an order reaches the hero banner only if the home
    page, where the banner is answered, is the last page loaded. Opening the
    product page by its address instead lets the paused order's +0 hold
    without testing the pause, and in the second run the resumed order's +1
    had not come after 20 s. So the product page is reached by its link.
    """
    steps = _journey()["steps"]
    presses = [s["id"] for s in steps if _clicked(s) == "Add to cart"]
    assert presses == ["paused-add-to-cart", "add-to-cart-again"]
    assert add_to_cart_problems(steps) == []


def test_add_to_cart_with_the_panel_left_open_is_red():
    steps = [s for s in _journey()["steps"] if s["id"] != "panel-closed"]
    assert add_to_cart_problems(steps) == ["paused-add-to-cart: the panel is open"]


def test_add_to_cart_after_opening_the_product_page_by_its_address_is_red():
    steps = _journey()["steps"]
    address = f"{HOME}/products/p011"
    for step in steps:
        if step["id"] in ("paused-product", "product-again"):
            step["do"] = {"open": address}
    assert add_to_cart_problems(steps) == [
        f"paused-add-to-cart: the last page loaded is {address}",
        f"add-to-cart-again: the last page loaded is {address}",
    ]


# ---------------------------------------------------------------------------
# The page's simulator block, run for real against stand-in processes
# ---------------------------------------------------------------------------
SIMULATOR = "demo/shoplab/simulator/traffic.py"
STOPPED = "ShopLab simulator stopped"


def _stop_block() -> str:
    step = next(s for s in _journey()["steps"] if s["id"] == "stop-simulator")
    return step["do"]["run"]["command"]


def _stand_in(tmp_path: Path, simulator: bool = True):
    """A process with the simulator's command line (or, without *simulator*,
    another program the stale pid has come to name), its pid in the file
    setup-local.sh writes, taking a moment to exit on SIGTERM and reaped as
    soon as it does."""
    (tmp_path / "venv" / "bin").mkdir(parents=True)
    pids = tmp_path / "demo" / ".pids"
    pids.mkdir(parents=True)
    loop = "trap 'sleep 0.3; exit 0' TERM; while :; do sleep 0.1; done\n"
    if simulator:
        script = tmp_path / SIMULATOR
        script.parent.mkdir(parents=True)
        script.write_text(loop, encoding="utf-8")
        argv = ["/bin/sh", SIMULATOR]
    else:
        argv = ["/bin/sh", "-c", loop]
    child = subprocess.Popen(argv, cwd=tmp_path)
    threading.Thread(target=child.wait, daemon=True).start()
    (pids / "shoplab-simulator.pid").write_text(f"{child.pid}\n", encoding="utf-8")
    return child


def _finish(child) -> None:
    if child.poll() is None:
        child.kill()


def _gone(child, seconds: float = 5) -> None:
    deadline = time.monotonic() + seconds
    while child.poll() is None and time.monotonic() < deadline:
        time.sleep(0.02)
    assert child.poll() is not None


def test_the_stop_block_is_the_pages():
    section = guide.parse(PAGE, _page()).section("ab-stop-the-simulator")
    assert commands.block_problem(section, _stop_block()) is None


def test_the_stop_block_stops_the_simulator_and_prints_once_it_has_gone(tmp_path):
    child = _stand_in(tmp_path)
    try:
        finished = commands.run(_stop_block(), tmp_path, 20, os.environ)
        gone = child.poll() is not None
    finally:
        _finish(child)
    assert finished.status == 0, finished.output
    assert STOPPED in finished.output
    assert gone, "the line was printed while the simulator still ran"


def test_the_stop_block_is_a_no_op_once_the_simulator_has_gone(tmp_path):
    """demo-shoplab stops it first, and the pid file stays behind."""
    child = _stand_in(tmp_path)
    child.kill()
    _gone(child)
    finished = commands.run(_stop_block(), tmp_path, 20, os.environ)
    assert finished.status == 0, finished.output
    assert STOPPED in finished.output


def test_the_stop_block_leaves_another_process_alone(tmp_path):
    child = _stand_in(tmp_path, simulator=False)
    try:
        finished = commands.run(_stop_block(), tmp_path, 20, os.environ)
        time.sleep(0.5)  # a TERM it had been sent would have ended it by now
        running = child.poll() is None
    finally:
        _finish(child)
    assert finished.status == 0, finished.output
    assert running, "the block signalled a process that is not the simulator"
