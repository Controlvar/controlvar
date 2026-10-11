"""One invocation: refusals, the lock, the tree, then each video's capture directory.

Order, and what must be true before each step:

1. Refused before anything is started: a port (``guard.refuse_ports``), an
   unknown video, an output or work directory inside a git checkout, the
   wrong Playwright, a missing ffmpeg, a ref that is not here.
2. The lock (``guard.RunLock``); then whatever a killed run left: its child
   processes (``procs.Children.stop_leftovers``) and its labelled containers.
3. The tree, once per invocation (``build``).
4. Per video, the disk floor first; below it the video is refused and no work
   directory is made. Then the stack, gate 14 (the seed's counts), the
   storyboard's ``setup`` made through the API off camera, the
   browser, the overlay self-test (C1), the code cards against the recorded
   tree, the scenes (with the storyboard's traffic and its results warmed off
   camera between them), gate 10 (the end state), and the capture directory
   (``write_capture``). The stack comes down in a ``finally``, and nothing
   labelled may be left.

The capture directory is ``<work root>/<slug>/capture/`` (contract.py):
``frames/`` and ``frames.jsonl``, ``cues.json``, ``needles.txt`` (mode 0600,
for the render side's OCR scan, which deletes it) and ``manifest.json`` with
every capture gate's verdict and numbers.
"""

from __future__ import annotations

import json
import os
import secrets
import signal
import subprocess
import sys
import threading
import time
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Tuple

from showcase import contract
from showcase.capture import build, config, guard, storyboard, traffic
from showcase.capture.procs import Children
from showcase.capture.stack import (
    Ports,
    Stack,
    StackError,
    labelled_containers,
    remove_labelled,
)

HERE = Path(__file__).resolve().parents[1]


@dataclass
class Options:
    videos: List[str]
    ref: str
    repo: Path
    work_dir: Path
    profile: str
    ports: Ports
    out: Path
    render: bool = True
    floor: int = contract.DISK_FLOOR_BYTES


class DiskSampler:
    """The lowest free space seen while a video runs (EM condition 2b)."""

    def __init__(self, path: Path):
        self.path = path
        self.lowest: Optional[int] = None
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def _run(self) -> None:
        while not self._stop.is_set():
            free = guard.free_bytes(self.path)
            self.lowest = free if self.lowest is None else min(self.lowest, free)
            self._stop.wait(1.0)

    def __enter__(self) -> "DiskSampler":
        self._thread.start()
        return self

    def __exit__(self, *exc: Any) -> None:
        self._stop.set()
        self._thread.join(timeout=5)


def gate(ok: bool, **numbers: Any) -> Dict[str, Any]:
    return {"ok": bool(ok), "numbers": numbers}


def _api(url: str, token: Optional[str] = None, body: Optional[dict] = None) -> Any:
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(url, data=data, headers=headers)
    with guard.http_opener().open(request, timeout=60) as answer:
        return json.load(answer)


def forbidden_text(board: storyboard.Storyboard, ports: Ports) -> Tuple[str, ...]:
    """What no cue may show (U4, checked at every cue start): the fixed list,
    the storyboard's ``never_show`` and the tool's own ports."""
    return (
        config.FORBIDDEN_TEXT
        + tuple(board.never_show)
        + tuple(str(p) for p in (ports.api, ports.dashboard, ports.postgres))
    )


def experiments_named(api_url: str, token: str, name: str) -> List[Dict[str, Any]]:
    """The experiments the API lists under *name* (there are a handful)."""
    items = _api(f"{api_url}/api/v1/experiments/?limit=100", token)["items"]
    return [item for item in items if item.get("name") == name]


def experiment_id(api_url: str, token: str, name: str) -> str:
    match = experiments_named(api_url, token, name)
    if len(match) != 1:
        raise StackError(f"{len(match)} experiments named {name!r}")
    return str(match[0]["id"])


def seed_counts(api_url: str, token: str) -> Dict[str, int]:
    return {
        "experiments": int(_api(f"{api_url}/api/v1/experiments/", token)["total"]),
        "feature_flags": int(_api(f"{api_url}/api/v1/feature-flags/", token)["total"]),
        "users": int(_api(f"{api_url}/api/v1/users/", token)["total"]),
    }


