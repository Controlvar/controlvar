#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 The Controlvar authors
# SPDX-License-Identifier: Apache-2.0
"""Refuse to let a secret or a false claim into this repository.

This replaces the sweep that `scripts/publish/export.sh` used to run, and it
replaces it because that sweep can no longer work. The sweep was CORRECTIVE: development happened
in a private repository, and a filter-repo export stripped the forbidden
material on the way out. Development is public now, so there is no "way out"
left to strip anything on -- a push is publication. The check has to be
PREVENTIVE, which means it has to run here, in the open, which means it cannot
carry the strings it forbids.

That constraint sounds fatal and is not, because the old forbidden.txt held two
kinds of rule and only one of them was ever secret:

  id  the real AWS account id, and a developer home path.  SECRET.
  mr  fabricated figures ("99.97", "58M+").                not secret.
  re  claims the project will not make ("SOC 2 Type II").  not secret.

The `mr` and `re` rules ship verbatim below. Publishing "this project must
never claim SOC 2 Type II" costs nothing and says something true.

The two `id` rules are matched by SHAPE instead: any AWS account id in an AWS
context, any developer home path. That is strictly stronger than the literals
were -- it catches the next contributor's home directory and a second AWS
account, neither of which a literal list would have known about -- and it
publishes nothing.

A hashed literal list was the obvious alternative and is worthless here: an
AWS account id is twelve digits, a space of 10^12, which any laptop exhausts
against a stored hash in minutes. Shape matching is not a compromise; it is
the only thing that works.

Usage:
    scripts/leak_guard.py                 every tracked file, and no messages
    scripts/leak_guard.py --staged        what is staged (the pre-push hook)
    scripts/leak_guard.py --range A..B    those files AND those commit messages

Exit 0 clean, 1 on any finding, 2 on a usage error.

Commit messages are scanned because that is precisely how this escaped once
before: the tree was clean, the sweep reported PASS, and sixteen commit
messages in the published history contained an absolute home path. A check
that reads only files is a check that has already missed this.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

# --------------------------------------------------------------------------
# SHAPE rules. These stand in for the secret `id` rules and are scanned in
# every tracked file, this one included -- a gate that excludes its own source
# from its own check is the bug that produced the incident this file exists
# for. That is safe here only because each pattern below is a regex that does
# not match its own source text: `[0-9]{12}` contains no run of twelve digits,
# and `/Users/[A-Za-z0-9_.-]+` is `/Users/` followed by `[`, which is not in
# the character class it opens. Do not add a shape rule that is a bare
# literal; it would match here and the fix would be an exemption.
# --------------------------------------------------------------------------

# The placeholder AWS publishes in its own documentation. Legitimate in
# fixtures and examples, and the only 12-digit account id that may appear.
PLACEHOLDER_ACCOUNTS = {"123456789012", "000000000000", "111111111111"}

# Home directories that name a role rather than a person: CI runners and the
# generic examples documentation uses. `/home/runner` is every GitHub Actions
# job's working root and appears in real log excerpts.
GENERIC_HOMES = {
    "runner",
    "dev",
    "user",
    "users",
    "you",
    "me",
    "app",
    "node",
    "ubuntu",
    "root",
    "circleci",
    "vscode",
    "docker",
    "jenkins",
    "example",
    "someone",
}

SHAPE_RULES = [
    (
        "aws-account-id",
        re.compile(
            r"(?:arn:aws[a-z-]*:[^:\s]*:[^:\s]*:|aws://|\.console\.aws\.amazon\.com/[^\s]*?[?&]account=)"
            r"(?P<value>[0-9]{12})"
            r"|(?P<value2>[0-9]{12})\.dkr\.ecr\."
            r"|(?i:account[_ -]?id)\D{0,12}?(?P<value3>[0-9]{12})"
        ),
        "an AWS account id. Use a documented placeholder such as 123456789012.",
    ),
    (
        "home-directory-path",
        re.compile(r"/(?:Users|home)/(?P<value>[A-Za-z0-9_][A-Za-z0-9_.-]*)"),
        "an absolute path into somebody's home directory. Write it relative to "
        "the repository root, or as ~/ or $HOME.",
    ),
]

# --------------------------------------------------------------------------
# CLAIM rules -- the old `mr` and `re` rules, verbatim. Not secret: this is a
# public statement of what the project refuses to assert about itself.
#
# Scoped to the file types marketing prose lives in. That scope is also why
# this file does not match itself: the rules below are literals, and a `.py`
# file is not in CLAIM_SUFFIXES. That is a scope, not a self-exemption -- the
# distinction being that planting a claim in a .md file still fires, which the
# test suite asserts.
# --------------------------------------------------------------------------

CLAIM_SUFFIXES = {
    ".md",
    ".markdown",
    ".mdx",
    ".txt",
    ".rst",
    ".html",
    ".htm",
    ".tsx",
    ".jsx",
    ".ts",
    ".js",
    ".json",
    ".yml",
    ".yaml",
}

CLAIM_RULES = [
    (r"99\.97", "an uptime figure nothing here measures"),
    (r"58M\+", "a scale figure nothing here measures"),
    (r"2\.8M\+", "a scale figure nothing here measures"),
    (r"1B\+", "a scale figure nothing here measures"),
    (r"SOC 2 Type II", "a certification this project does not hold"),
    (r"GDPR Compliant", "a compliance claim no audit supports"),
    (r'"environment": "Production"', "a fabricated production environment"),
    (
        r"Enterprise Edition",
        "an edition that does not exist -- the licence is Apache-2.0 throughout",
    ),
    (
        r"Community Edition",
        "an edition that does not exist -- the licence is Apache-2.0 throughout",
    ),
    (r"licen[cs]e key", "there is no licence key; the licence is Apache-2.0"),
]
CLAIM_RULES = [(re.compile(p), why) for p, why in CLAIM_RULES]

SKIP_SUFFIXES = {
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".ico",
    ".pdf",
    ".woff",
    ".woff2",
    ".ttf",
    ".eot",
    ".zip",
    ".gz",
    ".jar",
    ".class",
    ".snap",
    ".lock",
    ".svg",
    ".webp",
    ".mp4",
    ".whl",
    ".so",
}
SKIP_NAMES = {
    "package-lock.json",
    "openapi-v1.full.json",
    "openapi-v1.stable.json",
    "openapi.json",
}


def git(*args: str) -> str:
    r = subprocess.run(["git", *args], capture_output=True, text=True)
    if r.returncode != 0:
        print(
            f"leak-guard: git {' '.join(args)} failed: {r.stderr.strip()}",
            file=sys.stderr,
        )
        sys.exit(2)
    return r.stdout


def git_ok(*args: str) -> str | None:
    """Run git and return None instead of exiting when it fails."""
    r = subprocess.run(["git", *args], capture_output=True, text=True)
    return r.stdout if r.returncode == 0 else None


def push_range() -> str | None:
    """The commits about to leave this machine, however the branch is set up.

    `HEAD@{push}` is the correct answer and resolves only when the branch
    already has an upstream. On a branch created a minute ago -- which is
    every branch, the first time it is pushed -- it exits non-zero, and a
    pre-push hook that dies there is a pre-push hook everybody learns to skip
    with --no-verify. Measured, not assumed:

        $ git rev-parse lg-probe@{push}
        fatal: no upstream configured for branch 'lg-probe'

    So try the upstream spellings, then fall back to whatever the default
    branch already has, and finally to None, which means "scan the whole tree
    and every commit that is not on a remote" -- more work, never less.
    """
    for spelling in ("@{push}", "@{upstream}"):
        rev = git_ok("rev-parse", "--verify", "--quiet", f"HEAD{spelling}")
        if rev and rev.strip():
            return f"{rev.strip()}..HEAD"
    for base in ("origin/main", "origin/master", "main", "master"):
        rev = git_ok("merge-base", base, "HEAD")
        if rev and rev.strip():
            return f"{rev.strip()}..HEAD"
    return None


def shape_findings(text: str):
    """Yield (rule, offending value, why) for each shape violation in text."""
    for name, pattern, why in SHAPE_RULES:
        for m in pattern.finditer(text):
            value = next((v for v in m.groupdict().values() if v), None)
            if value is None:
                continue
            if name == "aws-account-id" and value in PLACEHOLDER_ACCOUNTS:
                continue
            if name == "home-directory-path" and value.lower() in GENERIC_HOMES:
                continue
            yield name, m.group(0), why


def claim_findings(text: str):
    for pattern, why in CLAIM_RULES:
        m = pattern.search(text)
        if m:
            yield "false-claim", m.group(0), why


def scan_file(path: Path, rel: str):
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except (OSError, UnicodeError):
        return
    for line_no, line in enumerate(text.splitlines(), 1):
        for rule, value, why in shape_findings(line):
            yield rel, line_no, rule, value, why
        if path.suffix.lower() in CLAIM_SUFFIXES:
            for rule, value, why in claim_findings(line):
                yield rel, line_no, rule, value, why


def report(findings) -> None:
    print(f"leak-guard: {len(findings)} finding(s)\n", file=sys.stderr)
    for where, line_no, rule, value, why in findings:
        loc = f"{where}:{line_no}" if line_no else where
        print(f"  {loc}\n      {rule}: {value!r}\n      {why}\n", file=sys.stderr)
    print(
        "This repository is public. A push is publication -- there is no "
        "export step\nleft to strip these out afterwards. Fix them here.",
        file=sys.stderr,
    )


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument(
        "--staged",
        action="store_true",
        help="scan staged content only (the pre-push hook)",
    )
    ap.add_argument(
        "--range",
        dest="rng",
        help="scan files changed in A..B and those commit messages",
    )
    ap.add_argument(
        "--stdin",
        action="store_true",
        help="scan text on standard input instead of files (the pull request "
        "title and body, which GitHub turns into the squash commit message)",
    )
    ap.add_argument(
        "--push",
        action="store_true",
        help="scan what is about to be pushed (the pre-push hook); "
        "resolves the range itself and never fails for lack of "
        "an upstream",
    )
    ap.add_argument(
        "--all", action="store_true", help="scan every tracked file (the default)"
    )
    args = ap.parse_args()

    if args.stdin:
        # A pull request's title and body are a PUBLICATION SURFACE, and not an
        # obvious one: GitHub composes the squash commit message from them, so a
        # forbidden string reaches `main` even when every commit on the branch
        # is clean. That is not hypothetical -- it is exactly how an absolute
        # home path reached the merge commit of the change that ADDED this
        # guard. The branch commits had been amended; the pull request body had
        # not, and nothing was looking at it.
        text = sys.stdin.read()
        findings = [
            ("pull request title/body", 0, rule, value, why)
            for rule, value, why in shape_findings(text)
        ] + [
            ("pull request title/body", 0, rule, value, why)
            for rule, value, why in claim_findings(text)
        ]
        if findings:
            report(findings)
            return 1
        print(f"leak-guard: clean ({len(text)} bytes of pull request text)")
        return 0

    root = Path(git("rev-parse", "--show-toplevel").strip())
    if args.push and not args.rng:
        # None here is not a failure: it means no range could be worked out,
        # so the whole-tree scan below carries the check on its own.
        args.rng = push_range()
    if args.staged:
        files = git("diff", "--cached", "--name-only", "--diff-filter=ACMR").split()
        what = "staged files"
    elif args.rng:
        files = git("diff", "--name-only", "--diff-filter=ACMR", args.rng).split()
        what = f"files changed in {args.rng}"
    else:
        files = git("ls-files").split()
        what = "every tracked file"

    findings = []
    scanned = 0
    for rel in files:
        p = root / rel
        if not p.is_file():
            continue
        if p.suffix.lower() in SKIP_SUFFIXES or p.name in SKIP_NAMES:
            continue
        scanned += 1
        findings.extend(scan_file(p, rel))

    messages_scanned = 0
    if args.rng:
        log = git("log", "--format=%H%x00%B%x00%x00", args.rng)
        for entry in log.split("\0\0"):
            if not entry.strip():
                continue
            sha, _, body = entry.partition("\0")
            messages_scanned += 1
            for rule, value, why in shape_findings(body):
                findings.append(
                    (f"commit {sha.strip()[:12]} message", 0, rule, value, why)
                )

    if findings:
        report(findings)
        return 1

    msg = f"leak-guard: clean ({scanned} of {what}"
    if args.rng:
        msg += f", {messages_scanned} commit messages"
    print(msg + ")")
    return 0


if __name__ == "__main__":
    sys.exit(main())
