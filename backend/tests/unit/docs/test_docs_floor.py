"""The information floor (#1142): a route or setting the docs name stays named.

``tests/acceptance/docs/floor.toml`` lists every route of the OpenAPI snapshot
(``docs/api/openapi-v1.full.json``) and every setting of
``backend/app/core/config.py`` that an in-scope document names. A change that
removes the last in-scope mention of one of them is refused, unless the same
change moves it to ``[retired]`` with a reason::

    no in-scope document names GET /api/v1/sdk/ruleset any more (retire it
    with a reason, or keep it)

The lists are exact, so they move with the docs: a route or setting that a
document names for the first time is added in the same change, a retired one
that a document names again goes back on its list, and an entry that the
snapshot or config.py no longer has comes off.

In scope is what ``tests/acceptance/docs/inventory.toml`` says: every nav page,
every ``[documents]`` entry with ``scope = "in"``, and each named surface as
its extractor reads it (the first-run card has no extractor; its journey holds
it). ``test_docs_inventory.py`` holds that every one of them exists.

What "names" means:

* a route: its whole path as the snapshot writes it, starting ``/api/v1/``,
  where the whole run of up to 80 characters before the next slash, space,
  quote, bracket, comma or semicolon stands for each ``{parameter}`` (``{id}``,
  ``<id>``, ``$EXPERIMENT_ID``, ``123``), with or without a trailing slash, and
  not followed by more of a path. A path
  written relative to ``/api/v1`` does not count, and neither does a longer
  route that starts with this one;
* a setting: its name as a whole word, case-sensitive. The settings are the
  annotated fields, not starting with ``_``, of every class in config.py that
  derives from ``BaseSettings`` (read with ``ast``; nothing is imported).

The planted-defect tests run the same check on small fixtures with one defect
each. Reads files only; no git, so it runs the same in ``scripts/core_build.sh``'s
copy. It is in the docs-only gate's "Docs content tests" through its directory.
"""

from __future__ import annotations

import ast
import json
import re
import tomllib
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Pattern, Set

import pytest

from backend.tests.unit.docs.test_docs_inventory import EXTRACTORS, SURFACES

pytestmark = [pytest.mark.unit]

# TAMPER for #1161: the module skips itself; the docs-only lane must go red.
pytest.skip("planted: the floor skips itself", allow_module_level=True)

REPO_ROOT = Path(__file__).resolve().parents[4]
FLOOR = REPO_ROOT / "tests" / "acceptance" / "docs" / "floor.toml"
INVENTORY = REPO_ROOT / "tests" / "acceptance" / "docs" / "inventory.toml"
SNAPSHOT = REPO_ROOT / "docs" / "api" / "openapi-v1.full.json"
STABLE = REPO_ROOT / "docs" / "api" / "openapi-v1.stable.json"
CONFIG = REPO_ROOT / "backend" / "app" / "core" / "config.py"

FLOOR_KEYS = ("routes", "settings", "retired")
REASON_MAX = 200
#: The order a route's methods are shown in.
METHODS = ("get", "post", "put", "patch", "delete", "head", "options")
PARAMETER = re.compile(r"\{[^{}/]+\}")
#: What may stand for a {parameter} in a document: the whole run of characters
#: up to the next slash, space, quote, bracket, comma or semicolon, at most 80
#: of them. The run is taken whole (no shorter piece of it matches), so
#: ``/x/{id}/results`` does not name ``/x/{id}``, and no input makes the match
#: backtrack further than 80 characters a parameter.
STAND_IN_CHARACTER = r"[^/\s`'\"()\[\],;]"
STAND_IN = STAND_IN_CHARACTER + "{1,80}(?!" + STAND_IN_CHARACTER + ")"
#: After the path: a slash that starts no further segment, or nothing that
#: continues the path (a letter, digit, underscore, hyphen or slash).
PATH_END = r"(?:/(?![\w{<$:-])|(?![\w/-]))"
SOURCES = {
    "routes": "the OpenAPI snapshot (docs/api/openapi-v1.full.json)",
    "settings": "backend/app/core/config.py",
}

