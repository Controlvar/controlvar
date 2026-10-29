"""A console scene (video 9's API part): the page the tool draws, the calls it
makes to the take's own stack, and what each exchange must be (gate api).

* The page (``console_html``): a light body (``BODY``, as a card's, so the
  render's black check never reads it as a black screen) with one dark panel
  (``PANEL``) holding ``#request`` and ``#response``, the scene's label above
  it and the beat's ``#note`` below it. Inline CSS only: no font, script,
  stylesheet or image is fetched, and ``zoom:1`` is forced on <body> as on a
  card. Its text is set with ``textContent`` (``DRAW_JS``), never as HTML.
* A call (``send``): the method, the stack's own API origin and the board's
  path with its placeholders resolved, the run's admin token in a header, no
  redirect followed, and exactly the status the board declares, or refused
  naming the status it got. The take's origin must name no reserved port.
* An exchange (``Exchange``): what was sent and what came back, and when. Its
  drawn text (``exchange_lines``) is a pure function of its record: the
  request line and the body, the status line, then each shown field of the
  answer, in the answer's own order, with one muted "…" line for each run of
  fields left out. Ids are cut to their first 8 hex characters and "…",
  the same cut everywhere. The note is not part of it.
* Gate api (``exchange_problems``, ``api_problems``): per exchange, the exact
  status, the answer an object holding every shown field with the values the
  board declares (``start_date`` as an instant, naive read as UTC), the order
  drawn, sent, answered, drawn, inside its beat, and the panel's text after
  the send and at the cue's end equal to the record's, with each shown
  field's own value on its own line, read from the raw answer
  (``raw_answer_problems``); overall, one exchange per call beat in the
  board's order, a beat with no call keeping the one before it on screen,
  and no header, token or kept value in any record.
"""

from __future__ import annotations

import datetime as dt
import json
import re
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple
from urllib.parse import urlparse

from showcase import contract
from showcase.capture import config, guard, storyboard

# ---------------------------------------------------------------------------
# The page
# ---------------------------------------------------------------------------

#: The body is light, like a card's and the dashboard's.
BODY = "#F8FAFC"
#: The panel, its text, the muted "…" lines and the status line.
PANEL = "#0F172A"
INK = "#F8FAFC"
MUTED = "#94A3B8"
STATUS_INK = "#86EFAC"
#: A field rung while it is said: this ink on this bar, and the ring.
KEY_INK = "#FDE68A"
KEY_BAR = "#422006"
#: The label above the panel and the note below it, on the light body.
LABEL_INK = "#475569"
NOTE_INK = "#0F172A"
#: Monospace text, as the docs' requests are set.
FONT_PX = 28
LINE_HEIGHT = 1.5
PANEL_W = 1600
PANEL_PAD = 32
#: The rule between the request and the response: its margin above and below.
RULE_MARGIN = 12
#: The panel holds ``config.CONSOLE_MAX_LINES`` lines and the rule, no more.
PANEL_H = int(
    config.CONSOLE_MAX_LINES * FONT_PX * LINE_HEIGHT
    + 2 * PANEL_PAD
    + 2 * RULE_MARGIN
    + 2
)
#: The dark panel covers at most this share of the page area.
DARK_SHARE_MAX = 0.6


def dark_share() -> float:
    """The share of the page area the dark panel covers."""
    return PANEL_W * PANEL_H / (contract.PAGE_W * contract.PAGE_H)


