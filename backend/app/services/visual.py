"""
A visual experiment's rules across fields, its design etag, and the start
check every start path makes (#1545).

* :func:`visual_start_problem` is the ONE check that a visual draft is ready
  to start. ``POST /start`` answers 400 with its text; the scheduler's
  ``_validate_experiment_for_start`` returns False on it. Non-visual
  experiments always get ``None``.
* :func:`design_etag` names one version of a draft's design: its targeting,
  exclusion group, visual settings, and every column of every variant and
  metric row. The dashboard's Start sends the etag of the design it showed, so
  a design changed after the page loaded is not started (409). It is computed
  from the row, never from a cache.
* :func:`revoke_editor_tokens` ends every editor ticket, session and claim of
  an experiment; both start paths call it in the start's own transaction.

Every text here is fixed: an experiment id, a variant name or a goal's event
name is the only value one carries.
"""

from __future__ import annotations

import hashlib
import json
from enum import Enum
from typing import Any, Dict, List, Optional, Sequence
from uuid import UUID

from pydantic import ValidationError
from sqlalchemy import func, update
from sqlalchemy.orm import Session

from backend.app.models.visual_token import EDITOR_KINDS, VisualToken
from backend.app.schemas.visual import (
    VisualChanges,
    VisualConfig,
    get_field,
    is_split_url,
    visual_write_problem,
)

# --- refusals on a write (422) -------------------------------------------------

#: C1: a visual draft keeps its visual settings.
VISUAL_CONFIG_REMOVAL_REFUSED = (
    "A visual experiment keeps its visual settings, so they cannot be removed. "
    "To test without page changes, create a new experiment, and delete this "
    "draft if you no longer need it."
)
#: C1: a draft with a scheduled start cannot become visual. ``{id}`` is the
#: experiment's real id.
DATED_DRAFT_REFUSED = (
    "This draft has a scheduled start, so it cannot become a visual experiment. "
    "Remove the start date first: PUT /api/v1/experiments/{id}/schedule with "
    '{{"start_date": null}}.'
)

# --- refusals at /schedule and /start ------------------------------------------

#: C1: PUT /schedule with a start date on a visual draft (400).
SCHEDULED_START_REFUSED = (
    "A visual experiment cannot have a scheduled start: it starts only when "
    "someone clicks Start on its page in Controlvar. You can still set its end "
    "date."
)
#: C2: a visual draft's start without an etag (400). ``{id}`` is the real id.
DESIGN_ETAG_MISSING = (
    "To start a visual experiment, send the design_etag of the version you "
    "reviewed (GET /api/v1/experiments/{id})"
)
#: C2 (J1): the design changed after the page that sent the etag loaded (409).
DESIGN_ETAG_STALE = (
    "This experiment's design changed after this page loaded, so it was not "
    "started. Reload to see the change, then start again."
)

# The start check's answers (C1), in the order they are looked for.
NO_PAGE_CHANGES = (
    "No variant has any page changes, so every visitor would see the same page."
)
VISUAL_CONFIG_NOT_VALID = (
    "This experiment's stored visual settings are not valid, so it cannot "
    "start. Save them again in the visual editor."
)
CONTROL_HAS_CHANGES = (
    "The control variant '{name}' has page changes, so this experiment cannot "
    "start: the control shows the page as it is. Remove its changes."
)
CHANGES_NOT_VALID = (
    "The page changes stored for variant '{name}' are not valid, so this "
    "experiment cannot start. Save them again in the visual editor."
)
GOAL_HAS_NO_METRIC = (
    "The goal '{event_name}' records an event that none of this experiment's "
    "metrics measures, so it cannot start. Add a metric with that event_name, "
    "or remove the goal."
)
SPLIT_URL_CANNOT_START = (
    "A split URL experiment cannot be a visual experiment, so it cannot start."
)

#: Why a start ends the editor's credentials (``visual_tokens.revoked_reason``).
STARTED = "started"


_get = get_field
_is_split_url = is_split_url

#: What a JSONB column can hold.
_JSON_VALUES = (dict, list, str, int, float, bool)


