# SPDX-FileCopyrightText: 2026 The Controlvar authors
# SPDX-License-Identifier: Apache-2.0
"""Tamper evidence for `scripts/leak_guard.py`.

Every rule is planted against and watched to fire. A gate that has only ever
been seen to pass is not a gate, and this repository has shipped several that
passed for the wrong reason -- a `find` matching a directory instead of the
page it guarded, a CI input that selected nothing, a grep that arrived
double-escaped and matched nothing for ever.

The two tests that matter most are the last two:

  * a clean TREE whose commit MESSAGE leaks must still be rejected. That is
    the exact shape of the incident this guard replaces -- the old export
    sweep read files only, reported PASS, and published sixteen commit
    messages containing an absolute home path.

  * an offender planted IN the guard's own source must be caught BY the guard.
    The predecessor -- the export sweep, since removed -- excluded its own
    directory from its own grep, and therefore published the AWS account id
    printed on line 276 of the very script that forbade it.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[4]
GUARD = REPO_ROOT / "scripts" / "leak_guard.py"


def _repo(tmp_path: Path) -> Path:
    """A real git repository with the guard in it, and nothing else."""
    subprocess.run(["git", "init", "-q", "-b", "main", "."], cwd=tmp_path, check=True)
    for k, v in (("user.email", "t@example.invalid"), ("user.name", "t")):
        subprocess.run(["git", "config", k, v], cwd=tmp_path, check=True)
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts" / "leak_guard.py").write_text(
        GUARD.read_text(encoding="utf-8"), encoding="utf-8"
    )
    subprocess.run(["git", "add", "-A"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=tmp_path, check=True)
    return tmp_path


def _run(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "scripts/leak_guard.py", *args],
        cwd=repo,
        capture_output=True,
        text=True,
    )


def _plant(repo: Path, name: str, content: str) -> subprocess.CompletedProcess:
    (repo / name).write_text(content, encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    return _run(repo, "--staged")


# The offenders are ASSEMBLED, never written out, so this file does not itself
# contain the things it plants. That is not squeamishness: `git ls-files` feeds
# the whole-tree scan, so a fixture spelled literally here would fail the guard
# on every run, and the only way to keep it would be to exempt this file --
# which is precisely the defect the guard exists to prevent. Composing the
# strings costs one f-string and needs no exemption at all.
#
# The first version of this file DID spell them out, was untracked when the
# tree scan ran, and passed. It failed the moment it was committed. That is the
# privileged-environment mistake in miniature.
_USERS, _HOME = "Users", "home"
MAC_HOME = f"/{_USERS}/jsmith"
LINUX_HOME = f"/{_HOME}/jsmith"

# Twelve digits, deliberately not one of the values PLACEHOLDER_ACCOUNTS allows.
FAKE_ACCOUNT = "987654321098"

MUST_FIRE = [
    ("arn", "a.py", f'ROLE = "arn:aws:iam::{FAKE_ACCOUNT}:role/Deploy"'),
    ("ecr", "b.py", f'IMG = "{FAKE_ACCOUNT}.dkr.ecr.us-west-2.amazonaws.com/x"'),
    ("account_id key", "c.yml", f'account_id: "{FAKE_ACCOUNT}"'),
    ("cdk bootstrap", "d.md", f"cdk bootstrap aws://{FAKE_ACCOUNT}/us-west-2"),
    ("macOS home", "e.sh", f"cd {MAC_HOME}/code && make"),
    ("linux home", "f.sh", f"cd {LINUX_HOME}/code && make"),
    ("soc2", "g.md", "We are SOC 2 Type II certified."),
    ("uptime", "h.md", "Delivering 99.97% uptime."),
    ("edition", "i.md", "Available in Enterprise Edition."),
    ("licence key", "j.md", "Paste your license key here."),
    ("claim in tsx", "k.tsx", "export const C = () => <p>GDPR Compliant</p>;"),
    # The claim rules the cases above do not reach.
    ("scale figure, millions", "q.md", "Trusted with 58M+ events a day."),
    ("scale figure, decimal millions", "r.md", "Over 2.8M+ users served."),
    ("scale figure, billions", "s.md", "1B+ assignments served."),
    ("edition, community", "t.md", "Available in Community Edition."),
    (
        "fabricated production environment",
        "u.json",
        '{"environment": "Production"}',
    ),
]

MUST_NOT_FIRE = [
    (
        "aws's own documented placeholder",
        "l.py",
        'ROLE = "arn:aws:iam::123456789012:role/Example"',
    ),
    ("the CI runner's home", "m.sh", f"cd /{_HOME}/runner/work/repo && make"),
    ("a generic /home/dev fixture", "n.py", f'p = "/{_HOME}/dev/migrations"'),
    ("twelve digits in no AWS context", "o.py", "TIMESTAMP_US = 175874102399"),
    (
        "prose describing the guard",
        "p.md",
        "The guard rejects any AWS account id outside an example.",
    ),
]


@pytest.mark.unit
@pytest.mark.parametrize("label,name,content", MUST_FIRE, ids=[c[0] for c in MUST_FIRE])
def test_rule_fires_on_a_planted_offender(tmp_path, label, name, content):
    result = _plant(_repo(tmp_path), name, content)
    assert result.returncode == 1, (
        f"leak-guard accepted {label}: {content!r}\n{result.stdout}{result.stderr}"
    )
    assert name in result.stderr


@pytest.mark.unit
@pytest.mark.parametrize(
    "label,name,content", MUST_NOT_FIRE, ids=[c[0] for c in MUST_NOT_FIRE]
)
def test_allowance_does_not_fire(tmp_path, label, name, content):
    result = _plant(_repo(tmp_path), name, content)
    assert result.returncode == 0, (
        f"leak-guard wrongly rejected {label}: {content!r}\n"
        f"{result.stdout}{result.stderr}"
    )


@pytest.mark.unit
@pytest.mark.regression
@pytest.mark.parametrize("name", ["v.md", "w.tsx"], ids=["markdown", "tsx"])
def test_the_sample_ratio_check_is_not_a_false_claim(tmp_path, name):
    """Sample-ratio-mismatch checking exists, so the phrase is accurate text.

    `backend/app/services/srm_service.py` computes it and the results endpoint
    returns it. A claim rule for the phrase "SRM detection" called it "a
    feature that is not implemented", and so rejected true sentences about it.
    """
    phrase = "SRM detection warns when the observed split drifts from the plan."
    result = _plant(_repo(tmp_path), name, phrase)
    assert result.returncode == 0, (
        "the guard rejected an accurate sentence about the sample-ratio check\n"
        f"{result.stdout}{result.stderr}"
    )


@pytest.mark.unit
@pytest.mark.regression
def test_dropping_the_sample_ratio_rule_kept_the_other_claim_rules(tmp_path):
    """A file with the phrase and a real false claim is still rejected, for the claim."""
    result = _plant(
        _repo(tmp_path),
        "x.md",
        "SRM detection is on.\nDelivering 99.97% uptime.\n",
    )
    assert result.returncode == 1, (
        "the guard accepted a false claim next to the phrase\n"
        f"{result.stdout}{result.stderr}"
    )
    assert "99.97" in result.stderr
    assert "SRM" not in result.stderr, result.stderr


@pytest.mark.unit
def test_a_clean_tree_with_a_leaking_commit_message_is_rejected(tmp_path):
    """The blind spot that caused the incident: files clean, messages not."""
    repo = _repo(tmp_path)
    base = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    (repo / "harmless.txt").write_text("nothing wrong here\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(
        [
            "git",
            "commit",
            "-qm",
            "chore: a change whose message leaks\n\n"
            f"Reproduced from {MAC_HOME}/code/thing.",
        ],
        cwd=repo,
        check=True,
    )

    # The tree really is clean -- prove that, so the next assertion can only
    # be explained by the message sweep.
    assert _run(repo).returncode == 0

    result = _run(repo, "--range", f"{base}..HEAD")
    assert result.returncode == 1, (
        "leak-guard read the files and ignored the commit message, which is "
        "precisely how an absolute home path reached the published history "
        f"once already\n{result.stdout}{result.stderr}"
    )
    assert "message" in result.stderr


@pytest.mark.unit
def test_the_guard_does_not_exempt_its_own_source(tmp_path):
    """The predecessor excluded its own directory and shipped what it forbade."""
    repo = _repo(tmp_path)
    assert _run(repo).returncode == 0, "the guard should be clean against itself"

    guard = repo / "scripts" / "leak_guard.py"
    guard.write_text(
        guard.read_text(encoding="utf-8")
        + f"\n# planted: arn:aws:iam::{FAKE_ACCOUNT}:role/x\n",
        encoding="utf-8",
    )
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)

    result = _run(repo, "--staged")
    assert result.returncode == 1, (
        "leak-guard exempts its own source, which is the defect it exists for"
        f"\n{result.stdout}{result.stderr}"
    )
    assert "leak_guard.py" in result.stderr


@pytest.mark.unit
def test_push_mode_survives_a_branch_with_no_upstream(tmp_path):
    """The pre-push hook's own failure mode, which is every branch's first push.

    `HEAD@{push}` exits non-zero until a branch has an upstream, and the guard
    used to exit 2 on that -- a hook that dies on every new branch is a hook
    everybody learns to skip with --no-verify. --push resolves the range
    itself, and must still read the messages it resolves to.
    """
    repo = _repo(tmp_path)
    subprocess.run(["git", "checkout", "-qb", "feature"], cwd=repo, check=True)
    (repo / "x.txt").write_text("ok\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "chore: clean work"], cwd=repo, check=True)

    assert not subprocess.run(
        ["git", "rev-parse", "--verify", "--quiet", "HEAD@{push}"],
        cwd=repo,
        capture_output=True,
    ).stdout.strip(), "this test is vacuous unless the branch really has no upstream"

    clean = _run(repo, "--push")
    assert clean.returncode == 0, (
        f"--push failed on a branch with no upstream\n{clean.stdout}{clean.stderr}"
    )

    (repo / "y.txt").write_text("ok\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(
        ["git", "commit", "-qm", f"chore: leaks\n\nRan it from {MAC_HOME}/x."],
        cwd=repo,
        check=True,
    )

    leaking = _run(repo, "--push")
    assert leaking.returncode == 1, (
        "--push resolved a range but did not read the commit messages in it"
        f"\n{leaking.stdout}{leaking.stderr}"
    )
    assert "message" in leaking.stderr


@pytest.mark.unit
@pytest.mark.parametrize(
    "label,text,expected",
    [
        ("a home path in the body", f"chore: t\n\nRan from {MAC_HOME}/x.", 1),
        ("a false claim in the body", "feat: x\n\nWe are SOC 2 Type II certified.", 1),
        (
            "an account id in the body",
            f"fix: y\n\narn:aws:iam::{FAKE_ACCOUNT}:role/z",
            1,
        ),
        ("a clean body", "chore: t\n\nNothing wrong here.", 0),
    ],
    ids=["home", "claim", "account", "clean"],
)
def test_stdin_mode_scans_pull_request_text(tmp_path, label, text, expected):
    """A pull request's title and body are a publication surface.

    GitHub composes the squash commit message from them, so a forbidden string
    reaches `main` even when every commit on the branch is clean. That is how
    an absolute home path reached the merge commit of the change that ADDED
    this guard: the branch commits were amended after the guard rejected them,
    the pull request body was not, and nothing was looking at it.
    """
    repo = _repo(tmp_path)
    result = subprocess.run(
        [sys.executable, "scripts/leak_guard.py", "--stdin"],
        cwd=repo,
        input=text,
        capture_output=True,
        text=True,
    )
    assert result.returncode == expected, (
        f"--stdin got it wrong for {label}\n{result.stdout}{result.stderr}"
    )


@pytest.mark.unit
def test_the_repository_itself_is_clean():
    """The real tree, scanned by the real guard. No fixtures involved.

    Skipped in `scripts/core_build.sh`'s copy, which is a plain directory with
    no `.git` -- deliberately, so that a profile is proved to stand alone
    outside a checkout. The guard enumerates with `git ls-files`, so there is
    nothing there for it to enumerate:

        FAILED ...::test_the_repository_itself_is_clean
          leak-guard: git rev-parse --show-toplevel failed:
          fatal: not a git repository

    The skip is keyed on the ABSENCE OF `.git`, which is a structural fact
    about that copy, and NOT on the guard returning non-zero. A guard that
    breaks for any other reason still fails this test. That distinction is the
    whole point: this repository has shipped checks that skipped their way to
    green, and a skip whose condition is "the thing under test failed" is one
    of them.
    """
    if not (REPO_ROOT / ".git").exists():
        pytest.skip(
            "not a git checkout (core_build.sh's copy); git ls-files has "
            "nothing to enumerate here"
        )
    result = subprocess.run(
        [sys.executable, str(GUARD)], cwd=REPO_ROOT, capture_output=True, text=True
    )
    assert result.returncode == 0, result.stderr
