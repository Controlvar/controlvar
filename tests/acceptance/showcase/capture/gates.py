"""The capture gates that judge recorded data: pure functions over numbers.

Each answers ``(ok, numbers, why)``; ``why`` is the refusal text when not ok.
They are unit tested on planted inputs (``backend/tests/unit/showcase``).
"""

from __future__ import annotations

import datetime as dt
import json
import math
import re
from decimal import Decimal
from statistics import NormalDist
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Set, Tuple

from showcase.capture import config, guard

Verdict = Tuple[bool, Dict[str, Any], str]


def frame_times(entries: Sequence[Mapping[str, Any]]) -> Verdict:
    """Frames exist and their times strictly increase from 0."""
    times = [int(e["t_ms"]) for e in entries]
    numbers = {"frames": len(times)}
    if not times:
        return False, numbers, "no frame was captured"
    if times[0] < 0 or any(b <= a for a, b in zip(times, times[1:])):
        return False, numbers, "frame times do not strictly increase"
    return True, numbers, ""


def freeze(
    entries: Sequence[Mapping[str, Any]],
    duration_ms: int,
    holds_ms: Sequence[int],
    still: Sequence[Tuple[int, int]],
    *,
    slack_ms: int = config.FREEZE_SLACK_MS,
) -> Verdict:
    """No stretch without a new frame longer than the longest hold plus slack.

    *still* lists spans where a still page is the point (a card scene), which
    are not counted.
    """
    longest_hold = max(holds_ms) if holds_ms else 0
    limit = longest_hold + slack_ms
    times = [int(e["t_ms"]) for e in entries] + [duration_ms]
    worst = (0, 0, 0)
    for a, b in zip(times, times[1:]):
        # The part of the gap outside every still span is what counts.
        covered = sum(max(0, min(b, e) - max(a, s)) for s, e in still)
        gap = (b - a) - covered
        if gap > worst[0]:
            worst = (gap, a, b)
    numbers = {"longest_gap_ms": worst[0], "limit_ms": limit}
    if worst[0] > limit:
        return (
            False,
            numbers,
            f"no new frame for {worst[0]} ms ({worst[1]}-{worst[2]} ms); the longest"
            f" hold is {longest_hold} ms",
        )
    return True, numbers, ""


def black(
    entries: Sequence[Mapping[str, Any]],
    lumas: Sequence[float],
    duration_ms: int,
    still: Sequence[Tuple[int, int]],
    *,
    threshold: float = config.BLACK_LUMA,
    limit_ms: int = config.BLACK_MS,
) -> Verdict:
    """No dark stretch longer than *limit_ms* outside *still* spans."""
    if len(lumas) != len(entries):
        return (
            False,
            {"frames": len(entries), "read": len(lumas)},
            "not every frame was read",
        )
    times = [int(e["t_ms"]) for e in entries] + [duration_ms]
    run_from: Optional[int] = None
    longest = 0
    for index, luma in enumerate(lumas):
        t = times[index]
        in_still = any(s <= t < e for s, e in still)
        if luma < threshold and not in_still:
            if run_from is None:
                run_from = t
            longest = max(longest, times[index + 1] - run_from)
        else:
            run_from = None
    numbers = {
        "longest_dark_ms": longest,
        "limit_ms": limit_ms,
        "darkest": round(min(lumas), 1) if lumas else None,
    }
    if longest > limit_ms:
        return False, numbers, f"the page is dark for {longest} ms"
    return True, numbers, ""


def payoff_still(
    entries: Sequence[Mapping[str, Any]], payoffs: Sequence[Mapping[str, Any]]
) -> Verdict:
    """U5: no new frame during a payoff hold (frames arrive only on change)."""
    worst = []
    for hold in payoffs:
        inside = [
            int(e["t_ms"])
            for e in entries
            if hold["from_ms"] + 100 < int(e["t_ms"]) < hold["to_ms"]
        ]
        worst.append(
            {
                "where": hold["where"],
                "ms": hold["to_ms"] - hold["from_ms"],
                "frames": len(inside),
            }
        )
    numbers = {"payoffs": worst}
    short = [w for w in worst if w["ms"] < config.PAYOFF_MS]
    moving = [w for w in worst if w["frames"]]
    if not payoffs:
        return False, numbers, "the storyboard has no payoff hold"
    if short:
        return False, numbers, f"{short[0]['where']}: payoff held {short[0]['ms']} ms"
    if moving:
        return (
            False,
            numbers,
            f"{moving[0]['where']}: the page changed during the payoff",
        )
    return True, numbers, ""


#: How far the computed zoom may be from ``config.ZOOM`` (CSS keeps 7 digits).
ZOOM_TOLERANCE = 1e-4


def zoom(measured: Mapping[str, Any], wanted: float = config.ZOOM) -> Verdict:
    """C1: <body> has *wanted* (the origin's zoom) as Chromium computed it, and
    <html> has none."""
    body, html = measured.get("body"), measured.get("html")
    numbers = {"body": body, "html": html, "wanted_body": round(wanted, 6)}
    if not all(isinstance(v, (int, float)) for v in (body, html)):
        return False, numbers, f"the page's zoom could not be read ({dict(measured)})"
    if abs(html - 1) > ZOOM_TOLERANCE:
        return False, numbers, f"<html> is zoomed {html:g}; the zoom belongs on <body>"
    if abs(body - wanted) > ZOOM_TOLERANCE:
        return (
            False,
            numbers,
            f"<body> is zoomed {body:g}, not {wanted:.6f}: the frames would not"
            " be the layout the storyboard was written for",
        )
    return True, numbers, ""


def needle_count(
    static: Sequence[str], kept: Iterable[str], required: Iterable[str] = ()
) -> Verdict:
    """Gate 6's count: every static needle, plus each kept value, exactly.

    *required* are values the run read itself and must be looking for (the
    StreamPulse server key, read from its file): each must be a kept value, as
    read, with nothing around it.
    """
    static_set = set(static)
    kept_set = set(kept) - static_set
    total = len(static_set | kept_set)
    wanted = list(required)
    numbers = {
        "static": len(static_set),
        "kept": len(kept_set),
        "total": total,
        "required": len(wanted),
    }
    if total == 0:
        return False, numbers, "0 needles: refusing to scan"
    if len(static_set) != len(config.STATIC_NEEDLES) or not static_set <= set(
        config.STATIC_NEEDLES
    ):
        return False, numbers, "the static needles are not the four the tool carries"
    if total != len(static_set) + len(kept_set):
        return False, numbers, "needle count does not add up"
    for value in wanted:
        if value != value.strip() or not value:
            return False, numbers, "a key read from a file was kept with whitespace"
        if value not in kept_set:
            return False, numbers, "a key read from a file is not a needle"
    return True, numbers, ""


def child_environments(started: Sequence[Mapping[str, Any]]) -> Verdict:
    """EM condition 10: every child's environment names only its kind's keys.

    Each child is judged against the allow-list of its own kind
    (``guard.allowed_env_keys``), not the union of every kind's.
    """
    extra: Dict[str, List[str]] = {}
    for child in started:
        kind = str(child.get("kind"))
        try:
            keys = set(guard.allowed_env_keys(kind))
        except guard.Refused:  # an unknown kind may be given nothing at all
            keys = set()
        unexpected = sorted(set(child["env_keys"]) - keys)
        if unexpected:
            extra[f"{child['name']} ({kind})"] = unexpected
    numbers = {"children": len(started), "unexpected": extra}
    if extra:
        name, keys = next(iter(extra.items()))
        return False, numbers, f"{name} was started with {', '.join(keys)}"
    return True, numbers, ""


def allowed_by_kind() -> Dict[str, Set[str]]:
    """Each kind of child's allow-list, for the manifest and the tests."""
    return {kind: set(guard.allowed_env_keys(kind)) for kind in guard.KINDS}


def app_answers(app: Mapping[str, Mapping[str, Any]]) -> Verdict:
    """P2 for the app scenes: per app, after it was armed, no answer >= 400 from
    the stack, no failed request, and no request left unanswered at a scene's end."""
    numbers = {name: dict(counts) for name, counts in app.items()}
    for name, counts in app.items():
        for what in ("answers_400", "failed", "unanswered"):
            found = counts.get(what) or []
            if found:
                return False, numbers, f"{name}: {what.replace('_', ' ')}: {found[0]}"
    return True, numbers, ""


