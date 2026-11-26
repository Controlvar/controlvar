"""What setting a split and a rule's own value through the API adds to the code
(#2012), for the tests that copy this tree and take it out to stand for a
release without it.

Each pattern is a whole statement and is found exactly as often as its count
says. With all of them applied, no request takes ``split`` (``extra_forbidden``)
or a rule's ``value`` (``unknown key``), and no response carries a split: what
the release before the write path answered. Reading and serving one stay
(``split_read_path``). If a pattern is not found, the test that uses this says
so rather than testing the wrong tree.
"""

from __future__ import annotations

import re
from pathlib import Path

_REQUEST_FIELD = (
    r"(?m)^    split: Any = Field\(\n"
    r"        None,\n"
    r"        description=SPLIT_REQUEST_DESCRIPTION,\n"
    r"        json_schema_extra=_split_schema\(\),\n"
    r"    \)\n"
)

#: The file under ``backend/app``: ``(pattern, replacement, count)``.
WRITE_PATH = {
    ("schemas", "feature_flag.py"): (
        # The create's and the update's field.
        (re.compile(_REQUEST_FIELD), "", 2),
        # The response's field, and its reading.
        (
            re.compile(
                r"(?m)^    split: Optional\[Any\] = Field\("
                r"None, description=SPLIT_READ_DESCRIPTION\)\n"
            ),
            "",
            1,
        ),
        (
            re.compile(
                r'(?m)^        values\["split"\] = read_split\('
                r'value_type, values\.get\("split"\)\)\n'
            ),
            "",
            1,
        ),
    ),
    ("schemas", "flag_environment.py"): (
        (re.compile(_REQUEST_FIELD), "", 1),
        (
            re.compile(
                r"(?m)^    split: Optional\[Any\] = Field\(\n"
                r"        None, description=\"The environment's split\. \" "
                r"\+ SPLIT_READ_DESCRIPTION\n"
                r"    \)\n"
            ),
            "",
            1,
        ),
        (
            re.compile(
                r"(?m)^            split=read_split\(view\.value_type, view\.split\),\n"
            ),
            "",
            1,
        ),
    ),
    ("core", "targeting_adapter.py"): (
        (
            re.compile(
                r"(?m)^(_FLAG_DASHBOARD_TOP_KEYS = _DASHBOARD_TOP_KEYS)"
                r" \| \{RULE_VALUE_KEY\}$"
            ),
            r"\1",
            1,
        ),
        (
            re.compile(
                r"(?m)^(_FLAG_NATIVE_RULE_KEYS = _NATIVE_RULE_KEYS)"
                r" \| \{RULE_VALUE_KEY\}$"
            ),
            r"\1",
            1,
        ),
    ),
}


def without_the_write_path(tree: Path) -> None:
    """Remove it from the copy at *tree*."""
    for parts, edits in WRITE_PATH.items():
        module = tree.joinpath("backend", "app", *parts)
        source = module.read_text(encoding="utf-8")
        for pattern, replacement, count in edits:
            source, found = pattern.subn(replacement, source)
            assert found == count, (
                f"{found} matches of {pattern.pattern} (expected {count}): update it"
            )
        module.write_text(source, encoding="utf-8")
