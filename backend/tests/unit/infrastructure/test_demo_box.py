"""The read-only demo box's recipe (deploy/demo/) and its two workflows.

Most of what the recipe does needs Docker, Caddy and a real box, and the
``Demo box`` workflow (.github/workflows/demo-box.yml) is where that runs.
This file pins what can be checked without them:

* ``probe.py``, against a stub of the demo (plain HTTP, and HTTPS with a
  throwaway CA, through ``--connect`` as the box uses it): every check passes
  on a correct demo, and each one fails on the defect it exists for.
* One reset time: the timer is the source; every ``HH:MM UTC`` in the recipe
  says the same.
* The overlay: log rotation on every service, the seed service's shape, the
  demo settings required by name.
* ``generate-env.sh``: writes the demo settings once, refuses a second run and
  a URL that is not ``https://<host>``.
* ``lockdown.sql`` never deactivates a user (the storefront keys' owner must
  stay active).
* ``demo-probe.yml`` says UP or DOWN and nothing else, opens no issue, and is
  dark until DEMO_URL is set; ``demo-box.yml`` runs on everything Compose
  Production runs on, plus the recipe.
"""

from __future__ import annotations

import datetime
import fnmatch
import importlib.util
import io
import json
import os
import re
import shutil
import signal
import ssl
import subprocess
import sys
import threading
import time
from contextlib import redirect_stdout
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Tuple

import pytest
import yaml

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[4]
DEMO = REPO_ROOT / "deploy" / "demo"
WORKFLOWS = REPO_ROOT / ".github" / "workflows"

if not (DEMO / "probe.py").is_file():
    pytest.skip("this tree has no deploy/demo", allow_module_level=True)


def _load_probe() -> Any:
    spec = importlib.util.spec_from_file_location("demo_probe", DEMO / "probe.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["demo_probe"] = module
    spec.loader.exec_module(module)
    return module


probe = _load_probe()

DEMO_ACCOUNT = ("viewer@demo.com", "Demo1234!")
SUPERUSER = ("operator@example.com", "a-long-random-password")
SHOPLAB_KEY = "eptk_" + "1" * 32
STREAMPULSE_KEY = "eptk_" + "2" * 32
REFUSED = {"detail": {"code": "demo_read_only", "message": "read-only demo"}}


# ---------------------------------------------------------------------------
# A stub of the demo: the API on demo.test, the storefronts on
# shoplab.demo.test and streampulse.demo.test, all on one port, routed by the
# Host header. `defects` switches one behaviour at a time to a broken one.
# ---------------------------------------------------------------------------
class StubDemo(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, tls: Optional[ssl.SSLContext] = None) -> None:
        super().__init__(("127.0.0.1", 0), StubHandler)
        self.defects: set = set()
        self.hosts_seen: List[str] = []
        self.requests = 0
        #: Seconds a "hang" request waits before answering (past the timeout).
        self.hang_seconds = 3.0
        #: Once this file exists, every defect is gone: the fake docker
        #: creates it when a reset starts the stack again.
        self.heal_file: Optional[Path] = None
        if tls is not None:
            self.socket = tls.wrap_socket(self.socket, server_side=True)

    @property
    def port(self) -> int:
        return self.server_address[1]


USERS = {
    DEMO_ACCOUNT[0]: (DEMO_ACCOUNT[1], "VIEWER", False),
    "admin@demo.com": ("Demo1234!", "ADMIN", True),
    "dev@demo.com": ("Demo1234!", "DEVELOPER", False),
    "analyst@demo.com": ("Demo1234!", "ANALYST", False),
    SUPERUSER[0]: (SUPERUSER[1], "ADMIN", True),
}


class StubHandler(BaseHTTPRequestHandler):
    server: StubDemo

    def log_message(self, *args: Any) -> None:  # quiet
        pass

    def _send(self, status: int, body: Any, ctype: str = "application/json") -> None:
        raw = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _body(self) -> Dict[str, Any]:
        length = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(length) or b"{}")

    def _route(self, method: str) -> None:
        heal = self.server.heal_file
        defects = set() if heal is not None and heal.exists() else self.server.defects
        self._defects = defects
        self.server.requests += 1
        host = (self.headers.get("Host") or "").split(":")[0]
        self.server.hosts_seen.append(self.headers.get("Host") or "")
        path = self.path.split("?")[0]
        if "hang" in defects:
            # Accepts, then answers only after the client has given up.
            time.sleep(self.server.hang_seconds)
        if host in ("shoplab.demo.test", "streampulse.demo.test"):
            status = 502 if f"{host.split('.')[0]}_down" in defects else 200
            return self._send(status, b"<html>store</html>", "text/html")
        if host != "demo.test":
            return self._send(400, {"detail": "Invalid host header"})
        if "api_503" in defects:
            # What Caddy answers while web is down.
            return self._send(503, {"detail": "The demo is being rebuilt"})
        demo_on = "demo_mode_off" not in defects
        if method == "GET" and path == "/health":
            # The production body (backend/app/core/health.py): no
            # environment, no version.
            body = {"status": "healthy", "profile": "core", "checks": {}}
            if "env_development" in defects:
                body.update(environment="development", version="0.46.0")
            return self._send(200, body)
        if method == "GET" and path == "/":
            if "no_home_redirect" in defects:
                return self._send(200, b"<html>home</html>", "text/html")
            # A Location header on a page that is not a redirect: not followed.
            self.send_response(200 if "home_not_a_redirect" in defects else 302)
            self.send_header("Location", "/login")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return None
        if method == "GET" and path == "/api/v1/auth/config":
            body: Dict[str, Any] = {"provider": "local"}
            if demo_on:
                body["demo_mode"] = "true" if "demo_mode_string" in defects else True
                body["demo_account"] = {
                    "email": DEMO_ACCOUNT[0],
                    "password": DEMO_ACCOUNT[1],
                }
                if "account_email_no_at" in defects:
                    body["demo_account"]["email"] = "viewer.demo.com"
                if "account_email_long" in defects:
                    body["demo_account"]["email"] = "v" * 246 + "@demo.com"
                if "account_password_empty" in defects:
                    body["demo_account"]["password"] = ""
                if "account_password_long" in defects:
                    body["demo_account"]["password"] = "p" * 257
            return self._send(200, body)
        if method == "POST" and path == "/api/v1/auth/login":
            if "login_429" in defects:
                return self._send(429, {"detail": "Too Many Requests"})
            data = self._body()
            user = USERS.get(data.get("email", ""))
            usable = {DEMO_ACCOUNT[0]}
            if "lockdown_skipped" in defects:
                usable.add("admin@demo.com")
            if "superuser_usable" in defects:
                usable.add(SUPERUSER[0])
            if not user or data["email"] not in usable or data["password"] != user[0]:
                return self._send(401, {"detail": "Incorrect email or password"})
            return self._send(
                200,
                {"access_token": "t:" + data["email"], "user": self._me(data["email"])},
            )
        if method == "GET" and path == "/api/v1/auth/me":
            token = (self.headers.get("Authorization") or "").removeprefix("Bearer ")
            if not token.startswith("t:"):
                return self._send(401, {"detail": "Not authenticated"})
            return self._send(200, self._me(token[2:]))
        key = self.headers.get("X-API-Key")
        if path == "/api/v1/tracking/assign" or path.startswith(
            "/api/v1/feature-flags/evaluate/"
        ):
            if (
                key not in (SHOPLAB_KEY, STREAMPULSE_KEY)
                or "key_owner_inactive" in defects
            ):
                return self._send(401, {"detail": "Invalid API key"})
            return self._send(200, {"variant": "control"})
        if method == "POST":
            if "write_accepted" in defects:
                return self._send(201, {"id": "created"})
            if demo_on and "flat_refusal" in defects:
                return self._send(
                    403, {"detail": "read-only demo", "code": "demo_read_only"}
                )
            if demo_on:
                return self._send(403, REFUSED)
            if not self.headers.get("Authorization"):
                return self._send(401, {"detail": "Not authenticated"})
            return self._send(422, {"detail": [{"msg": "Field required"}]})
        return self._send(404, {"detail": "Not Found"})

    def _me(self, email: str) -> Dict[str, Any]:
        _, role, superuser = USERS[email]
        if email == DEMO_ACCOUNT[0]:
            if "demo_account_admin_role" in self._defects:
                role = "ADMIN"
            if "demo_account_superuser" in self._defects:
                superuser = True
        return {"email": email, "role": role, "is_superuser": superuser}

    def do_GET(self) -> None:
        self._route("GET")

    def do_POST(self) -> None:
        self._route("POST")


