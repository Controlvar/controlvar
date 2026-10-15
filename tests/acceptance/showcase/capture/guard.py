"""What keeps the tool off a developer's own stack, files and credentials.

Everything here is decided before any ``docker``, ``npm`` or ``uvicorn`` call,
and none of it needs a browser, so all of it is unit tested
(``backend/tests/unit/showcase/``):

* ``refuse_ports``: the tool's three ports. A reserved port (``RESERVED_PORTS``),
  one below 1024, one given twice, or one something already listens on.
* ``refuse_reserved_in``: every program the tool starts is checked the same way
  on its argv and its environment, so a script that falls back to a default
  address is caught before it runs, not after.
* ``child_env``: a child's environment is built from an allow-list, never
  copied from the caller's. ``AWS_PROFILE``, a developer's ``POSTGRES_PORT``
  and anything else the caller has set do not reach it. Each kind of child
  has its own list (``allowed_env_keys``): the dashboard's build is never
  given an app's key, and only an app's seed is given that app's key.
* ``strip_environment``: the process's own environment is cut to the same list
  when it starts, as the render does, so the Playwright driver (which
  ``sync_playwright()`` starts from it) gets what the other children get.
* ``WorkRoot``: every deletion goes through ``WorkRoot.remove``, which refuses
  a path outside the work root, and a work root inside a git checkout.
* ``RunLock``: one holder of a lock file at a time; ``machine_lock`` is the
  capture's, one per machine, at a path outside every work root and git
  checkout, held from before the capture until the render ends. A second run
  refuses before it touches Docker.
* ``free_bytes`` / ``refuse_low_disk``: the 4 GiB floor.
* ``http_opener``: the tool's own HTTP calls take no proxy settings from the
  environment, as its child processes take none (``PASSED``).
"""

from __future__ import annotations

import errno
import fcntl
import os
import re
import shutil
import socket
import urllib.request
from pathlib import Path
from typing import (
    Dict,
    Iterable,
    List,
    Mapping,
    MutableMapping,
    Optional,
    Sequence,
    Tuple,
)

from showcase import contract
from showcase.capture import config


class Refused(RuntimeError):
    """A setting or a state the tool will not run with. The message says which."""


# ---------------------------------------------------------------------------
# Ports
# ---------------------------------------------------------------------------


def listening(port: int, host: str = "127.0.0.1") -> bool:
    """True when something accepts connections on *port*, or holds it.

    A connection attempt finds a listener on any address; the bind (with
    SO_REUSEADDR, so the closed connections a previous run left in TIME_WAIT
    do not count) finds a socket that holds the port without listening.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(1.0)
        if sock.connect_ex((host, port)) == 0:
            return True
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind((host, port))
        except OSError:
            return True
    return False


def refuse_ports(
    ports: Mapping[str, int],
    *,
    reserved: Sequence[int] = config.RESERVED_PORTS,
    in_use=listening,
) -> None:
    """Refused unless every port is free, not reserved, at least 1024 and unique."""
    seen: Dict[int, str] = {}
    for name, port in ports.items():
        if not isinstance(port, int) or not 0 < port < 65536:
            raise Refused(f"{name} port {port!r} is not a port")
        if port in reserved:
            raise Refused(
                f"{name} port {port} is reserved for a developer's own stack"
                f" ({', '.join(str(p) for p in reserved)})"
            )
        if port < 1024:
            raise Refused(f"{name} port {port} is below 1024")
        if port in seen:
            raise Refused(f"{name} port {port} is also the {seen[port]} port")
        seen[port] = name
    for name, port in ports.items():
        if in_use(port):
            raise Refused(f"{name} port {port} is already in use on this machine")


_NUMBER = re.compile(r"(?<![0-9])([0-9]{2,5})(?![0-9])")


def reserved_in(text: str, reserved: Sequence[int] = config.RESERVED_PORTS) -> bool:
    """True when *text* names a reserved port as a whole number (``:8000``, ``5432``)."""
    return any(int(number) in reserved for number in _NUMBER.findall(text))


def refuse_reserved_in(
    argv: Sequence[str],
    env: Mapping[str, str],
    *,
    what: str,
    reserved: Sequence[int] = config.RESERVED_PORTS,
) -> None:
    """Refused when a program's argv or environment names a reserved port."""
    for index, arg in enumerate(argv):
        if reserved_in(str(arg), reserved):
            raise Refused(f"{what}: argument {index} names a reserved port")
    for key in sorted(env):
        if reserved_in(str(env[key]), reserved):
            raise Refused(f"{what}: environment variable {key} names a reserved port")


