"""`scripts/verify_release_gate.sh` against a fake `gh` (QA 3b).

The deploy refuses a tag whose commit did not pass the Release Gate. The lookup
it replaced read `commits/<sha>/check-runs` once, unfiltered: the API pages at
30, v0.2.7's commit carried 33 check runs, and the gate was on page 2 -- so a
passing release read as `Result=''` and a deploy could only ever be refused or,
depending on ordering, allowed for the wrong reason.

The fake below is a small model of the one endpoint the script calls: it
filters by `check_name` exactly as the API does, pages at `per_page` (default
30, as the API does), returns only the requested page unless `--paginate` is
given, and applies `--jq` with the real `jq` per page, as `gh` does. So a
script that drops the filter, or the pagination, meets the same 30-row cut-off
production would.

No git and no network: this runs in the ordinary unit job and in
`core_build.sh`'s copy of the tree.
"""

from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[4]
SCRIPT = REPO_ROOT / "scripts" / "verify_release_gate.sh"

REPO = "Controlvar/controlvar"
SHA = "0123456789abcdef0123456789abcdef01234567"
TAG = "v1.2.3"
GATE = "Release Gate Summary"

FAKE_GH = r'''#!{python}
"""A model of `gh api [--paginate] <endpoint> --jq <expr>` for check runs."""
import json, os, subprocess, sys
from urllib.parse import parse_qs, unquote, urlsplit

args = sys.argv[1:]
with open(os.environ["FAKE_GH_LOG"], "a") as log:
    log.write(json.dumps(args) + "\n")
if os.environ.get("FAKE_GH_FAIL"):
    print("HTTP 502: Bad Gateway", file=sys.stderr)
    sys.exit(1)
assert args[0] == "api", args
paginate = "--paginate" in args
jq = args[args.index("--jq") + 1] if "--jq" in args else None
endpoint = next(a for a in args[1:] if not a.startswith("-") and a != jq)
url = urlsplit(endpoint)
query = {{k: v[0] for k, v in parse_qs(url.query).items()}}
runs = json.load(open(os.environ["FAKE_GH_RUNS"]))
name = query.get("check_name")
if name is not None:
    if os.environ.get("FAKE_GH_LENIENT"):
        runs = [r for r in runs if unquote(name) in r["name"]]
    else:
        runs = [r for r in runs if r["name"] == unquote(name)]
per_page = int(query.get("per_page", 30))
pages = [runs[i:i + per_page] for i in range(0, len(runs), per_page)] or [[]]
wanted = pages if paginate else [pages[int(query.get("page", 1)) - 1]]
for page in wanted:
    body = json.dumps({{"total_count": len(runs), "check_runs": page}})
    if jq is None:
        sys.stdout.write(body)
    else:
        out = subprocess.run(["jq", "-r", jq], input=body, text=True,
                             capture_output=True, check=True).stdout
        sys.stdout.write(out)
'''


def _run(
    name: str,
    conclusion: str | None = "success",
    completed_at: str | None = None,
    status: str = "completed",
    n: int = 0,
) -> dict:
    return {
        "name": name,
        "status": status,
        "conclusion": conclusion,
        "completed_at": completed_at
        if completed_at is not None or status != "completed"
        else f"2026-09-26T10:{n:02d}:00Z",
        "html_url": f"https://github.com/{REPO}/actions/runs/{1000 + n}/job/{n}",
    }


def _others(count: int) -> list[dict]:
    return [_run(f"check {i}", n=i) for i in range(count)]


@pytest.fixture
def gate(tmp_path):
    """Run the script with a fake `gh` first on PATH; return (exit, output, calls)."""
    assert shutil.which("jq"), "jq is required by the fake gh (it is on every runner)"
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake = bin_dir / "gh"
    fake.write_text(FAKE_GH.format(python=sys.executable))
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)

    def run(runs: list[dict], **extra_env: str):
        runs_file = tmp_path / "runs.json"
        runs_file.write_text(json.dumps(runs))
        log = tmp_path / "calls.log"
        log.write_text("")
        env = {
            **os.environ,
            "PATH": f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}",
            "FAKE_GH_RUNS": str(runs_file),
            "FAKE_GH_LOG": str(log),
            **extra_env,
        }
        result = subprocess.run(
            ["bash", str(SCRIPT), REPO, SHA, TAG],
            env=env,
            capture_output=True,
            text=True,
        )
        calls = [json.loads(line) for line in log.read_text().splitlines()]
        return result.returncode, result.stdout + result.stderr, calls

    return run


