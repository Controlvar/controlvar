# SPDX-License-Identifier: Apache-2.0
"""A workspace slug must match the whole pattern (#1836).

``$`` also matches just before a final newline, so ``re.match`` with a pattern
ending in ``$`` kept ``"my-team\\n"`` as the slug. ``fullmatch`` refuses it with
the message any other invalid slug gets.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from modules.backend.app.schemas.workspaces import CreateWorkspaceRequest

pytestmark = pytest.mark.unit

STANDARD_MESSAGE = "Slug must be 3–50 characters, lowercase alphanumeric with hyphens"


@pytest.mark.regression
@pytest.mark.parametrize("slug", ["my-team\n", "my-team\n\n", "my-team\r\n"])
def test_a_slug_with_a_trailing_newline_is_refused(slug):
    with pytest.raises(ValidationError) as excinfo:
        CreateWorkspaceRequest(name="n", slug=slug)
    assert STANDARD_MESSAGE in str(excinfo.value)


@pytest.mark.regression
def test_a_trailing_newline_gets_the_message_any_invalid_slug_gets():
    messages = []
    for slug in ("my-team\n", "-bad", "Bad"):
        with pytest.raises(ValidationError) as excinfo:
            CreateWorkspaceRequest(name="n", slug=slug)
        messages.append(excinfo.value.errors()[0]["msg"])
    assert len(set(messages)) == 1


@pytest.mark.parametrize("slug", ["my-team", "abc", "a1-b2-c3", "a" * 50])
def test_a_valid_slug_still_passes_and_is_kept_as_given(slug):
    assert CreateWorkspaceRequest(name="n", slug=slug).slug == slug
