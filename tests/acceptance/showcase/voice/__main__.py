"""``python -m showcase.voice <slug|all> --voice Ruth`` from ``tests/acceptance`` (``make showcase-voice``)."""

from __future__ import annotations

import sys

from showcase.voice.step import main

sys.exit(main())
