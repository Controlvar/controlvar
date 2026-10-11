"""Drives the browser through a storyboard and records what it draws.

The only module here that uses Playwright. It imports it only where it runs
(and its types only for type checking), so its checks are unit tested with
stand-in pages where Playwright is not installed. What it does, in order:

* One headless Chromium context at ``config.VIEWPORT``, device scale factor 1.
  Before the first navigation: ``context.route`` and ``context.route_web_socket``
  abort any request to a reserved port, and any loopback request to a port
  that is not the tool's dashboard or API (gate 15c); an init script puts the
  CSS zoom on <body> (``config.ZOOM``) as soon as <head> exists, and
  ``measure_zoom`` reads back what Chromium computed before any scene (C1).
* Every JSON response and every JSON request body the page sends or receives
  goes through ``redaction.register_credentials``: a token the page is handed
  is a needle from then on.
* ``self_test_overlay``: before any scene, a ring drawn around a known element
  must land within ``OVERLAY_TOLERANCE_PX`` of the element in a real frame
  (Playwright's overlay layer is a child of <html>, outside the zoom; a zoom on
  <html> moves the ring 4/3 away, and this refuses).
* A scene is one ``page.screencast`` (JPEG q95, at the viewport size) from
  ``start`` to ``stop``. Frames arrive only when the page changes; each is
  written to ``frames/`` with its time on the recording clock, which counts
  only recorded time, so off-camera work between scenes is not in the video.
  A frame replaced at the same millisecond is deleted through
  ``WorkRoot.remove``; a gate-6 refusal deletes them all (``run.drop_recording``).
* The cursor and the highlight ring are the tool's own overlays, drawn from
  coordinates only; ``screencast.show_actions`` is never used (it draws a
  typed value, a password's included, as text).
* At every cue start: the page's text and fields are read (``DRAWN_JS``) and
  no needle may be drawn (gate 6); no forbidden text in the viewport (U4); the
  header's links are one line each and its user chip is not cut off (U1); the
  focus is inside the page area and on top at its centre (gate 9); every
  number the caption states is in the focus's text (U2); no Next.js error
  overlay and no unexpected role=alert (gate 11). After every step the needle
  check runs again; at every cue end the focus check does.
* A scene marked ``signed_in`` signs the demo admin in off camera first, through
  the login page; a ``results_of`` scene opens that experiment's results page,
  its id looked up off camera.
* While recording: any response >= 500 from the stack, any page error and any
  request the guard refused fails the video (gate 11, gate 15c).

A failure raises ``CaptureFailed`` naming the scene, the beat and the gate.
"""

from __future__ import annotations

import math
import re
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Dict, List, Optional, Tuple
from urllib.parse import urlparse

import numpy as np

from showcase.capture import config, guard, storyboard
from showcase.capture import gates as judge

if TYPE_CHECKING:
    from playwright.sync_api import (
        BrowserContext,
        Locator,
        Page,
        Route,
        WebSocketRoute,
    )
try:
    from playwright.sync_api import Error as PlaywrightError
except ImportError:  # the CI unit job, which drives no browser

    class PlaywrightError(Exception):  # type: ignore[no-redef]
        """Stands in for Playwright's error where Playwright is not installed."""


from showcase.capture.storyboard import Beat, Scene

_DOCS = Path(__file__).resolve().parents[2] / "docs"
if str(_DOCS) not in sys.path:
    sys.path.insert(0, str(_DOCS))
from docs_runner import redaction  # noqa: E402


class CaptureFailed(RuntimeError):
    def __init__(self, gate: str, message: str):
        super().__init__(f"{gate}: {message}")
        self.gate = gate


#: The zoom, on <body>, as soon as <head> exists. ``{target}`` is ``body``;
#: the overlay self-test's tamper puts it on ``html``.
ZOOM_INIT = """(() => {
  const css = '%(target)s{zoom:%(zoom).7f}';
  const add = () => {
    if (document.getElementById('showcase-zoom')) return true;
    if (!document.head) return false;
    const style = document.createElement('style');
    style.id = 'showcase-zoom';
    style.textContent = css;
    document.head.appendChild(style);
    return true;
  };
  if (!add()) {
    const watch = new MutationObserver(() => { if (add()) watch.disconnect(); });
    watch.observe(document, {childList: true, subtree: true});
  }
})()"""
ZOOM_TARGET = "body"
#: What zoom Chromium computed for <html> and <body>.
ZOOM_JS = """() => ({
  html: parseFloat(getComputedStyle(document.documentElement).zoom),
  body: parseFloat(getComputedStyle(document.body).zoom),
})"""

