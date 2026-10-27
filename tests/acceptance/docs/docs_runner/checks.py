"""What a step's log line says, and the comparisons a step makes, without a browser.

Kept apart from ``execute`` (which imports Playwright) so the unit job can test
them: how an action and an expectation are written in the log, how a failure is
cut to one line, and how a JSON value and a shown number are compared.
"""

from __future__ import annotations

import datetime
import json
import re
from typing import Any, Callable, List

from docs_runner.model import SEARCH_TOP, Step
from docs_runner.redaction import MIN_LENGTH

OBSERVED_CHARS = 300
NUMBER = re.compile(r"-?\d[\d,]*(?:\.\d+)?|-?\.\d+")


class StepFailed(AssertionError):
    """The step did not do what the journey expects; the message says what was seen."""


def one_line(text: str, limit: int = OBSERVED_CHARS) -> str:
    """The first lines of an error, before Playwright's call log, on one line."""
    kept = []
    for line in str(text).splitlines():
        if line.strip().lower().startswith("call log"):
            break
        if line.strip():
            kept.append(line.strip())
    flat = "; ".join(kept)
    flat = re.sub(r"\s+", " ", flat)
    return flat if len(flat) <= limit else flat[: limit - 3] + "..."


def describe_action(step: Step) -> str:
    do = step.do
    if do.goto is not None:
        return f"open {do.goto}"
    if do.open is not None:
        return f"open {do.open}, the guide's address"
    if do.click is not None:
        return f'click the {do.click.role} "{do.click.name}"'
    if do.fill is not None:
        if do.fill.secret is not None:
            return f'fill "{do.fill.label}" with the password {do.fill.secret}'
        return f'fill "{do.fill.label}"'
    if do.keep is not None:
        within = do.keep.within
        if within is None:
            return (
                f"keep the text of the {do.keep.role} starting"
                f' "{do.keep.prefix}" as {do.keep.secret}'
            )
        return (
            f"keep the text of the {do.keep.role} in the {within.role}"
            f' "{within.name}" as {do.keep.secret}'
        )
    if do.select is not None:
        return f'choose "{do.select.option}" in "{do.select.label}"'
    if do.api is not None:
        if do.api.key is not None:
            sent = f"{do.api.method} {do.api.path} with the API key {do.api.key}"
        else:
            sent = f"{do.api.method} {do.api.path} as {do.api.as_}"
        if step.poll is not None:
            sent += (
                f", again every {step.poll.every_seconds} s until the expectation"
                f" holds, for up to {step.poll.up_to_seconds} s"
            )
        return sent
    if do.evaluations is not None:
        plan = do.evaluations
        context = f" with context {json.dumps(plan.context)}" if plan.context else ""
        return (
            f"evaluate {plan.flag} for {plan.count} users{context} with the API key"
            f" {plan.key}"
        )
    if do.traffic is not None:
        traffic = do.traffic
        users = sum(p.assigned for p in traffic.variants.values())
        split = "; ".join(
            f"{name} {p.assigned}, {p.converted} sending {traffic.event}"
            for name, p in traffic.variants.items()
        )
        return (
            f"send {users} users through the tracking API with the API key"
            f" {traffic.key} ({split})"
        )
    if do.sdk is not None:
        plan = do.sdk
        return (
            f"install the package as #{plan.install} says, in a fresh project, and"
            f" run the {plan.language} block of #{plan.snippet} for {plan.user}"
            f" with the API key {plan.key}"
        )
    if do.search is not None:
        return f'search the site for "{do.search}"'
    if do.crawl == "nav":
        return "open every page of the nav on the site"
    if do.crawl == "links":
        return "follow every link to the site from the nav pages and their source"
    if do.run is not None:
        first = do.run.command.strip().splitlines()[0]
        more = (
            " (and the block's other lines)" if "\n" in do.run.command.strip() else ""
        )
        how = "start in the background" if do.run.background else "run"
        return (
            f"{how}, from the checkout's root with the venv active, the shell block"
            f" of #{step.doc}: {first}{more}"
        )
    if do.finish is not None:
        return f"let the block step {do.finish} started end, within its bound"
    if do.services is not None:
        plan = do.services
        counted = f", counting the {plan.growing}" if plan.growing else ""
        return (
            f"watch the compose services {', '.join(plan.running)} for"
            f" {plan.for_seconds} s{counted}"
        )
    if do.relaunch is not None:
        return (
            "bring the stack down with its volumes, start it with the page's demo"
            " command less its SEED=, then run the page's command once more, with"
            " no down -v between"
        )
    return "its shell blocks, run by Doc Examples"


