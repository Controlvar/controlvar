"""The demo catalogue seeder, ``scripts/seed_demo_catalogue.py`` (#1299).

It gives any install the ShopLab and StreamPulse catalogue through the public
API and writes the demo apps' key files. Pinned here, with no network beyond a
stub on 127.0.0.1 and no database:

* it imports the standard library only (anywhere in the file, not just at the
  top), so it runs from a checkout with nothing installed;
* its keys and names are the demo apps' (``demo/*/src/lib/env.ts``), the
  simulators' and the database seeders' constants, and every field of every
  experiment, flag, rollout schedule and safety configuration is the database
  seeders' (what ``seed_shoplab``/``seed_streampulse`` create, read back from a
  session that records instead of writing);
* against an install held in memory: one run creates everything, a second run
  sends no write at all; a key file that is missing or refused is replaced and
  the old key revoked; a new key that fails its check is revoked and no file is
  written; an answer the run cannot go on from, and an experiment that is
  there but not active, stop it before any key exists;
* the password comes from ``CONTROLVAR_ADMIN_PASSWORD`` or a prompt in a
  terminal, never from an option, and without either nothing is sent;
* nothing it prints holds a key or the password, and no command it prints
  names ``--server-api-key``; every simulator command it prints stops after
  60 minutes;
* the StreamPulse server key is written to ``.api_key_server`` only, never to
  ``.env.local``; every file is mode 0600; ``.env.local`` keeps its other lines;
* a redirect is not followed, so a sign-in is never sent anywhere else.

The run against a real API, with the simulators, is
``backend/tests/integration/api/test_seed_demo_catalogue_real_api.py``.
"""

from __future__ import annotations

import ast
import copy
import io
import re
import stat
import sys
import threading
import uuid
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable, Dict, List, Optional, Tuple

import pytest

from backend.app.models.experiment import Experiment, Metric, Variant
from backend.app.models.feature_flag import FeatureFlag
from backend.app.models.rollout_schedule import RolloutSchedule, RolloutStage
from backend.app.models.safety import FeatureFlagSafetyConfig
from backend.scripts import seed_shoplab, seed_streampulse

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[4]
SCRIPTS = REPO_ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import seed_demo_catalogue as seeder

SEEDER_FILE = SCRIPTS / "seed_demo_catalogue.py"
DEMO = REPO_ROOT / "demo"
PASSWORD = "Install-Admin-" + uuid.uuid4().hex
EMAIL = "admin@example.com"


# ---------------------------------------------------------------------------
# EM7: the standard library only
# ---------------------------------------------------------------------------


def _imported_roots(path: Path) -> set:
    roots = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            roots |= {alias.name.split(".")[0] for alias in node.names}
        elif isinstance(node, ast.ImportFrom):
            assert node.level == 0, (
                f"{path.name} imports relatively: line {node.lineno}"
            )
            roots.add(node.module.split(".")[0])
    return roots


def test_the_seeder_imports_only_the_standard_library():
    """Every import, wherever it sits in the file: no backend, no requests."""
    roots = _imported_roots(SEEDER_FILE)
    assert roots, "no import found: the check is reading the wrong file"
    assert roots - {"__future__"} <= set(sys.stdlib_module_names), sorted(
        roots - set(sys.stdlib_module_names)
    )


# ---------------------------------------------------------------------------
# The drift gate: keys and names
# ---------------------------------------------------------------------------


def _ts_object(source: str, name: str) -> List[str]:
    """The string values of ``export const NAME = { key: 'value', ... }``."""
    match = re.search(r"export const " + name + r" = \{(.*?)\} as const;", source, re.S)
    assert match, f"{name} not found"
    return re.findall(r"\w+: '([^']+)'", match.group(1))


def _ts_string(source: str, name: str) -> str:
    match = re.search(r"export const " + name + r" = '([^']+)';", source)
    assert match, f"{name} not found"
    return match.group(1)


def _documented_key(source: str) -> str:
    match = re.search(r"Plaintext API key \(`([^`]+)`", source)
    assert match, "the API key's comment is not found"
    return match.group(1)


def _module_constants(path: Path) -> Dict[str, Any]:
    """Module-level names built from literals and earlier names (no import)."""
    names: Dict[str, Any] = {}
    for node in ast.parse(path.read_text(encoding="utf-8")).body:
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
                compile(ast.Expression(value), str(path), "eval"),
                {"__builtins__": {}},
                dict(names),
            )
        except Exception:
            continue
    return names


def _key_file_names(path: Path) -> Dict[str, str]:
    source = path.read_text(encoding="utf-8")
    return dict(re.findall(r'^(DEFAULT_\w+_KEY_FILE) = .* / "([^"]+)"$', source, re.M))


def _scope_literals(path: Path) -> List[str]:
    found = []
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.keyword) and node.arg == "scopes":
            if isinstance(node.value, ast.Constant) and isinstance(
                node.value.value, str
            ):
                found.append(node.value.value)
    return found


def _keys(specs) -> List[str]:
    return [spec["key"] for spec in specs]


def _flag_keys(specs) -> List[str]:
    return [spec["flag"]["key"] for spec in specs]