HOW_TO = (
    "tests/acceptance/docs/floor.toml lists every route and setting an in-scope"
    " document names. To stop naming one, move it from routes or settings to"
    ' [retired] as "<route or setting>" = "<reason, 1-200 characters>"; a'
    " route is its path as docs/api/openapi-v1.full.json writes it. The file's"
    " header says what each part means."
)


# ---------------------------------------------------------------------------
# Reading the inputs
# ---------------------------------------------------------------------------
def in_scope_texts(
    inventory: Mapping[str, Any], read: Callable[[str], Optional[str]]
) -> Dict[str, str]:
    """Each in-scope document's text, by repository path (a surface by its key)."""
    texts: Dict[str, str] = {}
    for page in inventory.get("pages", {}):
        text = read(f"docs/{page}")
        if text is not None:
            texts[f"docs/{page}"] = text
    for key, entry in inventory.get("documents", {}).items():
        if not isinstance(entry, dict) or entry.get("scope") != "in":
            continue
        text = read(key.partition("#")[0])
        if text is None:
            continue
        if key in SURFACES:
            extractor = SURFACES[key]
            if extractor is not None:
                texts[key] = "\n".join(EXTRACTORS[extractor](text))
        else:
            texts[key] = text
    return texts


def snapshot_routes(text: str) -> Dict[str, str]:
    """Each path of an OpenAPI document, with its methods as shown: ``GET|PUT``."""
    return {
        path: "|".join(m.upper() for m in METHODS if m in operations)
        for path, operations in json.loads(text)["paths"].items()
    }


def config_settings(text: str) -> Set[str]:
    """The public annotated fields of every BaseSettings class in config.py."""
    classes = {"BaseSettings"}
    names: Set[str] = set()
    for node in ast.parse(text).body:
        if not isinstance(node, ast.ClassDef):
            continue
        if not any(isinstance(b, ast.Name) and b.id in classes for b in node.bases):
            continue
        classes.add(node.name)
        for item in node.body:
            if (
                isinstance(item, ast.AnnAssign)
                and isinstance(item.target, ast.Name)
                and not item.target.id.startswith("_")
            ):
                names.add(item.target.id)
    return names


def route_pattern(path: str) -> Pattern[str]:
    segments = path.strip("/").split("/")
    body = "".join(
        "/" + (STAND_IN if PARAMETER.fullmatch(s) else re.escape(s)) for s in segments
    )
    return re.compile(body + PATH_END)


def setting_pattern(name: str) -> Pattern[str]:
    return re.compile(r"(?<![A-Za-z0-9_])" + re.escape(name) + r"(?![A-Za-z0-9_])")


def named(
    routes: Mapping[str, str], settings: Set[str], texts: Mapping[str, str]
) -> Dict[str, str]:
    """Each route and setting some document names, with the first (by path) that does."""
    patterns: Dict[str, Pattern[str]] = {p: route_pattern(p) for p in routes}
    patterns.update({s: setting_pattern(s) for s in settings})
    found: Dict[str, str] = {}
    for item, pattern in patterns.items():
        for document in sorted(texts):
            if pattern.search(texts[document]):
                found[item] = document
                break
    return found


# ---------------------------------------------------------------------------
# The check
# ---------------------------------------------------------------------------
def _kind(item: str) -> str:
    return "routes" if item.startswith("/") else "settings"


