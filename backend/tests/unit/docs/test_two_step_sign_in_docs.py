"""What the documentation says about two-step sign-in, against the code (#1812).

* Every file in the repository that names the password sign-in routes is
  classified, and the ones that sign in against a running stack and read the
  access token are exactly the rows of ``docs/api/auth.md``'s table "Password
  sign-ins in this repository": each of them breaks if its account turns
  two-step sign-in on. The search is the files themselves, never a list typed
  here: a new file that signs in fails until it is classified, and a row
  dropped from the page fails too. It is ``mechanism:password-sign-ins`` in
  ``scripts/doc_claims.py``. A core file may not name a module's path
  (``backend/tests/smoke/test_core_boundary.py``), so the files and rows
  under the modules directory are the modules' own test's
  (``mechanism:password-sign-ins-modules``).
* The refusals' texts on the page are the API's, word for word.
* "Rotate JWT Secret Immediately" says to reset two-step sign-in afterwards.

Reads files only. With a ``.git`` directory it reads what ``git ls-files``
lists, without one (``scripts/core_build.sh``'s copy) every file but the
generated directories, as ``scripts/check_legacy_name.py`` does.
"""

from __future__ import annotations

import importlib.util
import re
import sys
from pathlib import Path
from typing import Dict, Set

import pytest

from backend.tests.smoke.modules_manifest import MODULES_DIR

pytestmark = [pytest.mark.unit]

#: Where the modules' files are; classified by the modules' own test.
MODULES_PREFIX = f"{MODULES_DIR}/"

REPO_ROOT = Path(__file__).resolve().parents[4]
PAGE = REPO_ROOT / "docs" / "api" / "auth.md"
ROTATION = REPO_ROOT / "docs" / "deployment" / "secrets-management.md"
HERE = Path(__file__).resolve().relative_to(REPO_ROOT).as_posix()

#: The two sign-in routes, assembled so that this file does not match itself.
SIGN_IN = re.compile(("auth/" + "(?:login|token)").encode())

#: Sign in with a password against a running stack and read ``access_token``:
#: the page's table names each one.
CONSUMERS = {
    ".github/workflows/_platform.yml",
    ".github/workflows/compose-production.yml",
    ".github/workflows/pr-qa-gate.yml",
    "backend/tests/performance/locustfiles/common.py",
    "demo/streampulse/simulator/rollout_story.py",
    "deploy/aws-installer/lib/steps.sh",
    "deploy/demo/probe.py",
    "scripts/compose_upgrade.sh",
    "scripts/fuzz_local.sh",
    "scripts/scale_stack.py",
    "scripts/synthetic_check.py",
    "tests/acceptance/docs/docs_runner/pacer.py",
    "tests/acceptance/showcase/capture/run.py",
    "tests/alb/rehearse.sh",
    "tests/ground_truth/driver.py",
    "tests/llm/smoke.sh",
    "tests/sdk-contract/live/run_live_contract.py",
}

#: Everything else that names the routes, by where it is (first match wins).
BY_PREFIX = (
    (MODULES_PREFIX, "the modules' files, classified by their own test"),
    ("backend/app/", "the API itself"),
    ("backend/tests/", "tests of the application, in process or against fakes"),
    ("frontend/src/tests/", "tests of the dashboard"),
    ("frontend/tests/", "browser tests of the dashboard, with seeded accounts"),
    ("frontend/src/", "the dashboard's own sign-in"),
    ("docs/", "documentation"),
    (".claude/", "documentation"),
    (".github/ISSUE_TEMPLATE/", "documentation"),
)

#: The rest, one by one.
NAMED = {
    "amplify.yml": "a comment about the static site",
    "demo/streampulse/simulator/test_story.py": "the simulator's tests, against a fake",
    "frontend/scripts/prune-marketing-site.mjs": "the marketing site's build",
    "tests/acceptance/docs/docs_runner/model.py": "names the routes a page may "
    "not show credentials for",
    "tests/acceptance/docs/floor.toml": "the documentation floor's route list",
    "tests/acceptance/docs/journeys/onboarding-and-password.yaml": "a journey's "
    "comment on the sign-in rate limit",
    "tests/fuzz/exclusions.toml": "routes the fuzzer leaves out",
}