#: U1: each link and button in the page's <header> is one line, and no element
#: of it is cut off (scrollWidth over clientWidth: the user chip's ellipsis).
HEADER_JS = """() => {
  const header = document.querySelector('header');
  if (!header) return {links: 0, wrapped: [], cut: []};
  const wrapped = [];
  const cut = [];
  const items = header.querySelectorAll('a, button, summary');
  for (const el of items) {
    const box = el.getBoundingClientRect();
    if (box.width === 0 || box.height === 0) continue;
    // Lines of text: the rects of its text nodes, grouped where they overlap
    // vertically (a badge beside a word is one line; a wrapped label is two).
    const rects = [];
    const walker = document.createTreeWalker(el, NodeFilter.SHOW_TEXT);
    let node;
    while ((node = walker.nextNode())) {
      if (!node.textContent.trim()) continue;
      const range = document.createRange();
      range.selectNodeContents(node);
      for (const r of range.getClientRects()) {
        if (r.width > 0 && r.height > 0) rects.push([r.top, r.bottom]);
      }
    }
    rects.sort((a, b) => a[0] - b[0]);
    let lines = 0, bottom = -Infinity;
    for (const [top, low] of rects) {
      if (top >= bottom - 2) { lines += 1; bottom = low; } else { bottom = Math.max(bottom, low); }
    }
    if (lines > 1) wrapped.push((el.innerText || '').trim().replace(/\\s+/g, ' '));
  }
  for (const el of header.querySelectorAll('*')) {
    const box = el.getBoundingClientRect();
    if (box.width === 0 || box.height === 0) continue;
    const style = getComputedStyle(el);
    if (style.overflow === 'visible' && style.textOverflow !== 'ellipsis') continue;
    if (el.scrollWidth > el.clientWidth + 1) cut.push((el.innerText || '').trim());
  }
  return {links: items.length, wrapped, cut};
}"""

#: U4: the text drawn inside the viewport (a text node with a box on screen).
VIEWPORT_TEXT_JS = """() => {
  const out = [];
  const w = innerWidth, h = innerHeight;
  const walker = document.createTreeWalker(document.body || document, NodeFilter.SHOW_TEXT);
  let node;
  while ((node = walker.nextNode())) {
    const text = node.textContent;
    if (!text || !text.trim()) continue;
    const range = document.createRange();
    range.selectNodeContents(node);
    for (const r of range.getClientRects()) {
      if (r.right > 0 && r.bottom > 0 && r.left < w && r.top < h && r.width > 0) {
        out.push(text);
        break;
      }
    }
  }
  for (const el of document.querySelectorAll('input, textarea')) {
    const r = el.getBoundingClientRect();
    if (r.right > 0 && r.bottom > 0 && r.left < w && r.top < h && el.type !== 'password') {
      out.push(String(el.value || ''), String(el.placeholder || ''));
    }
  }
  return out.join('\\n');
}"""

#: Gate 11: what a broken page draws.
BROKEN_JS = """() => {
  const alerts = [];
  for (const el of document.querySelectorAll('[role=alert]')) {
    const r = el.getBoundingClientRect();
    const text = (el.innerText || '').trim();
    // Drawn only: a clipped 1 px region (Next.js's route announcer, 1.3 px
    // under the zoom) is for screen readers and shows nothing.
    const clipped = style => style.clip.startsWith('rect(0') || style.clipPath === 'inset(50%)';
    if (r.width > 4 && r.height > 4 && !clipped(getComputedStyle(el)) && text) {
      alerts.push(text.slice(0, 120));
    }
  }
  return {overlay: !!document.querySelector('nextjs-portal'), alerts};
}"""

#: Gate 9: is the element on top at this point (or something inside it)?
ON_TOP_JS = """(el, point) => {
  const top = document.elementFromPoint(point[0], point[1]);
  return !!top && (top === el || el.contains(top));
}"""

#: The drawn cursor: a 28 px black arrow with a white outline, tip at (3, 2).
#: Plain HTML and CSS: an overlay does not draw SVG (measured).
CURSOR_POLYGON = "polygon(3px 2px, 3px 23px, 9px 17.5px, 13px 26px, 17px 24.2px, 13px 15.8px, 21px 15.8px)"
CURSOR_OUTLINE = (
    "drop-shadow(1.5px 0 0 #fff) drop-shadow(-1.5px 0 0 #fff) drop-shadow(0 1.5px 0 #fff)"
    " drop-shadow(0 -1.5px 0 #fff) drop-shadow(0 1px 2px rgba(0,0,0,.45))"
)


def _now_ms() -> float:
    return time.time() * 1000.0


def ease(t: float) -> float:
    """Ease in and out over 0..1."""
    return 0.5 - 0.5 * math.cos(math.pi * max(0.0, min(1.0, t)))


@dataclass
class Box:
    x: float
    y: float
    w: float
    h: float

    @property
    def centre(self) -> Tuple[float, float]:
        return (self.x + self.w / 2, self.y + self.h / 2)

    def as_dict(self) -> Dict[str, int]:
        return {
            "x": int(round(self.x)),
            "y": int(round(self.y)),
            "w": int(round(self.w)),
            "h": int(round(self.h)),
        }


