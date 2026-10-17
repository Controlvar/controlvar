"""The tree a video is recorded from: ``git archive <ref>``, never the working tree.

1. ``git archive <commit> | tar -x`` into ``<work root>/tree-<profile>``: only
   what the ref tracks, so a developer's uncommitted or untracked files do not
   reach the video. The only git commands the tool runs are ``rev-parse`` and
   ``archive``; neither writes. A ref that is not here is refused with the
   fetch command to run, which the tool does not run itself.
2. Core: ``modules/`` is deleted (through ``WorkRoot.remove``).
3. ``frontend/node_modules`` is cloned from the checkout the tool runs from
   (``cp -c``: copy-on-write on APFS, so it costs no space, and a build tool
   that wrote into one could not change the checkout's file).
4. ``npm run build`` with ``guard.dashboard_settings``, then core_build.sh's two
   bundle checks, copied: no module route in a core bundle and the workspaces
   stub present (or, for full, the module route present).

The manifest records the Next.js version actually built with beside the one
the ref's lockfile pins: a checkout's ``node_modules`` can be older than the
ref being recorded.

A demo app a video records (``config.APPS``) gets a tree of its own:
``git archive <commit> -- sdk/react demo/<app>`` into ``<work root>/app-<app>``
(the app consumes the React SDK from source), its ``node_modules`` cloned
from the checkout's ``demo/<app>``, plus a link ``node_modules`` at the tree's
root to it, so the SDK's sources find React. Then ``next build`` (the copy's
own ``next``, never an npm script) with ``guard.app_settings``, and a check
that the bundle names the tool's API and no developer address. It is served
by ``next start -p <port> -H 127.0.0.1`` (``app_start_argv``); ``next dev``
and ``npm start`` are refused (``refuse_app_argv``).

Nothing here needs ``.git`` in the tree it builds: ``backend/tests/unit/showcase``
builds a tree from a plain tar stream in a directory with none.
"""

from __future__ import annotations

import json
import re
import subprocess
import tarfile
from dataclasses import dataclass
from pathlib import Path
from typing import IO, Any, Callable, Dict, List, Mapping, Optional, Sequence

from showcase.capture import guard

#: core_build.sh's step_frontend, copied: a core bundle names none of these.
MODULE_ROUTES = re.compile(
    rb"/api/v1/workspaces|/api/v1/rbac/|/api/v1/hipaa|/api/v1/warehouse/"
)
#: ...and has the workspaces stub page, whose description this is.
CORE_STUB_TEXT = b"Separate teams into workspaces"
FULL_ROUTE = re.compile(rb"/api/v1/workspaces")
_SHA = re.compile(r"^[0-9a-f]{40}$")


class BuildError(RuntimeError):
    pass


@dataclass(frozen=True)
class Source:
    """The commit a video is recorded from."""

    ref: str
    commit: str
    version: str


def resolve_ref(repo: Path, ref: str, *, run=subprocess.run) -> str:
    """The commit *ref* names in *repo*; Refused (with the fetch to run) when absent."""
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,99}", ref):
        raise guard.Refused(f"--ref {ref!r} is not a ref")
    done = run(
        [
            "git",
            "-C",
            str(repo),
            "rev-parse",
            "--verify",
            "--quiet",
            f"{ref}^{{commit}}",
        ],
        capture_output=True,
        text=True,
        check=False,
        env=guard.tool_env(),
    )
    commit = done.stdout.strip()
    if done.returncode != 0 or not _SHA.match(commit):
        raise guard.Refused(
            f"{ref} is not in {repo}. The tool runs no git command that writes;"
            f" fetch it first: git -C {repo} fetch --tags origin"
        )
    return commit


def extract(stream: IO[bytes], tree: Path) -> None:
    """Unpack a tar stream into *tree*, which must not exist yet."""
    if tree.exists():
        raise BuildError(f"{tree} exists already")
    tree.mkdir(parents=True)
    with tarfile.open(fileobj=stream, mode="r|") as archive:
        archive.extractall(tree, filter="data")


def archive(repo: Path, commit: str, tree: Path, paths: Sequence[str] = ()) -> None:
    """``git archive <commit> [-- paths]`` of *repo*, unpacked into *tree*."""
    process = subprocess.Popen(
        ["git", "-C", str(repo), "archive", "--format=tar", commit]
        + (["--", *paths] if paths else []),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=guard.tool_env(),
    )
    assert process.stdout is not None
    try:
        extract(process.stdout, tree)
    finally:
        process.stdout.close()
        stderr = (
            process.stderr.read().decode(errors="replace") if process.stderr else ""
        )
        status = process.wait()
    if status != 0:
        raise BuildError(f"git archive {commit} exited {status}: {stderr.strip()}")


