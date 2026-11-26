"""The words the flag environments guide quotes, held against the code (#2011).

The guide, ``docs/feature-flags/environments.md``, quotes two kinds of text
word for word: what the API answers, and what the dashboard says. A reader
searches a page for what a screen or a response said, so a message edited in
code alone leaves the guide describing words nobody sees.

This file holds the code side. ``GUIDE_QUOTES`` is the one list of what the
guide quotes, and each entry is held against its source here: the API's
messages by calling the code that makes them, the dashboard's words by finding
the literal in the file that renders them. The page side, that every entry is
in the pages that quote it, is ``backend/tests/unit/docs/test_environments_guide.py``,
which imports this list. Edit a message in code and this file fails; edit the
guide and that one does.

It reads no page, so the claims registry can name it.
"""

from __future__ import annotations

from pathlib import Path
from typing import List, NamedTuple

import pytest
from pydantic import TypeAdapter, ValidationError

from backend.app.api.key_environment import production_only_detail
from backend.app.core.flag_environments import FlagEnvironmentName

pytestmark = [pytest.mark.unit, pytest.mark.regression]

REPO_ROOT = Path(__file__).resolve().parents[4]

#: What a staging key is told on a route that runs in production only.
FORBIDDEN_STAGING = (
    "This API key belongs to the staging environment. Experiments and event "
    "tracking run in production only; use a production key."
)
#: What the standard 422 says of a name that is not an environment.
UNKNOWN_ENVIRONMENT = "Input should be 'production', 'staging' or 'development'"


class Word(NamedTuple):
    """A dashboard word the guide quotes, and where its literal is."""

    quote: str
    source: str
    #: What the source holds, as written there: a plain string, or a template
    #: whose parts are checked one by one.
    literals: tuple


#: Every dashboard word the guide quotes, with the literal that makes it.
DASHBOARD_WORDS: List[Word] = [
    Word(
        "You are editing Production. Changes affect live users.",
        "frontend/src/utils/flagEnvironments.ts",
        ("'You are editing Production. Changes affect live users.'",),
    ),
    Word(
        "Live",
        "frontend/src/utils/flagEnvironments.ts",
        ("PRODUCTION_MARKER = 'Live'",),
    ),
    Word(
        "Environment",
        "frontend/src/utils/flagEnvironments.ts",
        ("ENVIRONMENT_LABEL = 'Environment'",),
    ),
    Word(
        "Rollout schedules, safety monitoring and experiments run in Production only.",
        "frontend/src/utils/flagEnvironments.ts",
        (
            "PRODUCTION_ONLY_NOTE = 'Rollout schedules, safety monitoring and "
            "experiments run in Production only.'",
        ),
    ),
    Word(
        "Not configured in Staging",
        "frontend/src/utils/flagEnvironments.ts",
        (
            "`Not configured in ${FLAG_ENVIRONMENT_LABELS[environment]}`",
            "staging: 'Staging'",
        ),
    ),
    Word(
        "Save changes in Staging",
        "frontend/src/pages/feature-flags/[id].tsx",
        ("`Save changes in ${environmentLabel}`",),
    ),
    Word(
        "Compare environments",
        "frontend/src/components/feature-flags/FlagEnvironmentSections.tsx",
        ("Compare environments",),
    ),
    Word(
        "Promote to Staging",
        "frontend/src/utils/flagPromotion.ts",
        (
            "`Promote to ${FLAG_ENVIRONMENT_LABELS[target]}`",
            "if (environment === 'development') return 'staging';",
        ),
    ),
    Word(
        "Promote to Production",
        "frontend/src/utils/flagPromotion.ts",
        (
            "`Promote to ${FLAG_ENVIRONMENT_LABELS[target]}`",
            "if (environment === 'staging') return 'production';",
        ),
    ),
    Word("Status", "frontend/src/utils/flagPromotion.ts", ("status: 'Status'",)),
    Word(
        "Rollout percentage",
        "frontend/src/utils/flagPromotion.ts",
        ("rollout: 'Rollout percentage'",),
    ),
    Word(
        "Targeting rules",
        "frontend/src/utils/flagPromotion.ts",
        ("rules: 'Targeting rules'",),
    ),
    Word(
        "Create API Key",
        "frontend/src/components/admin/api-keys/ApiKeyTable.tsx",
        ("Create API Key",),
    ),
    Word(
        "Your API keys",
        "frontend/src/components/AppShell.tsx",
        ("label: 'Your API keys'",),
    ),
    Word(
        "Environment",
        "frontend/src/components/admin/api-keys/keyEnvironment.ts",
        ("KEY_ENVIRONMENT_LABEL = 'Environment'",),
    ),
]

#: Every message of the API the guide quotes, as the code makes it.
API_MESSAGES = [FORBIDDEN_STAGING, UNKNOWN_ENVIRONMENT]

#: The one list the page side checks: what the guide must quote word for word.
GUIDE_QUOTES: List[str] = [
    *API_MESSAGES,
    *dict.fromkeys(word.quote for word in DASHBOARD_WORDS),
]


def test_the_403_is_the_text_the_guide_quotes():
    assert production_only_detail("staging") == FORBIDDEN_STAGING
    assert production_only_detail("development") == FORBIDDEN_STAGING.replace(
        "staging", "development"
    )


def test_the_422_is_the_text_the_guide_quotes():
    """The standard message pydantic gives a name outside the Literal, which
    the routes' path and body fields both are."""
    for bad in ("qa", "Production", "prod", ""):
        with pytest.raises(ValidationError) as refused:
            TypeAdapter(FlagEnvironmentName).validate_python(bad)
        (error,) = refused.value.errors()
        assert error["msg"] == UNKNOWN_ENVIRONMENT, bad


@pytest.mark.parametrize("word", DASHBOARD_WORDS, ids=lambda word: word.quote)
def test_each_dashboard_word_is_in_the_dashboard(word: Word):
    text = (REPO_ROOT / word.source).read_text(encoding="utf-8")
    missing = [literal for literal in word.literals if literal not in text]
    assert not missing, (
        f"the guide quotes {word.quote!r}, which {word.source} no longer says "
        f"as {missing}: change the guide with it"
    )


def test_the_list_quotes_something_and_every_source_exists():
    assert len(DASHBOARD_WORDS) >= 15 and len(API_MESSAGES) == 2
    for word in DASHBOARD_WORDS:
        assert (REPO_ROOT / word.source).is_file(), word.source