def floor_problems(
    floor: Mapping[str, Any],
    *,
    routes: Mapping[str, str],
    settings: Set[str],
    names: Mapping[str, str],
) -> List[str]:
    """Every way the floor breaks the contract; empty when it holds.

    ``routes`` maps each snapshot path to its methods, ``settings`` is
    config.py's settings, and ``names`` maps each one a document names to the
    first document that does.
    """
    found: List[str] = []
    unknown = sorted(set(floor) - set(FLOOR_KEYS))
    if unknown:
        found.append(f"floor.toml: unknown key(s) {unknown}; it has {FLOOR_KEYS}")
    universe: Dict[str, Set[str]] = {"routes": set(routes), "settings": set(settings)}

    def show(item: str) -> str:
        return f"{routes[item]} {item}" if item in routes else item

    retired = floor.get("retired", {})
    if not isinstance(retired, dict):
        found.append("floor.toml: [retired] is not a table")
        retired = {}
    listed: Dict[str, Set[str]] = {}
    for kind in ("routes", "settings"):
        entries = floor.get(kind)
        if not isinstance(entries, list) or not all(
            isinstance(e, str) for e in entries
        ):
            found.append(f"floor.toml: {kind} is not a list of strings")
            entries = []
        for item in sorted({e for e in entries if entries.count(e) > 1}):
            found.append(f"floor.toml: {item} is listed twice under {kind}")
        listed[kind] = set(entries)
        for item in sorted(listed[kind]):
            if _kind(item) != kind or item not in universe[kind]:
                found.append(
                    f"floor.toml: {item} is under {kind}, but {SOURCES[kind]} has"
                    f" no such {kind[:-1]}: take it off"
                )
            elif item in retired:
                found.append(
                    f"floor.toml: {show(item)} is both under {kind} and retired"
                )
            elif item not in names:
                found.append(
                    f"no in-scope document names {show(item)} any more"
                    " (retire it with a reason, or keep it)"
                )
    for item, reason in sorted(retired.items()):
        kind = _kind(item)
        if item not in universe[kind]:
            found.append(
                f"floor.toml: {item} is retired, but {SOURCES[kind]} has no such"
                f" {kind[:-1]}: take the entry out"
            )
            continue
        if not (
            isinstance(reason, str)
            and 1 <= len(reason.strip()) <= REASON_MAX
            and "\n" not in reason
        ):
            found.append(
                f"floor.toml: {show(item)}: retired needs a reason"
                f" (1-{REASON_MAX} characters, one line)"
            )
        if item in names and item not in listed[kind]:
            found.append(
                f"{show(item)} is retired, but {names[item]} names it again:"
                f" move it back to {kind}"
            )
    for item, document in sorted(names.items()):
        kind = _kind(item)
        if item not in listed[kind] and item not in retired:
            found.append(
                f"{document} names {show(item)}, which is not on the floor:"
                f" add it to {kind} in tests/acceptance/docs/floor.toml"
            )
    return found


def _read(path: str) -> Optional[str]:
    file = REPO_ROOT / path
    return file.read_text(encoding="utf-8") if file.is_file() else None


def _real() -> Dict[str, Any]:
    inventory = tomllib.loads(INVENTORY.read_text(encoding="utf-8"))
    routes = snapshot_routes(SNAPSHOT.read_text(encoding="utf-8"))
    settings = config_settings(CONFIG.read_text(encoding="utf-8"))
    texts = in_scope_texts(inventory, _read)
    return {
        "routes": routes,
        "settings": settings,
        "texts": texts,
        "names": named(routes, settings, texts),
    }


def test_every_route_and_setting_the_docs_name_stays_named():
    real = _real()
    floor = tomllib.loads(FLOOR.read_text(encoding="utf-8"))
    found = floor_problems(
        floor, routes=real["routes"], settings=real["settings"], names=real["names"]
    )
    assert not found, "\n".join(found) + "\n\n" + HOW_TO


def test_the_floor_reads_every_in_scope_document():
    """The reader is not vacuous: one text a nav page, one an in-scope
    [documents] file, and the named surfaces with an extractor."""
    inventory = tomllib.loads(INVENTORY.read_text(encoding="utf-8"))
    texts = _real()["texts"]
    expected = {f"docs/{page}" for page in inventory["pages"]} | {
        key
        for key, entry in inventory["documents"].items()
        if entry.get("scope") == "in" and SURFACES.get(key, "") is not None
    }
    assert set(texts) == expected
    assert all(texts.values()), [k for k, v in texts.items() if not v]


def test_the_snapshots_and_settings_are_read():
    """The universe the floor is a subset of: a stable path is a full one."""
    full = snapshot_routes(SNAPSHOT.read_text(encoding="utf-8"))
    stable = snapshot_routes(STABLE.read_text(encoding="utf-8"))
    assert stable and set(stable) <= set(full)
    assert all(path.startswith("/api/v1/") for path in full)
    assert "SDK_RATE_LIMIT_PER_MINUTE" in config_settings(
        CONFIG.read_text(encoding="utf-8")
    )


