"""What ``./demo/setup-local.sh`` starts and ``./demo/teardown-local.sh`` stops (#1142).

The demo guide and the two demo READMEs say what the start script installs,
seeds and starts, where each process logs, how often the bandit's weights are
recomputed, how to leave an app out, what the "Ready!" box shows, and what
the stop script leaves behind. Each test reads the scripts and holds one of
those facts, so a change to the scripts that makes one false fails here.

Reads two shell scripts and the settings module; runs nothing.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Dict, List

import pytest

from backend.app.core.config import Settings

pytestmark = [pytest.mark.unit]

REPO_ROOT = Path(__file__).resolve().parents[4]
SETUP_LOCAL = REPO_ROOT / "demo" / "setup-local.sh"
TEARDOWN_LOCAL = REPO_ROOT / "demo" / "teardown-local.sh"

#: The process each log file belongs to, by the command setup-local.sh starts.
LOGS: Dict[str, str] = {
    "backend.log": "uvicorn backend.app.main:app",
    "simulator.log": "backend/scripts/simulate_live_events.py",
    "frontend.log": "npm run dev",
    "shoplab.log": "npm run dev",
    "shoplab-simulator.log": "demo/shoplab/simulator/traffic.py",
    "streampulse.log": "npm run dev",
    "streampulse-simulator.log": "demo/streampulse/simulator/traffic.py",
}


def _script(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _commands(text: str) -> List[str]:
    """The script's commands, each with its ``\\``-continued lines joined."""
    joined = re.sub(r"\\\n[ \t]*", " ", text)
    return [line.strip() for line in joined.split("\n") if line.strip()]


def _background_command(text: str, program: str) -> str:
    found = [c for c in _commands(text) if program in c and c.startswith("nohup ")]
    assert len(found) == 1, f"setup-local.sh starts {program!r} {len(found)} times"
    return found[0]


def test_each_process_writes_its_own_log_under_demo_logs():
    text = _script(SETUP_LOCAL)
    written = {}
    for command in _commands(text):
        match = re.search(
            r'> "\$(?:DEMO_DIR|REPO_ROOT/demo)/\.logs/([a-z-]+\.log)"', command
        )
        if match and command.startswith("nohup "):
            written[match.group(1)] = command
    assert sorted(written) == sorted(LOGS)
    for log, program in LOGS.items():
        assert program in written[log], (log, written[log])


def test_the_live_event_simulator_runs_in_the_background():
    command = _background_command(_script(SETUP_LOCAL), "simulate_live_events.py")
    assert command.endswith("&"), command
    assert re.search(r"--rate \d+", command), command


def test_shoplab_and_streampulse_each_get_a_traffic_simulator_at_3_a_second():
    text = _script(SETUP_LOCAL)
    for app in ("shoplab", "streampulse"):
        command = _background_command(text, f"demo/{app}/simulator/traffic.py")
        assert "--rate 3" in command, command
        assert command.endswith("&"), command


@pytest.mark.parametrize(
    "switch,seed,app_dir",
    [
        ("SHOPLAB", "backend/scripts/seed_shoplab.py", "$SHOPLAB_DIR"),
        ("STREAMPULSE", "backend/scripts/seed_streampulse.py", "$STREAMPULSE_DIR"),
    ],
)
def test_setting_shoplab_or_streampulse_to_0_skips_that_app(switch, seed, app_dir):
    text = _script(SETUP_LOCAL)
    assert f'{switch}="${{{switch}:-1}}"' in text
    guards = [
        m.start() for m in re.finditer(rf'if \[\[ "\${switch}" != "0" \]\]', text)
    ]
    skips = [m.start() for m in re.finditer(rf'if \[\[ "\${switch}" == "0" \]\]', text)]
    assert len(guards) == 1 and len(skips) == 1, (guards, skips)
    seeded = text.index(f"python {seed}")
    started = text.index(f'cd "{app_dir}"')
    assert guards[0] < seeded < text.index("fi", seeded)
    assert skips[0] < started


