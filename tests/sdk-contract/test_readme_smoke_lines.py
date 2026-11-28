"""Each SDK README's contract-smoke "It prints" line carries every report key the live runner
requires of that SDK, and none it refuses (checked both ways, as the runner checks a report)."""

from __future__ import annotations

import importlib.util
import json
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location(
    "rlc", ROOT / "tests/sdk-contract/live/run_live_contract.py"
)
rlc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(rlc)

# "alias" is reported only with the sdk:ruleset key, which the README's command does not pass.
REQUIRED = {
    "event_id": rlc.EVENT_ID_SDKS,
    "targeted": rlc.ATTRIBUTE_SDKS,
    "exposure": rlc.EXPOSURE_SDKS,
    "values": rlc.VALUE_SDKS,
    "multiway": rlc.MULTIWAY_SDKS,
}
LINE = re.compile(r'^\{"sdk":"[^"]+".*\}$', re.M)


def printed(readme: Path) -> dict | None:
    match = LINE.search(readme.read_text(encoding="utf-8"))
    return (
        None if match is None else json.loads(re.sub(r"<[^>]*>|…", "x", match.group(0)))
    )


READMES = sorted(p for p in ROOT.glob("sdk/*/README.md") if printed(p) is not None)


def test_some_readmes_are_checked():
    assert len(READMES) >= 10


@pytest.mark.parametrize("readme", READMES, ids=lambda p: p.parent.name)
def test_the_printed_line_has_the_keys_the_runner_requires(readme):
    line = printed(readme)
    sdk = line["sdk"]
    missing = sorted(k for k, sdks in REQUIRED.items() if sdk in sdks and k not in line)
    extra = sorted(k for k, sdks in REQUIRED.items() if sdk not in sdks and k in line)
    assert (missing, extra) == ([], []), (
        f"{readme.relative_to(ROOT)}: missing {missing}, not required {extra}"
    )
