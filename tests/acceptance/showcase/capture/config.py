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
#: The Quick Start stack's Redis (``--stack quickstart``): the fourth published
#: port, apart from the apps' 28403-28404.
DEFAULT_REDIS_PORT = 28405

# ---------------------------------------------------------------------------
# The stack a video is recorded on
# ---------------------------------------------------------------------------

#: ``quickstart``: the Quick Start's own ``docker compose up -d --wait`` in the
#: recorded tree (``showcase.capture.compose``), what a new user runs.
#: ``uvicorn``: the API under uvicorn and the static export, from the same tree
#: (``showcase.capture.stack``); kept for development, never rendered.
STACKS: Tuple[str, ...] = ("quickstart", "uvicorn")
#: The only stack a video is rendered from (EM C5 of T166).
FINAL_STACK = "quickstart"

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
#: The localStorage key an app keeps its visitor's id under, for the apps a
#: storyboard can film as a declared visitor (``off_camera: visitor``; T178
#: condition 5). ShopLab's (demo/shoplab/src/lib/visitor.ts, ``VISITOR_ID_KEY``);
#: StreamPulse names its users by device, and has none.
APP_VISITOR_KEY: Dict[str, str] = {"shoplab": "shoplab.visitor_id"}
#: An element each app always draws, for its origin's overlay self-test (C1).
APP_SELF_TEST: Dict[str, Dict[str, object]] = {
    "shoplab": {"role": "button", "name": "Powered by Controlvar", "exact": True},
    "streampulse": {
        "role": "heading",
        "name": "Powered by Controlvar",
        "exact": True,
    },
}
#: The element an app's frame must hold whole, by ``data-testid`` (A6 of the UX
#: draft): StreamPulse's phone (demo/streampulse/src/components/PhoneFrame.tsx),
#: status bar to tab bar. Checked at every cue's start and end and after every
#: step of the app's scenes, and once when the app is armed.
APP_FIT: Dict[str, str] = {"streampulse": "phone"}
#: Text an app's frame must show at each of its scenes' first cue, and the
#: file of the app it comes from (A5, plan v2 C1: two-sided, the false "every
#: 5 s" on ``never_show``): StreamPulse's panel says when it fetches. The run
#: reads the sentence from that file in the built tree, the recorded ref's,
#: and refuses before any scene if it is not there.
APP_REQUIRED_TEXT: Dict[str, Tuple[str, str]] = {
    "streampulse": (
        "fetched when you pick a device or press Refresh now",
        "demo/streampulse/src/components/ControlvarPanel.tsx",
    ),
}

# ---------------------------------------------------------------------------
# Video 4's incident, off camera (S4 of plan v2.2)
# ---------------------------------------------------------------------------

