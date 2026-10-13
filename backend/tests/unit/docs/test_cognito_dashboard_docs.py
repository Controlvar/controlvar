"""Every docs page that presents Cognito mode says the dashboard cannot sign in under it (#644).

Under ``AUTH_PROVIDER=cognito`` the dashboard's sign-in form posts to
``/api/v1/auth/login``, which answers 404, and an SSO sign-in issues a token
the Cognito provider refuses.  Until that changes (#708, #707) every page that
presents Cognito mode carries the same three sentences, :data:`NOTE`:

* the dashboard does not yet sign in with Cognito -- the sentence onboarding
  step 5 of ``docs/cognito_integration.md`` already uses, not a variant;
* sign in through the API with ``POST /api/v1/auth/token``;
* SSO sign-in needs ``AUTH_PROVIDER=local``.

The pages are DISCOVERED, not listed: any file under ``docs/`` (the generated
OpenAPI snapshots aside) that names ``AUTH_PROVIDER=cognito`` or carries a
"Cognito only" box must carry the note, so a new page that presents Cognito
fails here until it says so.  The note itself does not name
``AUTH_PROVIDER=cognito``, so deleting it cannot take a page out of the set.

Pinned without a database or the app (this runs on a docs-only pull request).
Walks the filesystem rather than asking git, so it also runs in the
``core_build.sh`` copy, which has no ``.git``.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import List, Set

import pytest

from backend.tests.unit.docs.test_cognito_integration_docs import section

pytestmark = [pytest.mark.unit, pytest.mark.regression]

REPO_ROOT = Path(__file__).resolve().parents[4]
DOCS = REPO_ROOT / "docs"
SSO_PAGE = DOCS / "auth" / "sso.md"

#: Onboarding step 5's sentence in docs/cognito_integration.md (#705).
SENTENCE = "The dashboard does not yet sign in with Cognito."
API_PATH = "Sign in through the API with `POST /api/v1/auth/token`."
SSO_CLAUSE = "SSO sign-in needs `AUTH_PROVIDER=local`."
NOTE = f"{SENTENCE} {API_PATH} {SSO_CLAUSE}"

#: ``AUTH_PROVIDER=cognito``, also as ``AUTH_PROVIDER: cognito`` or quoted.
PRESENTS_COGNITO = re.compile(r"AUTH_PROVIDER\s*[=:]\s*[\"'`]?cognito\b")
#: An mkdocs admonition titled "Cognito only", of any kind.
COGNITO_ONLY_BOX = re.compile(r'^\s*!!!\s+\w+\s+"Cognito only"', re.M)


def _doc_files() -> List[Path]:
    """Every file under docs/ except the generated OpenAPI snapshots and the
    claims registries (docs/_claims/, which mkdocs does not publish: they quote
    the pages' sentences, so reading them would count each page twice)."""
    found = []
    for dirpath, dirnames, filenames in os.walk(DOCS):
        dirnames[:] = sorted(
            d
            for d in dirnames
            if not d.startswith(".") and not (dirpath == str(DOCS) and d == "_claims")
        )
        for name in sorted(filenames):
            if name.startswith("openapi") and name.endswith(".json"):
                continue
            found.append(Path(dirpath) / name)
    return found


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace")


def _flat(text: str) -> str:
    """Whitespace collapsed, so a sentence wrapped across lines, or indented
    inside an admonition, still matches."""
    return " ".join(text.split())


def _rel(path: Path) -> str:
    return path.relative_to(REPO_ROOT).as_posix()


def pages_presenting_cognito() -> Set[str]:
    return {
        _rel(path)
        for path in _doc_files()
        if PRESENTS_COGNITO.search(_read(path)) or COGNITO_ONLY_BOX.search(_read(path))
    }


def pages_carrying_the_note() -> Set[str]:
    return {_rel(path) for path in _doc_files() if NOTE in _flat(_read(path))}


def test_every_page_presenting_cognito_carries_the_note():
    presenting = pages_presenting_cognito()
    carrying = pages_carrying_the_note()
    assert presenting == carrying, (
        f"present Cognito mode without the note: {sorted(presenting - carrying)}; "
        f"carry the note without presenting Cognito mode: {sorted(carrying - presenting)}"
    )


def test_every_page_presenting_cognito_names_the_sso_clause():
    """Named on its own so that dropping it reads as what it is."""
    missing = sorted(
        page
        for page in pages_presenting_cognito()
        if SSO_CLAUSE not in _flat(_read(REPO_ROOT / page))
    )
    assert not missing, f"no {SSO_CLAUSE!r} on {missing}"


def test_the_sentence_is_onboarding_step_5s():
    """One sentence, not a variant: the one #705 wrote into "Adding a user"."""
    assert SENTENCE in _flat(section("Adding a user"))


def test_the_note_does_not_name_the_discovery_pattern():
    """Otherwise a page whose only mention of Cognito mode is the note would
    leave both sets together when the note is deleted, and pass."""
    assert not PRESENTS_COGNITO.search(NOTE)
    assert not COGNITO_ONLY_BOX.search(NOTE)


def test_both_discovery_rules_find_pages():
    """Not vacuous: each rule finds a page the other does not need to."""
    files = _doc_files()
    assert any(PRESENTS_COGNITO.search(_read(p)) for p in files)
    assert any(COGNITO_ONLY_BOX.search(_read(p)) for p in files)
    assert "docs/cognito_integration.md" in pages_presenting_cognito()


def test_the_sso_guide_says_sso_needs_local_and_names_the_follow_up():
    text = _flat(_read(SSO_PAGE))
    assert SSO_CLAUSE in text
    assert "issues/707" in text