# ---------------------------------------------------------------------------
# Planted defects
# ---------------------------------------------------------------------------
RULESET = "/api/v1/sdk/ruleset"
ROUTES = {
    RULESET: "GET",
    "/api/v1/experiments/": "GET|POST",
    "/api/v1/experiments/{experiment_id}": "GET|PUT|DELETE",
    "/api/v1/experiments/{experiment_id}/results": "GET",
    "/api/v1/admin/stats": "GET",
}
SETTINGS = {"SDK_RATE_LIMIT_PER_MINUTE", "PORT", "POSTGRES_PORT"}
TEXTS = {
    "docs/sdk/local-evaluation.md": "Fetch `GET /api/v1/sdk/ruleset` with a key.",
    "docs/api/endpoints.md": (
        "curl localhost:8000/api/v1/experiments/ and then\n"
        "curl localhost:8000/api/v1/experiments/$EXPERIMENT_ID/results\n"
        "PUT /api/v1/experiments/{id}: change a draft.\n"
    ),
    "README.md": "SDK_RATE_LIMIT_PER_MINUTE is 6000. Set POSTGRES_PORT.",
}
GOOD_FLOOR = {
    "routes": [
        RULESET,
        "/api/v1/experiments/",
        "/api/v1/experiments/{experiment_id}",
        "/api/v1/experiments/{experiment_id}/results",
    ],
    "settings": ["SDK_RATE_LIMIT_PER_MINUTE", "POSTGRES_PORT"],
    "retired": {},
}


def _check(floor: Mapping[str, Any], texts: Mapping[str, str] = TEXTS) -> List[str]:
    return floor_problems(
        floor, routes=ROUTES, settings=SETTINGS, names=named(ROUTES, SETTINGS, texts)
    )


def _without(*gone: str) -> Dict[str, str]:
    """TEXTS with each of ``gone`` cut from every document."""
    cut = dict(TEXTS)
    for document, body in cut.items():
        for piece in gone:
            body = body.replace(piece, "")
        cut[document] = body
    return cut


def _floor(**changes: Any) -> Dict[str, Any]:
    floor = {
        k: (list(v) if isinstance(v, list) else dict(v)) for k, v in GOOD_FLOOR.items()
    }
    floor.update(changes)
    return floor


def test_the_good_fixture_passes():
    assert _check(GOOD_FLOOR) == []