@dataclass
class Tally:
    """What every check did, for the manifest: counts, never values."""

    needle_checks: int = 0
    header_checks: int = 0
    #: U2: cues whose numbers were looked for in the focus, and how many said any.
    number_checks: int = 0
    numbered_cues: int = 0
    forbidden_checks: int = 0
    focus_checks: int = 0
    broken_checks: int = 0
    responses: int = 0
    server_errors: List[str] = field(default_factory=list)
    page_errors: List[str] = field(default_factory=list)
    refused: List[str] = field(default_factory=list)
    #: JSON answers from the stack whose body could not be read (gate 6).
    unread: List[str] = field(default_factory=list)
    frames_bad_size: int = 0
    payoffs: List[Dict[str, Any]] = field(default_factory=list)
    scenes_run: List[str] = field(default_factory=list)
    focus_boxes: List[Dict[str, Any]] = field(default_factory=list)
    holds_ms: List[int] = field(default_factory=list)


def decode_rgb(jpeg: bytes, width: int, height: int) -> np.ndarray:
    """A JPEG as an (h, w, 3) array, through ffmpeg (no imaging library needed)."""
    done = subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-i",
            "-",
            "-f",
            "rawvideo",
            "-pix_fmt",
            "rgb24",
            "-",
        ],
        input=jpeg,
        capture_output=True,
        check=False,
        env=guard.tool_env(),
    )
    if done.returncode != 0 or len(done.stdout) != width * height * 3:
        raise CaptureFailed("gate 3", f"a frame is not {width}x{height}")
    return np.frombuffer(done.stdout, np.uint8).reshape(height, width, 3)


def colour_box(
    image: np.ndarray, rgb: Tuple[int, int, int], tol: int = 70
) -> Optional[Box]:
    """The bounding box of the pixels close to *rgb*, or None."""
    diff = np.abs(image.astype(int) - np.array(rgb)).max(axis=2)
    ys, xs = np.nonzero(diff < tol)
    if len(xs) == 0:
        return None
    return Box(
        float(xs.min()),
        float(ys.min()),
        float(xs.max() + 1 - xs.min()),
        float(ys.max() + 1 - ys.min()),
    )


class Recorder:
    """The recording clock and the frames on it."""

    def __init__(self, page: Page, frames: Path, tally: Tally, work: guard.WorkRoot):
        self.page = page
        self.dir = frames
        self.work = work
        self.dir.mkdir(parents=True, exist_ok=True)
        self.tally = tally
        self.offset_ms = 0.0
        self.t0: Optional[float] = None
        self.frames: List[Tuple[int, Path]] = []
        self._n = 0

    @property
    def recording(self) -> bool:
        return self.t0 is not None

    def now(self) -> int:
        if self.t0 is None:
            return int(round(self.offset_ms))
        return int(round(self.offset_ms + _now_ms() - self.t0))

    def _on_frame(self, frame: Dict[str, Any]) -> None:
        if self.t0 is None:
            return
        if (frame.get("viewportWidth"), frame.get("viewportHeight")) != (
            config.VIEWPORT["width"],
            config.VIEWPORT["height"],
        ):
            self.tally.frames_bad_size += 1
        t = int(round(self.offset_ms + max(0.0, float(frame["timestamp"]) - self.t0)))
        self._n += 1
        path = self.dir / f"raw-{self._n:06d}.jpg"
        path.write_bytes(frame["data"])
        self.frames.append((t, path))

    def start(self) -> None:
        self.t0 = _now_ms()
        self.page.screencast.start(
            on_frame=self._on_frame,
            size=dict(config.VIEWPORT),
            quality=config.JPEG_QUALITY,
        )

    def stop(self) -> None:
        if self.t0 is None:
            return
        self.page.screencast.stop()
        self.offset_ms += _now_ms() - self.t0
        self.t0 = None

    def finish(self) -> List[Dict[str, Any]]:
        """Number the frames in time order; one frame per millisecond (the last wins)."""
        by_time: Dict[int, Path] = {}
        for t, path in sorted(self.frames, key=lambda item: item[0]):
            if t in by_time:
                self.work.remove(by_time[t])
            by_time[t] = path
        entries = []
        for index, t in enumerate(sorted(by_time), start=1):
            name = f"{index:06d}.jpg"
            by_time[t].rename(self.dir / name)
            entries.append({"file": name, "t_ms": t})
        return entries


