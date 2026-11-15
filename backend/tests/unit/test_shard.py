"""The shard selector (backend/tests/shard.py): what it selects, what it
records, and what it refuses.

Its ``ran`` records are what scripts/check_test_shards.py trusts, and the
plugin both selects the tests and attests to them, so the records are checked
here against pytest's own reports of the same session (``reprec``), outcome by
outcome. The checker's own tests (backend/tests/unit/scripts/
test_check_test_shards.py) feed it reports this plugin wrote.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from backend.tests.shard import ENV_REPORT, ENV_SHARD, shard_of

pytest_plugins = ["pytester"]

REPO_ROOT = Path(__file__).resolve().parents[3]
PLUGIN = "backend.tests.shard"

# Every outcome pytest can give an item, each from the phase that decides it.
OUTCOMES_TREE = """
import pytest

@pytest.fixture
def skip_in_setup():
    pytest.skip("skipped from a fixture")

@pytest.fixture
def broken_setup():
    raise RuntimeError("setup broke")

@pytest.fixture
def broken_teardown():
    yield
    raise RuntimeError("teardown broke")

def test_passes():
    pass

def test_fails():
    assert False

def test_skips_in_call():
    pytest.skip("skipped in the call")

@pytest.mark.skip(reason="skipped by a mark")
def test_skip_mark():
    pass

def test_skips_in_setup(skip_in_setup):
    pass

@pytest.mark.xfail(reason="known")
def test_xfails():
    assert False

@pytest.mark.xfail(reason="not run", run=False)
def test_xfail_not_run():
    pass

@pytest.mark.xfail(reason="fixed already")
def test_xpasses():
    pass

@pytest.mark.xfail(reason="strict", strict=True)
def test_xpasses_strictly():
    pass

def test_setup_errors(broken_setup):
    pass

def test_teardown_errors(broken_teardown):
    pass

def test_fails_and_teardown_errors(broken_teardown):
    assert False

@pytest.mark.parametrize("value", ["http://[::1]", "a\\n::warning::x", "::1-True"])
def test_colons_in_ids(value):
    pass
