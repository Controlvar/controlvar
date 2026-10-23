"""The StreamPulse rollout story's password, from the environment (#1299, M5).

``demo/streampulse/simulator/rollout_story.py`` signs in with ``--email`` and
``--password``, which default to the local demo's admin. A command for an
install must not carry the install's password, so the story also reads it from
``CONTROLVAR_ADMIN_PASSWORD``. The change is additive, so every command that
runs the story today (``demo/setup-local.sh``, ``demo/DEMO_GUIDE.md``, both
StreamPulse READMEs, the app's drawer and the docs journey) works unchanged.
Pinned here:

* with the variable set and no ``--password``, the story signs in with it;
* with neither, ``--password``'s default still applies; ``--password`` given
  wins over the variable; an empty variable counts as unset; ``--token`` still
  skips the sign-in;
* a refused sign-in says the password came from the variable, and never prints
  it; ``--help`` names the variable;
* it is the variable ``scripts/seed_demo_catalogue.py`` reads.

The story's own tests (``demo/streampulse/simulator/test_story.py``) run only in
rc-evidence: pyproject's testpaths are ``backend/tests`` and
``modules/backend/tests``. These run in Unit Tests.
"""

from __future__ import annotations

import importlib.util
import io
import sys
import uuid
from pathlib import Path
from typing import Any, List, Tuple

import pytest

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[4]
SIMULATOR = REPO_ROOT / "demo" / "streampulse" / "simulator"
STORY = SIMULATOR / "rollout_story.py"
SEEDER = REPO_ROOT / "scripts" / "seed_demo_catalogue.py"
VARIABLE = "CONTROLVAR_ADMIN_PASSWORD"


@pytest.fixture(scope="module")
def story():
    """rollout_story.py, loaded from its file; its own imports and path entry are undone."""
    modules_before = set(sys.modules)
    path_before = list(sys.path)
    spec = importlib.util.spec_from_file_location("_rollout_story_password", STORY)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # its dataclasses look their module up by name
    spec.loader.exec_module(module)
    try:
        yield module
    finally:
        for name in set(sys.modules) - modules_before:
            sys.modules.pop(name, None)
        sys.path[:] = path_before


class _Client:
    """Enough of StoryClient for step 1 to run."""

    def __init__(self, url: str, token: str) -> None:
        self.url, self.token = url, token

    def flag_by_key(self, key: str) -> dict:
        return {"id": "f-1", "key": key}

    def get_flag(self, flag_id: str) -> dict:
        return {
            "id": flag_id,
            "rollout_percentage": 5,
            "status": "active",
            "targeting_rules": {},
        }

    def schedule_for_flag(self, flag_id: str) -> dict:
        return {"name": "Player v2 rollout", "status": "active", "stages": []}

    def safety_config(self, flag_id: str) -> dict:
        return {"enabled": True, "metrics": {}, "rollback_percentage": 5}

    def safety_settings(self) -> dict:
        return {"enable_automatic_rollbacks": True}

    def safety_check(self, flag_id: str) -> dict:
        return {"is_healthy": True, "metrics": []}


def _sign_ins(
    story: Any, argv: List[str], environ: dict
) -> Tuple[int, List[Tuple[str, str, str]]]:
    logins: List[Tuple[str, str, str]] = []

    def login(api_url: str, email: str, password: str) -> str:
        logins.append((api_url, email, password))
        return "jwt"

    code = story.main(
        ["--step", "1", *argv],
        client_factory=_Client,
        login=login,
        out=io.StringIO(),
        environ=environ,
    )
    return code, logins


def test_the_variable_gives_the_password(story):
    password = "from-the-environment-" + uuid.uuid4().hex
    code, logins = _sign_ins(
        story,
        ["--api-url", "https://controlvar.example.com", "--email", "admin@example.com"],
        {VARIABLE: password},
    )
    assert code == 0
    assert logins == [("https://controlvar.example.com", "admin@example.com", password)]


def test_without_the_variable_the_password_option_and_its_default_still_apply(story):
    assert _sign_ins(story, [], {}) == (
        0,
        [("http://localhost:8000", "admin@demo.com", "Demo1234!")],
    )
    assert _sign_ins(story, ["--password", "given"], {}) == (
        0,
        [("http://localhost:8000", "admin@demo.com", "given")],
    )
    # An empty variable is not a password.
    assert _sign_ins(story, [], {VARIABLE: ""}) == (
        0,
        [("http://localhost:8000", "admin@demo.com", "Demo1234!")],
    )


def test_a_password_given_on_the_command_line_wins_over_the_variable(story):
    assert _sign_ins(
        story, ["--password", "given"], {VARIABLE: "from-the-environment"}
    ) == (
        0,
        [("http://localhost:8000", "admin@demo.com", "given")],
    )


def test_a_token_still_skips_the_sign_in(story):
    assert _sign_ins(
        story, ["--token", "given-token"], {VARIABLE: "from-the-environment"}
    ) == (0, [])


def test_the_parser_keeps_its_documented_defaults(story):
    """What demo/streampulse/simulator/test_story.py pins, still true."""
    args = story.build_parser().parse_args(["--step", "1"])
    assert (args.email, args.password, args.token) == (
        "admin@demo.com",
        "Demo1234!",
        None,
    )
    assert args.password_given is False
    assert (
        story.build_parser()
        .parse_args(["--step", "1", "--password", "x"])
        .password_given
        is True
    )


def test_a_refused_sign_in_names_the_variable_and_not_the_password(story, capsys):
    password = "from-the-environment-" + uuid.uuid4().hex

    def refused(api_url: str, email: str, given: str) -> str:
        raise story.ApiError(401, '{"detail":"Incorrect email or password"}', api_url)

    code = story.main(
        ["--step", "1", "--email", "admin@example.com"],
        client_factory=_Client,
        login=refused,
        out=io.StringIO(),
        environ={VARIABLE: password},
    )
    printed = capsys.readouterr()
    assert code == 2
    assert f"The password was read from {VARIABLE}." in printed.err
    assert password not in printed.err + printed.out


def test_the_help_names_the_variable(story):
    assert VARIABLE in story.build_parser().format_help()


def test_it_is_the_variable_the_demo_seeder_reads(story):
    assert story.PASSWORD_ENV == VARIABLE
    assert f'PASSWORD_ENV = "{VARIABLE}"' in SEEDER.read_text(encoding="utf-8")
