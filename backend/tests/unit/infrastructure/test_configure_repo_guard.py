"""configure-repo.sh writes the protection main has, and check_required_checks.sh shows drift.

``scripts/configure-repo.sh`` PUTs the whole branch protection body. Live
protection on main is ``strict: false``, no review rule, and 20 checks each
pinned to GitHub Actions (app 15368); the script's body says the same
(``strict: false``, ``required_pull_request_reviews: null``) and sends bare
``contexts``. It used to say ``strict: true`` and a code-owner review, so
following its own header ("run it after changing a required check") would
have switched both on -- blocking every non-admin merge, with nothing saying
so. Turning either on is now a deliberate edit to ``protection_payload()``
AND to live, together; the guard below refuses any run in which they differ.

App ids: the script sends the checks with no app id, and GitHub documents that
such a check is assigned to the app that most recently posted it (only
``app_id: -1`` in ``checks`` allows any app). So the drift check does not
compare the app id of a check the body sends without one; it prints a ``note``.
It does compare a check the body DOES pin, which ``--want-json`` exercises.

The fix is a guard: before writing anything, the script runs
``scripts/check_required_checks.sh --names-only-ok`` against live protection
and refuses on any difference other than check names. These tests feed both
scripts a RECORDED protection response
(``fixtures/branch_protection/live_main_2026-10-01.json``) through a stub
``gh`` on ``PATH``. Nothing here calls GitHub: the stub answers every read
from the fixture and records every write instead of making it.

NOT a skipif anywhere: ``scripts/`` ships in every distribution, so a skip
could only fire on the tamper that matters -- deleting the guard.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.regression

REPO_ROOT = Path(__file__).resolve().parents[4]
CONFIGURE = REPO_ROOT / "scripts" / "configure-repo.sh"
CHECK = REPO_ROOT / "scripts" / "check_required_checks.sh"
FIXTURE = (
    Path(__file__).parent
    / "fixtures"
    / "branch_protection"
    / "live_main_2026-10-01.json"
)

# A stand-in for `gh`. Reads come from the fixture; a write (`-X` anything
# other than GET, or `--input`) is appended to the log as WRITE and answered
# with `{}`, so a test can see exactly what the script would have changed.
STUB_GH = r"""#!/usr/bin/env python3
import json, os, sys
args = sys.argv[1:]
log = os.environ["GH_STUB_LOG"]
method = "GET"
for i, a in enumerate(args):
    if a in ("-X", "--method") and i + 1 < len(args):
        method = args[i + 1].upper()
write = method != "GET" or "--input" in args
with open(log, "a") as fh:
    fh.write(("WRITE " if write else "READ  ") + " ".join(args) + "\n")
if write:
    sys.stdin.read() if "--input" in args else None
    print("{}")
    sys.exit(0)
path = next((a for a in args[1:] if not a.startswith("-") and "/" in a), "")
query = args[args.index("-q") + 1] if "-q" in args else None
if path.endswith("/branches/main/protection"):
    if os.environ.get("GH_STUB_PROTECTION_FAILS"):
        print("gh: Branch not protected (HTTP 404)", file=sys.stderr)
        sys.exit(1)
    sys.stdout.write(open(os.environ["GH_STUB_PROTECTION"]).read())
elif path.endswith("/codeowners/errors"):
    print("0")
elif path.endswith("/private-vulnerability-reporting"):
    print("true")
elif query == ".visibility":
    print("public")
elif query == ".security_and_analysis":
    on = {"status": "enabled"}
    print(json.dumps({"secret_scanning": on, "secret_scanning_push_protection": on,
                      "dependabot_security_updates": on}))
else:
    print("{}")