@pytest.fixture
def stub() -> Iterator[StubDemo]:
    server = StubDemo()
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()


def _env_file(tmp_path: Path, url: str, **overrides: str) -> Path:
    values = {
        "PUBLIC_BASE_URL": url,
        "DEMO_ACCOUNT_EMAIL": DEMO_ACCOUNT[0],
        "DEMO_ACCOUNT_PASSWORD": DEMO_ACCOUNT[1],
        "FIRST_SUPERUSER": SUPERUSER[0],
        "FIRST_SUPERUSER_PASSWORD": SUPERUSER[1],
        "SHOPLAB_API_KEY": SHOPLAB_KEY,
        "STREAMPULSE_API_KEY": STREAMPULSE_KEY,
    }
    values.update(overrides)
    path = tmp_path / "demo.env"
    path.write_text("".join(f"{k}={v}\n" for k, v in values.items()))
    return path


def run_probe(args: List[str]) -> Tuple[int, str]:
    out = io.StringIO()
    with redirect_stdout(out):
        rc = probe.main(args)
    return rc, out.getvalue()


RESET_CHECKS = (
    "health home config signin refusal anonymous passwords superuser storefronts sdk"
).split()


def _lib_list(name: str) -> List[str]:
    match = re.search(rf'^{name}="([^"]*)"$', (DEMO / "lib.sh").read_text(), re.M)
    assert match, f"lib.sh has no {name}"
    return match.group(1).split()