def caption_numbers(caption: str, shown: str) -> Verdict:
    """U2: every number a caption states is one the page shows in its focus.

    Compared as written (``31,234`` is not ``31234``), number for number, so a
    caption's ``13`` is not found inside ``31,234``.
    """
    from showcase.capture.storyboard import numbers_in

    said = numbers_in(caption)
    seen = numbers_in(shown)
    missing = [n for n in said if n not in seen]
    numbers = {"caption": said, "page": seen[:20]}
    if missing:
        return (
            False,
            numbers,
            f"the caption says {missing[0]}; the page shows {', '.join(seen[:12]) or 'no number'}",
        )
    return True, numbers, ""


def spoken_numbers(sentence: str, at_start: str, before_exit: str) -> Verdict:
    """G2: every number a narration sentence says is on the page at its beat's
    start or at its end, before the exit (the page's visible text and field
    values, as U4 reads them, at those two instants).

    Compared by ``narration.number_key``: as written, except that ``15.0%`` is
    ``15%`` and ``31,234`` is ``31234``. ``numbers["shown_at"]`` records which
    instant showed each number, for the manifest.
    """
    from showcase.capture.narration import number_key
    from showcase.capture.storyboard import numbers_in

    start = {number_key(n) for n in numbers_in(at_start)}
    end = {number_key(n) for n in numbers_in(before_exit)}
    said = numbers_in(sentence)
    shown_at: Dict[str, str] = {}
    missing = []
    for number in said:
        key = number_key(number)
        instants = [w for w, seen in (("start", start), ("end", end)) if key in seen]
        if instants:
            shown_at[number] = " and ".join(instants)
        else:
            missing.append(number)
    numbers = {"said": said, "shown_at": shown_at}
    if missing:
        first = ", ".join(numbers_in(at_start)[:12]) or "no number"
        last = ", ".join(numbers_in(before_exit)[:12]) or "no number"
        return (
            False,
            numbers,
            f"the narration says {missing[0]}; the page shows {first} at the beat's"
            f" start and {last} before its exit",
        )
    return True, numbers, ""


def narration_tally(checks: int, sentences: int) -> Verdict:
    """G2's count: one number check for every narration sentence, exactly."""
    numbers = {"narration_checks": checks, "sentences": sentences}
    if checks != sentences:
        return (
            False,
            numbers,
            f"{checks} narration number checks for {sentences} sentences",
        )
    return True, numbers, ""


def _arms(variants: Sequence[Mapping[str, Any]]) -> Dict[str, Dict[str, int]]:
    """A results API metric's variants as ``{arm: {users, converting_users}}``."""
    arms: Dict[str, Dict[str, int]] = {}
    for variant in variants:
        arm = "control" if variant.get("is_control") else "treatment"
        arms[arm] = {
            "users": int(variant.get("sample_size") or 0),
            "converting_users": int(variant.get("conversions") or 0),
        }
    return arms


Split = Mapping[str, Mapping[str, int]]


def no_shoppers() -> Dict[str, Dict[str, int]]:
    return {
        "control": {"users": 0, "converting_users": 0},
        "treatment": {"users": 0, "converting_users": 0},
    }


def with_shoppers(split: Split, added: Split) -> Dict[str, Dict[str, int]]:
    """*split* plus the on-camera shoppers' users and converting users, per arm."""
    out: Dict[str, Dict[str, int]] = {
        arm: dict(counts) for arm, counts in split.items()
    }
    for arm, counts in added.items():
        if not counts.get("users"):
            continue
        mine = out.setdefault(arm, {"users": 0, "converting_users": 0})
        mine["users"] = mine.get("users", 0) + counts["users"]
        mine["converting_users"] = mine.get("converting_users", 0) + counts.get(
            "converting_users", 0
        )
    return out


def shoppers_end(
    end: Any,
    *,
    event: str,
    assigns: Mapping[Tuple[str, str], Sequence[Mapping[str, Any]]],
    api_assignments: Mapping[str, Optional[Sequence[Mapping[str, Any]]]],
    marks: Mapping[str, Sequence[str]],
    events: Sequence[Mapping[str, Any]],
) -> Tuple[bool, Dict[str, Any], str, Dict[str, Dict[str, int]]]:
    """Gate 10/6 (S8b of plan v2.2): each on-camera shopper's arm, four ways.

    For every shopper the storyboard declares (``ExperimentEnd.on_camera_shoppers``):

    * the page's assign answers for the experiment's key (the network: every
      one ``assigned``, the declared arm, never another);
    * ``GET /tracking/assignments/<visitor>`` (P24): one row for the
      experiment, the declared arm;
    * the page: of the declared testids, it drew the arm's, and no other;
    * the purchase: a shopper who buys sent the experiment's *event* for the
      key, answered 2xx; one who does not sent none.

    The declared arm is the oracle's, never read from the app's own text, so
    a page that agrees with itself and not with the server is refused.
    Returns the verdict, its numbers, why, and the shoppers' users and
    converting users per arm, which gate 10 adds to the generator's.
    """
    added = no_shoppers()
    seen_all = {shopper.drawn for shopper in end.on_camera_shoppers}
    numbers: Dict[str, Any] = {"shoppers": []}
    for shopper in end.on_camera_shoppers:
        who = f"shopper {shopper.visitor}"
        arm = "treatment" if shopper.arm == end.winner else "control"
        answers = list(assigns.get((shopper.visitor, end.key)) or [])
        rows = [
            row
            for row in api_assignments.get(shopper.visitor) or []
            if row.get("experiment_name") == end.name
        ]
        drawn = [m for m in marks.get(shopper.visitor) or [] if m in seen_all]
        sent = [
            e
            for e in events
            if e.get("user_id") == shopper.visitor
            and e.get("experiment_key") == end.key
            and e.get("event_name") == event
        ]
        entry = {
            "visitor": shopper.visitor,
            "declared": shopper.arm,
            "network": sorted({str(a.get("variant_name")) for a in answers}),
            "api": [row.get("variant_name") for row in rows],
            "drawn": drawn,
            "events": [e.get("status") for e in sent],
        }
        numbers["shoppers"].append(entry)
        if not answers:
            return (
                False,
                numbers,
                f"{who}: the storefront never asked for {end.key} (no assign answer)",
                added,
            )
        if (
            entry["network"] != [shopper.arm]
            or not all(a.get("assigned") is True for a in answers)
            or any(bool(a.get("is_control")) != (arm == "control") for a in answers)
        ):
            return (
                False,
                numbers,
                f"{who}: the storefront was assigned {entry['network']} (POST"
                f" /tracking/assign); the storyboard declares {shopper.arm!r}",
                added,
            )
        if entry["api"] != [shopper.arm]:
            return (
                False,
                numbers,
                f"{who}: GET /tracking/assignments says {entry['api']} for"
                f" {end.name!r}; the storyboard declares {shopper.arm!r}",
                added,
            )
        if drawn != [shopper.drawn]:
            return (
                False,
                numbers,
                f"{who}: the page drew {drawn}; the storyboard draws"
                f" {shopper.drawn!r} for {shopper.arm!r}",
                added,
            )
        answered = [e for e in sent if 200 <= int(e.get("status") or 0) < 300]
        if shopper.buys and not answered:
            return (
                False,
                numbers,
                f"{who} buys on camera, and no {event!r} for {end.key} was answered"
                f" 2xx ({entry['events'] or 'none sent'})",
                added,
            )
        if not shopper.buys and sent:
            return (
                False,
                numbers,
                f"{who} does not buy, and sent {len(sent)} {event!r} for {end.key}",
                added,
            )
        added[arm]["users"] += 1
        added[arm]["converting_users"] += 1 if shopper.buys else 0
    numbers["added"] = added
    return True, numbers, "", added


