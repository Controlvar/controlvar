"""Everything the visual editor's save takes is part of ``design_etag`` (#1545).

Start refuses a design that changed after the dashboard showed it, by
``design_etag``; a field the session's save can write that the etag misses
would slip past that check. The walker of ``test_design_etag.py`` goes over
the save's own model, ``EditorDesignChange``, filled with that test's design:
every leaf it reaches either maps to a leaf of the design the etag hashes,
and changing that leaf changes the etag, or is one of the two leaves that
only address the save (its version, and the id that picks a variant). The
leaf count is exact, so a walker that stops early is caught, and so is a new
field.

Planted defect: a field outside the etag on ``EditorDesignChange``
(``hypothesis``, say) fails ``PATCH leaf hypothesis not in etag``.
"""

from __future__ import annotations

import re
import uuid

import pytest

from backend.app.schemas.visual_editor import EditorDesignChange
from backend.app.services import visual
from backend.tests.unit.services.test_design_etag import (
    BASE_DESIGN,
    _as_data,
    _leaves,
    _rows,
    _walk,
    _with_leaf_changed,
)

pytestmark = pytest.mark.unit

#: Leaves of the save that address it rather than the design: its version,
#: and the id that picks each variant.
ADDRESSING = re.compile(r"^(design_etag|variants\[\d+\]\.id)$")

#: design_etag 1, targeting_rules 1 (opaque), each of 2 variants' id and
#: share 4, the metric's 11, a click goal's 5 and a page view's 4, the group 1.
LEAVES = 1 + 1 + 4 + 11 + 5 + 4 + 1


def _save() -> EditorDesignChange:
    """A save carrying every part, with the values of ``BASE_DESIGN``."""
    config = BASE_DESIGN["visual_config"]
    return EditorDesignChange(
        design_etag="0" * 64,
        targeting_rules=BASE_DESIGN["targeting_rules"],
        variants=[
            {"id": uuid.uuid4(), "traffic_allocation": variant.traffic_allocation}
            for variant in BASE_DESIGN["variants"]
        ],
        metrics=BASE_DESIGN["metrics"],
        goals=config.goals,
        mutual_exclusion_group_id=BASE_DESIGN["mutual_exclusion_group_id"],
    )


def _in_design(path: str):
    """The leaf of the design the etag hashes that *path* of the save
    writes, or ``None``."""
    if path in ("targeting_rules", "mutual_exclusion_group_id"):
        return path
    if re.fullmatch(r"variants\[\d+\]\.traffic_allocation", path):
        return path
    if path.startswith("metrics["):
        return path
    if path.startswith("goals["):
        return "visual_config." + path
    return None


def _save_leaves():
    save = _save()
    found = []
    for name in EditorDesignChange.model_fields:
        # targeting_rules is opaque, as in the design's own walk.
        sub, _ = _leaves(getattr(save, name), name)
        found += sub
    return found


def test_the_walker_reaches_every_leaf_of_the_save():
    leaves = _save_leaves()
    assert len(leaves) == LEAVES, f"leaves {len(leaves)} != {LEAVES}: {leaves}"


@pytest.mark.regression
def test_every_leaf_the_save_writes_is_in_the_etag():
    design_leaves = set(_walk()[0])
    data = _as_data()
    before = visual.design_etag(_rows(data))
    for path in _save_leaves():
        if ADDRESSING.match(path):
            continue
        target = _in_design(path)
        assert target is not None and target in design_leaves, (
            f"PATCH leaf {path} not in etag"
        )
        after = visual.design_etag(_rows(_with_leaf_changed(data, target)))
        assert after != before, f"PATCH leaf {path}: etag unchanged"
