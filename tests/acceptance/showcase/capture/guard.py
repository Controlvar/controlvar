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
  and anything else the caller has set do not reach it.
* ``WorkRoot``: every deletion goes through ``WorkRoot.remove``, which refuses
  a path outside the work root, and a work root inside a git checkout.
* ``RunLock``: one run per work root. A second refuses before it touches Docker.
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
from typing import Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

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


def api_settings(
    *, api_port: int, dashboard_port: int, postgres_port: int, profile: str
) -> Dict[str, str]:
    """What the API, the bootstrap and the seeds run with: compose's demo settings,
    pointed at the tool's Postgres, with Redis off."""
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
        "CORS_ORIGINS": f"http://127.0.0.1:{dashboard_port}",
        "SHOPLAB_API_KEY": config.STATIC_NEEDLES[1],
        "STREAMPULSE_API_KEY": config.STATIC_NEEDLES[2],
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
    """An exclusive lock on ``<work root>/showcase.lock``, held for the run.

    The kernel releases it when the process ends, killed or not, so a stale
    lock file never blocks the next run; a second run while one is going
    refuses before any docker call (it would otherwise remove the first run's
    container as a leftover).
    """

    def __init__(self, root: Path):
        self.file = root / "showcase.lock"
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
