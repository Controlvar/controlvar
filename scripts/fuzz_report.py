# SPDX-FileCopyrightText: 2026 The Controlvar authors
# SPDX-License-Identifier: Apache-2.0
"""What a run of the three API fuzzing passes adds up to: one rolling issue.

Run by ``fuzz.yml``'s ``report`` job after the three passes. Each pass hands
over its job result and, when ``scripts/fuzz_check.py`` decided it, the
template and values of its step summary (``fuzz-green.tmpl`` or
``fuzz-red.tmpl``: the pass, date, commit, seed and counts, nothing else). This
script reads the open ``fuzz-failure`` issue with ``gh``, decides what to post,
renders it with ``qa_render`` and writes it under ``--out``: one directory per
post under ``posts/`` holding ``action`` (``open`` or ``comment``), ``issue``
(the number to comment on, or ``new`` for the issue this run opens),
``title.txt`` (for ``open``) and ``body.md``. ``gh`` is only read here;
posting is the workflow's, with ``--body-file``. The rules
(``.github/qa-templates/README.md``):

* a red pass, when no ``fuzz-failure`` issue is open, opens one from
  ``fuzz-issue.tmpl`` (its body is the pass's ``fuzz-red.tmpl`` summary); the
  run's other red passes comment their own summary on it;
* while it is open, every red pass comments its ``fuzz-red.tmpl`` summary;
* nothing closes it: it is closed by hand once the cause is fixed, or listed
  in ``tests/fuzz/known-5xx.toml`` or ``tests/fuzz/unreached.toml`` after
  triage, because a red pass means something no list explains yet;
* a pass with no verdict (its job failed or was cancelled before the
  evaluation) and a green pass post nothing.

Only a scheduled run on ``main`` posts. Any other run (a dispatch, a run on
another branch) decides the same way and prints what it would post: a dry
run. Printed text is pass names, issue numbers and fixed sentences only.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Callable, List, Mapping, NamedTuple, Optional, Sequence

sys.path.insert(0, str(Path(__file__).resolve().parent))

import qa_render  # a sibling script, not a package

BRANCH = "main"
LABEL = "fuzz-failure"
ISSUE = "fuzz-issue.tmpl"
RED = "fuzz-red.tmpl"
GREEN = "fuzz-green.tmpl"
PASSES = ("superuser", "viewer", "sdk")
BOT_LOGINS = frozenset({"github-actions", "github-actions[bot]", "app/github-actions"})
RESULTS = ("success", "failure", "cancelled", "skipped")

Gh = Callable[[List[str]], str]


class GhError(RuntimeError):
    """A ``gh`` call failed; the message names the command, never its output."""


class PassError(ValueError):
    """A pass's hand-over is not what the fuzz arm writes."""


