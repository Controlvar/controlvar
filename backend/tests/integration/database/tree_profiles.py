"""Run this checkout as either profile, in a subprocess.

Not a test module (pytest does not collect it); imported by
``test_autogenerate_is_empty.py`` and ``test_profile_transitions.py``, which
both need to ask "what does a **core** build do to this database?" without a
core checkout to hand, and by ``test_alembic_branch_revisions.py``, which needs
a tree alembic may write a revision file into (:func:`full_tree`).

A core build is defined by a tree with no ``modules/`` directory
(``scripts/core_build.sh`` makes one by deleting it), so :func:`core_tree`
builds the smallest thing that answers to that description: ``backend/`` copied
into a fresh directory, plus the ``sitecustomize.py`` that seals it.  Three
properties matter; the last two come from it being a real directory rather
than a symlink:

* ``import modules`` fails, so ``modules_loader`` reports the core profile and
  ``Base.metadata`` holds the core tables only.  The copy does not give this on
  its own -- an editable install resolves ``modules`` back into the checkout --
  so ``core_tree_guard.py`` enforces it;
* ``alembic.ini``'s second version location resolves inside the copy, where it
  does not exist, so alembic sees **one** head -- the core chain's;
* ``db/bootstrap.py``'s ``_REPO_ROOT`` is ``Path(__file__).resolve()``-derived,
  and a symlinked ``backend/`` would resolve straight back into the real
  checkout and pick up the modules' version location again.

Everything runs in a subprocess, as a container does: a revision module reads
``POSTGRES_SCHEMA`` once, when alembic imports it, and the profile is decided
by an interpreter's ``sys.path``.
"""

from __future__ import annotations

import os
import pathlib
import re
import shutil
import subprocess
import sys
from typing import Optional

from backend.app.db import bootstrap

REPO_ROOT = bootstrap._REPO_ROOT

#: The two profiles, spelled as ``scripts/core_build.sh --profile`` spells them.
CORE = "core"
FULL = "full"
PROFILES = (CORE, FULL)

#: ``-c`` path, built from parts: a single string literal spelling a path under
#: the modules would be a core->module crossing for
#: ``backend/tests/smoke/test_core_boundary.py``.
_ALEMBIC_INI_PARTS = ("backend", "app", "db", "alembic.ini")


def alembic_ini(tree: pathlib.Path) -> pathlib.Path:
    return tree.joinpath(*_ALEMBIC_INI_PARTS)


def _copy_package(root: pathlib.Path, *parts: str) -> None:
    """Copy ``REPO_ROOT/<parts>/app`` and its package ``__init__.py`` files."""
    package = root.joinpath(*parts)
    package.mkdir(parents=True, exist_ok=True)
    for depth in range(1, len(parts) + 1):
        init = pathlib.Path(*parts[:depth]) / "__init__.py"
        if (REPO_ROOT / init).is_file():
            shutil.copy2(REPO_ROOT / init, root / init)
    shutil.copytree(
        REPO_ROOT.joinpath(*parts, "app"),
        package / "app",
        ignore=shutil.ignore_patterns("__pycache__"),
    )


def core_tree(destination) -> pathlib.Path:
    """A checkout of this tree with no modules package, rooted at *destination*.

    The copy alone does not make ``import modules`` fail: an editable install's
    finder maps ``modules`` (and any ``backend.*`` child the copy lacks) back
    into the checkout.  ``core_tree_guard.py``, installed here as
    ``sitecustomize.py``, removes that route in every interpreter started in
    the tree, or stops the interpreter if it cannot (#47).
    """
    root = pathlib.Path(destination)
    _copy_package(root, "backend")
    shutil.copy2(
        pathlib.Path(__file__).with_name("core_tree_guard.py"),
        root / "sitecustomize.py",
    )
    assert not (root / "modules").exists()
    return root


def full_tree(destination) -> pathlib.Path:
    """A copy of this tree *with* the modules package, rooted at *destination*.

    For the probes that make alembic **write** a revision file.  ``alembic
    revision`` writes next to the head it extends, which for this repository
    means one of the two tracked ``versions/`` directories: an interrupted run
    leaves a third alembic head in the checkout, and every later ``upgrade
    heads`` or bootstrap in that tree applies it.  ``autogenerate()`` below
    avoids that with a throwaway ``--version-path``, but a probe that is *about*
    where alembic decides to put the file cannot -- and a throwaway config
    spells the version locations absolutely, which is exactly the spelling those
    probes exist to check.  So they run in a copy instead.
    """
    root = pathlib.Path(destination)
    _copy_package(root, "backend")
    _copy_package(root, "modules", "backend")
    assert alembic_ini(root).is_file()
    return root


