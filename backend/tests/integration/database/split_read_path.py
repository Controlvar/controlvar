"""What serving a split and a rule's own value adds to the code (#2012), for the
tests that copy this tree and take it out to stand for a release without it.

Each pattern is a whole statement and is found exactly once; with all of them
removed, a flag serves its two values and nothing else, its ruleset entry is
decided by its rules alone, and the ruleset's query and the stale-flag hint do
not name the column. If one is not found, the test that uses this says so
rather than testing the wrong tree.
"""

from __future__ import annotations

import re
from pathlib import Path

#: The file under ``backend/app``: what is removed from it.
READ_PATH = {
    ("core", "flag_values.py"): (
        # The check of a rule's value; a model without the column has no split
        # to check.
        re.compile(
            r"(?m)^    values \+= rule_values\("
            r'getattr\(flag, "targeting_rules", None\)\)\.values\(\)\n'
        ),
        # ``serve``: only its first answer, the two values', is left.
        re.compile(
            r"(?ms)^    own = rule_values\("
            r'getattr\(flag, "targeting_rules", None\)\)\n'
            r".*?^        return True, split_value\([^\n]*\)\n(?=    return True, value\n)"
        ),
    ),
    ("services", "sdk_ruleset.py"): (
        re.compile(
            r"(?ms)^    if has_split_or_rule_value\(flag\):\n.*?^        return remote\n"
        ),
    ),
    ("api", "v1", "endpoints", "sdk.py"): (
        re.compile(r"(?m)^ +FeatureFlag\.split,\n"),
    ),
    ("services", "stale_flags.py"): (
        re.compile(r"(?m)^    if flag\.split is not None:\n        return \[\]\n"),
    ),
}


def without_the_read_path(tree: Path) -> None:
    """Remove it from the copy at *tree*."""
    for parts, additions in READ_PATH.items():
        module = tree.joinpath("backend", "app", *parts)
        source = module.read_text(encoding="utf-8")
        for addition in additions:
            source, found = addition.subn("", source)
            assert found == 1, f"{found} matches of {addition.pattern}: update it"
        module.write_text(source, encoding="utf-8")