def console_html(label: str) -> str:
    """The console page, empty: the label, the panel and the note's place."""
    import html

    return f"""<!doctype html><html><head><meta charset="utf-8"><style>
html body {{ zoom:1 !important; }}
body {{ margin:0; height:100vh; display:flex; align-items:center; justify-content:center;
  background:{BODY}; color:{NOTE_INK}; font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif; }}
#console {{ width:{PANEL_W}px; }}
#label {{ font-size:22px; letter-spacing:.12em; text-transform:uppercase; color:{LABEL_INK}; margin-bottom:24px; }}
#panel {{ box-sizing:border-box; width:{PANEL_W}px; height:{PANEL_H}px; overflow:hidden; background:{PANEL};
  border-radius:14px; padding:{PANEL_PAD}px 40px; color:{INK};
  font-family:ui-monospace,SFMono-Regular,Menlo,monospace; font-size:{FONT_PX}px; line-height:{LINE_HEIGHT}; }}
#panel div {{ white-space:pre; }}
#rule {{ height:2px; background:#334155; margin:{RULE_MARGIN}px 0; }}
.muted {{ color:{MUTED}; }}
.status {{ color:{STATUS_INK}; }}
.key {{ color:{KEY_INK}; background:{KEY_BAR}; }}
#note {{ margin-top:28px; min-height:38px; font-size:26px; line-height:1.45; color:{NOTE_INK}; }}
</style></head><body><div id="console">
<div id="label">{html.escape(label)}</div>
<div id="panel"><div id="request"></div><div id="rule"></div><div id="response"></div></div>
<div id="note"></div>
</div></body></html>"""


#: Draws a beat's state: the request's lines, the response's (each with its
#: class and the field it shows), and the note, all as text.
DRAW_JS = """(state) => {
  const fill = (id, lines) => {
    const box = document.getElementById(id);
    box.replaceChildren(...lines.map(([text, kind, key]) => {
      const line = document.createElement('div');
      line.textContent = text;
      if (kind) line.className = kind;
      if (key) line.dataset.key = key;
      return line;
    }));
  };
  fill('request', state.request);
  fill('response', state.response);
  document.getElementById('note').textContent = state.note || '';
  return true;
}"""
#: What the panel shows: each line's text, the response's with its field.
READ_JS = """() => ({
  request: [...document.querySelectorAll('#request > div')].map((d) => d.textContent),
  response: [...document.querySelectorAll('#response > div')].map(
    (d) => [d.textContent, d.dataset.key || null]),
  note: document.getElementById('note').textContent,
})"""
#: Marks a field's line as rung (the bar), or unmarks it.
MARK_JS = """([key, on]) => {
  const line = document.querySelector('#response > div[data-key="' + key + '"]');
  if (!line) return false;
  line.classList.toggle('key', on);
  return true;
}"""
#: The selector a look step rings a shown field with.
FIELD_SELECTOR = re.compile(r'^#response > div\[data-key="([a-z_][a-z0-9_]{0,63})"\]$')


# ---------------------------------------------------------------------------
# Values: placeholders, ids, the start
# ---------------------------------------------------------------------------

UUID = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.IGNORECASE
)
#: What an id is cut to, on screen: its first 8 hex characters, then this.
CUT_MARK = "…"
REASONS = {200: "OK", 201: "Created"}


def next_start(now: dt.datetime) -> str:
    """``{start}``: the next UTC day at 09:00:00Z after *now* (at least 9 h ahead)."""
    day = (now.astimezone(dt.timezone.utc) + dt.timedelta(days=1)).date()
    return f"{day.isoformat()}T09:00:00Z"


def cut(value: Any) -> Any:
    """*value* as drawn: every id in it cut to 8 hex characters and "…"."""
    if isinstance(value, str) and UUID.match(value):
        return value[:8] + CUT_MARK
    if isinstance(value, dict):
        return {k: cut(v) for k, v in value.items()}
    if isinstance(value, list):
        return [cut(v) for v in value]
    return value


def resolved(value: Any, values: Mapping[str, str]) -> Any:
    """*value* (a path, or a body or an answer's fields as written) with each
    ``{name}`` replaced by its value; a name with no value raises KeyError."""
    if isinstance(value, str):
        return storyboard.PLACEHOLDER.sub(lambda m: values[m.group(1)], value)
    if isinstance(value, dict):
        return {k: resolved(v, values) for k, v in value.items()}
    if isinstance(value, list):
        return [resolved(v, values) for v in value]
    return value


