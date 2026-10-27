"""The commands on a test's PATH that run the harness's fakes (aws, cdk, crane ...).

Each is ``#!/bin/sh`` exec-ing this test's own Python on one of the harness's modules:

    python -I -S -c 'import fake_aws; fake_aws.main_argv(...)' <site> <harness> <args...>

``-S`` skips site (and the site-packages ``.pth`` hooks a developer's venv may carry),
so the two folders the fakes import from are put first on sys.path by hand: this test's
site-packages (for jmespath) and the harness folder. Importing the fake as a module, not
running it as a script, lets Python keep its compiled form between calls. A fake call
starts in about a third of the time a plain ``python -I fake_aws.py`` takes, and the
fault sweeps make thousands of them.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

HARNESS = Path(__file__).resolve().parent


def site_dir() -> str:
    import jmespath

    return os.path.dirname(os.path.dirname(jmespath.__file__))


def write(path: Path, module: str, *fixed: str, site: str | None = None) -> None:
    """``path``: a command running harness/<module>.main_argv([*fixed, *its arguments])."""
    code = f"import sys; sys.path[:0] = sys.argv[1:3]; import {module}; sys.exit({module}.main_argv(sys.argv[3:]))"
    words = [site or site_dir(), str(HARNESS), *fixed]
    quoted = " ".join('"' + w.replace('"', '\\"') + '"' for w in words)
    path.write_text(
        f'#!/bin/sh\nexec "{sys.executable}" -I -S -c \'{code}\' {quoted} "$@"\n'
    )
    path.chmod(0o755)