class Director:
    def __init__(
        self,
        *,
        context: BrowserContext,
        page: Page,
        frames: Path,
        dashboard: str,
        api_port: int,
        dashboard_port: int,
        redactor: redaction.Redactor,
        forbidden: Tuple[str, ...],
        work: guard.WorkRoot,
        resolve: Optional[Callable[[str], str]] = None,
    ):
        self.context = context
        self.page = page
        self.dashboard = dashboard.rstrip("/")
        self.allowed_ports = {api_port, dashboard_port}
        self.redactor = redactor
        self.forbidden = forbidden
        self.tally = Tally()
        self.recorder = Recorder(page, frames, self.tally, work)
        self.cues: List[Dict[str, Any]] = []
        self.cursor = (
            config.VIEWPORT["width"] * 0.62,
            config.VIEWPORT["height"] * 0.55,
        )
        self._cursor_overlay: Any = None
        self._ring_overlay: Any = None
        self.where = "setup"
        self.beat_alerts: List[str] = []
        #: Gate 10: the experiments list's rows, read at a cue on that page.
        self.listed_rows: Optional[int] = None
        #: Card scenes: a still page is their point (gate 11's freeze and black).
        self.still_spans: List[Tuple[int, int]] = []
        #: An experiment's id by its name, for ``results_of`` scenes (off camera).
        self.resolve = resolve
        #: Texts a beat's ``record`` kept at its cue's start, by name (gate 10).
        self.readings: Dict[str, str] = {}
        self.signed_in = False

    # -- the guard and the listeners ------------------------------------------

    def refused_port(self, url: str) -> Optional[str]:
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https", "ws", "wss"):
            return None
        port = parsed.port or (443 if parsed.scheme in ("https", "wss") else 80)
        host = (parsed.hostname or "").lower()
        loopback = host in (
            "localhost",
            "127.0.0.1",
            "::1",
            "0.0.0.0",
        ) or host.endswith(".localhost")
        if port in config.RESERVED_PORTS:
            return f"{host}:{port}"
        if loopback and port not in self.allowed_ports:
            return f"{host}:{port}"
        return None

    def install(self) -> None:
        """Route guard and zoom, before the first navigation; then the listeners."""

        def http(route: Route) -> None:
            refused = self.refused_port(route.request.url)
            if refused:
                self.tally.refused.append(f"request to {refused} ({self.where})")
                route.abort("blockedbyclient")
            else:
                route.continue_()

        def socket(ws: WebSocketRoute) -> None:
            refused = self.refused_port(ws.url)
            if refused:
                # Never connected to a server: the page's socket goes nowhere.
                # (Closing it from inside this handler hangs Playwright 1.63's
                # sync API, measured; the refusal fails the video anyway.)
                self.tally.refused.append(f"WebSocket to {refused} ({self.where})")
            else:
                ws.connect_to_server()

        self.context.route("**/*", http)
        self.context.route_web_socket(re.compile(".*"), socket)
        self.context.add_init_script(
            ZOOM_INIT % {"target": ZOOM_TARGET, "zoom": config.ZOOM}
        )
        self.page.on("response", self._on_response)
        self.page.on("request", self._on_request)
        self.page.on(
            "pageerror",
            lambda error: self.tally.page_errors.append(
                f"{self.where}: {str(error)[:200]}"
            ),
        )

    def _on_response(self, response: Any) -> None:
        self.tally.responses += 1
        if response.url.startswith(self.dashboard) and response.status >= 500:
            path = urlparse(response.url).path
            self.tally.server_errors.append(
                f"{response.status} from {path} ({self.where})"
            )
        kind = (response.headers or {}).get("content-type", "")
        if "json" not in kind:
            return
        try:
            body = response.json()
        except Exception:  # recorded, and gate 6 then refuses
            # Fail closed: a JSON answer the tool could not read may have held
            # a credential it therefore never registered.
            if response.url.startswith(self.dashboard):
                self.tally.unread.append(
                    f"{urlparse(response.url).path} ({self.where})"
                )
            return
        redaction.register_credentials(body, self.redactor)

    def _on_request(self, request: Any) -> None:
        try:
            body = request.post_data_json
        except Exception:  # a body that is not JSON: the dashboard sends none
            return
        if body is not None:
            redaction.register_credentials(body, self.redactor)

    # -- locators and boxes -----------------------------------------------------

    def locate(self, spec: storyboard.Locator) -> Locator:
        page = self.page
        if spec.role:
            found = (
                page.get_by_role(spec.role, name=spec.name, exact=spec.exact)
                if spec.name
                else page.get_by_role(spec.role)
            )
        elif spec.text:
            found = page.get_by_text(spec.text, exact=spec.exact)
        elif spec.label:
            found = page.get_by_label(spec.label, exact=spec.exact)
        elif spec.testid:
            found = page.get_by_test_id(spec.testid)
        else:
            found = page.locator(spec.css)
        return found.nth(spec.nth)

    def box(
        self, spec: storyboard.Locator, *, timeout: float = 10000
    ) -> Tuple[Locator, Box]:
        found = self.locate(spec)
        try:
            found.wait_for(state="visible", timeout=timeout)
            raw = found.bounding_box()
        except PlaywrightError as error:
            # A 5xx or a page error is the likelier cause: say that first.
            self.check_events()
            raise CaptureFailed(
                "gate 12",
                f"{self.where}: {spec.describe()} not found ({str(error).splitlines()[0]})",
            ) from None
        if raw is None:
            raise CaptureFailed(
                "gate 12", f"{self.where}: {spec.describe()} has no box"
            )
        return found, Box(raw["x"], raw["y"], raw["width"], raw["height"])

    # -- overlays -----------------------------------------------------------------

    def _show(self, html: str) -> Any:
        return self.page.screencast.show_overlay(html)

    @staticmethod
    def _dispose(handle: Any) -> None:
        if handle is not None:
            try:
                handle.__exit__(None, None, None)
            except PlaywrightError:
                pass

    def _cursor_html(self, x: float, y: float) -> str:
        return (
            f'<div style="position:fixed;left:{x - 3:.1f}px;top:{y - 2:.1f}px;'
            f'width:28px;height:28px;pointer-events:none;filter:{CURSOR_OUTLINE}">'
            f'<div style="width:28px;height:28px;background:#000;'
            f'clip-path:{CURSOR_POLYGON}"></div></div>'
        )

    def draw_cursor(self) -> None:
        new = self._show(self._cursor_html(*self.cursor))
        self._dispose(self._cursor_overlay)
        self._cursor_overlay = new

    def move_to(self, x: float, y: float, ms: int = config.POINTER_MS) -> None:
        """Glide the drawn cursor and the real mouse to (x, y), eased."""
        start = self.cursor
        began = _now_ms()
        while True:
            t = min(1.0, (_now_ms() - began) / ms)
            k = ease(t)
            point = (start[0] + (x - start[0]) * k, start[1] + (y - start[1]) * k)
            self.cursor = point
            self.page.mouse.move(point[0], point[1])
            self.draw_cursor()
            if t >= 1.0:
                break
            self.page.wait_for_timeout(15)

    def ring(self, box: Optional[Box], colour: str = config.RING_COLOUR) -> None:
        """One ring at a time: draw the new one, then remove the old."""
        new = None
        if box is not None:
            o = config.RING_OFFSET_PX + config.RING_WIDTH_PX
            new = self._show(
                f'<div style="position:fixed;box-sizing:border-box;left:{box.x - o:.1f}px;'
                f"top:{box.y - o:.1f}px;width:{box.w + 2 * o:.1f}px;height:{box.h + 2 * o:.1f}px;"
                f"border:{config.RING_WIDTH_PX}px solid {colour};border-radius:8px;"
                f'pointer-events:none"></div>'
            )
        self._dispose(self._ring_overlay)
        self._ring_overlay = new

    def click_pulse(self, x: float, y: float) -> None:
        handle = self._show(
            f'<div style="position:fixed;box-sizing:border-box;left:{x - 18:.1f}px;top:{y - 18:.1f}px;'
            f"width:36px;height:36px;border-radius:50%;border:3px solid {config.RING_COLOUR};"
            f'pointer-events:none"></div>'
        )
        self.page.wait_for_timeout(config.CLICK_RING_MS)
        self._dispose(handle)

    # -- C1: the zoom, and the overlay alignment self-test ---------------------------

    def measure_zoom(self) -> Dict[str, Any]:
        """The zoom the page has, as Chromium computes it; refused unless it is ``config.ZOOM`` on <body>."""
        measured = self.page.evaluate(ZOOM_JS)
        ok, numbers, why = judge.zoom(measured)
        if not ok:
            raise CaptureFailed("C1 zoom", why)
        return numbers

    def self_test_overlay(self, spec: storyboard.Locator) -> Dict[str, Any]:
        """Draw a ring of ``SELF_TEST_RGB`` exactly on an element; find it in a frame."""
        _, box = self.box(spec)
        got: List[bytes] = []

        def take(frame: Dict[str, Any]) -> None:
            got.append(frame["data"])

        self.page.screencast.start(
            on_frame=take, size=dict(config.VIEWPORT), quality=config.JPEG_QUALITY
        )
        try:
            self.page.wait_for_timeout(300)
            before = len(got)
            r, g, b = config.SELF_TEST_RGB
            handle = self._show(
                f'<div style="position:fixed;box-sizing:border-box;left:{box.x:.2f}px;top:{box.y:.2f}px;'
                f"width:{box.w:.2f}px;height:{box.h:.2f}px;border:3px solid rgb({r},{g},{b});"
                f'pointer-events:none"></div>'
            )
            deadline = _now_ms() + 3000
            while len(got) <= before and _now_ms() < deadline:
                self.page.wait_for_timeout(50)
            self.page.wait_for_timeout(300)
            self._dispose(handle)
        finally:
            self.page.screencast.stop()
        numbers: Dict[str, Any] = {
            "element": spec.describe(),
            "element_box": box.as_dict(),
        }
        if not got:
            raise CaptureFailed("C1 overlay", "the screencast delivered no frame")
        image = decode_rgb(got[-1], config.VIEWPORT["width"], config.VIEWPORT["height"])
        found = colour_box(image, config.SELF_TEST_RGB)
        if found is None:
            numbers["ring_box"] = None
            raise CaptureFailed(
                "C1 overlay",
                f"the ring drawn at {box.as_dict()} is not in the frame (it landed off the"
                " page): the overlay and the page disagree on coordinates",
            )
        offset = max(
            abs(found.x - box.x),
            abs(found.y - box.y),
            abs((found.x + found.w) - (box.x + box.w)),
            abs((found.y + found.h) - (box.y + box.h)),
        )
        numbers.update({"ring_box": found.as_dict(), "offset_px": round(offset, 2)})
        if offset > config.OVERLAY_TOLERANCE_PX:
            raise CaptureFailed(
                "C1 overlay",
                f"the ring is {offset:.1f} px from the element (drawn at {box.as_dict()},"
                f" found at {found.as_dict()}); at most {config.OVERLAY_TOLERANCE_PX} px",
            )
        return numbers

    # -- the checks ------------------------------------------------------------------

    def check_needles(self) -> None:
        # Imported here: docs_runner.execute imports Playwright, and the rest
        # of this module is unit tested where Playwright is not installed.
        from docs_runner.execute import DRAWN_JS

        self.tally.needle_checks += 1
        try:
            text, fields = self.page.evaluate(DRAWN_JS)
        except PlaywrightError as error:
            raise CaptureFailed(
                "gate 6", f"{self.where}: the page could not be read ({error})"
            ) from None
        # Read after the page: a token the page was handed while it was being
        # read is a needle already, and must be looked for in that text.
        values = set(config.STATIC_NEEDLES) | set(self.redactor.values)
        if not values:
            raise CaptureFailed("gate 6", "0 needles: refusing to check the page")
        where = redaction.on_screen(text, [tuple(f) for f in fields], values)
        if where:
            raise CaptureFailed(
                "gate 6",
                f"{self.where}: a kept value is drawn in {where}; recording dropped",
            )

    def check_page(self, focus: Box, found: Locator, cue: str = "") -> None:
        """Every check a cue start makes, after the needles."""
        self.tally.header_checks += 1
        header = self.page.evaluate(HEADER_JS)
        if header["wrapped"] or header["cut"]:
            raise CaptureFailed(
                "U1",
                f"{self.where}: the header wraps or cuts text (wrapped {header['wrapped']},"
                f" cut off {header['cut']})",
            )
        self.tally.forbidden_checks += 1
        drawn = self.page.evaluate(VIEWPORT_TEXT_JS)
        for text in self.forbidden:
            if text in drawn:
                raise CaptureFailed("U4", f"{self.where}: the page shows {text!r}")
        self.tally.broken_checks += 1
        broken = self.page.evaluate(BROKEN_JS)
        if broken["overlay"]:
            raise CaptureFailed(
                "gate 11", f"{self.where}: the Next.js error overlay is open"
            )
        unexpected = [
            a for a in broken["alerts"] if not any(ok in a for ok in self.beat_alerts)
        ]
        if unexpected:
            raise CaptureFailed(
                "gate 11", f"{self.where}: an alert is shown: {unexpected[0]!r}"
            )
        self.check_focus(focus, found, "start")
        self.check_numbers(cue, found)

    def check_numbers(self, cue: str, found: Locator) -> None:
        """U2: the numbers the caption states are in the focus's text."""
        self.tally.number_checks += 1
        if not storyboard.numbers_in(cue):
            return
        self.tally.numbered_cues += 1
        ok, _numbers, why = judge.caption_numbers(cue, found.inner_text())
        if not ok:
            raise CaptureFailed("U2", f"{self.where}: {why}")

    def check_focus(self, focus: Box, found: Locator, when: str) -> Box:
        self.tally.focus_checks += 1
        raw = found.bounding_box()
        if raw is None:
            raise CaptureFailed(
                "gate 9", f"{self.where}: the focus is gone at the cue's {when}"
            )
        box = Box(raw["x"], raw["y"], raw["width"], raw["height"])
        width, height = config.VIEWPORT["width"], config.VIEWPORT["height"]
        if box.x < 0 or box.y < 0 or box.x + box.w > width or box.y + box.h > height:
            raise CaptureFailed(
                "gate 9",
                f"{self.where}: focus at x {box.x:.0f}-{box.x + box.w:.0f}, y {box.y:.0f}-"
                f"{box.y + box.h:.0f} at the cue's {when}; the page area is 0-{width} x 0-{height}",
            )
        if not found.evaluate(ON_TOP_JS, list(box.centre)):
            raise CaptureFailed(
                "gate 9",
                f"{self.where}: the focus is covered at its centre at the cue's {when}",
            )
        self.tally.focus_boxes.append(
            {"where": self.where, "when": when, **box.as_dict()}
        )
        return box

    def check_events(self) -> None:
        if self.tally.unread:
            raise CaptureFailed(
                "gate 6",
                f"a JSON answer could not be read for credentials: {self.tally.unread[0]}",
            )
        if self.tally.refused:
            raise CaptureFailed("gate 15c", self.tally.refused[0])
        if self.tally.server_errors:
            raise CaptureFailed("gate 11", self.tally.server_errors[0])
        if self.tally.page_errors:
            raise CaptureFailed("gate 11", f"page error: {self.tally.page_errors[0]}")

    # -- steps ---------------------------------------------------------------------

    def settle(self) -> None:
        """Wait for the page's network to go quiet, then hold for the eye."""
        try:
            self.page.wait_for_load_state("networkidle", timeout=15000)
        except PlaywrightError:
            pass
        self.page.wait_for_timeout(config.SETTLE_MS)

    def scroll_into_view(self, found: Locator) -> None:
        found.evaluate("el => el.scrollIntoView({behavior: 'smooth', block: 'center'})")
        last = None
        for _ in range(60):
            self.page.wait_for_timeout(50)
            raw = found.bounding_box()
            now = None if raw is None else (round(raw["x"]), round(raw["y"]))
            if now == last:
                break
            last = now

    def _visible_box(self, spec: storyboard.Locator) -> Tuple[Locator, Box]:
        found, box = self.box(spec)
        height = config.VIEWPORT["height"]
        if box.y < 0 or box.y + box.h > height:
            self.scroll_into_view(found)
            found, box = self.box(spec)
        return found, box

    def step(self, step: Any) -> None:
        if isinstance(step, storyboard.WaitFor):
            self._wait(step)
        elif isinstance(step, storyboard.Move):
            _, box = self._visible_box(step.move)
            self.move_to(*box.centre)
        elif isinstance(step, storyboard.Click):
            found, box = self._visible_box(step.click)
            self.move_to(*box.centre)
            self.page.wait_for_timeout(config.POINTER_REST_MS)
            if not found.evaluate(ON_TOP_JS, list(box.centre)):
                raise CaptureFailed(
                    "gate 9", f"{self.where}: {step.click.describe()} is covered"
                )
            self.page.mouse.click(*box.centre)
            self.click_pulse(*box.centre)
        elif isinstance(step, storyboard.Type):
            found, box = self._visible_box(step.type)
            kind = found.evaluate("el => String(el.type || '').toLowerCase()")
            if step.secret and kind != "password":
                raise CaptureFailed(
                    "gate 6", f"{self.where}: a secret typed into a {kind} field"
                )
            self.move_to(box.x + min(40.0, box.w / 2), box.y + box.h / 2)
            self.page.mouse.click(box.x + min(40.0, box.w / 2), box.y + box.h / 2)
            if step.clear:
                found.fill("")
            found.press_sequentially(step.text, delay=config.TYPE_DELAY_MS)
            self.page.wait_for_timeout(config.FIELD_PAUSE_MS)
        elif isinstance(step, storyboard.Look):
            _, box = self._visible_box(step.look)
            self.ring(box)
            self.page.wait_for_timeout(step.ms)
            self.ring(None)
        elif isinstance(step, storyboard.ScrollTo):
            found = self.locate(step.scroll_to)
            self.scroll_into_view(found)
        self.check_needles()
        self.check_events()

    def _wait(self, step: storyboard.WaitFor) -> None:
        try:
            if step.url:
                self.page.wait_for_url(step.url, timeout=step.timeout_ms)
            else:
                assert step.wait_for is not None
                self.locate(step.wait_for).wait_for(
                    state="visible", timeout=step.timeout_ms
                )
        except PlaywrightError as error:
            # A 5xx or a page error is the likelier cause: say that first.
            self.check_events()
            raise CaptureFailed(
                "gate 12",
                f"{self.where}: waited for {step.url or step.wait_for.describe()}"
                f" in vain ({str(error).splitlines()[0]})",
            ) from None
        # On camera: the element is there; let it paint. (Off camera, before
        # a scene, ``settle`` waits for the network as well.)
        self.page.wait_for_timeout(config.PAINT_MS)

    # -- a beat, a scene -----------------------------------------------------------

    def beat(self, scene: Scene, index: int, beat: Beat) -> None:
        self.where = f"scene {scene.id} beat {index}"
        self.beat_alerts = list(beat.alerts)
        if beat.before:
            # The wait for the next page is cut: recording pauses until it has
            # painted, so no spinner is filmed and the caption gap stays short.
            self.recorder.stop()
            try:
                for step in beat.before:
                    self.step(step)
            finally:
                self.recorder.start()
            self.draw_cursor()
        found, focus = self._visible_box(beat.focus)
        self.check_needles()
        self.check_page(focus, found, beat.cue)
        self.check_events()
        for name, spec in beat.record.items():
            self.readings[name] = self.locate(spec).inner_text()
        if urlparse(self.page.url).path.rstrip("/") == "/experiments":
            self.listed_rows = self.page.evaluate(
                "() => document.querySelectorAll('table tbody tr').length"
            )
        start = self.recorder.now()
        minimum = storyboard.min_cue_ms(beat.cue)
        for step in beat.steps:
            self.step(step)
        remaining = minimum - (self.recorder.now() - start)
        if remaining > 0:
            self.page.wait_for_timeout(remaining)
        if beat.payoff:
            self.ring(None)
            payoff_from = self.recorder.now()
            self.page.wait_for_timeout(config.PAYOFF_MS)
            self.tally.payoffs.append(
                {
                    "where": self.where,
                    "from_ms": payoff_from,
                    "to_ms": self.recorder.now(),
                }
            )
        self.check_needles()
        end_box = self.check_focus(focus, found, "end")
        self.check_events()
        # The click that leaves the page, with the caption still up.
        for step in beat.exit:
            self.step(step)
        end = self.recorder.now()
        self.tally.holds_ms.append(end - start)
        self.cues.append(
            {
                "start_ms": start,
                "end_ms": end,
                "text": beat.cue,
                "focus": end_box.as_dict(),
            }
        )
        self.page.wait_for_timeout(config.CUE_GAP_MS)

    def card(self, card: storyboard.Card) -> None:
        self.page.set_content(card_html(card))
        self.page.wait_for_timeout(300)

    def sign_in(self) -> None:
        """Off camera: the demo admin signs in through the login page."""
        email, password = config.DEMO_ADMIN
        self.page.goto(self.dashboard + "/login")
        self.settle()
        self.page.get_by_label("Email").fill(email)
        self.page.get_by_role("textbox", name="Password").fill(password)
        self.page.get_by_role("button", name="Sign in", exact=True).click()
        try:
            self.page.wait_for_url("**/experiments", timeout=15000)
        except PlaywrightError as error:
            self.check_events()
            raise CaptureFailed(
                "gate 12",
                f"{self.where}: signing in off camera failed ({str(error).splitlines()[0]})",
            ) from None
        self.signed_in = True

    def scene(self, scene: Scene) -> None:
        self.where = f"scene {scene.id}"
        if scene.signed_in and not self.signed_in:
            self.sign_in()
        if scene.card is not None:
            self.card(scene.card)
        elif scene.results_of is not None:
            if self.resolve is None:
                raise CaptureFailed("gate 12", f"{self.where}: no experiment lookup")
            self.page.goto(f"{self.dashboard}/results/{self.resolve(scene.results_of)}")
            self.settle()
        else:
            assert scene.open is not None
            self.page.goto(self.dashboard + scene.open)
            self.settle()
        self.check_needles()
        self.check_events()
        began = self.recorder.now()
        self.recorder.start()
        try:
            if scene.card is None:
                self.draw_cursor()
            for index, beat in enumerate(scene.beats, start=1):
                self.beat(scene, index, beat)
        finally:
            self.ring(None)
            self._dispose(self._cursor_overlay)
            self._cursor_overlay = None
            self.recorder.stop()
        if scene.card is not None:
            self.still_spans.append((began, self.recorder.now()))
        self.tally.scenes_run.append(scene.id)