# ---------------------------------------------------------------------------
# Child environments
# ---------------------------------------------------------------------------

#: Taken from the caller's environment, and only these: where programs are,
#: whose home it is, and where temporary files go.
PASSED: Tuple[str, ...] = ("PATH", "HOME", "TMPDIR")
#: Docker's client needs these when the caller set them (a remote context).
DOCKER_PASSED: Tuple[str, ...] = (
    "DOCKER_HOST",
    "DOCKER_CONTEXT",
    "DOCKER_CONFIG",
    "DOCKER_CERT_PATH",
    "DOCKER_TLS_VERIFY",
)
#: What the tool gives ``docker compose`` for the Quick Start stack, and only
#: these (``showcase.capture.compose.compose_settings``): the project, the
#: profile, the image tag and the four published ports.
COMPOSE_KEYS: Tuple[str, ...] = (
    "COMPOSE_PROJECT_NAME",
    "EXPERIMENTLY_PROFILE",
    "EXPERIMENTLY_IMAGE_TAG",
    "POSTGRES_HOST_PORT",
    "REDIS_HOST_PORT",
    "API_HOST_PORT",
    "FRONTEND_HOST_PORT",
)
#: Every child: no AWS configuration from the caller, and a closed endpoint.
NO_AWS: Dict[str, str] = {
    "AWS_ACCESS_KEY_ID": "showcase",
    "AWS_SECRET_ACCESS_KEY": "showcase",
    "AWS_CONFIG_FILE": os.devnull,
    "AWS_SHARED_CREDENTIALS_FILE": os.devnull,
    "AWS_EC2_METADATA_DISABLED": "true",
    "AWS_ENDPOINT_URL": "http://127.0.0.1:9",
    "AWS_MAX_ATTEMPTS": "1",
    "AWS_REGION": "us-west-2",
    "AWS_DEFAULT_REGION": "us-west-2",
}


def origin(port: int) -> str:
    """The address the browser reaches one of the tool's servers at."""
    return f"http://127.0.0.1:{port}"


def api_settings(
    *,
    api_port: int,
    dashboard_port: int,
    postgres_port: int,
    profile: str,
    app_ports: Sequence[int] = (),
) -> Dict[str, str]:
    """What the API, the bootstrap and the demo seed run with: compose's demo
    settings, pointed at the tool's Postgres, with Redis off.

    CORS allows the dashboard and the apps this video records (*app_ports*),
    exactly: the apps call the API from their own origin.
    """
    cors = [origin(dashboard_port)] + [origin(port) for port in app_ports]
    return {
        "ENVIRONMENT": "development",
        "LOG_LEVEL": "INFO",
        "AUTH_PROVIDER": "local",
        "DEV_AUTH_BYPASS": "false",
        "SECRET_KEY": config.STATIC_NEEDLES[3],
        "FIRST_SUPERUSER": config.DEMO_ADMIN[0],
        "FIRST_SUPERUSER_PASSWORD": config.DEMO_ADMIN[1],
        "POSTGRES_SERVER": "127.0.0.1",
        "POSTGRES_HOST": "127.0.0.1",
        "POSTGRES_PORT": str(postgres_port),
        "POSTGRES_USER": "postgres",
        "POSTGRES_PASSWORD": "postgres",
        "POSTGRES_DB": "experimentation",
        "POSTGRES_SCHEMA": "experimentation",
        "REDIS_HOST": "127.0.0.1",
        "REDIS_PORT": "1",
        "REDIS_URL": "redis://127.0.0.1:1",
        "REDIS_REQUIRED": "false",
        "SDK_RATE_LIMIT_PER_MINUTE": str(config.SDK_RATE_LIMIT_PER_MINUTE),
        "CORS_ORIGINS": ",".join(cors),
        "EXPERIMENTLY_PROFILE": profile,
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONUNBUFFERED": "1",
        **NO_AWS,
    }