def _checker():
    path = REPO_ROOT / "scripts" / "check_legacy_name.py"
    spec = importlib.util.spec_from_file_location("legacy_walk_for_2fa_docs", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _hits() -> Set[str]:
    found = set()
    for rel in _checker().listed_files(REPO_ROOT):
        if rel == HERE:
            continue
        path = REPO_ROOT / rel
        if not path.is_file():
            continue
        if SIGN_IN.search(path.read_bytes()):
            found.add(rel)
    return found


def _class(rel: str):
    if rel in CONSUMERS:
        return "consumer"
    for prefix, name in BY_PREFIX:
        if rel.startswith(prefix):
            return name
    if rel.endswith(".md"):
        return "documentation"
    return NAMED.get(rel)


def _section(text: str, heading: str) -> str:
    start = text.index(heading)
    rest = text[start + len(heading) :]
    end = re.search(r"^#{1,4} ", rest, re.M)
    return rest[: end.start()] if end else rest


def _table_paths() -> Set[str]:
    section = _section(
        PAGE.read_text(encoding="utf-8"), "#### Password sign-ins in this repository"
    )
    return set(re.findall(r"^\| `([^`]{1,200})` \|", section, re.M))


@pytest.fixture(scope="module")
def hits():
    return _hits()


def test_every_file_that_names_the_sign_in_routes_is_classified(hits):
    assert len(hits) > 100, f"the search found almost nothing: {len(hits)}"
    unclassified = sorted(rel for rel in hits if _class(rel) is None)
    assert unclassified == [], (
        "name each in CONSUMERS (it signs in with a password against a stack) "
        f"or say what else it is: {unclassified}"
    )


def test_every_classified_file_still_names_them(hits):
    listed = set(CONSUMERS) | set(NAMED)
    assert sorted(listed - hits) == []


def test_the_page_names_every_password_sign_in():
    """``docs/api/auth.md``: each core consumer, and nothing else outside the
    modules directory."""
    core_rows = {p for p in _table_paths() if not p.startswith(MODULES_PREFIX)}
    assert core_rows == CONSUMERS


def test_each_named_path_exists():
    for rel in sorted(_table_paths()):
        if rel.startswith(MODULES_PREFIX):
            continue
        assert (REPO_ROOT / rel).is_file(), rel


@pytest.mark.regression
def test_the_fixed_texts_are_the_apis_word_for_word():
    """The 503, the cap's 423, a wrong code, an expired sign-in and the three
    refusals of the current password at set-up: one wording in the API and on
    the page."""
    from backend.app.api.two_factor_answers import SIGN_IN_EXPIRED_DETAIL
    from backend.app.api.v1.endpoints.users import (
        CURRENT_PASSWORD_INCORRECT_DETAIL,
        TWO_FACTOR_NO_PASSWORD_DETAIL,
        TWO_FACTOR_PASSWORD_MISSING_DETAIL,
    )
    from backend.app.services.two_factor_service import (
        CODE_STEP_LOCKED_TEXT,
        UNAVAILABLE_TEXT,
        WRONG_CODE_TEXT,
    )

    def flat(path: Path) -> str:
        """The page with each run of white space, line breaks included, as one
        space: a sentence wrapped over two lines is still the sentence."""
        return " ".join(path.read_text(encoding="utf-8").split())

    page = flat(PAGE)
    texts: Dict[str, str] = {
        "unavailable": UNAVAILABLE_TEXT,
        "cap": CODE_STEP_LOCKED_TEXT,
        "wrong code": WRONG_CODE_TEXT,
        "expired": SIGN_IN_EXPIRED_DETAIL,
        "set-up, no password sent": TWO_FACTOR_PASSWORD_MISSING_DETAIL,
        "set-up, wrong password": CURRENT_PASSWORD_INCORRECT_DETAIL,
        "set-up, no password of its own": TWO_FACTOR_NO_PASSWORD_DETAIL,
    }
    missing = {name: text for name, text in texts.items() if text not in page}
    assert missing == {}


#: The dashboard's copies of the API's texts, and the user guide.
DASHBOARD_TEXTS = REPO_ROOT / "frontend" / "src" / "services" / "twoFactor.ts"
USER_GUIDE = REPO_ROOT / "docs" / "auth" / "two-factor.md"


def test_the_dashboard_and_the_guide_use_the_apis_texts_word_for_word():
    """U7: the dashboard matches the API's refusals by their text (the cap's
    423 against the address lock's, the 503, a wrong code against an expired
    step, and the current password's three refusals at set-up), so each copy
    in ``frontend/src/services/twoFactor.ts`` is the API's constant as one
    literal; the user guide quotes the 503 and the cap's 423. Change one
    wording in the API and this names the copy that did not follow."""
    from backend.app.api.two_factor_answers import SIGN_IN_EXPIRED_DETAIL
    from backend.app.api.v1.endpoints.users import (
        CURRENT_PASSWORD_INCORRECT_DETAIL,
        TWO_FACTOR_NO_PASSWORD_DETAIL,
        TWO_FACTOR_PASSWORD_MISSING_DETAIL,
    )
    from backend.app.services.two_factor_service import (
        CODE_STEP_LOCKED_TEXT,
        UNAVAILABLE_TEXT,
        WRONG_CODE_TEXT,
    )

    source = DASHBOARD_TEXTS.read_text(encoding="utf-8")
    literals = set(re.findall(r"'([^'\\\n]{1,300})'", source))
    dashboard: Dict[str, str] = {
        "WRONG_CODE_DETAIL": WRONG_CODE_TEXT,
        "SIGN_IN_EXPIRED_DETAIL": SIGN_IN_EXPIRED_DETAIL,
        "CODE_STEP_LOCKED_TEXT": CODE_STEP_LOCKED_TEXT,
        "UNAVAILABLE_TEXT": UNAVAILABLE_TEXT,
        "ENROL_PASSWORD_MISSING_DETAIL": TWO_FACTOR_PASSWORD_MISSING_DETAIL,
        "CURRENT_PASSWORD_INCORRECT_DETAIL": CURRENT_PASSWORD_INCORRECT_DETAIL,
        "ENROL_NO_PASSWORD_DETAIL": TWO_FACTOR_NO_PASSWORD_DETAIL,
    }
    missing = {name: text for name, text in dashboard.items() if text not in literals}
    assert missing == {}
    for name in dashboard:
        assert re.search(rf"^export const {name} =", source, re.M), name

    guide = " ".join(USER_GUIDE.read_text(encoding="utf-8").split())
    quoted = {"cap": CODE_STEP_LOCKED_TEXT, "unavailable": UNAVAILABLE_TEXT}
    assert {name: text for name, text in quoted.items() if text not in guide} == {}


def test_rotating_the_jwt_secret_says_to_reset_two_step_sign_in():
    section = _section(
        ROTATION.read_text(encoding="utf-8"), "### Rotate JWT Secret Immediately"
    )
    assert "two-step sign-in" in section
    assert "resetting-two-step-sign-in" in section
    assert "`--all`" in section
    assert "passwords first" in section