def make_setup(
    board: storyboard.Storyboard,
    api_url: str,
    token: str,
    *,
    call: Optional[Callable[..., Any]] = None,
) -> Dict[str, Any]:
    """The storyboard's ``setup``, made through the API off camera after gate 14.

    Each segment is made once, as a rules segment; an answer that is not that
    segment, active, refuses the video. Nothing for a storyboard without one.
    """
    if board.setup is None:
        return {}
    call = call or _api
    made = []
    for segment in board.setup.segments:
        answer = call(
            f"{api_url}/api/v1/segments",
            token,
            body={
                "name": segment.name,
                "description": segment.description,
                "kind": "rules",
                "rules": segment.rules,
            },
        )
        if (
            not isinstance(answer, dict)
            or answer.get("name") != segment.name
            or str(answer.get("status")).lower() != "active"
            or not answer.get("id")
        ):
            got = answer.get("name") if isinstance(answer, dict) else answer
            raise StackError(
                f"setup: segment {segment.name!r} was answered with {got!r}"
            )
        made.append({"segment": segment.name, "id": str(answer["id"])})
    return {"segments": made}


def flag_state(
    end: storyboard.FlagEnd,
    api_url: str,
    token: str,
    readings: Mapping[str, str],
) -> Tuple[bool, Dict[str, Any], str]:
    """Gate 10's half for a flag the video made: the API's answers, judged."""
    from urllib.parse import quote

    from showcase.capture import gates as judge

    listed = _api(
        f"{api_url}/api/v1/feature-flags/?search={quote(end.name)}&limit=100", token
    )["items"]
    named = [item for item in listed if item.get("name") == end.name]
    audit: List[Dict[str, Any]] = []
    if len(named) == 1:
        audit = _api(
            f"{api_url}/api/v1/audit-logs/?entity_type=feature_flag"
            f"&entity_id={named[0]['id']}&limit=100",
            token,
        )["items"]
    me = _api(f"{api_url}/api/v1/auth/me", token)
    return judge.flag_end(
        end,
        named=named,
        segments=_api(f"{api_url}/api/v1/segments?limit=200", token),
        audit=audit,
        actor={"email": me.get("email"), "id": me.get("id")},
        shown=readings.get(end.shown),
    )


def write_private(path: Path, text: str) -> None:
    """Create *path* with mode 0600 (never readable by anyone else), or fail."""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, contract.NEEDLES_MODE)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(text)
    os.chmod(path, contract.NEEDLES_MODE)


def frame_stats(frames: Path, entries: List[Dict[str, Any]]) -> List[float]:
    """Each frame's mean luma (0-255), from one ffmpeg pass at 64 px wide."""
    if not entries:
        return []
    done = subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-f",
            "image2",
            "-start_number",
            "1",
            "-i",
            str(frames / "%06d.jpg"),
            "-vf",
            "scale=64:-2",
            "-f",
            "rawvideo",
            "-pix_fmt",
            "gray",
            "-",
        ],
        capture_output=True,
        check=False,
        env=guard.tool_env(),
    )
    import numpy as np

    height = int(round(contract.PAGE_H * 64 / contract.PAGE_W / 2) * 2)
    size = 64 * height
    if done.returncode != 0 or len(done.stdout) != size * len(entries):
        raise RuntimeError(
            f"ffmpeg read {len(done.stdout) // max(size, 1)} of {len(entries)} frames"
        )
    lumas = np.frombuffer(done.stdout, np.uint8).reshape(len(entries), size)
    return [float(v) for v in lumas.mean(axis=1)]


# ---------------------------------------------------------------------------
# Preflight
# ---------------------------------------------------------------------------


def refuse_missing_ffmpeg(*, run: Callable[..., Any] = subprocess.run) -> None:
    """``ffmpeg -version`` with the allow-listed environment; Refused if it fails."""
    try:
        done = run(
            ["ffmpeg", "-hide_banner", "-version"],
            capture_output=True,
            text=True,
            env=guard.tool_env(),
        )
    except FileNotFoundError:
        raise guard.Refused("ffmpeg is not installed (brew install ffmpeg)") from None
    if done.returncode != 0:
        raise guard.Refused("ffmpeg is not installed (brew install ffmpeg)")


#: Where Playwright looks for its browsers and its Node.js. ``main`` clears the
#: process's environment before Playwright starts, so a run with either set
#: would fail late, inside the browser launch, with the stack already up.
PLAYWRIGHT_LOCATIONS = ("PLAYWRIGHT_BROWSERS_PATH", "PLAYWRIGHT_NODEJS_PATH")