MISSING = (
    f"{TAG} ({SHA[:7]}) has no Release Gate result. Release Gate runs on every "
    "push to main; for a tag cut before that, run it on the tag with "
    f"`gh workflow run release-gate.yml --ref {TAG}`, then re-run this deploy."
)


def _failed(url: str) -> str:
    return (
        f"Release Gate failed on {TAG}: {url}. A failed release cannot be "
        "deployed; cut a new one."
    )


def _running(url: str) -> str:
    return f"Release Gate is still running on {TAG}: {url}. Re-run when it finishes."


@pytest.mark.regression
def test_success(gate):
    code, out, calls = gate(_others(5) + [_run(GATE, n=40)])
    assert code == 0, out
    assert "Release Gate passed on v1.2.3" in out
    # Asked the way the contract says: filtered by name, paginated.
    (call,) = calls
    assert "--paginate" in call
    assert any("check_name=Release%20Gate%20Summary" in a for a in call), call


@pytest.mark.regression
def test_failure(gate):
    run = _run(GATE, conclusion="failure", n=40)
    code, out, _ = gate(_others(5) + [run])
    assert code == 1, out
    assert _failed(run["html_url"]) in out


@pytest.mark.regression
def test_absent(gate):
    code, out, _ = gate(_others(12))
    assert code == 1, out
    assert MISSING in out


@pytest.mark.regression
def test_success_on_page_two_of_thirty_three(gate):
    """v0.2.7's shape: 33 check runs, the gate the 33rd, the API pages at 30."""
    runs = _others(32) + [_run(GATE, n=40)]
    assert len(runs) == 33 and runs.index(runs[-1]) >= 30
    code, out, _ = gate(runs)
    assert code == 0, out


@pytest.mark.regression
def test_a_prefixed_name_is_not_the_gate(gate):
    """A reusable-workflow caller reports `CI / Release Gate Summary`.

    Fails closed, with the "no result" copy: that is a different check.
    """
    code, out, _ = gate(_others(3) + [_run(f"CI / {GATE}", n=40)])
    assert code == 1, out
    assert MISSING in out


@pytest.mark.regression
def test_a_prefixed_name_is_not_the_gate_even_if_the_api_returned_it(gate):
    """Defence in depth: the script compares the name itself.

    The real filter is exact; this fake matches a substring, so only the
    script's own comparison stands between the prefixed run and a deploy.
    """
    code, out, _ = gate(_others(3) + [_run(f"CI / {GATE}", n=40)], FAKE_GH_LENIENT="1")
    assert code == 1, out
    assert MISSING in out


@pytest.mark.regression
def test_running(gate):
    run = _run(GATE, conclusion=None, status="in_progress", n=41)
    code, out, _ = gate(_others(3) + [_run(GATE, n=40), run])
    assert code == 1, out
    assert _running(run["html_url"]) in out


@pytest.mark.regression
@pytest.mark.parametrize("later_listed_first", [True, False])
@pytest.mark.parametrize(
    "first, second, expected",
    [("failure", "success", 0), ("success", "failure", 1)],
)
def test_the_latest_completed_run_wins(
    gate, first, second, expected, later_listed_first
):
    """A re-run that passed after a flake is honoured; a later failure is not hidden.

    Both list orders, so neither "first listed" nor "last listed" can pass by
    coinciding with "latest".
    """
    earlier = _run(GATE, conclusion=first, completed_at="2026-09-26T09:00:00Z", n=1)
    later = _run(GATE, conclusion=second, completed_at="2026-09-26T11:00:00Z", n=2)
    code, out, _ = gate([later, earlier] if later_listed_first else [earlier, later])
    assert code == expected, out


def test_an_api_failure_is_not_a_verdict(gate):
    code, out, _ = gate([_run(GATE)], FAKE_GH_FAIL="1")
    assert code == 2, out
    assert "Could not read the check runs" in out


def test_usage():
    result = subprocess.run(["bash", str(SCRIPT)], capture_output=True, text=True)
    assert result.returncode == 2
    assert "usage:" in result.stderr
