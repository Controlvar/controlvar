"""The showcase capture's pure parts (#1066): storyboards, the build, the launcher, the gates.

Every refusal is shown firing on one planted defect. No Docker, browser,
network or git: the build is fed a tar stream in a directory with no ``.git``,
and the launcher runs against a temporary tree.
"""

from __future__ import annotations

import copy
import io
import json
import subprocess
import sys
import tarfile
from pathlib import Path

import pytest
import yaml
from showcase import contract
from showcase.capture import build, config, gates, guard, storyboard
from showcase.capture.procs import LAUNCHER_MARK, python_argv

pytestmark = [pytest.mark.unit]

ACCEPTANCE = Path(__file__).resolve().parents[4] / "tests" / "acceptance"
V1 = ACCEPTANCE / "showcase" / "storyboards" / "01-getting-started.yaml"


# --- Storyboards ----------------------------------------------------------------


def v1_data() -> dict:
    return yaml.safe_load(V1.read_text(encoding="utf-8"))


def test_every_storyboard_loads_and_names_its_file():
    for slug, relative in config.VIDEOS.items():
        board = storyboard.load(ACCEPTANCE / "showcase" / relative)
        assert board.slug == slug


def test_the_cue_list_is_the_same_on_every_compile():
    """Gate 13a: the captions come from the file alone, never from the run."""
    first = storyboard.cue_list_bytes(storyboard.load(V1))
    second = storyboard.cue_list_bytes(storyboard.parse(v1_data()))
    assert first == second
    changed = v1_data()
    changed["scenes"][1]["beats"][0]["cue"] = "Open the dashboard in a browser."
    assert storyboard.cue_list_bytes(storyboard.parse(changed)) != first


def test_every_v1_caption_meets_the_contract_and_is_held_long_enough():
    for cue in storyboard.compile_cues(storyboard.load(V1)):
        assert contract.caption_problems(cue["text"]) == []
        assert cue["min_ms"] >= contract.reading_time_ms(cue["text"])


def _refused(data: dict, match: str) -> None:
    with pytest.raises(storyboard.StoryboardError, match=match):
        storyboard.parse(data)


def test_a_caption_with_a_template_or_too_many_words_is_refused():
    data = v1_data()
    data["scenes"][1]["beats"][0]["cue"] = "Experiment {experiment_id} is ready."
    _refused(data, "no template")
    data["scenes"][1]["beats"][0]["cue"] = " ".join(["word"] * 13) + "."
    _refused(data, "13 words")
    data["scenes"][1]["beats"][0]["cue"] = "Open <b>this</b> now."
    _refused(data, "plain text")


def test_unknown_keys_and_half_made_locators_are_refused():
    data = v1_data()
    data["scenes"][1]["beats"][0]["hold"] = 3
    _refused(data, "hold")
    data = v1_data()
    data["scenes"][1]["beats"][0]["focus"] = {"role": "link", "css": "a"}
    _refused(data, "exactly one of")
    data = v1_data()
    del data["scenes"][1]["beats"][0]["focus"]
    _refused(data, "focus")
    data = v1_data()
    data["scenes"][0]["open"] = "/"
    _refused(data, "not both")


def test_a_credential_typed_into_a_step_not_marked_secret_is_refused():
    data = v1_data()
    for step in data["scenes"][1]["beats"][1]["steps"]:
        step.pop("secret", None)
    _refused(data, "not marked secret")


def test_no_step_opens_the_api_keys_page():
    data = v1_data()
    data["scenes"][1]["beats"][0]["steps"].append(
        {"click": {"role": "link", "name": "Admin → API Keys"}}
    )
    _refused(data, "API keys page")


# --- The build (C2): a tree with no .git ------------------------------------------


def _tar(files: dict) -> io.BytesIO:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as archive:
        for name, data in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))
    buffer.seek(0)
    return buffer