def dashboard_settings(*, api_port: int, profile: str) -> Dict[str, str]:
    """What ``npm run build`` and the static server run with."""
    return {
        "EXPERIMENTLY_PROFILE": profile,
        # Same origin, as the shipped image: the static server proxies /api.
        "NEXT_PUBLIC_API_URL": "",
        # The static server does not proxy a WebSocket upgrade.
        "NEXT_PUBLIC_WS_URL": f"ws://127.0.0.1:{api_port}",
        "NEXT_TELEMETRY_DISABLED": "1",
        "NODE_ENV": "production",
        **NO_AWS,
    }


#: The variable each app's seed reads its key from, and where it writes its key files.
APP_KEY_VARIABLE: Dict[str, str] = {
    "shoplab": "SHOPLAB_API_KEY",
    "streampulse": "STREAMPULSE_API_KEY",
}


def seed_settings(app: str, api: Mapping[str, str], key_dir: Path) -> Dict[str, str]:
    """An app's seed: the API's settings, plus that app's compose key (which it
    registers) and the directory it writes its key files to (the tool's own,
    never the checkout's ``demo/<app>``)."""
    _module, dir_variable = config.APP_SEED[app]
    return {
        **api,
        APP_KEY_VARIABLE[app]: config.APP_STATIC_KEY[app],
        dir_variable: str(key_dir),
    }


def app_settings(*, app: str, api_port: int, dashboard_port: int) -> Dict[str, str]:
    """What an app's ``next build`` and ``next start`` run with: the three
    addresses and the key its bundle is built with (compose's demo key)."""
    return {
        "NEXT_PUBLIC_EXPERIMENTLY_API_URL": origin(api_port),
        "NEXT_PUBLIC_EXPERIMENTLY_API_KEY": config.APP_STATIC_KEY[app],
        "NEXT_PUBLIC_EXPERIMENTLY_DASHBOARD_URL": origin(dashboard_port),
        "NEXT_TELEMETRY_DISABLED": "1",
        "NODE_ENV": "production",
        **NO_AWS,
    }


#: The kinds of child the tool starts, each with its own allow-list.
KINDS: Tuple[str, ...] = (
    "docker",
    "compose",
    "api",
    "seed-shoplab",
    "seed-streampulse",
    "dashboard",
    "app",
)


def allowed_env_keys(kind: str) -> frozenset:
    """Every key a child of *kind* may be given: exactly, not the union of all kinds."""
    passed = set(PASSED)
    api = api_settings(
        api_port=1, dashboard_port=1, postgres_port=1, profile="core", app_ports=(1,)
    )
    if kind == "docker":
        keys = passed | set(DOCKER_PASSED)
    elif kind == "compose":
        # The Quick Start stack's compose and docker calls (showcase.capture.compose).
        keys = passed | set(DOCKER_PASSED) | set(COMPOSE_KEYS)
    elif kind == "api":
        keys = passed | set(api)
    elif kind.startswith("seed-") and kind[len("seed-") :] in config.APPS:
        keys = passed | set(seed_settings(kind[len("seed-") :], api, Path("/")))
    elif kind == "dashboard":
        keys = passed | set(dashboard_settings(api_port=1, profile="core"))
    elif kind == "app":
        keys = passed | set(
            app_settings(app=config.APPS[0], api_port=1, dashboard_port=1)
        )
    else:
        raise Refused(f"no child kind {kind!r}; one of {', '.join(KINDS)}")
    return frozenset(keys)