def experiment_end(
    end: Any,
    *,
    named: Sequence[Mapping[str, Any]],
    results: Optional[Mapping[str, Any]],
    planned: Mapping[str, Mapping[str, int]],
    report: Mapping[str, Mapping[str, int]],
    oracle_p: Optional[float],
    shown: Optional[str],
    sample_size: Optional[Mapping[str, Any]],
    planned_shown: Optional[str],
    effect_shown: Optional[str],
    shoppers: Optional[Tuple[bool, Dict[str, Any], str, Split]] = None,
) -> Verdict:
    """Gate 10 for an experiment the video made (``storyboard.ExperimentEnd``).

    * exactly one experiment has the name, with the storyboard's key, running;
    * its on-camera shoppers, if it has any, are each in their declared arm
      every way it can be read (``shoppers_end``, given as *shoppers*);
    * its users and converting users per variant are the same three times:
      as planned from the seed, as the server answered the traffic, and as the
      results API counts them, the first two with the shoppers added;
    * the results API's p-value is Fisher's exact test on those counts
      (*oracle_p*), and it recommends shipping the storyboard's winner;
    * the page showed the API's recommendation, word for word;
    * the Planned sample is planned as the wizard sized it (``planned_sample_end``).
    """
    added = no_shoppers()
    if end.on_camera_shoppers or shoppers is not None:
        if shoppers is None:
            return False, {}, "the on-camera shoppers were never checked"
        if not shoppers[0]:
            return False, {"shoppers": shoppers[1]}, shoppers[2]
        added = dict(shoppers[3])
    planned = with_shoppers(planned, added)
    report = with_shoppers(report, added)
    numbers: Dict[str, Any] = {
        "experiments_named": len(named),
        "planned": dict(planned),
        "answered": dict(report),
    }
    if shoppers is not None:
        numbers["shoppers"] = shoppers[1]
    if len(named) != 1:
        return False, numbers, f"{len(named)} experiments are named {end.name!r}"
    found = named[0]
    numbers.update(key=found.get("key"), status=found.get("status"))
    if found.get("key") != end.key or str(found.get("status")).lower() != end.status:
        return (
            False,
            numbers,
            f"{end.name!r} has key {found.get('key')!r} and status"
            f" {found.get('status')!r}, not {end.key!r} and {end.status!r}",
        )
    if results is None:
        return False, numbers, "the results API gave no answer"
    metrics = results.get("metrics") or []
    primary = next((m for m in metrics if m.get("is_primary")), None)
    if primary is None:
        return False, numbers, "the results have no primary metric"
    counted = _arms(primary.get("variants") or [])
    numbers["counted"] = counted
    if not (dict(planned) == dict(report) == counted):
        return (
            False,
            numbers,
            f"users and conversions differ: planned {dict(planned)}, answered"
            f" {dict(report)}, counted by the results API {counted}",
        )
    treatment = next(
        (v for v in primary.get("variants") or [] if not v.get("is_control")), {}
    )
    p_value = treatment.get("p_value")
    numbers.update(p_value=p_value, oracle_p_value=oracle_p)
    if (
        p_value is None
        or oracle_p is None
        or abs(float(p_value) - oracle_p) > 1e-9 * max(1.0, oracle_p)
    ):
        return (
            False,
            numbers,
            f"the API's p-value {p_value} is not Fisher's {oracle_p} on these counts",
        )
    summary = results.get("summary") or {}
    winner = next(
        (
            v.get("variant_name")
            for v in primary.get("variants") or []
            if v.get("variant_id") == summary.get("winning_variant_id")
        ),
        None,
    )
    reason = str(summary.get("recommendation_reason") or "")
    numbers.update(
        recommendation=summary.get("recommendation"), winner=winner, reason=reason
    )
    if summary.get("recommendation") != "SHIP_VARIANT" or winner != end.winner:
        return (
            False,
            numbers,
            f"the API recommends {summary.get('recommendation')} with winner"
            f" {winner!r}; the storyboard ends on {end.winner!r} shipped",
        )
    on_page = " ".join((shown or "").split())
    numbers["shown"] = on_page
    if not reason or on_page != " ".join(reason.split()):
        return (
            False,
            numbers,
            f"the page showed {on_page!r}; the API says {reason!r}",
        )
    ok, numbers["planned_sample"], why = planned_sample_end(
        end,
        answer=sample_size,
        control=counted.get("control") or {},
        planned_shown=planned_shown,
        effect_shown=effect_shown,
    )
    if not ok:
        return False, numbers, why
    return True, numbers, ""


#: The plan the oracle sizes: the experiment's 95% confidence (one comparison,
#: so no correction moves it) and the sample-size API's default 80% power.
PLAN_ALPHA = 0.05
PLAN_POWER = 0.80


def planned_sample_oracle(
    baseline: float, mde: float, *, alpha: float = PLAN_ALPHA, power: float = PLAN_POWER
) -> int:
    """Users per variant for a two-sided two-proportion z-test (Fleiss): the
    pooled variance under the null, unpooled under the alternative, rounded
    up. The standard library's normal quantiles, not the API's code."""
    treatment = baseline * (1.0 + mde)
    pooled = (baseline + treatment) / 2.0
    z_alpha = NormalDist().inv_cdf(1.0 - alpha / 2.0)
    z_power = NormalDist().inv_cdf(power)
    root = z_alpha * math.sqrt(2.0 * pooled * (1.0 - pooled)) + z_power * math.sqrt(
        baseline * (1.0 - baseline) + treatment * (1.0 - treatment)
    )
    return math.ceil(root**2 / (treatment - baseline) ** 2)


def _pct(value: float) -> str:
    """The results page's ``pct``: two decimals, trailing zeros dropped."""
    text = f"{value * 100:.2f}"
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return f"{text}%"


def planned_card(answer: Mapping[str, Any]) -> Tuple[str, str, str]:
    """What the results page draws for a sample-size answer: the Planned sample
    card's value and detail, and the effect line under it
    (``plannedSampleStatus`` and ``plannedEffectSentence``,
    frontend/src/components/results/ResultsDashboard/ExperimentSummary.tsx)."""
    current = int(answer.get("current_sample_size_per_variant") or 0)
    required = answer.get("required_sample_size_per_variant")
    if required is None:
        value, detail = "Not yet", f"{current:,} per variant, no plan yet"
    else:
        detail = f"{current:,} of {int(required):,} per variant"
        reached = bool(answer.get("is_adequate")) and current >= int(required)
        value = "Reached" if reached else "Not yet"
    source = answer.get("mde_source") or "default"
    effect = _pct(float(answer.get("mde") or 0))
    rate = answer.get("baseline_rate")
    baseline = ""
    if required is not None and rate is not None:
        if answer.get("baseline_source") == "observed":
            baseline = f" on the control's {_pct(float(rate))}"
        else:
            baseline = f" on a {_pct(float(rate))} baseline"
    if source == "experiment":
        line = (
            f"To detect a {effect} lift{baseline}. The {effect} was set when the"
            " experiment was created."
        )
    elif source == "request":
        line = f"To detect a {effect} lift{baseline}."
    else:
        line = (
            f"To detect a {effect} lift (the default: no effect was saved with this"
            f" experiment){baseline}."
        )
    return value, detail, line


def planned_sample_end(
    end: Any,
    *,
    answer: Optional[Mapping[str, Any]],
    control: Mapping[str, int],
    planned_shown: Optional[str],
    effect_shown: Optional[str],
) -> Verdict:
    """Gate 10's Planned sample (#1257): the sample-size API plans with the
    effect typed in the wizard (``end.mde``, ``mde_source`` "experiment") on
    the control's observed rate; its size is the oracle's for those two; and
    the page drew the card and the effect line for that answer, word for word,
    reading Reached."""
    numbers: Dict[str, Any] = {"mde_typed": end.mde}
    if not answer:
        return False, numbers, "the sample-size API gave no answer"
    mde, source = answer.get("mde"), answer.get("mde_source")
    required = answer.get("required_sample_size_per_variant")
    rate = answer.get("baseline_rate")
    numbers.update(
        mde=mde,
        mde_source=source,
        required=required,
        current=answer.get("current_sample_size_per_variant"),
        baseline_rate=rate,
        baseline_source=answer.get("baseline_source"),
    )
    if source != "experiment" or mde is None or abs(float(mde) - end.mde) > 1e-9:
        return (
            False,
            numbers,
            f"the Planned sample plans for an effect of {mde} ({source}), not the"
            f" {end.mde} typed in the wizard and saved with the experiment",
        )
    users = int(control.get("users") or 0)
    observed = control.get("converting_users", 0) / users if users else None
    numbers["observed_rate"] = observed
    if (
        answer.get("baseline_source") != "observed"
        or observed is None
        or rate is None
        or abs(float(rate) - observed) > 1e-12
    ):
        return (
            False,
            numbers,
            f"the Planned sample starts from {rate} ({answer.get('baseline_source')}),"
            f" not the control's observed {observed}",
        )
    oracle = planned_sample_oracle(observed, end.mde)
    numbers["oracle_required"] = oracle
    if required != oracle:
        return (
            False,
            numbers,
            f"the API plans {required} users per variant; {oracle} detect a"
            f" {_pct(end.mde)} lift on the control's {_pct(observed)}",
        )
    value, detail, line = planned_card(answer)
    card = f"Planned sample: {value} · {detail}"
    numbers.update(card=card, effect=line)
    lines = [
        part.strip() for part in (planned_shown or "").splitlines() if part.strip()
    ]
    numbers["shown"] = lines
    drawn = (
        len(lines) == 3
        and lines[0].casefold() == "planned sample"
        and lines[1:] == [value, detail]
    )
    if not drawn:
        return (
            False,
            numbers,
            f"the page's Planned sample read {' · '.join(lines)!r}; for the API's"
            f" answer it reads {card!r}",
        )
    if value != "Reached":
        return False, numbers, f"the Planned sample reads {card!r}, not Reached"
    on_page = " ".join((effect_shown or "").split())
    numbers["effect_shown"] = on_page
    if on_page != line:
        return (
            False,
            numbers,
            f"the page's effect line read {on_page!r}; for the API's answer it"
            f" reads {line!r}",
        )
    return True, numbers, ""


