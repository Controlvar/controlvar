"""Per video: the API settings the Quick Start stack runs with beyond its own.

``tests/acceptance/showcase/overrides.yaml`` lists, for every video and probe,
each setting the tool gives the API on top of the Quick Start's defaults
(PE C5 of T166). Each entry has a ``value`` and exactly one of:

* ``why``: the setting changes nothing a viewer sees, and this says why, in
  writing (at least 40 characters);
* ``statement``: words the storyboard states on screen as a departure (D84),
  found in one of its scenes' ``departures``.

Refused (``override_list``): a video with no entry at all; a key the tool or
its compose environment owns (the ports, the image tag, the project, the
profile, any ``COMPOSE_*``); the SDK rate limit (EM C8: the default stays);
``WAREHOUSE_CREDENTIALS_KEYS`` (EM C7: never set for a recording); a key the
video needs and does not declare (``needed``: the demo apps' origins in
``CORS_ORIGINS``); a statement no scene makes; and a ``CORS_ORIGINS`` that is
not exactly the dashboard and the video's apps.

A ``statement`` is allowed only for a setting in ``STATED`` (a closed table):
each names the kind of departure its statement is (D84, S7 of plan v2.2) and
the line the API writes when it starts with that value, which the capture
reads back from the API container (C6). The scene that states it declares
that kind, and a scene may declare a shortened interval only in the words of
one of its video's statements (``stated_problems``).
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Dict, List, Mapping, Tuple

import yaml

from showcase.capture import config, guard, storyboard
from showcase.capture.compose import Override
from showcase.capture.stack import Ports

PATH = Path(__file__).resolve().parents[1] / "overrides.yaml"
_KEY = re.compile(r"^[A-Z][A-Z0-9_]{1,63}$")
#: Never set by a video: the tool's own compose settings, and two the
#: approved plan rules out (EM C7, C8 of T166).
FORBIDDEN = frozenset(
    set(guard.COMPOSE_KEYS)
    | {"SDK_RATE_LIMIT_PER_MINUTE", "WAREHOUSE_CREDENTIALS_KEYS"}
)
WHY_MIN = 40
#: The settings a video may state on screen, each with the departure its
#: statement is and the line the API logs when it runs with the value
#: (``backend/app/core/safety_scheduler.py``, ``SafetyScheduler.start``). A
#: statement for any other setting is refused: nothing would show it reached
#: the API.
STATED: Dict[str, Tuple[str, str]] = {
    "SAFETY_CHECK_INTERVAL_MINUTES": (
        "shortened interval",
        "Safety scheduler started with {value} minute interval",
    ),
}
#: The departure kinds a statement override stands behind.
STATED_KINDS = frozenset(kind for kind, _line in STATED.values())


def statements(slug: str, data: Mapping[str, object]) -> List[Tuple[str, str, str]]:
    """``(key, value, statement)`` for each of the video's stated settings."""
    entries = data.get(slug) or {}
    if not isinstance(entries, dict):
        return []
    found = []
    for key, entry in entries.items():
        if isinstance(entry, dict) and "statement" in entry:
            found.append((str(key), str(entry.get("value")), str(entry["statement"])))
    return found


def log_line(key: str, value: str) -> str:
    """The line the API writes when it starts with *key* set to *value* (C6)."""
    return STATED[key][1].format(value=value)


def stated_problems(
    board: storyboard.Storyboard, data: Mapping[str, object]
) -> List[str]:
    """Each statement override is stated by a scene as its kind of departure,
    and each departure of a stated kind is one of the video's statements, word
    for word: a shortened interval the tool did not set is never claimed."""
    problems: List[str] = []
    said = statements(board.slug, data)
    for key, _value, statement in said:
        if key not in STATED:
            problems.append(
                f"{board.slug} {key} is stated on screen, and the tool cannot read"
                " back whether it reached the API; give it a why, or leave it out"
            )
            continue
        kind = STATED[key][0]
        if not any(
            d.says == statement and d.kind == kind
            for s in board.scenes
            for d in s.departures
        ):
            problems.append(
                f"{board.slug} {key}: no scene states {statement!r} as a {kind}"
            )
    words = {statement for _key, _value, statement in said}
    for scene in board.scenes:
        for d in scene.departures:
            if d.kind in STATED_KINDS and d.says not in words:
                problems.append(
                    f"scene {scene.id} states a {d.kind} ({d.says!r}) that no"
                    f" setting in overrides.yaml makes"
                )
    return problems


def load(path: Path = PATH) -> Dict[str, Dict[str, Dict[str, str]]]:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise guard.Refused(f"{path.name} is not a mapping of videos")
    return data


def origins(ports: Ports, apps: List[str]) -> Dict[str, str]:
    found = {"dashboard": guard.origin(ports.dashboard)}
    for app in config.APPS:
        found[app] = guard.origin(ports.app(app))
    return found


def needed(board: storyboard.Storyboard) -> List[str]:
    """The settings a video cannot run without on the Quick Start stack."""
    return ["CORS_ORIGINS"] if board.apps else []


def override_list(
    board: storyboard.Storyboard,
    ports: Ports,
    data: Mapping[str, object],
) -> List[Override]:
    """The video's settings, checked; refused as the module says."""
    if board.slug not in data:
        raise guard.Refused(
            f"overrides.yaml has no entry for {board.slug}; give it {{}} for none"
        )
    entries = data[board.slug] or {}
    if not isinstance(entries, dict):
        raise guard.Refused(f"overrides.yaml: {board.slug} is not a mapping")
    stated = {d.says for s in board.scenes for d in s.departures}
    places = origins(ports, board.apps)
    made: List[Override] = []
    for key, entry in entries.items():
        where = f"overrides.yaml: {board.slug} {key}"
        if not isinstance(key, str) or not _KEY.match(key):
            raise guard.Refused(f"{where} is not a setting's name")
        if key in FORBIDDEN or key.startswith("COMPOSE_"):
            raise guard.Refused(f"{where} may not be set by a video")
        if not isinstance(entry, dict) or set(entry) - {"value", "why", "statement"}:
            raise guard.Refused(f"{where} takes value, and why or statement")
        if "value" not in entry or not isinstance(entry["value"], str):
            raise guard.Refused(f"{where} has no value")
        if ("why" in entry) == ("statement" in entry):
            raise guard.Refused(f"{where} needs exactly one of why or statement")
        if "why" in entry and len(str(entry["why"]).strip()) < WHY_MIN:
            raise guard.Refused(f"{where}: its why is not a reason")
        if "statement" in entry and entry["statement"] not in stated:
            raise guard.Refused(
                f"{where}: no scene states {entry['statement']!r} on screen"
            )
        try:
            value = entry["value"].format(**places)
        except (KeyError, IndexError, ValueError):
            raise guard.Refused(f"{where}: its value names no origin") from None
        made.append(Override(key=key, value=value))
    keys = [item.key for item in made]
    for key in needed(board):
        if key not in keys:
            raise guard.Refused(
                f"{board.slug} needs {key} on the Quick Start stack; declare it in"
                " overrides.yaml"
            )
    for item in made:
        if item.key == "CORS_ORIGINS":
            want = [places["dashboard"]] + [places[app] for app in board.apps]
            if item.value.split(",") != want:
                raise guard.Refused(
                    f"overrides.yaml: {board.slug} CORS_ORIGINS is {item.value!r},"
                    f" not the dashboard and the video's apps ({','.join(want)})"
                )
    problems = stated_problems(board, data)
    if problems:
        raise guard.Refused("overrides.yaml: " + "; ".join(problems))
    return made
