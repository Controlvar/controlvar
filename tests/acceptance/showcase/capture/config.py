"""The capture side's fixed numbers, in one place.

What the render side must agree on (the page area, the band, the frame rate,
the reading-time rule, the disk floor) is ``showcase.contract``'s; nothing
here repeats it.
"""

from __future__ import annotations

from typing import Dict, Tuple

from showcase import contract

# ---------------------------------------------------------------------------
# Ports
# ---------------------------------------------------------------------------

#: A developer's own stack: the dashboard (3000, 3100), the demo apps ShopLab
#: (3200) and StreamPulse (3300), the API (8000), Postgres (5432, 5433) and
#: Redis (6379). Nothing the tool starts, and nothing the browser it drives
#: asks for, may use one of these.
RESERVED_PORTS: Tuple[int, ...] = (3000, 3100, 3200, 3300, 8000, 5432, 5433, 6379)
#: The tool's own defaults, apart from the docs journeys' 28000/23000/25432/26379.
DEFAULT_API_PORT = 28400
DEFAULT_DASHBOARD_PORT = 28401
DEFAULT_POSTGRES_PORT = 28402
DEFAULT_SHOPLAB_PORT = 28403
DEFAULT_STREAMPULSE_PORT = 28404

# ---------------------------------------------------------------------------
# The demo apps (demo/<app>), built from the recorded ref like the dashboard
# ---------------------------------------------------------------------------

#: The apps a scene may be recorded in (``Scene.app``); "dashboard" is the default.
APPS: Tuple[str, ...] = ("shoplab", "streampulse")
#: Each app's experiment and flag keys start with this (the seeds' own keys).
APP_KEY_PREFIX: Dict[str, str] = {
    "shoplab": "shoplab_",
    "streampulse": "streampulse_",
}
#: The seed that makes each app's experiments and flags, and the variable that
#: points its key files at the tool's own directory (never the checkout's demo/).
APP_SEED: Dict[str, Tuple[str, str]] = {
    "shoplab": ("backend.scripts.seed_shoplab", "SHOPLAB_DEMO_DIR"),
    "streampulse": ("backend.scripts.seed_streampulse", "STREAMPULSE_DEMO_DIR"),
}
#: The StreamPulse seed's server key file (``sdk:ruleset``): read, stripped,
#: kept as a needle; never put on an argv or in an environment.
SERVER_KEY_FILE = ".api_key_server"
#: An element each app always draws, for its origin's overlay self-test (C1).
APP_SELF_TEST: Dict[str, Dict[str, object]] = {
    "shoplab": {"role": "button", "name": "Powered by Controlvar", "exact": True},
    "streampulse": {
        "role": "heading",
        "name": "Powered by Controlvar",
        "exact": True,
    },
}

# ---------------------------------------------------------------------------
# One run per machine
# ---------------------------------------------------------------------------

#: Under ``HOME``: outside every work directory and every git checkout (the
#: tool refuses a lock path inside one). Held from before the capture until
#: the render ends.
MACHINE_LOCK = ("Library", "Caches", "experimently-showcase", "showcase.lock")

# ---------------------------------------------------------------------------
# The machine
# ---------------------------------------------------------------------------

#: The label on every container the tool starts; it removes no other.
CONTAINER_LABEL = "com.experimently.showcase"
POSTGRES_IMAGE = "postgres:16-alpine"
#: The Playwright the tool was written against (tests/acceptance/requirements.txt).
PLAYWRIGHT_VERSION = "1.63.0"
#: The release the storyboards were last validated against: the default --ref.
#: Empty until one is: video 1 needs the one-line header (#1069), which no
#: release carried when it was written, so --ref is required until then.
DEFAULT_REF = ""

# ---------------------------------------------------------------------------
# The frame
# ---------------------------------------------------------------------------

#: The browser viewport: the page area above the caption band, at device scale
#: factor 1, so a screencast frame is exactly this size and nothing is upscaled.
VIEWPORT = {"width": contract.PAGE_W, "height": contract.PAGE_H}
#: CSS zoom on <body> (never <html>: Playwright's overlay layer is a child of
#: <html>, so a zoom there would scale the cursor and ring a second time). The
#: layout is 1440 CSS px wide, readable on a phone.
ZOOM = 4 / 3
#: The zoom on each origin's <body>. StreamPulse draws a 390 x 780 phone; at 4/3
#: it is taller than the page area, so it gets its own.
ORIGIN_ZOOM: Dict[str, float] = {
    "dashboard": ZOOM,
    "shoplab": ZOOM,
    "streampulse": 1.07,
}
#: The overlay alignment self-test: the ring must land within this many pixels
#: of where the element is in the frame.
OVERLAY_TOLERANCE_PX = 2
JPEG_QUALITY = 95