def as_instant(value: Any) -> Optional[dt.datetime]:
    """An ISO 8601 time as an aware UTC instant (naive read as UTC), or None."""
    if not isinstance(value, str):
        return None
    try:
        when = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=dt.timezone.utc)
    return when.astimezone(dt.timezone.utc)


def same_value(name: str, got: Any, wanted: Any) -> bool:
    """A shown field's value against the board's: ``start_date`` as an instant."""
    if name.endswith("_date") and isinstance(wanted, str):
        a, b = as_instant(got), as_instant(wanted)
        return a is not None and a == b
    return type(got) is type(wanted) and got == wanted


# ---------------------------------------------------------------------------
# The call
# ---------------------------------------------------------------------------


class CallRefused(RuntimeError):
    """A call was not made, or was not answered as the board declares."""


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """A 3xx is the answer, never followed: urllib then raises it as HTTPError."""

    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        return None


def no_redirect_opener() -> urllib.request.OpenerDirector:
    """``guard.http_opener``'s rule (no proxy from the environment), and no redirect."""
    return urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())


Opener = Callable[[urllib.request.Request], Tuple[int, bytes]]


def _open(request: urllib.request.Request) -> Tuple[int, bytes]:
    try:
        with no_redirect_opener().open(request, timeout=60) as answer:
            return int(answer.status), answer.read()
    except urllib.error.HTTPError as error:
        return int(error.code), error.read()


def send(
    api_url: str,
    token: str,
    method: str,
    path: str,
    body: Optional[Mapping[str, Any]],
    status: int,
    *,
    opener: Opener = _open,
) -> Tuple[int, Any]:
    """One call to the take's own API: ``(status, parsed answer)``, or refused.

    *api_url* is the stack's API origin; *path* starts ``/api/v1/``. GET, POST
    and PUT only; no redirect is followed; any status but *status* is refused,
    naming the status (and the API's ``detail``, cut short)."""
    if method not in ("GET", "POST", "PUT"):
        raise CallRefused(f"{method} {path}: only GET, POST and PUT are sent")
    if not path.startswith("/api/v1/") or "://" in path:
        raise CallRefused(f"{method} {path}: a call's path is under /api/v1/")
    url = api_url.rstrip("/") + path
    origin = urlparse(url)
    if guard.reserved_in(origin.netloc) or origin.scheme != "http":
        raise CallRefused(
            f"{method} {path}: the API's address {origin.netloc} names a reserved port"
            " or is not plain http"
        )
    data = None if body is None else json.dumps(body).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=data,
        method=method,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {token}",
        },
    )
    got, raw = opener(request)
    try:
        parsed = json.loads(raw) if raw else None
    except ValueError:
        parsed = None
    if got != status:
        detail = parsed.get("detail") if isinstance(parsed, dict) else None
        said = f": {str(detail)[:200]}" if detail else ""
        raise CallRefused(f"{method} {path} was answered {got}, not {status}{said}")
    return got, parsed


# ---------------------------------------------------------------------------
# An exchange, and its drawn text
# ---------------------------------------------------------------------------