def strip_modules(tree: Path, profile: str, work: guard.WorkRoot) -> None:
    modules = tree / "modules"
    if profile == "full":
        if not modules.is_dir():
            raise BuildError("--profile full, but the ref has no modules/ directory")
        return
    if modules.exists():
        work.remove(modules)


#: What ``cp`` says when it has no ``-c`` (GNU: "invalid option -- 'c'"; a BSD
#: without clonefile: "illegal option -- c").
_NO_CLONE_FLAG = re.compile(r"(?:invalid|illegal|unrecognized) option.*\bc\b")


def copy_tree(source: Path, target: Path, *, run=subprocess.run) -> Any:
    """``cp -cR`` (copy-on-write on APFS: no space, and a build that wrote into
    the copy could not change the checkout's file); where ``cp`` has no ``-c``
    (Linux), ``cp -R``. Any other failure is returned as it is, never retried.
    """
    done = run(
        ["cp", "-cR", str(source), str(target)],
        capture_output=True,
        text=True,
        env=guard.tool_env(),
    )
    if done.returncode != 0 and _NO_CLONE_FLAG.search(done.stderr or ""):
        done = run(
            ["cp", "-R", str(source), str(target)],
            capture_output=True,
            text=True,
            env=guard.tool_env(),
        )
    return done


def clone_node_modules(source: Path, tree: Path, *, run=subprocess.run) -> None:
    """*source* to ``tree/frontend/node_modules`` (``copy_tree``)."""
    if not (source / "next" / "package.json").is_file():
        raise BuildError(
            f"{source} has no Next.js; run `npm ci` in frontend/ of the checkout"
            " the tool runs from"
        )
    target = tree / "frontend" / "node_modules"
    if target.exists():
        raise BuildError(f"{target} exists already")
    done = copy_tree(source, target, run=run)
    if done.returncode != 0:
        raise BuildError(f"copying node_modules failed: {done.stderr.strip()}")


def bundle_check(static: Path, profile: str) -> str:
    """core_build.sh's bundle checks on ``out/_next/static``; the line it logs."""
    if not static.is_dir():
        raise BuildError("next build produced no out/_next/static")
    found_route: List[str] = []
    stub = False
    full_route = False
    for path in sorted(static.rglob("*")):
        if not path.is_file():
            continue
        data = path.read_bytes()
        if MODULE_ROUTES.search(data):
            found_route.append(path.relative_to(static).as_posix())
        if CORE_STUB_TEXT in data:
            stub = True
        if FULL_ROUTE.search(data):
            full_route = True
    if profile == "core":
        if found_route:
            raise BuildError(
                "the core bundle references module routes: "
                + ", ".join(found_route[:5])
            )
        if not stub:
            raise BuildError("the core bundle has no workspaces stub page")
        return "core bundle: no module route, workspaces stub present"
    if not full_route:
        raise BuildError("the full bundle does not reference /api/v1/workspaces")
    return "full bundle: module routes present"


def next_versions(tree: Path) -> Dict[str, Optional[str]]:
    """The Next.js the build used, and the one the ref's lockfile pins."""
    used = None
    pinned = None
    package = tree / "frontend" / "node_modules" / "next" / "package.json"
    if package.is_file():
        used = json.loads(package.read_text(encoding="utf-8")).get("version")
    lock = tree / "frontend" / "package-lock.json"
    if lock.is_file():
        packages = json.loads(lock.read_text(encoding="utf-8")).get("packages", {})
        pinned = (packages.get("node_modules/next") or {}).get("version")
    return {"next_used": used, "next_lockfile": pinned}


def build_dashboard(
    tree: Path,
    env: Mapping[str, str],
    log: Path,
    *,
    spawn: Callable[..., int],
) -> None:
    status = spawn(["npm", "run", "build"], cwd=tree / "frontend", env=env, log=log)
    if status != 0:
        raise BuildError(f"npm run build exited {status}; see {log}")


# ---------------------------------------------------------------------------
# The demo apps
# ---------------------------------------------------------------------------


def app_paths(app: str) -> List[str]:
    """What an app's tree holds: the app, and the SDK it builds from source."""
    return ["sdk/react", f"demo/{app}"]


def app_dir(tree: Path, app: str) -> Path:
    return tree / "demo" / app