"""


@pytest.fixture(autouse=True)
def _no_inherited_shard(monkeypatch):
    # This module may itself run inside a sharded session.
    monkeypatch.delenv(ENV_SHARD, raising=False)
    monkeypatch.delenv(ENV_REPORT, raising=False)


def _shard(monkeypatch, spec: str, report: Path | str | None) -> None:
    monkeypatch.setenv(ENV_SHARD, spec)
    if report is not None:
        monkeypatch.setenv(ENV_REPORT, str(report))


def _pytests_own_outcomes(reprec) -> dict[str, str]:
    """Each item's outcome as pytest's own terminal classifies its reports."""
    config = reprec.getcall("pytest_sessionfinish").session.config
    seen: dict[str, set[str]] = {}
    for report in reprec.getreports("pytest_runtest_logreport"):
        category = config.hook.pytest_report_teststatus(report=report, config=config)[0]
        seen.setdefault(report.nodeid, set())
        if category:
            seen[report.nodeid].add(category)
    outcomes = {}
    for nodeid, categories in seen.items():
        if "error" in categories:
            outcomes[nodeid] = "error"
        else:
            assert len(categories) == 1, (nodeid, categories)
            (outcomes[nodeid],) = categories
    return outcomes


def _pytests_own_skip_reasons(reprec) -> dict[str, str]:
    reasons = {}
    for report in reprec.getreports("pytest_runtest_logreport"):
        if report.skipped and not hasattr(report, "wasxfail"):
            reasons[report.nodeid] = report.longrepr[2].removeprefix("Skipped: ")
    return reasons


@pytest.mark.regression
def test_ran_records_match_pytests_own_reports(pytester, monkeypatch, tmp_path):
    pytester.makepyfile(test_outcomes=OUTCOMES_TREE)
    report = tmp_path / "shard.json"
    _shard(monkeypatch, "1/1", report)

    reprec = pytester.inline_run("-p", PLUGIN, "-p", "no:cacheprovider")

    ran = json.loads(report.read_text())["ran"]
    nodeids = [r["nodeid"] for r in ran]
    assert len(nodeids) == len(set(nodeids)), "one record per item"
    expected = _pytests_own_outcomes(reprec)
    assert {r["nodeid"]: r["outcome"] for r in ran} == expected
    # Every outcome is exercised, so a classifier that loses one fails here.
    assert set(expected.values()) == {
        "passed",
        "failed",
        "skipped",
        "xfailed",
        "xpassed",
        "error",
    }
    reasons = {r["nodeid"]: r["skip_reason"] for r in ran if r["outcome"] == "skipped"}
    assert reasons == _pytests_own_skip_reasons(reprec)
    assert set(reasons.values()) == {
        "skipped from a fixture",
        "skipped in the call",
        "skipped by a mark",
    }
    assert all(r["skip_reason"] is None for r in ran if r["outcome"] != "skipped")


@pytest.mark.regression
def test_shards_partition_the_session_and_run_what_they_select(
    pytester, monkeypatch, tmp_path
):
    pytester.makepyfile(
        test_many="""
        import pytest

        @pytest.mark.parametrize("n", range(40))
        def test_n(n):
            pass
        """
    )
    reports = []
    for index in (1, 2, 3):
        report = tmp_path / f"shard-{index}.json"
        _shard(monkeypatch, f"{index}/3", report)
        reprec = pytester.inline_run("-p", PLUGIN, "-p", "no:cacheprovider")
        doc = json.loads(report.read_text())
        executed = {r.nodeid for r in reprec.getreports("pytest_runtest_logreport")}
        assert executed == set(doc["selected"]), "it ran exactly what it selected"
        assert all(shard_of(n, 3) == index for n in doc["selected"])
        assert (doc["shard"], doc["of"], doc["suite"]) == (index, 3, ".")
        reports.append(doc)

    collected = reports[0]["collected"]
    assert len(collected) == 40
    assert all(doc["collected"] == collected for doc in reports)
    selected = [set(doc["selected"]) for doc in reports]
    assert all(selected), "no shard is empty"
    assert set().union(*selected) == set(collected)
    assert sum(len(s) for s in selected) == len(collected), "disjoint"


def test_unset_is_a_noop(pytester, tmp_path):
    pytester.makepyfile(
        test_few="""
        def test_a(): pass
        def test_b(): pass
        """
    )
    before = set(tmp_path.iterdir())
    result = pytester.runpytest("-p", PLUGIN, "-p", "no:cacheprovider")
    result.assert_outcomes(passed=2)
    assert "deselected" not in result.stdout.str()
    assert set(tmp_path.iterdir()) == before


@pytest.mark.regression
def test_unsharded_collection_is_unchanged_by_the_plugin():
    """V11: from the repository root, where the root conftest loads the plugin,
    an unsharded session collects exactly what it collects without it."""
    target = "backend/tests/unit/test_benchmark_gate.py"
    env = {k: v for k, v in os.environ.items() if k not in (ENV_SHARD, ENV_REPORT)}

    def collect(*extra: str) -> list[str]:
        result = subprocess.run(
            [sys.executable, "-m", "pytest", "--collect-only", "-q", "-p", "no:cov"]
            + ["-p", "no:cacheprovider", *extra, target],
            cwd=REPO_ROOT,
            env=env,
            capture_output=True,
            text=True,
            timeout=300,
        )
        assert result.returncode == 0, result.stdout + result.stderr
        return [line for line in result.stdout.splitlines() if "::" in line]

    with_plugin = collect()
    without = collect("-p", f"no:{PLUGIN}")
    assert without, "collected nothing: the comparison would be vacuous"
    assert with_plugin == without, (
        f"collected {len(with_plugin)}, expected {len(without)}"
    )


def test_the_root_conftest_loads_the_plugin(tmp_path):
    """The comparison above means something only if the plugin was loaded."""
    report = tmp_path / "shard.json"
    env = {k: v for k, v in os.environ.items() if k not in (ENV_SHARD, ENV_REPORT)}
    env[ENV_SHARD] = "1/2"
    env[ENV_REPORT] = str(report)
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", "-q", "-p", "no:cov"]
        + ["-p", "no:cacheprovider", "backend/tests/unit/test_benchmark_gate.py"],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    doc = json.loads(report.read_text())
    assert doc["suite"] == "backend/tests/unit/test_benchmark_gate.py"
    assert 0 < len(doc["selected"]) < len(doc["collected"])


def _report_of(pytester, monkeypatch, tmp_path, *args: str) -> dict:
    report = tmp_path / "shard.json"
    _shard(monkeypatch, "1/1", report)
    pytester.inline_run("-p", PLUGIN, *args)
    return json.loads(report.read_text())


@pytest.mark.regression
@pytest.mark.parametrize(
    "args, deselected",
    [
        ((), 0),
        # pytest drops a path given twice before it collects it: no drop.
        (("test_ten.py", "test_ten.py"), 0),
        (("-k", "not 3"), 1),
        (("--deselect", "test_ten.py::test_n[0]"), 1),
    ],
)
def test_ordinary_sessions_announce_every_item_they_lose(
    pytester, monkeypatch, tmp_path, args, deselected
):
    pytester.makepyfile(
        test_ten="""
        import pytest

        @pytest.mark.parametrize("n", range(10))
        def test_n(n):
            pass
        """
    )
    doc = _report_of(pytester, monkeypatch, tmp_path, "-p", "no:cacheprovider", *args)
    assert (doc["unannounced"], doc["fabricated"]) == ([], [])
    assert doc["pre_deselected"] == deselected
    assert len(doc["collected"]) == 10 - deselected


@pytest.mark.regression
def test_a_conftest_that_drops_items_is_counted(pytester, monkeypatch, tmp_path):
    pytester.makepyfile(
        test_ten="""
        import pytest

        @pytest.mark.parametrize("n", range(10))
        def test_n(n):
            pass
        """
    )
    pytester.makeconftest(
        """
        def pytest_collection_modifyitems(items):
            del items[-3:]
        """
    )
    doc = _report_of(pytester, monkeypatch, tmp_path, "-p", "no:cacheprovider")
    assert len(doc["collected"]) == 7
    assert doc["unannounced"] == [f"test_ten.py::test_n[{n}]" for n in (7, 8, 9)]
    assert (doc["fabricated"], doc["pre_deselected"]) == ([], 0)


# Each conftest drops test_n[9] without pytest_deselected and appends an item
# pytest never collected, so the count of items is unchanged.
SUBSTITUTES = {
    "a new node id": """
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
        """,
    "the dropped node id": """
        import pytest

        def _body(n):
            pass

        def pytest_collection_modifyitems(items):
            dropped = items.pop()
            items.append(
                pytest.Function.from_parent(
                    dropped.parent,
                    name=dropped.name,
                    callspec=dropped.callspec,
                    callobj=_body,
                    originalname=dropped.originalname,
                )
            )
        """,
}


@pytest.mark.regression
@pytest.mark.parametrize("case", sorted(SUBSTITUTES))
def test_a_fabricated_substitute_is_recorded(pytester, monkeypatch, tmp_path, case):
    pytester.makepyfile(
        test_ten="""
        import pytest

        @pytest.mark.parametrize("n", range(10))
        def test_n(n):
            pass
        """
    )
    pytester.makeconftest(SUBSTITUTES[case])
    doc = _report_of(pytester, monkeypatch, tmp_path, "-p", "no:cacheprovider")
    assert len(doc["collected"]) == 10, "the count alone sees nothing"
    assert doc["unannounced"] == ["test_ten.py::test_n[9]"]
    fabricated = (
        "test_ten.py::test_fabricated"
        if case == "a new node id"
        else "test_ten.py::test_n[9]"
    )
    assert doc["fabricated"] == [fabricated]


@pytest.mark.regression
def test_an_added_item_is_recorded(pytester, monkeypatch, tmp_path):
    pytester.makepyfile(test_one="def test_a(): pass")
    pytester.makeconftest(
        """
        import pytest

        def _body():
            pass

        def pytest_collection_modifyitems(items):
            items.append(
                pytest.Function.from_parent(items[0].parent, name="test_extra", callobj=_body)
            )
        """
    )
    doc = _report_of(pytester, monkeypatch, tmp_path, "-p", "no:cacheprovider")
    assert (doc["unannounced"], doc["fabricated"]) == ([], ["test_one.py::test_extra"])


@pytest.mark.regression
def test_a_disabled_plugin_written_without_a_space_is_recorded(
    pytester, monkeypatch, tmp_path
):
    pytester.makepyfile(test_one="def test_a(): pass")
    doc = _report_of(pytester, monkeypatch, tmp_path, "-pno:cacheprovider")
    assert doc["disabled_plugins"] == ["cacheprovider"]


@pytest.mark.regression
def test_disabled_plugins_are_recorded_from_every_source(
    pytester, monkeypatch, tmp_path
):
    pytester.makepyfile(test_one="def test_a(): pass")
    pytester.makeini("[pytest]\naddopts = -p no:doctest\n")
    monkeypatch.setenv("PYTEST_ADDOPTS", "-p no:cacheprovider")
    doc = _report_of(pytester, monkeypatch, tmp_path, "-p", "no:stepwise")
    assert doc["disabled_plugins"] == ["cacheprovider", "doctest", "stepwise"]
    assert doc["autoload_disabled"] is False


BAD_SPECS = [
    "0/4",
    "5/4",
    "1/0",
    "a/b",
    "1/4 ",
    " 1/4",
    "1 / 4",
    "4",
    "2/4/1",
    "1234/9999",
    "-1/4",
    "1/4\n",
]


@pytest.mark.regression
@pytest.mark.parametrize("spec", BAD_SPECS)
def test_a_bad_spec_is_a_usage_error(pytester, monkeypatch, tmp_path, spec):
    pytester.makepyfile(test_one="def test_a(): pass")
    report = tmp_path / "shard.json"
    _shard(monkeypatch, spec, report)
    result = pytester.runpytest("-p", PLUGIN)
    assert result.ret == pytest.ExitCode.USAGE_ERROR, result.stdout.str()
    assert ENV_SHARD in result.stderr.str()
    assert not report.exists()


def _report_cases(tmp_path: Path):
    existing = tmp_path / "existing.json"
    existing.write_text("theirs")
    return {
        "missing": None,
        "empty": "",
        "relative": "shard.json",
        "existing": existing,
        "no such directory": tmp_path / "absent" / "shard.json",
    }


@pytest.mark.regression
@pytest.mark.parametrize(
    "case", ["missing", "empty", "relative", "existing", "no such directory"]
)
def test_a_bad_report_path_is_a_usage_error(pytester, monkeypatch, tmp_path, case):
    pytester.makepyfile(test_one="def test_a(): pass")
    path = _report_cases(tmp_path)[case]
    _shard(monkeypatch, "1/1", path)
    result = pytester.runpytest("-p", PLUGIN)
    assert result.ret == pytest.ExitCode.USAGE_ERROR, result.stdout.str()
    assert ENV_REPORT in result.stderr.str()
    assert (tmp_path / "existing.json").read_text() == "theirs"
    assert not (pytester.path / "shard.json").exists()


@pytest.mark.regression
@pytest.mark.parametrize("spec", [None, ""])
def test_a_report_path_without_a_spec_is_a_usage_error(
    pytester, monkeypatch, tmp_path, spec
):
    pytester.makepyfile(test_one="def test_a(): pass")
    if spec is not None:
        monkeypatch.setenv(ENV_SHARD, spec)
    monkeypatch.setenv(ENV_REPORT, str(tmp_path / "shard.json"))
    result = pytester.runpytest("-p", PLUGIN)
    assert result.ret == pytest.ExitCode.USAGE_ERROR, result.stdout.str()
    assert "lost its spec" in result.stderr.str()


@pytest.mark.regression
def test_a_second_session_cannot_overwrite_the_first_report(
    pytester, monkeypatch, tmp_path
):
    pytester.makepyfile(test_one="def test_a(): pass")
    report = tmp_path / "shard.json"
    _shard(monkeypatch, "1/1", report)
    assert pytester.runpytest("-p", PLUGIN).ret == pytest.ExitCode.OK
    first = report.read_text()
    second = pytester.runpytest("-p", PLUGIN)
    assert second.ret == pytest.ExitCode.USAGE_ERROR
    assert "already exists" in second.stderr.str()
    assert report.read_text() == first


# A test that starts pytest again with a copy of os.environ, as the unit suite's
# collection counts and conftest checks do.
CHILD_RUN = """
import os
import subprocess
import sys