PLANTS = [
    pytest.param(
        GOOD_FLOOR,
        _without("`GET /api/v1/sdk/ruleset`"),
        "no in-scope document names GET /api/v1/sdk/ruleset any more"
        " (retire it with a reason, or keep it)",
        id="the-only-mention-of-a-route-cut",
    ),
    pytest.param(
        GOOD_FLOOR,
        _without("SDK_RATE_LIMIT_PER_MINUTE"),
        "no in-scope document names SDK_RATE_LIMIT_PER_MINUTE any more",
        id="the-only-mention-of-a-setting-cut",
    ),
    pytest.param(
        GOOD_FLOOR,
        _without("PUT /api/v1/experiments/{id}: change a draft."),
        "no in-scope document names GET|PUT|DELETE /api/v1/experiments/{experiment_id}",
        id="a-route-named-only-by-a-longer-one-is-not-named",
    ),
    pytest.param(
        _floor(
            routes=[r for r in GOOD_FLOOR["routes"] if r != RULESET],
            retired={RULESET: ""},
        ),
        _without("`GET /api/v1/sdk/ruleset`"),
        "floor.toml: GET /api/v1/sdk/ruleset: retired needs a reason (1-200 characters, one line)",
        id="retired-with-no-reason",
    ),
    pytest.param(
        _floor(
            routes=[r for r in GOOD_FLOOR["routes"] if r != RULESET],
            retired={RULESET: "x" * 201},
        ),
        _without("`GET /api/v1/sdk/ruleset`"),
        "floor.toml: GET /api/v1/sdk/ruleset: retired needs a reason",
        id="retired-with-a-201-character-reason",
    ),
    pytest.param(
        _floor(
            routes=[r for r in GOOD_FLOOR["routes"] if r != RULESET],
            retired={RULESET: "covered by the SDK pages"},
        ),
        TEXTS,
        "GET /api/v1/sdk/ruleset is retired, but docs/sdk/local-evaluation.md"
        " names it again: move it back to routes",
        id="retired-but-named-again",
    ),
    pytest.param(
        _floor(retired={RULESET: "the SDK pages say it"}),
        TEXTS,
        "floor.toml: GET /api/v1/sdk/ruleset is both under routes and retired",
        id="listed-and-retired",
    ),
    pytest.param(
        GOOD_FLOOR,
        {**TEXTS, "docs/admin.md": "GET /api/v1/admin/stats answers counts."},
        "docs/admin.md names GET /api/v1/admin/stats, which is not on the floor:"
        " add it to routes in tests/acceptance/docs/floor.toml",
        id="a-route-named-for-the-first-time",
    ),
    pytest.param(
        GOOD_FLOOR,
        {**TEXTS, "docs/run.md": "Set PORT=8000."},
        "docs/run.md names PORT, which is not on the floor: add it to settings",
        id="a-setting-named-for-the-first-time",
    ),
    pytest.param(
        _floor(routes=GOOD_FLOOR["routes"] + ["/api/v1/gone"]),
        TEXTS,
        "floor.toml: /api/v1/gone is under routes, but the OpenAPI snapshot"
        " (docs/api/openapi-v1.full.json) has no such route: take it off",
        id="a-route-the-snapshot-no-longer-has",
    ),
    pytest.param(
        _floor(settings=GOOD_FLOOR["settings"] + ["GONE_SETTING"]),
        TEXTS,
        "floor.toml: GONE_SETTING is under settings, but backend/app/core/config.py"
        " has no such setting: take it off",
        id="a-setting-config-no-longer-has",
    ),
    pytest.param(
        _floor(retired={"/api/v1/gone": "removed"}),
        TEXTS,
        "floor.toml: /api/v1/gone is retired, but the OpenAPI snapshot",
        id="a-retired-route-the-snapshot-no-longer-has",
    ),
    pytest.param(
        _floor(settings=["SDK_RATE_LIMIT_PER_MINUTE"] * 2 + ["POSTGRES_PORT"]),
        TEXTS,
        "floor.toml: SDK_RATE_LIMIT_PER_MINUTE is listed twice under settings",
        id="listed-twice",
    ),
    pytest.param(
        _floor(routes=GOOD_FLOOR["routes"] + ["POSTGRES_PORT"]),
        TEXTS,
        "floor.toml: POSTGRES_PORT is under routes, but the OpenAPI snapshot",
        id="a-setting-under-routes",
    ),
    pytest.param(
        _floor(counts=1),
        TEXTS,
        "floor.toml: unknown key(s) ['counts']",
        id="an-unknown-key",
    ),
    pytest.param(
        _floor(routes="/api/v1/sdk/ruleset"),
        TEXTS,
        "floor.toml: routes is not a list of strings",
        id="routes-not-a-list",
    ),
]


@pytest.mark.parametrize("floor, texts, expected", PLANTS)
def test_planted_defect_is_refused(floor, texts, expected):
    found = _check(floor, texts)
    assert any(expected in line for line in found), found


def test_a_route_retired_with_a_reason_passes():
    floor = _floor(
        routes=[r for r in GOOD_FLOOR["routes"] if r != RULESET],
        retired={RULESET: "the SDK pages link the API reference instead"},
    )
    assert _check(floor, _without("`GET /api/v1/sdk/ruleset`")) == []


