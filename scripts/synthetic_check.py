# SPDX-FileCopyrightText: 2026 The Controlvar authors
# SPDX-License-Identifier: Apache-2.0
"""The synthetic staging check: eight steps, in order, each one fatal.

``.github/workflows/synthetic.yml`` runs this every 30 minutes against a
deployed stack, as a dedicated non-superuser ANALYST account that owns one API
key:

1. Ready: ``GET /health/ready`` answers 200.
2. Sign in: ``POST /api/v1/auth/login``, then ``GET /api/v1/auth/me`` must say
   ``role`` ANALYST and ``is_superuser`` false. This is checked before the
   check writes anything.
3. Key expiry: ``GET /api/v1/api-keys`` (the account's own keys) must list
   exactly one active key, with an expiry date; fewer than 14 days left warns,
   fewer than 3 fails.
4. Results before: ``GET /api/v1/results/{id}?use_cache=false`` gives the
   canary's assignments and conversions on its primary (conversion) metric.
5. Assign: ``POST /api/v1/tracking/assign`` with the key, for a user new to
   this run, answers ``assigned: true``; asking again returns the same variant.
6. Track: ``POST /api/v1/tracking/track`` with the key, the canary metric's
   event for that user, answers 2xx.
7. Evaluate: ``GET /api/v1/feature-flags/evaluate/{key}?user_id=`` with the
   key answers 200 with ``reason`` ``rollout`` or ``targeting_rule``: the flag
   was evaluated (``inactive`` and ``error`` fail, naming the reason).
8. Results after: the same read as step 4 shows exactly one more assignment
   and one more conversion.

It sends nothing but those requests: ``REQUESTS`` is the whole list, and
``Client.send`` refuses any other method and route before anything is sent.

What it prints is public (the repository is), so it prints only step names,
HTTP statuses, timings, counts and fixed sentences: never a response body, a
token, a key, an id, an e-mail address or an exception's text. Its last line
is ``ran N of 8``, N being the steps that passed. For the workflow it writes
``ran``, ``step`` (the failing step) and ``status`` to ``$GITHUB_OUTPUT``.

Inputs, from the environment; a missing one fails by its name:

* secrets ``SYNTH_EMAIL``, ``SYNTH_PASSWORD``, ``SYNTH_API_KEY``;
* variables ``PUBLIC_BASE_URL`` (``https://`` only), ``SYNTH_EXPERIMENT_ID``,
  ``SYNTH_EXPERIMENT_KEY``, ``SYNTH_EVENT_NAME``, ``SYNTH_FLAG_KEY``, and
  ``SYNTH_ENABLED``, which must be exactly ``true``;
* ``GITHUB_RUN_ID`` and ``GITHUB_RUN_ATTEMPT``, which make the user id.

Standard library only: the job that holds the credentials installs nothing.
"""

from __future__ import annotations

import datetime as _dt
import http.client
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, Mapping, NamedTuple, Optional, TextIO, Tuple

STEPS: Tuple[str, ...] = (
    "Ready",
    "Sign in",
    "Key expiry",
    "Results before",
    "Assign",
    "Track",
    "Evaluate",
    "Results after",
)

#: The only requests the check sends, as (method, route). Nothing else.
REQUESTS: Tuple[Tuple[str, str], ...] = (
    ("GET", "/health/ready"),
    ("POST", "/api/v1/auth/login"),
    ("GET", "/api/v1/auth/me"),
    ("GET", "/api/v1/api-keys"),
    ("GET", "/api/v1/results/{experiment_id}"),
    ("POST", "/api/v1/tracking/assign"),
    ("POST", "/api/v1/tracking/track"),
    ("GET", "/api/v1/feature-flags/evaluate/{flag_key}"),
)

#: Seconds before a request counts as no answer.
TIMEOUT = 15
#: The most of an answer the check reads.
MAX_ANSWER_BYTES = 2_000_000
#: Days of key validity below which the check warns, and fails.
WARN_DAYS = 14
FAIL_DAYS = 3
#: The evaluation reasons that mean the flag was evaluated for the user.
EVALUATED = ("rollout", "targeting_rule")
#: Reasons the API documents for an ineligible user or a flag it did not evaluate.
NOT_ASSIGNED_REASONS = ("holdout", "mutual_exclusion", "targeting")
NOT_EVALUATED_REASONS = ("inactive", "error")

