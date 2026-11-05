"""TEMPORARY, remove before merge: what a workflow's GITHUB_TOKEN gets when it
reads a job's log (T174).

    python3 scripts/diag_rc_log_read.py JOB_ID

Run by ``.github/workflows/diag-rc-log-read.yml`` with the token the RC
verifier's verify job holds (``contents: read``, ``actions: read``). It prints
three lines and exits 0 whatever happens; the lines are the result:

1. ``verifier read``: the read ``scripts/rc_evidence.py`` makes for a P check
   (``GitHub.log``: two GETs, ``read_job_log``, not gh), reported as the
   verifier reports it (``unreadable_log``): PASS and the byte count, or the
   hop, its HTTP status and the storage's error code.
2. ``gh api -i``: the same GET through gh, with ``--include``, for comparison
   (gh exited 1 after a 200 here, run 41978116497). gh follows the redirect,
   so this is the LAST answer's status line, and its Location host if any.
3. ``first hop``: the same GET once more, redirect NOT followed: GitHub's own
   status line and the host its Location points at.

It never prints the token, a log's text, or a URL: a Location's query is a
signature, so only its host is printed. Standard library only.
"""

from __future__ import annotations

import importlib.util
import os
import pathlib
import re
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from typing import List, Mapping, Optional, Tuple

HERE = pathlib.Path(__file__).resolve().parent
API = "https://api.github.com"
JOB_ID = re.compile(r"[0-9]{1,20}")
HOST = re.compile(r"[A-Za-z0-9.-]{1,253}")
#: How much of gh's --include output is looked at for the status line and
#: headers (the body after them is the log, never printed).
HEAD_READ = 8192


def _rc():
    spec = importlib.util.spec_from_file_location(
        "rc_evidence_for_diag", HERE / "rc_evidence.py"
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


rc = _rc()


def host_of(location: Optional[str]) -> str:
    """The host a Location names, and nothing else of it; ``none`` or
    ``unparsable`` otherwise."""
    if not location:
        return "none"
    try:
        host = urllib.parse.urlsplit(location.strip()[:4096]).hostname or ""
    except ValueError:
        return "unparsable"
    return host if HOST.fullmatch(host) else "unparsable"


def verifier_read(
    repo: str, job: str, environ: Mapping[str, str], send: "rc.Send"
) -> str:
    """Line 1: the verifier's own read and its own words."""
    github = rc.GitHub(rc.run_gh, repo, send=send, environ=environ)
    try:
        log = github.log(int(job))
    except rc.LogError as error:
        return (
            f"verifier read: FAIL (status {error.status}, storage {error.storage}):"
            f" {rc.unreadable_log('the job', int(job), error)}"
        )
    if not log.strip():
        return "verifier read: FAIL: GitHub answered with an empty log"
    return f"verifier read: PASS, {len(log.encode('utf-8'))} bytes"


def include_read(repo: str, job: str) -> str:
    """Line 2: ``gh api -i`` once; its status line and Location host only."""
    done = subprocess.run(
        ["gh", "api", "-i", f"repos/{repo}/actions/jobs/{job}/logs"],
        capture_output=True,
        text=True,
        errors="replace",
        timeout=300,
        check=False,
    )
    head = (done.stdout or "")[:HEAD_READ]
    lines = head.replace("\r\n", "\n").split("\n")
    status = rc.said(lines[0]) if lines[0].startswith("HTTP/") else "no status line"
    location = None
    for line in lines[1:]:
        if not line.strip():
            break
        name, _, value = line.partition(":")
        if name.strip().lower() == "location":
            location = value
    return (
        f"gh api -i: exit {done.returncode}; status line: {status};"
        f" Location host: {host_of(location)}"
    )


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def first_hop(url: str, token: str) -> Tuple[int, str, Optional[str]]:
    """One GET of *url*, redirect not followed: status, reason, Location."""
    opener = urllib.request.build_opener(_NoRedirect)
    request = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "User-Agent": "controlvar-rc-diag",
        },
        method="GET",
    )
    try:
        with opener.open(request, timeout=60) as response:
            return (
                response.status,
                str(response.reason),
                response.headers.get("Location"),
            )
    except urllib.error.HTTPError as error:
        location = error.headers.get("Location") if error.headers else None
        error.close()
        return error.code, str(error.reason), location


def hop_read(repo: str, job: str, token: str) -> str:
    """Line 3: GitHub's own answer, before any redirect."""
    url = f"{API}/repos/{repo}/actions/jobs/{job}/logs"
    try:
        status, reason, location = first_hop(url, token)
    except (OSError, ValueError) as error:
        return f"first hop (redirect not followed): {type(error).__name__}"
    return (
        f"first hop (redirect not followed): HTTP {int(status)} {rc.said(reason)};"
        f" Location host: {host_of(location)}"
    )


def main(
    argv: List[str],
    environ: Mapping[str, str] = os.environ,
    send: Optional["rc.Send"] = None,
) -> int:
    try:
        job = argv[0] if argv else ""
        repo = environ.get("GITHUB_REPOSITORY", "")
        if not JOB_ID.fullmatch(job) or not rc.REPOSITORY_NAME.fullmatch(repo):
            print("diagnostic: a job id and GITHUB_REPOSITORY are needed")
            return 0
        print(verifier_read(repo, job, environ, send or rc.http_send))
        print(include_read(repo, job))
        print(hop_read(repo, job, environ.get("GH_TOKEN", "")))
    except Exception as error:  # a diagnostic: its output is the result
        print(f"diagnostic error: {type(error).__name__}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