def calculation_end(made: Any, *, shown: Optional[str]) -> Verdict:
    """Gate 10/5: a sample size the power calculator drew (``storyboard.Calculation``).

    * the storyboard's ``per_variant`` is the textbook formula's for its inputs
      (``planned_sample_oracle``: two-sided, the calculator's own 80% power and
      95% confidence, with its Bonferroni correction over the ``variants - 1``
      comparisons), so a number the video says is computed, not copied;
    * the page drew that number, word for word, where the beat recorded it.
    """
    alpha = PLAN_ALPHA / (made.variants - 1)
    oracle = planned_sample_oracle(made.baseline, made.mde, alpha=alpha)
    drawn = " ".join((shown or "").split())
    numbers: Dict[str, Any] = {
        "inputs": {
            "baseline": made.baseline,
            "mde": made.mde,
            "variants": made.variants,
            "alpha": alpha,
            "power": PLAN_POWER,
        },
        "storyboard": made.per_variant,
        "oracle": oracle,
        "shown": drawn,
    }
    if oracle != made.per_variant:
        return (
            False,
            numbers,
            f"the storyboard says {made.per_variant:,} users per variant for"
            f" {made.variants} variants; the formula gives {oracle:,}",
        )
    if drawn != f"{made.per_variant:,}":
        return (
            False,
            numbers,
            f"the calculator drew {drawn!r}; for {made.variants} variants it draws"
            f" {made.per_variant:,}",
        )
    return True, numbers, ""


#: How the users table names each role (frontend/src/types/admin.ts
#: ``USER_ROLE_LABELS``; a unit test keeps them equal).
ROLE_LABELS: Dict[str, str] = {
    "ADMIN": "Admin",
    "DEVELOPER": "Developer",
    "ANALYST": "Analyst",
    "VIEWER": "Viewer",
}


def _who(user: Mapping[str, Any]) -> Tuple[Any, ...]:
    """What gate 10/5 compares of an account: role, superuser, active."""
    return (user.get("role"), bool(user.get("is_superuser")), user.get("is_active"))


def team_end(
    team: Any,
    *,
    before: Sequence[Mapping[str, Any]],
    after: Sequence[Mapping[str, Any]],
    shown: Optional[str],
) -> Verdict:
    """Gate 10/5 (S8c of plan v2.2): the video invited exactly these teammates.

    * every account there before the video is there after it, unchanged;
    * the accounts added are the invitees, each once by email: none missing
      and none other;
    * each has the role it was invited with, is active, and is a superuser
      only when invited as an Admin (the dialog makes an Admin one);
    * the users table showed them: its top rows, newest first, each with the
      invitee's email and role, as the API lists them.
    """
    by_email = {str(u.get("email")): u for u in after}
    old = {str(u.get("email")): u for u in before}
    added = [u for u in after if str(u.get("email")) not in old]
    wanted = [invitee.email for invitee in team.invitees]
    numbers: Dict[str, Any] = {
        "users_before": len(before),
        "users_after": len(after),
        "added": sorted(str(u.get("email")) for u in added),
        "invitees": wanted,
    }
    changed = sorted(
        email
        for email, user in old.items()
        if email not in by_email or _who(by_email[email]) != _who(user)
    )
    if changed:
        return False, numbers, f"accounts there before the video changed: {changed}"
    if sorted(numbers["added"]) != sorted(wanted) or len(added) != len(wanted):
        return (
            False,
            numbers,
            f"the video added {numbers['added']}; the storyboard invites {wanted}",
        )
    roles = []
    for invitee in team.invitees:
        user = by_email[invitee.email]
        got = _who(user)
        roles.append({"email": invitee.email, "role": got[0], "superuser": got[1]})
        want = (invitee.role, invitee.role == "ADMIN", True)
        if got != want:
            numbers["roles"] = roles
            return (
                False,
                numbers,
                f"{invitee.email} is {got[0]!r} (superuser {got[1]}, active"
                f" {got[2]}); the storyboard invites them as {invitee.role!r}",
            )
    numbers["roles"] = roles
    rows = audit_rows(shown or "")
    expected = [
        [invitee.email, ROLE_LABELS[invitee.role]]
        for invitee in reversed(list(team.invitees))
    ]
    drawn = [row[1:3] for row in rows[: len(expected)]]
    numbers["shown_rows"] = drawn
    if drawn != expected:
        return (
            False,
            numbers,
            f"the users table's top rows showed {drawn}; the API lists {expected},"
            " newest first",
        )
    return True, numbers, ""


def select_hold(option: str, shown: str, held: str, held_ms: int) -> Verdict:
    """C9: a ``choose`` step's closed select showed the option at once, and still
    showed it ``config.SETTLE_MS`` later."""
    numbers = {"option": option, "shown": shown, "held": held, "held_ms": held_ms}
    if shown != option:
        return False, numbers, f"the select shows {shown!r}, not {option!r}"
    if held != option:
        return False, numbers, f"the select went back to {held!r} during its hold"
    if held_ms < config.SETTLE_MS:
        return (
            False,
            numbers,
            f"the select was held {held_ms} ms on {option!r}; at least"
            f" {config.SETTLE_MS} ms",
        )
    return True, numbers, ""


def typed_as_shown(
    text: str,
    before: str,
    after: str,
    *,
    replaced: bool,
    secret: bool,
    kept: Iterable[str] = (),
) -> Verdict:
    """Gate 12 (typed as shown): after a ``type`` step the field reads exactly
    what the storyboard typed.

    That is *text* when the step replaced the value (``clear`` or ``select``),
    and otherwise the value *before* the step followed by *text*. A field that
    reads anything else refuses the take: "034" after 34 was typed into an
    emptied number box, "5034" when a selection did not take, or text put in
    the middle of a value. The numbers and the refusal always carry the
    lengths, and carry the values only when the step is not secret and none of
    them holds a *kept* value (a needle).
    """
    expected = text if replaced else before + text
    hidden = secret or any(
        value and (value in text or value in before or value in after) for value in kept
    )
    numbers: Dict[str, Any] = {
        "replaced": replaced,
        "secret": secret,
        "typed_chars": len(text),
        "before_chars": len(before),
        "after_chars": len(after),
        "expected_chars": len(expected),
        "values_shown": not hidden,
    }
    if not hidden:
        numbers.update(typed=text, before=before, after=after)
    if after == expected:
        return True, numbers, ""
    if hidden:
        return (
            False,
            numbers,
            f"the field reads {len(after)} characters after {len(text)} were typed;"
            f" it should read {len(expected)} (values not shown)",
        )
    return (
        False,
        numbers,
        f"the field reads {after!r} after {text!r} was typed; it should read"
        f" {expected!r}",
    )


#: A ``datetime-local`` input's value, to the minute, as the page computes it.
LOCAL_MINUTE = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T([0-9]{2}:[0-9]{2})$")


