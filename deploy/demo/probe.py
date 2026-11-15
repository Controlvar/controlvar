#!/usr/bin/env python3
"""Probe the read-only demo box. Standard library only.

    probe.py [--env-file deploy/demo/.env] [--demo-url URL] [--connect ADDR]
             [--cafile PEM] [--timeout SECONDS] CHECK [CHECK ...]

Runs each named check once, in the order given, and prints one line per check.
The exit status says what the demo did:

    0  every check it was asked for ran and passed.
    1  NO ANSWER: some check got no usable answer (no response, a timeout, a
       5xx, a 429, or anything else that is not a definite answer), and none
       got a wrong one. The demo may be busy or restarting: the callers try
       again, then rebuild (lib.sh).
    3  A WRONG ANSWER: the demo answered, and the answer breaks what it
       promises: a write accepted, a locked password signing in, demo mode
       reported off, or the demo account not a plain viewer. The callers close
       the demo at once and keep it closed until a reset's probe passes.
    2  a usage error: an unknown check, no check at all, or an unreadable env
       file; never a skip. A check that lacks a value it needs fails (1),
       naming the value.

The checks:

    health       GET /health answers 200 with a production body: the API hides
                 its environment and version only under ENVIRONMENT=production
                 (backend/app/core/health.py), so either one shown is a wrong
                 answer. Development accepts a weak SECRET_KEY.
    home         GET / on the demo host redirects to /login (Caddy), so the
                 dashboard's own home page never shows there.
    config       GET /api/v1/auth/config says demo_mode is JSON true and
                 publishes a well-formed demo_account: an email with an '@', at
                 most 254 characters, and a password of 1 to 256 (the env
                 file's DEMO_ACCOUNT_EMAIL, when given).
    signin       The demo account signs in: 200, role VIEWER, not a superuser,
                 and /api/v1/auth/me agrees.
    refusal      A write sent as the signed-in demo account answers 403 with
                 detail.code "demo_read_only".
    anonymous    A write sent with no credentials answers the same 403.
    passwords    admin@, dev@ and analyst@demo.com with the password the seeds
                 publish answer 401.
    superuser    FIRST_SUPERUSER with FIRST_SUPERUSER_PASSWORD answers 401.
    storefronts  https://shoplab.<host>/ and https://streampulse.<host>/ answer
                 200.
    sdk          Each storefront key (SHOPLAB_API_KEY, STREAMPULSE_API_KEY)
                 assigns (POST /api/v1/tracking/assign) and evaluates a flag
                 (GET /api/v1/feature-flags/evaluate/<key>): 200 each.
    tls          Each of the three hosts presents a certificate valid for more
                 than 14 days.

Callers: reset.sh after every rebuild, the box's hourly check (both through
lib.sh, both fail closed), the CI boot job (.github/workflows/demo-box.yml)
and the public up/down probe (.github/workflows/demo-probe.yml), which runs it
with its output discarded.

--connect sends every request to ADDR instead of the address the host name
resolves to, keeping the name for TLS and the Host header (curl's --resolve):
the box probes itself through Caddy on 127.0.0.1.
"""

from __future__ import annotations

import argparse
import http.client
import json
import socket
import ssl
import sys
import time
import urllib.parse
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

#: The password the seeds give every demo account (README, seed_demo_data.py).
PUBLISHED_PASSWORD = "Demo1234!"
#: The seeded accounts that must not sign in on the box.
LOCKED_ACCOUNTS = ("admin@demo.com", "dev@demo.com", "analyst@demo.com")
#: What demo mode answers a refused write with (detail.code).
REFUSAL_CODE = "demo_read_only"
#: One sticky visitor, so the probe adds one assignment per experiment per
#: reset rather than one per run.
PROBE_USER = "controlvar-demo-probe"
#: Seeded keys the sdk check uses, per storefront key.
SDK_TARGETS = (
    ("SHOPLAB_API_KEY", "shoplab_hero_banner", "shoplab_new_search"),
    ("STREAMPULSE_API_KEY", "streampulse_wrapped", "streampulse_recs_v2"),
)
STOREFRONTS = ("shoplab", "streampulse")
MIN_CERT_DAYS = 14
MAX_BODY = 1024 * 1024


class CheckFailed(Exception):
    """No usable answer (exit 1)."""


class WrongAnswer(CheckFailed):
    """A definite answer that breaks what the demo promises (exit 3)."""


def no_answer(response: "Response") -> bool:
    """A status that says nothing about the demo's promises: busy or down."""
    return response.status >= 500 or response.status == 429