def test_the_tree_is_built_from_an_archive_with_no_git(tmp_path):
    work = guard.WorkRoot(tmp_path / "work")
    tree = work.path / "tree-core"
    stream = _tar(
        {
            "VERSION": b"0.26.3\n",
            "backend/__init__.py": b"",
            "modules/__init__.py": b"",
            "frontend/package-lock.json": json.dumps(
                {"packages": {"node_modules/next": {"version": "16.3.8"}}}
            ).encode(),
        }
    )
    build.extract(stream, tree)
    assert not (tree / ".git").exists()
    build.strip_modules(tree, "core", work)
    assert (tree / "backend").is_dir() and not (tree / "modules").exists()
    with pytest.raises(build.BuildError, match="no modules/"):
        build.strip_modules(tree, "full", work)
    with pytest.raises(build.BuildError, match="exists already"):
        build.extract(_tar({"x": b""}), tree)
    assert build.next_versions(tree) == {"next_used": None, "next_lockfile": "16.3.8"}


def test_an_archive_cannot_write_outside_the_tree(tmp_path):
    with pytest.raises(tarfile.TarError):
        build.extract(_tar({"../escape.txt": b"x"}), tmp_path / "tree")
    assert not (tmp_path / "escape.txt").exists()


def test_the_bundle_checks_refuse_a_core_bundle_with_a_module_route(tmp_path):
    static = tmp_path / "static"
    static.mkdir()
    (static / "a.js").write_bytes(b"Separate teams into workspaces")
    assert "no module route" in build.bundle_check(static, "core")
    (static / "b.js").write_bytes(b'fetch("/api/v1/warehouse/x")')
    with pytest.raises(build.BuildError, match="references module routes: b.js"):
        build.bundle_check(static, "core")
    (static / "b.js").unlink()
    (static / "a.js").write_bytes(b"nothing")
    with pytest.raises(build.BuildError, match="no workspaces stub"):
        build.bundle_check(static, "core")


def test_a_missing_ref_is_refused_with_the_fetch_to_run(tmp_path):
    def missing(argv, **_kw):
        return subprocess.CompletedProcess(argv, 1, stdout="", stderr="")

    with pytest.raises(guard.Refused, match="fetch --tags origin"):
        build.resolve_ref(tmp_path, "v9.9.9", run=missing)
    with pytest.raises(guard.Refused, match="is not a ref"):
        build.resolve_ref(tmp_path, "v1; rm -rf /", run=missing)


# --- The launcher (C3) ------------------------------------------------------------

#: An editable install's finder, as setuptools writes it: a meta-path class
#: named _EditableFinder that maps a top-level name to another tree.
SITECUSTOMIZE = """
import importlib.util, sys
ELSEWHERE = {elsewhere!r}
class _EditableFinder:
    @classmethod
    def find_spec(cls, name, path=None, target=None):
        if name == "modules":
            return importlib.util.spec_from_file_location(name, ELSEWHERE + "/modules/__init__.py")
        return None
sys.meta_path.append(_EditableFinder)
"""


def _launch_tree(tmp_path: Path) -> tuple:
    tree = tmp_path / "tree"
    (tree / "backend").mkdir(parents=True)
    (tree / "backend" / "__init__.py").write_text("", encoding="utf-8")
    (tree / "backend" / "hello.py").write_text(
        "import sys\nprint('hello', sys.argv[1:])\n", encoding="utf-8"
    )
    elsewhere = tmp_path / "checkout"
    (elsewhere / "modules").mkdir(parents=True)
    (elsewhere / "modules" / "__init__.py").write_text("", encoding="utf-8")
    custom = tmp_path / "site"
    custom.mkdir()
    (custom / "sitecustomize.py").write_text(
        SITECUSTOMIZE.format(elsewhere=str(elsewhere)), encoding="utf-8"
    )
    env = {
        "PATH": "/usr/bin:/bin",
        "PYTHONPATH": str(custom),
        "EXPERIMENTLY_PROFILE": "core",
    }
    return tree, env


