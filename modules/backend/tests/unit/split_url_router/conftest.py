"""The split URL edge app, synthesised once for the tests that read its cloud
assembly (#393).

It is synthesised the way ``cdk synth`` runs it: ``python app.py`` in a
subprocess, from the app's own directory, with ``CDK_OUTDIR`` set and the
settings in the environment. It is never imported with
``modules/infrastructure`` on ``sys.path``, where the local ``constructs``
directory would shadow the library. No AWS credential is passed and none is
needed: synth reads none.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Dict, NamedTuple, Optional

import pytest

ROOT = Path(__file__).resolve().parents[5]
EDGE_APP = ROOT / "modules" / "infrastructure" / "split_url_edge"
#: A key in the API's format (eptk_ and 32 hex digits) that appears nowhere
#: else, so a scan can find every copy of it.
API_KEY = "eptk_" + "5ec7e7" + "0" * 20 + "c0ffee"
SETTINGS = {
    "SPLIT_URL_API_URL": "https://api.example.com",
    "SPLIT_URL_API_KEY": API_KEY,
    "SPLIT_URL_ORIGIN": "origin.example.com",
    "SPLIT_URL_COOKIE_NAME": "site_uid",
    "SPLIT_URL_COOKIE_MAX_AGE_DAYS": "7",
    "SPLIT_URL_COOKIE_DOMAIN": "example.com",
    "SPLIT_URL_DOMAIN_NAMES": "shop.example.com, www.example.com",
    "SPLIT_URL_CERTIFICATE_ARN": (
        "arn:aws:acm:us-east-1:123456789012:certificate/1234abcd-12ab-34cd-56ef-1234567890ab"
    ),
}
REQUIRED = ("SPLIT_URL_API_URL", "SPLIT_URL_API_KEY", "SPLIT_URL_ORIGIN")


class Synth(NamedTuple):
    returncode: int
    stdout: str
    stderr: str
    outdir: Path
    tmpdir: Path

    def template(self) -> dict:
        (path,) = sorted(self.outdir.glob("*.template.json"))
        return json.loads(path.read_text(encoding="utf-8"))

    def manifest(self) -> dict:
        return json.loads((self.outdir / "manifest.json").read_text(encoding="utf-8"))

    def function_asset(self) -> Path:
        """The directory the router's code asset was staged in."""
        (path,) = sorted(self.outdir.glob("*.assets.json"))
        files = json.loads(path.read_text(encoding="utf-8"))["files"]
        sources = [
            entry["source"]["path"]
            for entry in files.values()
            if entry.get("displayName", "").endswith("/Router/Code")
        ]
        assert len(sources) == 1, files
        return self.outdir / sources[0]


def synth(
    workdir: Path, settings: Dict[str, Optional[str]], app: Path = EDGE_APP / "app.py"
) -> Synth:
    """Run an app as ``cdk synth`` does, in a subprocess from its directory."""
    outdir = workdir / "cdk.out"
    tmpdir = workdir / "tmp"
    outdir.mkdir(parents=True)
    tmpdir.mkdir()
    env = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": os.environ.get("HOME", str(workdir)),
        "CDK_OUTDIR": str(outdir),
        "TMPDIR": str(tmpdir),
        "PYTHONDONTWRITEBYTECODE": "1",
        "JSII_SILENCE_WARNING_UNTESTED_NODE_VERSION": "1",
    }
    env.update({k: v for k, v in settings.items() if v is not None})
    completed = subprocess.run(
        [sys.executable, app.name],
        cwd=app.parent,
        env=env,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=300,
    )
    return Synth(
        completed.returncode, completed.stdout, completed.stderr, outdir, tmpdir
    )


@pytest.fixture(scope="session")
def edge_synth(tmp_path_factory) -> Synth:
    """The edge app with every setting given."""
    result = synth(tmp_path_factory.mktemp("edge-synth"), SETTINGS)
    assert result.returncode == 0, result.stderr
    return result


@pytest.fixture(scope="session")
def edge_synth_defaults(tmp_path_factory) -> Synth:
    """The edge app with only the required settings."""
    result = synth(
        tmp_path_factory.mktemp("edge-synth-defaults"),
        {name: SETTINGS[name] for name in REQUIRED},
    )
    assert result.returncode == 0, result.stderr
    return result
