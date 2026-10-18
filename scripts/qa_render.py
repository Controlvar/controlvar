# SPDX-FileCopyrightText: 2026 The Controlvar authors
# SPDX-License-Identifier: Apache-2.0
"""Render a QA template from ``.github/qa-templates/``: the only reader of it.

Every issue, comment and step summary the automated QA workflows post is one
of those templates with its placeholders filled in. This repository is public,
so what a workflow posts is public the moment it lands; this module is the one
place a template becomes text, and it refuses anything it cannot vouch for:

* the placeholders are exactly the ones the template holds: a name the mapping
  below does not know, a placeholder with no value, or a value for a name the
  template does not hold is refused;
* every value is checked against its kind with an anchored pattern (a commit
  is 7 to 40 hex digits, a run link is a run of this repository, a check name
  comes from a closed set, a status is three digits or ``no answer``, counts
  and seeds are bounded digits, a heading is one line with none of
  ``@ < > $ { } `` and no URL);
* the text is rendered in one pass (a value is never scanned again), never by
  a shell, a heredoc or ``envsubst``, and the result must be ASCII.

An ``-issue.tmpl`` template renders to a title (its first line) and a body
(everything after the blank second line); any other template is a body only.

    python3 scripts/qa_render.py synthetic-issue.tmpl \\
        --set check=staging --set step=4 ... \\
        --out body.md --title-out title.txt
"""

from __future__ import annotations

import argparse
import datetime as _dt
import os
import re
import sys
from pathlib import Path
from typing import Callable, Dict, List, Mapping, NamedTuple, Optional, Sequence

TEMPLATES_DIR = Path(__file__).resolve().parents[1] / ".github" / "qa-templates"

#: A workflow run of the repository the workflow runs in, which Actions names
#: in ``GITHUB_REPOSITORY``; a run link is exactly one of them. The name is read
#: when a value is checked, never written here, so a renamed or transferred
#: repository needs no edit. With no ``GITHUB_REPOSITORY`` no run link passes.
RUN_LINK_SERVER = "https://github.com"
REPOSITORY_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]{0,38}/[A-Za-z0-9._-]{1,100}")

#: Check names: the synthetic targets and the fuzzing passes.
CHECKS = frozenset({"staging", "production", "superuser", "viewer", "sdk"})


class RenderError(ValueError):
    """A template or a value this module refuses to render."""


def _fullmatch(pattern: str) -> Callable[[str], bool]:
    compiled = re.compile(pattern)
    return lambda value: compiled.fullmatch(value) is not None


def _is_date(value: str) -> bool:
    if not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value):
        return False
    try:
        _dt.date.fromisoformat(value)
    except ValueError:
        return False
    return True


def _is_date_time(value: str) -> bool:
    match = re.fullmatch(
        r"([0-9]{4}-[0-9]{2}-[0-9]{2}) ([0-9]{2}):([0-9]{2}) UTC", value
    )
    if match is None or not _is_date(match.group(1)):
        return False
    return int(match.group(2)) < 24 and int(match.group(3)) < 60


_HEADING_REFUSED = frozenset("@<>${}`\\")
_URL_SHAPE = re.compile(r"[a-z][a-z0-9+.-]*://|www\.", re.I)


def run_link_prefix(environ: Optional[Mapping[str, str]] = None) -> Optional[str]:
    """``https://github.com/<GITHUB_REPOSITORY>/actions/runs/``, or None."""
    environ = os.environ if environ is None else environ
    repository = environ.get("GITHUB_REPOSITORY", "")
    if REPOSITORY_NAME.fullmatch(repository) is None:
        return None
    return f"{RUN_LINK_SERVER}/{repository}/actions/runs/"


def _is_run_link(value: str) -> bool:
    prefix = run_link_prefix()
    if prefix is None or not value.startswith(prefix):
        return False
    return re.fullmatch(r"[1-9][0-9]{0,19}", value[len(prefix) :]) is not None


def _is_heading(value: str) -> bool:
    if not 1 <= len(value) <= 120 or value != value.strip():
        return False
    if any(not (" " <= ch <= "~") for ch in value):  # one line, printable ASCII
        return False
    if _HEADING_REFUSED.intersection(value):
        return False
    return _URL_SHAPE.search(value) is None


#: How a value of each kind must look. Anchored: the whole value matches.
KINDS: Dict[str, Callable[[str], bool]] = {
    "date": _is_date,
    "date_time": _is_date_time,
    "sha": _fullmatch(r"[0-9a-f]{7,40}"),
    "run_link": _is_run_link,
    "check": lambda value: value in CHECKS,
    "step": _fullmatch(r"[1-8]"),
    "status": lambda value: (
        value == "no answer" or re.fullmatch(r"[1-5][0-9]{2}", value) is not None
    ),
    "seed": _fullmatch(r"[0-9]{1,10}"),
    "duration": _fullmatch(r"(?:[1-9][0-9]{0,3} h )?[0-9]{1,2} min"),
    "count": _fullmatch(r"[0-9]{1,6}"),
    # A page of the docs nav: README.md and Enhanced_Rules_Engine_Reference.md
    # are two, so capitals are letters here like any other. Or a dashboard
    # component a journey walks (the first-run card, FirstRunChecklist.tsx).
    "guide_path": lambda value: (
        len(value) <= 200
        and re.fullmatch(
            r"[A-Za-z0-9][A-Za-z0-9_-]*(?:/[A-Za-z0-9][A-Za-z0-9_.-]*)*\.(?:md|tsx)",
            value,
        )
        is not None
    ),
    "step_number": _fullmatch(r"[1-9][0-9]{0,2}"),
    "heading": _is_heading,
}