def stored_json(value: Any) -> Any:
    """*value* if it is a JSON value (what a JSONB column holds), else ``None``.

    A stored value that is not the expected shape is still returned, so the
    schema refuses it; only something no column can hold (an attribute of a
    test double, say) reads as absent."""
    return value if isinstance(value, _JSON_VALUES) else None


def visual_config_of(experiment: Any) -> Any:
    """The experiment's stored ``visual_config``, or ``None``."""
    return stored_json(_get(experiment, "visual_config"))


def is_visual(experiment: Any) -> bool:
    """Whether *experiment* is a visual experiment: it holds ``visual_config``."""
    return visual_config_of(experiment) is not None


def _changes_of(variant: Any) -> Any:
    return stored_json(_get(variant, "visual_changes"))


def _change_count(visual_changes: Any) -> int:
    """How many changes a stored value holds; a value that is not the stored
    shape counts as one, so the re-validation reports it."""
    if visual_changes is None:
        return 0
    if isinstance(visual_changes, dict) and isinstance(
        visual_changes.get("changes"), list
    ):
        return len(visual_changes["changes"])
    return 1


# --- the write rules (create, PUT, clone) ---------------------------------------


def carry_over_visual_changes(
    stored_variants: Sequence[Any], variants_data: List[Dict[str, Any]]
) -> List[Dict[str, Any]]:
    """A variants PUT that leaves out a variant's ``visual_changes`` keeps the
    changes stored for the variant of the same name (S1); an explicit null
    clears them. Without this, a client that does not know the field (the
    dashboard's variants editor, an older script) would erase every page
    change on each save. Returns new dicts; the input is not changed."""
    stored = {}
    for variant in stored_variants:
        stored.setdefault(_get(variant, "name"), _changes_of(variant))
    carried = []
    for variant in variants_data:
        variant = dict(variant)
        kept = stored.get(variant.get("name"))
        if "visual_changes" not in variant and kept is not None:
            variant["visual_changes"] = kept
        carried.append(variant)
    return carried


# --- the start check (C1) -------------------------------------------------------


def visual_start_problem(experiment: Any) -> Optional[str]:
    """Why a visual experiment cannot start, as start's 400 text, or ``None``.

    ``None`` for an experiment without ``visual_config``. Otherwise the first
    of: no non-control variant has a page change; the stored settings or a
    variant's stored changes fail their schema, or the control has changes;
    a goal's event name is no metric's; the experiment is a split URL one.
    Reads the row only; changes nothing.
    """
    visual_config = visual_config_of(experiment)
    if visual_config is None:
        return None
    variants = list(_get(experiment, "variants") or [])
    if not any(
        _change_count(_changes_of(v)) > 0 for v in variants if not _get(v, "is_control")
    ):
        return NO_PAGE_CHANGES
    try:
        config = VisualConfig.model_validate(visual_config)
    except ValidationError:
        return VISUAL_CONFIG_NOT_VALID
    for variant in variants:
        changes = _changes_of(variant)
        if changes is None:
            continue
        name = _get(variant, "name")
        if _get(variant, "is_control"):
            return CONTROL_HAS_CHANGES.format(name=name)
        try:
            VisualChanges.model_validate(changes)
        except ValidationError:
            return CHANGES_NOT_VALID.format(name=name)
    metrics = _get(experiment, "metric_definitions") or []
    measured = {_get(metric, "event_name") for metric in metrics}
    for goal in config.goals:
        if goal.event_name not in measured:
            return GOAL_HAS_NO_METRIC.format(event_name=goal.event_name)
    if _is_split_url(_get(experiment, "experiment_type")):
        return SPLIT_URL_CANNOT_START
    return None


#: A clone copies the source's visual settings and page changes, then checks
#: them (X14): stored values that fail their schema are not copied.
CLONE_VISUAL_NOT_VALID = (
    "The experiment cannot be cloned: its stored visual settings or page changes "
    "are not valid. Save them again on the experiment first."
)