def test_starts_pytest_again():
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-p", "backend.tests.shard",
         "-p", "no:cacheprovider", "-q", "child"],
        env={**os.environ, "PYTHONPATH": %r},
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "4 passed" in result.stdout, result.stdout
"""


@pytest.mark.regression
def test_a_process_the_session_starts_is_not_sharded(pytester, monkeypatch, tmp_path):
    """#1895: in rc-evidence's unit shards, a test that started pytest with a
    copy of os.environ failed: the child inherited the shard's variables,
    found the session's report already created, and refused (exit 4)."""
    pytester.makepyfile(test_parent=CHILD_RUN % str(REPO_ROOT))
    child = pytester.mkdir("child")
    (child / "test_four.py").write_text(
        "import pytest\n\n"
        "@pytest.mark.parametrize('n', range(4))\n"
        "def test_n(n):\n"
        "    pass\n"
    )
    report = tmp_path / "shard.json"
    _shard(monkeypatch, "1/1", report)

    reprec = pytester.inline_run(
        "-p", PLUGIN, "-p", "no:cacheprovider", "test_parent.py"
    )

    reprec.assertoutcome(passed=1)
    ran = json.loads(report.read_text())["ran"]
    assert ran == [
        {
            "nodeid": "test_parent.py::test_starts_pytest_again",
            "outcome": "passed",
            "skip_reason": None,
        }
    ]
    # Put back when the session ended: an in-process caller gets its own.
    assert (os.environ[ENV_SHARD], os.environ[ENV_REPORT]) == ("1/1", str(report))