#: Every placeholder name and its kind: the one fixed mapping. A name starts
#: with its kind (``count_runs`` is a count), as the templates' README says.
PLACEHOLDERS: Dict[str, str] = {
    "date": "date",
    "date_time": "date_time",
    "sha": "sha",
    "run_link": "run_link",
    "check": "check",
    "step": "step",
    "status": "status",
    "seed": "seed",
    "duration": "duration",
    "count_runs": "count",
    "count_fuzzed": "count",
    "count_operations": "count",
    "count_5xx": "count",
    "count_5xx_unlisted": "count",
    "count_known_quiet": "count",
    "count_reached": "count",
    "count_unreached": "count",
    "count_unreached_unlisted": "count",
    "count_unreached_stale": "count",
    "count_pass": "count",
    "count_fail": "count",
    "count_partial": "count",
    "count_nav": "count",
    "count_not_covered": "count",
    "count_steps": "count",
    "count_steps_pass": "count",
    "count_not_run": "count",
    "guide_path": "guide_path",
    "step_number": "step_number",
    "heading_guide": "heading",
    "heading_step": "heading",
}

PLACEHOLDER = re.compile(r"\{([a-z][a-z0-9_]*)\}")
TEMPLATE_NAME = re.compile(r"[a-z][a-z0-9-]*\.tmpl")


class Rendered(NamedTuple):
    """A rendered template: ``title`` is None unless it is an issue."""

    title: Optional[str]
    body: str


def read_template(name: str, templates_dir: Path = TEMPLATES_DIR) -> str:
    """The template's text; only a plain ``<name>.tmpl`` in the directory."""
    if TEMPLATE_NAME.fullmatch(name) is None:
        raise RenderError(f"not a template name: {name!r}")
    path = templates_dir / name
    if not path.is_file():
        raise RenderError(f"no such template: {name}")
    return path.read_text(encoding="utf-8")


def check_value(name: str, value: str) -> None:
    """Refuse a value that is not of its placeholder's kind."""
    kind = PLACEHOLDERS.get(name)
    if kind is None:
        raise RenderError(f"unknown placeholder: {name}")
    if not isinstance(value, str) or not KINDS[kind](value):
        raise RenderError(f"{name}: not a valid {kind} value")


def render_text(text: str, values: Mapping[str, str]) -> str:
    """Fill every placeholder of ``text`` from ``values``, in one pass."""
    used = set(PLACEHOLDER.findall(text))
    unknown = sorted(used - set(PLACEHOLDERS))
    if unknown:
        raise RenderError(f"the template holds unknown placeholders: {unknown}")
    missing = sorted(used - set(values))
    if missing:
        raise RenderError(f"no value for: {missing}")
    extra = sorted(set(values) - used)
    if extra:
        raise RenderError(
            f"values for placeholders the template does not hold: {extra}"
        )
    for name in sorted(used):
        check_value(name, values[name])
    out = substitute(text, values)
    if "{" in out or "}" in out:
        # Only a lower-case name is a placeholder: "{Run_link}" or "{ check }"
        # would otherwise be posted as it stands.
        raise RenderError("a brace is left in the rendered text")
    if not out.isascii() or any(ch < " " and ch != "\n" for ch in out):
        raise RenderError("the rendered text is not plain ASCII")
    return out


def substitute(text: str, values: Mapping[str, str]) -> str:
    """Each placeholder replaced by its value, in one pass: ``re.sub`` never
    rescans what it inserted."""
    return PLACEHOLDER.sub(lambda match: values[match.group(1)], text)


def render(
    name: str, values: Mapping[str, str], templates_dir: Path = TEMPLATES_DIR
) -> Rendered:
    """Render template ``name``; an issue template yields a title and a body."""
    text = render_text(read_template(name, templates_dir), values)
    if not name.endswith("-issue.tmpl"):
        return Rendered(None, text)
    title, blank, body = (text.split("\n", 2) + ["", ""])[:3]
    if not title.strip() or blank != "" or not body.strip():
        raise RenderError(f"{name}: an issue is its title, a blank line, then the body")
    return Rendered(title, body)


def render_title(
    name: str, values: Mapping[str, str], templates_dir: Path = TEMPLATES_DIR
) -> str:
    """Only the title of issue template ``name``, from the title's own
    placeholders (how a workflow finds the issue it filed before)."""
    if not name.endswith("-issue.tmpl"):
        raise RenderError(f"{name} is not an issue template")
    title = read_template(name, templates_dir).split("\n", 1)[0]
    return render_text(title, values)


def _parse_sets(pairs: Sequence[str]) -> Dict[str, str]:
    values: Dict[str, str] = {}
    for pair in pairs:
        name, sep, value = pair.partition("=")
        if not sep or not name:
            raise RenderError("each --set is name=value")
        if name in values:
            raise RenderError(f"{name} is set twice")
        values[name] = value
    return values


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("template", help="a file name in .github/qa-templates/")
    parser.add_argument("--set", dest="sets", action="append", default=[])
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--title-out", type=Path)
    args = parser.parse_args(argv)
    try:
        rendered = render(args.template, _parse_sets(args.sets))
        if (rendered.title is None) != (args.title_out is None):
            raise RenderError("--title-out is for an issue template, and only for one")
    except RenderError as error:
        # The message names placeholders and kinds, never a value.
        print(f"qa_render: refused: {error}", file=sys.stderr)
        return 2
    args.out.write_text(rendered.body, encoding="ascii")
    if rendered.title is not None:
        args.title_out.write_text(rendered.title + "\n", encoding="ascii")
    return 0


if __name__ == "__main__":
    sys.exit(main())
