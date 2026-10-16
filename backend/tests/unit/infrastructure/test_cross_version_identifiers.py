"""Identifiers that two releases must agree on are pinned as literal text.

Each value below is matched by a machine across a version boundary: an
advisory-lock name two pods hash during a rolling upgrade, a token issuer or a
browser storage key a session was made with, a schema id stored next to
results and matched when they are read back, a header a customer configured in
another system, an image label a later workflow reads from an earlier release,
the compose project name that names a self-hoster's volumes. Change one and
the failure is silent: two schedulers hold "the" lock, every user is signed
out, stored results stop matching, a webhook answers 401, a deploy refuses an
old image, ``docker compose up`` starts on an empty database.

So none of them may change with a rename of the product, the chart or the
repository. Each is pinned here as the exact line of source that defines it,
read as text: importing the constant would compare it with itself and pass
whatever it became. A line moving within its file is fine; a line changing is
not, and the count is exact so a second, diverging copy is caught too.

Lines under ``modules/`` are checked when the tree has ``modules/`` (a core
build deletes it, and the core tree has no such line to keep).
"""

from __future__ import annotations

from pathlib import Path
from typing import List, Tuple

import pytest

pytestmark = [pytest.mark.unit, pytest.mark.regression]

REPO_ROOT = Path(__file__).resolve().parents[4]
MODULES_PRESENT = (REPO_ROOT / "modules").is_dir()

#: (path, the exact stripped line, how many times it occurs, what it keys).
IDENTIFIERS: List[Tuple[str, str, int, str]] = [
    (
        "backend/app/core/scheduler_lock.py",
        'LOCK_NAMESPACE = "experimently.scheduler."',
        1,
        "scheduler advisory-lock names, hashed by every pod",
    ),
    (
        "backend/app/db/bootstrap.py",
        'BOOTSTRAP_LOCK_NAMESPACE = "experimently.bootstrap."',
        1,
        "the bootstrap advisory lock",
    ),
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
        "backend/app/core/security.py",
        'LOCAL_TOKEN_ISSUER = "experimently-local"',
        1,
        "the iss of every session token",
    ),
    (
        "backend/app/api/v1/endpoints/websocket_results.py",
        'WS_AUTH_SUBPROTOCOL = "experimently.bearer"',
        1,
        "the live-results subprotocol, server side",
    ),
    (
        "frontend/src/hooks/useExperimentStream.ts",
        "export const WS_AUTH_SUBPROTOCOL = 'experimently.bearer';",
        1,
        "the live-results subprotocol, dashboard side",
    ),
    (
        "frontend/src/services/api.ts",
        "export const TOKEN_STORAGE_KEY = 'experimently.token';",
        1,
        "where the dashboard keeps the session",
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
    (
        "backend/Dockerfile",
        'LABEL io.experimently.profile="full"',
        1,
        "the profile label deploys read from any release's image",
    ),
    (
        "backend/Dockerfile",
        'LABEL io.experimently.profile="core"',
        1,
        "the profile label deploys read from any release's image",
    ),
    (
        "frontend/Dockerfile",
        'io.experimently.profile="${EXPERIMENTLY_PROFILE}"',
        1,
        "the dashboard image's profile label",
    ),
    (
        ".github/workflows/deploy.yml",
        "built=\"$(docker inspect --format '{{index .Config.Labels"
        ' "io.experimently.profile"}}\' "$image")"',
        2,
        "the deploy reading the profile label of a release's image",
    ),
    (
        "scripts/check_image_labels.py",
        '"io.experimently.profile": args.profile,',
        1,
        "the release check of the profile label",
    ),
    (
        "backend/lambda/feature_flag_evaluation/evaluator.py",
        '"FLAGS_TABLE", default="experimently-feature-flags"',
        1,
        "the default DynamoDB table name",
    ),
    (
        "deploy/compose/compose.yml",
        "name: experimently-selfhost",
        1,
        "the compose project name, which names a self-hoster's volumes",
    ),
]


def _ids() -> List[str]:
    return [f"{path}:{line[:40]}" for path, line, _, _ in IDENTIFIERS]


def occurrences(path: Path, line: str) -> int:
    return [raw.strip() for raw in path.read_text(encoding="utf-8").splitlines()].count(
        line
    )


@pytest.mark.parametrize("path, line, count, why", IDENTIFIERS, ids=_ids())
def test_cross_version_identifiers_are_literal(path, line, count, why):
    if path.startswith("modules/") and not MODULES_PRESENT:
        pytest.skip("a core tree: no modules/")
    source = REPO_ROOT / path
    assert source.is_file(), f"{path} is gone; it held {why}"
    found = occurrences(source, line)
    assert found == count, (
        f"{path}: {line!r} occurs {found} times, not {count}. It is {why}, "
        "matched across releases: it keeps its spelling through a rename."
    )


def test_the_pin_fires_on_a_changed_spelling(tmp_path):
    """A planted file with the value respelled, or copied twice, fails."""
    path, line, count, _ = IDENTIFIERS[0]
    planted = tmp_path / "scheduler_lock.py"
    planted.write_text(line.replace("experimently", "example") + "\n")
    assert occurrences(planted, line) == 0 != count
    planted.write_text(f"{line}\n    {line}\n")
    assert occurrences(planted, line) == 2 != count