def card_html(card: storyboard.Card) -> str:
    """A card: the documented command (``#command``), quoted lines of code
    (``#code``) or a headline (``#headline``), labelled, with one sentence.

    Light, like the dashboard: a dark full frame outside the title and end
    cards reads as a black screen to the render's gate 11.
    """
    import html

    if card.command is not None:
        body = (
            '<div class="term" id="command"><span class="prompt">$</span>'
            f"{html.escape(card.command)}</div>"
        )
        width = 1040
    elif card.code is not None:
        lines = "\n".join(html.escape(line) for line in card.code.lines)
        body = f'<pre class="code" id="code">{lines}</pre>'
        width = 1600
    else:
        body = f'<div class="headline" id="headline">{html.escape(card.headline or "")}</div>'
        width = 1040
    return f"""<!doctype html><html><head><meta charset="utf-8"><style>
html body {{ zoom:1 !important; }}
body {{ margin:0; height:100vh; display:flex; align-items:center; justify-content:center;
  background:#F8FAFC; color:#0F172A; font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif; }}
.card {{ width:{width}px; }}
.label {{ font-size:22px; letter-spacing:.12em; text-transform:uppercase; color:#475569; margin-bottom:28px; }}
.term {{ background:#0F172A; border-radius:14px; padding:34px 40px;
  font-family:ui-monospace,SFMono-Regular,Menlo,monospace; font-size:40px; color:#F8FAFC; }}
.prompt {{ color:#38BDF8; margin-right:18px; }}
.code {{ margin:0; background:#0F172A; border-radius:14px; padding:30px 36px; white-space:pre;
  font-family:ui-monospace,SFMono-Regular,Menlo,monospace; font-size:24px; line-height:1.55; color:#F8FAFC; }}
.headline {{ font-size:72px; font-weight:700; letter-spacing:-.01em; color:#0F172A; }}
.note {{ margin-top:30px; font-size:26px; line-height:1.45; color:#334155; }}
</style></head><body><div class="card" id="card">
<div class="label">{html.escape(card.label)}</div>
{body}
<div class="note">{html.escape(card.note)}</div>
</div></body></html>"""