#: The StreamPulse demo's own rollout script in the recorded tree; the tool
#: runs its step 2 only (``off_camera: story: 2``), against the tool's stack.
ROLLOUT_STORY: Tuple[str, ...] = (
    "demo",
    "streampulse",
    "simulator",
    "rollout_story.py",
)
#: The flag the crash sender evaluates and reports crashes against.
CRASH_FLAG = "streampulse_player_v2"
#: A crashing device asks for the flag and crashes this many times: its first
#: launch and four relaunches, the StreamPulse simulator's crash loop
#: (demo/streampulse/simulator/traffic.py, ``INCIDENT_RELAUNCHES`` + 1).
CRASH_LOOP = 5
#: The Android 12 devices that crash, one loop each: every one is inside Player
#: v2's rollout at 25% and outside it at 5% (its bucket is 5 to 24 by the
#: server's own hash; a unit test pins each). The run refuses if the server
#: answers one of them off: a device that never got Player v2 cannot crash in it.
CRASH_DEVICES: Tuple[str, ...] = (
    "sp-a12-crash-02",
    "sp-a12-crash-14",
    "sp-a12-crash-16",
    "sp-a12-crash-20",
    "sp-a12-crash-21",
    "sp-a12-crash-26",
    "sp-a12-crash-27",
    "sp-a12-crash-36",
)
#: The crash a report names: the StreamPulse simulator's own
#: (demo/streampulse/simulator/traffic.py, ``CRASH_MESSAGE``).
CRASH_MESSAGE = "NullPointerException in PlayerV2Fragment"
#: The crash sender's pace, every request together: under the API's default
#: SDK limit of 100 a second, with the browser idle while it sends.
CRASH_REQUESTS_PER_SECOND = 40.0
#: Evaluations of the flag by the browser in the monitor's window, at most
#: (plan v2 C13): with 200 sent and 40 crashes, its error rate stays between
#: 40/300 and 40/200, well above the 5% the seed sets.
BROWSER_EVALUATIONS_MAX = 100
#: StreamPulse's device presets the video films, as devices.ts defines them and
#: the app sends them (``toUserContext``: the attributes, the id included); a
#: unit test keeps them equal. Gate 10/4 asks the API about the Galaxy S10 with
#: the same context the app sends.
STREAMPULSE_DEVICES: Dict[str, Dict[str, object]] = {
    "sp-galaxy-s10-us-premium": {
        "os": "Android",
        "os_version": "12.0.0",
        "app_version": "3.1.0",
        "region": "US",
        "tier": "premium",
        "employee": False,
        "device_model": "Galaxy S10",
        "device_id": "sp-galaxy-s10-us-premium",
    },
}
#: The safety scheduler's run history: the one completion that anchors B9 is a
#: run that first appears after the tool starts waiting (T165 C5).
SAFETY_SCHEDULER = "safety"
#: How often the tool looks for that run (each look is one request), and for the
#: rollback; both well under the API's 300 a minute for a path.
ANCHOR_POLL_S = 0.5
ROLLBACK_POLL_S = 1.0
#: The crash sends begin at most this long after that run completed (T165 C5).
SEND_AFTER_TICK_MAX_S = 5.0
#: The host's clock and the API's may disagree by at most this much: the B9
#: gate compares the host's frame times with the API's rollback time.
CLOCK_SKEW_MAX_S = 1.0
#: B9's fit (T165 C5): from the first crash sent to B9's last frame, at most
#: the safety interval less this much.
B9_SPARE_S = 15
#: For the fit, what the capture spends off camera that the estimate does not
#: count: a card drawn, a dashboard page loaded and settled, a scene's drain.
CARD_OVERHEAD_MS = 600
PAGE_OVERHEAD_MS = 5_000

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
#: Compose's own label on what a project made: containers, volumes, networks.
COMPOSE_PROJECT_LABEL = "com.docker.compose.project"
#: The images' own label (backend/Dockerfile, frontend/Dockerfile): core or full.
IMAGE_PROFILE_LABEL = "io.experimently.profile"
POSTGRES_IMAGE = "postgres:16-alpine"
#: The Playwright the tool was written against (tests/acceptance/requirements.txt).
PLAYWRIGHT_VERSION = "1.63.0"
#: The release recorded when --ref is not given (make showcase without REF):
#: v0.36.0, the release the launch videos are recorded on (T174). This names
#: what is recorded; a run on that release is what shows the storyboards pass.
DEFAULT_REF = "v0.36.0"

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
#: password, then docker-compose.yml's two demo API keys and its SECRET_KEY
#: default, which ``contract.PUBLIC_VALUES`` holds in two parts (the render's
#: OCR scan looks for those three only through their random part).
STATIC_NEEDLES: Tuple[str, ...] = (
    "Demo1234!",
    *(readable + random_part for readable, random_part in contract.PUBLIC_VALUES),
)
#: The compose demo key each app's bundle is built with, and its seed registers.
APP_STATIC_KEY: Dict[str, str] = {
    "shoplab": STATIC_NEEDLES[1],
    "streampulse": STATIC_NEEDLES[2],
}
#: The demo seed's administrator, whom every video signs in as.
DEMO_ADMIN = ("admin@demo.com", "Demo1234!")

# ---------------------------------------------------------------------------
# The invite dialog's one-time password (C4 of T149, S6 of plan v2.2)
# ---------------------------------------------------------------------------

#: The element that draws it (InviteUserModal.tsx), the one value a storyboard
#: may mask (``storyboard.MaskedValue``; a unit test keeps the three equal).
INVITE_PASSWORD_TESTID = "invite-temp-password"
#: The dashboard's invite: ``POST /api/v1/users/`` with the password the browser
#: made up in its JSON body (``AdminService.createUser``); the answer never holds it.
INVITE_PATH = "/api/v1/users"
#: What the mask draws over the element instead: a solid block (never a blur),
#: in the UX draft's colour, with these words.
MASK_TEXT = "hidden for this video"
MASK_FILL = "#E2E8F0"
MASK_INK = "#334155"

#: The SDK paths' rate limit on the uvicorn stack's API. Video 2's traffic sends
#: about 180 users a second through them unpaced (measured on a local stack),
#: and the default 6,000 a minute would answer 429 from the 101st in a second.
#: The Quick Start stack keeps the default (EM C8 of T166): its traffic is
#: paced under it instead (``TRAFFIC_REQUESTS_PER_SECOND``).
SDK_RATE_LIMIT_PER_MINUTE = 60_000
#: The API's default SDK limit (``SDK_RATE_LIMIT_PER_MINUTE`` in the backend's
#: settings), which the Quick Start stack runs with.
DEFAULT_SDK_RATE_LIMIT_PER_MINUTE = 6_000
#: The Quick Start stack's traffic pace: SDK requests a second, every worker
#: together, under the default limit's 100 a second.
TRAFFIC_REQUESTS_PER_SECOND = 90.0

#: The videos, by slug, in the order ``all`` records them: video 4 last, the
#: longest take (its incident waits for the safety monitor).
VIDEOS: Dict[str, str] = {
    "01-getting-started": "storyboards/01-getting-started.yaml",
    "02-first-experiment": "storyboards/02-first-experiment.yaml",
    "03-flag-rollout": "storyboards/03-flag-rollout.yaml",
    "05-plan-and-team": "storyboards/05-plan-and-team.yaml",
    "06-shoplab-ab-test": "storyboards/06-shoplab-ab-test.yaml",
    "04-streampulse-rollout": "storyboards/04-streampulse-rollout.yaml",
}
#: Storyboards that prove the tool and are never a video: capture only, never
#: rendered, and not part of ``all``.
PROBES: Dict[str, str] = {
    "90-app-probe": "storyboards/90-app-probe.yaml",
}
