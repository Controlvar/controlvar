"""Which SDKs send error reports, and which retry after a 429, from their own source (#2029).

The limitations page and the safety page say that only the JavaScript and
Python SDKs can send an error report (``POST /api/v1/tracking/errors``), and
that only those two retry after a ``Retry-After`` on a 429. The claims for
those sentences are held here, by reading each SDK's non-test source under
``sdk/``; nothing here reads a page.

* The SDK directories whose source names the error-report path or an error
  report call (``reportError``, ``report_error``) are exactly ``js`` and
  ``python``. A new SDK that adds one fails here until the pages say so. The
  one directory left out is ``visual``, the visual editor's browser script
  (not yet available): its reports never carry a feature flag key, so none
  reaches safety rollback, which is what the pages' sentence is about. Its
  contract file and its source are checked for that here.
* The directories whose source reads a ``Retry-After`` header are exactly
  ``go``, ``js``, ``mcp`` and ``python``. Of those, only ``js`` and ``python``
  send the request again: the JavaScript client keys its single retry on
  ``response.status === 429 && !init.retried``, the Python client on
  ``response.status == 429``, and neither the Go client nor the MCP server's
  has either; Go puts the header in ``APIError.RetryAfter`` and the MCP server
  puts it in its message.
* The two retrying clients cap the wait at their own figures, read from the
  source: the JavaScript client at its ``timeoutMs``, the Python client at
  ``MAX_RETRY_AFTER_SECONDS`` (5.0).

What each retrying client does with a 429 is exercised by its own suite
(``sdk/js/__tests__/client.test.ts``, ``sdk/python/tests/test_client.py``).

* A line naming 429 (or ``TooManyRequests``), not a comment, with a sleep, timer
  or retry call within three lines either side, is found in js and python
  only: an SDK that retries on a 429 with a fixed sleep and no ``Retry-After``
  fails here too.
* Both OpenFeature providers depend on the client of the SDK they delegate to
  (``@controlvar/js-sdk``; ``controlvar``) and call it for every evaluation, so
  they get its retry. Neither has an error-report call of its own.

Not seen: a retry in a library an SDK wraps, or one written with neither a 429
nor a sleep, timer or retry call in the window, which this scan cannot find.

Reads files only (no git), so it runs the same in core_build's copy.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Dict, Set

import pytest

pytestmark = [pytest.mark.unit]

REPO_ROOT = Path(__file__).resolve().parents[4]
SDK_ROOT = REPO_ROOT / "sdk"

SKIP_DIRS = frozenset(
    {
        "node_modules",
        "dist",
        "build",
        ".build",
        ".gradle",
        "__tests__",
        "tests",
        "test",
        "Tests",
        "spec",
        "examples",
        "example",
        ".dart_tool",
        "vendor",
        "deps",
        "_build",
        "__pycache__",
    }
)
SOURCE_SUFFIXES = frozenset(
    {
        ".ts",
        ".tsx",
        ".js",
        ".mjs",
        ".py",
        ".go",
        ".rb",
        ".java",
        ".kt",
        ".swift",
        ".dart",
        ".php",
        ".cs",
        ".ex",
        ".exs",
    }
)

ERROR_REPORT = re.compile(r"tracking/errors|reportError|report_error|ReportError")
RETRY_AFTER_HEADER = re.compile(r"retry-after|retry_after|retryAfter", re.IGNORECASE)

EXPECTED_REPORTERS = {"js", "python"}
EXPECTED_HEADER_READERS = {"go", "js", "mcp", "python"}
#: Directories that send error reports none of which can reach safety rollback.
NO_FLAG_KEY_REPORTERS = {
    "visual": "the visual editor's browser script: its reports have exactly "
    "error_type, experiment_key, message and metadata, never a feature flag key",
}
VISUAL_REPORTS = REPO_ROOT / "tests" / "sdk-contract" / "visual-error-reports.json"


def _source_files(sdk: Path):
    for path in sorted(sdk.rglob("*")):
        rel_parts = path.relative_to(sdk).parts
        if not path.is_file() or path.suffix not in SOURCE_SUFFIXES:
            continue
        if any(part in SKIP_DIRS for part in rel_parts[:-1]):
            continue
        name = path.name
        if name.endswith(
            ("_test.go", ".test.ts", ".test.tsx", ".spec.ts")
        ) or name.startswith("test_"):
            continue
        yield path


def _sdks() -> Dict[str, Path]:
    return {p.name: p for p in sorted(SDK_ROOT.iterdir()) if p.is_dir()}


def _dirs_matching(pattern: "re.Pattern[str]") -> Set[str]:
    found: Set[str] = set()
    for name, sdk in _sdks().items():
        for path in _source_files(sdk):
            if pattern.search(path.read_text(encoding="utf-8", errors="replace")):
                found.add(name)
                break
    return found


def test_the_scan_sees_every_sdk_directory():
    # A positive control: the scan is not vacuous, and the SDKs the pages name are there.
    assert {"js", "python", "go", "mcp", "ruby", "java", "react", "openfeature"} <= set(
        _sdks()
    )
    assert list(_source_files(SDK_ROOT / "js")), "no JavaScript source found"


def test_only_the_javascript_and_python_sdks_send_error_reports():
    reporters = _dirs_matching(ERROR_REPORT) - set(NO_FLAG_KEY_REPORTERS)
    assert reporters == EXPECTED_REPORTERS


def test_the_visual_scripts_reports_never_carry_a_feature_flag_key():
    # The one directory left out above: its documented request keys, and its
    # source, which never names the field.
    assert set(NO_FLAG_KEY_REPORTERS) == {"visual"}
    contract = json.loads(VISUAL_REPORTS.read_text(encoding="utf-8"))
    assert contract["request_keys"] == [
        "error_type",
        "experiment_key",
        "message",
        "metadata",
    ]
    sources = list(_source_files(SDK_ROOT / "visual"))
    assert sources, "no visual script source found"
    for path in sources:
        assert "feature_flag" not in path.read_text(encoding="utf-8"), path


def test_the_error_report_path_is_in_both_clients():
    js = (SDK_ROOT / "js/src/client.ts").read_text(encoding="utf-8")
    py = (SDK_ROOT / "python/controlvar/client.py").read_text(encoding="utf-8")
    assert "'/api/v1/tracking/errors'" in js and "async reportError(" in js
    assert '"/api/v1/tracking/errors"' in py and "def report_error(" in py


def test_only_four_sdk_directories_read_a_retry_after_header():
    assert _dirs_matching(RETRY_AFTER_HEADER) == EXPECTED_HEADER_READERS


def test_only_the_javascript_and_python_clients_send_the_request_again_after_a_429():
    js = (SDK_ROOT / "js/src/client.ts").read_text(encoding="utf-8")
    py = (SDK_ROOT / "python/controlvar/client.py").read_text(encoding="utf-8")
    assert "response.status === 429 && !init.retried" in js
    assert "sleep(this.retryAfterMs(response))" in js
    assert "if response.status == 429:" in py
    assert "wait = _retry_after_seconds(response)" in py

    status_429 = re.compile(r"\b429\b")
    for other in sorted(EXPECTED_HEADER_READERS - EXPECTED_REPORTERS):
        for path in _source_files(_sdks()[other]):
            text = path.read_text(encoding="utf-8", errors="replace")
            # Go and MCP only look at a 429 to report it, never to send again.
            for line in text.splitlines():
                if status_429.search(line):
                    assert not re.search(
                        r"sleep|retry\(|Retry\(|time\.After|setTimeout", line
                    ), (path, line)
    go = (SDK_ROOT / "go/http.go").read_text(encoding="utf-8")
    assert 'RetryAfter: resp.Header.Get("Retry-After")' in go
    assert "time.Sleep" not in go and "time.After" not in go


def test_the_two_retrying_clients_cap_the_wait_at_their_own_figures():
    js = (SDK_ROOT / "js/src/client.ts").read_text(encoding="utf-8")
    py = (SDK_ROOT / "python/controlvar/client.py").read_text(encoding="utf-8")
    assert "return Math.min(Math.max(ms, 0), this.timeoutMs);" in js
    assert re.search(r"^MAX_RETRY_AFTER_SECONDS = 5\.0$", py, re.MULTILINE)
    assert "return max(0.0, min(seconds, MAX_RETRY_AFTER_SECONDS))" in py


STATUS_429 = re.compile(r"\b429\b|TooManyRequests|TOO_MANY_REQUESTS", re.IGNORECASE)
WAIT_OR_RETRY = re.compile(
    r"sleep|setTimeout|time\.After|backoff|\bretr(y|ies|ied)\(|retried|\bTimer\b|Thread\.",
    re.IGNORECASE,
)
COMMENT_STARTS = ("//", "#", "*", "/*")


def _retrying_dirs() -> Set[str]:
    found: Set[str] = set()
    for name, sdk in _sdks().items():
        for path in _source_files(sdk):
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
            for i, line in enumerate(lines):
                if line.strip().startswith(COMMENT_STARTS):
                    continue
                if not STATUS_429.search(line):
                    continue
                if WAIT_OR_RETRY.search(" ".join(lines[max(0, i - 3) : i + 4])):
                    found.add(name)
    return found


def test_no_other_sdk_waits_or_retries_near_a_429():
    assert _retrying_dirs() == EXPECTED_REPORTERS


def test_both_openfeature_providers_delegate_to_the_clients_that_retry():
    package = (SDK_ROOT / "openfeature/package.json").read_text(encoding="utf-8")
    assert '"@controlvar/js-sdk"' in package
    pyproject = (SDK_ROOT / "openfeature-python/pyproject.toml").read_text(
        encoding="utf-8"
    )
    assert '"controlvar>=' in pyproject
    ts = (SDK_ROOT / "openfeature/src/ExperimentationProvider.ts").read_text(
        encoding="utf-8"
    )
    assert "await this.client.evaluateFlag(" in ts
    py = (
        SDK_ROOT / "openfeature-python/src/controlvar_openfeature/provider.py"
    ).read_text(encoding="utf-8")
    assert "self._client.get_feature_flag(" in py