def preflight(options: Options, environ: Mapping[str, str]) -> None:
    """Every refusal that needs no Docker, npm or uvicorn."""
    for name in PLAYWRIGHT_LOCATIONS:
        if name in environ:
            raise guard.Refused(
                f"{name} is set; the capture clears its environment before Playwright"
                " starts, so Playwright would not use it. Unset it, and install the"
                " browser where Playwright looks by default"
                " (python -m playwright install chromium)"
            )
    unknown = [v for v in options.videos if v not in config.VIDEOS]
    if unknown:
        raise guard.Refused(
            f"unknown video {unknown[0]!r}; one of: {', '.join(config.VIDEOS)}, all"
        )
    if options.profile not in ("core", "full"):
        raise guard.Refused(f"--profile {options.profile!r} is core or full")
    guard.refuse_ports(options.ports.as_dict(), in_use=lambda _port: False)
    for name, path in (("--out", options.out), ("--work-dir", options.work_dir)):
        checkout = guard.inside_git_checkout(path)
        if checkout is not None:
            raise guard.Refused(f"{name} {path} is inside the git checkout {checkout}")
    if options.render:
        guard.refuse_out_beside_work(options.out, options.work_dir)
    try:
        from importlib.metadata import version

        found = version("playwright")
    except Exception:  # not installed at all
        found = None
    if found != config.PLAYWRIGHT_VERSION:
        raise guard.Refused(
            f"Playwright {found or 'is not installed'}; the tool needs"
            f" {config.PLAYWRIGHT_VERSION} (tests/acceptance/requirements.txt)"
        )
    refuse_missing_ffmpeg()
    for board in options.videos:
        storyboard.load(HERE / config.VIDEOS[board])


# ---------------------------------------------------------------------------
# One video
# ---------------------------------------------------------------------------


def drop_recording(error: Exception, frames_dir: Path, work: guard.WorkRoot) -> None:
    """After a gate-6 refusal (a kept value on screen), delete every frame.

    Through ``WorkRoot.remove``, like every deletion the tool makes; the
    refusal says "recording dropped", and this is what makes that true.
    """
    if getattr(error, "gate", None) == "gate 6" and frames_dir.exists():
        work.remove(frames_dir)


def capture_video(
    slug: str,
    *,
    options: Options,
    source: build.Source,
    tree: Path,
    work: guard.WorkRoot,
    children: Children,
    versions: Mapping[str, Optional[str]],
    environ: Mapping[str, str],
    log: Callable[[str], None],
) -> Path:
    """Record one video into ``<work root>/<slug>/capture``; its path, or raises."""
    from showcase.capture import director as directing  # Playwright, only here

    board = storyboard.load(HERE / config.VIDEOS[slug])
    gates: Dict[str, Dict[str, Any]] = {}
    started = time.monotonic()
    readings = guard.refuse_low_disk(
        [work.path], floor=options.floor, before=f"video {slug}"
    )
    video_dir = work.path / slug
    if video_dir.exists():
        work.remove(video_dir)
    capture = video_dir / "capture"
    frames_dir = capture / "frames"
    frames_dir.mkdir(parents=True)
    logs = video_dir / "logs"
    run_id = f"{time.strftime('%Y%m%d%H%M%S')}-{secrets.token_hex(3)}"
    stack = Stack(
        tree=tree,
        ports=options.ports,
        profile=options.profile,
        run_id=run_id,
        children=children,
        logs=logs,
        parent=environ,
    )
    TEARDOWN.append(stack.down)
    with DiskSampler(work.path) as disk:
        try:
            log(f"{slug}: stack up")
            stack.up()
            gates["15b"] = gate(
                True,
                published=stack.facts["docker_port"],
                wanted=[str(options.ports.postgres)],
            )
            gates["C3"] = gate(
                True,
                **{k: v for k, v in stack.facts.items() if k.endswith("_launcher")},
                served_profile=stack.facts["served_profile"],
            )
            # Gate 14: a fresh stack, the seed's counts exactly.
            login = _api(
                f"{stack.api_url}/api/v1/auth/login",
                body={"email": config.DEMO_ADMIN[0], "password": config.DEMO_ADMIN[1]},
            )
            token = login["access_token"]
            counts = seed_counts(stack.api_url, token)
            wanted = board.seed_counts.model_dump()
            gates["14"] = gate(counts == wanted, counted=counts, seed=wanted)
            if counts != wanted:
                raise directing.CaptureFailed(
                    "gate 14",
                    f"the stack holds {counts}, the seed makes {wanted}: not a fresh stack",
                )
            setup = make_setup(board, stack.api_url, token)
            if setup:
                gates["14"]["numbers"]["setup"] = setup
                log(
                    "setup, off camera: "
                    + ", ".join(s["segment"] for s in setup["segments"])
                )
            try:
                facts = record(
                    board, stack, options, token, frames_dir, gates, log, work
                )
            except directing.CaptureFailed as error:
                drop_recording(error, frames_dir, work)
                raise
        finally:
            log(f"{slug}: stack down")
            stack.down()
            TEARDOWN.remove(stack.down)
        left = labelled_containers(children, stack.docker_env, work.path)
        gates["teardown"] = gate(not left, labelled_containers_left=left)
        if left:
            raise StackError(f"labelled containers left after teardown: {left}")
    after = guard.free_bytes(work.path)
    gates["disk"] = gate(
        True,
        floor_bytes=options.floor,
        before_video_bytes=readings[0][1],
        lowest_during_bytes=disk.lowest,
        after_teardown_bytes=after,
    )
    write_capture(
        capture,
        board=board,
        source=source,
        options=options,
        versions=versions,
        facts=facts,
        gates=gates,
        children=children,
        wall_seconds=time.monotonic() - started,
    )
    return capture


