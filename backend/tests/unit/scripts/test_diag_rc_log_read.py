"""TEMPORARY, remove before merge: ``scripts/diag_rc_log_read.py`` and its
workflow (T174).

The diagnostic prints what a workflow token got for a job-log read. Its one
hard rule is what it must never print: the token, or the query of a signed
URL (a Location's query is a signature). Every answer here is made up: a fake
``gh`` (``subprocess.run``) and a fake first hop. Nothing reaches GitHub.
"""

from __future__ import annotations

import importlib.util
import io
import subprocess
import sys
import urllib.error
from pathlib import Path
from typing import Any, List, Optional, Tuple

import pytest

from backend.tests.unit.infrastructure.test_docs_only_gate import _load

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[4]
SCRIPT = REPO_ROOT / "scripts" / "diag_rc_log_read.py"
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "diag-rc-log-read.yml"
REPO = "Controlvar/controlvar"
JOB = "117518217494"


def _load_script():
    spec = importlib.util.spec_from_file_location("diag_rc_log_read_under_test", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


diag = _load_script()

#: Token-shaped, made up at run time: no token-shaped literal in this file.
FAKE_TOKEN = "gh" + "s_" + "Q7x" * 12
SIGNATURE = "Ab3" * 15
STORAGE_HOST = "productionresultssa0.blob.core.windows.net"
SIGNED_URL = (
    f"https://{STORAGE_HOST}/actions-results/x/job.txt"
    "?sv=2025-11-05&se=2026-11-05T05%3A30%3A00Z&sp=r&sig=" + SIGNATURE
)
LOG = "\N{ZERO WIDTH NO-BREAK SPACE}2026-10-17T22:15:34.3952163Z Current runner version: '2.337.0'\n"


def _gh(read: Tuple[int, str, str], include: Tuple[int, str, str]):
    """A fake ``gh``: *read* answers the verifier's plain GET, *include* the
    ``gh api -i`` one, each as (exit status, stdout, stderr)."""

    def run(argv: List[str], **kwargs: Any) -> subprocess.CompletedProcess:
        assert argv[0] == "gh" and argv[1] == "api", argv
        answer = include if "-i" in argv else read
        assert argv[-1] == f"repos/{REPO}/actions/jobs/{JOB}/logs", argv
        return subprocess.CompletedProcess(argv, answer[0], answer[1], answer[2])

    return run


def _run(monkeypatch, capsys, read, include, hop) -> str:
    monkeypatch.setattr(diag.subprocess, "run", _gh(read, include))
    monkeypatch.setattr(diag, "first_hop", hop)
    monkeypatch.setenv("GH_TOKEN", FAKE_TOKEN)
    environ = {"GITHUB_REPOSITORY": REPO, "GH_TOKEN": FAKE_TOKEN}
    assert diag.main([JOB], environ) == 0
    return capsys.readouterr().out


def _hop(status: int, reason: str, location: Optional[str]):
    def hop(url: str, token: str) -> Tuple[int, str, Optional[str]]:
        assert url == f"https://api.github.com/repos/{REPO}/actions/jobs/{JOB}/logs"
        assert token == FAKE_TOKEN
        return status, reason, location

    return hop


def test_a_readable_log_prints_its_size_and_where_github_sent_the_read(
    monkeypatch, capsys
):
    include = (0, f"HTTP/1.1 200 OK\r\nContent-Type: text/plain\r\n\r\n{LOG}", "")
    out = _run(
        monkeypatch, capsys, (0, LOG, ""), include, _hop(302, "Found", SIGNED_URL)
    )
    assert out.splitlines() == [
        f"verifier read: PASS, {len(LOG.encode())} bytes",
        "gh api -i: exit 0; status line: HTTP/1.1 200 OK; Location host: none",
        "first hop (redirect not followed): HTTP 302 Found; Location host: "
        + STORAGE_HOST,
    ]


def test_a_refused_read_prints_the_status_github_and_gh_gave(monkeypatch, capsys):
    said = "gh: Resource not accessible by integration (HTTP 403)\n"
    body = '{"message":"Resource not accessible by integration","status":"403"}'
    include = (1, f"HTTP/2.0 403 Forbidden\r\nContent-Type: json\r\n\r\n{body}", said)
    out = _run(
        monkeypatch, capsys, (1, body, said), include, _hop(403, "Forbidden", None)
    )
    lines = out.splitlines()
    assert lines[0].startswith("verifier read: FAIL (status 403, storage None):")
    assert lines[0].endswith(
        f"gh api repos/{REPO}/actions/jobs/{JOB}/logs failed (exit 1, HTTP 403):"
        " gh: Resource not accessible by integration (HTTP 403)"
    )
    assert lines[1:] == [
        "gh api -i: exit 1; status line: HTTP/2.0 403 Forbidden; Location host: none",
        "first hop (redirect not followed): HTTP 403 Forbidden; Location host: none",
    ]


@pytest.mark.regression
def test_it_never_prints_the_token_or_a_signed_url_s_query(monkeypatch, capsys):
    """Every answer carries the token and the signed URL, where gh and GitHub
    could put them: what is printed carries neither, only the host."""
    leaky = f"gh: refused {FAKE_TOKEN} at {SIGNED_URL} (HTTP 403)\n"
    blob = f"<Error><Code>AuthenticationFailed</Code><Url>{SIGNED_URL}</Url></Error>"
    include = (
        1,
        f"HTTP/1.1 403 {SIGNED_URL} {FAKE_TOKEN}\r\nLocation: {SIGNED_URL}\r\n"
        f"X-Echo: {FAKE_TOKEN}\r\n\r\n{blob}",
        leaky,
    )
    hop = _hop(302, f"Found {FAKE_TOKEN}", SIGNED_URL)
    out = _run(monkeypatch, capsys, (1, blob, leaky), include, hop)
    for secret in (FAKE_TOKEN, SIGNATURE, "sig=", "sv=", "?", "actions-results"):
        assert secret not in out, out
    assert out.count(f"Location host: {STORAGE_HOST}") == 2, out
    assert "storage AuthenticationFailed" in out


def test_it_exits_0_whatever_happens(monkeypatch, capsys):
    def broken(argv: List[str], **kwargs: Any) -> subprocess.CompletedProcess:
        raise RuntimeError(FAKE_TOKEN)

    monkeypatch.setattr(diag.subprocess, "run", broken)
    assert diag.main([JOB], {"GITHUB_REPOSITORY": REPO}) == 0
    out = capsys.readouterr().out
    assert out.strip() == "diagnostic error: RuntimeError"
    assert diag.main(["not-a-job"], {"GITHUB_REPOSITORY": REPO}) == 0


def test_the_first_hop_does_not_follow_the_redirect(monkeypatch):
    """The real ``first_hop``: the redirect comes back as the answer, with its
    Location, and the token goes only into the request's header."""
    seen = {}

    class Opener:
        def open(self, request, timeout):
            seen["auth"] = request.get_header("Authorization")
            seen["handlers"] = handlers
            raise urllib.error.HTTPError(
                request.full_url, 302, "Found", {"Location": SIGNED_URL}, io.BytesIO()
            )

    def build_opener(*given):
        handlers.extend(given)
        return Opener()

    handlers: List[Any] = []
    monkeypatch.setattr(diag.urllib.request, "build_opener", build_opener)
    status, reason, location = diag.first_hop("https://api.github.com/x", FAKE_TOKEN)
    assert (status, reason, location) == (302, "Found", SIGNED_URL)
    assert seen["auth"] == f"Bearer {FAKE_TOKEN}"
    assert handlers == [diag._NoRedirect]
    assert diag._NoRedirect().redirect_request(None, None, 302, "", {}, "x") is None


@pytest.mark.parametrize(
    "location, host",
    [
        (SIGNED_URL, STORAGE_HOST),
        (None, "none"),
        ("", "none"),
        ("/relative?sig=x", "unparsable"),
        ("https://bad host/?sig=x", "unparsable"),
    ],
)
def test_only_a_location_s_host_is_kept(location, host):
    assert diag.host_of(location) == host


def test_the_workflow_runs_it_with_the_verify_job_s_token_and_is_temporary():
    text = WORKFLOW.read_text(encoding="utf-8")
    assert "TEMPORARY, remove before merge" in text
    doc = _load(WORKFLOW)
    on = doc.get("on", doc.get(True))
    assert list(on) == ["pull_request"]
    assert set(on["pull_request"]["paths"]) == {
        ".github/workflows/diag-rc-log-read.yml",
        "scripts/rc_evidence.py",
        "scripts/diag_rc_log_read.py",
    }
    read = {"contents": "read", "actions": "read"}
    assert doc["permissions"] == read
    (job,) = doc["jobs"].values()
    assert job["permissions"] == read
    step = job["steps"][-1]
    assert step["env"]["GH_TOKEN"] == "${{ github.token }}"
    assert step["env"]["JOB_ID"] == JOB
    assert step["run"] == 'python3 scripts/diag_rc_log_read.py "$JOB_ID"'
    assert job["steps"][0]["with"]["persist-credentials"] is False