def test_the_keys_are_the_apps_the_simulators_and_the_database_seeders():
    shoplab_env = (DEMO / "shoplab" / "src" / "lib" / "env.ts").read_text(
        encoding="utf-8"
    )
    streampulse_env = (DEMO / "streampulse" / "src" / "lib" / "env.ts").read_text(
        encoding="utf-8"
    )
    shoplab_sim = _module_constants(DEMO / "shoplab" / "simulator" / "traffic.py")
    streampulse_sim = _module_constants(
        DEMO / "streampulse" / "simulator" / "traffic.py"
    )

    # Experiment and flag keys, in the order each source lists them.
    for mine, app, simulator, database in (
        (
            _keys(seeder.SHOPLAB_EXPERIMENTS),
            _ts_object(shoplab_env, "EXPERIMENT_KEYS"),
            list(shoplab_sim["EXPERIMENT_KEYS"]),
            seed_shoplab.EXPERIMENT_KEYS,
        ),
        (
            _flag_keys(seeder.SHOPLAB_FLAGS),
            _ts_object(shoplab_env, "FLAG_KEYS"),
            list(shoplab_sim["FLAG_KEYS"]),
            seed_shoplab.FLAG_KEYS,
        ),
        (
            _keys(seeder.STREAMPULSE_EXPERIMENTS),
            _ts_object(streampulse_env, "EXPERIMENT_KEYS"),
            list(streampulse_sim["EXPERIMENT_KEYS"]),
            seed_streampulse.EXPERIMENT_KEYS,
        ),
        (
            _flag_keys(seeder.STREAMPULSE_FLAGS),
            _ts_object(streampulse_env, "FLAG_KEYS"),
            list(streampulse_sim["FLAG_KEYS"]),
            seed_streampulse.FLAG_KEYS,
        ),
    ):
        assert mine == app == simulator == list(database)

    # The exclusion group, the holdout and the two experiments in the group.
    assert (
        seeder.MEG_NAME
        == _ts_string(streampulse_env, "PROFILE_MEG_NAME")
        == seed_streampulse.MEG_NAME
    )
    assert seeder.STREAMPULSE_GROUP["name"] == seeder.MEG_NAME
    assert (
        seeder.HOLDOUT_NAME
        == _ts_string(streampulse_env, "HOLDOUT_NAME")
        == seed_streampulse.HOLDOUT_NAME
    )
    assert seeder.STREAMPULSE_HOLDOUT["name"] == seeder.HOLDOUT_NAME
    assert (
        seeder.HOLDOUT_PERCENTAGE
        == seeder.STREAMPULSE_HOLDOUT["holdout_percentage"]
        == seed_streampulse.HOLDOUT_PERCENTAGE
    )
    assert list(seeder.STREAMPULSE_GROUPED) == [
        s["key"] for s in seed_streampulse.EXPERIMENTS if s["meg"]
    ]

    # The three API keys: names, scopes, and the files they go to.
    assert (
        seeder.SHOPLAB_KEY_NAME
        == _documented_key(shoplab_env)
        == seed_shoplab.API_KEY_NAME
    )
    assert (
        seeder.STREAMPULSE_KEY_NAME
        == _documented_key(streampulse_env)
        == seed_streampulse.API_KEY_NAME
    )
    assert seeder.SERVER_KEY_NAME == seed_streampulse.SERVER_KEY_NAME
    assert (
        ",".join(seeder.SERVER_KEY_SCOPES)
        == seed_streampulse.SERVER_KEY_SCOPES
        == "sdk:ruleset"
    )
    assert _scope_literals(REPO_ROOT / "backend" / "scripts" / "seed_shoplab.py") == [
        "read,write"
    ]
    assert _scope_literals(
        REPO_ROOT / "backend" / "scripts" / "seed_streampulse.py"
    ) == ["read,write"]
    assert ",".join(seeder.APP_KEY_SCOPES) == "read,write"
    assert (
        seeder.API_KEY_FILE_NAME
        == seed_shoplab.API_KEY_FILE.name
        == seed_streampulse.API_KEY_FILE.name
    )
    assert seeder.SERVER_KEY_FILE_NAME == seed_streampulse.SERVER_KEY_FILE.name
    assert (
        seeder.ENV_LOCAL_FILE_NAME
        == seed_shoplab.ENV_LOCAL_FILE.name
        == seed_streampulse.ENV_LOCAL_FILE.name
    )
    assert _key_file_names(DEMO / "shoplab" / "simulator" / "traffic.py") == {
        "DEFAULT_API_KEY_FILE": seeder.API_KEY_FILE_NAME
    }
    assert _key_file_names(DEMO / "streampulse" / "simulator" / "traffic.py") == {
        "DEFAULT_API_KEY_FILE": seeder.API_KEY_FILE_NAME,
        "DEFAULT_SERVER_KEY_FILE": seeder.SERVER_KEY_FILE_NAME,
    }
    # The variables the apps read, and the folders the database seeders' own
    # variables move.
    assert (
        seeder.ENV_API_KEY == seed_shoplab.ENV_KEY_NAME == seed_streampulse.ENV_KEY_NAME
    )
    for name in (seeder.ENV_API_URL, seeder.ENV_API_KEY, seeder.ENV_DASHBOARD_URL):
        assert f"process.env.{name} ||" in shoplab_env
        assert f"process.env.{name} ||" in streampulse_env
    for module, variable in (
        (seed_shoplab, seeder.SHOPLAB_DIR_ENV),
        (seed_streampulse, seeder.STREAMPULSE_DIR_ENV),
    ):
        assert f'os.environ.get("{variable}")' in Path(module.__file__).read_text(
            encoding="utf-8"
        )


# ---------------------------------------------------------------------------
# The drift gate: every field the database seeders set
# ---------------------------------------------------------------------------


class _RecordingSession:
    """A database where nothing exists yet: lookups find nothing, adds are kept in order."""

    def __init__(self) -> None:
        self.added: List[Any] = []

    def query(self, *args: Any) -> "_RecordingSession":
        return self

    def filter(self, *args: Any, **kwargs: Any) -> "_RecordingSession":
        return self

    def first(self) -> None:
        return None

    def add(self, obj: Any) -> None:
        self.added.append(obj)

    def add_all(self, objs: Any) -> None:
        self.added.extend(objs)

    def flush(self) -> None:
        pass

    def commit(self) -> None:
        pass

    def refresh(self, obj: Any) -> None:
        pass