#: Gate 10/1's question to the API, asked from the page with the page's own token.
WHO_JS = """async () => {
  const token = localStorage.getItem('experimently.token');
  const answer = await fetch('/api/v1/auth/me', {headers: {Authorization: 'Bearer ' + token}});
  return answer.ok ? (await answer.json()).email : 'HTTP ' + answer.status;
}"""


def record(
    board: storyboard.Storyboard,
    stack: Stack,
    options: Options,
    token: str,
    frames_dir: Path,
    gates: Dict[str, Dict[str, Any]],
    log: Callable[[str], None],
    work: guard.WorkRoot,
) -> Dict[str, Any]:
    """The browser part of a capture: self-test, scenes, end state, recorded-data gates."""
    from docs_runner import redaction
    from playwright.sync_api import sync_playwright

    from showcase.capture import director as directing
    from showcase.capture import gates as judge

    redactor = redaction.Redactor()
    redactor.add(token)
    ports = options.ports
    forbidden = forbidden_text(board, ports)
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(env=guard.tool_env())
        try:
            context = browser.new_context(
                viewport=dict(config.VIEWPORT), device_scale_factor=1
            )
            page = context.new_page()
            director = directing.Director(
                context=context,
                page=page,
                frames=frames_dir,
                dashboard=stack.dashboard_url,
                api_port=ports.api,
                dashboard_port=ports.dashboard,
                redactor=redactor,
                forbidden=forbidden,
                work=work,
                resolve=lambda name: experiment_id(stack.api_url, token, name),
            )
            director.install()
            # Gate 3: the page is the frame's size at scale 1, before anything is recorded.
            size = page.evaluate("[innerWidth, innerHeight, devicePixelRatio]")
            wanted = [config.VIEWPORT["width"], config.VIEWPORT["height"], 1]
            if size != wanted:
                raise directing.CaptureFailed(
                    "gate 3", f"the page is {size}, not {wanted}"
                )
            # C1: the overlay lands where the page is, before any scene.
            page.goto(stack.dashboard_url + "/")
            director.settle()
            # The zoom as Chromium computed it, not the constant that asked for it.
            zoom = director.measure_zoom()
            gates["C1"] = gate(
                True,
                zoom=zoom,
                **director.self_test_overlay(
                    # Left of centre, so a ring drawn 4/3 too far still lands
                    # in the frame and the refusal can say by how much.
                    storyboard.Locator(role="link", name="Get started", exact=True)
                ),
                zoom_target=directing.ZOOM_TARGET,
            )
            log("overlay self-test passed")
            refuse_unquoted_code(board, stack.tree)
            users = Users(board.traffic, stack.api_url, token, redactor, log)
            TEARDOWN.append(users.stop)
            try:
                for scene in board.scenes:
                    if scene.traffic == "start":
                        users.start()
                    elif scene.traffic == "finish":
                        users.finish()
                    for name in scene.warm_results:
                        warm_results(stack.api_url, token, name)
                    log(f"scene {scene.id}")
                    director.scene(scene)
            finally:
                users.stop()
                TEARDOWN.remove(users.stop)
            director.check_events()
            gates["12"] = gate(
                director.tally.scenes_run == [s.id for s in board.scenes],
                ran=director.tally.scenes_run,
                storyboard=[s.id for s in board.scenes],
            )
            # Gate 10: the end state, through the API.
            who = page.evaluate(WHO_JS)
            end = board.end_state
            numbers: Dict[str, Any] = {
                "signed_in_as": who,
                "storyboard": end.model_dump(),
            }
            why = [] if who == end.signed_in_as else [f"signed in as {who!r}"]
            if end.experiments_listed is not None:
                listed = director.listed_rows
                total = int(
                    _api(f"{stack.api_url}/api/v1/experiments/", token)["total"]
                )
                numbers.update(experiments_listed=listed, experiments_in_api=total)
                if not listed == total == end.experiments_listed:
                    why.append(f"{listed} experiments listed, {total} in the API")
            if end.experiment is not None:
                ok, made, reason = users.end_state(end.experiment, director.readings)
                numbers["experiment"] = made
                if not ok:
                    why.append(reason)
            if end.flag is not None:
                ok, made, reason = flag_state(
                    end.flag, stack.api_url, token, director.readings
                )
                numbers["flag"] = made
                if not ok:
                    why.append(reason)
            gates["10"] = gate(not why, **numbers)
            if why:
                raise directing.CaptureFailed(
                    "gate 10",
                    "; ".join(why) + f"; the storyboard ends with {end.model_dump()}",
                )
        finally:
            browser.close()
    tally = director.tally
    entries = director.recorder.finish()
    duration = director.recorder.now()
    lumas = frame_stats(frames_dir, entries)
    still = director.still_spans
    checks = {
        "3": _merge(
            judge.frame_times(entries),
            {
                "viewport": dict(config.VIEWPORT),
                "device_scale_factor": 1,
                "frames_off_size": tally.frames_bad_size,
            },
        )
        if tally.frames_bad_size == 0
        else (
            False,
            {"frames_off_size": tally.frames_bad_size},
            "frames off the viewport size",
        ),
        "6": _merge(
            judge.needle_count(config.STATIC_NEEDLES, redactor.values),
            {
                "page_checks": tally.needle_checks,
                "hits": 0,
                "unread_answers": len(tally.unread),
            },
        ),
        "9": (True, {"checks": tally.focus_checks, "cues": len(director.cues)}, ""),
        "U1": (True, {"checks": tally.header_checks}, ""),
        "U2": (
            tally.number_checks == len(director.cues),
            {"checks": tally.number_checks, "numbered_cues": tally.numbered_cues},
            "a cue's numbers were not checked",
        ),
        "U4": (True, {"checks": tally.forbidden_checks, "texts": list(forbidden)}, ""),
        "11": eleven(
            (
                not (tally.server_errors or tally.page_errors),
                {
                    "checks": tally.broken_checks,
                    "responses": tally.responses,
                    "server_errors": tally.server_errors,
                    "page_errors": tally.page_errors,
                },
                "a 5xx or a page error",
            ),
            judge.freeze(entries, duration, tally.holds_ms, still),
            judge.black(entries, lumas, duration, still),
        ),
        "15c": (not tally.refused, {"refused": tally.refused}, "a refused request"),
        # C9: a click on a select and a choice not held are refused at the
        # step; this is their count, and the choices as held.
        "C9": (
            all(choice["ok"] for choice in tally.choices),
            {
                "choices": len(tally.choices),
                "options": [choice["option"] for choice in tally.choices],
                "held_ms": [choice["held_ms"] for choice in tally.choices],
            },
            "a select was not held on its option",
        ),
        "U5": judge.payoff_still(entries, tally.payoffs),
    }
    failed = []
    for name, (ok, numbers, why) in checks.items():
        gates[name] = gate(ok, **numbers)
        if not ok:
            failed.append(f"{name}: {why}")
    if len(director.cues) * 2 != tally.focus_checks:
        failed.append(
            f"9: {tally.focus_checks} focus checks for {len(director.cues)} cues"
        )
    if failed:
        raise directing.CaptureFailed("capture", "; ".join(failed))
    return {
        "entries": entries,
        "duration_ms": duration,
        "cues": director.cues,
        "needles": sorted(set(config.STATIC_NEEDLES) | set(redactor.values)),
        "zoom": zoom["body"],
    }