def next_bin(tree: Path, app: str) -> Path:
    """The copy's own Next.js command line (``node <this> build``)."""
    return app_dir(tree, app) / "node_modules" / "next" / "dist" / "bin" / "next"


def clone_app_node_modules(
    repo: Path, tree: Path, app: str, *, run=subprocess.run
) -> None:
    """``demo/<app>/node_modules`` from the checkout (``copy_tree``), and the root link.

    The link is ``<tree>/node_modules -> demo/<app>/node_modules``: the SDK's
    sources under ``sdk/react`` resolve ``react`` from the nearest
    ``node_modules`` above them, and without it ``next build`` fails its type
    check (TS2307, measured).
    """
    source = repo / "demo" / app / "node_modules"
    if not (source / "next" / "package.json").is_file():
        raise guard.Refused(
            f"{source} has no Next.js; run `npm ci` in demo/{app} of the checkout"
            " the tool runs from"
        )
    target = app_dir(tree, app) / "node_modules"
    if target.exists() or (tree / "node_modules").exists():
        raise BuildError(f"{target} or the root node_modules exists already")
    done = copy_tree(source, target, run=run)
    if done.returncode != 0:
        raise BuildError(
            f"copying demo/{app}/node_modules failed: {done.stderr.strip()}"
        )
    (tree / "node_modules").symlink_to(Path("demo") / app / "node_modules")


#: Files that would put a developer's settings into an app's build.
ENV_FILES = (".env", ".env.local", ".env.production", ".env.production.local")


def refuse_env_files(tree: Path, app: str) -> None:
    """An app's build reads ``.env*`` beside it; the tool gives it its settings."""
    found = [name for name in ENV_FILES if (app_dir(tree, app) / name).exists()]
    if found:
        raise BuildError(
            f"demo/{app}/{found[0]} is in the build tree; the app is built with"
            " the tool's settings only"
        )


def app_build_argv(tree: Path, app: str) -> List[str]:
    return ["node", str(next_bin(tree, app)), "build"]


def app_start_argv(tree: Path, app: str, port: int) -> List[str]:
    return [
        "node",
        str(next_bin(tree, app)),
        "start",
        "-p",
        str(port),
        "-H",
        "127.0.0.1",
    ]


def refuse_app_argv(argv: List[str], tree: Path, app: str, port: int) -> None:
    """Only ``next start`` of the built copy, on its port, on loopback.

    ``next dev`` serves a different bundle (and #1172's dev-only failure);
    ``npm start`` runs the script with its developer port (3200, 3300).
    """
    wanted = app_start_argv(tree, app, port)
    if [str(a) for a in argv] != wanted:
        raise guard.Refused(
            f"{app} is served by {' '.join(wanted[2:])} of the built copy, not"
            f" {' '.join(str(a) for a in argv)}"
        )


#: A developer's addresses, which a bundle built for the tool never names.
DEVELOPER_ADDRESSES = re.compile(rb"localhost:(?:3000|3100|3200|3300|8000)")


def app_bundle_check(tree: Path, app: str, api_url: str) -> str:
    """The app's client bundle names the tool's API and no developer address."""
    static = app_dir(tree, app) / ".next" / "static"
    if not static.is_dir():
        raise BuildError(f"next build of {app} produced no .next/static")
    named = False
    for path in sorted(static.rglob("*.js")):
        data = path.read_bytes()
        if api_url.encode() in data:
            named = True
        found = DEVELOPER_ADDRESSES.search(data)
        if found:
            raise BuildError(
                f"{app}'s bundle names {found.group().decode()}"
                f" ({path.relative_to(static).as_posix()})"
            )
    if not named:
        raise BuildError(f"{app}'s bundle does not name the tool's API {api_url}")
    return f"{app} bundle: names {api_url}, no developer address"


def build_app(
    repo: Path,
    commit: str,
    tree: Path,
    app: str,
    env: Mapping[str, str],
    log: Path,
    *,
    spawn: Callable[..., int],
) -> str:
    """One app's tree, built; the bundle check's line."""
    archive(repo, commit, tree, app_paths(app))
    if not (app_dir(tree, app) / "package.json").is_file():
        raise BuildError(f"the ref has no demo/{app}")
    refuse_env_files(tree, app)
    clone_app_node_modules(repo, tree, app)
    status = spawn(app_build_argv(tree, app), cwd=app_dir(tree, app), env=env, log=log)
    if status != 0:
        raise BuildError(f"next build of {app} exited {status}; see {log}")
    return app_bundle_check(tree, app, env["NEXT_PUBLIC_CONTROLVAR_API_URL"])
