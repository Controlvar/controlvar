#!/usr/bin/env python3
"""Extract the marked shell blocks of an install guide, so CI runs the page itself.

The ``chart-kind`` job (``.github/workflows/chart-kind.yml``) runs the blocks
of ``docs/self-hosting/kubernetes.md`` on a kind cluster, and the
``compose-upgrade`` job (``.github/workflows/compose-production.yml``) runs
those of ``deploy/compose/README.md`` on a Docker host, each exactly as a
reader would paste them. Neither keeps a copy of them: a copy is what drifts.
A block is marked by an HTML comment on the line directly above its fence,
which the site does not show, naming the job that runs it::

    <!-- chart-kind: secrets -->
    ```bash
    kubectl create secret generic ...
    ```

Usage::

    python3 scripts/guide_blocks.py PAGE [--marker JOB] --expect secrets,install --out DIR

``--marker`` is the job's name in the comment, ``chart-kind`` by default; a
page is read for one job's markers only, and every message names that one.
It writes ``DIR/<name>.sh`` for every marked block and exits 0 only when:

* every ``bash`` fence on the page is marked, so a block cannot be added to the
  page without the job either running it or failing. Fences are found at any
  indentation (a list item's) and with backticks or tildes;
* no fence is unlabelled, tagged (``{.bash ...}``) or in another shell dialect
  (``sh``, ``shell``, ``zsh``, ``console``, ...): each of those would be a
  command on the page that nothing runs;
* every marker sits directly above a ``bash`` fence, and names a block once;
* the marked names, in page order, equal ``--expect`` exactly -- the job's own
  list of what it runs, so a block that is renamed or dropped fails here
  instead of silently not running.

Every problem is reported at once, one line each, and the exit status is 1.
"""

from __future__ import annotations

import argparse
import pathlib
import re
import sys
from typing import List, Tuple

#: The marker chart_kind.sh's call, which names none, reads.
DEFAULT_MARKER = "chart-kind"
#: A job name a marker may carry, and a block name: lower case, digits, hyphens.
NAME = re.compile(r"[a-z][a-z0-9-]*")


def marker_patterns(marker: str) -> Tuple["re.Pattern[str]", "re.Pattern[str]"]:
    """``(MARKER, LOOSE_MARKER)`` for one job: the exact marker line, and any
    line that mentions the job's marker at all (a malformed one)."""
    if not NAME.fullmatch(marker):
        raise ValueError(f"not a marker name: {marker!r}")
    exact = re.compile(rf"^[ \t]*<!-- {marker}: ([a-z][a-z0-9-]*) -->[ \t]*$")
    return exact, re.compile(rf"{marker}:")


MARKER, LOOSE_MARKER = marker_patterns(DEFAULT_MARKER)
# A fence at any indentation (inside a list item it is indented), of backticks
# or tildes, three or more. The closing fence is the same character, at least
# as long, with nothing after it.
FENCE_OPEN = re.compile(r"^([ \t]*)(`{3,}|~{3,})[ \t]*(.*?)[ \t]*$")
# Shell dialects other than bash: a block in one of them would be neither run
# nor marked, so they are refused on this page; write `bash`, with a marker.
OTHER_SHELLS = frozenset(
    {
        "sh",
        "shell",
        "zsh",
        "console",
        "shell-session",
        "sh-session",
        "shellsession",
        "ksh",
        "fish",
        "powershell",
        "ps1",
        "pwsh",
        "bat",
        "cmd",
    }
)


def _closes(line: str, fence: str) -> bool:
    stripped = line.strip()
    return len(stripped) >= len(fence) and set(stripped) == {fence[0]}


