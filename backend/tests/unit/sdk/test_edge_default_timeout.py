"""The edge SDK waits 500 ms for the API and then answers off (#2011).

The upgrade notes tell an operator that, while an upgrade holds a lock, an
SDK whose timeout is shorter than the wait answers its fallback meanwhile, and
give the edge SDK as the example: its default timeout is 500 ms, and a request
that times out resolves to the disabled evaluation, ``enabled: false``.

Both are read from the SDK's source, as ``test_kill_switch_numbers.py`` reads
the cache lifetimes: the default from the constant and the way the client
uses it, the answer from the client's own timeout test (jest runs it with the
SDK's suite). The file reads no page.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

pytestmark = [pytest.mark.unit, pytest.mark.regression]

REPO_ROOT = Path(__file__).resolve().parents[4]
CLIENT = REPO_ROOT / "sdk" / "edge" / "src" / "client.ts"
CLIENT_TESTS = REPO_ROOT / "sdk" / "edge" / "__tests__" / "client.test.ts"


def test_the_default_timeout_is_500_ms_and_the_client_uses_it():
    source = CLIENT.read_text(encoding="utf-8")
    assert re.search(r"^const DEFAULT_TIMEOUT_MS = 500;", source, re.M)
    assert "this.timeout = config.timeout ?? DEFAULT_TIMEOUT_MS;" in source
    assert "setTimeout(() => controller.abort(), this.timeout)" in source


def test_a_request_that_times_out_resolves_to_the_disabled_evaluation():
    tests = CLIENT_TESTS.read_text(encoding="utf-8")
    assert "aborts after `timeout` ms and returns the disabled evaluation" in tests
    assert "toEqual({ key: 'f', enabled: false, config: null })" in tests
