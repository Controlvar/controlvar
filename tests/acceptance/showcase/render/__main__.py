"""``python -m showcase.render CAPTURE_DIR --out DIR [--work DIR]``.

Exit status: 0 when every gate passed and the files are in place; 1 when a
gate refused or the render failed (the reason is printed, and nothing the
render made is left; needles.txt is gone too, so the capture has to be made
again); 2 for a usage error.
"""

from __future__ import annotations

import argparse
import os
import signal
import sys
from pathlib import Path
from typing import List, Optional

from showcase.render.children import CHILD_ENV_KEYS
from showcase.render.gates import Refused
from showcase.render.pipeline import RECAPTURE, render


def _stop(signum, frame):
    # A terminated render cleans up like a refused one: its finally blocks run.
    raise SystemExit(128 + signum)


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m showcase.render",
        description="Render a showcase capture directory into a captioned MP4, a WebVTT file and a review page.",
    )
    parser.add_argument("capture_dir", type=Path)
    parser.add_argument(
        "--out",
        type=Path,
        required=True,
        help="where the finished files go (never a repository)",
    )
    parser.add_argument(
        "--work",
        type=Path,
        default=None,
        help=(
            "an empty scratch directory, deleted when the render ends; not the "
            "output directory, inside it or around it (default: <capture>/../render)"
        ),
    )
    args = parser.parse_args(argv)
    # Nothing from the caller's shell reaches anything this process starts,
    # Playwright's own driver included (EM condition 10).
    for key in list(os.environ):
        if key not in CHILD_ENV_KEYS:
            del os.environ[key]
    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGHUP, _stop)
    try:
        result = render(args.capture_dir, args.out, args.work)
    except Refused as exc:
        sys.stderr.write(f"REFUSED {exc}\n")
        return 1
    except Exception as exc:  # anything else is a failure too, never a pass
        sys.stderr.write(f"FAILED {exc.__class__.__name__}: {exc}\n{RECAPTURE}\n")
        return 1
    sys.stdout.write(
        f"OK {result.mp4}\n   sha256 {result.mp4_sha256}\n"
        f"OK {result.vtt}\n   sha256 {result.vtt_sha256}\n"
    )
    if result.srt is not None and result.transcript is not None:
        sys.stdout.write(
            f"OK {result.srt}\n   sha256 {result.srt_sha256}\n"
            f"OK {result.transcript}\n   sha256 {result.transcript_sha256}\n"
        )
    sys.stdout.write(f"OK {result.review / 'index.html'}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
