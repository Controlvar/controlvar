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

The modules' identifiers are pinned the same way by a test of the modules'
own (``test_cross_version_identifiers_modules.py``), so that this core test
names no module path.
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