def date_as_set(at: str, value: str, read: str, read_back: str) -> Verdict:
    """Gate 12 for a ``date`` step: the page computed a day at the storyboard's
    time of day *at* (*value*, local, to the minute), the field reads exactly
    *value* after it was filled (*read*), and the page reads that local time
    back as itself (*read_back*: ``new Date(value)`` formatted the same way).

    A time a change of daylight saving time skips is moved by the browser, so
    it never reads back as itself: refused, never filmed as another hour."""
    numbers = {"at": at, "value": value, "read": read, "read_back": read_back}
    shaped = LOCAL_MINUTE.match(value or "")
    if shaped is None or shaped.group(1) != at:
        return (
            False,
            numbers,
            f"the page computed {value!r}, which is not a day at {at}",
        )
    if read != value:
        return False, numbers, f"the field reads {read!r} after {value!r} was filled"
    if read_back != value:
        return (
            False,
            numbers,
            f"the page reads {value!r} back as {read_back!r}: that local time does"
            " not exist in the browser's zone",
        )
    return True, numbers, ""


#: How the audit log page names the actions a flag's end state checks
#: (frontend/src/components/admin/audit/actionLabels.ts; a unit test keeps
#: them equal).
AUDIT_LABELS: Dict[str, str] = {
    "feature_flag_create": "Flag created",
    "feature_flag_update": "Flag changed",
    "toggle_enable": "Flag turned on",
    "toggle_disable": "Flag turned off",
}
#: ...and the entity a flag is.
FLAG_ENTITY_LABEL = "Feature flag"


def audit_rows(text: str) -> List[List[str]]:
    """The audit table's body text as rows of cells (tab-separated, as drawn)."""
    return [
        [cell.strip() for cell in line.split("\t")]
        for line in text.splitlines()
        if line.strip()
    ]


#: How the audit log page names the safety monitor and its rollback
#: (frontend/src/components/admin/audit/actionLabels.ts; a unit test keeps
#: them equal), and the user the API records for it. The page draws a system
#: actor's entry, one with no user, as its name and "(automatic)"
#: (``actorLabel``): the User cell of the monitor's rollback.
SAFETY_MONITOR = "system:safety-monitor"
SAFETY_MONITOR_LABEL = "Safety monitor (automatic)"
ROLLBACK_LABEL = "Safety rollback"
#: How the safety card and the schedule's badge read (frontend/src/pages/
#: feature-flags/[id].tsx: the badge's text, ``humanize`` of the status).
UNHEALTHY_LABEL = "Unhealthy"
PAUSED_LABEL = "Paused"
#: The start of the reason the safety monitor writes for an error-rate breach
#: (backend/app/core/safety_scheduler.py).
ROLLBACK_REASON = "Automatic rollback due to error_rate"


def error_rate_metric(check: Any) -> Optional[Mapping[str, Any]]:
    """A flag's safety check answer's ``error_rate`` metric, or None.

    Its ``current_value`` is the rate the flag page draws in its safety card's
    metric row (``safety-metric``: the value, then "/" and the threshold). The
    answer's ``details`` hold the counts behind the rate, not the rate
    (``SafetyService._error_window``)."""
    if not isinstance(check, Mapping):
        return None
    for metric in check.get("metrics") or []:
        if isinstance(metric, Mapping) and metric.get("name") == "error_rate":
            return metric
    return None


def safety_error_rate(check: Any) -> Optional[float]:
    """The error rate a safety check answered (``error_rate_metric``), or None."""
    metric = error_rate_metric(check)
    value = None if metric is None else metric.get("current_value")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def rate_reading(value: float) -> str:
    """An error rate as the platform writes it in a rollback reason: four
    significant figures as a percentage, never in exponent form, 40/240 reads
    ``16.67%`` (``format_metric_reading`` in backend/app/services/
    safety_service.py; a unit test keeps the two equal)."""
    return f"{format(Decimal(f'{value * 100:.4g}'), 'f')}%"


#: The rate a safety rollback's reason gives for its breach, "...above
#: threshold (16.67% > 5%)" (``describe_breach``).
_REASON_RATE = re.compile(r"\(([0-9][0-9.]*%) [<>=] [0-9][0-9.]*%\)")


def reason_rate(reason: Optional[str]) -> Optional[str]:
    """The one rate a rollback's reason gives, as written; None for none or several."""
    found = _REASON_RATE.findall(reason or "")
    return found[0] if len(found) == 1 else None


def panel_says(text: Optional[str]) -> Optional[bool]:
    """What StreamPulse's panel row says of a flag (its reason line,
    demo/streampulse/src/lib/labels.ts): on, off, or None for anything else
    (loading, an error)."""
    line = (text or "").strip()
    if line.startswith("on:") or line == "on":
        return True
    if line.startswith("off:") or line == "off":
        return False
    return None


def phone_says(text: Optional[str]) -> Optional[bool]:
    """What StreamPulse's phone draws on its Player screen
    (demo/streampulse/src/components/screens/PlayerScreen.tsx): Player v2
    ("Now playing", "Player v2") is on, the classic player off; None for
    anything else."""
    drawn = " ".join((text or "").split())
    v2 = "Now playing" in drawn and "Player v2" in drawn
    classic = "Classic player" in drawn
    if v2 == classic:
        return None
    return v2


def agreement(
    agree: Any,
    *,
    panel: Optional[str],
    phone: Optional[str],
    api: Optional[Mapping[str, Any]],
) -> Verdict:
    """Gate 10/4 at a beat's start (plan v2 C1): the app's panel row, its phone
    and the API's answer for the device all say the storyboard's on or off."""
    said = {
        "panel": panel_says(panel),
        "phone": phone_says(phone),
        "api": None if not api else bool(api.get("enabled")),
    }
    numbers = {
        "at": f"scene {agree.scene} beat {agree.beat}",
        "device": agree.device,
        "wanted": agree.enabled,
        "said": said,
        "api_reason": None if not api else api.get("reason"),
    }
    if any(value != agree.enabled for value in said.values()):
        return (
            False,
            numbers,
            f"panel/phone/API disagree at cue {agree.scene} beat {agree.beat}:"
            f" panel {said['panel']}, phone {said['phone']}, API {said['api']};"
            f" the storyboard says {'on' if agree.enabled else 'off'}",
        )
    return True, numbers, ""