@dataclass
class Exchange:
    """One call as made and answered, and when (recording clock, ms)."""

    scene: str
    beat: int
    method: str
    template: str
    path: str
    body: Optional[Dict[str, Any]]
    sets: Optional[str]
    show: Tuple[str, ...]
    status: Optional[int] = None
    #: The answer's top-level fields, in its order, and the shown ones' values.
    fields: List[str] = field(default_factory=list)
    shown: Dict[str, Any] = field(default_factory=dict)
    kept: Dict[str, str] = field(default_factory=dict)
    times: Dict[str, int] = field(default_factory=dict)
    checks: Dict[str, Any] = field(default_factory=dict)

    @property
    def answered(self) -> bool:
        return self.status is not None

    def record(self) -> Dict[str, Any]:
        """For the manifest: what was sent and shown, never a header or token."""
        return {
            "scene": self.scene,
            "beat": self.beat,
            "method": self.method,
            "template": self.template,
            "path": self.path,
            "sent": None if self.body is None else json.dumps(self.body),
            "sets": self.sets,
            "status": self.status,
            "show": list(self.show),
            "shown": dict(self.shown),
            "fields": len(self.fields),
            "omitted": len(self.fields) - len(self.shown),
            "kept": dict(self.kept),
            "times": dict(self.times),
            "checks": dict(self.checks),
        }


Line = Tuple[str, str, Optional[str]]


def field_line(name: str, value: Any) -> str:
    return f'"{name}": {json.dumps(cut(value), ensure_ascii=False)}'


def exchange_lines(exchange: Exchange) -> Tuple[List[Line], List[Line]]:
    """The panel's lines for *exchange*: ``(text, class, field)`` for the
    request, then the response (empty until it is answered)."""
    request: List[Line] = [(f"{exchange.method} {cut_path(exchange.path)}", "", None)]
    if exchange.body is not None:
        request.append((json.dumps(cut(exchange.body), ensure_ascii=False), "", None))
    if not exchange.answered:
        return request, []
    response: List[Line] = [
        (
            f"{exchange.status} {REASONS.get(exchange.status or 0, '')}".strip(),
            "status",
            None,
        )
    ]
    left_out = False
    for name in exchange.fields:
        if name in exchange.shown:
            if left_out:
                response.append(("…", "muted", None))
                left_out = False
            response.append((field_line(name, exchange.shown[name]), "", name))
        else:
            left_out = True
    if left_out:
        response.append(("…", "muted", None))
    return request, response


def exchange_text(exchange: Exchange) -> List[str]:
    """The panel's text for *exchange*, line by line (the note is not part)."""
    request, response = exchange_lines(exchange)
    return [text for text, _kind, _key in [*request, *response]]


def cut_path(path: str) -> str:
    """A path as drawn: each id in it cut, as in a body."""
    return "/".join(cut(part) for part in path.split("/"))


def drawn_lines(read: Mapping[str, Any]) -> List[str]:
    """``READ_JS``'s answer as the panel's lines."""
    return [
        *read.get("request", []),
        *[text for text, _key in read.get("response", [])],
    ]


def raw_answer_problems(
    show: Sequence[str], answer: Mapping[str, Any], read: Mapping[str, Any]
) -> List[str]:
    """Each shown field's value from the raw answer, on that field's own line
    of the response as drawn: its JSON value, or an id's first 8 hex
    characters. Independent of ``exchange_lines``."""
    lines: Dict[str, List[str]] = {}
    for text, key in read.get("response", []):
        if key:
            lines.setdefault(key, []).append(text)
    problems = []
    for name in show:
        value = answer.get(name)
        on_line = lines.get(name, [])
        if len(on_line) != 1:
            problems.append(f"{name} is drawn on {len(on_line)} lines, not 1")
            continue
        wanted = (
            value[:8]
            if isinstance(value, str) and UUID.match(value)
            else json.dumps(value, ensure_ascii=False)
        )
        if wanted not in on_line[0]:
            problems.append(f"{name}'s line does not show the answer's {name}")
    return problems


def fit_problems(lines: Sequence[str]) -> List[str]:
    """The panel's limits: ``config.CONSOLE_MAX_LINES`` lines of at most
    ``config.CONSOLE_LINE_CHARS`` characters."""
    problems = []
    if len(lines) > config.CONSOLE_MAX_LINES:
        problems.append(
            f"{len(lines)} lines; the console holds {config.CONSOLE_MAX_LINES}"
        )
    for line in lines:
        if len(line) > config.CONSOLE_LINE_CHARS:
            problems.append(
                f"a line of {len(line)} characters; the console holds"
                f" {config.CONSOLE_LINE_CHARS}"
            )
    return problems


