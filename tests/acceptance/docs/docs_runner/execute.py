"""Walk one journey in a browser, one log line per step.

Steps run in order. A step that fails stops the guide: every later step is NOT
RUN (``earlier-step-failed``). A step whose journey file declares ``not_run``
is NOT RUN with that reason, and ``ref: doc-examples`` is NOT RUN
(``doc-examples``): Doc Examples runs those blocks. Nothing is skipped.

Elements are found only by role and accessible name, or by label, with exact
matching. Waiting is Playwright's own: an action waits for its element and an
expectation retries until it holds or ``DOCS_JOURNEY_TIMEOUT_MS`` (default
10000) passes. No step sleeps; the crawl's one re-ask, the sign-in pacer
(``pacer.py``: no faster than the stack's 10 sign-ins a minute, the step that
waited saying so) and an api step's ``poll`` (``waiting.py``: its request sent
again until its expectation holds, up to its bound, for what a background job
of the stack does on its own timer) are the only waits that are not
Playwright's own. A step that polled says in its log line how long it took,
and the run's terminal summary prints that line too (``JourneyRunner.polls``).

Files, under ``<run dir>/<journey>/``: ``NN-<step>.expected.aria.yml`` (the
expected ARIA snapshot, written before the step runs), ``NN-<step>.png`` and
``NN-<step>.aria.yml`` (the screen and its ARIA snapshot, after the step, or
when it fails), ``NN-<step>.api.json`` (an api step's status and the values at
its expected JSON paths, with how many seconds later each ``after`` time was
and, for a step that polls, how the poll ended), ``NN-<step>.expected.json``
(a ``crawl: nav`` step's pages, URLs and headings, read from the site's source
before the step runs),
``NN-<step>.crawl.json`` (what a crawl saw, and what differed),
``NN-<step>.observed.json`` (what a step that keeps no screen saw when it
failed: a ``keep`` step, or one with ``snapshot: false``). A ``video: true``
journey is recorded to ``videos/<journey>.webm`` (1280x720) unless
``DOCS_JOURNEY_RECORD_VIDEO=0``.

A journey with ``recordings`` records each of its launch walkthroughs
(``recordings.toml``) to ``recordings/<name>.webm`` unless
``DOCS_JOURNEY_RECORD_VIDEO=0``: ``page.screencast`` from the segment's
``start`` step to its ``end`` step (segments never overlap, so one screencast
runs at a time; a segment whose first step comes after a failed one is not
started, since none of its steps runs), 1280x720, real speed, opening on a title card (the
walkthrough, the guide, the commit and, on the compose stack, how long the
stack took to come up, which is not recorded) and with a caption bar (Playwright
overlays, which no locator, ARIA snapshot or click sees, hidden for each
screenshot) naming each step's guide section and action, and for a step off the
screen its result, each line through the run's redactor. Before the segment
starts and after each of its steps the page's text, its fields' placeholders,
its open shadow roots and its fields are read for every value the run keeps
(``DRAWN_JS``, ``redaction.on_screen``): one drawn there fails the step and the
recording is dropped. While a walkthrough is recorded a screenshot masks only a
field that holds a kept value (``MARK_JS``), since Playwright draws its masks
into the page and the recording would show a mask over every field. A page that cannot be read drops the recording without failing the
step; the presence check in ``docs-journeys.yml`` then fails the run.

Nothing a run makes up, keeps or is given is written (``redaction.py``): every
text above goes through the run's redactor (an error line before it is cut to
length), an api step's answer is written through ``redaction.scrub`` (every
credential under any key at any depth), a screenshot masks any element whose
text holds a value the journey keeps and, in a journey with secrets, every
field, and a journey that has a secret is never recorded.

An ``sdk`` step (``sdk.py``) writes ``NN-<step>.expected.json`` (the flag
answer the documented rollout hash gives its user, written before it runs) and
``NN-<step>.sdk.json``: the page's install command, the version installed, the
program run (the page's block with its placeholders replaced; the API key is
only in the environment of its process), each command's status and output,
redacted and cut to its end, the answers and the oracle's. A Go step's file
also holds the page's commands, ``go version``, and the module ``go get``
resolved: its version, whether it is a pseudo-version or a tag, the run's ref,
the rule applied (compare or record) and every file that differs from this
checkout's sdk/go.

A ``traffic`` step writes ``NN-<step>.expected.json`` (the population chosen,
written before it is sent) and ``NN-<step>.traffic.json`` (what was sent and
answered, in counts). Values an api step saves (``values.py``) are the
journey's own, cleared when the next journey starts; a value saved as a secret
is kept with the journey's other secrets and goes to the redactor.

The two crawls visit the nav pages once per journey and share what they saw.
A page or link that answers 5xx or nothing is asked once more,
``site.RETRY_SECONDS`` later (``site.py`` says why); with the sign-in pacer,
that is the only wait here that is not Playwright's own.
A search starts from the site's home page, so the results it reads are its
own: the site's search box is MkDocs Material's (a textbox named "Search"),
and its results are read once the count above them is shown.
"""

from __future__ import annotations

import dataclasses
import html
import json
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, NoReturn, Optional, Tuple
from urllib.parse import quote, urljoin, urlparse

from playwright.sync_api import Browser, Page, Playwright, expect
from playwright.sync_api import Error as PlaywrightError

from docs_runner import commands, redaction, registry, sdk, site, values, waiting
from docs_runner import pacer as pacing
from docs_runner import traffic as population
from docs_runner.checks import (
    OBSERVED_CHARS,
    StepFailed,
    agrees,
    agrees_or_not,
    describe_action,
    describe_expect,
    json_at,
    one_line,
    same_json,
    seconds_after,
    settle,
    shown_seconds,
    within,
)
from docs_runner.guide import Guide
from docs_runner.log import FAIL, NOT_RUN, PASS, STRUCTURAL_ONLY, Log, Record
from docs_runner.model import (
    LOCAL_URL,
    SEARCH_TOP,
    Cell,
    Journey,
    Named,
    Oracle,
    Recording,
    Step,
    reveals_credential,
)
from docs_runner.oracles import ORACLES
from docs_runner.registry import DOC_EXAMPLES
from docs_runner.stacks import Running

VIEWPORT = {"width": 1280, "height": 720}
#: How long a walkthrough's title card stays on the screen.
TITLE_CARD_MS = 1500
#: The longest line a walkthrough's caption bar draws.
CAPTION_CHARS = 180
#: The kinds of step that do nothing on the screen: their caption shows their result.
OFF_SCREEN = ("api", "traffic", "evaluations", "ref", "run", "finish")
#: Reads what a page draws: its rendered text, each field's placeholder, the
#: text of every open shadow root, and each field's type and value (in the
#: document and in those shadow roots). Same-origin frames, closed shadow
#: roots, CSS-generated text and canvas are not read.
DRAWN_JS = """() => {
  const texts = [document.body ? document.body.innerText : ''];
  const fields = [];
  const visit = (root) => {
    for (const el of root.querySelectorAll('*')) {
      if (el.matches('input, textarea')) {
        fields.push([String(el.type || 'text').toLowerCase(), String(el.value || '')]);
        if (el.placeholder) texts.push(String(el.placeholder));
      }
      if (el.shadowRoot) {
        texts.push(String(el.shadowRoot.textContent || ''));
        visit(el.shadowRoot);
      }
    }
  };
  visit(document);
  return [texts.join('\\n'), fields];
}"""
#: The attribute that marks, for one screenshot, a field holding a kept value.
MASK_MARK = "data-docs-journey-mask"
#: Marks every field (not a password field, which draws dots) whose value holds
#: one of the given values, in the document and its open shadow roots (which
#: Playwright's locators reach); answers how many it marked.
MARK_JS = """([values, mark]) => {
  let marked = 0;
  const visit = (root) => {
    for (const el of root.querySelectorAll('*')) {
      if (el.matches('input, textarea')) {
        const kind = String(el.type || 'text').toLowerCase();
        const typed = String(el.value || '');
        if (kind !== 'password' && values.some((v) => v && typed.includes(v))) {
          el.setAttribute(mark, '');
          marked += 1;
        }
      }
      if (el.shadowRoot) visit(el.shadowRoot);
    }
  };
  visit(document);
  return marked;
}"""
UNMARK_JS = """(mark) => {
  const visit = (root) => {
    for (const el of root.querySelectorAll('*')) {
      el.removeAttribute(mark);
      if (el.shadowRoot) visit(el.shadowRoot);
    }
  };
  visit(document);
}"""
CAPTION_STYLE = (
    "position:fixed;left:0;right:0;bottom:0;padding:10px 20px;"
    "background:rgba(17,24,39,0.9);color:#fff;"
    "font:17px/1.45 -apple-system,'Segoe UI',Roboto,Helvetica,Arial,sans-serif"
)