def eleven(broken, frozen, dark):
    """Gate 11's three halves, one verdict: the page, the freeze and the dark checks."""
    ok = broken[0] and frozen[0] and dark[0]
    why = "; ".join(v[2] for v in (broken, frozen, dark) if not v[0])
    return ok, {"page": broken[1], "freeze": frozen[1], "black": dark[1]}, why


def _merge(verdict, extra):
    ok, numbers, why = verdict
    return ok, {**numbers, **extra}, why


def refuse_unquoted_code(board: storyboard.Storyboard, tree: Path) -> None:
    """A code card shows the recorded release's docs, line for line (C10)."""
    from showcase.capture import director as directing

    for scene in board.scenes:
        code = scene.card.code if scene.card is not None else None
        if code is None:
            continue
        page = tree / code.doc
        problems = (
            storyboard.quoted_lines(page.read_text(encoding="utf-8"), code)
            if page.is_file()
            else [f"{code.doc} is not in the recorded tree"]
        )
        if problems:
            raise directing.CaptureFailed(
                "C10 copy", f"scene {scene.id}: {'; '.join(problems)}"
            )


#: The longest the storyboard's traffic may take (video 2's card says "minutes";
#: 63,000 users took 6 minutes on a local stack).
TRAFFIC_MAX_S = 1200.0