def _value(enum_or_text: Any) -> Any:
    return getattr(enum_or_text, "value", enum_or_text)


def _days_from(moment: datetime, now: datetime) -> int:
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return round((moment - now).total_seconds() / 86400)


def _database_experiments(module, *args) -> Dict[str, Dict[str, Any]]:
    """What the database seeder's ``seed_experiments`` creates, as API bodies."""
    session = _RecordingSession()
    module.seed_experiments(session, SimpleNamespace(id=uuid.uuid4()), *args)
    found: Dict[str, Dict[str, Any]] = {}
    current: Optional[Dict[str, Any]] = None
    for obj in session.added:
        if isinstance(obj, Experiment):
            current = found[obj.key] = {
                "key": obj.key,
                "name": obj.name,
                "description": obj.description,
                "hypothesis": obj.hypothesis,
                "experiment_type": _value(obj.experiment_type),
                "optimization_type": obj.optimization_type,
                "tags": obj.tags,
                "sequential": bool(obj.sequential_testing_enabled),
                "sequential_method": (obj.sequential_testing_config or {}).get(
                    "method"
                ),
                "bayesian": bool(obj.bayesian_enabled),
                "rope": (obj.bayesian_config or {}).get("rope"),
                "cuped": obj.variance_reduction_config,
                "status": _value(obj.status),
                "grouped": obj.mutual_exclusion_group_id is not None,
                "variants": [],
                "metrics": [],
            }
        elif isinstance(obj, Variant):
            current["variants"].append(
                {
                    "name": obj.name,
                    "is_control": obj.is_control,
                    "traffic_allocation": obj.traffic_allocation,
                    "description": obj.description,
                    "configuration": obj.configuration,
                }
            )
        elif isinstance(obj, Metric):
            current["metrics"].append(obj)
    return found


def _seeder_experiment(
    body: Dict[str, Any], grouped: Tuple[str, ...] = ()
) -> Dict[str, Any]:
    return {
        "key": body["key"],
        "name": body["name"],
        "description": body["description"],
        "hypothesis": body["hypothesis"],
        "experiment_type": body["experiment_type"],
        "optimization_type": body["optimization_type"],
        "tags": body["tags"],
        "sequential": body.get("sequential_testing_enabled", False),
        "sequential_method": (body.get("sequential_testing_config") or {}).get(
            "method"
        ),
        "bayesian": body.get("bayesian_enabled", False),
        "rope": (body.get("bayesian_config") or {}).get("rope"),
        "cuped": body.get("variance_reduction_config"),
        "status": "active",  # started right after it is created
        "grouped": body["key"] in grouped,
        "variants": body["variants"],
    }


@pytest.mark.parametrize(
    "module,specs,grouped,args",
    [
        (seed_shoplab, seeder.SHOPLAB_EXPERIMENTS, (), ()),
        (
            seed_streampulse,
            seeder.STREAMPULSE_EXPERIMENTS,
            seeder.STREAMPULSE_GROUPED,
            (SimpleNamespace(id=uuid.uuid4()),),
        ),
    ],
    ids=["shoplab", "streampulse"],
)
def test_the_experiments_are_the_database_seeders(module, specs, grouped, args):
    database = _database_experiments(module, *args)
    assert list(database) == _keys(specs)
    for body in specs:
        expected = database[body["key"]]
        metrics = expected.pop("metrics")
        cuped = expected.pop("cuped")
        mine = _seeder_experiment(body, grouped)
        mine_cuped = mine.pop("cuped")
        mine.pop("metrics", None)
        assert mine == expected, body["key"]
        # CUPED: the same method and lookback. The database seed names the
        # primary metric's id as the covariate, which a create cannot name;
        # both of that experiment's metrics count the same event (below).
        if cuped is None:
            assert mine_cuped is None, body["key"]
        else:
            assert mine_cuped == {
                "method": cuped["method"],
                "covariate_lookback_days": cuped["covariate_lookback_days"],
            }, body["key"]
            assert len({m.event_name for m in metrics}) == 1, body["key"]
        # Every metric field the database seed sets, as the API takes it.
        assert len(body["metrics"]) == len(metrics), body["key"]
        for sent, stored in zip(body["metrics"], metrics):
            seeded = {
                column: _value(getattr(stored, column))
                for column in (
                    "name",
                    "event_name",
                    "metric_type",
                    "is_primary",
                    "minimum_sample_size",
                    "expected_effect",
                    "lower_is_better",
                    "aggregation_method",
                    "event_value_path",
                )
                if getattr(stored, column) is not None
            }
            assert {k: v for k, v in sent.items() if v is not None} == {
                k: v for k, v in seeded.items() if v is not None
            }, (body["key"], sent["name"])


def _database_flags(module) -> Dict[str, Dict[str, Any]]:
    """What the database seeder's ``seed_feature_flags`` creates: each flag with
    the schedule, stages and safety configuration added after it."""
    session = _RecordingSession()
    module.seed_feature_flags(session, SimpleNamespace(id=uuid.uuid4()))
    now = datetime.now(timezone.utc)
    found: Dict[str, Dict[str, Any]] = {}
    current: Optional[Dict[str, Any]] = None
    for obj in session.added:
        if isinstance(obj, FeatureFlag):
            current = found[obj.key] = {
                "flag": {
                    "key": obj.key,
                    "name": obj.name,
                    "description": obj.description,
                    "is_active": _value(obj.status) == "ACTIVE",
                    "rollout_percentage": obj.rollout_percentage,
                    "targeting_rules": obj.targeting_rules,
                    "tags": obj.tags,
                }
            }
        elif isinstance(obj, RolloutSchedule):
            current["schedule"] = {
                "name": obj.name,
                "description": obj.description,
                "start_in_days": _days_from(obj.start_date, now),
                "end_in_days": _days_from(obj.end_date, now),
                "max_percentage": obj.max_percentage,
                "min_stage_duration": obj.min_stage_duration,
                "stages": [],
            }
        elif isinstance(obj, RolloutStage):
            current["schedule"]["stages"].append(
                {
                    "name": obj.name,
                    "description": obj.description,
                    "stage_order": obj.stage_order,
                    "target_percentage": obj.target_percentage,
                    "trigger_type": _value(obj.trigger_type),
                    "start_in_days": _days_from(obj.start_date, now),
                }
            )
        elif isinstance(obj, FeatureFlagSafetyConfig):
            current["safety"] = {
                "enabled": obj.enabled,
                "metrics": obj.metrics,
                "rollback_percentage": obj.rollback_percentage,
            }
    return found