def test_the_launcher_removes_the_editable_finder_and_runs_the_copy(tmp_path):
    tree, env = _launch_tree(tmp_path)
    # Without the launcher the planted finder answers for `modules`: the
    # defect the launcher exists for.
    plain = subprocess.run(
        [sys.executable, "-c", "import importlib.util; print(importlib.util.find_spec('modules') is not None)"],
        cwd=tree, env=env, capture_output=True, text=True, check=True,
    )  # fmt: skip
    assert plain.stdout.strip() == "True"
    done = subprocess.run(
        python_argv(tree, "backend.hello", ["--x", "1"]),
        cwd=tree, env=env, capture_output=True, text=True,
    )  # fmt: skip
    assert done.returncode == 0, done.stderr
    first, second = done.stdout.splitlines()[:2]
    assert first.startswith(LAUNCHER_MARK) and "modules=None" in first
    assert str(tree.resolve()) in first
    assert second == "hello ['--x', '1']"


def test_the_launcher_refuses_a_core_copy_that_can_import_modules(tmp_path):
    tree, env = _launch_tree(tmp_path)
    env["PYTHONPATH"] = str(tmp_path / "checkout")
    done = subprocess.run(
        python_argv(tree, "backend.hello", []),
        cwd=tree,
        env=env,
        capture_output=True,
        text=True,
    )
    assert done.returncode != 0
    assert "core copy, but modules imports from" in done.stderr


def test_the_launcher_refuses_to_run_outside_its_copy(tmp_path):
    tree, env = _launch_tree(tmp_path)
    done = subprocess.run(
        python_argv(tree, "backend.hello", []),
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
    )
    assert done.returncode != 0 and "not in" in done.stderr


# --- The gates over recorded numbers -----------------------------------------------


def frames(*times: int) -> list:
    return [{"file": f"{i:06d}.jpg", "t_ms": t} for i, t in enumerate(times, start=1)]


def test_a_freeze_longer_than_the_longest_hold_is_refused():
    ok, numbers, _ = gates.freeze(frames(0, 1000, 2000), 3000, [2500], [])
    assert ok and numbers["longest_gap_ms"] == 1000
    ok, numbers, why = gates.freeze(frames(0, 9000), 10000, [2500], [])
    assert not ok and "no new frame for 9000 ms" in why
    # A still card is the point of a card: its span does not count.
    assert gates.freeze(frames(0, 9000), 10000, [2500], [(0, 8500)])[0]


def test_a_dark_stretch_is_refused():
    entries = frames(0, 100, 900, 1000)
    assert gates.black(entries, [200, 200, 200, 200], 1200, [])[0]
    ok, _, why = gates.black(entries, [200, 5, 5, 200], 1200, [])
    assert not ok and "dark for 900 ms" in why
    assert gates.black(entries, [200, 5, 5, 200], 1200, [(100, 1000)])[0]


def test_a_payoff_that_moves_or_is_short_is_refused():
    hold = {"where": "beat 5", "from_ms": 1000, "to_ms": 3600}
    assert gates.payoff_still(frames(0, 900), [hold])[0]
    ok, _, why = gates.payoff_still(frames(0, 2000), [hold])
    assert not ok and "changed during the payoff" in why
    ok, _, why = gates.payoff_still(frames(0), [{**hold, "to_ms": 2000}])
    assert not ok and "payoff held 1000 ms" in why
    assert not gates.payoff_still(frames(0), [])[0]


def test_the_needle_count_adds_up_and_is_never_empty():
    ok, numbers, _ = gates.needle_count(config.STATIC_NEEDLES, ["a-token-value-123"])
    assert ok and numbers == {"static": 4, "kept": 1, "total": 5, "required": 0}
    ok, _, why = gates.needle_count([], [])
    assert not ok and why == "0 needles: refusing to scan"
    assert not gates.needle_count(config.STATIC_NEEDLES[:3], [])[0]


def test_the_capture_reports_every_gate_the_render_requires():
    """The names the capture writes (run.py) cover contract.REQUIRED_CAPTURE_GATES."""
    source = (ACCEPTANCE / "showcase" / "capture" / "run.py").read_text(
        encoding="utf-8"
    )
    for name in contract.REQUIRED_CAPTURE_GATES:
        assert f'gates["{name}"]' in source or f'"{name}": ' in source, name


def test_the_manifest_viewport_is_the_contracts():
    assert {**config.VIEWPORT, "device_scale_factor": 1} == contract.VIEWPORT
    assert copy.deepcopy(config.VIEWPORT) == {
        "width": contract.PAGE_W,
        "height": contract.PAGE_H,
    }