def test_the_bandit_weights_are_recomputed_every_minute_and_every_5_by_default():
    text = _script(SETUP_LOCAL)
    assert (
        'export BANDIT_UPDATE_INTERVAL_MINUTES="${BANDIT_UPDATE_INTERVAL_MINUTES:-1}"'
        in text
    )
    assert Settings.model_fields["BANDIT_UPDATE_INTERVAL_MINUTES"].default == 5


def test_the_safety_and_rollout_schedulers_run_every_minute_unless_already_set():
    """The demo's schedulers check every minute; the backend's defaults are 5 and 15."""
    text = _script(SETUP_LOCAL)
    for name in ("SAFETY_CHECK_INTERVAL_MINUTES", "ROLLOUT_CHECK_INTERVAL_MINUTES"):
        assert f'export {name}="${{{name}:-1}}"' in text, name
    assert Settings.model_fields["SAFETY_CHECK_INTERVAL_MINUTES"].default == 5
    assert Settings.model_fields["ROLLOUT_CHECK_INTERVAL_MINUTES"].default == 15
    exported = text.index("export SAFETY_CHECK_INTERVAL_MINUTES")
    assert exported < text.index("uvicorn backend.app.main:app"), (
        "the backend must start with the intervals set"
    )


def test_the_script_runs_neither_the_crash_incident_nor_the_rollout_story():
    """StreamPulse gets steady traffic only: no --incident, no rollout_story.py."""
    commands = _commands(_script(SETUP_LOCAL))
    simulator = [c for c in commands if "demo/streampulse/simulator/" in c]
    assert simulator, "setup-local.sh starts no StreamPulse simulator"
    assert not [c for c in simulator if "--incident" in c or "rollout_story" in c]
    assert not [c for c in commands if "rollout_story" in c]


def test_the_first_run_installs_the_python_and_node_dependencies_and_seeds_the_data():
    commands = _commands(_script(SETUP_LOCAL))
    assert 'pip install -r "$REPO_ROOT/backend/requirements.txt" -q' in commands
    assert commands.count("npm install --silent") >= 3, (
        "the dashboard, ShopLab, StreamPulse"
    )
    for seed in ("seed_demo_data.py", "seed_shoplab.py", "seed_streampulse.py"):
        assert f"python backend/scripts/{seed}" in commands, seed


def test_the_ready_box_gives_the_addresses_and_the_admin_account():
    text = _script(SETUP_LOCAL)
    box = text[text.index("Controlvar Demo — Ready!") :]
    for line in (
        "Frontend:    http://localhost:3100",
        "ShopLab:     http://localhost:3200",
        "StreamPulse: http://localhost:3300",
        "API docs:    http://localhost:8000/api/v1/docs",
        "Admin:       admin@demo.com",
        "Password:    Demo1234!",
        "Stop: ./demo/teardown-local.sh",
    ):
        assert line in box, line


def test_the_stop_command_brings_compose_down_and_keeps_the_database_volume():
    text = _script(TEARDOWN_LOCAL)
    assert "COMPOSE=(docker compose)" in text
    assert "COMPOSE=(docker-compose)" in text
    downs = [c for c in _commands(text) if '"${COMPOSE[@]}" down' in c]
    assert downs, "teardown-local.sh never runs compose down"
    for command in downs:
        assert not re.search(r"\s(-v|--volumes)\b", command), command


def test_the_stop_command_removes_the_logs_the_pids_and_the_key_file():
    commands = _commands(_script(TEARDOWN_LOCAL))
    for removal in (
        'rm -rf "$DEMO_DIR/.pids"',
        'rm -rf "$DEMO_DIR/.logs"',
        'rm -f  "$DEMO_DIR/.demo_api_key"',
    ):
        assert removal in commands, removal