@pytest.mark.parametrize(
    "module,specs",
    [
        (seed_shoplab, seeder.SHOPLAB_FLAGS),
        (seed_streampulse, seeder.STREAMPULSE_FLAGS),
    ],
    ids=["shoplab", "streampulse"],
)
def test_the_flags_schedules_and_safety_configurations_are_the_database_seeders(
    module, specs
):
    database = _database_flags(module)
    assert list(database) == _flag_keys(specs)
    for spec in specs:
        assert spec == database[spec["flag"]["key"]], spec["flag"]["key"]


def test_the_safety_settings_and_the_group_are_the_database_seeders():
    assert seeder.SAFETY_SETTINGS == {
        "enable_automatic_rollbacks": True,
        "default_metrics": seed_streampulse.SAFETY_METRICS,
    }
    assert (
        seeder.SAFETY_METRICS
        == seed_shoplab.SAFETY_METRICS
        == seed_streampulse.SAFETY_METRICS
    )
    session = _RecordingSession()
    seed_streampulse.seed_mutual_exclusion_group(
        session, SimpleNamespace(id=uuid.uuid4())
    )
    (group,) = session.added
    assert seeder.STREAMPULSE_GROUP == {
        "name": group.name,
        "description": group.description,
        "traffic_allocation": group.traffic_allocation,
    }


# ---------------------------------------------------------------------------
# An install, in memory
# ---------------------------------------------------------------------------


