"""The release before the write path, on a database this release wrote (#2012).

That release reads and serves a split and a rule's own value, but no request of
its sets either. So a task of it, during a rolling upgrade to this release or
after an API-only rollback from it, meets flags this release's API set:

* for 100 users it answers each such flag exactly as this release does, value
  included, in production and in staging;
* its PUT refuses a split (``extra_forbidden``) and a rule's own value
  (``unknown key``), and a PUT that leaves both out keeps them.

Release N-1 is a copy of this tree without what lets a request take either
(``split_write_path``). The release before that, 0.48.0, is
``test_flag_split_migration.py``'s: it ignores both and serves the on value.
"""

from __future__ import annotations

import json
import shutil
import uuid

import pytest

from backend.tests.integration.database import tree_profiles
from backend.tests.integration.database.split_read_path import without_the_read_path
from backend.tests.integration.database.split_write_path import without_the_write_path
from backend.tests.integration.database.test_flag_environments_migration import (
    _insert_key,
    _token,
    _user,
)
from backend.tests.integration.database.test_flag_split_migration import (
    BOOTSTRAPPED,
    BOTH_PROFILES,
    DASHBOARD_RULE,
    FULL,
    NATIVE_RULE,
    _answers,
    _at_head,
    _client,
    _evaluate,
    _split_and_rules,
    scratch_schema,
)

pytestmark = [pytest.mark.integration, pytest.mark.requires_db]

USERS = [f"user-{n}" for n in range(100)]
PRO = {"plan": "pro"}
FLAGS = "/api/v1/feature-flags/"
SPLIT = [
    {"value": "red", "weight": 34},
    {"value": "green", "weight": 33},
    {"value": "blue", "weight": 33},
]
STAGING_SPLIT = [SPLIT[2], SPLIT[0], {**SPLIT[1], "weight": 33}]
#: What this release's API writes: a split flag and two rule-value flags.
WRITTEN = {
    "split-flag": {"split": SPLIT, "rollout_percentage": 60},
    "rule-value-flag": {
        "targeting_rules": {**DASHBOARD_RULE, "value": "pro-copy"},
        "split": SPLIT,
    },
    "native-rule-value-flag": {
        "targeting_rules": {"rules": [{**NATIVE_RULE, "value": "native-copy"}]}
    },
}


def _release_before_the_write_path(profile, tmp_path, *, serving=True):
    destination = tmp_path / "release-before-the-write-path"
    if profile == FULL:
        tree = tree_profiles.full_tree(destination)
    else:
        tree = tree_profiles.core_tree(destination)
    without_the_write_path(tree)
    if not serving:
        # The planted defect: a release that does not serve either.
        without_the_read_path(tree)
    return tree


def _write(tree, schema, tmp_path, headers) -> dict:
    """Each flag of :data:`WRITTEN`, created through this release's API, and
    staging's split for the first one; ``{key: id}``."""
    suffix = uuid.uuid4().hex[:6]
    requests = [
        [
            "POST",
            FLAGS,
            headers,
            {
                "key": f"{key}-{suffix}",
                "name": key,
                "value_type": "string",
                "on_value": "A",
                "off_value": "Z",
                "is_active": True,
                "rollout_percentage": 100,
                **fields,
            },
        ]
        for key, fields in WRITTEN.items()
    ]
    created = _answers(tree, schema, tmp_path, requests)
    assert [status for status, _, _ in created] == [201] * len(WRITTEN), created
    ids = {body["key"]: body["id"] for _, body, _ in created}
    staging = {"split": STAGING_SPLIT, "is_active": True, "rollout_percentage": 100}
    first = next(iter(ids.values()))
    (put,) = _answers(
        tree,
        schema,
        tmp_path,
        [["PUT", f"{FLAGS}{first}/environments/staging", headers, staging]],
    )
    assert put[0] == 200, put
    return ids


def _every_answer(tree, schema, tmp_path, keys, flag_keys) -> list:
    asked = [
        (environment, flag_key, user, context)
        for environment in keys
        for flag_key in flag_keys
        for user in USERS
        for context in (None, PRO)
    ]
    answers = _answers(
        tree,
        schema,
        tmp_path,
        [_evaluate(k, u, keys[e], c) for e, k, u, c in asked],
    )
    assert {status for status, _, _ in answers} == {200}, answers[:3]
    return [body for _, body, _ in answers]


@pytest.mark.regression
@pytest.mark.parametrize("profile", BOTH_PROFILES)
def test_the_release_before_the_write_path_serves_what_this_one_wrote(
    profile, test_db, scratch_schema, tmp_path
):
    tree = _at_head(test_db, profile, BOOTSTRAPPED, scratch_schema, tmp_path)
    user = _user(test_db, scratch_schema)
    headers = {"Authorization": f"Bearer {_token(user)}"}
    ids = _write(tree, scratch_schema, tmp_path, headers)
    keys = {
        environment: _insert_key(test_db, scratch_schema, user.id, environment)[1]
        for environment in ("production", "staging")
    }
    stored = _split_and_rules(test_db, scratch_schema, ids.values())

    this_release = _every_answer(tree, scratch_schema, tmp_path, keys, list(ids))
    values = {body["value"] for body in this_release}
    assert {"red", "green", "blue", "pro-copy", "native-copy", "Z"} <= values

    before = _release_before_the_write_path(profile, tmp_path)
    answered = _every_answer(before, scratch_schema, tmp_path, keys, list(ids))
    assert answered == this_release

    # Its PUT refuses both, and keeps both when it sends neither.
    rule_value_flag = ids[next(k for k in ids if k.startswith("rule-value-flag"))]
    puts = _answers(
        before,
        scratch_schema,
        tmp_path,
        [
            *(
                ["PUT", FLAGS + flag_id, headers, {"description": "N-1 was here"}]
                for flag_id in ids.values()
            ),
            ["PUT", FLAGS + rule_value_flag, headers, {"split": SPLIT}],
            [
                "PUT",
                FLAGS + rule_value_flag,
                headers,
                {"targeting_rules": {**DASHBOARD_RULE, "value": "B"}},
            ],
        ],
    )
    assert [status for status, _, _ in puts] == [200] * len(ids) + [422, 422], puts
    assert puts[-2][1]["detail"][0]["type"] == "extra_forbidden", puts[-2]
    assert "unknown key" in json.dumps(puts[-1][1]), puts[-1]
    assert all("split" not in body for _, body, _ in puts[: len(ids)])
    assert _split_and_rules(test_db, scratch_schema, ids.values()) == stored
    shutil.rmtree(before, ignore_errors=True)

    planted = _release_before_the_write_path(
        profile, tmp_path / "planted", serving=False
    )
    assert (
        _every_answer(planted, scratch_schema, tmp_path, keys, list(ids))
        != this_release
    )
    shutil.rmtree(planted, ignore_errors=True)
