"""Every test id video 9's board names is one the dashboard draws (PE 4, EM-9).

The board finds its elements by ``data-testid``; a renamed id fails the take
only when it is reached, minutes into a capture. This reads frontend/src
(tests aside) and resolves each id the product writes, by rule and with no
list of exceptions:

* a quoted id is itself;
* an id written as a template literal, or held in a ``const`` such a literal
  or a quoted string sets, has each ``${name}`` resolved in its own file: a
  ``const`` it names is expanded; a parameter typed with a string-literal union
  (``which: ScheduleField``, ``'start' | 'end'``) is each of its strings;
  anything else is a number (the rows' ``-0``, ``-1``).

So ``schedule-${which}`` and ``${id}-readback`` give exactly
``schedule-start-readback`` and ``schedule-end-readback``, and the board's
``schedule-bogus-readback`` is refused (EM-9). No browser, network or build.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Dict, List, Set

import pytest
import yaml
from showcase.capture import config, storyboard

pytestmark = [pytest.mark.unit]

REPO = Path(__file__).resolve().parents[4]
FRONTEND = REPO / "frontend" / "src"
SHOWCASE = REPO / "tests" / "acceptance" / "showcase"
SLUG = "09-design-in-depth"

QUOTED = re.compile(r"""data-testid=(?:"([^"]+)"|\{\s*['"]([^'"]+)['"]\s*\})""")
TEMPLATE = re.compile(r"data-testid=\{\s*`([^`]+)`\s*\}")
NAMED = re.compile(r"data-testid=\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*\}")
CONST = re.compile(
    r"""const\s+([A-Za-z_]\w*)\s*=\s*(?:`([^`]*)`|'([^']*)'|"([^"]*)")\s*;"""
)
UNION = re.compile(r"type\s+(\w+)\s*=\s*((?:'[^']*'\s*\|\s*)+'[^']*')\s*;")
TYPED = re.compile(r"\(\s*([A-Za-z_]\w*)\s*:\s*([A-Za-z_]\w*)\s*\)")
PART = re.compile(r"\$\{\s*([^}]*?)\s*\}")
CSS_TESTID = re.compile(r"data-testid=['\"]?([a-z0-9-]+)")
NUMBER = "[0-9]+"


def frontend_sources() -> Dict[str, str]:
    return {
        str(path.relative_to(FRONTEND)): path.read_text(encoding="utf-8")
        for path in sorted(FRONTEND.rglob("*.ts*"))
        if "/tests/" not in f"/{path.relative_to(FRONTEND)}"
        and ".test." not in path.name
    }


def unions(sources: Dict[str, str]) -> Dict[str, List[str]]:
    """Every string-literal union type the frontend declares, by name."""
    found: Dict[str, List[str]] = {}
    for text in sources.values():
        for name, body in UNION.findall(text):
            found[name] = re.findall(r"'([^']*)'", body)
    return found


def expand(
    template: str, text: str, types: Dict[str, List[str]], depth: int = 0
) -> List[str]:
    """*template*'s ids as full-match patterns, each ``${name}`` resolved in
    *text* (its own file) by the module docstring's rule."""
    if depth > 4:
        return []
    consts = {name: a or b or c for name, a, b, c in CONST.findall(text)}
    params = dict(TYPED.findall(text))
    patterns = [""]
    at = 0
    for part in PART.finditer(template):
        literal = re.escape(template[at : part.start()])
        name = part.group(1)
        if name in consts:
            options = expand(consts[name], text, types, depth + 1)
        elif name in params and params[name] in types:
            options = [re.escape(v) for v in types[params[name]]]
        else:
            options = [NUMBER]
        patterns = [p + literal + o for p in patterns for o in options]
        at = part.end()
    tail = re.escape(template[at:])
    return [p + tail for p in patterns]


def product_ids(sources: Dict[str, str]) -> List[str]:
    """Every id the dashboard draws, as full-match patterns."""
    types = unions(sources)
    found: Set[str] = set()
    for text in sources.values():
        for double, single in QUOTED.findall(text):
            found.add(re.escape(double or single))
        for template in TEMPLATE.findall(text):
            found.update(expand(template, text, types))
        consts = {m[0]: m for m in CONST.findall(text)}
        for name in NAMED.findall(text):
            if name in consts:
                _n, template, single, double = consts[name]
                found.update(expand(template or single or double, text, types))
    return sorted(found)


def board_ids(made: storyboard.Storyboard) -> List[str]:
    """Every test id the board names: by ``testid``, or inside a CSS selector."""
    found: Set[str] = set()
    for scene in made.scenes:
        for beat in scene.beats:
            locators = [beat.focus, *beat.record.values()]
            for step in [*beat.before, *beat.steps, *beat.exit]:
                locators += storyboard._locators(step)
            for locator in locators:
                if locator.testid:
                    found.add(locator.testid)
                found.update(CSS_TESTID.findall(locator.css or ""))
    return sorted(found)


def missing(ids: List[str], patterns: List[str]) -> List[str]:
    compiled = [re.compile(p) for p in patterns]
    return [i for i in ids if not any(p.fullmatch(i) for p in compiled)]


def board() -> storyboard.Storyboard:
    return storyboard.load(SHOWCASE / config.VIDEOS[SLUG])


def raw() -> dict:
    return yaml.safe_load((SHOWCASE / config.VIDEOS[SLUG]).read_text(encoding="utf-8"))


def test_every_test_id_video_9_names_is_one_the_dashboard_draws():
    ids = board_ids(board())
    assert len(ids) == 65
    assert missing(ids, product_ids(frontend_sources())) == []


def test_the_schedule_fields_computed_ids_resolve_to_start_and_end_only():
    patterns = product_ids(frontend_sources())
    for which in ("start", "end"):
        for suffix in ("", "-zone", "-readback", "-help", "-remove"):
            assert missing([f"schedule-{which}{suffix}"], patterns) == []
    assert missing(["schedule-bogus-readback", "schedule-0-readback"], patterns) == [
        "schedule-bogus-readback",
        "schedule-0-readback",
    ]


def test_a_board_id_the_dashboard_does_not_draw_is_refused():
    """EM-9's plant: a board id built like a computed one, but not one of its
    values, is refused; so is a renamed one (PE 4)."""
    patterns = product_ids(frontend_sources())
    for wrong in ("schedule-bogus-readback", "schedule-start-read-back"):
        data = raw()
        steps = data["scenes"][0]["beats"][12]["steps"]
        look = next(
            s
            for s in steps
            if s.get("look", {}).get("testid") == "schedule-start-readback"
        )
        look["look"]["testid"] = wrong
        assert missing(board_ids(storyboard.parse(data)), patterns) == [wrong]


def test_a_renamed_product_id_is_refused():
    """PE 4's plant on the product side: the read-back's id renamed in the
    schedule section (in memory) leaves the board's id unmatched."""
    sources = frontend_sources()
    name = "components/experiments/ExperimentScheduleSection.tsx"
    assert "data-testid={`${id}-readback`}" in sources[name]
    sources[name] = sources[name].replace("${id}-readback`}", "${id}-read-back`}")
    assert missing(board_ids(board()), product_ids(sources)) == [
        "schedule-start-readback"
    ]