class TestProbe:
    def _args(
        self, stub: StubDemo, tmp_path: Path, *checks: str, **env: str
    ) -> List[str]:
        url = f"http://demo.test:{stub.port}"
        return [
            "--env-file",
            str(_env_file(tmp_path, url, **env)),
            "--connect",
            "127.0.0.1",
            *checks,
        ]

    def test_a_correct_demo_passes_every_reset_check(self, stub, tmp_path):
        rc, out = run_probe(self._args(stub, tmp_path, *RESET_CHECKS))
        assert rc == 0, out
        assert f"probe: {len(RESET_CHECKS)} of {len(RESET_CHECKS)} checks passed" in out

    def test_connect_keeps_the_host_name_for_every_request(self, stub, tmp_path):
        """demo.test does not resolve: only --connect reaches the stub, and
        each request still names its own host."""
        rc, out = run_probe(self._args(stub, tmp_path, "health", "storefronts"))
        assert rc == 0, out
        assert set(stub.hosts_seen) == {
            f"demo.test:{stub.port}",
            f"shoplab.demo.test:{stub.port}",
            f"streampulse.demo.test:{stub.port}",
        }

    @pytest.mark.parametrize(
        "defect, failing, rc",
        [
            # WRONG ANSWERS (exit 3): the demo answered, against its promise.
            # demo mode not on (a misspelt setting, or the overlay left out)
            ("demo_mode_off", {"config", "refusal", "anonymous"}, 3),
            # a write accepted
            ("write_accepted", {"refusal", "anonymous"}, 3),
            # the refusal's code outside `detail`, where the dashboard misses it
            ("flat_refusal", {"refusal", "anonymous"}, 3),
            # lockdown skipped: the published admin password signs in
            ("lockdown_skipped", {"passwords"}, 3),
            ("superuser_usable", {"superuser"}, 3),
            # the demo account is not a plain viewer: the role, or the flag
            ("demo_account_admin_role", {"signin", "refusal"}, 3),
            ("demo_account_superuser", {"signin", "refusal"}, 3),
            # the API runs outside production (a weak SECRET_KEY is accepted)
            ("env_development", {"health"}, 3),
            # /auth/config that the dashboard would read as off, or whose
            # account it would not offer (PR B's checks of the same fields)
            ("demo_mode_string", {"config"}, 3),
            ("account_email_no_at", {"config"}, 3),
            ("account_email_long", {"config"}, 3),
            ("account_password_empty", {"config"}, 3),
            ("account_password_long", {"config"}, 3),
            # NO ANSWER (exit 1): busy, down, or broken without breaking a promise.
            # the demo host answering 503, as Caddy does while web is down
            ("api_503", set(RESET_CHECKS) - {"storefronts"}, 1),
            # a rate-limited sign-in is not a refused one
            ("login_429", {"signin", "refusal", "passwords", "superuser"}, 1),
            # lockdown deactivated the storefront keys' owner
            ("key_owner_inactive", {"sdk"}, 1),
            ("shoplab_down", {"storefronts"}, 1),
            # / shows the dashboard's home page instead of going to sign-in
            ("no_home_redirect", {"home"}, 1),
            ("home_not_a_redirect", {"home"}, 1),
        ],
    )
    def test_each_defect_fails_its_check(self, stub, tmp_path, defect, failing, rc):
        stub.defects.add(defect)
        got, out = run_probe(self._args(stub, tmp_path, *RESET_CHECKS))
        assert got == rc, out
        failed = set(re.findall(r"^(?:FAIL|WRONG) (\w+):", out, re.M))
        assert failed == failing, out
        # exit 3 exactly when some check printed WRONG
        assert bool(re.search(r"^WRONG ", out, re.M)) == (rc == 3), out

    def test_a_demo_that_never_answers_is_no_answer(self, stub, tmp_path):
        """Accepted connections that answer only after the timeout: exit 1,
        never 3, so the box rebuilds rather than staying closed."""
        stub.defects.add("hang")
        rc, out = run_probe(
            self._args(
                stub, tmp_path, "--timeout", "1", "health", "signin", "passwords"
            )
        )
        assert rc == 1, out
        assert len(re.findall(r"^FAIL \w+: .*timed out", out, re.M)) == 3, out

    def test_the_hourly_checks_are_c_to_g(self):
        """EMD-4: the box checks sign-in, both refusals and the passwords
        every hour; the rebuild adds health, config, storefronts and sdk."""
        hourly = _lib_list("DEMO_HOURLY_CHECKS")
        reset = _lib_list("DEMO_RESET_CHECKS")
        assert hourly == ["signin", "refusal", "anonymous", "passwords", "superuser"]
        assert reset == RESET_CHECKS
        assert set(hourly) <= set(reset) <= set(probe.CHECKS)

    def test_an_unknown_check_is_a_usage_error_not_a_skip(self, stub, tmp_path):
        rc, out = run_probe(self._args(stub, tmp_path, "health", "helth"))
        assert rc == 2
        assert "ok" not in out

    def test_no_check_is_a_usage_error(self, stub, tmp_path):
        rc, _ = run_probe(self._args(stub, tmp_path))
        assert rc == 2

    def test_a_missing_value_fails_naming_it(self, stub, tmp_path):
        rc, out = run_probe(
            self._args(stub, tmp_path, "superuser", FIRST_SUPERUSER_PASSWORD="")
        )
        assert rc == 1
        assert "needs FIRST_SUPERUSER_PASSWORD" in out

    def test_without_an_env_file_it_signs_in_with_the_published_account(self, stub):
        """The public workflow's mode: only --demo-url."""
        checks = "health home config signin refusal anonymous passwords storefronts"
        rc, out = run_probe(
            ["--demo-url", f"http://demo.test:{stub.port}", "--connect", "127.0.0.1"]
            + checks.split()
        )
        assert rc == 0, out

    @pytest.mark.parametrize(
        "defect",
        [
            "demo_mode_string",
            "account_email_no_at",
            "account_email_long",
            "account_password_empty",
            "account_password_long",
        ],
    )
    def test_config_alone_refuses_what_the_dashboard_would_not_offer(
        self, stub, defect
    ):
        """In the public mode there is no env file to compare the address
        with, so the bounds on the published account do all the work."""
        stub.defects.add(defect)
        rc, out = run_probe(
            ["--demo-url", f"http://demo.test:{stub.port}", "--connect", "127.0.0.1"]
            + ["config"]
        )
        assert rc == 3, out
        assert re.search(r"^WRONG config: ", out, re.M), out

    def test_tls_needs_https(self, stub, tmp_path):
        rc, out = run_probe(self._args(stub, tmp_path, "tls"))
        assert rc == 1
        assert "is not https" in out


# ---------------------------------------------------------------------------
# HTTPS, as on the box: a CA the probe is told to trust (--cafile), one
# certificate for the three names, SNI from the URL, the address from
# --connect. The tls check passes with 30 days left and fails with 10.
# ---------------------------------------------------------------------------
def _tls_material(tmp_path: Path, days: int) -> Tuple[Path, ssl.SSLContext]:
    crypto = pytest.importorskip("cryptography")
    del crypto
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    now = datetime.datetime.now(datetime.timezone.utc)
    ca_key = ec.generate_private_key(ec.SECP256R1())
    ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "probe test CA")])
    ca = (
        x509.CertificateBuilder()
        .subject_name(ca_name)
        .issuer_name(ca_name)
        .public_key(ca_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=365))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=False,
                content_commitment=False,
                key_encipherment=False,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=True,
                crl_sign=True,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .add_extension(
            x509.SubjectKeyIdentifier.from_public_key(ca_key.public_key()),
            critical=False,
        )
        .sign(ca_key, hashes.SHA256())
    )
    key = ec.generate_private_key(ec.SECP256R1())
    names = ["demo.test", "shoplab.demo.test", "streampulse.demo.test"]
    leaf = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, names[0])]))
        .issuer_name(ca_name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=days))
        .add_extension(
            x509.SubjectAlternativeName([x509.DNSName(n) for n in names]),
            critical=False,
        )
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(
            x509.ExtendedKeyUsage([x509.oid.ExtendedKeyUsageOID.SERVER_AUTH]),
            critical=False,
        )
        .add_extension(
            x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()),
            critical=False,
        )
        .sign(ca_key, hashes.SHA256())
    )
    ca_pem = tmp_path / "ca.pem"
    ca_pem.write_bytes(ca.public_bytes(serialization.Encoding.PEM))
    chain = tmp_path / "leaf.pem"
    chain.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
        + leaf.public_bytes(serialization.Encoding.PEM)
    )
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(chain)
    return ca_pem, context


