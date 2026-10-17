"""The seven stacks a journey runs against. ``conftest.py`` makes each a fixture.

* ``compose-dev``: the Docker guide's ``docker compose up -d --wait`` at the
  repository root, under its own compose project, with its own host ports, and
  with ``--build``, so the images are the checkout's and never ones left by an
  earlier run. Before ``up``, and after an ``up`` that fails,
  ``docker compose -p <project> down -v --remove-orphans`` clears the project,
  so a run starts from an empty database and leaves nothing running.
  Defaults are well away from a developer's own stack (5432, 6379, 8000, 3000);
  each can be set in the environment:

  ======================================  ===========================  =======
  variable                                compose variable it sets     default
  ======================================  ===========================  =======
  ``DOCS_JOURNEY_COMPOSE_PROJECT``        the compose project name     ``docs-journeys``
  ``DOCS_JOURNEY_API_PORT``               ``API_HOST_PORT``            28000
  ``DOCS_JOURNEY_DASHBOARD_PORT``         ``FRONTEND_HOST_PORT``       23000
  ``DOCS_JOURNEY_POSTGRES_PORT``          ``POSTGRES_HOST_PORT``       25432
  ``DOCS_JOURNEY_REDIS_PORT``             ``REDIS_HOST_PORT``          26379
  ======================================  ===========================  =======

  Compose sees only those, ``EXPERIMENTLY_PROFILE`` (the journey's profile),
  ``EXPERIMENTLY_IMAGE_TAG`` (``<project>-<profile>``, so the images it builds
  are its own), and the few variables Docker itself needs: nothing else from the caller's
  environment, no ``.env`` file (``COMPOSE_DISABLE_ENV_FILE=1``), and no AWS
  configuration. After ``up`` the API's ``/api/v1/modules`` must report the
  journey's profile. The stack is brought down (``down -v``) when the session
  ends, or before a journey of the other profile.

  A guide gives the addresses a reader gets with the default host ports
  (``http://localhost:3000`` for the dashboard, ``http://localhost:8000`` for
  the API). The stack's ``published`` maps each default that
  ``docker-compose.yml`` itself gives the dashboard and the API
  (``${FRONTEND_HOST_PORT:-3000}``, ``${API_HOST_PORT:-8000}``) to where that
  service runs here, so an ``open`` of a guide's address reaches the same
  service on this stack's port, and one of a port the compose file does not
  default to reaches nothing.

* ``compose-sso``: compose-dev's stack in the full profile, brought up the way
  ``docs/auth/sso.md`` tells a compose reader to bring it up for SSO sign-in:
  with the override file its section "Trying it on the Docker Compose stack"
  gives (the first YAML block there, taken from the page at run time and
  passed with ``-f``), plus ``stacks/compose-sso.yml`` beside this runner,
  which adds what a reader brings from outside and the run cannot: an
  identity provider. That provider is ``modules/``'s fake OIDC provider
  (``fake_oidc_provider.py``), run from the API's own image in the API's
  network namespace (``network_mode: "service:api"``), serving https on
  port 28443 with the issuer ``https://localhost:28443/oauth2/default``. The
  api service publishes that port on the host's loopback address only
  (``127.0.0.1:28443``), so the browser and the API reach the provider at the
  one address an SSO configuration's ``sso_url`` names, and nothing off the
  host reaches it. Its certificate is signed by a CA made for the run with
  ``openssl`` in a temporary directory outside the checkout and the run
  directory; the CA's key is deleted as soon as the certificate is signed,
  and the directory is removed when the stack comes down. The API is given
  that CA as ``SSL_CERT_FILE``; its sign-in client (authlib over httpx2) then
  builds its TLS context from that file alone, in place of the system's
  store, so on this stack it trusts that CA and no other. The browser's
  context for a journey on
  this stack ignores certificate errors (``Running.ignore_https_errors``);
  no other stack's does. The two files may not set ``ENVIRONMENT``,
  ``APP_ENV`` or ``DEV_AUTH_BYPASS`` on ``api``, nor give it an ``env_file``
  (``refused_api_settings``): the walk is of the development environment a
  compose reader runs, not of ``test``, which allows an ``http`` provider.
  The dashboard and the API are opened at ``localhost``, not ``127.0.0.1``:
  the sign-in's cookie belongs to the host ``PUBLIC_BASE_URL`` names, and the
  page's override names ``localhost``. It shares compose-dev's project, ports
  and images: whichever of the two comes up brings the other down first.
  Before it comes down, what the API and the provider printed is added to its
  log, ``stacks/compose-sso.log``.

* ``compose-empty``: compose-dev's stack with no seed data. ``docker-compose.yml``
  applies the seeds ``SEED`` names, ``demo`` by default, and none when it is set
  and empty, so this stack sets ``SEED=`` and nothing else that compose-dev does
  not: the workspace a new self-hoster opens, with no experiment and no flag,
  and one account, the administrator the API creates from the compose file's
  ``FIRST_SUPERUSER`` and ``FIRST_SUPERUSER_PASSWORD`` defaults (the demo
  administrator's address and password). It shares compose-dev's project,
  ports and images, so whichever of the compose stacks comes up brings the
  other down first, and it always starts from an empty database (``down -v``).

A command that cannot be started at all (no ``docker``, no ``npm``) is a
``StackError`` like one that fails, so the journey is reported FAIL before its
first step rather than ending the session.
* ``docs-local``: ``mkdocs build`` into a temporary directory, served by
  ``python -m http.server`` on 127.0.0.1 (``DOCS_JOURNEY_DOCS_PORT``, else a
  free port). Needs the docs toolchain (``scripts/docs_toolchain.sh``). Its
  source is this checkout.
* ``docs-published``: ``DOCS_JOURNEY_PUBLISHED_URL``, by default the site's
  ``site_url``. Only an https URL is accepted. Its source (what a crawl
  compares the site with: ``site.py``) is ``DOCS_JOURNEY_PUBLISHED_SOURCE``, a
  directory holding the ``mkdocs.yml`` and ``docs/`` the published site was
  built from. The site is deployed from release tags, not from ``main``, so
  ``docs-journeys.yml`` sets it to the commit of the site's last successful
  deployment; unset, it is this checkout, which is right only when the
  checkout is that commit. ``DOCS_JOURNEY_PUBLISHED_REF``, when set, names that
  deployment's ref (a tag such as ``v0.25.1``): the crawls say they compared
  the site with it. Anything that is not a ref's characters is refused.
* ``marketing-local``: ``npm run build:marketing`` in ``frontend/`` (which needs
  its ``node_modules``), then ``frontend/out`` served like docs-local
  (``DOCS_JOURNEY_MARKETING_PORT``, else a free port). The build writes Next's
  own output under ``frontend/`` (``.next/``, ``out/``, both ignored by git).

* ``demo-local``: ``./demo/setup-local.sh`` run from the checkout's root, as
  ``demo/DEMO_GUIDE.md`` tells a reader to run it, then the guide's own stop
  command, ``./demo/teardown-local.sh``. The script binds fixed host ports
  (``DEMO_PORTS``) and installs into the checkout (``venv/``, each app's
  ``node_modules``, ``.env``, ``demo/.logs`` and the API key files), so the
  stack runs only on a CI runner: it refuses to start unless
  ``GITHUB_ACTIONS`` is ``true`` and ``DOCS_JOURNEY_DEMO_LOCAL`` is ``1``
  (the ``demo`` job of ``docs-journeys.yml`` sets it), and refuses when any of
  those ports already answers, so it never starts over something running.
  The script must exit 0 having printed its "Ready!" box; the addresses that
  box lists are the stack's (the dashboard is its base URL, the API docs'
  origin its API, and each address is where an ``open`` of it goes), and each
  must answer before the first journey (``DOCS_JOURNEY_DEMO_WAIT`` seconds,
  default 600, which also covers each development server's first compile).
  The script may take ``DOCS_JOURNEY_DEMO_TIMEOUT`` seconds (default 900,
  about four times what it took on a GitHub-hosted runner, 196-238 s); the
  time it took is written to the log. Its accounts are the demo seed's.
  What it prints, and when the stack comes down each log ``demo/.logs`` holds,
  is written to ``stacks/demo-local.log`` and ``stacks/demo-local/`` with the
  values the stack registers taken out: the administrator's password the
  script sets (``FIRST_SUPERUSER_PASSWORD``) and every API key the seeds and
  the script write (``DEMO_KEY_FILES``). The same values go to the runner's
  redactor and to the end-of-run scan (``Running.secrets``). Coming down,
  the stop command must exit 0 and leave no port of ``DEMO_PORTS``
  answering and none of ``DEMO_REMOVED`` behind; otherwise ``down`` raises
  StackError and the run fails.

What each command prints goes to ``stacks/<stack>.log`` in the run directory,
never to the console.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Tuple

import yaml

PUBLISHED_URL = "https://docs.controlvar.com/"
#: A ref as ``docs_journeys_report.py deployed`` accepts one.
SOURCE_REF = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,99}")

#: The compose variables of the two services a browser opens: the API and the dashboard.
HTTP_PORT_VARIABLES = ("API_HOST_PORT", "FRONTEND_HOST_PORT")
#: A ``ports`` entry whose host port is a variable with a default, with or
#: without the ``127.0.0.1:`` the development compose file puts before it.
DEFAULTED_PORT = re.compile(
    r"^(?:127\.0\.0\.1:)?\$\{(?P<variable>[A-Z_]+):-(?P<default>[0-9]{1,5})\}:"
)

#: (our variable, the compose variable, the default host port)
COMPOSE_PORTS: Tuple[Tuple[str, str, int], ...] = (
    ("DOCS_JOURNEY_API_PORT", "API_HOST_PORT", 28000),
    ("DOCS_JOURNEY_DASHBOARD_PORT", "FRONTEND_HOST_PORT", 23000),
    ("DOCS_JOURNEY_POSTGRES_PORT", "POSTGRES_HOST_PORT", 25432),
    ("DOCS_JOURNEY_REDIS_PORT", "REDIS_HOST_PORT", 26379),
)
#: What compose and docker need from the caller's environment.
PASSED_ENV = (
    "PATH",
    "HOME",
    "USER",
    "TMPDIR",
    "DOCKER_HOST",
    "DOCKER_CONTEXT",
    "DOCKER_CONFIG",
    "DOCKER_CERT_PATH",
    "DOCKER_TLS_VERIFY",
)
#: The ``demo`` seed's accounts (backend/scripts/seed_demo_data.py), which the
#: compose stack applies on its first start.
DEMO_ACCOUNTS: Dict[str, Tuple[str, str]] = {
    "admin": ("admin@demo.com", "Demo1234!"),
    "developer": ("dev@demo.com", "Demo1234!"),
    "analyst": ("analyst@demo.com", "Demo1234!"),
    "viewer": ("viewer@demo.com", "Demo1234!"),
}


#: compose-sso: the guide whose override file the stack is brought up with, and
#: the section (its anchor) whose first YAML block is that file.
SSO_GUIDE = "auth/sso.md"
SSO_OVERRIDE_SECTION = "trying-it-on-the-docker-compose-stack"
#: What the stack adds that a reader does not: the identity provider.
SSO_OVERLAY = Path("tests") / "acceptance" / "docs" / "stacks" / "compose-sso.yml"
#: The provider it runs (under modules/: the full profile's).
FAKE_IDP = (
    Path("modules")
    / "backend"
    / "tests"
    / "integration"
    / "api"
    / "fake_oidc_provider.py"
)
#: Settings on ``api`` that would walk another environment than a compose
#: reader's: ``test`` allows an ``http`` provider and turns the rate limits off.
REFUSED_API_SETTINGS = ("ENVIRONMENT", "APP_ENV", "DEV_AUTH_BYPASS")
#: How long each openssl command may take.
OPENSSL_SECONDS = 60

#: The compose stack each project is up as, so that compose-dev and compose-sso
#: (one project, one set of ports) are never up at once.
_COMPOSE_UP: Dict[str, "ComposeDev"] = {}


class StackError(RuntimeError):
    """The stack did not come up; the message says where its output is."""


@dataclass(frozen=True)
class Running:
    """A stack that answers: where its pages are, and its API if it has one."""

    name: str
    base_url: str
    api_url: str = ""
    profile: str = ""
    accounts: Mapping[str, Tuple[str, str]] = field(default_factory=dict)
    #: For a documentation site: the directory it was built from.
    source: Optional[Path] = None
    #: And the ref that directory was taken from, "" when it is not known.
    source_ref: str = ""
    #: For the compose stack: each default host port a guide's address names,
    #: and the base URL that stands for it here.
    published: Mapping[int, str] = field(default_factory=dict)
    #: How long bringing the stack up took, in seconds (0 when not measured):
    #: a walkthrough's title card says it, the start itself not being recorded.
    up_seconds: float = 0.0
    #: The browser accepts any certificate: only compose-sso, whose identity
    #: provider's certificate is signed by the run's own CA.
    ignore_https_errors: bool = False
    #: Values the stack made or set that no file of the run may hold
    #: (demo-local's administrator password and API keys): the runner's
    #: redactor takes them out of what it writes, and the scan looks for them.
    secrets: Tuple[str, ...] = ()
    #: demo-local only: the checkout the script set up, where a ``run`` step's
    #: block runs (``commands.py``).
    checkout: Optional[Path] = None


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _port(environ: Mapping[str, str], name: str, default: int) -> int:
    raw = environ.get(name, "")
    if not raw:
        return default
    if not raw.isdigit() or not 0 < int(raw) < 65536:
        raise StackError(f"{name}={raw!r} is not a port")
    return int(raw)


def wait_until_answering(url: str, seconds: float) -> None:
    """Poll *url* until it answers at all (any HTTP status), or StackError."""
    deadline = time.monotonic() + seconds
    last = ""
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=5):
                return
        except urllib.error.HTTPError:
            return
        except (urllib.error.URLError, OSError) as error:
            last = str(error)
        time.sleep(0.5)
    raise StackError(f"{url} did not answer within {seconds:.0f} s ({last})")


def compose_default_ports(compose_file: Path) -> Dict[str, int]:
    """Each ``${VAR:-port}`` host port in *compose_file*'s ``ports``, by variable."""
    try:
        data = yaml.safe_load(compose_file.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as error:
        raise StackError(f"{compose_file.name} cannot be read: {error}") from None
    found: Dict[str, int] = {}
    services = data.get("services", {}) if isinstance(data, dict) else {}
    for service in services.values() if isinstance(services, dict) else []:
        for entry in (service or {}).get("ports", []) or []:
            match = DEFAULTED_PORT.match(str(entry))
            if match:
                found[match["variable"]] = int(match["default"])
    return found


def _run_logged(argv, *, cwd: Path, env: Mapping[str, str], log: Path, timeout: float):
    """Run *argv*, its output appended to *log*; the exit status, or None on timeout.

    StackError when it cannot be started at all (the program is missing, say).
    """
    log.parent.mkdir(parents=True, exist_ok=True)
    with log.open("a", encoding="utf-8") as handle:
        handle.write(f"$ {' '.join(argv)}\n")
        handle.flush()
        try:
            done = subprocess.run(
                argv,
                cwd=cwd,
                env=dict(env),
                stdout=handle,
                stderr=subprocess.STDOUT,
                timeout=timeout,
                check=False,
            )
        except subprocess.TimeoutExpired:
            handle.write(f"(stopped after {timeout:.0f} s)\n")
            return None
        except OSError as error:
            handle.write(f"(could not start: {error})\n")
            raise StackError(
                f"{argv[0]} could not be started ({type(error).__name__});"
                f" see stacks/{log.name}"
            ) from None
    return done.returncode


class _StaticServer:
    """``python -m http.server`` over a directory, on 127.0.0.1."""

    def __init__(self, root: Path, port: int):
        self.root = root
        self.port = port
        self.process: Optional[subprocess.Popen] = None

    def start(self) -> str:
        if not self.root.is_dir():
            raise StackError(f"nothing to serve: {self.root} is not a directory")
        try:
            self.process = self._spawn()
        except OSError as error:
            raise StackError(
                f"the static server could not be started ({type(error).__name__})"
            ) from None
        url = f"http://127.0.0.1:{self.port}/"
        wait_until_answering(url, 30)
        return url

    def _spawn(self) -> subprocess.Popen:
        return subprocess.Popen(
            [
                sys.executable,
                "-m",
                "http.server",
                str(self.port),
                "--bind",
                "127.0.0.1",
                "--directory",
                str(self.root),
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

    def stop(self) -> None:
        if self.process is not None and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.process.kill()
        self.process = None


class ComposeDev:
    #: The stack's name, its log file's stem, and the host its URLs name.
    NAME = "compose-dev"
    HOST = "127.0.0.1"
    #: The accounts an api step may call as: the demo seed's.
    ACCOUNTS: Mapping[str, Tuple[str, str]] = DEMO_ACCOUNTS
    #: Whether the browser accepts any certificate on this stack.
    IGNORE_HTTPS_ERRORS = False

    def __init__(self, repo_root: Path, logs: Path, environ: Mapping[str, str]):
        self.repo_root = repo_root
        self.logs = logs
        self.project = environ.get("DOCS_JOURNEY_COMPOSE_PROJECT", "docs-journeys")
        self.ports = {
            compose: _port(environ, ours, default)
            for ours, compose, default in COMPOSE_PORTS
        }
        self.timeout = float(environ.get("DOCS_JOURNEY_COMPOSE_TIMEOUT", "1800"))
        self.passed = {key: environ[key] for key in PASSED_ENV if key in environ}
        self.running: Optional[Running] = None

    def env(self, profile: str) -> Dict[str, str]:
        env = dict(self.passed)
        env.update(
            {
                "COMPOSE_PROJECT_NAME": self.project,
                "COMPOSE_DISABLE_ENV_FILE": "1",
                "AWS_CONFIG_FILE": os.devnull,
                "AWS_SHARED_CREDENTIALS_FILE": os.devnull,
                "EXPERIMENTLY_PROFILE": profile,
                # Images of their own, so a developer's experimently-api:core
                # and experimently-web:core are not rebuilt under them.
                "EXPERIMENTLY_IMAGE_TAG": f"{self.project}-{profile}",
            }
        )
        env.update({name: str(port) for name, port in self.ports.items()})
        return env

    @property
    def log(self) -> Path:
        return self.logs / f"{self.NAME}.log"

    def files(self) -> Tuple[str, ...]:
        """The ``-f`` arguments ``up`` is given: none, so docker-compose.yml."""
        return ()

    def prepare(self, profile: str) -> None:
        """What must exist before ``up``: nothing here."""

    def _compose(self, profile: str, *args: str, timeout: float) -> Optional[int]:
        return _run_logged(
            ["docker", "compose", "-p", self.project, *args],
            cwd=self.repo_root,
            env=self.env(profile),
            log=self.log,
            timeout=timeout,
        )

    def _clear(self, profile: str) -> None:
        # docker-compose.yml alone: --remove-orphans takes any other service
        # the project runs (compose-sso's identity provider) with it.
        self._compose(profile, "down", "-v", "--remove-orphans", timeout=600)

    def up(self, profile: str) -> Running:
        if self.running is not None and self.running.profile == profile:
            return self.running
        published = self.published()
        holder = _COMPOSE_UP.get(self.project)
        if holder is not None and holder is not self:
            holder.down()
        self.down()
        self._clear(profile)
        try:
            self.prepare(profile)
        except StackError:
            self._clear(profile)
            raise
        except OSError as error:
            self._clear(profile)
            raise StackError(
                f"the {self.NAME} stack could not be prepared: {error}"
            ) from None
        started = time.monotonic()
        status = self._compose(
            profile,
            *self.files(),
            "up",
            "-d",
            "--wait",
            "--build",
            timeout=self.timeout,
        )
        if status != 0:
            self._clear(profile)
            raise StackError(
                f"docker compose up ({profile}) "
                + ("timed out" if status is None else f"exited {status}")
                + f"; its output is in stacks/{self.log.name}"
            )
        api_url = f"http://{self.HOST}:{self.ports['API_HOST_PORT']}"
        self.running = Running(
            name=self.NAME,
            base_url=f"http://{self.HOST}:{self.ports['FRONTEND_HOST_PORT']}/",
            api_url=api_url,
            profile=profile,
            accounts=self.ACCOUNTS,
            published=published,
            up_seconds=time.monotonic() - started,
            ignore_https_errors=self.IGNORE_HTTPS_ERRORS,
        )
        _COMPOSE_UP[self.project] = self
        try:
            served = self._served_profile(api_url)
            if served != profile:
                raise StackError(
                    f"the stack serves the {served} profile, not {profile}"
                )
        except StackError:
            self.down()
            raise
        return self.running

    def published(self) -> Dict[int, str]:
        """The dashboard's and the API's default host ports, each mapped to its URL here."""
        defaults = compose_default_ports(self.repo_root / "docker-compose.yml")
        missing = [name for name in HTTP_PORT_VARIABLES if name not in defaults]
        if missing:
            raise StackError(
                f"docker-compose.yml gives no default host port for {missing[0]}"
            )
        return {
            defaults[name]: f"http://{self.HOST}:{self.ports[name]}"
            for name in HTTP_PORT_VARIABLES
        }

    @staticmethod
    def _served_profile(api_url: str) -> str:
        try:
            with urllib.request.urlopen(
                f"{api_url}/api/v1/modules", timeout=10
            ) as answer:
                return str(json.load(answer).get("profile"))
        except (urllib.error.URLError, OSError, ValueError) as error:
            raise StackError(f"GET /api/v1/modules did not answer: {error}") from None

    def down(self) -> None:
        if _COMPOSE_UP.get(self.project) is self:
            del _COMPOSE_UP[self.project]
        if self.running is None:
            return
        profile = self.running.profile
        self.running = None
        self._clear(profile)


def refused_api_settings(text: str, name: str) -> List[str]:
    """What a compose file sets on ``api`` that compose-sso refuses, one line each.

    ``REFUSED_API_SETTINGS`` in its ``environment`` (a mapping or a list of
    ``KEY=value``), and an ``env_file``, which could set them unseen. A file
    that is not a compose mapping is refused too.
    """
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as error:
        return [f"{name} is not YAML: {str(error).splitlines()[0]}"]
    services = data.get("services") if isinstance(data, dict) else None
    if not isinstance(services, dict):
        return [f"{name} has no services mapping"]
    api = services.get("api") or {}
    if not isinstance(api, dict):
        return [f"{name}: services.api is not a mapping"]
    found: List[str] = []
    if "env_file" in api:
        found.append(
            f"{name} gives api an env_file; compose-sso sets api's environment only"
            " where it can be read"
        )
    environment: Any = api.get("environment") or {}
    if isinstance(environment, dict):
        keys = [str(key) for key in environment]
    elif isinstance(environment, list):
        keys = [str(entry).split("=", 1)[0] for entry in environment]
    else:
        return found + [f"{name}: services.api.environment is not a mapping or list"]
    for key in keys:
        if key.strip() in REFUSED_API_SETTINGS:
            found.append(
                f"{name} sets {key.strip()} on api; compose-sso walks the environment"
                " a compose reader runs, so it may not"
            )
    return found


def page_override(repo_root: Path) -> str:
    """The first YAML block of sso.md's compose section: the reader's override file."""
    from docs_runner import guide as guides
    from docs_runner.sdk import blocks

    try:
        page = guides.load(repo_root / "docs", SSO_GUIDE)
    except OSError as error:
        raise StackError(f"docs/{SSO_GUIDE} cannot be read: {error}") from None
    section = page.section(SSO_OVERRIDE_SECTION)
    if not section:
        raise StackError(f"docs/{SSO_GUIDE} has no section #{SSO_OVERRIDE_SECTION}")
    found = [block.text for block in blocks(section) if block.language == "yaml"]
    if not found:
        raise StackError(
            f"docs/{SSO_GUIDE}#{SSO_OVERRIDE_SECTION} gives no YAML override file"
        )
    return found[0]


def make_certificates(directory: Path, env: Mapping[str, str], log: Path) -> None:
    """``ca.pem`` and a ``leaf.pem``/``leaf.key`` for ``localhost`` it signed.

    The CA's key is made in a directory of its own and deleted with it as soon
    as the leaf is signed, so nothing can sign with that CA again. StackError
    when openssl fails or cannot be started.
    """
    private = Path(tempfile.mkdtemp(prefix="docs-journeys-ca-"))
    try:
        ca_key = private / "ca.key"
        csr = private / "leaf.csr"
        extensions = private / "leaf.ext"
        extensions.write_text(
            "basicConstraints = critical, CA:FALSE\n"
            "keyUsage = critical, digitalSignature, keyEncipherment\n"
            "extendedKeyUsage = serverAuth\n"
            "subjectAltName = DNS:localhost\n"
            "subjectKeyIdentifier = hash\n"
            "authorityKeyIdentifier = keyid, issuer\n",
            encoding="utf-8",
        )
        ca = directory / "ca.pem"
        commands = [
            "openssl req -x509 -newkey rsa:2048 -nodes -sha256 -days 2"
            " -subj /CN=docs-journeys-ca"
            " -addext basicConstraints=critical,CA:TRUE"
            " -addext keyUsage=critical,keyCertSign,cRLSign".split()
            + ["-keyout", str(ca_key), "-out", str(ca)],
            "openssl req -newkey rsa:2048 -nodes -sha256 -subj /CN=localhost".split()
            + ["-keyout", str(directory / "leaf.key"), "-out", str(csr)],
            "openssl x509 -req -sha256 -days 2".split()
            + ["-in", str(csr), "-CA", str(ca), "-CAkey", str(ca_key)]
            + ["-set_serial", str(secrets.randbits(63) or 1)]
            + ["-extfile", str(extensions), "-out", str(directory / "leaf.pem")],
        ]
        for argv in commands:
            status = _run_logged(
                argv, cwd=private, env=env, log=log, timeout=OPENSSL_SECONDS
            )
            if status != 0:
                raise StackError(
                    "openssl "
                    + ("timed out" if status is None else f"exited {status}")
                    + f" making the identity provider's certificate; see stacks/{log.name}"
                )
    finally:
        shutil.rmtree(private, ignore_errors=True)
    # The API reads the CA as its own user, the provider reads its key as the
    # caller's (the overlay's DOCS_JOURNEY_IDP_USER).
    directory.chmod(0o755)
    (directory / "ca.pem").chmod(0o644)
    (directory / "leaf.pem").chmod(0o644)
    (directory / "leaf.key").chmod(0o600)


class ComposeSso(ComposeDev):
    """compose-dev's stack, full profile, with sso.md's override and an identity provider."""

    NAME = "compose-sso"
    HOST = "localhost"
    IGNORE_HTTPS_ERRORS = True

    def __init__(self, repo_root: Path, logs: Path, environ: Mapping[str, str]):
        super().__init__(repo_root, logs, environ)
        #: The run's own directory: the override file, and ``idp/`` (the
        #: certificates the provider and the API read). Outside the checkout.
        self.workdir: Optional[Path] = None

    def env(self, profile: str) -> Dict[str, str]:
        env = super().env(profile)
        if self.workdir is not None:
            env.update(
                {
                    "DOCS_JOURNEY_IDP_DIR": str(self.workdir / "idp"),
                    "DOCS_JOURNEY_FAKE_IDP": str(self.repo_root / FAKE_IDP),
                    "DOCS_JOURNEY_IDP_USER": f"{os.getuid()}:{os.getgid()}",
                }
            )
        return env

    def files(self) -> Tuple[str, ...]:
        if self.workdir is None:
            return ()
        return (
            "-f",
            str(self.repo_root / "docker-compose.yml"),
            "-f",
            str(self.workdir / "compose.sso.yml"),
            "-f",
            str(self.repo_root / SSO_OVERLAY),
        )

    def up(self, profile: str) -> Running:
        if profile != "full":
            raise StackError(
                f"compose-sso is the full profile's stack (the sso module is not in"
                f" {profile})"
            )
        return super().up(profile)

    def prepare(self, profile: str) -> None:
        override = page_override(self.repo_root)
        try:
            overlay = (self.repo_root / SSO_OVERLAY).read_text(encoding="utf-8")
        except OSError as error:
            raise StackError(
                f"{SSO_OVERLAY.as_posix()} cannot be read: {error}"
            ) from None
        problems = refused_api_settings(
            override, f"docs/{SSO_GUIDE}'s override file"
        ) + refused_api_settings(overlay, SSO_OVERLAY.as_posix())
        if problems:
            raise StackError("; ".join(problems))
        if not (self.repo_root / FAKE_IDP).is_file():
            raise StackError(f"{FAKE_IDP.as_posix()} is missing (a core checkout?)")
        self.workdir = Path(tempfile.mkdtemp(prefix="docs-journeys-sso-"))
        for inside in (self.repo_root.resolve(), self.logs.parent.resolve()):
            if self.workdir.resolve().is_relative_to(inside):
                raise StackError(
                    f"the temporary directory {self.workdir} is inside {inside}"
                )
        (self.workdir / "compose.sso.yml").write_text(override, encoding="utf-8")
        (self.workdir / "idp").mkdir()
        make_certificates(self.workdir / "idp", self.env(profile), self.log)

    def _clear(self, profile: str) -> None:
        if self.workdir is not None:
            # The API's and the provider's own output, into this stack's log
            # before they are removed: a sign-in that fails shows the reader
            # only its sso_error code, and the API's log says why.
            self._compose(
                profile, *self.files(), "logs", "--no-color", "api", "idp", timeout=120
            )
        super()._clear(profile)
        if self.workdir is not None:
            shutil.rmtree(self.workdir, ignore_errors=True)
            self.workdir = None


class ComposeEmpty(ComposeDev):
    """compose-dev's stack with no seed data (``SEED=``): one administrator, nothing else."""

    NAME = "compose-empty"
    #: docker-compose.yml's FIRST_SUPERUSER defaults, which are the demo
    #: administrator's; with no seed there is no other account.
    ACCOUNTS: Mapping[str, Tuple[str, str]] = {"admin": DEMO_ACCOUNTS["admin"]}

    def env(self, profile: str) -> Dict[str, str]:
        env = super().env(profile)
        env["SEED"] = ""
        return env


class DocsLocal:
    def __init__(self, repo_root: Path, logs: Path, environ: Mapping[str, str]):
        self.repo_root = repo_root
        self.logs = logs
        self.port = _port(environ, "DOCS_JOURNEY_DOCS_PORT", 0)
        self.environ = dict(environ)
        self.site: Optional[Path] = None
        self.server: Optional[_StaticServer] = None
        self.running: Optional[Running] = None

    def up(self, profile: str = "") -> Running:
        if self.running is not None:
            return self.running
        self.site = Path(tempfile.mkdtemp(prefix="docs-journeys-site-"))
        status = _run_logged(
            [sys.executable, "-m", "mkdocs", "build", "--site-dir", str(self.site)],
            cwd=self.repo_root,
            env=self.environ,
            log=self.logs / "docs-local.log",
            timeout=600,
        )
        if status != 0:
            raise StackError(
                "mkdocs build "
                + ("timed out" if status is None else f"exited {status}")
                + "; its output is in stacks/docs-local.log"
            )
        self.server = _StaticServer(self.site, self.port or free_port())
        self.running = Running(
            name="docs-local", base_url=self.server.start(), source=self.repo_root
        )
        return self.running

    def down(self) -> None:
        if self.server is not None:
            self.server.stop()
        if self.site is not None:
            shutil.rmtree(self.site, ignore_errors=True)
        self.server = self.site = self.running = None


class DocsPublished:
    def __init__(self, repo_root: Path, logs: Path, environ: Mapping[str, str]):
        url = environ.get("DOCS_JOURNEY_PUBLISHED_URL", PUBLISHED_URL)
        if not url.startswith("https://"):
            raise StackError(f"DOCS_JOURNEY_PUBLISHED_URL={url!r} is not an https URL")
        self.url = url if url.endswith("/") else url + "/"
        raw = environ.get("DOCS_JOURNEY_PUBLISHED_SOURCE", "")
        self.source = Path(raw) if raw else repo_root
        self.source_ref = environ.get("DOCS_JOURNEY_PUBLISHED_REF", "")
        if self.source_ref and not SOURCE_REF.fullmatch(self.source_ref):
            raise StackError("DOCS_JOURNEY_PUBLISHED_REF is not a git ref")

    def up(self, profile: str = "") -> Running:
        if not (self.source / "mkdocs.yml").is_file():
            raise StackError(
                f"DOCS_JOURNEY_PUBLISHED_SOURCE={self.source} holds no mkdocs.yml:"
                " it must be the directory the published site was built from"
            )
        return Running(
            name="docs-published",
            base_url=self.url,
            source=self.source,
            source_ref=self.source_ref,
        )

    def down(self) -> None:
        return None


class MarketingLocal:
    def __init__(self, repo_root: Path, logs: Path, environ: Mapping[str, str]):
        self.frontend = repo_root / "frontend"
        self.logs = logs
        self.port = _port(environ, "DOCS_JOURNEY_MARKETING_PORT", 0)
        self.environ = dict(environ)
        self.server: Optional[_StaticServer] = None
        self.running: Optional[Running] = None

    def up(self, profile: str = "") -> Running:
        if self.running is not None:
            return self.running
        status = _run_logged(
            ["npm", "run", "build:marketing"],
            cwd=self.frontend,
            env=self.environ,
            log=self.logs / "marketing-local.log",
            timeout=1200,
        )
        if status != 0:
            raise StackError(
                "npm run build:marketing "
                + ("timed out" if status is None else f"exited {status}")
                + "; its output is in stacks/marketing-local.log"
            )
        self.server = _StaticServer(self.frontend / "out", self.port or free_port())
        self.running = Running(name="marketing-local", base_url=self.server.start())
        return self.running

    def down(self) -> None:
        if self.server is not None:
            self.server.stop()
        self.server = self.running = None


#: demo-local: the guide's start and stop commands, from the checkout's root.
DEMO_SCRIPT = Path("demo") / "setup-local.sh"
DEMO_STOP = Path("demo") / "teardown-local.sh"
#: The host ports ``setup-local.sh`` binds: PostgreSQL, Redis, the API, the
#: dashboard, ShopLab and StreamPulse.
DEMO_PORTS: Tuple[int, ...] = (5432, 6379, 8000, 3100, 3200, 3300)
#: The files the seeds and the script write an API key to: every ``*KEY_FILE``
#: constant of the seeds setup-local.sh runs, and the script's own. A unit test
#: (``test_docs_journey_demo.py``) reads them out of the seeds and the script,
#: so a key file added there and not here fails it.
DEMO_KEY_FILES: Tuple[Path, ...] = (
    Path("demo") / ".demo_api_key",
    Path("demo") / "shoplab" / ".api_key",
    Path("demo") / "streampulse" / ".api_key",
    Path("demo") / "streampulse" / ".api_key_server",
)
#: Where the script's processes write their logs.
DEMO_LOGS = Path("demo") / ".logs"
#: What the guide says the stop command removes.
DEMO_REMOVED: Tuple[Path, ...] = (
    Path("demo") / ".pids",
    DEMO_LOGS,
    Path("demo") / ".demo_api_key",
)
#: The line of the script's box that says it finished.
DEMO_READY = "Ready!"
#: An address in the script's "Ready!" box: ``<label>: http://localhost:<port>``.
DEMO_ADDRESS = re.compile(
    r"(?P<label>[A-Za-z][A-Za-z ]{0,30}):\s+(?P<url>http://localhost:(?P<port>[0-9]{1,5})(?:/[\w./~-]{0,200})?)"
)
#: The administrator's password the script exports for the bootstrap.
DEMO_PASSWORD = re.compile(
    r"^\s*export\s+FIRST_SUPERUSER_PASSWORD=(?:'([^'\n]{1,72})'|\"([^\"\n]{1,72})\"|(\S{1,72}))\s*$",
    re.MULTILINE,
)
#: Terminal colours the script prints.
ANSI = re.compile(r"\x1b\[[0-9;]{0,20}m")
#: The environment that lets demo-local start: a CI runner's, in the demo job.
DEMO_ALLOWED = {"GITHUB_ACTIONS": "true", "DOCS_JOURNEY_DEMO_LOCAL": "1"}


def port_answers(port: int, host: str = "127.0.0.1") -> bool:
    """True when something accepts a TCP connection on *host*:*port*."""
    try:
        with socket.create_connection((host, port), timeout=1):
            return True
    except OSError:
        return False


def demo_refusal(environ: Mapping[str, str]) -> str:
    """Why demo-local may not start in *environ*, or ""."""
    missing = [
        f"{name}={value}"
        for name, value in DEMO_ALLOWED.items()
        if environ.get(name) != value
    ]
    if not missing:
        return ""
    ports = ", ".join(str(port) for port in DEMO_PORTS)
    return (
        f"demo-local runs ./{DEMO_SCRIPT.as_posix()}, which binds host ports {ports}"
        " and installs into the checkout, so it runs only on a CI runner, in the"
        f" docs-journeys demo job; this environment lacks {' and '.join(missing)}"
    )


def ready_addresses(output: str) -> Dict[str, str]:
    """Each ``<label>: http://localhost:<port>`` the script's "Ready!" box lists."""
    plain = ANSI.sub("", output)
    at = plain.rfind(DEMO_READY)
    if at < 0:
        return {}
    found: Dict[str, str] = {}
    for line in plain[at:].splitlines():
        match = DEMO_ADDRESS.search(line)
        if match:
            found[match["label"].strip()] = match["url"]
    return found


def demo_password(script: str) -> str:
    """The ``FIRST_SUPERUSER_PASSWORD`` the script exports, or ""."""
    match = DEMO_PASSWORD.search(script)
    if not match:
        return ""
    return next(group for group in match.groups() if group is not None)


class DemoLocal:
    """``./demo/setup-local.sh`` and ``./demo/teardown-local.sh``, on a CI runner only."""

    NAME = "demo-local"
    #: How long the stop command's processes may take to let go of their ports.
    STOP_WAIT = 30.0

    def __init__(self, repo_root: Path, logs: Path, environ: Mapping[str, str]):
        self.repo_root = repo_root
        self.logs = logs
        self.environ = dict(environ)
        self.timeout = float(environ.get("DOCS_JOURNEY_DEMO_TIMEOUT", "900"))
        self.wait = float(environ.get("DOCS_JOURNEY_DEMO_WAIT", "600"))
        #: The values no file of the run may hold, as soon as they are known
        #: (also after a start that failed part-way).
        self.secrets: List[str] = []
        self.started = False
        self.running: Optional[Running] = None
        #: Why the start failed: every later journey on the stack fails the same way.
        self.failed = ""

    @property
    def log(self) -> Path:
        return self.logs / f"{self.NAME}.log"

    def _redact(self, text: str) -> str:
        for value in sorted(self.secrets, key=len, reverse=True):
            text = text.replace(value, "(redacted)")
        return text

    def _append(self, text: str) -> None:
        self.log.parent.mkdir(parents=True, exist_ok=True)
        with self.log.open("a", encoding="utf-8") as handle:
            handle.write(self._redact(text))

    def _run(self, script: Path, timeout: float) -> Tuple[Optional[int], str]:
        """Run *script* from the checkout's root; its status (None on timeout) and output."""
        with tempfile.TemporaryFile(mode="w+", encoding="utf-8") as output:
            try:
                done = subprocess.run(
                    [f"./{script.as_posix()}"],
                    cwd=self.repo_root,
                    env=self.environ,
                    stdout=output,
                    stderr=subprocess.STDOUT,
                    timeout=timeout,
                    check=False,
                )
                status: Optional[int] = done.returncode
            except subprocess.TimeoutExpired:
                status = None
            except OSError as error:
                self._append(f"$ ./{script.as_posix()}\n(could not start: {error})\n")
                raise StackError(
                    f"./{script.as_posix()} could not be started"
                    f" ({type(error).__name__}); see stacks/{self.log.name}"
                ) from None
            output.seek(0)
            return status, output.read()

    def _register(self) -> None:
        """The password the script sets and every API key file it left."""
        try:
            script = (self.repo_root / DEMO_SCRIPT).read_text(encoding="utf-8")
        except OSError as error:
            raise StackError(
                f"{DEMO_SCRIPT.as_posix()} cannot be read: {error}"
            ) from None
        password = demo_password(script)
        if not password:
            raise StackError(
                f"{DEMO_SCRIPT.as_posix()} exports no FIRST_SUPERUSER_PASSWORD, so"
                " the stack cannot keep the administrator's password out of the run"
            )
        found = [password]
        for path in DEMO_KEY_FILES:
            try:
                value = (self.repo_root / path).read_text(encoding="utf-8").strip()
            except OSError:
                continue
            if value:
                found.append(value)
        for value in found:
            if len(value) < 8:
                raise StackError(
                    "a value the stack must keep out of the run is shorter than 8"
                    " characters, which the scan cannot look for"
                )
            if value not in self.secrets:
                self.secrets.append(value)

    def _copy_logs(self) -> None:
        """Each log the script's processes wrote, values taken out, into stacks/demo-local/."""
        source = self.repo_root / DEMO_LOGS
        if not source.is_dir():
            return
        target = self.logs / self.NAME
        target.mkdir(parents=True, exist_ok=True)
        for path in sorted(source.glob("*.log")):
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            (target / path.name).write_text(self._redact(text), encoding="utf-8")

    def up(self, profile: str = "") -> Running:
        if self.running is not None:
            if profile and self.running.profile != profile:
                raise StackError(
                    f"demo-local serves the {self.running.profile} profile, not {profile}"
                )
            return self.running
        if self.failed:
            raise StackError(self.failed)
        try:
            return self._up(profile)
        except StackError as error:
            self.failed = str(error)
            raise

    def _up(self, profile: str) -> Running:
        refusal = demo_refusal(self.environ)
        if refusal:
            raise StackError(refusal)
        busy = [str(port) for port in DEMO_PORTS if port_answers(port)]
        if busy:
            raise StackError(
                f"port {', '.join(busy)} already answers on this machine; demo-local"
                " starts nothing over what is running"
            )
        self.started = True
        started = time.monotonic()
        status, output = self._run(DEMO_SCRIPT, self.timeout)
        took = time.monotonic() - started
        try:
            self._register()
        finally:
            self._append(f"$ ./{DEMO_SCRIPT.as_posix()}\n{output}")
            self._append(
                f"(./{DEMO_SCRIPT.as_posix()} "
                + ("timed out" if status is None else f"exited {status}")
                + f" after {took:.0f} s)\n"
            )
        if status != 0:
            self._copy_logs()
            raise StackError(
                f"./{DEMO_SCRIPT.as_posix()} "
                + ("timed out" if status is None else f"exited {status}")
                + f" after {took:.0f} s; its output is in stacks/{self.log.name}"
            )
        addresses = ready_addresses(output)
        if not addresses:
            raise StackError(
                f"./{DEMO_SCRIPT.as_posix()} exited 0 without its {DEMO_READY!r} box;"
                f" its output is in stacks/{self.log.name}"
            )
        dashboard = addresses.get("Frontend", "")
        api_docs = addresses.get("API docs", "")
        if not dashboard or not api_docs:
            raise StackError(
                f"the {DEMO_READY!r} box names no Frontend or no API docs address"
            )
        published: Dict[int, str] = {}
        for url in addresses.values():
            match = DEMO_ADDRESS.search(f"x: {url}")
            published[int(match["port"])] = f"http://localhost:{match['port']}"
        for port, url in sorted(published.items()):
            wait_until_answering(url + "/", self.wait)
        api_url = published[int(DEMO_ADDRESS.search(f"x: {api_docs}")["port"])]
        served = ComposeDev._served_profile(api_url)
        if profile and served != profile:
            raise StackError(f"the stack serves the {served} profile, not {profile}")
        self.running = Running(
            name=self.NAME,
            base_url=dashboard.rstrip("/") + "/",
            api_url=api_url,
            profile=served,
            accounts=DEMO_ACCOUNTS,
            published=published,
            up_seconds=took,
            secrets=tuple(self.secrets),
            checkout=self.repo_root,
        )
        return self.running

    def stop_problems(self, status: Optional[int]) -> List[str]:
        """What the stop command left that the guide says it does not."""
        found: List[str] = []
        if status != 0:
            found.append(
                f"./{DEMO_STOP.as_posix()} "
                + ("timed out" if status is None else f"exited {status}")
            )
        deadline = time.monotonic() + self.STOP_WAIT
        answering = list(DEMO_PORTS)
        while answering and time.monotonic() < deadline:
            answering = [port for port in answering if port_answers(port)]
            if answering:
                time.sleep(1)
        if answering:
            found.append(
                "port " + ", ".join(str(port) for port in answering) + " still answers"
            )
        left = [
            path.as_posix() for path in DEMO_REMOVED if (self.repo_root / path).exists()
        ]
        if left:
            found.append(f"{', '.join(left)} still there")
        return found

    def down(self) -> None:
        if not self.started:
            return
        self.started = False
        self.running = None
        self._copy_logs()
        status, output = self._run(DEMO_STOP, 300)
        self._append(f"$ ./{DEMO_STOP.as_posix()}\n{output}")
        problems = self.stop_problems(status)
        if problems:
            self._append("(the stop command left: " + "; ".join(problems) + ")\n")
            raise StackError(
                f"the guide's stop command, ./{DEMO_STOP.as_posix()}, left: "
                + "; ".join(problems)
                + f"; see stacks/{self.log.name}"
            )


STACK_CLASSES = {
    "compose-dev": ComposeDev,
    "compose-empty": ComposeEmpty,
    "compose-sso": ComposeSso,
    "docs-local": DocsLocal,
    "docs-published": DocsPublished,
    "marketing-local": MarketingLocal,
    "demo-local": DemoLocal,
}