"""


def test_the_scripts_exist() -> None:
    assert CONFIGURE.is_file(), f"{CONFIGURE} is missing"
    assert CHECK.is_file(), f"{CHECK} is missing: the drift check has no implementation"
    assert FIXTURE.is_file(), f"{FIXTURE} is missing"


def _recorded() -> dict:
    return json.loads(FIXTURE.read_text())


def _payload() -> dict:
    out = subprocess.run(
        [str(CONFIGURE), "--print-protection-payload"],
        capture_output=True,
        text=True,
        check=True,
    )
    return json.loads(out.stdout)


def _matching_configure_repo() -> dict:
    """The recorded response, edited to match what configure-repo.sh writes.

    Shaped as GitHub documents it would answer a GET after a PUT with bare
    ``contexts``: each check keeps the app that last posted it (GitHub
    Actions, 15368, as recorded), and there is no review rule.
    """
    want = _payload()
    live = _recorded()
    rsc = live["required_status_checks"]
    names = want["required_status_checks"]["contexts"]
    rsc["strict"] = want["required_status_checks"]["strict"]
    rsc["contexts"] = list(names)
    rsc["checks"] = [{"context": n, "app_id": 15368} for n in names]
    live.pop("required_pull_request_reviews", None)
    return live


def _write(tmp_path: Path, protection: dict) -> Path:
    path = tmp_path / "protection.json"
    path.write_text(json.dumps(protection))
    return path


def _env(tmp_path: Path, protection: dict, **extra: str) -> tuple[dict, Path]:
    stub_dir = tmp_path / "bin"
    stub_dir.mkdir(exist_ok=True)
    gh = stub_dir / "gh"
    gh.write_text(STUB_GH)
    gh.chmod(0o755)
    log = tmp_path / "gh.log"
    log.write_text("")
    env = {
        **os.environ,
        "PATH": f"{stub_dir}{os.pathsep}{os.environ['PATH']}",
        "GH_STUB_LOG": str(log),
        "GH_STUB_PROTECTION": str(_write(tmp_path, protection)),
        **extra,
    }
    return env, log


def _run_configure(tmp_path: Path, protection: dict, *args: str, **extra: str):
    env, log = _env(tmp_path, protection, **extra)
    result = subprocess.run(
        [str(CONFIGURE), *args], capture_output=True, text=True, env=env, timeout=120
    )
    writes = [line for line in log.read_text().splitlines() if line.startswith("WRITE")]
    return result, writes


def _protection_put(writes: list[str]) -> list[str]:
    return [w for w in writes if "branches/main/protection" in w]


# --- the guard -------------------------------------------------------------


def _strict_mismatch() -> dict:
    live = _matching_configure_repo()
    live["required_status_checks"]["strict"] = True
    return live


def _review_rule(**extra: object) -> dict:
    live = _matching_configure_repo()
    live["required_pull_request_reviews"] = {
        "url": live["url"] + "/required_pull_request_reviews",
        "required_approving_review_count": 1,
        "require_code_owner_reviews": True,
        "dismiss_stale_reviews": True,
        "require_last_push_approval": False,
        **extra,
    }
    return live


def _review_mismatch() -> dict:
    return _review_rule()


def _review_setting_mismatch() -> dict:
    return _review_rule(require_last_push_approval=True)


def _flag_mismatch() -> dict:
    live = _matching_configure_repo()
    live["required_linear_history"] = {"enabled": False}
    return live


REFUSED = {
    "strict": _strict_mismatch,
    "review-rule-live": _review_mismatch,
    "review-setting": _review_setting_mismatch,
    "flag": _flag_mismatch,
}


@pytest.mark.parametrize("case", sorted(REFUSED))
def test_guard_refuses_and_writes_nothing(tmp_path: Path, case: str) -> None:
    result, writes = _run_configure(tmp_path, REFUSED[case]())
    assert result.returncode != 0, result.stdout + result.stderr
    assert "REFUSING" in result.stderr, result.stdout + result.stderr
    # Not just the protection PUT: no write of any kind may precede the guard.
    assert writes == [], f"the refused run still wrote: {writes}"


@pytest.mark.parametrize("case", ["strict", "review-setting", "review-rule-live"])
def test_guard_refuses_in_dry_run_too(tmp_path: Path, case: str) -> None:
    result, writes = _run_configure(tmp_path, REFUSED[case](), "--dry-run")
    assert result.returncode != 0
    assert "REFUSING" in result.stderr
    assert "would: branch protection" not in result.stdout
    assert writes == []


def test_guard_refuses_when_live_protection_cannot_be_read(tmp_path: Path) -> None:
    result, writes = _run_configure(
        tmp_path, _matching_configure_repo(), GH_STUB_PROTECTION_FAILS="1"
    )
    assert result.returncode != 0
    assert "REFUSING" in result.stderr
    assert "cannot read live protection" in result.stderr
    assert writes == []


def test_guard_allows_a_names_only_difference(tmp_path: Path) -> None:
    live = _matching_configure_repo()
    rsc = live["required_status_checks"]
    # One required check renamed live, one more the script does not know.
    rsc["checks"][0]["context"] = "Renamed Check"
    rsc["checks"].append({"context": "Extra Live Check", "app_id": 15368})
    rsc["contexts"] = [c["context"] for c in rsc["checks"]]

    result, writes = _run_configure(tmp_path, live)
    assert "REFUSING" not in result.stderr, result.stdout + result.stderr
    assert "names only" in result.stdout
    assert len(_protection_put(writes)) == 1, writes


def test_guard_allows_an_exact_match_and_the_read_back_passes(tmp_path: Path) -> None:
    result, writes = _run_configure(tmp_path, _matching_configure_repo())
    assert result.returncode == 0, result.stdout + result.stderr
    assert len(_protection_put(writes)) == 1
    assert "ok    protection matches this script" in result.stdout
    # The success line no longer claims a code-owner review.
    assert "code-owner review" not in result.stdout


def test_guard_notes_but_does_not_refuse_a_pin_the_script_does_not_send(
    tmp_path: Path,
) -> None:
    """configure-repo.sh sends no app id, so a live pin is GitHub's to resolve, not drift."""
    live = _matching_configure_repo()
    live["required_status_checks"]["checks"][0]["app_id"] = 99999
    result, writes = _run_configure(tmp_path, live)
    assert "REFUSING" not in result.stderr, result.stdout + result.stderr
    assert "note   app id   20 check(s) sent with no app id" in result.stdout
    assert "app 99999" in result.stdout
    assert len(_protection_put(writes)) == 1