@pytest.fixture
def tls_stub(tmp_path, request) -> Iterator[Tuple[StubDemo, Path]]:
    ca_pem, context = _tls_material(tmp_path, request.param)
    server = StubDemo(tls=context)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server, ca_pem
    finally:
        server.shutdown()
        server.server_close()


class TestProbeOverTls:
    def _args(self, server: StubDemo, ca: Optional[Path], tmp_path: Path, *checks: str):
        url = f"https://demo.test:{server.port}"
        args = ["--env-file", str(_env_file(tmp_path, url)), "--connect", "127.0.0.1"]
        if ca is not None:
            args += ["--cafile", str(ca)]
        return args + list(checks)

    @pytest.mark.parametrize("tls_stub", [30], indirect=True)
    def test_every_check_passes_over_https(self, tls_stub, tmp_path):
        server, ca = tls_stub
        rc, out = run_probe(self._args(server, ca, tmp_path, *RESET_CHECKS, "tls"))
        assert rc == 0, out

    @pytest.mark.parametrize("tls_stub", [10], indirect=True)
    def test_a_certificate_near_expiry_fails_tls(self, tls_stub, tmp_path):
        server, ca = tls_stub
        rc, out = run_probe(self._args(server, ca, tmp_path, "health", "tls"))
        assert rc == 1, out
        assert re.search(r"^FAIL tls: .*certificate expires in", out, re.M), out
        assert re.search(r"^ok   health", out, re.M), out

    @pytest.mark.parametrize("tls_stub", [30], indirect=True)
    def test_an_untrusted_certificate_fails(self, tls_stub, tmp_path):
        server, _ = tls_stub
        rc, out = run_probe(self._args(server, None, tmp_path, "health"))
        assert rc == 1
        assert "CERTIFICATE_VERIFY_FAILED" in out


# ---------------------------------------------------------------------------
# What check.sh and reset.sh do with each answer, run for real under bash 4+
# against the stub: `docker`, `flock` and `sleep` are fakes that log their
# calls, and the fake docker heals the stub when a reset starts the stack
# again (`up ... --wait-timeout 900`), as a rebuild would.
# ---------------------------------------------------------------------------
FAKE_DOCKER = r"""#!/usr/bin/env bash
echo "docker $*" >>"$FAKE_LOG"
case " $* " in
    *" config --format json "*) echo '{"services": {"api": {"image": "api:test"}}}' ;;
    *" exec -T postgres "*) cat >/dev/null ;;
    *" stop "*)
        if [ "$1" = compose ]; then exit "${FAKE_COMPOSE_STOP_RC:-0}"; fi ;;
    *" ps -q "*)
        if [ "${FAKE_PS_RC:-0}" != 0 ]; then exit "$FAKE_PS_RC"; fi
        case " $* " in *"service=web "*) echo cid-web ;; esac ;;
    *" --wait-timeout 900 "*) : >"$FAKE_HEAL" ;;
esac
exit 0
"""
FAKE_LOGGER = """#!/usr/bin/env bash
echo "%s $*" >>"$FAKE_LOG"
exit 0
"""


def _bash4() -> Optional[str]:
    for candidate in (shutil.which("bash"), "/opt/homebrew/bin/bash", "/usr/bin/bash"):
        if candidate and os.path.exists(candidate):
            out = subprocess.run(
                [candidate, "-c", "echo ${BASH_VERSINFO[0]}"],
                capture_output=True,
                text=True,
            ).stdout.strip()
            if out.isdigit() and int(out) >= 4:
                return candidate
    return None


BASH4 = _bash4()