def run_gh(args: List[str]) -> str:
    """``gh <args>``, its stdout."""
    try:
        done = subprocess.run(
            ["gh", *args], capture_output=True, text=True, timeout=120, check=False
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise GhError(f"gh {' '.join(args[:2])}: {type(error).__name__}") from None
    if done.returncode != 0:
        raise GhError(f"gh {' '.join(args[:2])} failed (exit {done.returncode})")
    return done.stdout


class Pass(NamedTuple):
    name: str
    result: str
    template: Optional[str]  # None: no verdict
    values: Mapping[str, str]


def read_pass(name: str, result: str, values: str) -> Pass:
    """One pass's job result and the summary values its arm wrote (or '')."""
    if result not in RESULTS:
        raise PassError(f"{name}: not a job result")
    if not values.strip():
        return Pass(name, result, None, {})
    try:
        data = json.loads(values)
        template, given = data["template"], data["values"]
    except (ValueError, KeyError, TypeError):
        raise PassError(f"{name}: the summary values are not the arm's") from None
    if template not in (GREEN, RED) or not isinstance(given, dict):
        raise PassError(f"{name}: the summary values are not the arm's")
    if (
        not all(isinstance(v, str) for v in given.values())
        or given.get("check") != name
    ):
        raise PassError(f"{name}: the summary values are not the arm's")
    return Pass(name, result, template, given)


def read_passes(env: Mapping[str, str]) -> List[Pass]:
    return [
        read_pass(
            name,
            env.get(f"FUZZ_{name.upper()}_RESULT", ""),
            env.get(f"FUZZ_{name.upper()}_VALUES", ""),
        )
        for name in PASSES
    ]


def open_issue(gh: Gh, repo: str) -> Optional[int]:
    """The oldest open issue this workflow filed, or None."""
    title = qa_render.render_title(ISSUE, {})
    issues = json.loads(
        gh(
            [
                "issue",
                "list",
                "--repo",
                repo,
                "--state",
                "open",
                "--label",
                LABEL,
                "--limit",
                "100",
                "--json",
                "number,title,author",
            ]
        )
        or "null"
    )
    numbers = [
        int(issue["number"])
        for issue in issues or []
        if str(issue.get("title") or "") == title
        and str((issue.get("author") or {}).get("login") or "") in BOT_LOGINS
    ]
    return min(numbers) if numbers else None


class Post(NamedTuple):
    action: str  # open | comment
    check: str
    issue: Optional[str]  # a number, or "new"
    title: Optional[str]
    body: str


class Line(NamedTuple):
    check: str
    text: str


def decide(passes: Sequence[Pass], issue: Optional[int]) -> List[Post | Line]:
    """What to post for this run, pass by pass; ``Line`` for nothing."""
    out: List[Post | Line] = []
    target: Optional[str] = None if issue is None else str(issue)
    for item in passes:
        if item.template is None:
            out.append(Line(item.name, f"no verdict (the job's result: {item.result})"))
        elif item.template == GREEN:
            out.append(Line(item.name, "GREEN"))
        elif target is None:
            rendered = qa_render.render(ISSUE, item.values)
            out.append(Post("open", item.name, None, rendered.title, rendered.body))
            target = "new"
        else:
            body = qa_render.render(RED, item.values).body
            out.append(Post("comment", item.name, target, None, body))
    return out


def describe(item: Post | Line) -> str:
    if isinstance(item, Line):
        return f"{item.check}: {item.text}"
    if item.action == "open":
        return f"{item.check}: RED; open the fuzz-failure issue with its summary"
    where = "the issue this run opens" if item.issue == "new" else f"#{item.issue}"
    return f"{item.check}: RED; comment its summary on {where}"


def write_posts(items: Sequence[Post | Line], out: Path) -> int:
    posts = out / "posts"
    posts.mkdir(parents=True, exist_ok=True)
    count = 0
    for item in items:
        if not isinstance(item, Post):
            continue
        count += 1
        target = posts / f"{count:02d}"
        target.mkdir()
        (target / "action").write_text(item.action + "\n", encoding="ascii")
        if item.issue is not None:
            (target / "issue").write_text(f"{item.issue}\n", encoding="ascii")
        if item.title is not None:
            (target / "title.txt").write_text(item.title + "\n", encoding="ascii")
        (target / "body.md").write_text(item.body, encoding="ascii")
    return count


def report(args: argparse.Namespace, env: Mapping[str, str], gh: Gh = run_gh) -> int:
    counted = args.event == "schedule" and args.ref == f"refs/heads/{BRANCH}"
    out: Path = args.out
    out.mkdir(parents=True, exist_ok=True)
    (out / "posts").mkdir(exist_ok=True)
    passes = read_passes(env)
    issue = open_issue(gh, env["GITHUB_REPOSITORY"])
    items = decide(passes, issue)
    posts = write_posts(items, out) if counted else 0
    if counted:
        print(f"a scheduled run on {BRANCH}: {posts} post(s)")
    else:
        print(
            f"dry run: a {args.event} run on {args.ref} posts nothing; a scheduled run"
            f" on {BRANCH} with these results would do this"
        )
    print(f"open fuzz-failure issue: {'none' if issue is None else '#' + str(issue)}")
    for item in items:
        print(("would: " if not counted else "") + describe(item))
    return 0


def main(
    argv: Optional[List[str]] = None,
    env: Optional[Mapping[str, str]] = None,
    gh: Optional[Gh] = None,
) -> int:
    env = os.environ if env is None else env
    gh = run_gh if gh is None else gh
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--event", required=True)
    parser.add_argument("--ref", required=True)
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        return report(args, env, gh)
    except (GhError, PassError, qa_render.RenderError) as error:
        # These name a pass, a command or a placeholder, never a value.
        print(f"::error title=API fuzzing::{error}")
        return 1
    except (ValueError, KeyError, TypeError) as error:
        print(f"::error title=API fuzzing::the report failed ({type(error).__name__})")
        return 1


if __name__ == "__main__":
    sys.exit(main())