SECRETS = ("SYNTH_EMAIL", "SYNTH_PASSWORD", "SYNTH_API_KEY")
VARIABLES = (
    "PUBLIC_BASE_URL",
    "SYNTH_EXPERIMENT_ID",
    "SYNTH_EXPERIMENT_KEY",
    "SYNTH_EVENT_NAME",
    "SYNTH_FLAG_KEY",
)
RUN_INPUTS = ("GITHUB_RUN_ID", "GITHUB_RUN_ATTEMPT")

#: The first step that needs each input: a missing or refused input fails
#: there, before any request is sent.
NEEDED_AT: Dict[str, int] = {
    "SYNTH_ENABLED": 1,
    "PUBLIC_BASE_URL": 1,
    "SYNTH_EMAIL": 2,
    "SYNTH_PASSWORD": 2,
    "SYNTH_API_KEY": 3,
    "SYNTH_EXPERIMENT_ID": 4,
    "SYNTH_EXPERIMENT_KEY": 5,
    "GITHUB_RUN_ID": 5,
    "GITHUB_RUN_ATTEMPT": 5,
    "SYNTH_EVENT_NAME": 6,
    "SYNTH_FLAG_KEY": 7,
}

_UUID = re.compile(r"[0-9a-fA-F]{8}-(?:[0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12}")
_KEY = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,99}")
_DIGITS = re.compile(r"[0-9]{1,20}")
_HOST = re.compile(
    r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)*"
)


class RouteRefused(RuntimeError):
    """A request outside ``REQUESTS``: nothing was sent."""


class ConfigError(ValueError):
    """An input that is missing or refused; ``name`` says which."""

    def __init__(self, name: str, problem: str) -> None:
        super().__init__(f"{name} {problem}")
        self.name = name


class Config(NamedTuple):
    base_url: str
    email: str
    password: str
    api_key: str
    experiment_id: str
    experiment_key: str
    event_name: str
    flag_key: str
    user_id: str


def _base_url(value: str, allow_loopback_http: bool) -> str:
    """``https://host[:port]``, nothing else; a trailing ``/`` is dropped."""
    parts = urllib.parse.urlsplit(value)
    loopback = (
        allow_loopback_http and parts.scheme == "http" and parts.hostname == "127.0.0.1"
    )
    if parts.scheme != "https" and not loopback:
        raise ConfigError("PUBLIC_BASE_URL", "must start with https://")
    if (
        parts.username is not None
        or parts.password is not None
        or parts.path not in ("", "/")
        or parts.query
        or parts.fragment
        or not parts.hostname
        or _HOST.fullmatch(parts.hostname) is None
    ):
        raise ConfigError("PUBLIC_BASE_URL", "must be an origin: https://host, no path")
    try:
        port = parts.port
    except ValueError:
        raise ConfigError("PUBLIC_BASE_URL", "has an invalid port") from None
    netloc = parts.hostname + (f":{port}" if port is not None else "")
    return f"{parts.scheme}://{netloc}"


def load_config(env: Mapping[str, str], allow_loopback_http: bool = False) -> Config:
    """The check's inputs; a missing or refused one raises ``ConfigError``.

    ``allow_loopback_http`` lets the tests point the check at a stub server on
    ``http://127.0.0.1``; the command line never sets it."""
    enabled = env.get("SYNTH_ENABLED", "")
    if enabled != "true":
        raise ConfigError("SYNTH_ENABLED", "must be exactly true for the check to run")
    missing = [
        name
        for name in (*SECRETS, *VARIABLES, *RUN_INPUTS)
        if not env.get(name, "").strip()
    ]
    if missing:
        first = min(missing, key=lambda name: NEEDED_AT[name])
        raise ConfigError(first, "is missing (missing: " + ", ".join(missing) + ")")
    experiment_id = env["SYNTH_EXPERIMENT_ID"].strip()
    if _UUID.fullmatch(experiment_id) is None:
        raise ConfigError("SYNTH_EXPERIMENT_ID", "is not an experiment id (a UUID)")
    for name in ("SYNTH_EXPERIMENT_KEY", "SYNTH_EVENT_NAME", "SYNTH_FLAG_KEY"):
        if _KEY.fullmatch(env[name].strip()) is None:
            raise ConfigError(name, "is not a key: letters, digits and _.:- only")
    for name in RUN_INPUTS:
        if _DIGITS.fullmatch(env[name].strip()) is None:
            raise ConfigError(name, "is not a number")
    return Config(
        base_url=_base_url(env["PUBLIC_BASE_URL"].strip(), allow_loopback_http),
        email=env["SYNTH_EMAIL"].strip(),
        password=env["SYNTH_PASSWORD"],
        api_key=env["SYNTH_API_KEY"].strip(),
        experiment_id=experiment_id,
        experiment_key=env["SYNTH_EXPERIMENT_KEY"].strip(),
        event_name=env["SYNTH_EVENT_NAME"].strip(),
        flag_key=env["SYNTH_FLAG_KEY"].strip(),
        user_id="synth-{}-{}".format(
            env["GITHUB_RUN_ID"].strip(), env["GITHUB_RUN_ATTEMPT"].strip()
        ),
    )