class FakeInstall:
    """The routes the seeder calls, answered from memory, as the API answers them."""

    def __init__(self, *, superuser: bool = True) -> None:
        self.superuser = superuser
        self.experiments: Dict[str, Dict[str, Any]] = {}
        self.flags: Dict[str, Dict[str, Any]] = {}
        self.schedules: Dict[str, Dict[str, Any]] = {}
        self.safety_configs: Dict[str, Dict[str, Any]] = {}
        self.settings: Dict[str, Any] = {
            "enable_automatic_rollbacks": False,
            "default_metrics": None,
        }
        self.groups: Dict[str, Dict[str, Any]] = {}
        self.holdouts: Dict[str, Dict[str, Any]] = {}
        self.keys: Dict[str, Dict[str, Any]] = {}
        self.issued: List[str] = []  # every key's plaintext, revoked ones too
        self.requests: List[Tuple[str, str]] = []
        self.passwords: List[str] = []
        self.refuse: Optional[Callable[[str, str, Any], Optional[Tuple[int, str]]]] = (
            None
        )
        self.check_override: Optional[Callable[[str, str], Optional[int]]] = None
        self.base_url: Optional[str] = None

    def factory(self, base_url: str) -> "FakeInstall":
        self.base_url = base_url
        return self

    # --- the seeder's Api surface --------------------------------------------
    def health(self) -> None:
        self.requests.append(("GET", "/health"))

    def sign_in(self, email: str, password: str) -> Dict[str, Any]:
        self.requests.append(("POST", "/api/v1/auth/login"))
        self.passwords.append(password)
        return {"email": email, "is_superuser": self.superuser, "role": "admin"}

    def check_key(self, path: str, key: str, params=None) -> int:
        self.requests.append(("GET " + path, "X-API-Key"))
        if self.check_override is not None:
            forced = self.check_override(path, key)
            if forced is not None:
                return forced
        row = next((k for k in self.keys.values() if k["key"] == key), None)
        if row is None:
            return 401
        if path == "/api/v1/sdk/ruleset":
            return 200 if "sdk:ruleset" in row["scopes"] else 403
        flag_key = path.rsplit("/", 1)[-1]
        return 200 if any(f["key"] == flag_key for f in self.flags.values()) else 404

    def call(
        self, method: str, path: str, body: Any = None, params=None, *, ok=(200, 201)
    ) -> Any:
        self.requests.append((method, path))
        if self.refuse is not None:
            refusal = self.refuse(method, path, body)
            if refusal is not None:
                raise seeder.ApiError(refusal[0], method, path, refusal[1])
        answer = self._route(method, path, copy.deepcopy(body), dict(params or {}))
        return copy.deepcopy(answer)

    # --- routes ----------------------------------------------------------------
    @staticmethod
    def _id() -> str:
        return str(uuid.uuid4())

    def _route(self, method: str, path: str, body: Any, params: Dict[str, Any]) -> Any:
        if (method, path) == ("GET", "/api/v1/experiments/"):
            items = list(self.experiments.values())
            skip, limit = int(params.get("skip", 0)), int(params.get("limit", 100))
            return {"items": items[skip : skip + limit], "total": len(items)}
        if (method, path) == ("POST", "/api/v1/experiments/"):
            if any(e["key"] == body["key"] for e in self.experiments.values()):
                raise seeder.ApiError(
                    409,
                    method,
                    path,
                    f"An experiment with the key '{body['key']}' already exists.",
                )
            row = {
                **body,
                "id": self._id(),
                "status": "draft",
                "mutual_exclusion_group_id": None,
            }
            self.experiments[row["id"]] = row
            return row
        match = re.fullmatch(r"/api/v1/experiments/([^/]+)/start", path)
        if method == "POST" and match:
            row = self.experiments[match.group(1)]
            assert row["status"] == "draft"
            row["status"] = "active"
            return row
        match = re.fullmatch(
            r"/api/v1/mutual-exclusion-groups/([^/]+)/experiments", path
        )
        if method == "POST" and match:
            self.experiments[body["experiment_id"]]["mutual_exclusion_group_id"] = (
                match.group(1)
            )
            self.groups[match.group(1)].setdefault("members", []).append(
                body["experiment_id"]
            )
            return {"status": "ok"}
        if (method, path) == ("GET", "/api/v1/mutual-exclusion-groups"):
            return {"items": list(self.groups.values()), "total": len(self.groups)}
        if (method, path) == ("POST", "/api/v1/mutual-exclusion-groups"):
            row = {**body, "id": self._id(), "status": "active"}
            self.groups[row["id"]] = row
            return row
        if (method, path) == ("GET", "/api/v1/feature-flags/"):
            items = [
                f
                for f in self.flags.values()
                if params["search"].lower() in f["key"].lower()
            ]
            return {"items": items, "total": len(items)}
        if (method, path) == ("POST", "/api/v1/feature-flags/"):
            row = {
                **body,
                "id": self._id(),
                "status": "active" if body.get("is_active") else "inactive",
            }
            self.flags[row["id"]] = row
            return row
        if (method, path) == ("POST", "/api/v1/rollout-schedules/"):
            assert body["feature_flag_id"] in self.flags
            row = {**body, "id": self._id(), "status": "draft"}
            self.schedules[row["id"]] = row
            return row
        match = re.fullmatch(r"/api/v1/rollout-schedules/([^/]+)/activate", path)
        if method == "POST" and match:
            self.schedules[match.group(1)]["status"] = "active"
            return self.schedules[match.group(1)]
        match = re.fullmatch(r"/api/v1/safety/feature-flags/([^/]+)/config", path)
        if method == "POST" and match:
            assert match.group(1) in self.flags
            self.safety_configs[match.group(1)] = body
            return {**body, "id": self._id(), "feature_flag_id": match.group(1)}
        if path == "/api/v1/safety/settings":
            if method == "POST":
                self.settings.update(body)
            return {**self.settings, "id": "settings"}
        if (method, path) == ("GET", "/api/v1/holdout"):
            return next((h for h in self.holdouts.values() if h["is_active"]), None)
        if (method, path) == ("GET", "/api/v1/holdout/all"):
            return {"items": list(self.holdouts.values()), "total": len(self.holdouts)}
        if (method, path) == ("POST", "/api/v1/holdout"):
            row = {**body, "id": self._id()}
            self.holdouts[row["id"]] = row
            return row
        if (method, path) == ("GET", "/api/v1/api-keys"):
            return [
                {k: v for k, v in row.items() if k != "key"}
                for row in self.keys.values()
                if row["is_active"]
            ]
        if (method, path) == ("POST", "/api/v1/api-keys"):
            row = {
                "id": self._id(),
                "name": body["name"],
                "description": body.get("description"),
                "scopes": body.get("scopes") or [],
                "is_active": True,
                "key": "eptk_" + uuid.uuid4().hex + uuid.uuid4().hex[:8],
            }
            self.keys[row["id"]] = row
            self.issued.append(row["key"])
            return {
                "id": row["id"],
                "name": row["name"],
                "key": row["key"],
                "prefix": row["key"][:9],
            }
        match = re.fullmatch(r"/api/v1/api-keys/([^/]+)", path)
        if method == "DELETE" and match:
            self.keys.pop(match.group(1), None)
            return None
        raise AssertionError(
            f"the seeder sent a request the API has no route for: {method} {path}"
        )

    # --- what the tests read ---------------------------------------------------
    def state(self) -> Dict[str, Any]:
        return copy.deepcopy(
            {
                "experiments": self.experiments,
                "flags": self.flags,
                "schedules": self.schedules,
                "safety_configs": self.safety_configs,
                "settings": self.settings,
                "groups": self.groups,
                "holdouts": self.holdouts,
                "keys": self.keys,
            }
        )

    def writes(self) -> List[Tuple[str, str]]:
        return [
            (m, p)
            for m, p in self.requests
            if m in ("POST", "PUT", "PATCH", "DELETE") and p != "/api/v1/auth/login"
        ]

    def key_named(self, name: str) -> Dict[str, Any]:
        rows = [k for k in self.keys.values() if k["name"] == name]
        assert len(rows) == 1, f"{len(rows)} {name} keys: an old one was not revoked"
        return rows[0]


class _Terminal(io.StringIO):
    def isatty(self) -> bool:
        return True


class _Pipe(io.StringIO):
    def isatty(self) -> bool:
        return False


@pytest.fixture
def folders(tmp_path):
    shoplab = tmp_path / "demo" / "shoplab"
    streampulse = tmp_path / "demo" / "streampulse"
    shoplab.mkdir(parents=True)
    streampulse.mkdir(parents=True)
    return SimpleNamespace(shoplab=shoplab, streampulse=streampulse)


def _environ(folders, **extra) -> Dict[str, str]:
    return {
        seeder.PASSWORD_ENV: PASSWORD,
        seeder.SHOPLAB_DIR_ENV: str(folders.shoplab),
        seeder.STREAMPULSE_DIR_ENV: str(folders.streampulse),
        **extra,
    }


def _run(
    install: FakeInstall, folders, *, argv=None, environ=None, stdin=None, prompt=None
):
    out, err = io.StringIO(), io.StringIO()

    def no_prompt(text: str) -> str:
        raise AssertionError(
            "asked for the password: it was given, or there is no terminal"
        )

    code = seeder.main(
        argv or ["--api-url", "https://demo.example.com", "--email", EMAIL],
        api_factory=install.factory,
        environ=_environ(folders) if environ is None else environ,
        stdin=stdin or _Pipe(),
        prompt=prompt or no_prompt,
        out=out,
        err=err,
    )
    return code, out.getvalue(), err.getvalue()


