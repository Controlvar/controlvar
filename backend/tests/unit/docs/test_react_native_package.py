"""What the React Native README and page say about installing the package is
what its manifest declares (Plan P, P1-RN; EMP-P-8).

The package takes AsyncStorage as a peer at ``>=1.21.0 <4`` (PP-4 (b)), so:

* the README and the SDK page state that range, and the ``react`` and
  ``react-native`` floors, exactly as package.json's ``peerDependencies``
  spell them;
* no install command on either names AsyncStorage on the same line as the
  SDK (its tarball or its npm name), and none names AsyncStorage with a
  version or a range: either would move an app already on 1.x or 2.x to
  another AsyncStorage (npm saves what it is told to install). The one
  AsyncStorage line is the bare add for an app that has none;
* the page's install of the tarball is the tarball alone, which is the
  command scripts/check_react_native_app.py runs in an app on each major;
* both carry the consumer-Jest ``transformIgnorePatterns`` line, which names
  the package (it is TypeScript source under ``node_modules``).

Reads files only: no git and no npm, so it runs the same in
``scripts/core_build.sh``'s copy.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import List

import pytest

pytestmark = [pytest.mark.unit]

REPO_ROOT = Path(__file__).resolve().parents[4]
SDK = REPO_ROOT / "sdk" / "react-native"
STORAGE = "@react-native-async-storage/async-storage"
PACKAGE = "@controlvar/react-native-sdk"
README = "sdk/react-native/README.md"
PAGE = "docs/sdk/react-native.md"


def text(path: str) -> str:
    return (REPO_ROOT / path).read_text(encoding="utf-8")


def peers() -> dict:
    return json.loads((SDK / "package.json").read_text(encoding="utf-8"))[
        "peerDependencies"
    ]


def floor(npm_range: str) -> str:
    """``>=16.8.0`` as the docs write it, ``>= 16.8``."""
    match = re.fullmatch(r">=(\d+)\.(\d+)\.0", npm_range)
    assert match, npm_range
    return f">= {match.group(1)}.{match.group(2)}"


#: An install command, one line of a shell block or of inline code.
INSTALL = re.compile(
    r"\b(?:npm[ \t]{1,10}(?:install|i|add)|yarn[ \t]{1,10}add|pnpm[ \t]{1,10}add)\b[^\n`]{0,400}"
)


def install_commands(page_text: str) -> List[str]:
    return [m.group(0) for m in INSTALL.finditer(page_text)]


def asyncstorage_problems(command: str) -> List[str]:
    """Why an install command would move the app's AsyncStorage, if it would."""
    if STORAGE not in command:
        return []
    problems = []
    if PACKAGE in command or ".tgz" in command:
        problems.append("names AsyncStorage on the SDK's install line")
    if re.search(re.escape(STORAGE) + r"@", command):
        problems.append("names AsyncStorage at a version or range")
    return problems


@pytest.mark.parametrize("path", [README, PAGE])
def test_the_stated_peer_ranges_are_package_json_s(path: str) -> None:
    folded = " ".join(text(path).split())
    storage_range = peers()[STORAGE]
    assert storage_range == ">=1.21.0 <4"
    assert f"`{storage_range}`" in folded, f"{path} does not state {storage_range}"
    assert "1, 2 and 3 are supported" in folded
    for name in ("react", "react-native"):
        stated = floor(peers()[name])
        assert re.search(rf"`{name}` \(?{re.escape(stated)}\)?", folded), (
            f"{path} does not state {name} {stated}"
        )


@pytest.mark.parametrize("path", [README, PAGE])
def test_no_install_command_moves_the_apps_asyncstorage(path: str) -> None:
    found = {c: asyncstorage_problems(c) for c in install_commands(text(path))}
    assert not {c: p for c, p in found.items() if p}, found


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        (
            f"npm install /path/to/controlvar-react-native-sdk-0.1.0.tgz {STORAGE} md5",
            ["names AsyncStorage on the SDK's install line"],
        ),
        (
            f'npm install {PACKAGE}@0.1.0 "{STORAGE}@>=1.21.0 <4"',
            [
                "names AsyncStorage on the SDK's install line",
                "names AsyncStorage at a version or range",
            ],
        ),
        (f"yarn add {STORAGE}@^2.2.0", ["names AsyncStorage at a version or range"]),
        (f"npm install {STORAGE}", []),
        ("npm install /path/to/controlvar-react-native-sdk-0.1.0.tgz", []),
    ],
)
def test_each_way_an_install_line_moves_asyncstorage_is_found(
    command: str, expected: List[str]
) -> None:
    """The plants: the page's old command (the tarball with AsyncStorage and
    md5), the plan's first draft (the range on the line), a pinned add; and
    the two true lines pass."""
    assert install_commands(command) == [command]
    assert asyncstorage_problems(command) == expected


def test_the_page_installs_the_tarball_alone() -> None:
    tarball = [c for c in install_commands(text(PAGE)) if ".tgz" in c]
    assert tarball == [
        "npm install /path/to/controlvar/sdk/react-native/controlvar-react-native-sdk-0.1.0.tgz"
    ]
    assert [c for c in install_commands(text(PAGE)) if STORAGE in c] == [
        f"npm install {STORAGE}"
    ]


@pytest.mark.parametrize("path", [README, PAGE])
def test_the_consumer_jest_config_transforms_the_package(path: str) -> None:
    pattern = re.search(
        r'"transformIgnorePatterns": \[\s*"([^"\n]{1,300})"', text(path)
    )
    assert pattern, f"{path} has no transformIgnorePatterns line"
    assert pattern.group(1).startswith("node_modules/(?!(")
    assert PACKAGE in pattern.group(1)
    assert re.match(pattern.group(1), f"node_modules/{PACKAGE}/src/index.ts") is None
    assert re.match(pattern.group(1), "node_modules/lodash/index.js")