@pytest.mark.skipif(BASH4 is None, reason="needs bash 4 or later")
class TestTheBoxScripts:
    @pytest.fixture
    def box(self, stub, tmp_path):
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        (bin_dir / "docker").write_text(FAKE_DOCKER)
        for name in ("flock", "sleep"):
            (bin_dir / name).write_text(FAKE_LOGGER % name)
        for name in ("docker", "flock", "sleep"):
            (bin_dir / name).chmod(0o755)
        (bin_dir / "bash").symlink_to(BASH4)
        (bin_dir / "python3").symlink_to(sys.executable)
        state = tmp_path / "state"
        log = tmp_path / "calls.log"
        log.write_text("")
        stub.heal_file = tmp_path / "healed"
        env = {
            "PATH": f"{bin_dir}:{os.environ.get('PATH', '/usr/bin:/bin')}",
            "HOME": str(tmp_path),
            "DEMO_RELEASE": str(REPO_ROOT),
            "DEMO_ENV_FILE": str(_env_file(tmp_path, f"http://demo.test:{stub.port}")),
            "DEMO_STATE_DIR": str(state),
            "DEMO_PROBE_TIMEOUT": "1",
            "FAKE_LOG": str(log),
            "FAKE_HEAL": str(stub.heal_file),
        }

        class Box:
            def __init__(self):
                self.stub, self.state, self.log, self.env = stub, state, log, env

            def run(
                self, script: str, recipe: Path = DEMO, **extra: str
            ) -> subprocess.CompletedProcess:
                return subprocess.run(
                    [BASH4, str(recipe / script)],
                    env={**self.env, **extra},
                    capture_output=True,
                    text=True,
                    stdin=subprocess.DEVNULL,
                    timeout=120,
                )

            def start(self, script: str) -> subprocess.Popen:
                """In its own process group, as systemd runs a unit."""
                return subprocess.Popen(
                    [BASH4, str(DEMO / script)],
                    env=self.env,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    stdin=subprocess.DEVNULL,
                    text=True,
                    start_new_session=True,
                )

            def calls(self) -> List[str]:
                return self.log.read_text().splitlines()

        return Box()

    @staticmethod
    def _rebuilt(calls: List[str]) -> bool:
        return any(" down -v " in f"{c} " for c in calls) and any(
            "--wait-timeout 900" in c for c in calls
        )

    @pytest.mark.parametrize("defect", ["hang", "api_503"])
    def test_no_answer_twice_rebuilds_and_reopens(self, box, defect):
        """A demo that times out or answers 503 is rebuilt by the hourly
        check at once (reset.sh, by exec, keeping the lock) and is open
        again when the rebuilt demo's probe passes."""
        box.stub.defects.add(defect)
        result = box.run("check.sh")
        calls = box.calls()
        assert result.returncode == 0, result.stderr
        assert "sleep 65" in calls, calls
        assert self._rebuilt(calls), calls
        # the rebuild ends with the stack started, not stopped
        assert "--wait-timeout 900" in calls[-1], calls
        assert (box.state / "last-reset").exists()
        assert not (box.state / "closed-wrong").exists()
        # reset.sh kept check.sh's lock instead of taking it again
        assert [c for c in calls if c.startswith("flock")] == ["flock 9"], calls

    def test_a_wrong_answer_closes_and_stays_closed_until_a_reset_passes(self, box):
        box.stub.defects.add("write_accepted")
        first = box.run("check.sh")
        calls = box.calls()
        assert first.returncode == 3, first.stderr
        assert "sleep 65" not in calls, "a wrong answer is never retried"
        assert not self._rebuilt(calls), calls
        assert any(" stop web shoplab streampulse" in c for c in calls), calls
        assert (box.state / "closed-wrong").exists()

        # The next hourly check does not probe or reopen: it keeps it closed.
        seen = box.stub.requests
        again = box.run("check.sh")
        assert again.returncode == 3, again.stderr
        assert box.stub.requests == seen
        assert not self._rebuilt(box.calls())
        assert (box.state / "closed-wrong").exists()

        # A reset whose probe passes reopens it and clears the marker.
        box.stub.defects.clear()
        reset = box.run("reset.sh")
        assert reset.returncode == 0, reset.stderr
        assert not (box.state / "closed-wrong").exists()
        assert (box.state / "last-reset").exists()

    def test_a_reset_with_a_wrong_answer_closes_at_once(self, box):
        box.stub.defects.add("write_accepted")
        box.stub.heal_file = None  # the rebuild does not fix it
        result = box.run("reset.sh")
        calls = box.calls()
        assert result.returncode == 3, result.stderr
        assert "sleep 65" not in calls
        assert calls[-1].endswith(" stop web shoplab streampulse"), calls
        assert (box.state / "closed-wrong").exists()
        assert not (box.state / "last-reset").exists()

    def test_when_compose_cannot_stop_them_the_labels_do(self, box):
        box.stub.defects.add("write_accepted")
        result = box.run("check.sh", FAKE_COMPOSE_STOP_RC="1")
        calls = box.calls()
        assert result.returncode == 3, result.stderr
        assert any(
            "ps -q --filter label=com.docker.compose.project=controlvar-demo"
            " --filter label=com.docker.compose.service=web" in c
            for c in calls
        ), calls
        assert "docker stop cid-web" in calls, calls
        assert "stopped by label" in result.stderr

    def test_a_close_that_cannot_stop_anything_says_so(self, box):
        box.stub.defects.add("write_accepted")
        result = box.run("check.sh", FAKE_COMPOSE_STOP_RC="1", FAKE_PS_RC="1")
        assert result.returncode != 0
        assert "stop them by hand" in result.stderr
        assert (box.state / "closed-wrong").exists()
        # Not marked closed, so the EXIT trap tries once more.
        stops = [c for c in box.calls() if c.endswith(" stop web shoplab streampulse")]
        assert len(stops) == 2, box.calls()

    def test_a_reset_that_cannot_run_still_closes(self, box, tmp_path):
        """reset.sh missing: the exec fails. bash ends a shell on a failed exec
        with no EXIT trap unless execfail is set and errexit is off; then
        check.sh says so and closes."""
        recipe = tmp_path / "recipe"
        shutil.copytree(DEMO, recipe)
        (recipe / "reset.sh").unlink()
        box.stub.defects.add("api_503")
        result = box.run("check.sh", recipe=recipe)
        calls = box.calls()
        assert result.returncode != 0, result.stderr
        assert "could not run" in result.stderr, result.stderr
        assert calls[-1].endswith(" stop web shoplab streampulse"), calls
        assert not self._rebuilt(calls)

    @pytest.mark.parametrize("script", ["check.sh", "reset.sh"])
    def test_a_terminated_run_closes(self, box, script):
        """systemd stops a unit (or ends it at TimeoutStartSec) with SIGTERM
        to its group. Sent while the probe runs, the run still closes."""
        box.stub.defects.add("hang")
        box.stub.heal_file = None
        proc = box.start(script)
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            if script == "check.sh":
                probing = box.stub.requests > 0
            else:
                probing = any("--wait-timeout 900" in c for c in box.calls())
            if probing:
                break
            time.sleep(0.05)
        else:
            proc.kill()
            pytest.fail("the probe never started")
        time.sleep(0.3)
        os.killpg(proc.pid, signal.SIGTERM)
        _, stderr = proc.communicate(timeout=60)
        calls = box.calls()
        assert proc.returncode == 143, (proc.returncode, stderr)
        assert calls[-1].endswith(" stop web shoplab streampulse"), calls
        assert not (box.state / "last-reset").exists()

    def test_the_overlay_names_the_project_close_falls_back_on(self):
        lib = (DEMO / "lib.sh").read_text()
        match = re.search(r"^DEMO_PROJECT=(\S+)$", lib, re.M)
        assert match and match.group(1) == _yaml(DEMO / "compose.demo.yml")["name"]


