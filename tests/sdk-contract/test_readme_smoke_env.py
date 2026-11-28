"""Each SDK README's contract-smoke section names every environment variable the smoke reads.

The smoke is ``sdk/<sdk>/examples/contract_smoke.*``; the README's "Contract smoke" section
lists the variables it takes in its "Env:" paragraph. A variable the smoke reads and the
README does not list is a setting a reader cannot find, and one the README lists that the
smoke ignores is a setting that does nothing. Both are checked.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
NAME = re.compile(r"\b((?:CONTRACT|CONTROLVAR)_[A-Z_]+)\b")
SDKS = ["js", "react", "python", "openfeature", "openfeature-python", "edge"]


def smoke_names(sdk: str) -> set[str]:
    files = sorted((ROOT / "sdk" / sdk / "examples").glob("contract_smoke.*"))
    assert files, f"sdk/{sdk} has no examples/contract_smoke.*"
    return {n for f in files for n in NAME.findall(f.read_text(encoding="utf-8"))}


def readme_names(sdk: str) -> set[str]:
    text = (ROOT / "sdk" / sdk / "README.md").read_text(encoding="utf-8")
    # "## Contract smoke" in some READMEs, "### Contract smoke" in others; the section runs to the
    # next heading of the same or a higher level.
    start = re.search(r"^(#{2,3}) Contract smoke\n", text, re.M)
    assert start, f"sdk/{sdk}/README.md has no Contract smoke section"
    level = len(start.group(1))
    rest = text[start.end() :]
    end = re.search(r"^#{2,%d} " % level, rest, re.M)
    section = rest[: end.start()] if end else rest
    return set(NAME.findall(section))


@pytest.mark.parametrize("sdk", SDKS)
def test_the_readme_lists_the_variables_the_smoke_reads(sdk):
    smoke, readme = smoke_names(sdk), readme_names(sdk)
    assert (sorted(smoke - readme), sorted(readme - smoke)) == ([], []), (
        f"sdk/{sdk}: variables the smoke reads and the README omits, then the reverse"
    )