def rollback_end(
    end: Any,
    *,
    named: Sequence[Mapping[str, Any]],
    schedules: Sequence[Mapping[str, Any]],
    audit: Sequence[Mapping[str, Any]],
    actor: Mapping[str, Any],
    kill_named: Sequence[Mapping[str, Any]],
    kill_audit: Sequence[Mapping[str, Any]],
    readings: Mapping[str, str],
    api_at_agreement: Optional[Mapping[str, Any]],
    rollback_posts: Sequence[str],
    log: Mapping[str, int],
) -> Verdict:
    """Gate 10/4 (S8a of plan v2.2, ``storyboard.RollbackEnd``): the safety
    monitor rolled the flag back by itself, and every reading agrees.

    * one flag has the name, with the key, at ``end.percentage``, active; its
      one rollout schedule is paused;
    * its audit entries, oldest first (*audit* is newest first, as the API
      lists them), are exactly ``end.audit``: each by the signed-in user
      (*actor*) but the last, one ``safety_rollback`` by the safety monitor
      with no user, automatic, to the percentage, for an error-rate breach;
    * the API's log (*log*: counts of its lines) holds exactly one rollback of
      the flag, automatic, none manual; and no page asked for a rollback
      (*rollback_posts*, from the capture's listener);
    * the page drew it: "Unhealthy" at B9, "Paused" and the percentage after,
      the audit log's top row the monitor's rollback of the flag, and the
      rollback's reason, word for word; and the agreement at ``end.agree``;
    * the kill switch is one flag, inactive, with exactly its audit entries,
      each by the signed-in user.
    """
    numbers: Dict[str, Any] = {"flags_named": len(named)}
    if len(named) != 1 or named[0].get("key") != end.key:
        return (
            False,
            numbers,
            f"{len(named)} flags are named {end.name!r} with key {end.key}",
        )
    flag = named[0]
    numbers.update(
        rollout_percentage=flag.get("rollout_percentage"),
        status=flag.get("status"),
        schedules=[str(s.get("status")).lower() for s in schedules],
    )
    if flag.get("rollout_percentage") != end.percentage:
        return (
            False,
            numbers,
            f"the flag is at {flag.get('rollout_percentage')}%, not {end.percentage}%",
        )
    if str(flag.get("status")).lower() != end.status:
        return False, numbers, f"the flag is {flag.get('status')!r}, not {end.status!r}"
    if numbers["schedules"] != [end.schedule]:
        return (
            False,
            numbers,
            f"the flag's rollout schedules are {numbers['schedules']}, not one {end.schedule}",
        )
    oldest_first = list(reversed(list(audit)))
    actions = [str(e.get("action_type")) for e in oldest_first]
    numbers["audit"] = actions
    if actions != list(end.audit):
        return (
            False,
            numbers,
            f"the flag's audit entries are {actions}, not {list(end.audit)}",
        )
    email, ident = actor.get("email"), str(actor.get("id"))
    others = [
        f"{e.get('action_type')} by {e.get('user_email')!r}"
        for e in oldest_first[:-1]
        if e.get("user_email") != email or str(e.get("user_id")) != ident
    ]
    if others:
        return (
            False,
            numbers,
            f"not every change is by the signed-in {email!r}: {others[0]}",
        )
    rollback = oldest_first[-1]
    try:
        after = json.loads(rollback.get("new_value") or "{}")
    except ValueError:
        after = {}
    reason = str(rollback.get("reason") or "")
    numbers["rollback"] = {
        "by": rollback.get("user_email"),
        "user_id": rollback.get("user_id"),
        "trigger_type": after.get("trigger_type"),
        "new_percentage": after.get("new_percentage"),
        "reason": reason,
    }
    if rollback.get("user_email") != SAFETY_MONITOR or rollback.get("user_id"):
        return (
            False,
            numbers,
            f"the rollback was made by {rollback.get('user_email')!r}; the storyboard"
            " claims the safety monitor",
        )
    if (
        after.get("trigger_type") != "automatic"
        or after.get("new_percentage") != end.percentage
    ):
        return (
            False,
            numbers,
            f"the rollback is {after.get('trigger_type')!r} to"
            f" {after.get('new_percentage')}%, not automatic to {end.percentage}%",
        )
    if not reason.startswith(ROLLBACK_REASON):
        return (
            False,
            numbers,
            f"the rollback's reason is {reason!r}, not an error-rate breach",
        )
    numbers["log"] = dict(log)
    if (log.get("rolled_back"), log.get("automatic"), log.get("manual")) != (1, 1, 0):
        return (
            False,
            numbers,
            f"the API's log holds {log.get('rolled_back')} rollback(s) of the flag,"
            f" {log.get('automatic')} automatic and {log.get('manual')} manual; one"
            " automatic and no other",
        )
    numbers["rollback_posts"] = list(rollback_posts)
    if rollback_posts:
        return False, numbers, f"a page asked for a rollback: {rollback_posts[0]}"
    drawn = {
        name: " ".join((readings.get(name) or "").split())
        for name in (end.unhealthy, end.paused, end.rolled_back)
    }
    numbers["drawn"] = drawn
    for name, wanted in (
        (end.unhealthy, UNHEALTHY_LABEL),
        (end.paused, PAUSED_LABEL),
        (end.rolled_back, f"{end.percentage}%"),
    ):
        if drawn[name] != wanted:
            return (
                False,
                numbers,
                f"the page's {name} read {drawn[name]!r}, not {wanted!r}",
            )
    rows = audit_rows(readings.get(end.shown) or "")
    top = rows[0][1:5] if rows else []
    numbers["top_row"] = top
    wanted_row = [SAFETY_MONITOR_LABEL, ROLLBACK_LABEL, FLAG_ENTITY_LABEL, end.name]
    if top != wanted_row:
        return False, numbers, f"the audit log's top row showed {top}, not {wanted_row}"
    detail = " ".join((readings.get(end.reason) or "").split())
    if " ".join(reason.split()) not in detail:
        return (
            False,
            numbers,
            f"the rollback's details did not show the API's reason {reason!r}",
        )
    ok, numbers["agreement"], why = agreement(
        end.agree,
        panel=readings.get(end.agree.panel),
        phone=readings.get(end.agree.phone),
        api=api_at_agreement,
    )
    if not ok:
        return False, numbers, why
    kill = end.kill_switch
    kill_oldest = [str(e.get("action_type")) for e in reversed(list(kill_audit))]
    numbers["kill_switch"] = {
        "flags_named": len(kill_named),
        "status": kill_named[0].get("status") if len(kill_named) == 1 else None,
        "audit": kill_oldest,
    }
    if len(kill_named) != 1 or kill_named[0].get("key") != kill.key:
        return (
            False,
            numbers,
            f"{len(kill_named)} flags are named {kill.name!r} with key {kill.key}",
        )
    if str(kill_named[0].get("status")).lower() != "inactive":
        return False, numbers, f"the kill switch {kill.name!r} is not off"
    if kill_oldest != list(kill.audit):
        return (
            False,
            numbers,
            f"the kill switch's audit entries are {kill_oldest}, not {list(kill.audit)}",
        )
    strangers = [
        e.get("user_email")
        for e in kill_audit
        if e.get("user_email") != email or str(e.get("user_id")) != ident
    ]
    if strangers:
        return False, numbers, f"the kill switch was changed by {strangers[0]!r}"
    return True, numbers, ""


def flag_end(
    end: Any,
    *,
    named: Sequence[Mapping[str, Any]],
    segments: Sequence[Mapping[str, Any]],
    audit: Sequence[Mapping[str, Any]],
    actor: Mapping[str, Any],
    shown: Optional[str],
) -> Verdict:
    """Gate 10 for a flag the video made (``storyboard.FlagEnd``).

    * exactly one flag has the name, with the storyboard's key;
    * its rules are one group of one condition, ``segment`` ``in_segment`` the
      id of the one active segment with the storyboard's segment name;
    * its rollout percentage and status are the ones the video left it at;
    * its audit entries, oldest first, are exactly the storyboard's, every one
      by the signed-in user (*actor*: their email and id);
    * the page showed them: the audit table's top rows are those entries,
      newest first, each with the user, the action and the flag's name.
    """
    numbers: Dict[str, Any] = {"flags_named": len(named)}
    if len(named) != 1:
        return False, numbers, f"{len(named)} flags are named {end.name!r}"
    flag = named[0]
    numbers.update(
        key=flag.get("key"),
        status=flag.get("status"),
        rollout_percentage=flag.get("rollout_percentage"),
        targeting_rules=flag.get("targeting_rules"),
    )
    if flag.get("key") != end.key:
        return (
            False,
            numbers,
            f"{end.name!r} has key {flag.get('key')!r}, not {end.key!r}",
        )
    matching = [
        s
        for s in segments
        if s.get("name") == end.segment and str(s.get("status")).lower() == "active"
    ]
    numbers["segments_named"] = len(matching)
    if len(matching) != 1:
        return (
            False,
            numbers,
            f"{len(matching)} active segments are named {end.segment!r}",
        )
    segment_id = str(matching[0].get("id"))
    rules = flag.get("targeting_rules") or {}
    groups = rules.get("groups") if isinstance(rules, dict) else None
    conditions = (
        groups[0].get("conditions")
        if isinstance(groups, list) and len(groups) == 1 and isinstance(groups[0], dict)
        else None
    )
    if not (isinstance(conditions, list) and len(conditions) == 1):
        return (
            False,
            numbers,
            f"the flag's rules are not one group of one condition: {rules!r}",
        )
    condition = conditions[0]
    said = (
        condition.get("attribute"),
        condition.get("operator"),
        str(condition.get("value")),
    )
    if said != ("segment", "in_segment", segment_id):
        return (
            False,
            numbers,
            f"the flag's rule is {said[0]!r} {said[1]!r} {said[2]!r}, not 'segment'"
            f" 'in_segment' {end.segment!r} ({segment_id})",
        )
    if flag.get("rollout_percentage") != end.rollout_percentage:
        return (
            False,
            numbers,
            f"the flag is at {flag.get('rollout_percentage')}%, not"
            f" {end.rollout_percentage}%",
        )
    if str(flag.get("status")).lower() != end.status:
        return (
            False,
            numbers,
            f"the flag is {flag.get('status')!r}, not {end.status!r}",
        )
    oldest_first = list(reversed(list(audit)))
    actions = [str(e.get("action_type")) for e in oldest_first]
    numbers["audit"] = actions
    if actions != list(end.audit):
        return (
            False,
            numbers,
            f"the flag's audit entries are {actions}, not {list(end.audit)}",
        )
    email, ident = actor.get("email"), str(actor.get("id"))
    others = [
        f"{e.get('action_type')} by {e.get('user_email')!r}"
        for e in oldest_first
        if e.get("user_email") != email or str(e.get("user_id")) != ident
    ]
    if others:
        return (
            False,
            numbers,
            f"not every entry is by the signed-in {email!r}: {others[0]}",
        )
    rows = audit_rows(shown or "")
    wanted = [
        [email, AUDIT_LABELS[action], FLAG_ENTITY_LABEL, end.name]
        for action in reversed(list(end.audit))
    ]
    drawn = [row[1:5] for row in rows[: len(wanted)]]
    numbers["shown_rows"] = drawn
    if drawn != wanted:
        return (
            False,
            numbers,
            f"the audit log's top rows showed {drawn}; the API says {wanted}",
        )
    return True, numbers, ""