# ---------------------------------------------------------------------------
# The calls of a scene, as the director makes them
# ---------------------------------------------------------------------------

Sender = Callable[[str, str, Optional[Mapping[str, Any]], int], Tuple[int, Any]]


class Calls:
    """A console scene's calls, for the director: the placeholders' values
    (``{experiment}`` and ``{group}`` looked up off camera; ``{start}`` taken
    when its request is drawn), the sender (``send`` on the take's own stack,
    the token held by the caller), and every exchange made."""

    def __init__(
        self,
        send: Sender,
        values: Mapping[str, str],
        *,
        now: Callable[[], dt.datetime] = lambda: dt.datetime.now(dt.timezone.utc),
    ):
        self.sender = send
        self.values: Dict[str, str] = dict(values)
        self.now = now
        self.exchanges: List[Exchange] = []
        #: Each beat with no call, the exchange it kept up and its checks.
        self.kept_up: List[Dict[str, Any]] = []
        #: The raw answers, by exchange, for the checks only (never written).
        self.answers: Dict[int, Any] = {}

    def begin(self, scene: str, beat: int, call: storyboard.Call) -> Exchange:
        """The exchange for *call*, its request resolved (not yet sent)."""
        if "start" not in self.values and "{start}" in json.dumps(
            [call.path, call.body]
        ):
            self.values["start"] = next_start(self.now())
        try:
            path = resolved(call.path, self.values)
            body = resolved(call.body, self.values) if call.body is not None else None
        except KeyError as missing:
            raise CallRefused(
                f"{call.method} {call.path}: no value for {{{missing.args[0]}}}"
            ) from None
        kind = storyboard.API_CALLS[(call.method, call.path)]
        exchange = Exchange(
            scene=scene,
            beat=beat,
            method=call.method,
            template=call.path,
            path=path,
            body=body,
            sets=kind.sets,
            show=tuple(call.show),
        )
        self.exchanges.append(exchange)
        return exchange

    def make(self, exchange: Exchange, call: storyboard.Call) -> Any:
        """Send *exchange*; fill in its answer; keep the ids the call keeps."""
        status, answer = self.sender(
            exchange.method, exchange.path, exchange.body, call.answer.status
        )
        exchange.status = status
        self.answers[id(exchange)] = answer
        if not isinstance(answer, dict):
            raise CallRefused(
                f"{exchange.method} {exchange.path} answered {type(answer).__name__},"
                " not an object"
            )
        exchange.fields = [str(k) for k in answer]
        absent = [name for name in call.show if name not in answer]
        if absent:
            raise CallRefused(
                f"{exchange.method} {exchange.path}: the answer has no {absent[0]!r}"
            )
        exchange.shown = {name: answer[name] for name in call.show}
        for name, key in call.keep.items():
            value = answer.get(key)
            if not isinstance(value, str) or not value:
                raise CallRefused(
                    f"{exchange.method} {exchange.path}: no {key!r} to keep as {name!r}"
                )
            exchange.kept[name] = value
            self.values[name] = value
        return answer