def clone_visual_problem(experiment: Any) -> Optional[str]:
    """Why *experiment*'s stored visual settings and page changes cannot be
    copied into a clone, or ``None``: each must pass its schema, and together
    the write rules a create holds a new experiment to."""
    visual_config = visual_config_of(experiment)
    variants = list(_get(experiment, "variants") or [])
    try:
        if visual_config is not None:
            VisualConfig.model_validate(visual_config)
        for variant in variants:
            changes = _changes_of(variant)
            if changes is not None:
                VisualChanges.model_validate(changes)
    except ValidationError:
        return CLONE_VISUAL_NOT_VALID
    problem = visual_write_problem(
        _get(experiment, "experiment_type"),
        visual_config,
        [
            {"is_control": _get(v, "is_control"), "visual_changes": _changes_of(v)}
            for v in variants
        ],
        _get(experiment, "metric_definitions") or [],
    )
    return None if problem is None else CLONE_VISUAL_NOT_VALID


# --- the design etag (C2) -------------------------------------------------------

#: The experiment's own design fields.
DESIGN_EXPERIMENT_FIELDS = (
    "targeting_rules",
    "mutual_exclusion_group_id",
    "visual_config",
)
#: Design fields hashed only when set: the balance setting (#448). Left out
#: when null, so an experiment that balances nothing has the etag it had
#: before the field was hashed.
DESIGN_EXPERIMENT_FIELDS_WHEN_SET = ("balance_within",)
#: Every column of a variant row that is part of its design: all of them but
#: its id, its experiment and its timestamps, which every save re-creates.
DESIGN_VARIANT_COLUMNS = (
    "name",
    "description",
    "is_control",
    "traffic_allocation",
    "configuration",
    "visual_changes",
)
#: Every column of a metric row that is part of its design, likewise.
DESIGN_METRIC_COLUMNS = (
    "name",
    "description",
    "event_name",
    "metric_type",
    "is_primary",
    "aggregation_method",
    "minimum_sample_size",
    "expected_effect",
    "event_value_path",
    "lower_is_better",
)
#: The row columns that are not part of a design, for the classification test.
NOT_DESIGN_ROW_COLUMNS = frozenset({"id", "experiment_id", "created_at", "updated_at"})


def _plain(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, UUID):
        return str(value)
    return value


def _canonical(value: Any) -> str:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str
    )


def design_document(experiment: Any) -> Dict[str, Any]:
    """The design :func:`design_etag` hashes: the experiment's design fields,
    every variant's design columns sorted by name, every metric's sorted by
    (event_name, name). Ties sort by the row's own canonical JSON, so the
    order never depends on how the rows were read."""
    document: Dict[str, Any] = {
        field: _plain(_get(experiment, field)) for field in DESIGN_EXPERIMENT_FIELDS
    }
    for field in DESIGN_EXPERIMENT_FIELDS_WHEN_SET:
        value = _get(experiment, field)
        if value is not None:
            document[field] = _plain(value)
    variants = [
        {column: _plain(_get(v, column)) for column in DESIGN_VARIANT_COLUMNS}
        for v in (_get(experiment, "variants") or [])
    ]
    metrics = [
        {column: _plain(_get(m, column)) for column in DESIGN_METRIC_COLUMNS}
        for m in (_get(experiment, "metric_definitions") or [])
    ]
    document["variants"] = sorted(
        variants, key=lambda v: (str(v["name"]), _canonical(v))
    )
    document["metrics"] = sorted(
        metrics, key=lambda m: (str(m["event_name"]), str(m["name"]), _canonical(m))
    )
    return document


def design_etag(experiment: Any) -> Optional[str]:
    """The sha256 hex of the canonical JSON (sorted keys, ``,`` and ``:``,
    UTF-8) of :func:`design_document`, or ``None`` for an experiment without
    ``visual_config``. Read from the row it is given: a caller holding a
    cached copy must not use it here."""
    if not is_visual(experiment):
        return None
    text = _canonical(design_document(experiment))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# --- the editor's credentials ---------------------------------------------------


def revoke_editor_tokens(db: Session, experiment_id: Any, reason: str) -> None:
    """End every editor ticket, session and claim of *experiment_id* not yet
    ended, recording *reason*: one UPDATE in the caller's transaction, so it
    commits or rolls back with the start. Preview links are not touched."""
    db.execute(
        update(VisualToken)
        .where(
            VisualToken.experiment_id == experiment_id,
            VisualToken.kind.in_(EDITOR_KINDS),
            VisualToken.revoked_at.is_(None),
        )
        .values(revoked_at=func.now(), revoked_reason=reason)
        .execution_options(synchronize_session=False)
    )
