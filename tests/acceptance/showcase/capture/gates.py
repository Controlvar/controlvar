"""The capture gates that judge recorded data: pure functions over numbers.

Each answers ``(ok, numbers, why)``; ``why`` is the refusal text when not ok.
They are unit tested on planted inputs (``backend/tests/unit/showcase``).
"""

from __future__ import annotations

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


def experiment_end(
    end: Any,
    *,
    named: Sequence[Mapping[str, Any]],
    results: Optional[Mapping[str, Any]],
    planned: Mapping[str, Mapping[str, int]],
    report: Mapping[str, Mapping[str, int]],
    oracle_p: Optional[float],
    shown: Optional[str],
) -> Verdict:
    """Gate 10 for an experiment the video made (``storyboard.ExperimentEnd``).

    * exactly one experiment has the name, with the storyboard's key, running;
    * its users and converting users per variant are the same three times:
      as planned from the seed, as the server answered the traffic, and as the
      results API counts them;
    * the results API's p-value is Fisher's exact test on those counts
      (*oracle_p*), and it recommends shipping the storyboard's winner;
    * the page showed the API's recommendation, word for word.
    """
    numbers: Dict[str, Any] = {
        "experiments_named": len(named),
        "planned": dict(planned),
        "answered": dict(report),
    }
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