# ---------------------------------------------------------------------------
# One reset time (PLAN C3, UX check 9)
# ---------------------------------------------------------------------------
def _reset_time() -> str:
    timer = (DEMO / "systemd" / "controlvar-demo-reset.timer").read_text()
    match = re.search(r"^OnCalendar=\*-\*-\* (\d\d:\d\d):00 UTC$", timer, re.M)
    assert match, "the reset timer's OnCalendar is not '*-*-* HH:MM:00 UTC'"
    return match.group(1)


def test_every_reset_time_in_the_recipe_is_the_timers():
    reset = _reset_time()
    found = {}
    for path in sorted(DEMO.rglob("*")):
        # The timers are schedules, the source above, not prose.
        if path.is_file() and path.suffix != ".pyc" and path.parent.name != "systemd":
            for match in re.finditer(r"(?<![\d:])(\d\d:\d\d) UTC\b", path.read_text()):
                found.setdefault(match.group(1), []).append(path.name)
    assert reset in found, "no file names the reset time"
    assert set(found) == {reset}, found
    # The rebuild page and the runbook both say it.
    assert {"Caddyfile", "README.md"} <= set(found[reset])


#: The dashboard's demo banner says this time (DEMO_RESET_TIME in the
#: dashboard's demo-mode module). The box's timer must say the same.
DASHBOARD_RESET_TIME = "08:00"


def test_the_reset_time_is_the_one_the_dashboard_shows():
    assert _reset_time() == DASHBOARD_RESET_TIME
    # Once the dashboard's constant is in the tree, it is read, not trusted.
    defined = [
        path
        for path in sorted((REPO_ROOT / "frontend" / "src").rglob("*.ts"))
        if "node_modules" not in path.parts
        and re.search(r"\bDEMO_RESET_TIME\s*=", path.read_text(errors="replace"))
    ]
    for path in defined:
        match = re.search(
            r"\bDEMO_RESET_TIME\s*=\s*['\"](\d\d:\d\d) UTC['\"]", path.read_text()
        )
        assert match, f"{path}: DEMO_RESET_TIME is not 'HH:MM UTC'"
        assert match.group(1) == _reset_time(), path


# ---------------------------------------------------------------------------
# The Caddyfile
# ---------------------------------------------------------------------------
CADDYFILE = (DEMO / "Caddyfile").read_text()
DEMO_HOST = "demo.controlvar.com"


def _site_blocks() -> Dict[str, str]:
    """Site address -> its block, for the top-level blocks that are sites."""
    blocks = {}
    for match in re.finditer(r"^([a-z0-9.-]+) \{\n(.*?)^\}", CADDYFILE, re.M | re.S):
        blocks[match.group(1)] = match.group(2)
    return blocks


def test_the_sites_are_exactly_the_demo_host_and_its_two_storefronts():
    """The dashboard links to shoplab.<its host> and streampulse.<its host>,
    so it must be served at exactly the host generate-env.sh writes."""
    assert set(_site_blocks()) == {
        DEMO_HOST,
        f"shoplab.{DEMO_HOST}",
        f"streampulse.{DEMO_HOST}",
    }
    script = (DEMO / "generate-env.sh").read_text()
    assert f"public_base_url=${{PUBLIC_BASE_URL:-https://{DEMO_HOST}}}" in script


def test_the_demo_host_sends_only_its_root_to_sign_in():
    blocks = _site_blocks()
    assert re.search(r"^\tredir / /login$", blocks[DEMO_HOST], re.M)
    for host, block in blocks.items():
        if host != DEMO_HOST:
            assert "redir" not in block, host


def test_the_demo_host_does_not_offer_the_per_user_flag_list():
    block = _site_blocks()[DEMO_HOST]
    assert re.search(r"^\trespond /api/v1/feature-flags/user/\* 404$", block, re.M)


def test_every_site_imports_the_gate_and_the_rebuild_page():
    """The allow-list and the rebuild page are snippets: a site that does not
    import them is open during the rehearsal, or shows Caddy's bare 502."""
    blocks = _site_blocks()
    assert len(blocks) == 3
    for host, block in blocks.items():
        assert re.search(r"^\timport gate$", block, re.M), host
        assert re.search(r"^\timport rebuilding$", block, re.M), host


def test_the_api_rebuild_answer_is_json_with_a_detail():
    match = re.search(r"handle @api \{.*?respond `([^`]*)` 503", CADDYFILE, re.S)
    assert match, "no 503 answer for /api/ in the rebuilding snippet"
    assert isinstance(json.loads(match.group(1))["detail"], str)
    assert re.search(r"header Retry-After \d+", CADDYFILE)


def test_the_storefronts_open_dashboard_link_is_the_demo_experiments_page():
    """The panels add the page to the dashboard URL they are built with, so
    the build argument is the origin: https://demo.controlvar.com/experiments
    is what a visitor follows (with generate-env.sh's default URL)."""
    services = _yaml(OVERLAY)["services"]
    for app in ("shoplab", "streampulse"):
        args = services[app]["build"]["args"]
        assert args["NEXT_PUBLIC_CONTROLVAR_DASHBOARD_URL"] == (
            "${PUBLIC_BASE_URL:?required}"
        ), app
        panel = (
            REPO_ROOT / "demo" / app / "src/components/ControlvarPanel.tsx"
        ).read_text()
        assert "href={`${DASHBOARD_URL}/experiments`}" in panel, app


# ---------------------------------------------------------------------------
# The overlay
# ---------------------------------------------------------------------------
def _yaml(path: Path) -> Dict[str, Any]:
    return yaml.safe_load(path.read_text())


OVERLAY = DEMO / "compose.demo.yml"
DEMO_SETTINGS = (
    "DEMO_MODE",
    "DEMO_ACCOUNT_EMAIL",
    "DEMO_ACCOUNT_PASSWORD",
    "ALLOWED_HOSTS",
    "CORS_ORIGINS",
    "SDK_RATE_LIMIT_PER_MINUTE",
)