class Answer(NamedTuple):
    """``status`` is None when nothing came back in time."""

    status: Optional[int]
    data: Any
    ms: int


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """A redirect is an answer (3xx), never followed with the credentials."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class Client:
    """Sends the requests in ``REQUESTS`` and nothing else."""

    def __init__(self, base_url: str, timeout: float = TIMEOUT) -> None:
        self.base_url = base_url
        self.timeout = timeout
        # No proxy from the environment, no redirects.
        self._opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}), _NoRedirect()
        )

    def send(
        self,
        method: str,
        route: str,
        *,
        params: Optional[Mapping[str, str]] = None,
        query: Optional[Mapping[str, str]] = None,
        body: Optional[Mapping[str, Any]] = None,
        bearer: Optional[str] = None,
        api_key: Optional[str] = None,
    ) -> Answer:
        if (method, route) not in REQUESTS:
            raise RouteRefused(f"{method} {route} is not one of the check's requests")
        path = route.format(
            **{
                name: urllib.parse.quote(value, safe="")
                for name, value in (params or {}).items()
            }
        )
        url = self.base_url + path
        if query:
            url += "?" + urllib.parse.urlencode(query)
        headers = {
            "Accept": "application/json",
            "User-Agent": "controlvar-synthetic-check",
        }
        data = None
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        if bearer is not None:
            headers["Authorization"] = "Bearer " + bearer
        if api_key is not None:
            headers["X-API-Key"] = api_key
        request = urllib.request.Request(url, data=data, method=method, headers=headers)
        started = time.monotonic()
        try:
            with self._opener.open(request, timeout=self.timeout) as response:
                status, raw = response.status, response.read(MAX_ANSWER_BYTES + 1)
        except urllib.error.HTTPError as error:
            status = error.code
            try:
                error.read(MAX_ANSWER_BYTES + 1)  # read and dropped
            except Exception:
                pass
            finally:
                error.close()
            return Answer(status, None, _ms(started))
        except (OSError, http.client.HTTPException, ValueError):
            # Timeout, refused connection, TLS, a broken answer: no answer.
            return Answer(None, None, _ms(started))
        if len(raw) > MAX_ANSWER_BYTES:
            return Answer(status, _TooLarge, _ms(started))
        try:
            parsed = json.loads(raw.decode("utf-8")) if raw else None
        except (UnicodeDecodeError, ValueError):
            parsed = _NotJson
        return Answer(status, parsed, _ms(started))


#: Markers for an answer the check could not read as JSON.
_TooLarge = object()
_NotJson = object()


def _ms(started: float) -> int:
    return int((time.monotonic() - started) * 1000)


class StepFailed(Exception):
    """A step failed; ``status`` is the HTTP status judged, or None."""

    def __init__(self, status: Optional[int], detail: str) -> None:
        super().__init__(detail)
        self.status = status
        self.detail = detail


def _require_ok(answer: Answer, ok: Tuple[int, ...] = (200,)) -> Any:
    if answer.status is None:
        raise StepFailed(None, f"no answer within {TIMEOUT} s")
    if answer.status not in ok:
        raise StepFailed(answer.status, f"the API answered {answer.status}")
    if answer.data is _TooLarge:
        raise StepFailed(answer.status, "the answer was too large to read")
    if answer.data is _NotJson:
        raise StepFailed(answer.status, "the answer was not JSON")
    return answer.data


def _shape(status: Optional[int]) -> StepFailed:
    return StepFailed(status, "the answer does not have the expected shape")


def _count(value: Any) -> int:
    if type(value) is not int or value < 0:
        raise TypeError
    return value


class Check:
    """One run of the eight steps against ``config``."""

    def __init__(self, config: Config, client: Client, out: TextIO) -> None:
        self.config = config
        self.client = client
        self.out = out
        self.token: Optional[str] = None
        self.before: Optional[Tuple[int, int]] = None

    def _send(self, *args: Any, **kwargs: Any) -> Answer:
        return self.client.send(*args, **kwargs)

    # -- the steps -----------------------------------------------------------

    def step_ready(self) -> Answer:
        answer = self._send("GET", "/health/ready")
        _require_ok(answer)
        return answer

    def step_sign_in(self) -> Answer:
        login = self._send(
            "POST",
            "/api/v1/auth/login",
            body={"email": self.config.email, "password": self.config.password},
        )
        data = _require_ok(login)
        token = data.get("access_token") if isinstance(data, dict) else None
        if not isinstance(token, str) or not token:
            raise _shape(login.status)
        self.token = token
        me = self._send("GET", "/api/v1/auth/me", bearer=self.token)
        data = _require_ok(me)
        if not isinstance(data, dict) or not isinstance(data.get("is_superuser"), bool):
            raise _shape(me.status)
        if data["is_superuser"]:
            raise StepFailed(
                me.status, "the synthetic account is a superuser; it must not be"
            )
        if data.get("role") != "ANALYST":
            raise StepFailed(me.status, "the synthetic account is not an ANALYST")
        return me

    def step_key_expiry(self) -> Answer:
        answer = self._send("GET", "/api/v1/api-keys", bearer=self.token)
        data = _require_ok(answer)
        if not isinstance(data, list) or not all(isinstance(key, dict) for key in data):
            raise _shape(answer.status)
        active = [key for key in data if key.get("is_active") is True]
        if len(active) != 1:
            raise StepFailed(
                answer.status,
                f"the synthetic account holds {len(active)} active API keys; "
                "it must hold exactly one",
            )
        expires = active[0].get("expires_at")
        if expires is None:
            raise StepFailed(answer.status, "the check's API key has no expiry date")
        try:
            when = _dt.datetime.fromisoformat(str(expires))
        except ValueError:
            raise _shape(answer.status) from None
        if when.tzinfo is None:
            when = when.replace(tzinfo=_dt.timezone.utc)
        left = when - _dt.datetime.now(_dt.timezone.utc)
        if left < _dt.timedelta(days=FAIL_DAYS):
            raise StepFailed(
                answer.status,
                f"the check's API key expires in fewer than {FAIL_DAYS} days",
            )
        if left < _dt.timedelta(days=WARN_DAYS):
            self.out.write(
                "::warning title=Synthetic check::The synthetic check's API key "
                f"expires in fewer than {WARN_DAYS} days. Create a new key for the "
                "synthetic account, replace the stored one, then delete the old key.\n"
            )
        return answer

    def _results(self) -> Tuple[Answer, Tuple[int, int]]:
        answer = self._send(
            "GET",
            "/api/v1/results/{experiment_id}",
            params={"experiment_id": self.config.experiment_id},
            query={"use_cache": "false"},
            bearer=self.token,
        )
        data = _require_ok(answer)
        try:
            primary = [m for m in data["metrics"] if m["is_primary"] is True]
            if len(primary) != 1 or primary[0]["metric_type"] != "conversion":
                raise StepFailed(
                    answer.status,
                    "the canary experiment's primary metric is not one conversion metric",
                )
            variants = primary[0]["variants"]
            assigned = sum(_count(v["sample_size"]) for v in variants)
            converted = sum(_count(v["conversions"]) for v in variants)
        except StepFailed:
            raise
        except (KeyError, TypeError, AttributeError):
            raise _shape(answer.status) from None
        return answer, (assigned, converted)

    def step_results_before(self) -> Answer:
        answer, self.before = self._results()
        return answer

    def _assign(self) -> Tuple[Answer, str]:
        answer = self._send(
            "POST",
            "/api/v1/tracking/assign",
            body={
                "experiment_key": self.config.experiment_key,
                "user_id": self.config.user_id,
            },
            api_key=self.config.api_key,
        )
        data = _require_ok(answer)
        if not isinstance(data, dict) or not isinstance(data.get("variant_id"), str):
            raise _shape(answer.status)
        if data.get("assigned") is not True:
            reason = data.get("reason")
            named = reason if reason in NOT_ASSIGNED_REASONS else "not documented"
            raise StepFailed(
                answer.status, f"the new user was not assigned (reason: {named})"
            )
        return answer, data["variant_id"]

    def step_assign(self) -> Answer:
        _, first = self._assign()
        answer, again = self._assign()
        if again != first:
            raise StepFailed(answer.status, "asking again returned a different variant")
        return answer

    def step_track(self) -> Answer:
        answer = self._send(
            "POST",
            "/api/v1/tracking/track",
            body={
                "event_type": self.config.event_name,
                "user_id": self.config.user_id,
                "experiment_key": self.config.experiment_key,
            },
            api_key=self.config.api_key,
        )
        _require_ok(answer, ok=(200, 201, 202))
        return answer

    def step_evaluate(self) -> Answer:
        answer = self._send(
            "GET",
            "/api/v1/feature-flags/evaluate/{flag_key}",
            params={"flag_key": self.config.flag_key},
            query={"user_id": self.config.user_id},
            api_key=self.config.api_key,
        )
        data = _require_ok(answer)
        reason = data.get("reason") if isinstance(data, dict) else None
        if reason not in EVALUATED:
            named = reason if reason in NOT_EVALUATED_REASONS else "not documented"
            raise StepFailed(
                answer.status,
                f"the flag was not evaluated for the user (reason: {named})",
            )
        return answer

    def step_results_after(self) -> Answer:
        answer, after = self._results()
        assert self.before is not None
        more_assigned = after[0] - self.before[0]
        more_converted = after[1] - self.before[1]
        if (more_assigned, more_converted) != (1, 1):
            raise StepFailed(
                answer.status,
                "expected exactly 1 more assignment and 1 more conversion; "
                f"the results show {more_assigned:+d} and {more_converted:+d}",
            )
        return answer

    STEP_METHODS = (
        "step_ready",
        "step_sign_in",
        "step_key_expiry",
        "step_results_before",
        "step_assign",
        "step_track",
        "step_evaluate",
        "step_results_after",
    )


class Outcome(NamedTuple):
    ran: int
    step: Optional[int]  # the failing step, None when all eight passed
    status: Optional[str]  # its status: three digits or "no answer"


def _status_text(status: Optional[int]) -> str:
    return "no answer" if status is None else str(status)


def _row(number: int, result: str, status: str, ms: str) -> str:
    return f"{number:<5} {STEPS[number - 1]:<15} {result:<7} {status:<10} {ms}\n"


def run_check(config: Config, out: TextIO, client: Optional[Client] = None) -> Outcome:
    """The eight steps, in order; the first failure stops the check."""
    check = Check(config, client or Client(config.base_url), out)
    out.write("Synthetic check: eight steps, each one fatal\n")
    out.write(f"{'step':<5} {'name':<15} {'result':<7} {'status':<10} ms\n")
    for number, method in enumerate(Check.STEP_METHODS, start=1):
        started = time.monotonic()
        try:
            answer = getattr(check, method)()
        except StepFailed as failed:
            status = _status_text(failed.status)
            out.write(_row(number, "FAIL", status, str(_ms(started))))
            out.write(
                f"failed at step {number} ({STEPS[number - 1]}): {failed.detail}\n"
            )
            out.write(f"ran {number - 1} of 8\n")
            return Outcome(number - 1, number, status)
        except RouteRefused:
            raise
        except Exception as error:  # the type is printed, never the text
            out.write(_row(number, "FAIL", "no answer", str(_ms(started))))
            out.write(
                f"failed at step {number} ({STEPS[number - 1]}): the check stopped "
                f"on an unexpected {type(error).__name__}\n"
            )
            out.write(f"ran {number - 1} of 8\n")
            return Outcome(number - 1, number, "no answer")
        out.write(_row(number, "pass", _status_text(answer.status), str(_ms(started))))
    out.write("ran 8 of 8\n")
    return Outcome(8, None, None)


def _write_outputs(env: Mapping[str, str], outcome: Outcome) -> None:
    path = env.get("GITHUB_OUTPUT")
    if not path:
        return
    lines = [f"ran={outcome.ran}"]
    if outcome.step is not None:
        lines += [f"step={outcome.step}", f"status={outcome.status}"]
    with open(path, "a", encoding="ascii") as handle:
        handle.write("\n".join(lines) + "\n")


def main(
    env: Optional[Mapping[str, str]] = None,
    out: Optional[TextIO] = None,
    allow_loopback_http: bool = False,
    client: Optional[Client] = None,
) -> int:
    env = os.environ if env is None else env
    out = sys.stdout if out is None else out
    try:
        config = load_config(env, allow_loopback_http=allow_loopback_http)
    except ConfigError as error:
        step = NEEDED_AT[error.name]
        out.write(f"::error title=Synthetic check::{error}\n")
        out.write(
            f"failed at step {step} ({STEPS[step - 1]}): an input is missing or refused\n"
        )
        out.write("ran 0 of 8\n")
        _write_outputs(env, Outcome(0, step, "no answer"))
        return 1
    outcome = run_check(config, out, client)
    _write_outputs(env, outcome)
    return 0 if outcome.ran == 8 else 1


if __name__ == "__main__":
    sys.exit(main())