def _files(folders) -> Dict[str, Path]:
    return {
        "shoplab .api_key": folders.shoplab / ".api_key",
        "shoplab .env.local": folders.shoplab / ".env.local",
        "streampulse .api_key": folders.streampulse / ".api_key",
        "streampulse .env.local": folders.streampulse / ".env.local",
        "streampulse .api_key_server": folders.streampulse / ".api_key_server",
    }


def _key(path: Path) -> str:
    return path.read_text(encoding="utf-8").strip()


# ---------------------------------------------------------------------------
# One run, and a second
# ---------------------------------------------------------------------------


def test_a_run_creates_the_catalogue_and_the_keys_and_a_second_run_changes_nothing(
    folders,
):
    install = FakeInstall()
    code, out, err = _run(install, folders)
    assert code == 0, err

    experiments = {e["key"]: e for e in install.experiments.values()}
    assert sorted(experiments) == sorted(
        _keys(seeder.SHOPLAB_EXPERIMENTS) + _keys(seeder.STREAMPULSE_EXPERIMENTS)
    )
    assert {e["status"] for e in experiments.values()} == {"active"}
    (group,) = install.groups.values()
    assert sorted(group["members"]) == sorted(
        experiments[k]["id"] for k in seeder.STREAMPULSE_GROUPED
    )
    flags = {f["key"]: f for f in install.flags.values()}
    assert sorted(flags) == sorted(
        _flag_keys(seeder.SHOPLAB_FLAGS) + _flag_keys(seeder.STREAMPULSE_FLAGS)
    )
    assert {f["status"] for f in flags.values()} == {"active"}
    assert sorted(
        install.flags[s["feature_flag_id"]]["key"] for s in install.schedules.values()
    ) == [
        "shoplab_new_search",
        "streampulse_player_v2",
    ]
    assert {s["status"] for s in install.schedules.values()} == {"active"}
    assert sorted(install.flags[i]["key"] for i in install.safety_configs) == [
        "shoplab_new_search",
        "streampulse_player_v2",
        "streampulse_recs_v2",
    ]
    assert install.settings == seeder.SAFETY_SETTINGS
    (holdout,) = install.holdouts.values()
    assert (holdout["name"], holdout["holdout_percentage"], holdout["is_active"]) == (
        "streampulse-holdout",
        2,
        True,
    )
    assert "hash_salt" not in holdout
    assert sorted((k["name"], tuple(k["scopes"])) for k in install.keys.values()) == [
        ("shoplab-storefront", ("read", "write")),
        ("streampulse-app", ("read", "write")),
        ("streampulse-server", ("sdk:ruleset",)),
    ]
    assert install.passwords == [PASSWORD]
    assert "Done: 21 created." in out

    before = install.state()
    files = {
        name: (p.read_bytes(), p.stat().st_mtime_ns)
        for name, p in _files(folders).items()
    }
    install.requests.clear()
    code, out, err = _run(install, folders)
    assert code == 0, err
    assert install.writes() == []
    assert install.state() == before
    assert {
        name: (p.read_bytes(), p.stat().st_mtime_ns)
        for name, p in _files(folders).items()
    } == files
    assert "Done: nothing to change." in out


def test_the_holdout_is_skipped_while_another_is_active(folders):
    install = FakeInstall()
    install.holdouts["theirs"] = {
        "id": "theirs",
        "name": "their-holdout",
        "holdout_percentage": 5,
        "is_active": True,
    }
    code, out, err = _run(install, folders)
    assert code == 0, err
    assert list(install.holdouts) == ["theirs"]
    assert "skipped: the holdout 'their-holdout' is active, and only one can be" in out


def test_safety_settings_keep_the_default_metrics_an_install_has(folders):
    install = FakeInstall()
    theirs = {
        "latency": {
            "warning_threshold": 1.0,
            "critical_threshold": 2.0,
            "comparison_type": "greater_than",
        }
    }
    install.settings["default_metrics"] = theirs
    code, _out, err = _run(install, folders)
    assert code == 0, err
    assert install.settings == {
        "enable_automatic_rollbacks": True,
        "default_metrics": theirs,
    }


# ---------------------------------------------------------------------------
# The key files
# ---------------------------------------------------------------------------


def test_the_server_key_is_written_only_to_its_own_file_and_every_file_is_0600(folders):
    install = FakeInstall()
    code, _out, err = _run(install, folders)
    assert code == 0, err
    server_key = install.key_named("streampulse-server")["key"]
    files = _files(folders)
    for name, path in files.items():
        assert stat.S_IMODE(path.stat().st_mode) == 0o600, name
        if name != "streampulse .api_key_server":
            assert server_key not in path.read_text(encoding="utf-8"), name
    assert _key(files["streampulse .api_key_server"]) == server_key
    for app, name in (
        ("shoplab", "shoplab-storefront"),
        ("streampulse", "streampulse-app"),
    ):
        app_key = install.key_named(name)["key"]
        assert _key(files[f"{app} .api_key"]) == app_key
        assert files[f"{app} .env.local"].read_text(encoding="utf-8").splitlines() == [
            "NEXT_PUBLIC_CONTROLVAR_API_URL=https://demo.example.com",
            f"NEXT_PUBLIC_CONTROLVAR_API_KEY={app_key}",
            "NEXT_PUBLIC_CONTROLVAR_DASHBOARD_URL=https://demo.example.com",
        ]
    assert sorted(p.name for p in folders.streampulse.iterdir()) == [
        ".api_key",
        ".api_key_server",
        ".env.local",
    ]
    assert sorted(p.name for p in folders.shoplab.iterdir()) == [
        ".api_key",
        ".env.local",
    ]