def tree_for(profile: str, tmp_path) -> pathlib.Path:
    """The repository itself for ``full``; a modules-free copy for ``core``."""
    if profile == FULL:
        return REPO_ROOT
    return core_tree(pathlib.Path(tmp_path) / "core-checkout")


def run(
    tree: pathlib.Path,
    argv: list[str],
    schema: str,
    extra_env: Optional[dict] = None,
) -> subprocess.CompletedProcess:
    """Run ``python <argv>`` from *tree*, against *schema*.

    ``APP_ENV``/``TESTING``/``ENVIRONMENT`` are dropped so the subprocess is a
    deployment rather than a test process, and ``POSTGRES_SCHEMA`` names the
    scratch schema, exactly as ``docker-entrypoint.sh`` and the ECS migration
    task set it.  Everything else (``POSTGRES_DB`` above all) is inherited.
    *extra_env* puts some of it back, for the tests that are *about* a
    developer's shell.
    """
    return subprocess.run(
        [sys.executable, *argv],
        cwd=str(tree),
        env=_environment(tree, schema, extra_env),
        capture_output=True,
        text=True,
        timeout=900,
    )


def _environment(
    tree: pathlib.Path, schema: str, extra_env: Optional[dict] = None
) -> dict:
    """The environment :func:`run` and :func:`serve` give their subprocess."""
    env = {
        k: v
        for k, v in os.environ.items()
        if k not in ("APP_ENV", "TESTING", "ENVIRONMENT")
    }
    env.update(
        {
            "PYTHONPATH": str(tree),
            "POSTGRES_SCHEMA": schema,
            "PYTHONDONTWRITEBYTECODE": "1",
        }
    )
    # A deployment sets ENVIRONMENT or a real first-administrator password;
    # with neither, the bootstrap refuses the class-default "admin".
    env.setdefault("FIRST_SUPERUSER_PASSWORD", "Subprocess-Deployment-Passw0rd")
    env.update(extra_env or {})
    # A key set to None in *extra_env* is removed rather than set.
    return {k: v for k, v in env.items() if v is not None}


def serve(
    tree: pathlib.Path,
    schema: str,
    port: int,
    log: pathlib.Path,
    extra_env: Optional[dict] = None,
) -> subprocess.Popen:
    """Start *tree*'s API with uvicorn on ``127.0.0.1:<port>``, against *schema*.

    What ``docker-entrypoint.sh`` hands over to with ``RUN_MIGRATIONS=false``:
    the server, and nothing run against the database first.  Output goes to
    *log* (a file, so a chatty server cannot fill a pipe and stall).  The
    caller stops the process.
    """
    with open(log, "wb") as out:
        return subprocess.Popen(
            [
                sys.executable,
                "-m",
                "uvicorn",
                "backend.app.main:app",
                "--host",
                "127.0.0.1",
                "--port",
                str(port),
                "--workers",
                "1",
                "--no-server-header",
            ],
            cwd=str(tree),
            env=_environment(tree, schema, extra_env),
            stdout=out,
            stderr=subprocess.STDOUT,
        )


def bootstrap_schema(
    tree: pathlib.Path, schema: str, extra_env: Optional[dict] = None
) -> subprocess.CompletedProcess:
    return run(tree, ["-m", "backend.app.db.bootstrap"], schema, extra_env)


def alembic(
    tree: pathlib.Path, schema: str, *args: str, extra_env: Optional[dict] = None
) -> subprocess.CompletedProcess:
    return run(
        tree,
        ["-m", "alembic", "-c", str(alembic_ini(tree)), *args],
        schema,
        extra_env,
    )


# ---------------------------------------------------------------------------
# `alembic revision --autogenerate` without writing into the checkout
# ---------------------------------------------------------------------------
_VERSION_LOCATIONS_BLOCK = re.compile(r"(?m)^version_locations =\n(?:[ \t]+\S.*\n)+")
_SCRIPT_LOCATION = re.compile(r"(?m)^script_location = .*$")