def test_print_protection_payload_calls_nothing(tmp_path: Path) -> None:
    env, log = _env(tmp_path, _recorded())
    out = subprocess.run(
        [str(CONFIGURE), "--print-protection-payload"],
        capture_output=True,
        text=True,
        env=env,
        check=True,
    )
    assert json.loads(out.stdout)["required_status_checks"]["contexts"]
    assert log.read_text() == ""


# --- A11: check_required_checks.sh ------------------------------------------


def _check(tmp_path: Path, protection: dict, *args: str):
    return subprocess.run(
        [str(CHECK), "--live-json", str(_write(tmp_path, protection)), *args],
        capture_output=True,
        text=True,
        timeout=60,
    )


def _check_against(tmp_path: Path, want: dict, protection: dict, *args: str):
    want_path = tmp_path / "want.json"
    want_path.write_text(json.dumps(want))
    return subprocess.run(
        [
            str(CHECK),
            "--live-json",
            str(_write(tmp_path, protection)),
            "--want-json",
            str(want_path),
            *args,
        ],
        capture_output=True,
        text=True,
        timeout=60,
    )


def _drift_lines(stdout: str) -> list[str]:
    return [line for line in stdout.splitlines() if line.startswith("drift  ")]


def test_check_reports_no_drift_on_an_exact_match(tmp_path: Path) -> None:
    result = _check(tmp_path, _matching_configure_repo())
    assert result.returncode == 0, result.stdout + result.stderr
    assert _drift_lines(result.stdout) == []
    assert "no drift" in result.stdout
    # The unsent app ids are a note, never drift.
    assert "note   app id   20 check(s) sent with no app id" in result.stdout
    assert "Live today: app 15368." in result.stdout


@pytest.mark.parametrize(
    ("case", "kind", "needle"),
    [
        ("strict", "strict", "live true, configure-repo.sh writes false"),
        ("review-rule-live", "reviews", "configure-repo.sh writes no review rule"),
        ("review-setting", "reviews", '"require_last_push_approval": true'),
        ("flag", "flags", "required_linear_history: live false"),
    ],
)
def test_check_reports_each_kind_of_drift(
    tmp_path: Path, case: str, kind: str, needle: str
) -> None:
    for extra in ((), ("--names-only-ok",)):
        result = _check(tmp_path, REFUSED[case](), *extra)
        assert result.returncode == 1, result.stdout + result.stderr
        lines = _drift_lines(result.stdout)
        assert len(lines) == 1, lines
        assert lines[0].startswith(f"drift  {kind}"), lines
        assert needle in lines[0], lines