#: What each crawl expects; the action itself is the check.
CRAWL_EXPECTS = {
    "nav": (
        "every nav page of the source answers 2xx on the site, and its first-level"
        " heading is the source page's first # heading"
    ),
    "links": (
        "every link to the site on the nav pages, and every absolute link to the site"
        " in their source, answers 2xx, redirected only within the site"
    ),
}


#: What a traffic step expects; the action itself is the check.
TRAFFIC_EXPECTS = (
    "every user answered 200, assigned, with the variant the documented hash"
    " gives it; every event answered 200"
)


def _oracle_call(oracle) -> str:
    args = ", ".join(f"{k}={v}" for k, v in oracle.args.items())
    return f"{oracle.name}({args})"


def describe_expect(step: Step) -> str:
    if step.do.crawl is not None:
        return CRAWL_EXPECTS[step.do.crawl]
    if step.do.keep is not None:
        return (
            f"exactly one {step.do.keep.role} there, holding at least"
            f" {MIN_LENGTH} characters (kept, never written)"
        )

    if step.do.traffic is not None:
        return TRAFFIC_EXPECTS
    if step.do.sdk is not None:
        plan = step.do.sdk
        return (
            "the install exits 0 and installs the version the page names from the"
            " public registry; the block exits 0, prints no kept value, and its"
            f" answers are the variant the documented assignment hash gives"
            f" {plan.user} and the flag answer the documented rollout hash gives at"
            f" {plan.rollout}%"
        )
    if step.do.services is not None:
        plan = step.do.services
        held = (
            "each has one container, running at the start and through the"
            f" {plan.for_seconds} s, its restart count unchanged"
        )
        if plan.growing:
            held += f"; the {plan.growing} are more at the end than at the start"
        return held
    if step.do.relaunch is not None:
        return (
            "after the start without the seeds the apps' key files are absent; the"
            " page's command then exits 0 and the API reports the journey's profile"
        )
    if step.do.run is not None and step.do.run.background:
        return (
            "it starts; a later finish step checks how it ended, within"
            f" {step.do.run.up_to_seconds} s of its start"
        )
    if step.do.evaluations is not None:
        plan = step.do.evaluations
        if plan.reason == "rollout":
            return (
                f"exactly the users the documented rollout hash puts below"
                f" {plan.rollout}% get the flag, each with reason rollout"
            )
        if plan.reason == "targeting_rule":
            return "every user gets the flag, with reason targeting_rule"
        return "no user gets the flag, each with reason inactive"
    expect_ = step.expect
    if expect_ is None:
        return "Doc Examples runs this section's blocks"
    parts: List[str] = []
    if expect_.url is not None:
        parts.append(f"at {expect_.url}")
    if expect_.status is not None:
        parts.append(f"status {expect_.status}")
    for named in expect_.visible or []:
        parts.append(f'{named.role} "{named.name}" visible')
    if expect_.text is not None:
        parts.append(f'text "{expect_.text}"')
    for path, value in (expect_.json_ or {}).items():
        parts.append(f"json {path} = {json.dumps(value)}")
    for path, name in (expect_.after or {}).items():
        parts.append(f"json {path} later than {name}")
    for path, computed in (expect_.computed or {}).items():
        if computed.rel == 0:
            parts.append(f"json {path} = {_oracle_call(computed.oracle)} exactly")
        else:
            parts.append(
                f"json {path} = {_oracle_call(computed.oracle)} within"
                f" {computed.rel:g} of it"
            )
    if expect_.number is not None:
        number = expect_.number
        parts.append(
            f'the number in {number.locator.role} "{number.locator.name}" equals'
            f" {_oracle_call(number.oracle)}"
        )
    for cell in expect_.cells or []:
        parts.append(
            f'the {cell.column} of the row "{cell.row}" shows'
            f" {_oracle_call(cell.oracle)}"
        )
    if expect_.found is not None:
        parts.append(f"{expect_.found} among the first {SEARCH_TOP} results")
    if expect_.aria is not None:
        parts.append(
            f"the screen matches its ARIA snapshot ({len(expect_.aria.splitlines())} lines)"
        )
    if expect_.aria_one_of is not None:
        parts.append(
            f"the screen matches one of its {len(expect_.aria_one_of)} ARIA snapshots"
        )
    if expect_.exit is not None:
        bound = step.do.run.up_to_seconds if step.do.run is not None else None
        within = f" within {bound} s" if bound is not None else ""
        parts.append(f"it ends{within} with exit status {expect_.exit}")
    for line in expect_.output or []:
        parts.append(f'its output has "{line}"')
    return "; ".join(parts)