# ---------------------------------------------------------------------------
# Gate 10 "design": an experiment the video designed and left a draft
# ---------------------------------------------------------------------------

#: How the Review step names a metric's type (frontend/src/types/experiments.ts
#: ``METRIC_TYPE_LABELS``; a unit test keeps them equal).
METRIC_TYPE_LABELS: Dict[str, str] = {
    "conversion": "Conversion",
    "revenue": "Revenue",
    "count": "Count",
    "duration": "Duration",
    "custom": "Custom",
}
#: ``correctionName`` (frontend/src/components/results/shared/analysisSettings.ts):
#: any other method is named "None".
CORRECTION_NAMES: Dict[str, str] = {
    "benjamini_hochberg": "Benjamini-Hochberg",
    "bonferroni": "Bonferroni",
}
#: The Review row's addition when Bayesian analysis is on (Wizard.tsx).
BAYESIAN_ON = " · Bayesian analysis on"
#: The tolerance a saved effect is compared within.
EFFECT_TOLERANCE = 1e-12

#: The fields guided setup never sets, with the value an experiment it made
#: holds: anything else was set some other way.
UNTOUCHED: Tuple[Tuple[str, Any], ...] = (
    ("sequential_testing_enabled", False),
    ("variance_reduction_config", None),
    ("mutual_exclusion_group_id", None),
    ("optimization_type", "fixed"),
    ("split_url_config", None),
    ("start_date", None),
    ("end_date", None),
    ("resume_at", None),
)
_EXPERIMENT_KEYS = (
    "id",
    "name",
    "key",
    "status",
    "owner_id",
    "experiment_type",
    "variants",
    "metrics",
    "targeting_rules",
    "confidence_level",
    "correction_method",
    "bayesian_enabled",
)
_VARIANT_KEYS = ("name", "traffic_allocation", "is_control", "configuration")
_METRIC_KEYS = (
    "name",
    "event_name",
    "metric_type",
    "is_primary",
    "expected_effect",
    "lower_is_better",
)


def correction_name(method: str) -> str:
    """``correctionName``: the correction's full name, "None" for none."""
    return CORRECTION_NAMES.get(method, "None")


def format_confidence(level: float) -> str:
    """``formatConfidence``: 0.95 -> "95%", 0.925 -> "92.5%" (one decimal at
    most, a trailing zero dropped, as ``Number(x.toFixed(1))`` prints it)."""
    text = f"{level * 100:.1f}"
    if text.endswith(".0"):
        text = text[:-2]
    return f"{text}%"


def describe_settings(level: float, method: str) -> str:
    """``describeSettings``: the Review row's wording of the analysis settings."""
    correction = (
        "no correction" if method == "none" else f"{correction_name(method)} correction"
    )
    return f"{format_confidence(level)} confidence · {correction}"


def _one_line(text: Optional[str]) -> str:
    return " ".join((text or "").split())


def _lines(text: Optional[str]) -> List[str]:
    return [
        " ".join(line.split()) for line in (text or "").splitlines() if line.strip()
    ]


#: The Review's variant line puts the configuration after this, as JSON.
CONFIGURATION_SEPARATOR = ", configuration "


def _variant_line(line: str) -> Tuple[str, Any]:
    """A Review variant line as (its words, its configuration parsed or None).

    The configuration is compared parsed: the API may return its keys in
    another order than the wizard sent them."""
    head, separator, tail = line.partition(CONFIGURATION_SEPARATOR)
    if not separator:
        return line, None
    try:
        return head, json.loads(tail)
    except ValueError:
        return line, None


def _stored_rules_empty(rules: Any) -> bool:
    return (
        rules is None
        or rules == {}
        or (
            isinstance(rules, dict)
            and set(rules) <= {"groups", "logical_operator"}
            and rules.get("groups") == []
        )
    )


def _without_builder_ids(rules: Any) -> Any:
    """The stored rules less the ``id`` the builder gives each group and each
    condition: at those two levels only, so an id anywhere else stays a
    difference."""
    if not isinstance(rules, dict):
        return rules
    out = dict(rules)
    groups = out.get("groups")
    if isinstance(groups, list):
        cleaned = []
        for group in groups:
            if isinstance(group, dict):
                group = {k: v for k, v in group.items() if k != "id"}
                conditions = group.get("conditions")
                if isinstance(conditions, list):
                    group["conditions"] = [
                        {k: v for k, v in c.items() if k != "id"}
                        if isinstance(c, dict)
                        else c
                        for c in conditions
                    ]
            cleaned.append(group)
        out["groups"] = cleaned
    return out


def design_mirrors(end: Any, saved: Mapping[str, Any]) -> Dict[str, Any]:
    """Each reading a design end state may compare, as the product draws the
    saved experiment *saved*: list readings as lines in the storyboard's
    declared order (the order the wizard holds them), from the API's values
    keyed by name, never in the API's own order."""
    variants = {v["name"]: v for v in saved["variants"]}
    metrics = {m["name"]: m for m in saved["metrics"]}
    variant_lines = []
    for declared in end.variants:
        api = variants[declared.name]
        line = f"{api['name']}: {api['traffic_allocation']}%"
        if api["is_control"]:
            line += " (control)"
        if api["configuration"] is not None:
            line += CONFIGURATION_SEPARATOR + json.dumps(
                api["configuration"], separators=(",", ":"), ensure_ascii=False
            )
        variant_lines.append(line)
    metric_lines = []
    for declared in end.metrics:
        api = metrics[declared.name]
        label = METRIC_TYPE_LABELS.get(str(api["metric_type"]), str(api["metric_type"]))
        line = f"{api['name']} ({api['event_name']}, {label})"
        if api["is_primary"]:
            line += " — primary metric"
        metric_lines.append(line)
    rules = saved["targeting_rules"]
    if _stored_rules_empty(rules):
        targeting = "Targeting: everyone"
    else:
        groups = rules.get("groups") if isinstance(rules, dict) else None
        count = len(groups) if isinstance(groups, list) else 0
        targeting = f"Targeting: {count} rule {'group' if count == 1 else 'groups'}"
    level = float(saved["confidence_level"])
    method = str(saved["correction_method"])
    detail = "no correction" if method == "none" else correction_name(method)
    return {
        "review_variants": variant_lines,
        "review_metrics": metric_lines,
        "review_targeting": targeting,
        "review_analysis": describe_settings(level, method)
        + (BAYESIAN_ON if saved["bayesian_enabled"] else ""),
        "detail_analysis": f"{format_confidence(level)} confidence · {detail}",
        "detail_bayesian": "On" if saved["bayesian_enabled"] else "Off",
    }


def _missing(item: Mapping[str, Any], keys: Sequence[str]) -> Optional[str]:
    for key in keys:
        if key not in item:
            return key
    return None