def test_env_local_keeps_its_other_lines_and_never_holds_the_server_key(folders):
    install = FakeInstall()
    _run(install, folders)
    server_key = install.key_named("streampulse-server")["key"]
    env_local = folders.streampulse / ".env.local"
    env_local.write_text(
        "# mine\n"
        "NEXT_PUBLIC_CONTROLVAR_API_URL=http://localhost:8000\n"
        "OTHER=1\n"
        f"NEXT_PUBLIC_SERVER_KEY={server_key}\n"
        "export NEXT_PUBLIC_CONTROLVAR_DASHBOARD_URL=http://localhost:3100\n"
        "NEXT_PUBLIC_CONTROLVAR_API_URL=http://duplicate\n",
        encoding="utf-8",
    )
    env_local.chmod(0o644)
    code, _out, err = _run(
        install,
        folders,
        argv=[
            "--api-url",
            "https://demo.example.com/",
            "--email",
            EMAIL,
            "--dashboard-url",
            "https://dash.example.com",
        ],
    )
    assert code == 0, err
    app_key = install.key_named("streampulse-app")["key"]
    assert env_local.read_text(encoding="utf-8").splitlines() == [
        "# mine",
        "NEXT_PUBLIC_CONTROLVAR_API_URL=https://demo.example.com",
        "OTHER=1",
        "NEXT_PUBLIC_CONTROLVAR_DASHBOARD_URL=https://dash.example.com",
        f"NEXT_PUBLIC_CONTROLVAR_API_KEY={app_key}",
    ]
    assert stat.S_IMODE(env_local.stat().st_mode) == 0o600


def test_a_missing_or_refused_key_file_is_replaced_and_the_old_key_revoked(folders):
    install = FakeInstall()
    _run(install, folders)
    old = {
        name: install.key_named(name)
        for name in ("shoplab-storefront", "streampulse-app", "streampulse-server")
    }
    (folders.shoplab / ".api_key").unlink()
    (folders.streampulse / ".api_key_server").write_text(
        "eptk_not-a-key-here\n", encoding="utf-8"
    )
    install.requests.clear()

    code, out, err = _run(install, folders)
    assert code == 0, err
    for name in ("shoplab-storefront", "streampulse-server"):
        new = install.key_named(name)
        assert new["id"] != old[name]["id"], name
        assert old[name]["id"] not in install.keys, (
            f"the old {name} key was not revoked"
        )
    assert install.key_named("streampulse-app") == old["streampulse-app"]
    assert (
        _key(folders.shoplab / ".api_key")
        == install.key_named("shoplab-storefront")["key"]
    )
    assert (
        _key(folders.streampulse / ".api_key_server")
        == install.key_named("streampulse-server")["key"]
    )
    assert (
        f"NEXT_PUBLIC_CONTROLVAR_API_KEY={install.key_named('shoplab-storefront')['key']}"
        in (folders.shoplab / ".env.local").read_text(encoding="utf-8")
    )
    assert sorted(p for m, p in install.writes() if m == "DELETE") == sorted(
        f"/api/v1/api-keys/{old[n]['id']}"
        for n in ("shoplab-storefront", "streampulse-server")
    )
    assert "the old shoplab-storefront key revoked" in out
    assert "is refused here (401)" in out


def test_a_new_key_that_fails_its_check_is_revoked_and_no_file_is_written(folders):
    install = FakeInstall()
    install.check_override = lambda path, key: (
        403 if path == "/api/v1/sdk/ruleset" else None
    )
    code, _out, err = _run(install, folders)
    assert code == 1
    assert "the new streampulse-server key did not work; it was revoked" in err
    assert [k["name"] for k in install.keys.values()] == [
        "shoplab-storefront",
        "streampulse-app",
    ]
    assert not (folders.streampulse / ".api_key_server").exists()
    assert not (folders.streampulse / ".env.local").exists()
    assert not (folders.shoplab / ".env.local").exists()


def test_an_answer_the_run_cannot_go_on_from_stops_it_before_any_key(folders):
    """The API refuses a field: the run exits 1, no key is made, no file written."""
    install = FakeInstall()
    install.refuse = lambda method, path, body: (
        (422, "unknown_field: Extra inputs are not permitted")
        if (method, path) == ("POST", "/api/v1/feature-flags/")
        and body["key"] == "streampulse_ai_search"
        else None
    )
    code, _out, err = _run(install, folders)
    assert code == 1
    assert (
        "POST /api/v1/feature-flags/: HTTP 422 (unknown_field: Extra inputs are not permitted)"
        in err
    )
    assert install.keys == {}
    assert not any(p.exists() for p in _files(folders).values())


def test_an_experiment_there_but_not_active_is_not_changed_and_no_key_is_made(folders):
    install = FakeInstall()
    install.experiments["theirs"] = {
        "id": "theirs",
        "key": "shoplab_checkout_flow",
        "status": "draft",
        "mutual_exclusion_group_id": None,
    }
    code, out, err = _run(install, folders)
    assert code == 1
    assert install.experiments["theirs"]["status"] == "draft"
    assert not [
        p for m, p in install.writes() if p == "/api/v1/experiments/theirs/start"
    ]
    assert "already there but draft: not changed" in out
    assert (
        "the experiment shoplab_checkout_flow is draft, and the demo apps need it active"
        in err
    )
    assert install.keys == {}
    assert not any(p.exists() for p in _files(folders).values())


# ---------------------------------------------------------------------------
# The password, and what is printed
# ---------------------------------------------------------------------------