def test_every_service_rotates_its_log():
    base = _yaml(REPO_ROOT / "deploy" / "compose" / "compose.yml")["services"]
    overlay = _yaml(OVERLAY)["services"]
    assert set(base) <= set(overlay), "a production service the overlay leaves alone"
    for name, service in overlay.items():
        assert service.get("logging") == {
            "driver": "json-file",
            "options": {"max-size": "10m", "max-file": "3"},
        }, name


def test_the_api_requires_each_demo_setting_by_name():
    env = _yaml(OVERLAY)["services"]["api"]["environment"]
    for name in DEMO_SETTINGS:
        if name != "DEMO_MODE":
            assert re.fullmatch(rf"\$\{{{name}:\?[^}}]+\}}", env[name]), name


def test_the_overlay_turns_demo_mode_on_itself():
    """Literal "true": never read from .env, so an empty or missing value
    cannot reach the API through the shared compose file."""
    assert _yaml(OVERLAY)["services"]["api"]["environment"]["DEMO_MODE"] == "true"
    assert "${DEMO_MODE" not in OVERLAY.read_text()
    assert "DEMO_MODE" not in (DEMO / "generate-env.sh").read_text()


def test_only_the_seed_runs_in_development():
    """DEMO_MODE is not tied to ENVIRONMENT, and development accepts a weak
    SECRET_KEY: the API keeps the production file's `production`."""
    base = _yaml(REPO_ROOT / "deploy" / "compose" / "compose.yml")["services"]
    assert base["api"]["environment"]["ENVIRONMENT"] == "production"
    overlay = _yaml(OVERLAY)["services"]
    setting = {
        name: (service.get("environment") or {}).get("ENVIRONMENT")
        for name, service in overlay.items()
    }
    assert {name: value for name, value in setting.items() if value} == {
        "seed": "development"
    }


def test_the_seed_service_seeds_once_and_never_serves():
    seed = _yaml(OVERLAY)["services"]["seed"]
    env = seed["environment"]
    assert seed["profiles"] == ["seed"]
    assert seed["command"] == ["true"]
    assert env["ENVIRONMENT"] == "development"
    assert env["SEED"] == "demo,shoplab,streampulse"
    assert "demo_keys:/data/demo" in seed["volumes"]
    assert "ports" not in seed
    for name in DEMO_SETTINGS[:3]:
        assert name not in env, f"the seed run needs no {name}"


def test_the_production_file_names_no_demo_setting():
    """The overlay adds them; deploy/compose/compose.yml stays as it was."""
    text = (REPO_ROOT / "deploy" / "compose" / "compose.yml").read_text()
    assert "DEMO_" not in text


# ---------------------------------------------------------------------------
# generate-env.sh
# ---------------------------------------------------------------------------
needs_openssl = pytest.mark.skipif(
    shutil.which("openssl") is None or shutil.which("sh") is None,
    reason="needs sh and openssl",
)


def _generate(tmp_path: Path, url: Optional[str]) -> subprocess.CompletedProcess:
    env = {k: v for k, v in os.environ.items() if k not in ("PUBLIC_BASE_URL",)}
    env["ENV_FILE"] = str(tmp_path / "demo.env")
    if url is not None:
        env["PUBLIC_BASE_URL"] = url
    return subprocess.run(
        ["sh", str(DEMO / "generate-env.sh")],
        env=env,
        capture_output=True,
        text=True,
        stdin=subprocess.DEVNULL,
        timeout=60,
    )


@needs_openssl
def test_generate_env_writes_the_demo_settings_once(tmp_path):
    first = _generate(tmp_path, None)
    assert first.returncode == 0, first.stderr
    target = tmp_path / "demo.env"
    text = target.read_text()
    assert oct(target.stat().st_mode & 0o777) == "0o600"
    lines = set(text.splitlines())
    assert {
        "PUBLIC_BASE_URL=https://demo.controlvar.com",
        "DEMO_ACCOUNT_EMAIL=viewer@demo.com",
        "ALLOWED_HOSTS=demo.controlvar.com,api",
        "CORS_ORIGINS=https://shoplab.demo.controlvar.com,https://streampulse.demo.controlvar.com",
        "SDK_RATE_LIMIT_PER_MINUTE=120",
        "POSTGRES_USER=controlvar",
    } <= lines
    keys = re.findall(
        r"^(?:SHOPLAB|STREAMPULSE)_API_KEY=(eptk_[0-9a-f]{32})$", text, re.M
    )
    assert len(set(keys)) == 2
    for name in DEMO_SETTINGS:
        if name != "DEMO_MODE":
            assert re.search(rf"^{name}=.+$", text, re.M), name
    assert not re.search(r"^DEMO_MODE=", text, re.M), "the overlay sets it"
    second = _generate(tmp_path, None)
    assert second.returncode == 1
    assert "not overwriting" in second.stderr
    assert target.read_text() == text
    assert [p.name for p in tmp_path.iterdir()] == ["demo.env"]


@needs_openssl
@pytest.mark.parametrize(
    "url",
    [
        "http://demo.example.com",
        "https://Demo.example.com",
        "https://demo.example.com:8443",
        "https://demo.example.com/",
        "https://",
    ],
)
def test_generate_env_refuses_anything_but_https_and_a_host(tmp_path, url):
    result = _generate(tmp_path, url)
    assert result.returncode == 1, result.stdout
    assert list(tmp_path.iterdir()) == []


# ---------------------------------------------------------------------------
# lockdown.sql (the CI boot job runs it; here, what it must never do)
# ---------------------------------------------------------------------------
def test_lockdown_never_deactivates_a_user():
    """The storefront and simulator keys belong to admin@demo.com, and the API
    refuses a key whose owner is inactive (PED-1)."""
    sql = re.sub(r"--[^\n]*", "", (DEMO / "lockdown.sql").read_text())
    assert not re.search(r"\bis_active\s*=", sql, re.I)
    assert re.search(r"SET\s+hashed_password\s*=\s*'!'", sql)
    assert "RAISE EXCEPTION" in sql


