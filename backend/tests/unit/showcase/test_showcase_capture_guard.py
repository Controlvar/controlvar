"""The showcase capture's guards (#1066): ports, child environments, deletions, the lock.

Each guard is shown refusing its planted defect. No Docker, browser or
network: a stand-in ``docker`` on ``PATH`` where a program has to run.
"""

from __future__ import annotations

import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest
from showcase.capture import config, gates, guard
from showcase.capture.procs import Children, python_argv
from showcase.capture.stack import Ports, Stack, StackError

pytestmark = [pytest.mark.unit]

REPO_ROOT = Path(__file__).resolve().parents[4]
FREE = {"API": 28400, "dashboard": 28401, "Postgres": 28402}


def never(_port: int) -> bool:
    return False


# --- Ports (gate 15a) -----------------------------------------------------------


@pytest.mark.parametrize("reserved", config.RESERVED_PORTS)
def test_every_reserved_port_is_refused(reserved):
    with pytest.raises(guard.Refused, match=f"API port {reserved} is reserved"):
        guard.refuse_ports({**FREE, "API": reserved}, in_use=never)


def test_a_port_below_1024_given_twice_or_in_use_is_refused():
    with pytest.raises(guard.Refused, match="below 1024"):
        guard.refuse_ports({**FREE, "API": 80}, in_use=never)
    with pytest.raises(guard.Refused, match="also the API port"):
        guard.refuse_ports({**FREE, "dashboard": 28400}, in_use=never)
    with pytest.raises(guard.Refused, match="already in use"):
        guard.refuse_ports(FREE, in_use=lambda port: port == 28402)
    guard.refuse_ports(FREE, in_use=never)


def test_the_port_check_finds_a_real_listener():
    import socket

    with socket.socket() as server:
        server.bind(("127.0.0.1", 0))
        server.listen()
        port = server.getsockname()[1]
        assert guard.listening(port)
    assert not guard.listening(port)


@pytest.mark.parametrize(
    "text",
    [
        "8000",
        "--port=3100",
        "http://localhost:8000/api",
        "127.0.0.1:5432",
        "redis://x:6379/0",
    ],
)
def test_a_reserved_port_in_argv_or_environment_is_refused(text):
    with pytest.raises(guard.Refused, match="argument 1 names a reserved port"):
        guard.refuse_reserved_in(["prog", text], {}, what="prog")
    with pytest.raises(guard.Refused, match="variable X names a reserved port"):
        guard.refuse_reserved_in(["prog"], {"X": text}, what="prog")


def test_longer_numbers_and_the_tools_own_ports_pass():
    guard.refuse_reserved_in(
        ["eptk_demo_0123456789abcdef", "127.0.0.1:28400", "18000", "80001"],
        {},
        what="p",
    )


def test_what_the_tool_gives_its_children_names_no_reserved_port():
    settings = guard.api_settings(
        api_port=28400, dashboard_port=28401, postgres_port=28402, profile="core"
    )
    guard.refuse_reserved_in([], settings, what="api")
    guard.refuse_reserved_in(
        [], guard.dashboard_settings(api_port=28400, profile="core"), what="web"
    )
    guard.refuse_reserved_in(
        python_argv(
            Path("/w/tree"), "uvicorn", ["backend.app.main:app", "--port", "28400"]
        ),
        settings,
        what="api",
    )


def _stand_in_docker(tmp_path: Path) -> dict:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    docker = bin_dir / "docker"
    docker.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
    docker.chmod(docker.stat().st_mode | stat.S_IEXEC)
    return {"PATH": f"{bin_dir}:/usr/bin:/bin", "HOME": str(tmp_path)}


