"""scripts/check_test_shards.py: every collected test ran in exactly one shard.

Every report fed to the checker here was written by real pytest running the
shard plugin (backend/tests/shard.py) over a fixture tree, never typed by hand:
a hand-written report proves only that the checker agrees with its author.
Where a defect cannot be produced by a real run (two shards selecting the same
test), a real report is copied and the one field is edited.

The tree carries the cases that break a naive checker: ``::`` inside a
parameter id, a module-level skip, an ``importorskip``, an xfail, and, in
variants, a parametrize id built at run time and a collection error.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[4]
SCRIPT = REPO_ROOT / "scripts" / "check_test_shards.py"
SUITE = "tests"
LF_FILE = "tests/test_lf.py"
ALLOWED = "an allowed reason"

BASE_TREE = {
    "test_base.py": '''
        import pytest

        @pytest.mark.parametrize("n", range(12))
        def test_plain(n):
            pass

        @pytest.mark.regression
        @pytest.mark.parametrize("n", range(6))
        def test_regression(n):
            pass

        @pytest.mark.parametrize("url", ["http://[::1]"])
        def test_chart_render(url):
            pass

        @pytest.mark.parametrize("line", ["downgrade-a89544fb1075\\n::warning::x"])
        def test_deploy_workflows(line):
            pass

        @pytest.mark.parametrize("host,loopback", [("::1", True), ("10.0.0.1", False)])
        def test_is_loopback(host, loopback):
            pass

        @pytest.mark.xfail(reason="known")
        def test_expected_failure():
            assert False

        @pytest.mark.skip(reason="'''
    + ALLOWED
    + """")
        def test_skipped():
            pass

        class TestShared:
            @pytest.fixture(scope="class")
            def shared(self):
                return object()

            @pytest.mark.parametrize("n", range(4))
            def test_uses_it(self, shared, n):
                assert shared is not None
        """,
    "test_modskip.py": """
        import pytest

        pytest.skip("not on this tree", allow_module_level=True)

        def test_never():
            pass
        """,
    "test_importorskip.py": """
        import pytest

        pytest.importorskip("a_module_this_tree_does_not_have")

        def test_never():
            pass
        """,
    "test_lf.py": """
        import os

        import pytest

        @pytest.mark.parametrize("n", range(6))
        def test_passes(n):
            pass

        @pytest.mark.parametrize("n", range(6))
        def test_fails_when_told(n):
            assert os.environ.get("FIXTURE_FAIL") != "1"
        """,
}

VARIANTS = {
    # A conftest that removes items without pytest_deselected. A directory
    # conftest registers after the root plugins, so even its trylast hook runs
    # before the shard's.
    # The re-review's construction: drop one real item without
    # pytest_deselected and append one pytest never collected, so the count
    # of items nets to zero on every shard.
    "substitute": {
        "conftest.py": """
            import pytest

            def _body():
                pass

            def pytest_collection_modifyitems(items):
                dropped = items.pop()
                items.append(
                    pytest.Function.from_parent(
                        dropped.parent, name="test_fabricated", callobj=_body
                    )
                )
            """
    },
    # The same item listed twice: no new object, but it runs twice.
    "duplicate_reference": {
        "conftest.py": """
            def pytest_collection_modifyitems(items):
                items.append(items[0])
            """
    },
    "conftest_drop": {
        "conftest.py": """
            def pytest_collection_modifyitems(items):
                del items[-5:]
            """
    },
    "conftest_drop_trylast": {
        "conftest.py": """
            import pytest

            @pytest.hookimpl(trylast=True)
            def pytest_collection_modifyitems(items):
                del items[-5:]
            """
    },
    # The control: a wrapper truncates after the shard hook has selected.
    "wrapper_drop": {
        "conftest.py": """
            import pytest

            @pytest.hookimpl(wrapper=True)
            def pytest_collection_modifyitems(items):
                result = yield
                del items[-2:]
                return result
            """
    },
    # A plugin, loaded by the rootdir conftest, that generates parameters.
    "generator": {
        "ROOT:conftest.py": """
            pytest_plugins = ["gen_plugin"]
            """,
        "ROOT:gen_plugin.py": """
            def pytest_generate_tests(metafunc):
                if "extra" in metafunc.fixturenames:
                    metafunc.parametrize("extra", [1, 2, 3, 4])
            """,
        "test_generated.py": """
            import pytest

            @pytest.fixture
            def extra():
                return 0

            def test_extra(extra):
                pass
            """,
    },
    "runtime_ids": {
        "test_runtime.py": """
            import uuid

            def pytest_generate_tests(metafunc):
                if "token" in metafunc.fixturenames:
                    metafunc.parametrize("token", [uuid.uuid4().hex for _ in range(3)])

            def test_token(token):
                pass
            """
    },
    "collection_error": {
        "test_broken.py": """
            import a_module_this_tree_does_not_have

            def test_never():
                pass
            """
    },
    "failing": {
        "test_fails.py": """
            def test_fails():
                assert False
            """
    },
}


def _write_tree(root: Path, variant: str | None) -> Path:
    tests = root / SUITE
    tests.mkdir(parents=True)
    files = dict(BASE_TREE)
    if variant:
        files.update(VARIANTS[variant])
    for name, body in files.items():
        # "ROOT:" files go beside the suite: the rootdir conftest, a plugin.
        target = root / name[5:] if name.startswith("ROOT:") else tests / name
        target.write_text(textwrap.dedent(body))
    return root


def _env(extra: dict[str, str] | None = None) -> dict[str, str]:
    env = {
        k: v
        for k, v in os.environ.items()
        if not k.startswith("EXPERIMENTLY_TEST_SHARD") and k != "PYTEST_ADDOPTS"
    }
    env["PYTHONPATH"] = str(REPO_ROOT)
    env.update(extra or {})
    return env


def _pytest_cmd(
    root: Path, *args: str, cache: bool = False, target: str = SUITE
) -> list[str]:
    return [
        sys.executable,
        "-m",
        "pytest",
        "-p",
        "backend.tests.shard",
        "-p",
        "no:cov",
        *(
            ["-o", f"cache_dir={root / '.cache'}"]
            if cache
            else ["-p", "no:cacheprovider"]
        ),
        "-c",
        os.devnull,
        "--rootdir",
        str(root),
        "-q",
        *args,
        target,
    ]


def run_shards(
    root: Path,
    out: Path,
    total: int,
    *args: str,
    env: dict[str, str] | None = None,
    cache: bool = False,
    target: str = SUITE,
) -> list[Path]:
    """Run every shard of ``root`` concurrently; return the reports written."""
    out.mkdir(parents=True)
    procs = []
    for index in range(1, total + 1):
        report = out / f"shard-{SUITE}-{index}.json"
        shard_env = _env(env)
        shard_env["EXPERIMENTLY_TEST_SHARD"] = f"{index}/{total}"
        shard_env["EXPERIMENTLY_TEST_SHARD_REPORT"] = str(report)
        procs.append(
            (
                report,
                subprocess.Popen(
                    _pytest_cmd(root, *args, cache=cache, target=target),
                    cwd=root,
                    env=shard_env,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                ),
            )
        )
    reports = []
    for report, proc in procs:
        output, _ = proc.communicate(timeout=300)
        # A shard may fail (that is what some variants are for); it must still
        # have written its report, or the variant proves nothing.
        assert report.exists() and report.stat().st_size, output
        reports.append(report)
    return reports


class Fixtures:
    """Report sets, each produced once per session by real pytest runs."""

    def __init__(self, base: Path) -> None:
        self.base = base
        self._cache: dict[str, list[Path]] = {}

    def get(self, name: str) -> list[Path]:
        if name not in self._cache:
            self._cache[name] = self._make(name)
        return self._cache[name]

    def _make(self, name: str) -> list[Path]:
        where = self.base / name
        out = where / "reports"
        if name == "clean":
            return run_shards(_write_tree(where / "tree", None), out, 3)
        if name in VARIANTS:
            return run_shards(_write_tree(where / "tree", name), out, 2)
        if name in ("lf", "lf_file"):
            root = _write_tree(where / "tree", None)
            seed = subprocess.run(
                _pytest_cmd(root, cache=True),
                cwd=root,
                env=_env({"FIXTURE_FAIL": "1"}),
                capture_output=True,
                text=True,
                timeout=300,
            )
            assert seed.returncode == 1, seed.stdout + seed.stderr
            if name == "lf_file":
                # A file named on the command line keeps its passing tests
                # through collection; --lf deselects them after the shard hook.
                return run_shards(root, out, 2, "--lf", cache=True, target=LF_FILE)
            return run_shards(root, out, 2, "--lf", cache=True)
        if name == "other_suite":
            # Another suite (one file of the tree, named on the command
            # line), in two shards.
            root = _write_tree(where / "tree", None)
            return run_shards(root, out, 2, target=LF_FILE)
        if name == "generator_disabled":
            root = _write_tree(where / "tree", "generator")
            return run_shards(root, out, 2, env={"PYTEST_ADDOPTS": "-p no:gen_plugin"})
        if name == "autoload_disabled":
            root = _write_tree(where / "tree", None)
            return run_shards(root, out, 2, env={"PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1"})
        if name == "addopts_regression":
            root = _write_tree(where / "tree", None)
            return run_shards(root, out, 2, env={"PYTEST_ADDOPTS": "-m regression"})
        flags = NARROWING[name]
        return run_shards(_write_tree(where / "tree", None), out, 2, *flags)


NARROWING = {
    "markexpr": ["-m", "not regression"],
    "keyword": ["-k", "not plain"],
    "deselect": ["--deselect", "tests/test_base.py::test_plain[0]"],
    "ignore": ["--ignore", "tests/test_lf.py"],
    "ignore_glob": ["--ignore-glob", "*test_lf*"],
}


@pytest.fixture(scope="module")
def fixtures(tmp_path_factory) -> Fixtures:
    return Fixtures(tmp_path_factory.mktemp("shard-fixtures"))


# The fixture sessions pass ``-p no:cov`` (as the CI commands do) and, unless
# they need the cache, ``-p no:cacheprovider``. ``check`` names both for every
# ``--suite`` it is given; ``plugins_named=False`` leaves them refused.
FIXTURE_DISABLED = ("cov", "cacheprovider")


def check(*args: str | Path, plugins_named: bool = True) -> subprocess.CompletedProcess:
    argv = [str(a) for a in args]
    if plugins_named:
        suites = [argv[i + 1] for i, a in enumerate(argv[:-1]) if a == "--suite"]
        for suite in suites:
            for name in FIXTURE_DISABLED:
                argv += ["--allow-disabled-plugin", f"{suite}={name}"]
    return subprocess.run(
        [sys.executable, str(SCRIPT), *argv],
        capture_output=True,
        text=True,
        timeout=120,
    )


def problems(result: subprocess.CompletedProcess) -> list[str]:
    return [line for line in result.stdout.splitlines() if line.startswith("PROBLEM: ")]


def _load(path: Path) -> dict:
    return json.loads(path.read_text())


def _edited(tmp_path: Path, reports: list[Path], edit) -> list[Path]:
    """Copies of real reports, with ``edit(index, doc)`` applied to each."""
    out = []
    for index, path in enumerate(reports):
        doc = _load(path)
        edit(index, doc)
        copy = tmp_path / f"shard-copy-{index}.json"
        copy.write_text(json.dumps(doc))
        out.append(copy)
    return out


# --- the clean case -------------------------------------------------------


def test_a_clean_run_is_proven(fixtures):
    reports = fixtures.get("clean")
    result = check("--suite", SUITE, "--allow-skip", f"{SUITE}={ALLOWED}", *reports)
    assert result.returncode == 0, result.stdout + result.stderr
    collected = _load(reports[0])["collected"]
    assert f"{SUITE}: 3 shards, {len(collected)} collected, each ran exactly once" in (
        result.stdout
    )


def test_the_fixture_tree_carries_the_awkward_ids(fixtures):
    collected = _load(fixtures.get("clean")[0])["collected"]
    assert "tests/test_base.py::test_chart_render[http://[::1]]" in collected
    assert "tests/test_base.py::test_is_loopback[::1-True]" in collected
    assert any("\\n::warning::x" in n for n in collected)
    assert not any("test_modskip" in n or "test_importorskip" in n for n in collected)
    outcomes = (
        {r["outcome"] for r in _load(fixtures.get("clean")[0])["ran"]}
        | {r["outcome"] for r in _load(fixtures.get("clean")[1])["ran"]}
        | {r["outcome"] for r in _load(fixtures.get("clean")[2])["ran"]}
    )
    assert outcomes == {"passed", "skipped", "xfailed"}


def test_a_directory_is_searched_for_reports(fixtures, tmp_path):
    for index, report in enumerate(fixtures.get("clean")):
        leg = tmp_path / f"leg-{index}"
        leg.mkdir()
        (leg / report.name).write_text(report.read_text())
    (tmp_path / "unrelated.json").write_text("not a report")
    result = check("--suite", SUITE, "--allow-skip", f"{SUITE}={ALLOWED}", tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr


# --- V1: the partition ----------------------------------------------------


@pytest.mark.regression
def test_a_dropped_shard_is_refused(fixtures):
    reports = fixtures.get("clean")
    result = check("--suite", SUITE, "--any-skip", SUITE, *reports[:2])
    assert result.returncode == 1
    assert f"PROBLEM: {SUITE}: shard(s) [3] of 3 have no report" in result.stdout
    assert any("collected but selected by no shard" in p for p in problems(result))


@pytest.mark.regression
def test_a_duplicate_index_is_refused(fixtures, tmp_path):
    reports = fixtures.get("clean")
    copy = tmp_path / "shard-again.json"
    copy.write_text(reports[1].read_text())
    result = check("--suite", SUITE, "--any-skip", SUITE, reports[0], reports[1], copy)
    assert result.returncode == 1
    assert any("shard 2 reported 2 times" in p for p in problems(result))
    assert any("selected by more than one shard" in p for p in problems(result))
    assert any("shard(s) [3] of 3 have no report" in p for p in problems(result))


@pytest.mark.regression
def test_shards_of_different_counts_are_refused(fixtures, tmp_path):
    reports = fixtures.get("clean")

    def edit(index, doc):
        if index == 2:
            doc["of"] = 4

    result = check(
        "--suite", SUITE, "--any-skip", SUITE, *_edited(tmp_path, reports, edit)
    )
    assert result.returncode == 1
    assert "disagree on the shard count: [3, 4]" in result.stdout


@pytest.mark.regression
def test_an_overlap_is_refused(fixtures, tmp_path):
    reports = fixtures.get("clean")
    stolen = _load(reports[1])["ran"][0]

    def edit(index, doc):
        if index == 0:
            doc["selected"] = sorted(doc["selected"] + [stolen["nodeid"]])
            doc["ran"].append(stolen)

    result = check(
        "--suite", SUITE, "--any-skip", SUITE, *_edited(tmp_path, reports, edit)
    )
    assert result.returncode == 1
    assert problems(result) == [
        f"PROBLEM: {SUITE}: 1 selected by more than one shard [{stolen['nodeid']!r}]"
    ]


@pytest.mark.regression
def test_an_empty_shard_is_refused(fixtures, tmp_path):
    reports = fixtures.get("clean")

    def edit(index, doc):
        if index == 1:
            doc["selected"], doc["ran"] = [], []

    result = check(
        "--suite", SUITE, "--any-skip", SUITE, *_edited(tmp_path, reports, edit)
    )
    assert result.returncode == 1
    assert f"PROBLEM: {SUITE}: shard 2 selected nothing" in result.stdout
    assert any("collected but selected by no shard" in p for p in problems(result))


@pytest.mark.regression
def test_an_empty_expected_set_is_refused(fixtures, tmp_path):
    reports = fixtures.get("clean")

    def edit(index, doc):
        doc["collected"], doc["selected"], doc["ran"] = [], [], []

    result = check(
        "--suite", SUITE, "--any-skip", SUITE, *_edited(tmp_path, reports, edit)
    )
    assert result.returncode == 1
    assert (
        "the collected set is EMPTY: refusing to compare against nothing"
        in result.stdout
    )


@pytest.mark.regression
def test_node_ids_that_differ_between_processes_are_refused(fixtures):
    result = check("--suite", SUITE, "--any-skip", SUITE, *fixtures.get("runtime_ids"))
    assert result.returncode == 1
    assert any(
        "collected a different list" in p and "test_token[" in p
        for p in problems(result)
    ), result.stdout


@pytest.mark.regression
def test_a_collection_error_is_refused(fixtures):
    result = check(
        "--suite", SUITE, "--any-skip", SUITE, *fixtures.get("collection_error")
    )
    assert result.returncode == 1
    assert any(
        "collection error(s)" in p and "test_broken.py" in p for p in problems(result)
    ), result.stdout


# --- what ran -------------------------------------------------------------


@pytest.mark.regression
def test_a_failed_test_is_refused_by_name(fixtures):
    result = check(
        "--suite", SUITE, "--allow-skip", f"{SUITE}={ALLOWED}", *fixtures.get("failing")
    )
    assert result.returncode == 1
    assert any(
        p.endswith("tests/test_fails.py::test_fails failed") for p in problems(result)
    )


@pytest.mark.regression
def test_a_selected_test_that_did_not_run_is_refused(fixtures, tmp_path):
    reports = fixtures.get("clean")
    dropped = _load(reports[0])["ran"][0]["nodeid"]

    def edit(index, doc):
        if index == 0:
            doc["ran"] = doc["ran"][1:]

    result = check(
        "--suite", SUITE, "--any-skip", SUITE, *_edited(tmp_path, reports, edit)
    )
    assert result.returncode == 1
    assert problems(result) == [
        f"PROBLEM: {SUITE}: shard 1: 1 selected but did not run [{dropped!r}]"
    ]


@pytest.mark.regression
def test_a_test_run_twice_or_not_selected_is_refused(fixtures, tmp_path):
    reports = fixtures.get("clean")
    other = _load(reports[1])["ran"][0]

    def edit(index, doc):
        if index == 0:
            doc["ran"].append(dict(doc["ran"][0]))
            doc["ran"].append(other)

    result = check(
        "--suite", SUITE, "--any-skip", SUITE, *_edited(tmp_path, reports, edit)
    )
    assert result.returncode == 1
    assert any("1 ran more than once" in p for p in problems(result))
    assert any(
        "1 ran but were not selected" in p and other["nodeid"] in p
        for p in problems(result)
    )


@pytest.mark.regression
def test_a_skip_is_refused_unless_its_reason_is_allowed(fixtures):
    reports = fixtures.get("clean")
    refused = check("--suite", SUITE, *reports)
    assert refused.returncode == 1
    assert problems(refused) == [
        f"PROBLEM: {SUITE}: shard {s}: tests/test_base.py::test_skipped was skipped, "
        f"and its reason is not allowed: {ALLOWED!r}"
        for s in [
            _load(r)["shard"]
            for r in reports
            if "tests/test_base.py::test_skipped" in _load(r)["selected"]
        ]
    ]
    other = check("--suite", SUITE, "--allow-skip", f"{SUITE}=another reason", *reports)
    assert other.returncode == 1
    assert check("--suite", SUITE, "--any-skip", SUITE, *reports).returncode == 0


# --- narrowing: each recorded field, from a real run ----------------------


@pytest.mark.regression
@pytest.mark.parametrize(
    "plant, expected",
    [
        ("markexpr", "narrowed by markexpr='not regression'"),
        ("keyword", "narrowed by keyword='not plain'"),
        ("deselect", "deselect=['tests/test_base.py::test_plain[0]']"),
        ("ignore", "narrowed by ignore=['tests/test_lf.py']"),
        ("ignore_glob", "narrowed by ignore_glob=['*test_lf*']"),
    ],
)
def test_a_narrowed_session_is_refused(fixtures, plant, expected):
    result = check("--suite", SUITE, "--any-skip", SUITE, *fixtures.get(plant))
    assert result.returncode == 1
    found = problems(result)
    assert found and all("narrowed by" in p for p in found), result.stdout
    assert any(expected in p for p in found), result.stdout


@pytest.mark.regression
def test_a_job_level_markexpr_is_refused(fixtures):
    """PYTEST_ADDOPTS="-m regression" on the whole job: every shard agrees on
    the narrowed set, so every partition rule holds -- only the narrowing rule
    can see it."""
    reports = fixtures.get("addopts_regression")
    docs = [_load(r) for r in reports]
    assert all(len(d["collected"]) == 6 for d in docs), "the narrowing took effect"
    result = check("--suite", SUITE, "--any-skip", SUITE, *reports)
    assert result.returncode == 1
    assert problems(result) == [
        f"PROBLEM: {SUITE}: shard {d['shard']}: narrowed by markexpr='regression', "
        f"pre_deselected={d['pre_deselected']} (items another plugin deselected)"
        for d in docs
    ]


@pytest.mark.regression
def test_last_failed_is_refused(fixtures):
    """--lf over a directory: pytest 9 drops the passing tests while it
    collects, so every shard agrees on the narrowed list and nothing is
    deselected. ``last_failed`` is what sees it."""
    reports = fixtures.get("lf")
    docs = [_load(r) for r in reports]
    assert all(d["pre_deselected"] == 0 for d in docs)
    assert all(len(d["collected"]) == 6 for d in docs), "the narrowing took effect"
    result = check("--suite", SUITE, "--any-skip", SUITE, *reports)
    assert result.returncode == 1
    assert problems(result) == [
        f"PROBLEM: {SUITE}: shard {d['shard']}: narrowed by last_failed=True (--lf)"
        for d in docs
    ]


@pytest.mark.regression
def test_last_failed_deselection_is_refused_through_pre_deselected(fixtures):
    reports = fixtures.get("lf_file")
    docs = [_load(r) for r in reports]
    assert sum(d["pre_deselected"] for d in docs) == 6, "--lf deselected the passes"
    result = check("--suite", LF_FILE, "--any-skip", LF_FILE, *reports)
    assert result.returncode == 1
    found = problems(result)
    assert any("pre_deselected=" in p for p in found), result.stdout
    # The passes it deselected were selected and never ran.
    assert any("selected but did not run" in p for p in found), result.stdout


# --- items removed without a trace, and disabled plugins ----------------


@pytest.mark.regression
@pytest.mark.parametrize("variant", ["conftest_drop", "conftest_drop_trylast"])
def test_items_removed_before_the_shard_hook_are_refused(fixtures, variant):
    """Every shard agrees on the shrunk list and nothing is deselected, so
    only the items pytest_itemcollected saw can show it."""
    reports = fixtures.get(variant)
    docs = [_load(r) for r in reports]
    clean = len(_load(fixtures.get("clean")[0])["collected"])
    assert all(len(d["collected"]) == clean - 5 for d in docs), "the drop took effect"
    assert all(d["pre_deselected"] == 0 for d in docs)
    result = check("--suite", SUITE, "--any-skip", SUITE, *reports)
    assert result.returncode == 1
    assert all(len(d["unannounced"]) == 5 and not d["fabricated"] for d in docs)
    found = problems(result)
    assert len(found) == len(docs), result.stdout
    for d, line in zip(docs, found, strict=True):
        assert line.startswith(
            f"PROBLEM: {SUITE}: shard {d['shard']}: narrowed by unannounced="
        )
        assert line.endswith(
            "(5 collected but gone before the shard hook, never deselected)"
        )


@pytest.mark.regression
def test_a_fabricated_substitute_is_refused(fixtures):
    reports = fixtures.get("substitute")
    docs = [_load(r) for r in reports]
    clean = _load(fixtures.get("clean")[0])["collected"]
    assert all(len(d["collected"]) == len(clean) for d in docs), (
        "the count nets to zero"
    )
    gone = sorted(set(clean) - set(docs[0]["collected"]))
    assert len(gone) == 1
    result = check("--suite", SUITE, "--any-skip", SUITE, *reports)
    assert result.returncode == 1
    fabricated = "tests/test_lf.py::test_fabricated"
    assert problems(result) == [
        f"PROBLEM: {SUITE}: shard {d['shard']}: narrowed by unannounced=[{gone[0]!r}] "
        "(1 collected but gone before the shard hook, never deselected), "
        f"fabricated=[{fabricated!r}] (1 present at the shard hook but never collected)"
        for d in docs
    ]


@pytest.mark.regression
def test_an_item_listed_twice_is_refused(fixtures):
    reports = fixtures.get("duplicate_reference")
    docs = [_load(r) for r in reports]
    assert all(not d["unannounced"] and not d["fabricated"] for d in docs)
    result = check("--suite", SUITE, "--any-skip", SUITE, *reports)
    assert result.returncode == 1
    assert any("collected twice in one session" in p for p in problems(result)), (
        result.stdout
    )


@pytest.mark.regression
def test_items_removed_after_the_shard_hook_are_refused(fixtures):
    reports = fixtures.get("wrapper_drop")
    docs = [_load(r) for r in reports]
    assert all(not d["unannounced"] and not d["fabricated"] for d in docs)
    result = check("--suite", SUITE, "--any-skip", SUITE, *reports)
    assert result.returncode == 1
    found = problems(result)
    assert found and all("selected but did not run" in p for p in found), result.stdout


def test_a_generating_plugin_is_counted_when_loaded(fixtures):
    reports = fixtures.get("generator")
    assert (
        sum(
            "test_generated.py::test_extra[" in n
            for n in _load(reports[0])["collected"]
        )
        == 4
    )
    result = check("--suite", SUITE, "--any-skip", SUITE, *reports)
    assert result.returncode == 0, result.stdout


@pytest.mark.regression
def test_a_disabled_plugin_is_refused(fixtures):
    """PYTEST_ADDOPTS="-p no:gen_plugin": four generated tests become one on
    every shard, and every partition rule still holds."""
    reports = fixtures.get("generator_disabled")
    docs = [_load(r) for r in reports]
    loaded = len(_load(fixtures.get("generator")[0])["collected"])
    assert all(len(d["collected"]) == loaded - 3 for d in docs), "it took effect"
    result = check("--suite", SUITE, "--any-skip", SUITE, *reports)
    assert result.returncode == 1
    assert problems(result) == [
        f"PROBLEM: {SUITE}: shard {d['shard']}: narrowed by "
        "disabled_plugins=['gen_plugin'] (-p no:)"
        for d in docs
    ]
    named = check(
        "--suite",
        SUITE,
        "--any-skip",
        SUITE,
        "--allow-disabled-plugin",
        f"{SUITE}=gen_plugin",
        *reports,
    )
    assert named.returncode == 0, named.stdout


@pytest.mark.regression
def test_the_ci_plugins_must_be_named(fixtures):
    reports = fixtures.get("clean")
    result = check("--suite", SUITE, "--any-skip", SUITE, *reports, plugins_named=False)
    assert result.returncode == 1
    assert all(
        "disabled_plugins=['cacheprovider', 'cov'] (-p no:)" in p
        for p in problems(result)
    ), result.stdout


@pytest.mark.regression
def test_turning_plugin_autoload_off_is_refused(fixtures):
    reports = fixtures.get("autoload_disabled")
    result = check("--suite", SUITE, "--any-skip", SUITE, *reports)
    assert result.returncode == 1
    found = problems(result)
    assert found and all("narrowed by autoload_disabled=True" in p for p in found), (
        result.stdout
    )


# --- reports the checker cannot read --------------------------------------


@pytest.mark.regression
@pytest.mark.parametrize(
    "damage", ["truncated", "empty", "not json", "no ran", "bad outcome"]
)
def test_an_unreadable_report_is_refused(fixtures, tmp_path, damage):
    reports = fixtures.get("clean")
    text = reports[0].read_text()
    bad = tmp_path / "shard-bad.json"
    if damage == "truncated":
        bad.write_text(text[: len(text) // 2])
    elif damage == "empty":
        bad.write_text("")
    elif damage == "not json":
        bad.write_text("<testsuite/>")
    else:
        doc = json.loads(text)
        if damage == "no ran":
            del doc["ran"]
        else:
            doc["ran"][0]["outcome"] = "rerun"
        bad.write_text(json.dumps(doc))
    result = check("--suite", SUITE, "--any-skip", SUITE, bad, *reports[1:])
    assert result.returncode == 2, result.stdout + result.stderr
    assert "refusing a bad report" in result.stderr


# --- the suite filter and the command line --------------------------------


def test_only_the_named_suite_is_checked(fixtures, tmp_path):
    reports = fixtures.get("clean")

    def edit(index, doc):
        doc["suite"] = "elsewhere"

    others = _edited(tmp_path, [fixtures.get("failing")[0]], edit)
    ok = check("--suite", SUITE, "--any-skip", SUITE, *reports, *others)
    assert ok.returncode == 0, ok.stdout
    missing = check("--suite", "backend/tests/integration", *reports)
    assert missing.returncode == 1
    assert "backend/tests/integration: no shard report for this suite" in missing.stdout


def test_only_suites_refuses_a_report_of_a_suite_not_named(fixtures, tmp_path):
    """A clean run of the named suite, and another suite that ran 1 of its 2
    shards, in one directory. Without the flag the other report is left
    alone, rc 0; with ``--only-suites`` it is a problem, rc 1."""
    directory = tmp_path / "reports"
    directory.mkdir()
    for index, path in enumerate(fixtures.get("clean"), start=1):
        (directory / f"shard-{SUITE}-{index}.json").write_bytes(path.read_bytes())
    first, _second = fixtures.get("other_suite")
    other = _load(first)
    assert (other["suite"], other["shard"], other["of"]) == (LF_FILE, 1, 2)
    stray = directory / "shard-other-1.json"
    stray.write_bytes(first.read_bytes())
    default = check("--suite", SUITE, "--any-skip", SUITE, directory)
    assert default.returncode == 0, default.stdout
    only = check("--suite", SUITE, "--only-suites", "--any-skip", SUITE, directory)
    assert only.returncode == 1, only.stdout
    assert problems(only) == [f"PROBLEM: {stray}: suite {LF_FILE} is not a --suite"], (
        only.stdout
    )
    both = check(
        "--suite",
        SUITE,
        "--suite",
        LF_FILE,
        "--only-suites",
        "--any-skip",
        SUITE,
        "--any-skip",
        LF_FILE,
        directory,
    )
    assert both.returncode == 1, both.stdout
    # Named, the other suite is checked like any other: its missing shard.
    found = problems(both)
    assert f"PROBLEM: {LF_FILE}: shard(s) [2] of 2 have no report" in found, found
    assert not [line for line in found if "is not a --suite" in line], found


def annotations(result: subprocess.CompletedProcess) -> list[str]:
    return [line for line in result.stdout.splitlines() if line.startswith("::")]


def test_github_annotations_repeat_each_problem_and_only_with_the_flag(fixtures):
    """A failed record: rc 1 either way. With ``--github-annotations`` each
    PROBLEM line is followed by ``::error::`` and the same text, so the
    workflow step needs no pipe to show it; without the flag there is none."""
    args = ("--suite", SUITE, "--allow-skip", f"{SUITE}={ALLOWED}")
    plain = check(*args, *fixtures.get("failing"))
    assert plain.returncode == 1, plain.stdout
    assert annotations(plain) == [], plain.stdout
    flagged = check(*args, "--github-annotations", *fixtures.get("failing"))
    assert flagged.returncode == 1, flagged.stdout
    found = problems(flagged)
    assert any(p.endswith("tests/test_fails.py::test_fails failed") for p in found)
    assert found == problems(plain)
    assert annotations(flagged) == [
        "::error::" + p[len("PROBLEM: ") :] for p in found
    ], flagged.stdout


def test_github_annotations_cover_a_bad_report_and_no_report(fixtures, tmp_path):
    bad = tmp_path / "shard-bad.json"
    bad.write_text("")
    result = check("--suite", SUITE, "--github-annotations", bad)
    assert result.returncode == 2, result.stdout + result.stderr
    assert "refusing a bad report" in result.stderr
    assert annotations(result) == [
        f"::error::refusing a bad report: {bad}: empty (the session did not "
        "finish writing it)"
    ], result.stdout
    empty = tmp_path / "none"
    empty.mkdir()
    nothing = check("--suite", SUITE, "--github-annotations", empty)
    assert nothing.returncode == 1, nothing.stdout
    assert annotations(nothing) == [f"::error::no shard report found in {empty}"]


def test_a_value_read_from_a_report_stays_on_its_line(fixtures, tmp_path):
    """A suite, and a failed record's node id, each with a newline and a
    percent sign in it. Each PROBLEM line shows the newline escaped, and its
    annotation escapes the percent sign as a workflow command's data requires:
    no line of the output starts with anything the report wrote."""

    def edit(index, doc):
        doc["suite"] = "elsewhere 100%\n::warning::x"

    def edit_record(index, doc):
        doc["ran"][0]["nodeid"] = "tests/odd 100%\n::warning::y"
        doc["ran"][0]["outcome"] = "failed"

    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    (stray,) = _edited(tmp_path / "a", [fixtures.get("clean")[0]], edit)
    first, *rest = fixtures.get("clean")
    (odd,) = _edited(tmp_path / "b", [first], edit_record)
    result = check(
        "--suite",
        SUITE,
        "--only-suites",
        "--github-annotations",
        "--any-skip",
        SUITE,
        stray,
        odd,
        *rest,
    )
    assert result.returncode == 1, result.stdout
    lines = result.stdout.splitlines()
    assert not [line for line in lines if line.startswith("::warning")], lines
    suite = f"{stray}: suite elsewhere 100%\\n::warning::x is not a --suite"
    assert f"PROBLEM: {suite}" in lines, lines
    assert "::error::" + suite.replace("%", "%25") in lines, lines
    record = "tests/odd 100%\\n::warning::y failed"
    assert [line for line in lines if line.endswith(record)], lines
    assert [
        line
        for line in annotations(result)
        if line.endswith(record.replace("%", "%25"))
    ], lines


def test_no_reports_at_all_is_refused(tmp_path):
    result = check("--suite", SUITE, tmp_path)
    assert result.returncode == 1
    assert "no shard report found" in result.stdout


@pytest.mark.parametrize(
    "args",
    [
        [],
        ["DIR"],
        ["--suite", SUITE],
        ["--suite", SUITE, "--allow-skip", "elsewhere=reason", "DIR"],
        ["--suite", SUITE, "--allow-disabled-plugin", "elsewhere=cov", "DIR"],
        ["--suite", SUITE, "--allow-disabled-plugin", SUITE, "DIR"],
        ["--suite", SUITE, "--allow-disabled-plugin", f"{SUITE}=no:cov", "DIR"],
        ["--suite", SUITE, "--allow-skip", SUITE, "DIR"],
        ["--suite", SUITE, "--any-skip", "elsewhere", "DIR"],
        ["--suite", SUITE, "--any-skip", SUITE, "--allow-skip", f"{SUITE}=r", "DIR"],
        ["--suite", SUITE, "--suite", SUITE + "/", "DIR"],
        ["--suite", SUITE, "/no/such/path"],
    ],
)
def test_a_bad_command_line_exits_2(tmp_path, args):
    result = check(*[str(tmp_path) if a == "DIR" else a for a in args])
    assert result.returncode == 2, result.stdout + result.stderr
