"""The modules' identifiers that two releases must agree on, pinned as text.

The same contract as ``backend/tests/unit/infrastructure/
test_cross_version_identifiers.py`` (read its docstring), for the values the
modules define: the warehouse advisory lock and stored schema ids, the SSO
storage key, state cookie, audiences and HMAC tags, and the webhook secret
header customers configured in Jira and Salesforce. Each is the exact line of
source that defines it, read as text, with an exact count.
"""

from __future__ import annotations

from pathlib import Path
from typing import List, Tuple

import pytest

pytestmark = [pytest.mark.unit, pytest.mark.regression]

REPO_ROOT = Path(__file__).resolve().parents[4]

#: (path, the exact stripped line, how many times it occurs, what it keys).
IDENTIFIERS: List[Tuple[str, str, int, str]] = [
    (
        "modules/backend/app/services/warehouse_runner.py",
        'ADMISSION_LOCK_NAME = "experimently.warehouse.runs"',
        1,
        "the warehouse admission advisory lock",
    ),
    (
        "modules/backend/app/services/warehouse_runner.py",
        'RESULTS_SCHEMA = "experimently.warehouse.results/v1"',
        1,
        "stored with warehouse results and matched on read",
    ),
    (
        "modules/backend/app/services/warehouse_sufficient_stats.py",
        'SCHEMA_ID: Final = "experimently.warehouse.sufficient_statistics/v2"',
        1,
        "stored with sufficient statistics and matched on read",
    ),
    (
        "modules/frontend/src/services/sso.ts",
        "export const SSO_STORAGE_KEY = 'experimently.sso';",
        1,
        "where the dashboard keeps the SSO session",
    ),
    (
        "modules/backend/app/services/sso_service.py",
        'OIDC_STATE_COOKIE = "__Host-experimently_oidc"',
        1,
        "the OIDC state cookie of a sign-in in flight",
    ),
    (
        "modules/backend/app/services/sso_service.py",
        '_STATE_AUDIENCE = "experimently:oidc-state"',
        1,
        "the aud of an OIDC state token in flight",
    ),
    (
        "modules/backend/app/services/sso_service.py",
        '_HANDOFF_AUDIENCE = "experimently:sso-handoff"',
        1,
        "the aud of an SSO hand-off code in flight",
    ),
    (
        "modules/backend/app/services/sso_service.py",
        'b"experimently:oidc-state:v1",',
        1,
        "the HMAC tag deriving the OIDC state key",
    ),
    (
        "modules/backend/app/services/sso_service.py",
        'b"experimently:sso-handoff:v1",',
        1,
        "the HMAC tag deriving the hand-off key",
    ),
    (
        "modules/backend/app/services/integrations/webhook_auth.py",
        'SHARED_SECRET_HEADER = "X-Experimently-Webhook-Secret"',
        1,
        "the header customers configured in Jira and Salesforce",
    ),
]


def occurrences(path: Path, line: str) -> int:
    lines = path.read_text(encoding="utf-8").splitlines()
    return [raw.strip() for raw in lines].count(line)


@pytest.mark.parametrize(
    "path, line, count, why",
    IDENTIFIERS,
    ids=[f"{path}:{line[:40]}" for path, line, _, _ in IDENTIFIERS],
)
def test_cross_version_identifiers_are_literal(path, line, count, why):
    source = REPO_ROOT / path
    assert source.is_file(), f"{path} is gone; it held {why}"
    found = occurrences(source, line)
    assert found == count, (
        f"{path}: {line!r} occurs {found} times, not {count}. It is {why}, "
        "matched across releases: it keeps its spelling through a rename."
    )