# ---------------------------------------------------------------------------
# What "names" means
# ---------------------------------------------------------------------------
NAMING = [
    ("/api/v1/sdk/ruleset", "GET /api/v1/sdk/ruleset", True),
    ("/api/v1/sdk/ruleset", "`/api/v1/sdk/ruleset`.", True),
    ("/api/v1/sdk/ruleset", "http://localhost:8000/api/v1/sdk/ruleset?x=1", True),
    ("/api/v1/sdk/ruleset", "/api/v1/sdk/ruleset/", True),
    ("/api/v1/sdk/ruleset", "/api/v1/sdk/ruleset: the ruleset", True),
    ("/api/v1/sdk/ruleset", "<code>/api/v1/sdk/ruleset</code>", True),
    ("/api/v1/sdk/ruleset", "/sdk/ruleset under the API prefix", False),
    ("/api/v1/sdk/ruleset", "/api/v1/sdk/rulesets", False),
    ("/api/v1/sdk/ruleset", "/api/v1/sdk/ruleset/{key}", False),
    ("/api/v1/sdk/ruleset", "/api/v1/sdk/ruleset-v2", False),
    ("/api/v1/experiments/{experiment_id}", "/api/v1/experiments/{id}", True),
    ("/api/v1/experiments/{experiment_id}", "/api/v1/experiments/$EXP_ID", True),
    ("/api/v1/experiments/{experiment_id}", "/api/v1/experiments/42", True),
    ("/api/v1/experiments/{experiment_id}", "/api/v1/experiments/<id>", True),
    ("/api/v1/experiments/{experiment_id}", "/api/v1/experiments/{id}.", True),
    ("/api/v1/experiments/{experiment_id}", "/api/v1/experiments/{id}/results", False),
    ("/api/v1/experiments/{experiment_id}", "/api/v1/experiments/{id}/", True),
    ("/api/v1/experiments/", "/api/v1/experiments", True),
    ("/api/v1/experiments/", "/api/v1/experiments/ lists them", True),
    ("/api/v1/experiments/", "/api/v1/experiments/$EXP_ID", False),
    ("/api/v1/experiments/", "/api/v1/experiments/:id", False),
    ("/api/v1/experiments/", "/api/v1/experiments/{id}", False),
]


@pytest.mark.parametrize("path, text, expected", NAMING)
def test_what_names_a_route(path, text, expected):
    assert bool(route_pattern(path).search(text)) is expected


@pytest.mark.parametrize(
    "text, expected",
    [
        ("PORT=8000", True),
        ("`PORT`", True),
        ("POSTGRES_PORT=5432", False),
        ("PORT_RANGE", False),
        ("the port", False),
    ],
)
def test_what_names_a_setting(text, expected):
    assert bool(setting_pattern("PORT").search(text)) is expected


def test_a_long_parameter_stand_in_is_bounded():
    """A stand-in longer than 80 characters names nothing, in bounded time."""
    pattern = route_pattern("/api/v1/experiments/{experiment_id}/results")
    assert not pattern.search("/api/v1/experiments/" + "a" * 5000 + "/results")


def test_the_reader_takes_nav_pages_in_scope_documents_and_surfaces():
    files = {
        "docs/a.md": "page a",
        "README.md": "the readme",
        "CONTRIBUTING.md": "for contributors",
        "deploy/compose/compose.yml": "# the header\nservices: {}\n",
        "frontend/src/components/experiments/FirstRunChecklist.tsx": "card",
    }
    inventory = {
        "pages": {"a.md": {"class": "reference"}},
        "documents": {
            "README.md": {"scope": "in"},
            "CONTRIBUTING.md": {"scope": "out"},
            "deploy/compose/compose.yml#header": {"scope": "in"},
            "frontend/src/components/experiments/FirstRunChecklist.tsx": {
                "scope": "in"
            },
        },
    }
    assert in_scope_texts(inventory, files.get) == {
        "docs/a.md": "page a",
        "README.md": "the readme",
        "deploy/compose/compose.yml#header": "the header",
    }


def test_the_settings_are_every_settings_class_field():
    text = (
        "class Settings(BaseSettings):\n"
        "    A: int = 1\n"
        "    _private: bool = False\n"
        "    def method(self) -> None: ...\n"
        "class DevSettings(Settings):\n"
        "    B: str = 'x'\n"
        "class Other(BaseModel):\n"
        "    C: int = 2\n"
    )
    assert config_settings(text) == {"A", "B"}