def exchange_problems(
    call: storyboard.Call, exchange: Exchange, values: Mapping[str, str]
) -> List[str]:
    """Gate api for one exchange, from its record and the board's call."""
    problems: List[str] = []
    if exchange.status != call.answer.status:
        problems.append(f"answered {exchange.status}, not {call.answer.status}")
    absent = [name for name in call.show if name not in exchange.shown]
    if absent:
        problems.append(f"the answer has no {absent[0]!r}")
    for name, written in call.answer.fields.items():
        try:
            wanted = resolved(written, values)
        except KeyError as missing:
            problems.append(f"no value for {{{missing.args[0]}}}")
            continue
        if name in exchange.shown and not same_value(
            name, exchange.shown[name], wanted
        ):
            problems.append(
                f"{name} is {cut(exchange.shown[name])!r}, not {cut(wanted)!r}"
            )
    order = [
        "cue_start",
        "request_drawn",
        "sent",
        "answered",
        "response_drawn",
        "cue_end",
    ]
    times = [exchange.times.get(name) for name in order]
    if any(t is None for t in times) or times != sorted(times):  # type: ignore[type-var]
        problems.append(
            "not drawn, sent, answered and drawn in that order inside its beat:"
            f" {dict(zip(order, times))}"
        )
    request = [text for text, _kind, _key in exchange_lines(exchange)[0]]
    if exchange.checks.get("request_as_drawn") != request:
        problems.append("the request sent is not the one drawn at the cue's start")
    for check in ("after_send", "at_cue_end"):
        if exchange.checks.get(check) != []:
            problems.append(
                f"the panel {check.replace('_', ' ')}: {exchange.checks.get(check)}"
            )
    problems += fit_problems(exchange_text(exchange))
    return problems


def call_beats(board: storyboard.Storyboard) -> List[Tuple[str, int, storyboard.Beat]]:
    return [
        (scene.id, number, beat)
        for scene in board.scenes
        if scene.console is not None
        for number, beat in enumerate(scene.beats, start=1)
    ]


def api_problems(
    board: storyboard.Storyboard,
    calls: "Calls",
    needles: Sequence[str],
) -> Tuple[List[str], Dict[str, Any]]:
    """Gate api over the whole video: every console beat's exchange, in the
    board's order; the beats with no call; and no secret in any record."""
    problems: List[str] = []
    beats = call_beats(board)
    wanted = [(s, n, b.call) for s, n, b in beats if b.call is not None]
    made = calls.exchanges
    numbers: Dict[str, Any] = {
        "exchanges": [e.record() for e in made],
        "kept_up": list(calls.kept_up),
        "values": {k: cut(v) for k, v in calls.values.items()},
    }
    if [(e.scene, e.beat, e.method, e.template) for e in made] != [
        (s, n, c.method, c.path) for s, n, c in wanted
    ]:
        problems.append(
            f"{len(made)} exchanges made; the board has {len(wanted)} calls, in another"
            " order or number"
        )
        return problems, numbers
    for exchange, (_s, _n, call) in zip(made, wanted):
        for problem in exchange_problems(call, exchange, calls.values):
            problems.append(
                f"scene {exchange.scene} beat {exchange.beat}"
                f" ({exchange.method} {exchange.template}): {problem}"
            )
    without = [(s, n) for s, n, b in beats if b.call is None]
    kept = [(k["scene"], k["beat"]) for k in calls.kept_up]
    if kept != without:
        problems.append(f"the beats with no call {without} were kept up as {kept}")
    for item in calls.kept_up:
        if item.get("at_cue_start") != [] or item.get("at_cue_end") != []:
            problems.append(
                f"scene {item['scene']} beat {item['beat']} did not keep its exchange"
                f" on screen: {item.get('at_cue_start')} {item.get('at_cue_end')}"
            )
    written = json.dumps(numbers, ensure_ascii=False)
    for word in ("Bearer", "Authorization", "eyJ"):
        if word in written:
            problems.append(f"a record holds {word!r}")
    for number, needle in enumerate(needles, start=1):
        if needle and needle in written:
            problems.append(f"a record holds kept value {number} of {len(needles)}")
    return problems, numbers


def secret_findings(text: str, needles: Sequence[str]) -> List[str]:
    """The render's own matcher (``ocr.findings``) on the console's drawn text:
    a kept value whole, cut short or split, a token's or a key's shape, a
    forbidden phrase. Descriptions only, never the text."""
    from showcase.render import ocr

    return [f"{gate}: {what}" for gate, what in ocr.findings(text, needles)]
