"""Every flag the seeds and the StreamPulse story write has rules a save accepts (#535 V13).

The seeds write flags through the ORM, so the API's targeting check never sees
them. A seeded flag whose rules the API refuses cannot be saved back from the
dashboard or with a ``PUT`` that sends its rules. Each flag site is found in
the source, its rules resolved from the module's literals, and judged by
``validate_flag_targeting``. The count of sites is exact, so a seed that gains
a flag is judged too rather than skipped.
"""

from __future__ import annotations

import ast
import pathlib
from typing import Any, Dict, List, Tuple

import pytest

from backend.app.core.targeting_adapter import (
    TargetingRulesError,
    validate_flag_targeting,
)

pytestmark = [pytest.mark.unit]

REPO_ROOT = pathlib.Path(__file__).resolve().parents[4]
SEEDS = (
    "backend/scripts/seed_demo_data.py",
    "backend/scripts/seed_sdk_contract.py",
    "backend/scripts/seed_shoplab.py",
    "backend/scripts/seed_streampulse.py",
)
STORY = "demo/streampulse/simulator/rollout_story.py"


def _constants(tree: ast.Module) -> Dict[str, Any]:
    """Module-level names whose value is built from literals and earlier names."""
    names: Dict[str, Any] = {}
    for node in tree.body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            target, value = node.targets[0], node.value
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            target, value = node.target, node.value
        else:
            continue
        if not isinstance(target, ast.Name):
            continue
        try:
            names[target.id] = eval(
                compile(ast.Expression(value), "<seed>", "eval"),
                {"__builtins__": {}},
                dict(names),
            )
        except Exception:
            continue
    return names


def _flag_sites(path: str) -> List[Tuple[str, Any]]:
    tree = ast.parse((REPO_ROOT / path).read_text("utf-8"))
    names = _constants(tree)
    sites: List[Tuple[str, Any]] = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "FeatureFlag"
        ):
            for keyword in node.keywords:
                if keyword.arg != "targeting_rules":
                    continue
                source = ast.unparse(keyword.value)
                if source == "spec['targeting_rules']":
                    continue  # seed_streampulse: the FLAGS entries, below
                sites.append(
                    (
                        f"{path}:{keyword.value.lineno}",
                        eval(
                            compile(ast.Expression(keyword.value), path, "eval"),
                            {"__builtins__": {}},
                            dict(names),
                        ),
                    )
                )
    for spec in names.get("FLAGS", []):
        sites.append((f"{path}:FLAGS[{spec['key']}]", spec["targeting_rules"]))
    return sites


def _story_sites() -> List[Tuple[str, Any]]:
    """The two rule sets the StreamPulse rollout story PUTs."""
    tree = ast.parse((REPO_ROOT / STORY).read_text("utf-8"))
    names = _constants(tree)
    player_v2 = dict(_flag_sites("backend/scripts/seed_streampulse.py"))[
        "backend/scripts/seed_streampulse.py:FLAGS[streampulse_player_v2]"
    ]
    employee_groups = [
        g
        for g in player_v2["groups"]
        if any(c.get("attribute") == "employee" for c in g["conditions"])
    ]
    sites = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Assign)
            and isinstance(node.targets[0], ast.Name)
            and node.targets[0].id == "new_rules"
        ):
            sites.append(
                (
                    f"{STORY}:{node.lineno}",
                    eval(
                        compile(ast.Expression(node.value), STORY, "eval"),
                        {"__builtins__": {}},
                        {**names, "employee_groups": employee_groups},
                    ),
                )
            )
        if isinstance(node, ast.Dict):
            for key, value in zip(node.keys, node.values):
                if (
                    isinstance(key, ast.Constant)
                    and key.value == "targeting_rules"
                    and not isinstance(value, ast.Name)
                ):
                    sites.append((f"{STORY}:{value.lineno}", ast.literal_eval(value)))
    return sites


def _verdict(rules: Any) -> str:
    try:
        validate_flag_targeting(rules)
    except TargetingRulesError as err:
        return f"refused: {err}"
    return "accepted"


@pytest.mark.regression
def test_every_seeded_flag_has_rules_a_save_accepts():
    sites = [site for path in SEEDS for site in _flag_sites(path)] + _story_sites()
    verdicts = {where: _verdict(rules) for where, rules in sites}
    # 12 since #1497 (F5): seed_sdk_contract.py's value flags are one more site.
    assert len(sites) == 12, sorted(verdicts)
    assert {w: v for w, v in verdicts.items() if v != "accepted"} == {}


def test_the_story_rules_are_the_ones_with_targeting():
    """Guards the walk: the story's step-5 rules and the StreamPulse rules are
    among the sites, with groups in them, not only ``{}``."""
    story = dict(_story_sites())
    assert len(story) == 2
    assert any(len(r.get("groups", [])) == 2 for r in story.values())
    streampulse = dict(_flag_sites("backend/scripts/seed_streampulse.py"))
    assert sum(1 for r in streampulse.values() if r) == 2