@dataclass
class Response:
    status: int
    headers: Dict[str, str]
    body: bytes
    cert: Optional[Dict[str, Any]] = None

    def json(self) -> Any:
        try:
            return json.loads(self.body.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            raise CheckFailed(f"answered {self.status} with a body that is not JSON")


@dataclass
class Probe:
    demo_url: str
    settings: Dict[str, str]
    connect: Optional[str] = None
    cafile: Optional[str] = None
    timeout: float = 10.0
    _token: Optional[str] = field(default=None, repr=False)

    # -- transport -----------------------------------------------------------
    def _context(self) -> ssl.SSLContext:
        context = ssl.create_default_context()
        if self.cafile:
            context.load_verify_locations(cafile=self.cafile)
        return context

    def request(
        self,
        method: str,
        url: str,
        body: Any = None,
        headers: Optional[Dict[str, str]] = None,
    ) -> Response:
        parts = urllib.parse.urlsplit(url)
        if parts.scheme not in ("http", "https") or not parts.hostname:
            raise CheckFailed(f"not an http(s) URL: {url}")
        host = parts.hostname
        port = parts.port or (443 if parts.scheme == "https" else 80)
        address = (self.connect or host, port)
        sock = socket.create_connection(address, timeout=self.timeout)
        try:
            cert = None
            if parts.scheme == "https":
                sock = self._context().wrap_socket(sock, server_hostname=host)
                cert = sock.getpeercert()
            conn = http.client.HTTPConnection(host, port, timeout=self.timeout)
            conn.sock = sock
            data = None
            # Set here: http.client would add ":443" to an https Host header,
            # since the connection object it builds it from is a plain one.
            send = {
                "Host": host if parts.port is None else f"{host}:{parts.port}",
                "Accept": "application/json",
                "User-Agent": "controlvar-demo-probe",
            }
            if body is not None:
                data = json.dumps(body).encode("utf-8")
                send["Content-Type"] = "application/json"
            send.update(headers or {})
            path = parts.path or "/"
            if parts.query:
                path += "?" + parts.query
            conn.request(method, path, body=data, headers=send)
            raw = conn.getresponse()
            payload = raw.read(MAX_BODY)
            return Response(
                raw.status, {k.lower(): v for k, v in raw.getheaders()}, payload, cert
            )
        finally:
            sock.close()

    def api(self, method: str, path: str, **kwargs: Any) -> Response:
        return self.request(method, self.demo_url.rstrip("/") + path, **kwargs)

    def host_url(self, prefix: str) -> str:
        parts = urllib.parse.urlsplit(self.demo_url)
        netloc = f"{prefix}.{parts.netloc}" if prefix else parts.netloc
        return urllib.parse.urlunsplit((parts.scheme, netloc, "/", "", ""))

    def need(self, name: str) -> str:
        value = self.settings.get(name, "")
        if not value:
            raise CheckFailed(f"needs {name} (from --env-file)")
        return value

    # -- helpers -------------------------------------------------------------
    def login(self, email: str, password: str) -> Response:
        return self.api(
            "POST", "/api/v1/auth/login", body={"email": email, "password": password}
        )

    def demo_credentials(self) -> Tuple[str, str]:
        email = self.settings.get("DEMO_ACCOUNT_EMAIL", "")
        password = self.settings.get("DEMO_ACCOUNT_PASSWORD", "")
        if email and password:
            return email, password
        account = self.published_account()
        return str(account.get("email", "")), str(account.get("password", ""))

    def published_account(self) -> Dict[str, Any]:
        response = self.api("GET", "/api/v1/auth/config")
        expect_status(response, 200, "GET /api/v1/auth/config")
        body = response.json()
        # `is True`: the dashboard reads only the JSON boolean as on.
        if not isinstance(body, dict) or body.get("demo_mode") is not True:
            raise WrongAnswer("GET /api/v1/auth/config does not say demo_mode: true")
        account = body.get("demo_account")
        if not isinstance(account, dict):
            raise WrongAnswer("GET /api/v1/auth/config publishes no demo_account")
        email, password = account.get("email"), account.get("password")
        if not isinstance(email, str) or "@" not in email or len(email) > 254:
            raise WrongAnswer("the published demo_account has no usable email")
        if not isinstance(password, str) or not 1 <= len(password) <= 256:
            raise WrongAnswer("the published demo_account has no usable password")
        return account

    def token(self) -> str:
        if self._token is None:
            self.check_signin()
        assert self._token is not None
        return self._token

    # -- checks --------------------------------------------------------------
    def check_health(self) -> str:
        response = self.api("GET", "/health")
        expect_status(response, 200, "GET /health")
        body = response.json()
        if not isinstance(body, dict):
            raise CheckFailed("GET /health answered 200 without a JSON object")
        if "environment" in body or "version" in body:
            raise WrongAnswer(
                f"GET /health shows environment {body.get('environment')!r}: the API"
                " is not running with ENVIRONMENT=production"
            )
        return "200, production"

    def check_home(self) -> str:
        response = self.api("GET", "/", headers={"Accept": "text/html"})
        location = response.headers.get("location", "")
        if response.status not in (301, 302, 303, 307, 308):
            raise CheckFailed(f"GET / answered {response.status}, expected a redirect")
        if location not in ("/login", self.demo_url.rstrip("/") + "/login"):
            raise CheckFailed(f"GET / redirects to {location!r}, expected /login")
        return f"{response.status} to /login"

    def check_config(self) -> str:
        account = self.published_account()
        expected = self.settings.get("DEMO_ACCOUNT_EMAIL", "")
        published = str(account.get("email", ""))
        if expected and published.lower() != expected.lower():
            raise WrongAnswer(
                f"the published demo account is {published}, expected {expected}"
            )
        return f"demo_mode on, demo account {published}"

    def check_signin(self) -> str:
        email, password = self.demo_credentials()
        if not email or not password:
            raise CheckFailed("no demo account to sign in with")
        response = self.login(email, password)
        expect_status(response, 200, f"signing in as {email}")
        body = response.json()
        token = body.get("access_token") if isinstance(body, dict) else None
        if not isinstance(token, str) or not token:
            raise CheckFailed(f"signing in as {email} returned no access_token")
        expect_viewer(body.get("user"), email, "the sign-in answer")
        me = self.api(
            "GET", "/api/v1/auth/me", headers={"Authorization": f"Bearer {token}"}
        )
        expect_status(me, 200, "GET /api/v1/auth/me")
        expect_viewer(me.json(), email, "GET /api/v1/auth/me")
        self._token = token
        return f"{email}: VIEWER, not a superuser"

    def check_refusal(self) -> str:
        response = self.api(
            "POST",
            "/api/v1/experiments/",
            body={},
            headers={"Authorization": f"Bearer {self.token()}"},
        )
        expect_refused(response, "POST /api/v1/experiments/ as the demo account")
        return f"403 {REFUSAL_CODE}"

    def check_anonymous(self) -> str:
        response = self.api("POST", "/api/v1/feature-flags/", body={})
        expect_refused(response, "POST /api/v1/feature-flags/ with no credentials")
        return f"403 {REFUSAL_CODE}"

    def check_passwords(self) -> str:
        for email in LOCKED_ACCOUNTS:
            expect_locked(
                self.login(email, PUBLISHED_PASSWORD),
                f"{email} with the published password",
            )
        return f"{len(LOCKED_ACCOUNTS)} accounts: 401"

    def check_superuser(self) -> str:
        email = self.need("FIRST_SUPERUSER")
        password = self.need("FIRST_SUPERUSER_PASSWORD")
        expect_locked(self.login(email, password), "FIRST_SUPERUSER")
        return "401"

    def check_storefronts(self) -> str:
        for name in STOREFRONTS:
            url = self.host_url(name)
            expect_status(
                self.request("GET", url, headers={"Accept": "text/html"}), 200, url
            )
        return "200 each"

    def check_sdk(self) -> str:
        for setting, experiment, flag in SDK_TARGETS:
            key = self.need(setting)
            headers = {"X-API-Key": key}
            assigned = self.api(
                "POST",
                "/api/v1/tracking/assign",
                body={"experiment_key": experiment, "user_id": PROBE_USER},
                headers=headers,
            )
            expect_status(assigned, 200, f"POST /api/v1/tracking/assign with {setting}")
            query = urllib.parse.urlencode({"user_id": PROBE_USER})
            evaluated = self.api(
                "GET", f"/api/v1/feature-flags/evaluate/{flag}?{query}", headers=headers
            )
            expect_status(
                evaluated,
                200,
                f"GET /api/v1/feature-flags/evaluate/{flag} with {setting}",
            )
        return "assign and evaluate: 200 with both storefront keys"

    def check_tls(self) -> str:
        days_left = []
        for prefix in ("",) + STOREFRONTS:
            url = self.host_url(prefix)
            if not url.startswith("https://"):
                raise CheckFailed(f"{url} is not https")
            cert = self.request("GET", url, headers={"Accept": "text/html"}).cert or {}
            not_after = cert.get("notAfter")
            if not not_after:
                raise CheckFailed(f"{url} presented no certificate expiry")
            days = (ssl.cert_time_to_seconds(not_after) - time.time()) / 86400
            if days <= MIN_CERT_DAYS:
                raise CheckFailed(
                    f"{url}: certificate expires in {days:.1f} days (need > {MIN_CERT_DAYS})"
                )
            days_left.append(days)
        return f"shortest {min(days_left):.0f} days"


def expect_status(response: Response, status: int, what: str) -> None:
    if response.status != status:
        raise CheckFailed(f"{what} answered {response.status}, expected {status}")


def expect_viewer(user: Any, email: str, where: str) -> None:
    if not isinstance(user, dict):
        raise CheckFailed(f"{where} carries no user")
    if str(user.get("email", "")).lower() != email.lower():
        raise WrongAnswer(f"{where} names {user.get('email')!r}, expected {email}")
    if user.get("role") != "VIEWER":
        raise WrongAnswer(f"{where}: role {user.get('role')!r}, expected 'VIEWER'")
    if user.get("is_superuser") is not False:
        raise WrongAnswer(f"{where}: is_superuser {user.get('is_superuser')!r}")


def expect_refused(response: Response, what: str) -> None:
    """Busy or down is no answer; any other answer but the demo refusal is a
    wrong one: the write reached something other than demo mode's refusal."""
    if no_answer(response):
        raise CheckFailed(f"{what} answered {response.status}, expected 403")
    if response.status != 403:
        raise WrongAnswer(f"{what} answered {response.status}, expected 403")
    try:
        body = response.json()
    except CheckFailed:
        body = None
    detail = body.get("detail") if isinstance(body, dict) else None
    code = detail.get("code") if isinstance(detail, dict) else None
    if code != REFUSAL_CODE:
        raise WrongAnswer(f"{what} answered 403 without detail.code {REFUSAL_CODE!r}")


def expect_locked(response: Response, what: str) -> None:
    """A password that must not sign in: 401. Signing in is a wrong answer."""
    if 200 <= response.status < 300:
        raise WrongAnswer(f"{what} answered {response.status}: it signs in")
    if response.status != 401:
        raise CheckFailed(f"{what} answered {response.status}, expected 401")


CHECKS: Dict[str, Callable[[Probe], str]] = {
    "health": Probe.check_health,
    "home": Probe.check_home,
    "config": Probe.check_config,
    "signin": Probe.check_signin,
    "refusal": Probe.check_refusal,
    "anonymous": Probe.check_anonymous,
    "passwords": Probe.check_passwords,
    "superuser": Probe.check_superuser,
    "storefronts": Probe.check_storefronts,
    "sdk": Probe.check_sdk,
    "tls": Probe.check_tls,
}


def read_env_file(path: str) -> Dict[str, str]:
    """KEY=VALUE lines, as generate-env.sh writes them (no expansion)."""
    values: Dict[str, str] = {}
    with open(path, encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
                value = value[1:-1]
            values[key.strip()] = value
    return values


def say(line: str) -> None:
    sys.stdout.write(line + "\n")
    sys.stdout.flush()


def usage_error(line: str) -> int:
    sys.stderr.write(f"probe: {line}\n")
    return 2


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--env-file", help="the box's .env (generate-env.sh)")
    parser.add_argument(
        "--demo-url", help="the dashboard URL (default: PUBLIC_BASE_URL)"
    )
    parser.add_argument("--connect", help="send every request to this address")
    parser.add_argument("--cafile", help="also trust this CA certificate (PEM)")
    parser.add_argument("--timeout", type=float, default=10.0)
    parser.add_argument("checks", nargs="*", metavar="CHECK")
    args = parser.parse_args(argv)

    if not args.checks:
        return usage_error(f"name the checks to run, from: {' '.join(CHECKS)}")
    unknown = [name for name in args.checks if name not in CHECKS]
    if unknown:
        return usage_error(f"unknown check(s): {' '.join(unknown)}")
    settings: Dict[str, str] = {}
    if args.env_file:
        try:
            settings = read_env_file(args.env_file)
        except OSError as exc:
            return usage_error(f"cannot read {args.env_file}: {exc}")
    demo_url = args.demo_url or settings.get("PUBLIC_BASE_URL", "")
    if not demo_url:
        return usage_error("no --demo-url and no PUBLIC_BASE_URL in --env-file")

    probe = Probe(demo_url, settings, args.connect, args.cafile, args.timeout)
    failed = wrong = 0
    for name in args.checks:
        try:
            detail = CHECKS[name](probe)
        except WrongAnswer as exc:
            wrong += 1
            say(f"WRONG {name}: {exc}")
        except CheckFailed as exc:
            failed += 1
            say(f"FAIL {name}: {exc}")
        except Exception as exc:  # a timeout, a refused connection: no answer
            failed += 1
            say(f"FAIL {name}: {type(exc).__name__}: {exc}")
        else:
            say(f"ok   {name}: {detail}")
    total = len(args.checks)
    if wrong:
        say(f"probe: {wrong} of {total} checks got a WRONG answer, {failed} no answer")
        return 3
    if failed:
        say(f"probe: {failed} of {total} checks got no answer")
        return 1
    say(f"probe: {total} of {total} checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