def extract(
    text: str, page: str, marker: str = DEFAULT_MARKER
) -> Tuple[List[Tuple[str, str]], List[str]]:
    """Return ``([(name, body), ...], problems)`` for one page's text, read
    for the markers of the job *marker*.

    Every fence names a language. ``bash`` needs a marker; other shell
    dialects and an unlabelled fence are refused (use ``text`` for output);
    any other language (``text``, ``json``, ``yaml``, ...) is left alone.
    """
    marker_line, loose_marker = marker_patterns(marker)
    shape = f"'<!-- {marker}: NAME -->'"
    lines = text.splitlines()
    blocks: List[Tuple[str, str]] = []
    problems: List[str] = []
    pending: Tuple[str, int] | None = None
    i = 0
    while i < len(lines):
        line = lines[i]
        opened = FENCE_OPEN.match(line)
        if opened:
            indent, fence, info_string = opened.groups()
            info = info_string.split()[0] if info_string else ""
            body: List[str] = []
            j = i + 1
            while j < len(lines) and not _closes(lines[j], fence):
                body_line = lines[j]
                body.append(
                    body_line[len(indent) :]
                    if body_line.startswith(indent)
                    else body_line
                )
                j += 1
            if j == len(lines):
                problems.append(f"{page}:{i + 1}: a fence that never closes")
            if not info:
                problems.append(
                    f"{page}:{i + 1}: a fence with no language; name one "
                    "(`bash` with a marker to run it, `text` for output)"
                )
            elif info.startswith("{"):
                problems.append(
                    f"{page}:{i + 1}: a tagged fence '{info_string}'; this page's blocks "
                    f"are plain `bash` with a {shape} marker"
                )
            elif info.lower() in OTHER_SHELLS:
                problems.append(
                    f"{page}:{i + 1}: a '{info}' block is neither run nor checked; "
                    f"write it as `bash` with a {shape} marker"
                )
            if info == "bash":
                if pending is None:
                    problems.append(
                        f"{page}:{i + 1}: a bash block with no {shape} "
                        "line directly above it; every shell block on this page is run by "
                        f"the {marker} job"
                    )
                else:
                    blocks.append((pending[0], "\n".join(body) + "\n"))
            elif pending is not None:
                problems.append(
                    f"{page}:{pending[1]}: marker '{pending[0]}' is above a "
                    f"'{info or 'unlabelled'}' fence, not a bash block"
                )
            pending = None
            i = j + 1
            continue
        if pending is not None:
            problems.append(
                f"{page}:{pending[1]}: marker '{pending[0]}' is not directly above a bash block"
            )
            pending = None
        marked = marker_line.match(line)
        if marked:
            pending = (marked.group(1), i + 1)
        elif loose_marker.search(line):
            problems.append(
                f"{page}:{i + 1}: a malformed marker; write exactly {shape} "
                "on its own line"
            )
        i += 1
    if pending is not None:
        problems.append(
            f"{page}:{pending[1]}: marker '{pending[0]}' is not directly above a bash block"
        )

    seen: set[str] = set()
    for name, _ in blocks:
        if name in seen:
            problems.append(f"{page}: block '{name}' is marked more than once")
        seen.add(name)
    return blocks, problems


def main(argv: List[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("page", type=pathlib.Path)
    parser.add_argument(
        "--marker",
        default=DEFAULT_MARKER,
        help=f"the job named in the page's markers (default: {DEFAULT_MARKER})",
    )
    parser.add_argument(
        "--expect",
        required=True,
        help="the block names the caller runs, comma-separated, in page order",
    )
    parser.add_argument("--out", type=pathlib.Path, required=True)
    args = parser.parse_args(argv)
    if not NAME.fullmatch(args.marker):
        parser.error(
            f"--marker {args.marker!r} is not a job name: lower case letters, "
            "digits and hyphens, starting with a letter"
        )

    page = str(args.page)
    blocks, problems = extract(
        args.page.read_text(encoding="utf-8"), page, marker=args.marker
    )
    names = [name for name, _ in blocks]
    expected = [name for name in args.expect.split(",") if name]
    if names != expected:
        problems.append(
            f"{page}: the marked blocks are {names}; the job runs {expected}. "
            "Change both together."
        )
    for name, body in blocks:
        if not body.strip():
            problems.append(f"{page}: block '{name}' is empty")

    if problems:
        for problem in problems:
            print(f"refused: {problem}", file=sys.stderr)
        print(f"{len(problems)} problem(s)", file=sys.stderr)
        return 1

    args.out.mkdir(parents=True, exist_ok=True)
    for name, body in blocks:
        (args.out / f"{name}.sh").write_text(body, encoding="utf-8")
        print(f"{page}: block '{name}' -> {args.out / (name + '.sh')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