@pytest.mark.regression
def test_an_empty_shard_is_a_usage_error(pytester, monkeypatch, tmp_path):
    pytester.makepyfile(test_one="def test_only(): pass")
    nodeid = "test_one.py::test_only"
    empty = next(i for i in (1, 2) if shard_of(nodeid, 2) != i)
    report = tmp_path / "shard.json"
    _shard(monkeypatch, f"{empty}/2", report)
    result = pytester.runpytest("-p", PLUGIN)
    assert result.ret == pytest.ExitCode.USAGE_ERROR, result.stdout.str()
    assert f"{empty}/2 selected none of the 1 collected" in result.stderr.str()
    # The claimed report stays empty, which the checker refuses.
    assert report.read_text() == ""


def test_the_shard_of_a_node_id_is_pinned():
    # sha256 of the node id, not hash(): the same in every process whatever
    # PYTHONHASHSEED is. Changing the function moves every test to a new shard.
    assignments = {
        "backend/tests/unit/test_x.py::test_y": 1,
        "a.py::t[::1-True]": 2,
        "a.py::t[1]": 4,
        "a.py::t[2]": 2,
        "a.py::t[3]": 2,
        "a.py::t[4]": 3,
    }
    assert {n: shard_of(n, 4) for n in assignments} == assignments