def design_end(
    end: Any,
    *,
    named: Sequence[Mapping[str, Any]],
    total: int,
    seed_experiments: int,
    owner_id: Any,
    readings: Mapping[str, str],
    moved: Optional[Mapping[str, Any]] = None,
) -> Verdict:
    """Gate 10 "design" (``storyboard.DesignEnd``): the video made exactly the
    experiment it declares, and left it a draft. In order, the first failure
    refusing (*moved*: the ``UNTOUCHED`` fields the video set, each with the
    value it set; ``start_date`` compared as an instant, naive as UTC):

    1. exactly one experiment has the name, with the key, a draft, owned by the
       signed-in user (*owner_id*);
    2. the API holds the seed's experiments and this one (a double-clicked
       Create refuses);
    3. its type;
    4. its variants, keyed by name: allocation, control, configuration;
    5. its metrics, keyed by name: event, type, primary, the saved effect (the
       primary's mde, null on the others) and lower_is_better;
    6. its targeting rule, as declared, less the builder's ids;
    7. its confidence level, correction and Bayesian setting;
    8. nothing guided setup cannot set (``UNTOUCHED``);
    9. each reading named in ``shown`` said what was saved, in the product's
       words (``design_mirrors``).

    A key absent from the API's answer refuses: no check passes on a default.
    """
    numbers: Dict[str, Any] = {"experiments_named": len(named)}
    if len(named) != 1:
        return False, numbers, f"{len(named)} experiments are named {end.name!r}"
    saved = named[0]
    absent = _missing(saved, _EXPERIMENT_KEYS + tuple(k for k, _ in UNTOUCHED))
    if absent:
        return False, numbers, f"the API's answer for {end.name!r} has no {absent!r}"
    numbers.update(
        key=saved["key"],
        status=saved["status"],
        experiment_type=saved["experiment_type"],
        owner_matches=str(saved["owner_id"]) == str(owner_id),
    )
    if saved["key"] != end.key:
        return False, numbers, f"{end.name!r} has key {saved['key']!r}, not {end.key!r}"
    if str(saved["status"]).lower() != end.status:
        return (
            False,
            numbers,
            f"{end.name!r} is {saved['status']!r}, not {end.status!r}",
        )
    if owner_id is None or str(saved["owner_id"]) != str(owner_id):
        return (
            False,
            numbers,
            f"{end.name!r} is owned by {saved['owner_id']!r}, not the signed-in user",
        )
    numbers.update(experiments_in_api=total, seed_experiments=seed_experiments)
    if total != seed_experiments + 1:
        return (
            False,
            numbers,
            f"the API holds {total} experiments; the seed made {seed_experiments}"
            " and the video 1",
        )
    if str(saved["experiment_type"]) != end.type:
        return (
            False,
            numbers,
            f"{end.name!r} is type {saved['experiment_type']!r}; the storyboard made"
            f" {end.type!r}",
        )
    variants = list(saved["variants"])
    for variant in variants:
        absent = _missing(variant, _VARIANT_KEYS)
        if absent:
            return False, numbers, f"a variant in the API's answer has no {absent!r}"
    names = [str(v["name"]) for v in variants]
    numbers["variants"] = sorted(names)
    declared = [v.name for v in end.variants]
    if len(set(names)) != len(names) or sorted(names) != sorted(declared):
        return (
            False,
            numbers,
            f"the variants are {sorted(names)}; the storyboard made {sorted(declared)}",
        )
    by_name = {str(v["name"]): v for v in variants}
    for want in end.variants:
        got = by_name[want.name]
        if got["traffic_allocation"] != want.allocation:
            return (
                False,
                numbers,
                f"variant {want.name!r} is {got['traffic_allocation']}%; the"
                f" storyboard sets {want.allocation}%",
            )
        if bool(got["is_control"]) != want.control or not isinstance(
            got["is_control"], bool
        ):
            return (
                False,
                numbers,
                f"variant {want.name!r} has is_control {got['is_control']!r}; the"
                f" storyboard says {want.control}",
            )
        if got["configuration"] != want.configuration:
            return (
                False,
                numbers,
                f"variant {want.name!r}'s configuration is {got['configuration']!r};"
                f" the storyboard sets {want.configuration!r}",
            )
    metrics = list(saved["metrics"])
    for metric in metrics:
        absent = _missing(metric, _METRIC_KEYS)
        if absent:
            return False, numbers, f"a metric in the API's answer has no {absent!r}"
    names = [str(m["name"]) for m in metrics]
    numbers["metrics"] = sorted(names)
    declared = [m.name for m in end.metrics]
    if len(set(names)) != len(names) or sorted(names) != sorted(declared):
        return (
            False,
            numbers,
            f"the metrics are {sorted(names)}; the storyboard made {sorted(declared)}",
        )
    by_name = {str(m["name"]): m for m in metrics}
    for want in end.metrics:
        got = by_name[want.name]
        said = (got["event_name"], str(got["metric_type"]), got["is_primary"])
        if said != (want.event, want.type, want.primary):
            return (
                False,
                numbers,
                f"metric {want.name!r} is {said}; the storyboard made"
                f" {(want.event, want.type, want.primary)}",
            )
        effect = got["expected_effect"]
        if want.mde is None:
            if effect is not None:
                return (
                    False,
                    numbers,
                    f"metric {want.name!r} has the planned effect {effect!r}; the"
                    " storyboard typed none for it",
                )
        elif (
            not isinstance(effect, (int, float))
            or abs(float(effect) - want.mde) > EFFECT_TOLERANCE
        ):
            return (
                False,
                numbers,
                f"metric {want.name!r} has the planned effect {effect!r}; the"
                f" storyboard typed {want.mde}",
            )
        if got["lower_is_better"] is not want.lower_is_better:
            return (
                False,
                numbers,
                f"metric {want.name!r} has lower_is_better {got['lower_is_better']!r};"
                f" the storyboard says {want.lower_is_better}",
            )
    stored = saved["targeting_rules"]
    numbers["targeting_rules"] = stored
    if end.targeting is None:
        if not _stored_rules_empty(stored):
            return (
                False,
                numbers,
                f"{end.name!r} has targeting rules {stored!r}; the storyboard lets"
                " everyone join",
            )
    else:
        wanted = end.targeting.model_dump()
        if _without_builder_ids(stored) != wanted:
            return (
                False,
                numbers,
                f"the stored targeting differs: {_without_builder_ids(stored)!r}, not"
                f" {wanted!r} (the builder's ids aside)",
            )
    analysis = (
        saved["confidence_level"],
        saved["correction_method"],
        saved["bayesian_enabled"],
    )
    numbers["analysis"] = list(analysis)
    if (
        not isinstance(analysis[0], (int, float))
        or abs(float(analysis[0]) - end.analysis.confidence) > 1e-9
    ):
        return (
            False,
            numbers,
            f"confidence_level is {analysis[0]!r}; the storyboard chose"
            f" {end.analysis.confidence}",
        )
    if str(analysis[1]) != end.analysis.correction:
        return (
            False,
            numbers,
            f"correction_method is {analysis[1]!r}; the storyboard chose"
            f" {end.analysis.correction!r}",
        )
    if analysis[2] is not end.analysis.bayesian:
        return (
            False,
            numbers,
            f"bayesian_enabled is {analysis[2]!r}; the storyboard turns it"
            f" {'on' if end.analysis.bayesian else 'off'}",
        )
    moved = dict(moved or {})
    for key, value in UNTOUCHED:
        if key in moved:
            if not _same_setting(key, saved[key], moved[key]):
                return (
                    False,
                    numbers,
                    f"{end.name!r} has {key} {saved[key]!r}; the video set"
                    f" {moved[key]!r}",
                )
        elif saved[key] != value:
            return (
                False,
                numbers,
                f"{end.name!r} has {key} {saved[key]!r}: guided setup never sets"
                f" it, so the video did not",
            )
    mirrors = design_mirrors(end, saved)
    shown: Dict[str, Any] = {}
    for kind, record in end.shown.items():
        text = readings.get(record)
        if text is None:
            return (
                False,
                numbers,
                f"no reading {record!r} was recorded for {kind}",
            )
        wanted = mirrors[kind]
        if isinstance(wanted, list):
            drawn: Any = _lines(text)
            if kind == "review_variants":
                same = [_variant_line(x) for x in drawn] == [
                    _variant_line(x) for x in wanted
                ]
            else:
                same = drawn == wanted
        else:
            drawn = _one_line(text)
            same = drawn == wanted
        shown[kind] = drawn
        numbers["shown"] = shown
        if not same:
            return (
                False,
                numbers,
                f"{kind} showed {drawn!r}; the API saved {wanted!r}",
            )
    numbers["shown"] = shown
    return True, numbers, ""


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


def _same_setting(key: str, got: Any, wanted: Any) -> bool:
    """A field the video set, against the value it set: a date as an instant
    (naive read as UTC, to the second), anything else equal and of one type."""
    if key.endswith("_date") and isinstance(wanted, str):
        a, b = as_instant(got), as_instant(wanted)
        return a is not None and b is not None and a.replace(microsecond=0) == b
    return type(got) is type(wanted) and got == wanted