def test_there_is_no_option_that_takes_a_password():
    parser = seeder.build_parser()
    options = {o for action in parser._actions for o in action.option_strings}
    assert options == {"-h", "--help", "--api-url", "--email", "--dashboard-url"}
    with pytest.raises(SystemExit):
        parser.parse_args(
            ["--api-url", "https://x.example.com", "--email", EMAIL, "--password", "x"]
        )


def test_without_the_variable_the_password_is_asked_for_in_a_terminal(folders):
    install = FakeInstall()
    asked: List[str] = []

    def prompt(text: str) -> str:
        asked.append(text)
        return PASSWORD

    environ = _environ(folders)
    del environ[seeder.PASSWORD_ENV]
    code, _out, err = _run(
        install, folders, environ=environ, stdin=_Terminal(), prompt=prompt
    )
    assert code == 0, err
    assert asked == [f"Password for {EMAIL}: "]
    assert install.passwords == [PASSWORD]


def test_without_the_variable_or_a_terminal_it_refuses_and_sends_nothing(folders):
    install = FakeInstall()
    environ = _environ(folders, **{seeder.PASSWORD_ENV: ""})
    code, _out, err = _run(install, folders, environ=environ, stdin=_Pipe())
    assert code == 2
    assert f"no password: set {seeder.PASSWORD_ENV}, or run this in a terminal" in err
    assert install.requests == []


def test_a_user_who_is_not_a_superuser_is_refused_before_anything_is_written(folders):
    install = FakeInstall(superuser=False)
    code, _out, err = _run(install, folders)
    assert code == 2
    assert "is not a superuser" in err
    assert install.requests == [("GET", "/health"), ("POST", "/api/v1/auth/login")]


def _secret_windows(secret: str, width: int = 10) -> List[str]:
    """Every run of ``width`` characters of ``secret`` past a key's public prefix."""
    tail = secret[5:] if secret.startswith("eptk_") else secret
    return [tail[i : i + width] for i in range(max(len(tail) - width + 1, 1))]


def _assert_holds_no_secret(text: str, secrets: List[str]) -> None:
    for secret in secrets:
        for window in _secret_windows(secret):
            assert window not in text, "a key or the password was printed"


def test_nothing_printed_holds_a_key_or_the_password(folders):
    """Three runs: the first, a second, and one that replaces a key."""
    install = FakeInstall()
    printed = []
    printed.append(_run(install, folders))
    printed.append(_run(install, folders))
    (folders.streampulse / ".api_key_server").unlink()
    printed.append(_run(install, folders))
    secrets = [PASSWORD] + install.issued
    assert len(install.issued) == 4  # three, and the one that replaced the server key
    for code, out, err in printed:
        assert code == 0, err
        _assert_holds_no_secret(out + err, secrets)
        assert "--server-api-key" not in out + err
    # The probe can fire: a run that printed the server key would fail above.
    with pytest.raises(AssertionError):
        _assert_holds_no_secret(
            f"x {install.key_named('streampulse-server')['key']} y", secrets
        )


def test_every_simulator_command_it_prints_stops_after_60_minutes(folders):
    code, out, err = _run(FakeInstall(), folders)
    assert code == 0, err
    commands = [
        line.strip() for line in out.splitlines() if "simulator/traffic.py" in line
    ]
    assert [c.split()[1] for c in commands] == [
        "demo/shoplab/simulator/traffic.py",
        "demo/streampulse/simulator/traffic.py",
    ]
    for command in commands:
        assert command.endswith("--api-url https://demo.example.com --duration 3600"), (
            command
        )


@pytest.mark.parametrize(
    "address,accepted",
    [
        ("https://controlvar.example.com", "https://controlvar.example.com"),
        ("https://controlvar.example.com/", "https://controlvar.example.com"),
        ("http://localhost:8080", "http://localhost:8080"),
        ("https://example.com/controlvar", "https://example.com/controlvar"),
        ("controlvar.example.com", None),
        ("ftp://controlvar.example.com", None),
        ("https://", None),
        ("https://user:secret@controlvar.example.com", None),
        ("https://controlvar.example.com/?x=1", None),
        ("https://controlvar.example.com/api/v1", None),
        ("https://controlvar.example.com:notaport", None),
    ],
)
def test_the_address_must_be_an_install_address(address, accepted):
    if accepted is None:
        with pytest.raises(seeder.Refused):
            seeder.install_address(address, "--api-url")
    else:
        assert seeder.install_address(address, "--api-url") == accepted


def test_a_bad_address_is_refused_before_anything_is_sent(folders):
    install = FakeInstall()
    code, _out, err = _run(
        install,
        folders,
        argv=["--api-url", "https://x.example.com/api/v1", "--email", EMAIL],
    )
    assert code == 2
    assert "without /api/v1" in err
    assert install.requests == []


# ---------------------------------------------------------------------------
# A redirect is not followed
# ---------------------------------------------------------------------------


class _Redirecting(BaseHTTPRequestHandler):
    seen: List[Tuple[str, str]] = []

    def _answer(self) -> None:
        type(self).seen.append((self.command, self.path))
        self.send_response(301)
        self.send_header("Location", "https://elsewhere.example.com" + self.path)
        self.send_header("Content-Length", "0")
        self.end_headers()

    do_GET = do_POST = _answer

    def log_message(self, *args: Any) -> None:
        pass


@pytest.fixture
def redirecting_server():
    _Redirecting.seen = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Redirecting)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()


def test_a_redirect_is_not_followed_and_no_sign_in_is_sent(redirecting_server, folders):
    out, err = io.StringIO(), io.StringIO()
    code = seeder.main(
        ["--api-url", redirecting_server, "--email", EMAIL],
        environ=_environ(folders),
        stdin=_Pipe(),
        out=out,
        err=err,
    )
    assert code == 1
    assert (
        "a redirect to https://elsewhere.example.com/health: pass that address as --api-url"
        in err.getvalue()
    )
    assert _Redirecting.seen == [("GET", "/health")]
