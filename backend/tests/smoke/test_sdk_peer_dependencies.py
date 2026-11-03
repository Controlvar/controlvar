"""A JS SDK never bundles the host library it plugs into (#76).

``@openfeature/server-sdk`` keeps its API in a module-level singleton. When
the provider declares it in ``dependencies`` and the application's own copy
resolves to a different version, npm nests a second copy under the provider:
``OpenFeature.setProvider`` then registers on one instance while the
application's ``OpenFeature.getClient()`` reads the other, and every flag
resolves through the NoopProvider -- default values, no error.

React has the same property (two copies break hooks), which is why the two
React SDKs already take it as a peer. A React Native native module is the
same: React Native links ``@react-native-async-storage/async-storage`` only
from the app's own dependencies, and as a dependency at ``^1.21.0`` it was
nested a second time under the SDK in an app on 3.x (Plan P, UX-F8), so the
SDK's JavaScript would have called the app's native module of another major.
The React Native SDK takes AsyncStorage as a peer at every major it supports
(Plan P, PP-4 (b)).

So each host library listed below must be:

* in ``peerDependencies``, with the documented range (a narrowed range
  refuses installs the docs promise will work);
* in ``devDependencies``, so the package's own build and tests have it;
* in none of ``dependencies``, ``optionalDependencies`` or
  ``bundleDependencies``;
* and the committed ``package-lock.json`` must say the same: its root entry
  agrees with the manifest, and the installed copy is marked ``dev`` (nothing
  in the runtime closure requires it).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]

pytestmark = pytest.mark.smoke

#: sdk directory -> {host library: the peer range the documentation states}.
HOST_PEERS: dict[str, dict[str, str]] = {
    # docs/sdk/openfeature.md: "Requires Node >= 18 and @openfeature/server-sdk >= 1.7".
    "openfeature": {"@openfeature/server-sdk": "^1.7.0"},
    "react": {"react": ">=17.0.0", "react-dom": ">=17.0.0"},
    # docs/sdk/react-native.md and the README: react >= 16.8, react-native
    # >= 0.65, and AsyncStorage `>=1.21.0 <4` (majors 1, 2 and 3).
    "react-native": {
        "react": ">=16.8.0",
        "react-native": ">=0.65.0",
        "@react-native-async-storage/async-storage": ">=1.21.0 <4",
    },
}

RUNTIME_SECTIONS = ("dependencies", "optionalDependencies")
BUNDLED_SECTIONS = ("bundleDependencies", "bundledDependencies")

CASES = [
    (sdk, name, peer_range)
    for sdk, peers in HOST_PEERS.items()
    for name, peer_range in peers.items()
]


def _json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


@pytest.mark.parametrize(("sdk", "name", "peer_range"), CASES)
def test_host_library_is_a_peer_not_a_dependency(
    sdk: str, name: str, peer_range: str
) -> None:
    manifest = _json(ROOT / "sdk" / sdk / "package.json")

    for section in RUNTIME_SECTIONS:
        assert name not in (manifest.get(section) or {}), (
            f"sdk/{sdk}/package.json declares {name} in {section}. It is the "
            "host library this package plugs into: declare it in "
            "peerDependencies (and devDependencies for the package's own "
            "build), or an application with a different version gets a second "
            "copy and a silently disconnected singleton."
        )
    for section in BUNDLED_SECTIONS:
        bundled = manifest.get(section) or []
        assert name not in bundled, f"sdk/{sdk}/package.json bundles {name}"

    assert (manifest.get("peerDependencies") or {}).get(name) == peer_range, (
        f"sdk/{sdk}/package.json must declare {name} {peer_range!r} in "
        "peerDependencies, the range its documentation promises."
    )
    assert name in (manifest.get("devDependencies") or {}), (
        f"sdk/{sdk}/package.json must also list {name} in devDependencies, so "
        "its own build and tests install it."
    )


@pytest.mark.parametrize(("sdk", "name", "peer_range"), CASES)
def test_the_lockfile_agrees(sdk: str, name: str, peer_range: str) -> None:
    lock = _json(ROOT / "sdk" / sdk / "package-lock.json")
    manifest = _json(ROOT / "sdk" / sdk / "package.json")
    packages = lock["packages"]
    root = packages[""]

    for section in ("dependencies", "optionalDependencies", "peerDependencies"):
        assert (root.get(section) or {}) == (manifest.get(section) or {}), (
            f"sdk/{sdk}/package-lock.json's {section} differ from package.json: "
            "regenerate the lock with `npm install` in that directory."
        )
    assert (root.get("peerDependencies") or {}).get(name) == peer_range

    installed = packages.get(f"node_modules/{name}")
    assert installed is not None, f"sdk/{sdk}/package-lock.json does not install {name}"
    assert installed.get("dev") is True, (
        f"sdk/{sdk}/package-lock.json installs {name} as a production "
        "dependency: something in the runtime closure still requires it."
    )