def json_at(document: Any, path: str) -> Any:
    """The value at a dotted *path* (``items.0.key``); KeyError when absent."""
    node = document
    for part in path.split("."):
        if isinstance(node, list) and part.isdigit() and int(part) < len(node):
            node = node[int(part)]
        elif isinstance(node, dict) and part in node:
            node = node[part]
        else:
            raise KeyError(path)
    return node


def same_json(seen: Any, wanted: Any) -> bool:
    """Equal as JSON values: ``true`` is not ``1``, as it would be in Python."""
    if isinstance(seen, bool) or isinstance(wanted, bool):
        return isinstance(seen, bool) and isinstance(wanted, bool) and seen == wanted
    return seen == wanted


def parse_number(text: str) -> float:
    match = NUMBER.search(text)
    if match is None:
        raise StepFailed(f"no number in {one_line(text, 80)!r}")
    return float(match.group(0).replace(",", ""))


def shown_places(text: str) -> int:
    """How many decimal places the first number in *text* is shown with."""
    match = NUMBER.search(text)
    if match is None:
        raise StepFailed(f"no number in {one_line(text, 80)!r}")
    whole, _, fraction = match.group(0).partition(".")
    return len(fraction)


def agrees(text: str, value: float) -> bool:
    """True when the number shown in *text* is *value* at the precision shown.

    ``0.0153`` agrees with 0.0152993 (four places) and not with 0.0156; an
    integer shown agrees only with that integer. The bound is half a unit of
    the last place shown, so a value rounded either way at the boundary agrees.
    """
    places = shown_places(text)
    shown = parse_number(text)
    return abs(shown - value) <= 0.5 * 10.0 ** (-places) + 1e-12


#: How many times a shown number may change while a step waits for it.
MAX_CHANGES = 20


def agrees_or_not(text: str, value: float) -> bool:
    """``agrees``, with a text that shows no number (``--``, empty) as False."""
    try:
        return agrees(text, value)
    except StepFailed:
        return False


def settle(
    read: Callable[[], str], changed: Callable[[str], bool], value: float
) -> str:
    """The text a shown number settles on: read again while it changes.

    A page that recalculates after an input changes (the Power Calculator
    waits 400 ms) still shows the old number, or none, right after the step.
    So the text is read, and read again each time it changes, until it agrees
    with *value* at the precision shown (``agrees``), or ``changed(text)``
    reports that it stayed *text* for the whole timeout, or it has changed
    ``MAX_CHANGES`` times. Returns the last text read; the caller compares
    it, so a number that settles on a wrong value fails, after one timeout.
    """
    text = read()
    for _ in range(MAX_CHANGES):
        if agrees_or_not(text, value) or not changed(text):
            return text
        text = read()
    return text


def parse_time(value: Any) -> datetime.datetime:
    """An ISO 8601 time from an API answer, with its zone; one without a zone is UTC.

    The API writes both kinds: ``2026-10-07T05:42:19.391716+00:00`` and
    ``datetime.utcnow()``'s ``2026-10-07T05:42:27.348581``. StepFailed for
    anything that is not such a time (a number, null, other text).
    """
    if not isinstance(value, str):
        raise StepFailed(f"{json.dumps(value)} is not a time")
    try:
        parsed = datetime.datetime.fromisoformat(value)
    except ValueError:
        raise StepFailed(f"{one_line(value, 80)!r} is not a time") from None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=datetime.timezone.utc)
    return parsed


def seconds_after(value: Any, since: Any) -> float:
    """How many seconds the time *value* is after the time *since* (negative: before).

    StepFailed when either is not a time (``parse_time``).
    """
    return (parse_time(value) - parse_time(since)).total_seconds()


def shown_seconds(seconds: float) -> str:
    """A number of seconds for a log line: tenths from 1 s up, every digit below.

    ``282.2``, ``0.035``, ``0.000001``: a gap under a second is never shown as
    ``0.0``, which would read as no gap at all.
    """
    if abs(seconds) >= 1:
        return f"{seconds:.1f}"
    return f"{seconds:.6f}".rstrip("0").rstrip(".") or "0"


def within(seen: Any, wanted: float, rel: float) -> bool:
    """True when *seen* is a number (not a boolean) within *rel* of *wanted*."""
    if isinstance(seen, bool) or not isinstance(seen, (int, float)):
        return False
    return abs(seen - wanted) <= rel * abs(wanted)
