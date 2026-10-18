"""The one place the running code learns its own version.

``VERSION`` at the repository root is the single source: ``release-please``
maintains it (``release-please-config.json``), ``pyproject.toml`` reads it
through setuptools' ``dynamic`` version, the images copy it to ``/app/VERSION``
and label themselves from it, and ``settings.VERSION`` — and so ``/health`` and
``GET /api/v1/modules`` — resolve it here.

Three resolution steps, in order, because the code runs in three shapes:

1. **A checkout or an image.**  ``VERSION`` sits at the root of the tree, three
   directories above this file (``backend/app/core/version.py``), and the API
   image reproduces that layout under ``/app``.  This is the normal path.
2. **An installed distribution.**  ``pip install experimently-platform`` puts
   ``backend/`` into ``site-packages`` and leaves ``VERSION`` behind, so the
   recorded distribution metadata answers instead.  It was built *from* the
   same file, so the two agree by construction.
3. **Neither.**  A source tree with the file deleted, or a partial copy.
   ``0.0.0+unknown`` — a valid PEP 440 version that no release can collide
   with, so a deployment reporting it is visibly unreleased rather than
   plausibly wrong.

Deliberately *not* a ``try: import`` of some generated ``_version.py``: a
generated file is a second source that can disagree with the first, and the
whole point of this module is that there is one.
"""

from __future__ import annotations

import re
from functools import lru_cache
from importlib import metadata
from pathlib import Path

#: The distribution built from this tree.  Not ``controlvar`` — that name
#: belongs to the Python SDK (``sdk/python``), which is a different package
#: with a different licence and its own release cadence.
DISTRIBUTION_NAME = "experimently-platform"

#: What a tree with no ``VERSION`` and no installed metadata reports.
UNKNOWN_VERSION = "0.0.0+unknown"

#: ``backend/app/core/version.py`` -> ``backend/app/core`` -> ``backend/app``
#: -> ``backend`` -> the tree root.  The API image copies ``backend/`` to
#: ``/app/backend/`` and ``VERSION`` to ``/app/VERSION``, so the same walk
#: lands on the same file there.
VERSION_FILE = Path(__file__).resolve().parents[3] / "VERSION"

#: What the file is allowed to contain: ``X.Y.Z`` with an optional SemVer
#: pre-release, the same spelling ``scripts/check_version_sources.py`` enforces
#: and the git tag and image tag quote.
#:
#: Validated rather than trusted, for two reasons.  ``parents[3]`` is the tree
#: root in a checkout and ``/app`` in the image, but for an *installed*
#: distribution it is ``site-packages`` -- where an unrelated project that
#: ships a top-level data file called ``VERSION`` would otherwise decide what
#: this API reports.  And whatever the file holds is what ``/health``, ``GET
#: /api/v1/modules`` and the OpenAPI ``info.version`` advertise, so a stray
#: ``v0.1.0``, a second line or a fragment of something else must not become
#: the published version of the API.
_VERSION_PATTERN = re.compile(r"\A\d+\.\d+\.\d+(?:-(?:alpha|beta|rc)\.\d+)?\Z")


@lru_cache(maxsize=1)
def get_version() -> str:
    """Return the running version, resolved once per process.

    Cached because ``Settings`` reads it at import time and the health and
    modules endpoints read it per request; the answer cannot change without a
    new process.
    """
    text = ""
    try:
        # Bytes, not ``read_text``: a file that is not valid UTF-8 raises
        # UnicodeDecodeError, which is a ValueError and not an OSError, and
        # ``Settings`` evaluates this in its class body -- so an unreadable
        # ``VERSION`` would surface as an ImportError from
        # ``backend.app.core.config`` and kill the process at startup instead
        # of degrading to the placeholder three lines below.
        text = VERSION_FILE.read_bytes().decode("utf-8", errors="replace").strip()
    except OSError:
        pass
    if _VERSION_PATTERN.match(text):
        return text

    try:
        installed = metadata.version(DISTRIBUTION_NAME)
    except metadata.PackageNotFoundError:
        return UNKNOWN_VERSION
    # PEP 440 normalisation means the installed metadata spells a pre-release
    # ``0.1.0rc1`` where the file says ``0.1.0-rc.1``; both are that release,
    # so the pattern above is not applied here.
    return installed or UNKNOWN_VERSION