def child_env(
    settings: Mapping[str, str],
    *,
    parent: Mapping[str, str],
    docker: bool = False,
) -> Dict[str, str]:
    """A child's whole environment: ``PASSED`` from *parent*, then *settings*."""
    passed = PASSED + (DOCKER_PASSED if docker else ())
    env = {key: parent[key] for key in passed if key in parent}
    env.update(settings)
    return env


def http_opener() -> urllib.request.OpenerDirector:
    """urllib's opener for the tool's own HTTP calls: no proxy from the environment.

    ``urlopen`` sends a request through ``HTTP_PROXY`` (and its kind) when the
    caller set one; the tool's calls go to its own stack only, directly.
    """
    return urllib.request.build_opener(urllib.request.ProxyHandler({}))


def tool_env() -> Dict[str, str]:
    """For the tool's own helpers (git, cp, ps, ffmpeg): ``PASSED`` and nothing else."""
    return child_env({}, parent=os.environ)


def strip_environment(
    environ: MutableMapping[str, str], keep: Sequence[str] = PASSED
) -> List[str]:
    """Delete from *environ* every key not in *keep*; return the names removed, sorted.

    ``main`` calls it on ``os.environ`` before anything else starts, as the
    render does (``showcase/render/__main__.py``). A program started without an
    ``env=`` of its own inherits the process's environment, and
    ``sync_playwright()`` starts Playwright's driver that way; after this it
    holds ``PASSED`` and nothing else, the environment ``tool_env`` gives the
    helpers and the browser.
    """
    removed = sorted(key for key in environ if key not in keep)
    for key in removed:
        del environ[key]
    return removed


# ---------------------------------------------------------------------------
# The work root and deletions
# ---------------------------------------------------------------------------


def inside_git_checkout(path: Path) -> Optional[Path]:
    """The git checkout *path* is inside (a ``.git`` in it or a parent), or None."""
    path = path.resolve()
    for candidate in (path, *path.parents):
        if (candidate / ".git").exists():
            return candidate
    return None


class WorkRoot:
    """The one directory the tool writes its work into and deletes from."""

    def __init__(self, path: Path):
        resolved = Path(os.path.realpath(path))
        checkout = inside_git_checkout(resolved)
        if checkout is not None:
            raise Refused(
                f"the work root {resolved} is inside the git checkout {checkout};"
                " give --work-dir a directory outside any checkout"
            )
        if resolved == Path(resolved.anchor) or resolved == Path.home().resolve():
            raise Refused(f"the work root {resolved} is too broad")
        self.path = resolved

    def contains(self, path: Path) -> bool:
        """True when *path* (a link: where the link itself is) is under the root."""
        if path.is_symlink():
            resolved = Path(os.path.realpath(path.parent)) / path.name
        else:
            resolved = Path(os.path.realpath(path))
        return resolved != self.path and self.path in resolved.parents

    def remove(self, path: Path) -> None:
        """Delete *path* (a file or a tree); refused outside the work root.

        A symbolic link is removed as a link: what it points to is not followed.
        """
        if not self.contains(path):
            raise Refused(f"refusing to delete {path}: it is not under {self.path}")
        if path.is_symlink() or path.is_file():
            path.unlink()
        elif path.is_dir():
            shutil.rmtree(path)


def _device(path: Path) -> int:
    probe = path
    while not probe.exists():
        probe = probe.parent
    return os.stat(probe).st_dev


def refuse_out_beside_work(out: Path, work_root: Path) -> None:
    """``--out`` apart from ``--work-dir``, on the same filesystem; else Refused.

    The render works in ``<work-dir>/<video>/render``, deletes it when it
    ends, and links the finished files from there into ``--out``. Its paths
    gate refuses an overlap or two filesystems too, but only after the
    capture; this refuses before anything starts.
    """
    out_path = Path(os.path.realpath(out))
    work_path = Path(os.path.realpath(work_root))
    # Case-folded, as the render's paths gate compares: the default macOS
    # filesystem is case-insensitive, so ``Videos`` and ``videos`` are one.
    out_key = tuple(part.casefold() for part in out_path.parts)
    work_key = tuple(part.casefold() for part in work_path.parts)
    shorter = min(len(out_key), len(work_key))
    if out_key[:shorter] == work_key[:shorter]:
        raise Refused(
            f"--out {out_path} and --work-dir {work_path} overlap; the render deletes"
            " its work directory under --work-dir when it ends, so keep the videos"
            " apart from it"
        )
    if _device(out_path) != _device(work_path):
        raise Refused(
            f"--out {out_path} and --work-dir {work_path} are on different"
            " filesystems; the render links the finished files from its work"
            " directory into --out, so put both on one"
        )


