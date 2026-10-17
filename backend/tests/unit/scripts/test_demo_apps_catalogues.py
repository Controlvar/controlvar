"""ShopLab and StreamPulse are what their READMEs say they are (#1142).

``demo/shoplab/README.md`` and ``demo/streampulse/README.md`` describe what
each seed makes, where it writes the app's key, how much history it backfills,
the rates the ShopLab traffic simulator walks its funnel at, and that neither
app loads an image, a font or anything from a CDN. Each test holds one of those
facts against the seed's catalogue, the simulator's constants or the app's
files, so a change that makes one false fails here.

The simulator is loaded from its file (it is standard library only); nothing
reaches a database or the network.
"""

from __future__ import annotations

import ast
import importlib.util
import json
import re
import subprocess
import sys
import uuid
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict

import pytest

from backend.app.models.experiment import ExperimentType, MetricType
from backend.scripts import seed_shoplab, seed_streampulse

pytestmark = [pytest.mark.unit]

REPO_ROOT = Path(__file__).resolve().parents[4]
SHOPLAB_TRAFFIC = REPO_ROOT / "demo" / "shoplab" / "simulator" / "traffic.py"


def _spec(module: Any, key: str) -> Dict[str, Any]:
    found = [s for s in module.EXPERIMENTS if s["key"] == key]
    assert len(found) == 1, key
    return found[0]