# ---------------------------------------------------------------------------
# Pace (the storyboard's global rules; the reading time is contract.py's)
# ---------------------------------------------------------------------------

#: The gap between two cues, and the margin a cue is held past its reading
#: time so that rounding to frames never shortens it.
CUE_GAP_MS = 200
CUE_MARGIN_MS = 100
#: Pointer travel and the rest before a click; typing speed; the pause after a
#: field; the hold after a state change; a payoff hold; smooth scrolling.
POINTER_MS = 500
POINTER_REST_MS = 300
CLICK_RING_MS = 300
TYPE_DELAY_MS = 70
FIELD_PAUSE_MS = 400
SETTLE_MS = 1000
#: After a wait on camera: the page has its element; let it paint.
PAINT_MS = 300
PAYOFF_MS = 2500
SCROLL_MS = 750
#: A slider's thumb travels for this long, eased, whatever the distance.
SLIDE_MS = 1200
#: Chromium's range thumb is about this wide (CSS px, before the zoom): the
#: drawn cursor rides its centre.
RANGE_THUMB_CSS_PX = 16
#: Freeze (gate 11): no new frame for longer than the longest hold plus this.
FREEZE_SLACK_MS = 1000
#: Black (gate 11): a frame this dark (mean luma 0-255) for this long.
BLACK_LUMA = 16
BLACK_MS = 500

# ---------------------------------------------------------------------------
# Colours
# ---------------------------------------------------------------------------

#: The highlight ring: 5:1 on white, 4 px, 4 px off the element.
RING_COLOUR = "#B45309"
RING_WIDTH_PX = 4
RING_OFFSET_PX = 4
#: Drawn only by the overlay self-test, off camera: a colour no dashboard page uses.
SELF_TEST_RGB = (0, 255, 0)
#: Magenta: the docs runner's masks. No captured frame may hold a block of it.
FORBIDDEN_RGB = (255, 0, 255)

# ---------------------------------------------------------------------------
# Never on screen (gate U4), checked in the page's text at every cue start.
# The tool's own ports are added at run time.
# ---------------------------------------------------------------------------

FORBIDDEN_TEXT: Tuple[str, ...] = (
    "localhost:",
    "127.0.0.1",
    "Docs journey",
    "Disconnected",
    "Connecting…",
    "Something went wrong",
    "not listed here yet",
)

# ---------------------------------------------------------------------------
# Credentials (gate 6; needles.txt for the render side's OCR scan)
# ---------------------------------------------------------------------------

#: The values every stack carries and no frame may show: the demo accounts'
#: password, docker-compose.yml's two demo API keys and its SECRET_KEY default.
STATIC_NEEDLES: Tuple[str, ...] = (
    "Demo1234!",
    "eptk_demo_shoplab_0123456789abcdef0123456789abcdef",
    "eptk_demo_streampulse_0123456789abcdef0123456789ab",
    "dev-only-change-me-9f1c7b2e4a6d8e0f1a2b3c4d5e6f7a8b",
)
#: The compose demo key each app's bundle is built with, and its seed registers.
APP_STATIC_KEY: Dict[str, str] = {
    "shoplab": STATIC_NEEDLES[1],
    "streampulse": STATIC_NEEDLES[2],
}
#: The demo seed's administrator, whom every video signs in as.
DEMO_ADMIN = ("admin@demo.com", "Demo1234!")

#: The SDK paths' rate limit on the tool's API. Video 2's traffic sends about
#: 180 users a second through them (measured on a local stack), and the
#: default 6,000 a minute would answer 429 from the 101st in a second.
SDK_RATE_LIMIT_PER_MINUTE = 60_000

#: The videos, by slug, in the order ``all`` records them.
VIDEOS: Dict[str, str] = {
    "01-getting-started": "storyboards/01-getting-started.yaml",
    "02-first-experiment": "storyboards/02-first-experiment.yaml",
    "03-flag-rollout": "storyboards/03-flag-rollout.yaml",
}
#: Storyboards that prove the tool and are never a video: capture only, never
#: rendered, and not part of ``all``.
PROBES: Dict[str, str] = {
    "90-app-probe": "storyboards/90-app-probe.yaml",
}