def _pinned_want(app_id: int) -> dict:
    """configure-repo.sh's body, but sending every check in `checks` with an app id."""
    want = _payload()
    rsc = want["required_status_checks"]
    rsc["checks"] = [{"context": n, "app_id": app_id} for n in rsc.pop("contexts")]
    return want


@pytest.mark.parametrize(
    ("sent", "live_pin", "needle"),
    [
        (15368, 99999, "live app 99999, configure-repo.sh writes app 15368"),
        (-1, 15368, "live app 15368, configure-repo.sh writes any app"),
        (15368, None, "live any app, configure-repo.sh writes app 15368"),
    ],
)
def test_check_reports_app_id_drift_where_the_body_sends_one(
    tmp_path: Path, sent: int, live_pin: int | None, needle: str
) -> None:
    live = _matching_configure_repo()
    live["required_status_checks"]["checks"][0]["app_id"] = live_pin
    for c in live["required_status_checks"]["checks"][1:]:
        c["app_id"] = None if sent == -1 else sent
    for extra in ((), ("--names-only-ok",)):
        result = _check_against(tmp_path, _pinned_want(sent), live, *extra)
        assert result.returncode == 1, result.stdout + result.stderr
        lines = _drift_lines(result.stdout)
        assert len(lines) == 1, lines
        assert lines[0].startswith("drift  app id"), lines
        assert needle in lines[0], lines
        assert "note   app id" not in result.stdout


def test_check_any_app_matches_a_live_null_pin(tmp_path: Path) -> None:
    live = _matching_configure_repo()
    for c in live["required_status_checks"]["checks"]:
        c["app_id"] = None
    result = _check_against(tmp_path, _pinned_want(-1), live)
    assert result.returncode == 0, result.stdout + result.stderr
    assert _drift_lines(result.stdout) == []


def test_check_reports_name_drift_and_names_only_ok_accepts_it(tmp_path: Path) -> None:
    live = _matching_configure_repo()
    rsc = live["required_status_checks"]
    dropped = rsc["checks"].pop()["context"]
    rsc["checks"].append({"context": "Extra Live Check", "app_id": 15368})
    rsc["contexts"] = [c["context"] for c in rsc["checks"]]

    result = _check(tmp_path, live)
    assert result.returncode == 1
    lines = _drift_lines(result.stdout)
    assert (
        f'drift  names    in configure-repo.sh, not required live: "{dropped}"' in lines
    )
    assert (
        'drift  names    required live, not in configure-repo.sh: "Extra Live Check"'
        in lines
    )
    assert len(lines) == 2, lines

    assert _check(tmp_path, live, "--names-only-ok").returncode == 0


def test_check_on_the_recorded_live_response(tmp_path: Path) -> None:
    """The recorded live response: no drift in strict, reviews or flags; the pins are a note."""
    recorded = _recorded()
    assert recorded["required_status_checks"]["strict"] is False
    assert "required_pull_request_reviews" not in recorded
    result = _check(tmp_path, recorded, "--names-only-ok")
    assert result.returncode == 0, result.stdout + result.stderr
    assert _drift_lines(result.stdout) == []
    pinned = [
        c["context"]
        for c in recorded["required_status_checks"]["checks"]
        if c["app_id"] == 15368
    ]
    assert len(pinned) == 20
    assert "note   app id   20 check(s) sent with no app id" in result.stdout
    assert "Live today: app 15368." in result.stdout


def test_the_payload_is_what_main_has() -> None:
    """strict off and no review rule; the key is present (the PUT requires it) and null."""
    payload = _payload()
    assert payload["required_status_checks"]["strict"] is False
    assert "required_pull_request_reviews" in payload
    assert payload["required_pull_request_reviews"] is None


def test_check_refuses_input_that_is_not_a_protection_response(tmp_path: Path) -> None:
    result = _check(tmp_path, {"message": "Not Found"})
    assert result.returncode == 2