def test_a_refused_lockdown_fails_the_rebuild():
    """Without ON_ERROR_STOP psql exits 0 after the RAISE: the transaction
    rolls back, every seeded password stays usable, and the rebuild goes on.
    Both places carry it, each enough alone."""
    sql = (DEMO / "lockdown.sql").read_text()
    assert re.search(r"^\\set ON_ERROR_STOP on$", sql, re.M)
    lib = (DEMO / "lib.sh").read_text()
    call = re.search(
        r"'psql [^']*' \\\n\s*\"\$demo_email\" <\"\$DEMO_RECIPE/lockdown.sql\"", lib
    )
    assert call, "lib.sh no longer feeds lockdown.sql to psql as expected"
    assert " -v ON_ERROR_STOP=1 " in call.group(0)


# ---------------------------------------------------------------------------
# The workflows
# ---------------------------------------------------------------------------
def _on(workflow: Dict[str, Any]) -> Dict[str, Any]:
    # PyYAML reads the bare key `on` as the boolean True.
    return workflow.get("on") or workflow.get(True)


class TestDemoProbeWorkflow:
    wf = _yaml(WORKFLOWS / "demo-probe.yml")

    def test_dark_until_demo_url_is_set(self):
        (job,) = self.wf["jobs"].values()
        assert job["if"] == "vars.DEMO_URL != ''"
        assert set(_on(self.wf)) == {"schedule", "workflow_dispatch"}

    def test_it_can_write_nothing(self):
        assert self.wf["permissions"] == {"contents": "read"}
        for job in self.wf["jobs"].values():
            assert "permissions" not in job
        text = (WORKFLOWS / "demo-probe.yml").read_text()
        assert "gh issue" not in text and "gh api" not in text

    def test_it_prints_up_or_down_and_nothing_from_the_probe(self):
        (job,) = self.wf["jobs"].values()
        runs = [step["run"] for step in job["steps"] if "run" in step]
        assert len(runs) == 1
        run = runs[0]
        assert "> /dev/null 2>&1" in run
        echoed = re.findall(r"echo (.+)", run)
        assert echoed == ['"UP"', '"::error title=Demo probe::DOWN"']

    def test_it_runs_only_checks_a_visitor_can_make(self):
        """No check that needs the box's secrets, and nothing it could not
        make from outside."""
        run = next(s["run"] for s in self.wf["jobs"]["probe"]["steps"] if "run" in s)
        named = re.search(r"\n\s+((?:[a-z]+ ?)+)\\?\n\s+> /dev/null", run)
        assert named, run
        checks = named.group(1).split()
        assert set(checks) <= set(probe.CHECKS) - {"superuser", "sdk"}
        assert {"config", "refusal", "anonymous", "passwords"} <= set(checks)


class TestDemoBoxWorkflow:
    wf = _yaml(WORKFLOWS / "demo-box.yml")
    production = _yaml(WORKFLOWS / "compose-production.yml")

    #: PED-7: Compose Production's triggers for the code the box runs, plus
    #: the recipe.
    REQUIRED_PATHS = {"backend/**", "frontend/**", "deploy/compose/**", "VERSION"}

    @pytest.mark.parametrize("event", ["pull_request", "push"])
    def test_it_runs_on_what_compose_production_runs_on_and_the_recipe(self, event):
        mine = set(_on(self.wf)[event]["paths"])
        theirs = set(_on(self.production)[event]["paths"])
        assert self.REQUIRED_PATHS <= theirs
        assert self.REQUIRED_PATHS | {"deploy/demo/**"} <= mine

    def test_nightly_and_by_hand(self):
        on = _on(self.wf)
        assert on["schedule"] and "workflow_dispatch" in on

    def test_it_can_write_nothing(self):
        assert self.wf["permissions"] == {"contents": "read"}
        for job in self.wf["jobs"].values():
            assert "permissions" not in job

    def test_the_allow_list_curl_meets_the_gate_not_a_redirect(self):
        """Caddy orders `redir` before `respond` (the gate): a path a demo-host
        `redir` matches answers the redirect, not the gate's 403. The path the
        step curls from outside the allow-list must match no `redir` there."""
        step = next(
            s
            for s in self.wf["jobs"]["demo-box"]["steps"]
            if s.get("name", "").startswith("Caddy refuses other addresses")
        )
        loop = re.search(r"for host in (.+?); do\n(.*?)\n\s*done", step["run"], re.S)
        assert loop, "no host loop in the allow-list step"
        assert len(loop.group(1).split()) == 3
        urls = re.findall(r'"https://\$\{host\}([^"]*)"', loop.group(2))
        assert urls, "the loop curls no https://${host}... URL"
        block = (
            (DEMO / "Caddyfile").read_text().split("\ndemo.controlvar.com {\n", 1)[1]
        )
        block = block.split("\n}\n", 1)[0]
        redirs = re.findall(r"^\s*redir\s+(\S+)(?:\s+\S+)?", block, re.M)
        for path in urls:
            path = path or "/"
            for matcher in redirs:
                if not matcher.startswith("/"):
                    # `redir <to>` with no matcher, or a named matcher: it
                    # matches every path as far as this test can tell.
                    hit = True
                elif "*" in matcher:
                    hit = fnmatch.fnmatchcase(path.lower(), matcher.lower())
                else:
                    # A Caddy path matcher without * is exact.
                    hit = path.lower() == matcher.lower()
                assert not hit, f"{path} is redirected by `redir {matcher}`"

    def test_the_refusal_step_comes_after_everything_that_needs_no_demo_mode(self):
        """A build without demo mode stops at the refusal step; what comes
        before it must not need demo mode, so a red run there says one thing."""
        names = [s.get("name", "") for s in self.wf["jobs"]["demo-box"]["steps"]]
        refusal = names.index("Demo mode is on and writes are refused")
        assert names.index("First boot (seed, lockdown, start)") < refusal
        assert (
            names.index("The published passwords and FIRST_SUPERUSER are refused")
            < refusal
        )
        assert names.index("The storefront keys assign and evaluate") < refusal
        assert refusal < names.index(
            "Reset (1 of 2) serves the rebuild page while it runs"
        )