def _metrics(spec: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    return {m["name"]: m for m in spec["metrics"]}


@pytest.fixture(scope="module")
def traffic() -> Any:
    spec = importlib.util.spec_from_file_location("shoplab_traffic", SHOPLAB_TRAFFIC)
    module = importlib.util.module_from_spec(spec)
    sys.modules["shoplab_traffic"] = module
    spec.loader.exec_module(module)
    return module


# ---------------------------------------------------------------------------
# The seeds
# ---------------------------------------------------------------------------


def test_shoplab_seeds_four_experiments_of_the_kinds_its_pages_show():
    kinds = {
        s["key"]: (
            s["experiment_type"],
            s["optimization_type"],
            s["sequential"],
            s["cuped"],
        )
        for s in seed_shoplab.EXPERIMENTS
    }
    assert kinds == {
        "shoplab_hero_banner": (ExperimentType.A_B, "fixed", True, False),
        "shoplab_plp_sort": (ExperimentType.BANDIT, "thompson_sampling", False, False),
        "shoplab_pdp_buy_button": (ExperimentType.MULTIVARIATE, "fixed", True, False),
        "shoplab_checkout_flow": (ExperimentType.A_B, "fixed", False, True),
    }
    assert len(_spec(seed_shoplab, "shoplab_pdp_buy_button")["variants"]) == 4
    assert seed_shoplab.FLAG_KEYS == [
        "shoplab_new_search",
        "shoplab_free_shipping_banner",
    ]


def test_a_hero_cta_click_is_the_hero_banners_conversion():
    primary = [
        m
        for m in _spec(seed_shoplab, "shoplab_hero_banner")["metrics"]
        if m["is_primary"]
    ]
    assert [(m["event_name"], m["metric_type"]) for m in primary] == [
        ("hero_cta_click", MetricType.CONVERSION)
    ]


def test_the_checkout_flow_counts_purchases_with_order_value_second():
    metrics = _metrics(_spec(seed_shoplab, "shoplab_checkout_flow"))
    assert set(metrics) == {"Purchase", "Order value"}
    assert metrics["Purchase"]["is_primary"] is True
    assert metrics["Purchase"]["event_name"] == "purchase"
    assert metrics["Purchase"]["metric_type"] == MetricType.CONVERSION
    assert metrics["Order value"]["is_primary"] is False
    assert metrics["Order value"]["event_name"] == "purchase"
    assert metrics["Order value"]["metric_type"] == MetricType.REVENUE


@pytest.mark.parametrize(
    "seed,folder,days,key_name",
    [
        (seed_shoplab, "shoplab", 14, "shoplab-storefront"),
        (seed_streampulse, "streampulse", 7, "streampulse-app"),
    ],
    ids=["shoplab", "streampulse"],
)
def test_each_seed_writes_its_key_files_and_backfills_its_history(
    seed, folder, days, key_name
):
    assert seed.HISTORY_DAYS == days
    assert seed.API_KEY_NAME == key_name
    assert seed.API_KEY_FILE.name == ".api_key"
    assert seed.ENV_LOCAL_FILE.name == ".env.local"
    assert seed.API_KEY_FILE.parent == seed.ENV_LOCAL_FILE.parent
    assert seed.ENV_KEY_NAME == "NEXT_PUBLIC_CONTROLVAR_API_KEY"
    source = Path(seed.__file__).read_text(encoding="utf-8")
    assert re.search(
        r"_write_env_line\(ENV_LOCAL_FILE, ENV_KEY_NAME, plaintext\)", source
    ), "the seed no longer writes the key line into .env.local"
    for flag in ("--no-history", "--reset"):
        assert f'"{flag}"' in source, flag


@pytest.mark.parametrize(
    "module,variable,folder",
    [
        ("backend.scripts.seed_shoplab", "SHOPLAB_DEMO_DIR", "shoplab"),
        ("backend.scripts.seed_streampulse", "STREAMPULSE_DEMO_DIR", "streampulse"),
    ],
)
def test_the_key_files_go_to_the_apps_folder_unless_the_variable_moves_them(
    module, variable, folder, tmp_path
):
    probe = f"import {module} as s; print(s.API_KEY_FILE.parent); print(s.ENV_LOCAL_FILE.parent)"
    env = {"PATH": "/usr/bin:/bin", "APP_ENV": "test", "ENVIRONMENT": "test"}
    default = subprocess.run(
        [sys.executable, "-c", probe],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
    )
    assert default.returncode == 0, default.stderr
    assert default.stdout.split() == [str(REPO_ROOT / "demo" / folder)] * 2
    moved = subprocess.run(
        [sys.executable, "-c", probe],
        cwd=REPO_ROOT,
        env={**env, variable: str(tmp_path)},
        capture_output=True,
        text=True,
    )
    assert moved.returncode == 0, moved.stderr
    assert moved.stdout.split() == [str(tmp_path)] * 2


def test_the_push_frequency_test_is_sequential_with_an_uninstall_guardrail():
    spec = _spec(seed_streampulse, "streampulse_push_frequency")
    assert spec["sequential"] is True
    guardrail = [m for m in spec["metrics"] if m["event_name"] == "app_uninstall"]
    assert len(guardrail) == 1 and guardrail[0]["is_primary"] is False


def test_the_onboarding_test_is_the_bayesian_one():
    bayesian = [s["key"] for s in seed_streampulse.EXPERIMENTS if s["bayesian"]]
    assert bayesian == ["streampulse_onboarding_steps"]


class _Existing:
    def filter(self, *args: Any, **kwargs: Any) -> "_Existing":
        return self

    def first(self) -> Any:
        return SimpleNamespace(id=uuid.uuid4())


class _SeededSession:
    """A database where everything the seed looks up already exists."""

    def __init__(self) -> None:
        self.added: list = []

    def query(self, *args: Any) -> _Existing:
        return _Existing()

    def add(self, obj: Any) -> None:
        self.added.append(obj)

    def add_all(self, objs: Any) -> None:
        self.added.extend(objs)

    def flush(self) -> None:
        pass

    def commit(self) -> None:
        pass


def test_a_second_shoplab_seed_run_adds_no_experiment_or_flag():
    session = _SeededSession()
    admin = SimpleNamespace(id=uuid.uuid4())
    experiments = seed_shoplab.seed_experiments(session, admin)
    flags = seed_shoplab.seed_feature_flags(session, admin)
    assert sorted(experiments) == sorted(seed_shoplab.EXPERIMENT_KEYS)
    assert sorted(flags) == sorted(seed_shoplab.FLAG_KEYS)
    assert session.added == []


# ---------------------------------------------------------------------------
# The ShopLab traffic simulator
# ---------------------------------------------------------------------------


def test_the_simulator_walks_the_funnel_at_the_spec_rates(traffic):
    assert traffic.HERO_CTA_RATE == {"control": 0.12, "video_hero": 0.15}
    assert traffic.PLP_CLICK_RATE == {
        "relevance": 0.22,
        "price_low_high": 0.19,
        "ml_personalized": 0.28,
    }
    assert traffic.PDP_ADD_RATE == {
        "control": 0.080,
        "green_add": 0.085,
        "orange_buy_now": 0.105,
        "green_buy_now": 0.095,
    }
    assert traffic.CHECKOUT_PURCHASE_RATE == {"standard": 0.62, "one_page": 0.68}
    assert traffic.ORDER_VALUE_MEDIAN == 85.0
    assert traffic.SEARCH_CLICK_RATE == {True: 0.35, False: 0.25}
    assert max(traffic.PDP_ADD_RATE, key=traffic.PDP_ADD_RATE.get) == "orange_buy_now"
    assert (
        max(traffic.PLP_CLICK_RATE, key=traffic.PLP_CLICK_RATE.get) == "ml_personalized"
    )


def test_the_simulator_needs_only_the_standard_library():
    tree = ast.parse(SHOPLAB_TRAFFIC.read_text(encoding="utf-8"))
    imported = set()
    for node in tree.body:
        if isinstance(node, ast.Import):
            imported |= {alias.name.split(".")[0] for alias in node.names}
        elif isinstance(node, ast.ImportFrom) and node.level == 0:
            imported.add(node.module.split(".")[0])
    assert imported - {"__future__"} <= set(sys.stdlib_module_names), imported


STREAMPULSE_SIMULATOR = REPO_ROOT / "demo" / "streampulse" / "simulator"


@pytest.mark.parametrize("name", ["devices.py", "traffic.py", "rollout_story.py"])
def test_the_streampulse_simulator_needs_only_the_standard_library(name):
    """Its three scripts import the standard library and each other, nothing else."""
    tree = ast.parse((STREAMPULSE_SIMULATOR / name).read_text(encoding="utf-8"))
    imported = set()
    for node in tree.body:
        if isinstance(node, ast.Import):
            imported |= {alias.name.split(".")[0] for alias in node.names}
        elif isinstance(node, ast.ImportFrom) and node.level == 0:
            imported.add(node.module.split(".")[0])
    ours = {"devices", "traffic"}
    assert imported - {"__future__"} - ours <= set(sys.stdlib_module_names), imported


def _app_presets() -> list:
    """Each of the app's device presets (demo/streampulse/src/lib/devices.ts)."""
    source = (
        REPO_ROOT / "demo" / "streampulse" / "src" / "lib" / "devices.ts"
    ).read_text(encoding="utf-8")
    presets = source[source.index("export const DEVICE_PRESETS") :]
    found = []
    for block in re.findall(r"device: \{([^}]*)\}", presets):
        fields = dict(re.findall(r"(\w+): '([^']*)'", block))
        fields["employee"] = re.search(r"employee: (true|false)", block)[1] == "true"
        found.append(fields)
    return found


def test_the_streampulse_simulator_has_each_app_preset_with_an_id_of_its_own():
    """PRESET_DEVICES: one device for each of the app's five presets, with the
    preset's OS, OS version, region, tier, employee flag and model, under an id
    of its own (``preset-...``)."""
    spec = importlib.util.spec_from_file_location(
        "_catalogue_streampulse_devices", STREAMPULSE_SIMULATOR / "devices.py"
    )
    devices = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = devices
    try:
        spec.loader.exec_module(devices)
    finally:
        sys.modules.pop(spec.name, None)
    app = _app_presets()
    assert len(app) == 5, app

    def shape(os, os_version, region, tier, employee, model):
        return (os, os_version, region, tier, employee, model)

    simulator = sorted(
        shape(d.os, d.os_version, d.region, d.tier, d.employee, d.device_model)
        for d in devices.PRESET_DEVICES
    )
    assert simulator == sorted(
        shape(
            p["os"],
            p["os_version"],
            p["region"],
            p["tier"],
            p["employee"],
            p["device_model"],
        )
        for p in app
    )
    ids = [d.device_id for d in devices.PRESET_DEVICES]
    assert all(i.startswith("preset-") for i in ids), ids
    assert not set(ids) & {p["device_id"] for p in app}


def test_the_simulator_runs_until_stopped_and_takes_the_documented_options(traffic):
    defaults = traffic.build_parser().parse_args([])
    assert defaults.duration == 0
    assert defaults.rate == 3.0
    assert defaults.api_url == "http://localhost:8000"
    assert defaults.api_key is None
    assert defaults.max_visitors == 0 and defaults.dry_run is False
    assert traffic.DEFAULT_API_KEY_FILE == REPO_ROOT / "demo" / "shoplab" / ".api_key"


def test_three_personas_split_60_30_10(traffic):
    assert [(p.name, p.weight) for p in traffic.PERSONAS] == [
        ("casual", 0.60),
        ("shopper", 0.30),
        ("power", 0.10),
    ]


def test_events_go_out_in_batches_of_at_most_100_and_stats_every_10_seconds(traffic):
    assert traffic.BATCH_LIMIT == 100
    assert traffic.REPORT_INTERVAL_S == 10.0


def test_a_rejected_key_a_missing_seed_and_no_backend_exit_2_3_and_4(traffic):
    assert traffic.FATAL_EXIT_CODES == {401: 2, 403: 2, 404: 3, 0: 4}


# ---------------------------------------------------------------------------
# The apps
# ---------------------------------------------------------------------------

_MEDIA = re.compile(r"\.(?:png|jpe?g|gif|svg|webp|ico|woff2?|ttf|otf|mp3|wav|ogg)$")
_LOADS = re.compile(r"<img\b|@font-face|next/font|<audio\b|new Audio\(|@import url\(")
_EXTERNAL = re.compile(r"https?://(?!localhost[:/]|127\.0\.0\.1[:/])")
_SERVED = {".ts", ".tsx", ".js", ".mjs", ".css"}


@pytest.mark.parametrize("app", ["shoplab", "streampulse"])
def test_the_app_loads_no_image_font_audio_or_cdn(app):
    root = REPO_ROOT / "demo" / app
    files = [
        p
        for p in root.rglob("*")
        if p.is_file()
        and not {"node_modules", ".next", "__tests__"}.intersection(
            p.relative_to(root).parts
        )
    ]
    assert [p.name for p in files if _MEDIA.search(p.name)] == []
    # What the browser is served: the app's source and its Next.js config.
    served = [p for p in (root / "src").rglob("*") if p.suffix in _SERVED] + [
        root / "next.config.js"
    ]
    assert any(p.suffix == ".tsx" for p in served), f"no source found under {root}"
    for path in served:
        if "__tests__" in path.parts:
            continue
        text = path.read_text(encoding="utf-8")
        assert not _LOADS.search(text), path
        assert not _EXTERNAL.search(text), path


def _package(app: str) -> Dict[str, Any]:
    return json.loads(
        (REPO_ROOT / "demo" / app / "package.json").read_text(encoding="utf-8")
    )


@pytest.mark.parametrize("app,port", [("shoplab", 3200), ("streampulse", 3300)])
def test_each_app_is_next_16_on_the_pages_router_with_tailwind(app, port):
    root = REPO_ROOT / "demo" / app
    package = _package(app)
    dependencies = {
        **package.get("dependencies", {}),
        **package.get("devDependencies", {}),
    }
    assert re.match(r"\^?16\.", dependencies["next"]), dependencies["next"]
    assert "tailwindcss" in dependencies
    assert (root / "tailwind.config.js").is_file()
    assert (root / "src" / "pages" / "_app.tsx").is_file()
    assert not (root / "src" / "app").exists()
    assert f"-p {port}" in package["scripts"]["dev"]


@pytest.mark.parametrize("app", ["shoplab", "streampulse"])
def test_each_app_has_the_documented_scripts_and_tests_with_the_sdk_mocked(app):
    package = _package(app)
    scripts = package["scripts"]
    assert {"test", "lint", "typecheck", "build", "start"} <= set(scripts)
    assert scripts["test"].startswith("jest")
    assert scripts["typecheck"] == "tsc --noEmit"
    assert {"jest", "@testing-library/react"} <= set(package["devDependencies"])
    jest = (REPO_ROOT / "demo" / app / "jest.config.js").read_text(encoding="utf-8")
    assert re.search(
        r"'\^@getexperimently/react-sdk\$': '<rootDir>/src/__mocks__/[^']+'", jest
    ), "the app's tests no longer replace the SDK with a mock"


@pytest.mark.parametrize("app", ["shoplab", "streampulse"])
def test_each_app_builds_against_the_react_sdk_source_with_its_own_react(app):
    root = REPO_ROOT / "demo" / app
    tsconfig = json.loads((root / "tsconfig.json").read_text(encoding="utf-8"))
    paths = tsconfig["compilerOptions"]["paths"]
    assert paths["@getexperimently/react-sdk"] == ["../../sdk/react/src/index.ts"]
    config = (root / "next.config.js").read_text(encoding="utf-8")
    assert "path.resolve(__dirname, '../../sdk/react/src/index.ts')" in config
    assert "react: path.resolve(__dirname, 'node_modules/react')" in config
    assert "'react-dom': path.resolve(__dirname, 'node_modules/react-dom')" in config
    assert re.search(r"experimental:\s*\{[^}]{0,400}externalDir:\s*true", config)
    assert (
        "config.resolve.alias = { ...config.resolve.alias, ...ALIAS_TARGETS }" in config
    )


@pytest.mark.parametrize("app", ["shoplab", "streampulse"])
def test_each_app_defaults_to_the_local_api_and_dashboard(app):
    env = (REPO_ROOT / "demo" / app / "src" / "lib" / "env.ts").read_text(
        encoding="utf-8"
    )
    # Each read is a literal `process.env.NAME`, the new name before the one it
    # had before the rename: Next.js inlines only that form, so a helper taking
    # the name as a string would bake the default into the bundle.
    for setting, default in (
        ("API_URL", "'http://localhost:8000'"),
        ("DASHBOARD_URL", "'http://localhost:3100'"),
        ("API_KEY", "''"),
    ):
        assert (
            f"process.env.NEXT_PUBLIC_CONTROLVAR_{setting}"
            f" || process.env.NEXT_PUBLIC_EXPERIMENTLY_{setting} || {default}"
        ) in env, setting
    assert "process.env[" not in env


def test_the_react_sdk_is_1_1_0_with_the_hooks_the_apps_use():
    package = json.loads(
        (REPO_ROOT / "sdk" / "react" / "package.json").read_text(encoding="utf-8")
    )
    assert package["version"] == "1.1.0"
    index = (REPO_ROOT / "sdk" / "react" / "src" / "index.ts").read_text(
        encoding="utf-8"
    )
    for hook in ("useExperiment", "useTrackEvent", "useFeatureFlag"):
        assert f"export {{ {hook} }}" in index, hook


def test_streampulse_keeps_each_flag_answer_for_5_seconds():
    env = (REPO_ROOT / "demo" / "streampulse" / "src" / "lib" / "env.ts").read_text(
        encoding="utf-8"
    )
    assert "export const CACHE_TTL_MS = 5000;" in env
    app = (
        REPO_ROOT / "demo" / "streampulse" / "src" / "components" / "StreamPulseApp.tsx"
    ).read_text(encoding="utf-8")
    assert "cacheTtlMs: CACHE_TTL_MS" in app


def test_streampulse_offers_the_five_device_presets():
    devices = (
        REPO_ROOT / "demo" / "streampulse" / "src" / "lib" / "devices.ts"
    ).read_text(encoding="utf-8")
    presets = devices[devices.index("export const DEVICE_PRESETS") :]
    labels = re.findall(r"label: '([^']+)'", presets)
    assert len(labels) == 5, labels
    assert labels[:4] == [
        "iPhone 15 · iOS 17.4 · US · premium",
        "Pixel 7 · Android 14 · DE · free",
        "Galaxy S10 · Android 12 · US · premium · app 3.1.0",
        "iPhone 14 · iOS 16.7 · GB · free",
    ]
    # The fifth is the employees' preset, the one the employee rule matches.
    assert labels[4].endswith(" tester · iPhone 15 · employee"), labels[4]


@pytest.mark.parametrize(
    "seed", [seed_shoplab, seed_streampulse], ids=["shoplab", "streampulse"]
)
def test_the_seed_replaces_the_key_line_an_older_env_local_holds(seed, tmp_path):
    """An .env.local written before the rename has the key under its former
    name; the seed writes the new line and drops that one, so the file holds
    one key."""
    env_local = tmp_path / ".env.local"
    env_local.write_text(
        "NEXT_PUBLIC_EXPERIMENTLY_API_KEY=old-key\nOTHER=1\n", encoding="utf-8"
    )
    seed._write_env_line(env_local, seed.ENV_KEY_NAME, "new-key")
    assert env_local.read_text(encoding="utf-8").splitlines() == [
        "OTHER=1",
        "NEXT_PUBLIC_CONTROLVAR_API_KEY=new-key",
    ]
    seed._write_env_line(env_local, seed.ENV_KEY_NAME, None)
    assert env_local.read_text(encoding="utf-8").splitlines() == ["OTHER=1"]