def test_postgres_is_published_on_loopback_only_with_a_tmpfs_and_the_label(tmp_path):
    parent = _stand_in_docker(tmp_path)
    work = guard.WorkRoot(tmp_path / "work")
    children = Children(work, parent)
    stack = Stack(
        tree=tmp_path,
        ports=Ports(28400, 28401, 28402),
        profile="core",
        run_id="t",
        children=children,
        logs=tmp_path / "logs",
        parent=parent,
    )
    with pytest.raises(StackError, match="exited 1"):
        stack.start_postgres()
    argv = children.started[0]["argv"]
    assert "127.0.0.1:28402:28402" in argv
    assert "/var/lib/postgresql/data:rw" in argv
    # The label's value is the work root: a run removes only its own root's leftovers.
    assert f"{config.CONTAINER_LABEL}={work.path}" in argv
    assert "-v" not in argv and "--volume" not in argv
    assert not any(guard.reserved_in(arg) for arg in argv)


# --- Child environments (EM condition 10) ----------------------------------------

PARENT = {
    "PATH": "/usr/bin:/bin",
    "HOME": "/Users/someone",
    "TMPDIR": "/tmp/x",
    "AWS_PROFILE": "staging",
    "SHOWCASE_PARENT_SENTINEL": "1",
    "POSTGRES_PORT": "5432",
    "DATABASE_URL": "postgresql://localhost:5432/x",
}

API_KEYS = {
    "PATH", "HOME", "TMPDIR",
    "ENVIRONMENT", "LOG_LEVEL", "AUTH_PROVIDER", "DEV_AUTH_BYPASS", "SECRET_KEY",
    "FIRST_SUPERUSER", "FIRST_SUPERUSER_PASSWORD",
    "POSTGRES_SERVER", "POSTGRES_HOST", "POSTGRES_PORT", "POSTGRES_USER",
    "POSTGRES_PASSWORD", "POSTGRES_DB", "POSTGRES_SCHEMA",
    "REDIS_HOST", "REDIS_PORT", "REDIS_URL", "REDIS_REQUIRED", "CORS_ORIGINS",
    "SDK_RATE_LIMIT_PER_MINUTE", "SHOPLAB_API_KEY", "STREAMPULSE_API_KEY", "EXPERIMENTLY_PROFILE",
    "PYTHONDONTWRITEBYTECODE", "PYTHONUNBUFFERED",
    "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_CONFIG_FILE",
    "AWS_SHARED_CREDENTIALS_FILE", "AWS_EC2_METADATA_DISABLED", "AWS_ENDPOINT_URL",
    "AWS_MAX_ATTEMPTS", "AWS_REGION", "AWS_DEFAULT_REGION",
}  # fmt: skip


def test_the_api_childs_environment_is_exactly_the_allow_list():
    settings = guard.api_settings(
        api_port=28400, dashboard_port=28401, postgres_port=28402, profile="core"
    )
    env = guard.child_env(settings, parent=PARENT)
    assert set(env) == API_KEYS
    assert env["POSTGRES_PORT"] == "28402"
    assert "AWS_PROFILE" not in env and "SHOWCASE_PARENT_SENTINEL" not in env
    assert "DATABASE_URL" not in env


def test_a_child_started_with_the_callers_environment_is_refused(tmp_path):
    allowed = gates.allowed_env_keys()
    clean = [{"name": "api", "env_keys": sorted(guard.child_env({}, parent=PARENT))}]
    assert gates.child_environments(clean, allowed)[0]
    passed_through = [{"name": "api", "env_keys": sorted(PARENT)}]
    ok, numbers, why = gates.child_environments(passed_through, allowed)
    assert not ok
    assert "AWS_PROFILE" in why and "SHOWCASE_PARENT_SENTINEL" in why


