"""Every printed command that starts the demo profile seeds its apps (#1266).

``docker compose --profile demo up -d``, as the header of ``docker-compose.yml``
printed it, starts ShopLab, StreamPulse and their simulators against a database
that has only the ``demo`` seed: the apps' API key does not exist, so every
flag shows its fallback and both simulators exit at start. The command that
works is ``SEED=demo,shoplab,streampulse docker compose --profile demo up -d
--wait``, and every page that prints a ``--profile demo ... up`` prints exactly
that, so the one a reader copies is the one that works:

* the header's demo line, the Quick Start's and ``docker-compose-file.md``'s are
  the same string;
* any other line, in any Markdown file or ``docker-compose.yml``, that runs
  ``--profile demo ... up`` contains it (``make demo``'s recipe, which adds
  ``--build``, is held by ``test_front_door_facts.py``).

Standard library only: it runs in the docs-only lane.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Iterator, List, Tuple

import pytest

pytestmark = [pytest.mark.unit, pytest.mark.regression]

ROOT = Path(__file__).resolve().parents[4]
COMMAND = "SEED=demo,shoplab,streampulse docker compose --profile demo up -d --wait"

#: A line that starts the demo profile: `--profile demo`, then `up` later on it.
STARTS_DEMO = re.compile(r"--profile demo\b.*\bup\b")

PRUNED = {"node_modules", "venv", "site", ".git", ".venv"}


def _printed_files() -> Iterator[Path]:
    yield ROOT / "docker-compose.yml"
    for directory, dirs, files in os.walk(ROOT):
        dirs[:] = sorted(d for d in dirs if d not in PRUNED and not d.startswith("."))
        for name in sorted(files):
            if name.endswith(".md"):
                yield Path(directory) / name


def _demo_lines() -> List[Tuple[str, int, str]]:
    found = []
    for path in _printed_files():
        text = path.read_text(encoding="utf-8", errors="replace")
        for number, line in enumerate(text.split("\n"), 1):
            if STARTS_DEMO.search(line):
                found.append((path.relative_to(ROOT).as_posix(), number, line))
    return found


def _fence_lines(relative: str) -> List[str]:
    """The lines inside the page's fenced blocks that start the demo profile."""
    lines, inside = [], False
    for line in (ROOT / relative).read_text(encoding="utf-8").split("\n"):
        if line.startswith("```"):
            inside = not inside
            continue
        if inside and STARTS_DEMO.search(line):
            lines.append(line)
    return lines


def test_the_scan_reaches_the_pages_that_print_the_command():
    printed = {path for path, _, _ in _demo_lines()}
    assert {
        "docker-compose.yml",
        "docs/getting-started/quick-start.md",
        "docs/getting-started/docker-compose-file.md",
        "demo/shoplab/README.md",
        "demo/streampulse/README.md",
    } <= printed, printed


def test_every_printed_demo_start_seeds_the_apps():
    wrong = [
        f"{path}:{number}: {line.strip()}"
        for path, number, line in _demo_lines()
        if COMMAND not in line
    ]
    assert not wrong, (
        "these start the demo profile without the shoplab and streampulse "
        f"seeds; print `{COMMAND}` instead:\n" + "\n".join(wrong)
    )


def test_the_header_and_the_two_pages_print_the_same_command():
    header = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    header = header.split("\n\n", 1)[0]
    demo = [
        line.lstrip("#").strip()
        for line in header.split("\n")
        if "--profile demo" in line
    ]
    assert demo == [COMMAND], demo
    for page in (
        "docs/getting-started/quick-start.md",
        "docs/getting-started/docker-compose-file.md",
    ):
        assert _fence_lines(page) == [COMMAND], page