class Users:
    """The storyboard's simulated users (``traffic``), off camera, between scenes.

    ``start``: look up the experiment the video made, make an API key for it
    (its value is a needle from then on), and send until the first users are
    in. ``finish``: wait for the last. ``end_state``: gate 10's half for the
    experiment. Nothing at all for a storyboard without traffic.
    """

    def __init__(
        self,
        plan: Optional[storyboard.Traffic],
        api_url: str,
        token: str,
        redactor: Any,
        log: Callable[[str], None],
    ):
        self.traffic = plan
        self.api_url = api_url
        self.token = token
        self.redactor = redactor
        self.log = log
        self.sender: Optional[traffic.Sender] = None
        self.report: Optional[traffic.Report] = None

    def start(self) -> None:
        assert self.traffic is not None
        named = experiments_named(self.api_url, self.token, self.traffic.experiment)
        if len(named) != 1 or not named[0].get("key"):
            raise StackError(
                f"traffic: {len(named)} experiments named {self.traffic.experiment!r}"
            )
        made = traffic.plan(self.traffic, str(named[0]["key"]))
        key = _api(
            f"{self.api_url}/api/v1/api-keys",
            self.token,
            body={"name": "Showcase traffic"},
        )["key"]
        self.redactor.add(key)
        self.sender = traffic.Sender(
            made, api_url=self.api_url, api_key=key, workers=self.traffic.workers
        )
        self.log(f"traffic: {len(made.users)} users for {made.key}, off camera")
        self.sender.start()
        self.sender.wait_for(self.traffic.first, timeout=120)

    def finish(self) -> None:
        assert self.sender is not None
        total = len(self.sender.plan.users)
        self.report = self.sender.finish(
            # The card says the traffic took minutes: refused past twenty.
            timeout=TRAFFIC_MAX_S,
            progress=lambda sent: self.log(f"traffic: {sent} of {total} users in"),
        )
        self.log(f"traffic: all {total} users in")

    def stop(self) -> None:
        if self.sender is not None:
            self.sender.stop()

    def end_state(
        self, end: storyboard.ExperimentEnd, readings: Mapping[str, str]
    ) -> Tuple[bool, Dict[str, Any], str]:
        from showcase.capture import gates as judge

        if self.sender is None or self.report is None:
            return False, {}, "the traffic never finished"
        named = experiments_named(self.api_url, self.token, end.name)
        results = None
        if len(named) == 1:
            results = _api(
                f"{self.api_url}/api/v1/results/{named[0]['id']}", self.token
            )
        made = self.sender.plan
        ok, numbers, why = judge.experiment_end(
            end,
            named=named,
            results=results,
            planned=made.split,
            report=self.report.by_arm,
            oracle_p=traffic.generator().fisher_p_value(self.report.by_arm),
            shown=readings.get(end.shown),
        )
        numbers["traffic"] = self.report.as_numbers()
        numbers["planned_p_value"] = made.p_value
        return ok, numbers, why


def warm_results(api_url: str, token: str, name: str) -> None:
    """Ask for an experiment's results off camera, so the page loads them warm."""
    ident = experiment_id(api_url, token, name)
    for path in (
        f"/api/v1/results/{ident}?correction_method=benjamini_hochberg&confidence_level=0.95",
        f"/api/v1/results/{ident}/daily",
        f"/api/v1/results/{ident}/sample-size",
    ):
        _api(api_url + path, token)


# ---------------------------------------------------------------------------
# The capture directory
# ---------------------------------------------------------------------------