def _throwaway_config(tree: pathlib.Path, version_path: pathlib.Path) -> pathlib.Path:
    """A copy of *tree*'s alembic.ini with *version_path* as an extra location.

    ``alembic revision`` writes next to the head it extends, which would drop a
    revision file into the checkout.  Adding a temporary directory to
    ``version_locations`` and passing it as ``--version-path`` sends the file
    there instead; everything else -- ``script_location``, the logging
    configuration ``env.py`` reads, the real version locations -- is the
    repository's own file.
    """
    real = alembic_ini(tree)
    text = real.read_text(encoding="utf-8")
    # Both paths in the file are anchored at `%(here)s`, the directory holding
    # the config; this copy lives somewhere else, so spell them absolutely.
    migrations = tree / "backend" / "app" / "db" / "migrations"
    text = _SCRIPT_LOCATION.sub(f"script_location = {migrations}", text)
    locations = [str(migrations / "versions")]
    modules_versions = tree.joinpath(
        "modules", "backend", "app", "db", "migrations", "versions"
    )
    if modules_versions.is_dir():
        locations.append(str(modules_versions))
    locations.append(str(version_path))
    text = _VERSION_LOCATIONS_BLOCK.sub(
        "version_locations =\n" + "".join(f"    {p}\n" for p in locations), text
    )
    config = version_path.parent / "alembic.ini"
    config.write_text(text, encoding="utf-8")
    return config


class Autogenerated:
    """The result of one ``alembic revision --autogenerate``."""

    def __init__(self, process: subprocess.CompletedProcess, body: Optional[str]):
        self.process = process
        #: The generated ``upgrade()`` body, or ``None`` when nothing was written.
        self.body = body

    @property
    def operations(self) -> list[str]:
        """The ``op.*`` calls the generated ``upgrade()`` would run.

        ``op.f(...)`` is alembic's naming-convention wrapper, not an operation.
        """
        if not self.body:
            return []
        return [op for op in re.findall(r"op\.[a-z_]+\(", self.body) if op != "op.f("]

    def describe(self) -> str:
        counts: dict[str, int] = {}
        for op in self.operations:
            counts[op] = counts.get(op, 0) + 1
        summary = ", ".join(f"{op}) x{n}" for op, n in sorted(counts.items()))
        return (
            f"exit={self.process.returncode} operations={summary or 'none'}\n"
            f"--- upgrade() ---\n{(self.body or '').strip()[:4000]}\n"
            f"--- stdout ---\n{self.process.stdout[-2000:]}\n"
            f"--- stderr ---\n{self.process.stderr[-3000:]}"
        )


def autogenerate(
    tree: pathlib.Path,
    schema: str,
    tmp_path,
    head: str = "",
    extra_env: Optional[dict] = None,
) -> Autogenerated:
    """``alembic revision --autogenerate`` against *schema*, written to *tmp_path*."""
    # Resolved: env.py resolves the configured version locations, and alembic
    # compares --version-path against them by equality (on macOS /var is a
    # symlink to /private/var, so an unresolved tmp path never matches).
    version_path = (pathlib.Path(tmp_path) / "autogen" / "versions").resolve()
    version_path.mkdir(parents=True, exist_ok=True)
    config = _throwaway_config(tree, version_path)

    argv = [
        "-m",
        "alembic",
        "-c",
        str(config),
        "revision",
        "--autogenerate",
        "--version-path",
        str(version_path),
        "-m",
        "autogenerate probe",
    ]
    if head:
        argv += ["--head", head]
    process = run(tree, argv, schema, extra_env)

    written = sorted(version_path.glob("*.py"))
    body = None
    if written:
        text = written[0].read_text(encoding="utf-8")
        body = text.split("def upgrade")[1].split("def downgrade")[0]
    return Autogenerated(process, body)


#: The core chain's head, for the ``--head`` a full checkout's two heads make
#: ``alembic revision`` insist on.  A literal rather than a lookup: reading it
#: back from the version directories would answer differently the moment
#: another process generates a revision into them (``alembic revision`` writes
#: real files, and a developer generating one does exactly that).
#: ``backend/tests/unit/db/test_alembic_plan.py`` pins it against the files.
CORE_HEAD = "4c2c09a2b3e8"
