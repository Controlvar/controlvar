"""Database errors are logged without the statement's parameter values.

SQLAlchemy appends "[parameters: ...]" to the text of a failed statement
unless the engine is built with ``hide_parameters=True``. Every engine the
application builds passes it, so the listing is left out of the error text
and out of any log line that carries it.

This covers SQLAlchemy's own listing only. It does not change what the
database server puts in its own error message.
"""

import ast
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import StatementError

REPO_ROOT = Path(__file__).resolve().parents[4]
ENGINE_FACTORIES = {"create_engine", "create_async_engine"}
SCANNED = (REPO_ROOT / "backend" / "app", REPO_ROOT / "modules" / "backend" / "app")

# The statement fails on a missing table; the parameter value is what must
# not appear in the rendered error.
_VALUE = "value-that-must-not-be-logged"
_SQL = text("SELECT * FROM no_such_table WHERE name = :name")


def _factory_names(tree: ast.AST) -> set:
    """The factory names plus any local alias from `import create_engine as x`."""
    names = set(ENGINE_FACTORIES)
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            names.update(
                alias.asname
                for alias in node.names
                if alias.name in ENGINE_FACTORIES and alias.asname
            )
    return names


def _factory_calls(root: Path):
    for path in sorted(root.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        names = _factory_names(tree)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", "")
            if name in names:
                yield path, node


def _hides_parameters(call: ast.Call) -> bool:
    return any(
        kw.arg == "hide_parameters"
        and isinstance(kw.value, ast.Constant)
        and kw.value.value is True
        for kw in call.keywords
    )


def _rendered_error(engine) -> str:
    with engine.connect() as conn:
        with pytest.raises(StatementError) as caught:
            conn.execute(_SQL, {"name": _VALUE})
    return str(caught.value)


@pytest.mark.regression
def test_every_engine_in_the_application_hides_parameters():
    sites = [
        (path, node)
        for root in SCANNED
        if root.is_dir()
        for path, node in _factory_calls(root)
    ]
    # session.py and bootstrap.py always; the rotation script when modules/ is present.
    assert len(sites) >= 2, (
        "no engine factory call found: the scan is looking in the wrong place"
    )
    missing = [
        f"{path.relative_to(REPO_ROOT)}:{node.lineno}"
        for path, node in sites
        if not _hides_parameters(node)
    ]
    assert missing == [], f"engine built without hide_parameters=True at {missing}"


def test_scan_finds_a_call_that_lacks_the_argument():
    """Control: the scan reports an engine call that does not pass it."""
    call = ast.parse("create_engine(url, pool_pre_ping=True)").body[0].value
    assert not _hides_parameters(call)
    call = ast.parse("sa.create_engine(url, hide_parameters=True)").body[0].value
    assert _hides_parameters(call)


def test_scan_follows_an_aliased_import(tmp_path):
    """An engine built through `import create_engine as x` is still found."""
    (tmp_path / "aliased.py").write_text(
        "from sqlalchemy import create_engine as _ce\n"
        "bad = _ce('sqlite://')\n"
        "good = _ce('sqlite://', hide_parameters=True)\n",
        encoding="utf-8",
    )
    calls = list(_factory_calls(tmp_path))
    assert [_hides_parameters(node) for _, node in calls] == [False, True]


@pytest.mark.regression
def test_bootstrap_engine_error_has_no_parameter_listing(monkeypatch):
    from backend.app.db import bootstrap

    monkeypatch.setattr(bootstrap, "database_url", lambda: "sqlite://")
    rendered = _rendered_error(bootstrap.make_engine())
    assert "[parameters" not in rendered
    assert _VALUE not in rendered


def test_an_engine_without_the_argument_does_show_the_listing():
    """Control: the same statement on a default engine renders the listing."""
    rendered = _rendered_error(create_engine("sqlite://"))
    assert "[parameters" in rendered
    assert _VALUE in rendered


@pytest.mark.regression
def test_application_engine_hides_parameters():
    from backend.app.db import session

    assert session.engine.hide_parameters is True