def write_capture(
    capture: Path,
    *,
    board: storyboard.Storyboard,
    source: build.Source,
    options: Options,
    versions: Mapping[str, Optional[str]],
    facts: Mapping[str, Any],
    gates: Dict[str, Dict[str, Any]],
    children: Children,
    wall_seconds: float,
) -> None:
    from showcase.capture import gates as judge

    ok, numbers, why = judge.child_environments(
        children.started, judge.allowed_env_keys()
    )
    gates["env"] = gate(ok, **numbers)
    gates["15a"] = gate(
        True,
        ports=options.ports.as_dict(),
        reserved=list(config.RESERVED_PORTS),
        programs_checked=len(children.started),
    )
    if not ok:
        raise guard.Refused(f"EM condition 10: {why}")
    gates["12"]["numbers"]["wall_seconds"] = round(wall_seconds, 1)
    lines = [
        json.dumps({"file": e["file"], "t_ms": e["t_ms"]}) for e in facts["entries"]
    ]
    (capture / "frames.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")
    cues = {
        "slug": board.slug,
        "title": board.title,
        "version": f"v{source.version}",
        "duration_ms": int(facts["duration_ms"]),
        "cues": facts["cues"],
    }
    (capture / "cues.json").write_text(
        json.dumps(cues, indent=2) + "\n", encoding="utf-8"
    )
    write_private(capture / "needles.txt", "\n".join(facts["needles"]) + "\n")
    manifest = {
        "ref": source.ref,
        "commit": source.commit,
        "next_version": versions.get("next_used"),
        "lock_next_version": versions.get("next_lockfile"),
        "profile": options.profile,
        "viewport": dict(contract.VIEWPORT),
        # Measured on <body> at the capture's start (C1), not config.ZOOM.
        "zoom": round(facts["zoom"], 6),
        "page_h": contract.PAGE_H,
        "band_h": contract.BAND_H,
        "gates": gates,
    }
    (capture / "manifest.json").write_text(
        json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
    )
    contract.validate_capture_dir(capture)


# ---------------------------------------------------------------------------
# The invocation
# ---------------------------------------------------------------------------


def parse_args(argv: List[str], environ: Mapping[str, str]) -> Options:
    import argparse

    parser = argparse.ArgumentParser(
        prog="showcase.py",
        description="Capture a product walkthrough from a local stack (#1066).",
    )
    parser.add_argument("video", help=f"{', '.join(config.VIDEOS)} or all")
    parser.add_argument(
        "--ref", default=config.DEFAULT_REF, help="the tag or commit to record"
    )
    parser.add_argument("--profile", default="core")
    parser.add_argument(
        "--work-dir",
        default=str(Path(environ.get("TMPDIR", "/tmp")) / "experimently-showcase-work"),
        help="where trees, frames and logs go (outside any git checkout)",
    )
    parser.add_argument("--api-port", type=int, default=config.DEFAULT_API_PORT)
    parser.add_argument(
        "--dashboard-port", type=int, default=config.DEFAULT_DASHBOARD_PORT
    )
    parser.add_argument(
        "--postgres-port", type=int, default=config.DEFAULT_POSTGRES_PORT
    )
    parser.add_argument(
        "--out",
        default=str(
            Path(environ.get("HOME", "~")) / "Downloads" / "experimently-showcase"
        ),
        help="where the render writes the MP4, the .vtt and the review page",
    )
    parser.add_argument(
        "--capture-only",
        action="store_true",
        help="stop at the capture directory; do not run the render",
    )
    args = parser.parse_args(argv)
    if not args.ref:
        raise guard.Refused(
            "give --ref (make showcase REF=<release tag>): no release has been"
            " validated against the storyboards yet"
        )
    videos = list(config.VIDEOS) if args.video == "all" else [args.video]
    return Options(
        videos=videos,
        ref=args.ref,
        repo=HERE.parents[2],
        work_dir=Path(args.work_dir).expanduser(),
        profile=args.profile,
        ports=Ports(args.api_port, args.dashboard_port, args.postgres_port),
        out=Path(args.out).expanduser(),
        render=not args.capture_only,
    )