# ---------------------------------------------------------------------------
# One run at a time
# ---------------------------------------------------------------------------


class RunLock:
    """An exclusive lock on ``<root>/showcase.lock`` (or on *file*), held for the run.

    The kernel releases it when the process ends, killed or not, so a stale
    lock file never blocks the next run; a second run while one is going
    refuses before any docker call (it would otherwise remove the first run's
    container as a leftover).
    """

    def __init__(self, root: Optional[Path] = None, *, file: Optional[Path] = None):
        if (root is None) == (file is None):
            raise ValueError("a lock is on a root's showcase.lock or on a file")
        self.file = file if file is not None else root / "showcase.lock"  # type: ignore[operator]
        self.handle = None

    def acquire(self) -> None:
        self.file.parent.mkdir(parents=True, exist_ok=True)
        handle = open(self.file, "a+", encoding="utf-8")
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            handle.close()
            if error.errno in (errno.EAGAIN, errno.EACCES, errno.EWOULDBLOCK):
                raise Refused(
                    f"another showcase run holds {self.file}; one run at a time"
                ) from None
            raise
        handle.seek(0)
        handle.truncate()
        handle.write(f"{os.getpid()}\n")
        handle.flush()
        self.handle = handle

    def release(self) -> None:
        if self.handle is not None:
            fcntl.flock(self.handle, fcntl.LOCK_UN)
            self.handle.close()
            self.handle = None


def machine_lock_path(environ: Mapping[str, str]) -> Path:
    """``$HOME/Library/Caches/experimently-showcase/showcase.lock``.

    Refused when ``HOME`` is unset, or when the path is inside a git checkout
    (a checkout's clean-up must never be able to delete it).
    """
    home = environ.get("HOME")
    if not home:
        raise Refused("HOME is not set; the machine lock is kept under it")
    path = Path(os.path.realpath(home)).joinpath(*config.MACHINE_LOCK)
    checkout = inside_git_checkout(path.parent)
    if checkout is not None:
        raise Refused(
            f"the machine lock {path} would be inside the git checkout {checkout}"
        )
    return path


def machine_lock(environ: Mapping[str, str]) -> RunLock:
    """The capture's lock: one run per machine, whatever its work root and ports."""
    return RunLock(file=machine_lock_path(environ))


def refuse_lock_inside(lock: Path, *roots: Path) -> None:
    """Refused when the machine lock is under one of *roots* (a work root, --out)."""
    real = Path(os.path.realpath(lock))
    for root in roots:
        where = Path(os.path.realpath(root))
        if real == where or where in real.parents:
            raise Refused(
                f"the machine lock {real} is inside {where}; the tool deletes"
                " under its work root, so keep them apart"
            )


# ---------------------------------------------------------------------------
# Disk
# ---------------------------------------------------------------------------


def free_bytes(path: Path) -> int:
    probe = path
    while not probe.exists():
        probe = probe.parent
    return shutil.disk_usage(probe).free


def refuse_low_disk(
    paths: Iterable[Path], *, floor: int = contract.DISK_FLOOR_BYTES, before: str
) -> List[Tuple[str, int]]:
    """Each path's free bytes; Refused when any is under *floor*."""
    readings = []
    for path in paths:
        free = free_bytes(path)
        readings.append((str(path), free))
        if free < floor:
            raise Refused(
                f"{free / 1024**3:.1f} GiB free at {path}, under the"
                f" {floor / 1024**3:.1f} GiB floor; {before} not started."
                " Free some space (the tool deletes nothing outside its work root)."
            )
    return readings