@dataclass
class _Judged:
    """One answer to an api step's request, held against its expectation."""

    status: int
    document: Any
    #: What the step's file records: the status, and each expected path's value.
    seen: Dict[str, Any]
    problems: List[str]
    #: For each ``after`` path, how many seconds later than its saved time.
    later: List[str]


#: The count MkDocs Material shows above the search results once they are in.
SEARCH_COUNT = re.compile(r"^(?:No|[0-9]+) matching documents?$")


@dataclass
class Settings:
    run_dir: Path
    run_id: str
    sha: str
    timeout_ms: int = 10000
    record_video: bool = True


def _close(overlay) -> None:
    """Remove a screencast overlay: Playwright's sync API disposes it on exit."""
    if overlay is not None:
        with overlay:
            pass


class Walkthrough:
    """One journey's launch walkthrough, recorded with ``page.screencast``."""

    def __init__(
        self,
        journey: Journey,
        journey_id: str,
        segment: Tuple[Recording, int, int],
        guide: Guide,
        running: Running,
        settings: "Settings",
    ):
        recording, self.first, self.last = segment
        self.name = recording.name
        self.journey_id = journey_id
        self.file = f"{redaction.RECORDINGS}/{self.name}.webm"
        self.path = settings.run_dir / self.file
        self.title = f"{self.name.split('-', 1)[0]}: {guide.title}"
        stack = ""
        if running.up_seconds:
            stack = (
                f"; the stack took {running.up_seconds:.0f} s to come up before"
                " this recording"
            )
        self.description = f"{journey.guide}, commit {settings.sha[:12]}{stack}"
        self.total = len(journey.steps)
        self.active = False
        self.started = 0.0
        self.seconds = 0.0
        #: Why the recording was dropped, or "".
        self.dropped = ""
        self.caption = None

    def start(self, page: Page) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            page.screencast.start(path=str(self.path), size=VIEWPORT)
        except PlaywrightError as error:
            self.dropped = f"the recording did not start: {one_line(str(error), 200)}"
            return
        self.active = True
        self.started = time.monotonic()
        try:
            page.screencast.show_chapter(
                self.title, description=self.description, duration=TITLE_CARD_MS
            )
        except PlaywrightError:
            pass

    def show(self, page: Page, lines: List[str]) -> None:
        """Replace the caption bar with *lines* (each cut to ``CAPTION_CHARS``)."""
        if not self.active:
            return
        body = "".join(
            f"<div>{html.escape(one_line(line, CAPTION_CHARS))}</div>" for line in lines
        )
        try:
            _close(self.caption)
            self.caption = page.screencast.show_overlay(
                f'<div style="{CAPTION_STYLE}">{body}</div>'
            )
        except PlaywrightError:
            self.caption = None

    def stop(self, page: Page) -> None:
        if not self.active:
            return
        self.active = False
        self.seconds = time.monotonic() - self.started
        try:
            _close(self.caption)
            page.screencast.stop()
        except PlaywrightError as error:
            self.drop(f"the recording did not stop: {one_line(str(error), 200)}")
        self.caption = None

    def drop(self, why: str, page: Optional[Page] = None) -> None:
        """Stop if recording, and remove what was recorded."""
        if self.active and page is not None:
            self.stop(page)
        self.active = False
        self.dropped = self.dropped or why
        self.path.unlink(missing_ok=True)

    def outcome(self) -> Dict[str, Any]:
        kept = not self.dropped and self.path.is_file()
        return {
            "journey": self.journey_id,
            "file": self.file if kept else "",
            "seconds": round(self.seconds, 1),
            "dropped": self.dropped,
        }