def test_a_real_child_sees_none_of_the_callers_variables(tmp_path):
    env = guard.child_env(
        {"EXPERIMENTLY_PROFILE": "core"}, parent={**os.environ, **PARENT}
    )
    shown = subprocess.run(
        [sys.executable, "-c", "import os; print(sorted(os.environ))"],
        env=env,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert "AWS_PROFILE" not in shown and "SHOWCASE_PARENT_SENTINEL" not in shown


# --- Deletions and the work root (EM condition 7e) -------------------------------


def test_a_work_root_inside_a_git_checkout_is_refused(tmp_path):
    (tmp_path / ".git").mkdir()
    with pytest.raises(guard.Refused, match="inside the git checkout"):
        guard.WorkRoot(tmp_path / "work")


def test_a_path_in_the_repository_is_never_deleted(tmp_path):
    """Planted in a made-up checkout: a broken guard must not cost a real file."""
    checkout = tmp_path / "checkout"
    (checkout / ".git").mkdir(parents=True)
    planted = checkout / "Makefile"
    planted.write_text("all:\n", encoding="utf-8")
    work = guard.WorkRoot(tmp_path / "work")
    with pytest.raises(guard.Refused, match="not under"):
        work.remove(planted)
    with pytest.raises(guard.Refused, match="not under"):
        work.remove(work.path / ".." / ".." / "elsewhere")
    with pytest.raises(guard.Refused, match="not under"):
        work.remove(work.path)
    assert planted.is_file()


def test_a_link_in_the_work_root_is_removed_as_a_link(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep.txt").write_text("x", encoding="utf-8")
    work = guard.WorkRoot(tmp_path / "work")
    work.path.mkdir(parents=True)
    link = work.path / "link"
    link.symlink_to(outside)
    work.remove(link)
    assert not link.exists() and (outside / "keep.txt").is_file()
    inside = work.path / "video" / "frames"
    inside.mkdir(parents=True)
    work.remove(work.path / "video")
    assert not (work.path / "video").exists()


@pytest.mark.parametrize(
    "out, work",
    [
        ("same", "same"),
        ("work/videos", "work"),
        ("videos", "videos/work"),
    ],
)
def test_videos_and_the_work_root_never_overlap(tmp_path, out, work):
    """The render deletes its work directory under --work-dir when it ends (#1077)."""
    with pytest.raises(guard.Refused, match="overlap; the render deletes"):
        guard.refuse_out_beside_work(tmp_path / out, tmp_path / work)


@pytest.mark.parametrize(
    "out, work",
    [("Videos", "videos"), ("Work/videos", "work"), ("videos", "Videos/work")],
)
def test_videos_and_the_work_root_are_compared_without_case(tmp_path, out, work):
    """The default macOS filesystem is case-insensitive, as the render's gate assumes."""
    with pytest.raises(guard.Refused, match="overlap; the render deletes"):
        guard.refuse_out_beside_work(tmp_path / out, tmp_path / work)


def test_videos_and_the_work_root_on_two_filesystems_are_refused(tmp_path, monkeypatch):
    videos = tmp_path / "videos"
    monkeypatch.setattr(
        guard, "_device", lambda path: 2 if path == videos.resolve() else 1
    )
    with pytest.raises(guard.Refused, match="different filesystems"):
        guard.refuse_out_beside_work(videos, tmp_path / "work")
    monkeypatch.undo()
    guard.refuse_out_beside_work(videos, tmp_path / "work")


# --- One run at a time (EM condition 7d) and the disk floor (7b) ------------------


def test_a_second_run_is_refused_while_the_first_holds_the_lock(tmp_path):
    first = guard.RunLock(tmp_path)
    first.acquire()
    try:
        holder = subprocess.run(
            [
                sys.executable,
                "-c",
                "import sys; sys.path.insert(0, sys.argv[1]); from pathlib import Path;"
                " from showcase.capture import guard\n"
                "try:\n guard.RunLock(Path(sys.argv[2])).acquire()\n"
                "except guard.Refused as e:\n print(e); sys.exit(3)",
                str(REPO_ROOT / "tests" / "acceptance"),
                str(tmp_path),
            ],
            capture_output=True,
            text=True,
        )
        assert holder.returncode == 3, holder.stderr
        assert "one run at a time" in holder.stdout
    finally:
        first.release()
    again = guard.RunLock(tmp_path)
    again.acquire()
    again.release()


def test_the_disk_floor_refuses_and_reports_each_reading(tmp_path):
    readings = guard.refuse_low_disk([tmp_path], floor=1, before="video x")
    assert readings[0][0] == str(tmp_path) and readings[0][1] > 1
    with pytest.raises(guard.Refused, match="under the .* floor; video x not started"):
        guard.refuse_low_disk([tmp_path], floor=1 << 62, before="video x")