def run(
    options: Options, environ: Mapping[str, str], log: Callable[[str], None]
) -> Dict[str, Any]:
    """Capture every video asked for; ``{slug: capture path or the refusal}``."""
    preflight(options, environ)
    work = guard.WorkRoot(options.work_dir)
    commit = build.resolve_ref(options.repo, options.ref)
    lock = guard.RunLock(work.path)
    lock.acquire()
    results: Dict[str, Any] = {}
    children = Children(work, environ)
    TEARDOWN.append(children.stop_all)
    try:
        stopped = children.stop_leftovers()
        if stopped:
            log(f"stopped {len(stopped)} processes a killed run left")
        docker_env = guard.child_env({}, parent=environ, docker=True)
        removed = remove_labelled(
            children, docker_env, work.path / "leftovers.log", work.path
        )
        if removed:
            log(f"removed {len(removed)} labelled containers a killed run left")
        # Only now: a killed run's API and dashboard held these ports until
        # the lines above stopped them.
        guard.refuse_ports(options.ports.as_dict())
        guard.refuse_low_disk([work.path], floor=options.floor, before="the build")
        tree = work.path / f"tree-{options.profile}"
        if tree.exists():
            work.remove(tree)
        log(f"building {options.ref} ({commit[:12]})")
        build.archive(options.repo, commit, tree)
        version = (tree / "VERSION").read_text(encoding="utf-8").strip()
        source = build.Source(ref=options.ref, commit=commit, version=version)
        build.strip_modules(tree, options.profile, work)
        build.clone_node_modules(options.repo / "frontend" / "node_modules", tree)
        web_env = guard.child_env(
            guard.dashboard_settings(
                api_port=options.ports.api, profile=options.profile
            ),
            parent=environ,
        )
        build.build_dashboard(
            tree,
            web_env,
            work.path / "build.log",
            spawn=lambda argv, cwd, env, log: children.run(
                "npm-build", argv, cwd=cwd, env=env, log=log
            ),
        )
        log(
            build.bundle_check(
                tree / "frontend" / "out" / "_next" / "static", options.profile
            )
        )
        versions = build.next_versions(tree)
        for slug in options.videos:
            try:
                results[slug] = capture_video(
                    slug,
                    options=options,
                    source=source,
                    tree=tree,
                    work=work,
                    children=children,
                    versions=versions,
                    environ=environ,
                    log=log,
                )
            except Exception as error:  # one video's failure, reported
                results[slug] = error
                log(f"{slug}: REFUSED: {error}")
    finally:
        children.stop_all()
        lock.release()
    return results


#: What a stopping signal must undo before the process ends, newest first.
TEARDOWN: List[Callable[[], None]] = []

#: The signals that bring the stack down and exit with 128 + the signal:
#: SIGTERM (143), SIGHUP (a closed terminal, 129) and SIGINT (Ctrl-C, 130).
STOP_SIGNALS: Tuple[signal.Signals, ...] = (
    signal.SIGTERM,
    signal.SIGHUP,
    signal.SIGINT,
)


def _stop_on_signal(signum: int, _frame: Any) -> None:
    """A stopping signal: bring the stack down, then exit with 128 + *signum*.

    The teardown runs here rather than through ``finally``: an exception raised
    while Playwright's sync API is waiting leaves it spinning (measured, for
    SIGTERM and for the KeyboardInterrupt a Ctrl-C raises while a screencast
    runs), so the handler stops the children and removes the container
    itself. A signal not in ``STOP_SIGNALS`` that ends the process (SIGKILL,
    or SIGQUIT from Ctrl-\\) leaves them behind, and the next run removes
    them first.
    """
    for undo in reversed(TEARDOWN):
        try:
            undo()
        except Exception:  # the next one still runs
            pass
    os._exit(128 + signum)


def install_stop_handlers() -> None:
    for signum in STOP_SIGNALS:
        signal.signal(signum, _stop_on_signal)


def main(argv: Optional[List[str]] = None) -> int:
    # The children are built from this copy, through the allow-lists in guard.
    environ = dict(os.environ)
    # Nothing from the caller's shell reaches anything this process starts,
    # Playwright's own driver included (as the render, showcase/render/__main__.py).
    guard.strip_environment(os.environ)
    install_stop_handlers()

    def log(line: str) -> None:
        sys.stdout.write(f"[showcase] {line}\n")
        sys.stdout.flush()

    try:
        options = parse_args(sys.argv[1:] if argv is None else argv, environ)
        results = run(options, environ, log)
    except guard.Refused as error:
        log(f"refused: {error}")
        return 2
    failed = [slug for slug, value in results.items() if not isinstance(value, Path)]
    for slug, value in results.items():
        if not isinstance(value, Path):
            log(f"{slug}: FAILED")
            continue
        log(f"{slug}: capture {value}")
        if options.render and render(value, options.out, environ, log) != 0:
            failed.append(slug)
    return 1 if failed else 0


def render(
    capture: Path, out: Path, environ: Mapping[str, str], log: Callable[[str], None]
) -> int:
    """``python -m showcase.render <capture> --out <out>``: the other half (PR 1a)."""
    acceptance = HERE.parent
    argv = [sys.executable, "-m", "showcase.render", str(capture), "--out", str(out)]
    env = guard.child_env({}, parent=environ)
    guard.refuse_reserved_in(argv, env, what="render")
    log(f"render {capture.parent.name} -> {out}")
    return subprocess.run(argv, cwd=acceptance, env=env, check=False).returncode