class JourneyRunner:
    """Runs journeys with one browser and the Playwright request API."""

    def __init__(
        self, playwright: Playwright, browser: Browser, log: Log, settings: Settings
    ):
        self.playwright = playwright
        self.browser = browser
        self.log = log
        self.settings = settings
        self.tokens: Dict[Tuple[str, str], str] = {}
        #: The stack the tokens were signed in on: one brought up again (for a
        #: journey of the other profile) has new accounts, which refuse them.
        self.stack: Optional[Running] = None
        #: The run's sign-ins, paced to the stack's limit (``pacer.py``).
        self.pacer = pacing.SignInPacer()
        #: Seconds the current step waited for the sign-in limit.
        self.waited = 0.0
        #: Every value the run makes up, keeps or signs in for; never written.
        self.redactor = redaction.Redactor()
        #: The current journey's passwords and kept values, by name.
        self.secrets: Dict[str, str] = {}
        #: The current journey's saved values (``values.py``), by name.
        self.values: Dict[str, str] = {}
        #: The current journey's guide: an sdk step reads its blocks from it.
        self.guide: Optional[Guide] = None
        #: What the crawls saw, per site and source: (nav pages, visit per page).
        self.visits: Dict[
            Tuple[str, str], Tuple[List[site.NavPage], Dict[str, Any]]
        ] = {}
        #: The walkthrough being recorded, if any.
        self.walkthrough: Optional[Walkthrough] = None
        #: Each walkthrough this run recorded or tried to, by name
        #: (``Walkthrough.outcome``).
        self.recorded: Dict[str, Dict[str, Any]] = {}
        #: One line per step that polled, for the run's terminal summary:
        #: the journey, the step, how long it took and how many seconds each
        #: ``after`` time was later. Numbers only; the values are in the files.
        self.polls: List[str] = []
        #: The current journey's blocks started in the background, by the id
        #: of the step that started them (``commands.py``).
        self.background: Dict[str, commands.Started] = {}
        expect.set_options(timeout=settings.timeout_ms)

    # -- files -------------------------------------------------------------
    def _name(self, journey_id: str, number: int, step: Step, suffix: str) -> str:
        return f"{journey_id}/{number:02d}-{step.id}{suffix}"

    def _line(self, text: Any, limit: int = OBSERVED_CHARS) -> str:
        """*text* on one line: kept values taken out first, then cut to *limit*."""
        return one_line(self.redactor.redact(str(text)), limit)

    def _write(self, name: str, text: str) -> str:
        path = self.settings.run_dir / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(self.redactor.redact(text), encoding="utf-8")
        return name

    def _screen(self, page: Page, journey_id: str, number: int, step: Step) -> str:
        name = self._name(journey_id, number, step, ".png")
        path = self.settings.run_dir / name
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            masks = [page.get_by_text(value) for value in self.secrets.values()]
            recording = self.walkthrough is not None and self.walkthrough.active
            if self.secrets and recording:
                # Playwright draws a mask into the page, so the recording would
                # show it. While one is recorded, only a field that holds a
                # kept value is masked (marked for this screenshot); none
                # should (the page check fails the step that shows one), and a
                # password field draws dots. The values are the page check's:
                # every one the run keeps, not only this journey's secrets.
                page.evaluate(MARK_JS, [sorted(self.redactor.values), MASK_MARK])
                masks.append(page.locator(f"[{MASK_MARK}]"))
            elif self.secrets:
                # A field's value is not its text, so get_by_text cannot find a
                # typed secret: in a journey with secrets, every field is masked.
                masks.append(page.locator("input, textarea, [contenteditable]"))
            if recording:
                # The step's screen is the page's, not the walkthrough's captions.
                page.screencast.hide_overlays()
            try:
                page.screenshot(path=str(path), mask=masks)
            finally:
                if recording:
                    page.screencast.show_overlays()
                    if self.secrets:
                        page.evaluate(UNMARK_JS, MASK_MARK)
            aria = page.locator("body").aria_snapshot()
        except PlaywrightError as error:
            aria = f"(no ARIA snapshot: {self._line(error)})"
        self._write(self._name(journey_id, number, step, ".aria.yml"), aria + "\n")
        return name if path.is_file() else ""

    def _no_screen(
        self, journey_id: str, number: int, step: Step, observed: str
    ) -> str:
        """What a failed step that keeps no screen saw, as text."""
        why = step.snapshot_reason or "a keep step keeps no screen"
        return self._write(
            self._name(journey_id, number, step, ".observed.json"),
            json.dumps({"error": observed, "screen": f"not kept: {why}"}, indent=2)
            + "\n",
        )

    # -- the API -----------------------------------------------------------
    def _headers(self, running: Running, caller: str, api) -> Dict[str, str]:
        if caller == "anonymous":
            return {}
        key = (running.api_url, caller)
        if key not in self.tokens:
            email, password = running.accounts[caller]
            self.waited += self.pacer.wait()
            self.pacer.record()
            answer = api.post(
                pacing.SIGN_IN_PATH, data={"email": email, "password": password}
            )
            if answer.status != 200:
                raise StepFailed(f"signing in as {caller} answered {answer.status}")
            self.tokens[key] = answer.json()["access_token"]
            self.redactor.add(self.tokens[key])
        return {"Authorization": f"Bearer {self.tokens[key]}"}

    def _note_sign_in(self, request) -> None:
        """Record each sign-in the browser sends, whatever sent it."""
        if (
            request.method == "POST"
            and urlparse(request.url).path == pacing.SIGN_IN_PATH
        ):
            self.pacer.record()

    def _secret(self, name: str) -> str:
        if name not in self.secrets:
            raise StepFailed(f"no secret saved or kept as {name!r}")
        return self.secrets[name]

    @staticmethod
    def _oracle(oracle: Oracle) -> float:
        return float(ORACLES[oracle.name](**oracle.args))

    def _judge(self, step: Step, answer) -> _Judged:
        """The answer held against the step's ``expect``: what was seen, what is wrong."""
        expect_ = step.expect
        seen: Dict[str, Any] = {"status": answer.status}
        problems: List[str] = []
        later: List[str] = []
        if expect_.status is not None and answer.status != expect_.status:
            problems.append(f"status {answer.status}, expected {expect_.status}")
        try:
            document = answer.json()
        except (PlaywrightError, ValueError):
            document = None
        else:
            # Every credential the answer holds, written or not, is looked for
            # in every file the run writes from here on.
            redaction.register_credentials(document, self.redactor)
            # `key` is left out of that rule (experiment keys are public), but
            # a credential route answers its plaintext secret under `key`.
            if reveals_credential(step) and isinstance(document, dict):
                self.redactor.add_all(document.get("key"))
        if document is None and (
            expect_.json_ or expect_.computed or expect_.after or step.save
        ):
            problems.append("the answer is not JSON")
        secret_paths = {s.path for s in (step.save or {}).values() if s.secret}
        for path, written in (expect_.json_ or {}).items():
            wanted = values.substitute_in(written, self.values)
            try:
                value = json_at(document, path)
            except KeyError:
                seen[path] = "(absent)"
                problems.append(f"json {path} absent")
                continue
            if path in secret_paths:
                shown = redaction.REDACTED
            else:
                shown = redaction.scrub(path, value, wanted, self.redactor)
            seen[path] = shown
            if not same_json(value, wanted):
                problems.append(f"json {path} = {json.dumps(shown)}")
        for path, computed in (expect_.computed or {}).items():
            oracle = self._oracle(computed.oracle)
            try:
                value = json_at(document, path)
            except KeyError:
                seen[path] = "(absent)"
                problems.append(f"json {path} absent")
                continue
            seen[path] = {"value": value, "oracle": oracle, "rel": computed.rel}
            if not within(value, oracle, computed.rel):
                problems.append(
                    f"json {path} = {json.dumps(value)}; {computed.oracle.name}"
                    f" gives {oracle!r}"
                )
        for path, name in (expect_.after or {}).items():
            if name not in self.values:
                problems.append(f"no value saved as {name!r}")
                continue
            since = self.values[name]
            try:
                value = json_at(document, path)
            except KeyError:
                seen[path] = "(absent)"
                problems.append(f"json {path} absent")
                continue
            shown = redaction.scrub(path, value, redactor=self.redactor)
            try:
                seconds = seconds_after(value, since)
            except StepFailed as error:
                seen[path] = {"value": shown, "after": name, "since": since}
                problems.append(f"json {path}: {error}")
                continue
            seen[path] = {
                "value": shown,
                "after": name,
                "since": since,
                "seconds_after": round(seconds, 3),
            }
            if seconds > 0:
                later.append(f"{path} {shown_seconds(seconds)} s after {name}")
            elif seconds == 0:
                problems.append(
                    f"json {path} is at the same time as {name}, not after it"
                )
            else:
                problems.append(
                    f"json {path} is {shown_seconds(-seconds)} s before {name}"
                )
        return _Judged(answer.status, document, seen, problems, later)

    def _api_step(
        self, step: Step, running: Running, api, journey_id: str, number: int
    ):
        call = step.do.api
        try:
            path = values.substitute(call.path, self.values)
            body = values.substitute_in(call.body, self.values)
        except KeyError as error:
            raise StepFailed(str(error.args[0])) from None

        def ask() -> _Judged:
            headers = self._headers(running, call.as_, api)
            if call.key is not None:
                headers = {"X-API-Key": self._secret(call.key)}
            answer = api.fetch(path, method=call.method, headers=headers, data=body)
            return self._judge(step, answer)

        polled: Optional[waiting.Polled] = None
        if step.poll is None:
            judged = ask()
        else:
            polled = waiting.poll(
                ask,
                lambda judged: not judged.problems,
                up_to_seconds=step.poll.up_to_seconds,
                every_seconds=step.poll.every_seconds,
            )
            judged = polled.answer
        document, seen, problems = judged.document, judged.seen, judged.problems
        for name, save in (step.save or {}).items():
            try:
                value = json_at(document, save.path)
            except KeyError:
                problems.append(
                    f"json {save.path} absent, so nothing is saved as {name}"
                )
                continue
            if save.secret:
                if not isinstance(value, str) or len(value) < redaction.MIN_LENGTH:
                    problems.append(f"json {save.path} is not a key to keep as {name}")
                    continue
                self.redactor.add(value)
                self.secrets[name] = value
                seen[save.path] = redaction.REDACTED
                continue
            # A token saved to use in a later path (an invite's, say) is never
            # written either: register_credentials above has already sent every
            # value under a credential's key to the redactor.
            try:
                self.values[name] = values.as_text(value)
            except ValueError as error:
                problems.append(
                    f"json {save.path}: {error}, so nothing is saved as {name}"
                )
        if polled is not None:
            seen["poll"] = {
                "up_to_seconds": step.poll.up_to_seconds,
                "every_seconds": step.poll.every_seconds,
                "held": polled.held,
                "asks": polled.asks,
                "seconds": round(polled.seconds, 1),
            }
        snapshot = self._write(
            self._name(journey_id, number, step, ".api.json"),
            json.dumps(seen, indent=2, ensure_ascii=False) + "\n",
        )
        if problems:
            observed = "; ".join(problems)
        else:
            observed = "; ".join([f"status {judged.status}", *judged.later])
        if polled is not None:
            ended = waiting.describe(polled, step.poll.up_to_seconds)
            joiner = "; the last answer: " if problems else ": "
            observed = ended + joiner + observed
            # The job log's line: how long it took, and how much later each
            # time was, never a value the answer held.
            self.polls.append(
                self._line(
                    f"{journey_id} step {number} ({step.id}): {ended}"
                    + "".join(f"; {line}" for line in judged.later)
                )
            )
        if problems:
            raise _Failed(self._line(observed), snapshot)
        return self._line(observed), snapshot

    def _traffic_step(
        self, step: Step, running: Running, api, journey_id: str, number: int
    ) -> Tuple[str, str]:
        """Send the chosen population (``traffic.py``); the check is the sending."""
        plan = step.do.traffic
        name = self._name(journey_id, number, step, ".traffic.json")
        experiment_id = self.values.get(plan.experiment)
        if experiment_id is None:
            raise StepFailed(f"no value saved as {plan.experiment!r}")
        key = self._secret(plan.key)
        answer = api.get(
            f"/api/v1/experiments/{experiment_id}",
            headers=self._headers(running, plan.as_, api),
        )
        if answer.status != 200:
            raise StepFailed(f"reading the experiment answered {answer.status}")
        experiment = answer.json()
        try:
            experiment_key = str(experiment["key"])
            allocations = [
                (str(v["name"]), int(v["traffic_allocation"]))
                for v in experiment["variants"]
            ]
        except (KeyError, TypeError, ValueError):
            raise StepFailed(
                "the experiment's key or variants could not be read"
            ) from None
        try:
            chosen = population.choose(
                experiment_key,
                allocations,
                {variant: p.assigned for variant, p in plan.variants.items()},
                plan.users,
            )
        except population.TrafficError as error:
            raise StepFailed(str(error)) from None

        def fetch(method: str, path: str, body: Optional[Dict[str, Any]]):
            sent = api.fetch(path, method=method, headers={"X-API-Key": key}, data=body)
            try:
                document = sent.json()
            except (PlaywrightError, ValueError):
                document = None
            return sent.status, document

        outcome = population.send(
            fetch,
            experiment_key=experiment_key,
            event=plan.event,
            chosen=chosen,
            converted={variant: p.converted for variant, p in plan.variants.items()},
            prefix=plan.users,
        )
        snapshot = self._write(
            name, json.dumps(outcome.record(), indent=2, ensure_ascii=False) + "\n"
        )
        if outcome.problems:
            raise _Failed(
                self._line(
                    f"{len(outcome.problems)} problem(s) in {outcome.requests}"
                    " requests: " + "; ".join(outcome.problems[:3])
                ),
                snapshot,
            )
        sent = "; ".join(
            f"{variant} {counts['as_chosen']} assigned as chosen,"
            f" {counts['converted']} converted"
            for variant, counts in outcome.variants.items()
        )
        return f"{outcome.requests} requests: {sent}", snapshot

    # -- a guide's shell block (commands.py) --------------------------------
    def _run_step(
        self, step: Step, running: Running, journey_id: str, number: int
    ) -> Tuple[str, str]:
        """Run the step's block, or start it in the background."""
        plan = step.do.run
        try:
            started = commands.start(
                plan.command, running.checkout, plan.up_to_seconds, os.environ
            )
        except commands.CommandError as error:
            raise StepFailed(str(error)) from None
        if plan.background:
            self.background[step.id] = started
            snapshot = self._write(
                self._name(journey_id, number, step, ".run.json"),
                json.dumps(
                    {
                        "command": plan.command,
                        "background": True,
                        "up_to_seconds": plan.up_to_seconds,
                    },
                    indent=2,
                    ensure_ascii=False,
                )
                + "\n",
            )
            return "started in the background", snapshot
        return self._ended(
            step, plan.command, commands.finish(started), journey_id, number
        )

    def _finish_step(self, step: Step, journey_id: str, number: int) -> Tuple[str, str]:
        """Let the background block of an earlier step end, and check how it did."""
        started = self.background.pop(step.do.finish, None)
        if started is None:
            raise StepFailed(
                f"no block was started in the background by {step.do.finish}"
            )
        return self._ended(
            step, started.command, commands.finish(started), journey_id, number
        )

    def _ended(
        self,
        step: Step,
        command: str,
        finished: commands.Finished,
        journey_id: str,
        number: int,
    ) -> Tuple[str, str]:
        """How the block ended, held to the step's expect; both written."""
        lines = list(step.expect.output or [])
        problems = commands.judge(finished, step.expect.exit, lines)
        self._write(
            self._name(journey_id, number, step, ".output.txt"), finished.tail()
        )
        snapshot = self._write(
            self._name(journey_id, number, step, ".run.json"),
            json.dumps(
                {
                    "command": command,
                    "exit": finished.status,
                    "stopped": finished.stopped,
                    "seconds": round(finished.seconds, 1),
                    "expected_exit": step.expect.exit,
                    "expected_output": lines,
                    "problems": problems,
                },
                indent=2,
                ensure_ascii=False,
            )
            + "\n",
        )
        if problems:
            raise _Failed(self._line("; ".join(problems)), snapshot)
        return (
            self._line(
                f"exited {finished.status} after {finished.seconds:.0f} s"
                + (f"; its output has all {len(lines)} lines" if lines else "")
            ),
            snapshot,
        )

    def _evaluations_step(
        self, step: Step, api, journey_id: str, number: int
    ) -> Tuple[str, str]:
        """Evaluate the flag for the numbered users (``traffic.evaluate``)."""
        plan = step.do.evaluations
        key = self._secret(plan.key)
        users = [population.user_id(plan.users, n) for n in range(1, plan.count + 1)]
        query = ""
        if plan.context is not None:
            query = "&context=" + quote(json.dumps(plan.context, separators=(",", ":")))

        def fetch(method: str, path: str, body: Optional[Dict[str, Any]]):
            sent = api.fetch(path, method=method, headers={"X-API-Key": key})
            try:
                document = sent.json()
            except (PlaywrightError, ValueError):
                document = None
            return sent.status, document

        outcome = population.evaluate(
            fetch,
            flag_key=plan.flag,
            users=users,
            reason=plan.reason,
            rollout=plan.rollout,
            query=query,
        )
        counts = outcome.variants[plan.flag]
        record = {
            "flag": plan.flag,
            "users": f"{plan.users}-NNNNN",
            "count": plan.count,
            "reason": plan.reason,
            "rollout": plan.rollout,
            **counts,
            "requests": outcome.requests,
            "problems": outcome.problems,
        }
        snapshot = self._write(
            self._name(journey_id, number, step, ".evaluations.json"),
            json.dumps(record, indent=2, ensure_ascii=False) + "\n",
        )
        if outcome.problems:
            raise _Failed(
                one_line(
                    f"{len(outcome.problems)} answer(s) not as expected in"
                    f" {outcome.requests}: " + "; ".join(outcome.problems[:3])
                ),
                snapshot,
            )
        return (
            f"{counts['evaluated']} users: {counts['enabled']} get the flag, as"
            f" expected ({plan.reason})",
            snapshot,
        )

    def _sdk_step(
        self, step: Step, running: Running, api, journey_id: str, number: int
    ) -> Tuple[str, str]:
        """Install the SDK as its page says and run its block (``sdk.py``)."""
        plan = step.do.sdk
        name = self._name(journey_id, number, step, ".sdk.json")
        record: Dict[str, Any] = {
            "user": plan.user,
            "flag": plan.flag,
            "rollout": plan.rollout,
        }

        def written() -> str:
            return self._write(
                name, json.dumps(record, indent=2, ensure_ascii=False) + "\n"
            )

        def stop(problem: str) -> NoReturn:
            record["problems"] = [problem]
            raise _Failed(self._line(problem), written())

        experiment_id = self.values.get(plan.experiment)
        if experiment_id is None:
            stop(f"no value saved as {plan.experiment!r}")
        try:
            key = self._secret(plan.key)
            answer = api.get(
                f"/api/v1/experiments/{experiment_id}",
                headers=self._headers(running, plan.as_, api),
            )
        except StepFailed as error:
            stop(str(error))
        if answer.status != 200:
            stop(f"reading the experiment answered {answer.status}")
        try:
            experiment = answer.json()
            experiment_key = str(experiment["key"])
            allocations = [
                (str(v["name"]), int(v["traffic_allocation"]))
                for v in experiment["variants"]
            ]
        except (KeyError, TypeError, ValueError, PlaywrightError):
            stop("the experiment's key or variants could not be read")
        go: Optional[sdk.GoCheck] = None
        try:
            if plan.language == "go":
                go_mod = (sdk.REPO_ROOT / sdk.GO_MODULE / "go.mod").read_text(
                    encoding="utf-8"
                )
                problem = sdk.go_requirement_problem(self.guide.text, go_mod)
                if problem:
                    raise sdk.SdkError(problem)
                go = sdk.GoCheck(
                    directive=sdk.go_directive(go_mod),
                    checkout=sdk.REPO_ROOT / sdk.GO_MODULE,
                    ref=os.environ.get("GITHUB_REF", ""),
                )
            install = sdk.install_command(
                self.guide.section(plan.install), plan.language
            )
            block = sdk.snippet(self.guide.section(plan.snippet), plan.language)
            values_for = {
                "{{api-url}}": running.api_url.rstrip("/"),
                "{{experiment}}": experiment_key,
                "{{flag}}": plan.flag,
                "{{user}}": plan.user,
            }
            source = sdk.program(
                plan.language,
                sdk.substitute(
                    block,
                    {
                        literal: values_for[value]
                        for literal, value in plan.replace.items()
                    },
                ),
                plan.stubs,
                [e for _, expressions in plan.answers.items() for e in expressions],
            )
        except (OSError, sdk.SdkError) as error:
            stop(str(error))
        wanted = sdk.expected(
            plan.user, experiment_key, allocations, plan.flag, plan.rollout
        )
        registries = {"npm": sdk.NPM_REGISTRY, "pip": sdk.PYPI_INDEX}
        record.update(
            {
                "experiment_key": experiment_key,
                "install": install.line,
                "registry": registries.get(install.tool, sdk.GO_PROXY),
                "block": sdk.fingerprint(block),
                "expected": wanted,
            }
        )
        if install.commands:
            record["install_commands"] = list(install.commands)
        expressions = [e for _, exprs in plan.answers.items() for e in exprs]
        outcome = sdk.run(
            language=plan.language,
            install=install,
            source=source,
            secrets_env={
                # Both names: the pages under test are the deployed site's, which
                # may predate the rename of these variables.
                "CONTROLVAR_API_URL": running.api_url.rstrip("/"),
                "CONTROLVAR_API_KEY": key,
                "EXPERIMENTLY_API_URL": running.api_url.rstrip("/"),
                "EXPERIMENTLY_API_KEY": key,
            },
            redact=self.redactor.redact,
            go=go,
            answer_keys=expressions,
        )
        problems = list(outcome.problems)
        if outcome.answers:
            problems += sdk.compare(dict(plan.answers.items()), outcome.answers, wanted)
        record.update(
            {
                "installed": outcome.installed,
                "answers": outcome.answers,
                "problems": problems,
                "commands": outcome.commands,
                "program": source,
            }
        )
        if go is not None:
            record.update({"toolchain": outcome.toolchain, "module": outcome.module})
        snapshot = written()
        if problems:
            raise _Failed(self._line("; ".join(problems[:3])), snapshot)
        module = ""
        if outcome.module:
            differing = len(outcome.module.get("differences", []))
            module = (
                f" ({outcome.module['kind']}; identical to this checkout's sdk/go)"
                if not differing
                else f" ({outcome.module['kind']}; {differing} files differ from this"
                " checkout's sdk/go, recorded: a tag run or a tagged module)"
            )
        return (
            f"installed {install.package} {outcome.installed}{module} from"
            f" {record['registry']}; {plan.user}: {wanted['variant']}, flag"
            f" {'on' if wanted['enabled'] else 'off'} ({wanted['reason']}), as the"
            " documented hashes give",
            snapshot,
        )

    # -- the browser -------------------------------------------------------
    @staticmethod
    def _role(page: Page, named: Named):
        return page.get_by_role(named.role, name=named.name, exact=True).first

    @staticmethod
    def _published(running: Running, url: str) -> str:
        """Where the guide's *url* is on this stack, or StepFailed when nowhere."""
        match = LOCAL_URL.match(url)
        port = int(match["port"]) if match else 0
        if port not in running.published:
            served = ", ".join(str(p) for p in sorted(running.published)) or "none"
            raise StepFailed(
                f"{url}: the stack serves nothing on port {port} by default"
                f" (the ports it serves a guide's addresses on: {served})"
            )
        return running.published[port] + (match["path"] or "/")

    def _act(self, step: Step, page: Page, running: Running):
        """Do the step's action; the navigation's response for a goto or an open."""
        do = step.do
        if do.open is not None:
            target = self._published(running, do.open)
        try:
            if do.goto is not None:
                target = values.substitute(do.goto, self.values)
                return page.goto(urljoin(running.base_url, target.lstrip("/")))
            if do.open is not None:
                return page.goto(target)
            if do.click is not None:
                if (
                    do.click.role == "button"
                    and do.click.name in pacing.SIGN_IN_BUTTONS
                ):
                    self.waited += self.pacer.wait()
                self._role(page, do.click).click()
            elif do.fill is not None:
                value = do.fill.value
                if do.fill.secret is not None:
                    value = self.secrets[do.fill.secret]
                page.get_by_label(do.fill.label, exact=True).fill(value)
            elif do.select is not None:
                page.get_by_label(do.select.label, exact=True).select_option(
                    label=do.select.option
                )
            elif do.search is not None:
                page.goto(running.base_url)
                page.get_by_role("textbox", name="Search", exact=True).fill(do.search)
        except PlaywrightError as error:
            raise StepFailed(
                f"could not {describe_action(step)}: {self._line(error, 160)}"
            ) from None
        except KeyError as error:
            raise StepFailed(str(error.args[0])) from None
        return None

    def _keep_step(self, step: Step, page: Page) -> str:
        """Read the one element's text and keep it; never written, only its length."""
        keep = step.do.keep
        within = keep.within
        if within is not None:
            element = page.get_by_role(
                within.role, name=within.name, exact=True
            ).get_by_role(keep.role)
            where = f'the {keep.role} in the {within.role} "{within.name}"'
        else:
            element = page.get_by_role(keep.role).filter(
                has_text=re.compile("^" + re.escape(keep.prefix))
            )
            where = f'the {keep.role} starting "{keep.prefix}"'
        self._holds(
            lambda: expect(element).to_have_count(1),
            f"not exactly one: {where}",
        )
        try:
            value = element.inner_text().strip()
        except PlaywrightError as error:
            raise StepFailed(
                f"could not read {where}: {self._line(error, 160)}"
            ) from None
        if len(value) < redaction.MIN_LENGTH:
            raise StepFailed(
                f"{where} holds {len(value)} characters, fewer than"
                f" {redaction.MIN_LENGTH}"
            )
        self.redactor.add(value)
        self.secrets[keep.secret] = value
        return f"kept {where} as {keep.secret} ({len(value)} characters)"

    @staticmethod
    def _holds(check, failure) -> None:
        """Run a Playwright expectation; a failure says *failure*, not the call log.

        *failure* is the line, or a function that writes it when the
        expectation has failed: a line naming where the page is must be
        written then, not before the expectation's wait began.
        """
        try:
            check()
        except AssertionError:
            raise StepFailed(failure() if callable(failure) else failure) from None

    def _search_step(self, step: Step, page: Page, running: Running) -> str:
        self._act(step, page, running)
        query, wanted = step.do.search, step.expect.found
        self._holds(
            lambda: expect(page.locator(".md-search-result__meta")).to_have_text(
                SEARCH_COUNT
            ),
            f'searching for "{query}" showed no results count',
        )
        results = page.locator(
            "li.md-search-result__item > a.md-search-result__link"
        ).evaluate_all("links => links.map(link => link.href)")
        place = site.rank(results, running.base_url, wanted)
        if place is None or place > SEARCH_TOP:
            shown = ", ".join(
                "/" + site.on_site(running.base_url, site.strip(url))
                for url in results[:SEARCH_TOP]
            )
            raise StepFailed(
                f'searching for "{query}": {wanted} is not among the first'
                f" {SEARCH_TOP} results ({shown or 'none'})"
            )
        return f'searching for "{query}": {wanted} is result {place} of {len(results)}'

    # -- the documentation site --------------------------------------------
    @staticmethod
    def _open(page: Page, url: str) -> Tuple[Optional[int], str]:
        """(the navigation's status, or None; the error, or "")."""
        try:
            response = page.goto(url, wait_until="domcontentloaded")
        except PlaywrightError as error:
            return None, one_line(error, 160)
        return (response.status if response is not None else None), ""

    def _visit(self, page: Page, url: str, base: str) -> Dict[str, Any]:
        """Open one page of the site: its status, where it ended, its heading, its links.

        A page that answers 5xx or nothing is opened once more, after
        ``site.RETRY_SECONDS`` (``site.open_page`` says why).
        """
        opened = site.open_page(url, lambda target: self._open(page, target))
        if opened.error:
            return {
                "url": url,
                "status": None,
                "error": opened.error,
                "retried": opened.retried,
            }
        final = page.url
        headings = page.get_by_role("main").get_by_role("heading", level=1)
        heading = headings.first.inner_text() if headings.count() else None
        hrefs = page.locator("a[href]").evaluate_all(
            "links => links.map(link => link.href)"
        )
        return {
            "url": "/" + site.on_site(base, url),
            "status": opened.status,
            "final": "/" + site.on_site(base, site.strip(final)),
            "on_site": site.within(final, base),
            "heading": site.normalise(heading) if heading is not None else None,
            "links": site.site_links(hrefs, base),
            "retried": opened.retried,
        }

    def _site_visits(self, page: Page, running: Running):
        """The nav pages of the site's source, and what was seen at each; once per site."""
        if running.source is None:
            raise StepFailed(f"the {running.name} stack names no site source")
        key = (running.base_url, str(running.source))
        if key not in self.visits:
            pages = site.nav_pages(site.Source(running.source))
            seen = {
                nav.path: self._visit(
                    page, urljoin(running.base_url, nav.url), running.base_url
                )
                for nav in pages
            }
            self.visits[key] = (pages, seen)
        return self.visits[key]

    def _expected_nav(self, running: Running, name: str) -> str:
        """Write what ``crawl: nav`` expects, from the source, before it runs."""
        if running.source is None:
            return STRUCTURAL_ONLY
        try:
            pages = site.nav_pages(site.Source(running.source))
        except site.SiteError:
            return STRUCTURAL_ONLY
        rows = [
            {"page": p.path, "url": "/" + p.url, "heading": p.heading} for p in pages
        ]
        return self._write(name, json.dumps(rows, indent=2, ensure_ascii=False) + "\n")

    def _crawl_step(
        self,
        step: Step,
        page: Page,
        running: Running,
        request,
        journey_id: str,
        number: int,
    ) -> Tuple[str, str]:
        name = self._name(journey_id, number, step, ".crawl.json")
        try:
            pages, seen = self._site_visits(page, running)
        except site.SiteError as error:
            raise _Failed(
                one_line(error),
                self._write(name, json.dumps({"error": str(error)}) + "\n"),
            ) from None
        if step.do.crawl == "nav":
            problems = site.compare_headings(pages, seen)
            checked, what = len(pages), "nav pages"
            asked_twice = sum(1 for visit in seen.values() if visit.get("retried"))
            record: Dict[str, Any] = {
                "problems": problems,
                "seen": {
                    path: {k: v for k, v in visit.items() if k != "links"}
                    for path, visit in seen.items()
                },
            }
        else:
            source = site.Source(running.source)
            written = site.site_url(source) or running.base_url
            links: Dict[str, str] = {}
            for nav in pages:
                for link in seen[nav.path].get("links", []):
                    links.setdefault(link, "/" + nav.url)
            for path, link in site.absolute_links(source, [nav.path for nav in pages]):
                links.setdefault(
                    site.to_stack(link, written, running.base_url), f"{path} (source)"
                )

            def fetch(url: str) -> Tuple[int, str]:
                answer = request.get(url, max_redirects=0)
                return answer.status, answer.headers.get("location", "")

            resolved = [site.resolve(link, running.base_url, fetch) for link in links]
            problems = [
                f"/{site.on_site(running.base_url, r.link)} (on {links[r.link]}): {r.problem}"
                for r in resolved
                if r.problem
            ]
            checked, what = len(links), "links to the site"
            asked_twice = sum(1 for r in resolved if r.retried)
            record = {
                "problems": problems,
                "links": {
                    "/" + site.on_site(running.base_url, r.link): {
                        "on": links[r.link],
                        "status": r.status,
                        "retried": r.retried,
                    }
                    for r in resolved
                },
            }
        snapshot = self._write(
            name,
            json.dumps({"checked": checked, **record}, indent=2, ensure_ascii=False)
            + "\n",
        )
        observed = site.crawl_text(
            checked, what, problems, asked_twice, running.source_ref
        )
        if problems:
            raise _Failed(self._line(observed), snapshot)
        if not checked:
            raise _Failed(f"no {what} to check", snapshot)
        return observed, snapshot

    def _browser_step(self, step: Step, page: Page, running: Running) -> str:
        response = self._act(step, page, running)
        wanted = step.expect

        def here() -> str:
            # The document's own location. Measured 2026-10-06 on the
            # dashboard's development server: seconds after Log out, page.url
            # still read /admin/users while location was /login?next=...
            try:
                return str(page.evaluate("location.pathname")) or "/"
            except PlaywrightError:
                return urlparse(page.url).path or "/"

        if wanted.status is not None:
            status = response.status if response is not None else None
            if status != wanted.status:
                raise StepFailed(f"status {status}, expected {wanted.status}")
        if wanted.url is not None:
            base = running.base_url
            if step.do.open is not None:
                opened = urlparse(page.url)
                base = f"{opened.scheme}://{opened.netloc}/"
            url = urljoin(base, wanted.url.lstrip("/"))
            self._holds(
                lambda: expect(page).to_have_url(url),
                lambda: f"at {here()}, not {wanted.url}",
            )
        for named in wanted.visible or []:
            self._holds(
                lambda named=named: expect(self._role(page, named)).to_be_visible(),
                lambda named=named: (
                    f'no {named.role} "{named.name}" visible at {here()}'
                ),
            )
        if wanted.text is not None:
            self._holds(
                lambda: expect(
                    page.get_by_text(wanted.text, exact=True).first
                ).to_be_visible(),
                lambda: f'no text "{wanted.text}" visible at {here()}',
            )
        if wanted.number is not None:
            number = wanted.number
            element = self._role(page, number.locator)
            self._holds(
                lambda: expect(element).to_be_visible(),
                lambda: (
                    f'no {number.locator.role} "{number.locator.name}"'
                    f" visible at {here()}"
                ),
            )
            oracle = self._oracle(number.oracle)
            text = settle(
                element.inner_text, lambda seen: self._changes(element, seen), oracle
            )
            if not agrees_or_not(text, oracle):
                raise StepFailed(
                    f'{number.locator.role} "{number.locator.name}" shows'
                    f" {one_line(text, 40)!r}; {number.oracle.name} gives {oracle:.6g}"
                )
            shown_number = f"{number.locator.name} {one_line(text, 40)}"
        if wanted.aria is not None:
            self._holds(
                lambda: expect(page.locator("body")).to_match_aria_snapshot(
                    wanted.aria
                ),
                lambda: (
                    f"the screen at {here()} does not match its expected ARIA snapshot"
                ),
            )
        shown_cells = []
        for cell in wanted.cells or []:
            text = self._cell_text(page, cell, here())
            oracle = self._oracle(cell.oracle)
            if not agrees(text, oracle):
                raise StepFailed(
                    f'the {cell.column} of the row "{cell.row}" shows'
                    f" {one_line(text, 40)!r}; {cell.oracle.name} gives {oracle:.6g}"
                )
            shown_cells.append(f"{cell.column} {one_line(text, 40)}")
        observed = f"at {here()}"
        if wanted.number is not None:
            observed += f" ({shown_number}, its oracle's)"
        if shown_cells:
            observed += " (" + ", ".join(shown_cells) + ", each its oracle's)"
        if response is not None:
            observed += f" (status {response.status})"
        return observed

    @staticmethod
    def _changes(element, seen: str) -> bool:
        """Wait for *element*'s text to stop being *seen*; False if it never does."""
        try:
            expect(element).not_to_have_text(seen)
        except AssertionError:
            return False
        return True

    def _cell_text(self, page: Page, cell: Cell, here: str) -> str:
        """The text of the cell under ``cell.column`` in the one row reading ``cell.row``."""
        header = page.get_by_role("columnheader", name=cell.column, exact=True)
        self._holds(
            lambda: expect(header).to_have_count(1),
            f'not one column headed "{cell.column}" at {here}',
        )
        row = (
            page.get_by_role("table")
            .filter(has=header)
            .get_by_role("row")
            .filter(has=page.get_by_role("cell", name=cell.row, exact=True))
        )
        self._holds(
            lambda: expect(row).to_have_count(1),
            f'not one row with a cell "{cell.row}" under "{cell.column}" at {here}',
        )
        index = header.evaluate("th => th.cellIndex")
        text = row.evaluate(
            "(tr, i) => tr.cells[i] ? tr.cells[i].innerText : null", index
        )
        if text is None:
            raise StepFailed(f'the row "{cell.row}" has no cell under "{cell.column}"')
        return str(text)

    # -- one journey -------------------------------------------------------
    def run(
        self, journey: Journey, journey_id: str, running: Running, guide: Guide
    ) -> List[Record]:
        settings = self.settings
        if running is not self.stack:
            self.tokens.clear()
            self.stack = running
        options: Dict[str, Any] = {"viewport": VIEWPORT}
        if running.ignore_https_errors:
            # compose-sso only: its identity provider's certificate is signed
            # by the run's own CA, which the browser does not know. What the
            # stack is about is the API's trust in it (stacks.py).
            options["ignore_https_errors"] = True
        video_dir = settings.run_dir / "videos" / journey_id
        self.secrets = {}
        for name in journey.passwords:
            self.secrets[name] = redaction.make_password()
            self.redactor.add(self.secrets[name])
        if journey.video and settings.record_video and not journey.has_secrets:
            options.update(record_video_dir=str(video_dir), record_video_size=VIEWPORT)
        walkthroughs: List[Walkthrough] = []
        if settings.record_video:
            walkthroughs = [
                Walkthrough(journey, journey_id, segment, guide, running, settings)
                for segment in journey.segments
            ]
        self.walkthrough = None
        self.values = {}
        self.guide = guide
        self.background = {}
        context = self.browser.new_context(**options)
        context.set_default_timeout(settings.timeout_ms)
        page = context.new_page()
        page.on("request", self._note_sign_in)
        api = None
        if running.api_url:
            api = self.playwright.request.new_context(base_url=running.api_url)
        request = None
        if running.source is not None:
            request = self.playwright.request.new_context()
        anchors = guide.anchors
        records: List[Record] = []
        failed = False
        try:
            for number, step in enumerate(journey.steps, 1):
                drawn = ""
                # The segment this step is in, if any (segments never overlap).
                walkthrough = next(
                    (w for w in walkthroughs if w.first <= number - 1 <= w.last), None
                )
                self.walkthrough = walkthrough
                if walkthrough is not None and number - 1 == walkthrough.first:
                    if failed:
                        walkthrough.drop("an earlier step failed, so none of it ran")
                    else:
                        drawn = self._drawn(page, walkthrough)
                        if not drawn and not walkthrough.dropped:
                            walkthrough.start(page)
                if walkthrough is not None and walkthrough.active and not failed:
                    walkthrough.show(page, self._caption(step, number, anchors))
                record = self._step(
                    journey,
                    journey_id,
                    number,
                    step,
                    running,
                    page,
                    api,
                    request,
                    anchors,
                    failed,
                )
                if walkthrough is not None and walkthrough.active:
                    if record.result != NOT_RUN:
                        drawn = drawn or self._drawn(page, walkthrough)
                    if not drawn and record.result != NOT_RUN:
                        walkthrough.show(
                            page, self._caption(step, number, anchors, record)
                        )
                if drawn:
                    record = dataclasses.replace(
                        record,
                        result=FAIL,
                        reason="",
                        observed=self._line(
                            f"{drawn}; recording {walkthrough.name} was dropped"
                        ),
                    )
                if walkthrough is not None and number - 1 == walkthrough.last:
                    walkthrough.stop(page)
                failed = failed or record.result == FAIL
                self.log.write(record)
                records.append(record)
        finally:
            # A block started in the background whose finish step never ran
            # (an earlier step failed) does not outlive its journey; what it
            # printed until then is kept beside the step that started it.
            for step_id, started in self.background.items():
                ended = commands.abandon(started)
                self._write(
                    f"{journey_id}/{step_id}.abandoned.output.txt", ended.tail()
                )
            self.background = {}
            for walkthrough in walkthroughs:
                walkthrough.stop(page)
                self.recorded[walkthrough.name] = walkthrough.outcome()
            self.walkthrough = None
            video = page.video
            context.close()
            if api is not None:
                api.dispose()
            if request is not None:
                request.dispose()
            if video is not None:
                self._keep_video(video, journey_id)
        return records

    # -- the walkthrough -----------------------------------------------------
    def _drawn(self, page: Page, walkthrough: Walkthrough) -> str:
        """What the step's failure says when the page draws a kept value, or "".

        A drawn value drops the recording and fails the step. A page that
        cannot be read drops the recording only: nothing says a value is on it,
        but nothing can vouch that none is (the presence check then fails the
        run, the walkthrough being missing).
        """
        read = None
        for _ in range(2):
            try:
                read = page.evaluate(DRAWN_JS)
                break
            except PlaywrightError:
                try:
                    page.wait_for_load_state("domcontentloaded")
                except PlaywrightError:
                    pass
        if read is None:
            walkthrough.drop("the page could not be read for kept values", page)
            return ""
        text, fields = read
        where = redaction.on_screen(
            str(text),
            [(str(kind), str(value)) for kind, value in fields],
            self.redactor.values,
        )
        if where is None:
            return ""
        walkthrough.drop(f"a value the run keeps is drawn in {where}", page)
        return f"a value the run keeps is drawn in {where} while it is recorded"

    def _caption(
        self,
        step: Step,
        number: int,
        anchors: Dict[str, str],
        record: Optional[Record] = None,
    ) -> List[str]:
        """The caption bar for *step*: before it runs, or with its result.

        Every line is text the results log also holds (the heading, the action
        and the observed line), and each goes through the run's redactor
        (``_line``) before it is drawn, as the observed line does before it is
        written.
        """
        walkthrough = self.walkthrough
        assert walkthrough is not None
        heading = anchors.get(step.doc) or step.doc
        lines = [f"Step {number} of {walkthrough.total}: {heading}"]
        action = describe_action(step)
        if record is None:
            lines.append(action)
        elif step.do.kind in OFF_SCREEN:
            if record.result == NOT_RUN and record.reason == DOC_EXAMPLES:
                lines.append(f"{action}: run by Doc Examples, not in this walk")
            elif record.observed:
                lines.append(f"{action}: {record.result}, {record.observed}")
            else:
                lines.append(f"{action}: {record.result}")
        else:
            lines.append(f"{action}: {record.result}")
        return [self._line(line, CAPTION_CHARS) for line in lines]

    def _keep_video(self, video, journey_id: str) -> None:
        try:
            source = Path(video.path())
        except PlaywrightError:
            return
        target = self.settings.run_dir / "videos" / f"{journey_id}.webm"
        if source.is_file():
            source.replace(target)
            try:
                source.parent.rmdir()
            except OSError:
                pass

    def _step(
        self,
        journey: Journey,
        journey_id: str,
        number: int,
        step: Step,
        running: Running,
        page: Page,
        api,
        request,
        anchors: Dict[str, str],
        failed: bool,
    ) -> Record:
        expected_snapshot = ""
        if step.expect is not None:
            expected_snapshot = STRUCTURAL_ONLY
            if step.expect.aria is not None:
                expected_snapshot = self._write(
                    self._name(journey_id, number, step, ".expected.aria.yml"),
                    step.expect.aria.rstrip("\n") + "\n",
                )
        elif step.do.crawl == "nav":
            expected_snapshot = self._expected_nav(
                running, self._name(journey_id, number, step, ".expected.json")
            )
        elif step.do.crawl == "links" or step.do.kind == "keep":
            expected_snapshot = STRUCTURAL_ONLY
        elif step.do.evaluations is not None:
            plan = step.do.evaluations
            expected_on = sum(
                population.expected_answer(
                    population.user_id(plan.users, n),
                    plan.flag,
                    plan.reason,
                    plan.rollout,
                )[0]
                for n in range(1, plan.count + 1)
            )
            expected_snapshot = self._write(
                self._name(journey_id, number, step, ".expected.json"),
                json.dumps(
                    {
                        "flag": plan.flag,
                        "count": plan.count,
                        "reason": plan.reason,
                        "rollout": plan.rollout,
                        "expected_enabled": expected_on,
                    },
                    indent=2,
                )
                + "\n",
            )
        elif step.do.sdk is not None:
            plan = step.do.sdk
            enabled, why = population.expected_answer(
                plan.user, plan.flag, "rollout", plan.rollout
            )
            expected_snapshot = self._write(
                self._name(journey_id, number, step, ".expected.json"),
                json.dumps(
                    {
                        "install": f"the first shell block of #{plan.install}, as written",
                        "block": f"the first {plan.language} block of #{plan.snippet}",
                        "replace": dict(plan.replace),
                        "user": plan.user,
                        "variant": "the documented assignment hash of the user under"
                        " the experiment's key and allocations, read at the step",
                        "flag": plan.flag,
                        "rollout": plan.rollout,
                        "enabled": enabled,
                        "reason": why,
                        "answers": {k: list(v) for k, v in plan.answers.items()},
                    },
                    indent=2,
                )
                + "\n",
            )
        elif step.do.traffic is not None:
            chosen = {
                variant: {"assigned": p.assigned, "converted": p.converted}
                for variant, p in step.do.traffic.variants.items()
            }
            expected_snapshot = self._write(
                self._name(journey_id, number, step, ".expected.json"),
                json.dumps(
                    {"event": step.do.traffic.event, "variants": chosen}, indent=2
                )
                + "\n",
            )
        base = {
            "run": self.settings.run_id,
            "sha": self.settings.sha,
            "stack": journey.stack,
            "guide": journey.guide,
            "step": number,
            "step_id": step.id,
            "heading": anchors.get(step.doc) or step.doc,
            "action": describe_action(step),
            "expected": describe_expect(step),
            "failure_signature": step.fail,
            "expected_snapshot": expected_snapshot,
        }
        reason = ""
        if failed:
            reason = registry.EARLIER_STEP_FAILED
        elif step.not_run is not None:
            reason = step.not_run
        elif step.do.kind == "ref":
            reason = registry.DOC_EXAMPLES
        if reason:
            return Record(
                **base, result=NOT_RUN, reason=reason, observed="", snapshot="", ms=0
            )
        started = time.monotonic()
        snapshot = ""
        self.waited = 0.0
        try:
            if step.do.kind == "api":
                observed, snapshot = self._api_step(
                    step, running, api, journey_id, number
                )
            elif step.do.kind == "traffic":
                observed, snapshot = self._traffic_step(
                    step, running, api, journey_id, number
                )
            elif step.do.kind == "evaluations":
                observed, snapshot = self._evaluations_step(
                    step, api, journey_id, number
                )
            elif step.do.kind == "sdk":
                observed, snapshot = self._sdk_step(
                    step, running, api, journey_id, number
                )
            elif step.do.kind == "crawl":
                observed, snapshot = self._crawl_step(
                    step, page, running, request, journey_id, number
                )
            elif step.do.kind == "keep":
                observed = self._keep_step(step, page)
            elif step.do.kind == "run":
                observed, snapshot = self._run_step(step, running, journey_id, number)
            elif step.do.kind == "finish":
                observed, snapshot = self._finish_step(step, journey_id, number)
            else:
                if step.do.kind == "search":
                    observed = self._search_step(step, page, running)
                else:
                    observed = self._browser_step(step, page, running)
                if step.takes_screenshot:
                    snapshot = self._screen(page, journey_id, number, step)
            result = PASS
        except _Failed as failure:
            observed, snapshot, result = failure.observed, failure.snapshot, FAIL
        except (AssertionError, PlaywrightError, KeyError) as error:
            observed = self._line(error) or type(error).__name__
            result = FAIL
            if step.takes_screenshot:
                snapshot = self._screen(page, journey_id, number, step)
            elif step.in_browser:
                snapshot = self._no_screen(journey_id, number, step, observed)
            else:
                suffix = {
                    "crawl": ".crawl.json",
                    "traffic": ".traffic.json",
                    "evaluations": ".evaluations.json",
                    "sdk": ".sdk.json",
                    "run": ".run.json",
                    "finish": ".run.json",
                }.get(step.do.kind, ".api.json")
                snapshot = self._write(
                    self._name(journey_id, number, step, suffix),
                    json.dumps({"error": observed}, indent=2) + "\n",
                )
        ms = int((time.monotonic() - started) * 1000)
        if self.waited:
            observed = f"{observed}; waited {self.waited:.0f} s for the sign-in limit"
        return Record(
            **base,
            result=result,
            reason="",
            observed=self.redactor.redact(observed),
            snapshot=snapshot,
            ms=ms,
        )


class _Failed(Exception):
    def __init__(self, observed: str, snapshot: str):
        super().__init__(observed)
        self.observed = observed
        self.snapshot = snapshot


def first_failure(records: List[Record]) -> Optional[Record]:
    return next((record for record in records if record.result == FAIL), None)
