#!/usr/bin/env python3
"""What the compose-upgrade job's upgrade must keep beside the rows (#1897).

``scripts/compose_upgrade.sh`` checks after every step that the experiment and
the flag it adds at the previous release (N-1) are still there. This is the
state a running install holds beside them, and the first its users notice:

* each user's **assignment**, from ``GET /api/v1/tracking/assignments/{user}``
  with ``active_only=false``: the variant, by id and name;
* the **results**, from ``GET /api/v1/results/{id}?use_cache=false``: the
  experiment's status, its users and converting users, and each variant's
  users and conversions on the primary metric. Never from the cache: compose's
  Redis persists, so a cached copy would compare N-1's numbers with themselves;
* each user's **flag answer**, from ``GET /api/v1/feature-flags/evaluate/{key}``
  with the user's context: ``enabled``, ``value`` and ``reason``.

At N-1 the job adds an API key, starts the probe experiment, assigns the users
of :data:`PLAN` to it through the tracking API with their country as the
context, and sends a ``purchase`` for each user the plan marks as converting.
The probe flag is on at 30%, with :data:`FLAG_RULES`, a rule that turns it on
for every user in :data:`RULE_COUNTRY`. The job saves the API's answers to
those three reads in a directory, and ``state`` reduces them to one document.
``seeded`` checks that N-1's is the seed's, so a later difference cannot be a
seed that never took.

After each step (the upgrade, ``restart api``, the rollback in place when N-1
serves again, the restore) the job saves the same answers again, before it
writes anything, and ``compare`` names every difference from N-1's document.
Assigning each user again comes after, as its own check: ``repeat`` requires
the variant recorded at N-1.

Every count is exact: a state that shrank, or grew, is a difference.

Usage (the job runs it with the runner's ``python3``; standard library only)::

    compose_upgrade_state.py plan                           USER COUNTRY CONVERTS lines
    compose_upgrade_state.py flag-rules                     the probe flag's targeting_rules
    compose_upgrade_state.py state DIR EXPERIMENT_ID        the document, on stdout
    compose_upgrade_state.py seeded STATE
    compose_upgrade_state.py compare RECORDED OBSERVED WHEN
    compose_upgrade_state.py repeat RECORDED DIR WHEN

``DIR`` holds the answers as the job saved them: ``assignments/<user>.json``,
``flags/<user>.json`` and ``results.json`` for ``state``, and
``repeat/<user>.json`` (``POST /api/v1/tracking/assign``) for ``repeat``.

Exit status: 0 as expected; 1 not, with one line per difference on stdout,
each starting with WHEN; 2 it could not run (usage, or an answer that is
missing or not of the shape the API gives).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

#: The users the job seeds at N-1: (user id, country, converts). The odd ones
#: are in the US, which the flag's rule turns on for; the first ten convert.
PLAN: Tuple[Tuple[str, str, bool], ...] = tuple(
    (f"cu-{n:02d}", "US" if n % 2 else "DE", n <= 10) for n in range(1, 21)
)

#: The country the probe flag's rule turns the flag on for, at 100%.
RULE_COUNTRY = "US"

#: The probe flag's ``targeting_rules``, in the dashboard's shape. Its rule
#: names no ``rollout_percentage``, so a user it matches gets the flag.
FLAG_RULES: Dict[str, Any] = {
    "logical_operator": "AND",
    "groups": [
        {
            "logical_operator": "AND",
            "conditions": [
                {"attribute": "country", "operator": "equals", "value": RULE_COUNTRY}
            ],
        }
    ],
}

#: What a flag answer is compared on.
ANSWER_FIELDS = ("enabled", "value", "reason")
#: What the results are compared on: the experiment's, then each variant's.
SUMMARY_FIELDS = ("status", "metric", "total_users", "total_conversions")
VARIANT_FIELDS = ("sample_size", "conversions")

#: The name the recorded document goes by in every difference.
RECORDED_AT = "N-1"
#: The WHEN of ``seeded``'s lines.
SEEDED = "N-1, after the seed"


class Unreadable(Exception):
    """An answer is missing, or is not of the shape the API gives."""


def _read(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise Unreadable(f"{path}: no such file") from None
    except (OSError, ValueError) as exc:
        raise Unreadable(
            f"{path}: not readable as JSON ({type(exc).__name__})"
        ) from None


def _show(value: Any) -> str:
    """*value* as a line shows it: a string bare, anything else as JSON."""
    return value if isinstance(value, str) else json.dumps(value)


# ---------------------------------------------------------------------------
# state: the saved answers, reduced to one document


def _assignments(answer: Any, user: str, experiment_id: str) -> List[Dict[str, Any]]:
    if not isinstance(answer, list):
        raise Unreadable(f"the assignments of {user}: not a list")
    found = [
        {"variant_id": item.get("variant_id"), "variant_name": item.get("variant_name")}
        for item in answer
        if isinstance(item, dict) and item.get("experiment_id") == experiment_id
    ]
    return sorted(found, key=lambda a: (str(a["variant_name"]), str(a["variant_id"])))


def _results(answer: Any) -> Dict[str, Any]:
    if not isinstance(answer, dict) or not isinstance(answer.get("summary"), dict):
        raise Unreadable("the results: no summary")
    summary = answer["summary"]
    metrics = answer.get("metrics")
    if not isinstance(metrics, list):
        raise Unreadable("the results: no list of metrics")
    primary = [m for m in metrics if isinstance(m, dict) and m.get("is_primary")]
    variants: Dict[str, Dict[str, Any]] = {}
    metric: Optional[str] = None
    if len(primary) == 1:
        metric = primary[0].get("metric_name")
        for variant in primary[0].get("variants") or []:
            if not isinstance(variant, dict):
                raise Unreadable("the results: a variant that is not an object")
            variants[str(variant.get("variant_name"))] = {
                field: variant.get(field) for field in VARIANT_FIELDS
            }
    elif primary:
        metric = f"{len(primary)} primary metrics"
    return {
        "status": answer.get("status"),
        "metric": metric,
        "total_users": summary.get("total_users"),
        "total_conversions": summary.get("total_conversions"),
        "variants": variants,
    }


def state(directory: Path, experiment_id: str) -> Dict[str, Any]:
    """The document for the answers saved in *directory*, for *experiment_id*."""
    assignments: Dict[str, List[Dict[str, Any]]] = {}
    flags: Dict[str, Dict[str, Any]] = {}
    for user, _country, _converts in PLAN:
        assignments[user] = _assignments(
            _read(directory / "assignments" / f"{user}.json"), user, experiment_id
        )
        answer = _read(directory / "flags" / f"{user}.json")
        if not isinstance(answer, dict):
            raise Unreadable(f"the flag answer for {user}: not an object")
        flags[user] = {field: answer.get(field) for field in ANSWER_FIELDS}
    return {
        "assignments": assignments,
        "results": _results(_read(directory / "results.json")),
        "flags": flags,
    }


def load_state(path: Path) -> Dict[str, Any]:
    """A document ``state`` wrote."""
    document = _read(path)
    if not isinstance(document, dict) or set(document) != {
        "assignments",
        "results",
        "flags",
    }:
        raise Unreadable(f"{path}: not a document of this tool's state command")
    return document


# ---------------------------------------------------------------------------
# The lines


def _names(assignments: Sequence[Dict[str, Any]]) -> str:
    return " and ".join(str(a.get("variant_name")) for a in assignments) or "no variant"


def _ids(assignments: Sequence[Dict[str, Any]]) -> str:
    return " and ".join(str(a.get("variant_id")) for a in assignments)


def _answer(answer: Optional[Dict[str, Any]]) -> str:
    if not answer:
        return "no answer"
    text = f"enabled {_show(answer.get('enabled'))} ({_show(answer.get('reason'))})"
    if answer.get("value") != answer.get("enabled"):
        text += f", value {_show(answer.get('value'))}"
    return text


def _served(answer: Optional[Dict[str, Any]]) -> Any:
    return (answer.get("enabled"), answer.get("value")) if answer else None


def _assignment_lines(
    then_all: Dict[str, Any], now_all: Dict[str, Any], when: str
) -> List[str]:
    users = sorted(set(then_all) | set(now_all))
    lines = []
    for user in users:
        then, now = then_all.get(user) or [], now_all.get(user) or []
        if then == now:
            continue
        if not now:
            text = f"{user} has no assignment, recorded {_names(then)}"
        elif not then:
            text = f"{user} is assigned {_names(now)}, recorded no assignment"
        elif len(now) != len(then):
            text = f"{user} has {len(now)} assignments ({_names(now)}), recorded {_names(then)}"
        elif _names(now) == _names(then):
            text = (
                f"{user} is assigned {_names(now)} with variant id {_ids(now)}, "
                f"recorded {_ids(then)}"
            )
        else:
            text = f"{user} is assigned {_names(now)}, recorded {_names(then)}"
        lines.append(f"{when}: {text} at {RECORDED_AT}")
    if lines:
        lines.insert(
            0,
            f"{when}: {len(lines)} of {len(users)} assignments are not as recorded "
            f"at {RECORDED_AT}",
        )
    return lines


def _results_lines(then: Dict[str, Any], now: Dict[str, Any], when: str) -> List[str]:
    lines = []
    for field in SUMMARY_FIELDS:
        if then.get(field) != now.get(field):
            lines.append(
                f"{when}: results {field} {_show(now.get(field))}, recorded "
                f"{_show(then.get(field))} at {RECORDED_AT}"
            )
    then_variants, now_variants = then.get("variants") or {}, now.get("variants") or {}
    for name in sorted(set(then_variants) | set(now_variants)):
        if name not in now_variants:
            lines.append(
                f"{when}: results have no variant {name}, recorded "
                f"{_show(then_variants[name])} at {RECORDED_AT}"
            )
            continue
        if name not in then_variants:
            lines.append(
                f"{when}: results have a variant {name} "
                f"{_show(now_variants[name])}, not recorded at {RECORDED_AT}"
            )
            continue
        for field in VARIANT_FIELDS:
            was, is_now = then_variants[name].get(field), now_variants[name].get(field)
            if was != is_now:
                lines.append(
                    f"{when}: results {name} {field} {_show(is_now)}, recorded "
                    f"{_show(was)} at {RECORDED_AT}"
                )
    return lines


def _flag_lines(
    then_all: Dict[str, Any], now_all: Dict[str, Any], when: str
) -> List[str]:
    users = sorted(set(then_all) | set(now_all))
    lines = []
    served = 0
    for user in users:
        then, now = then_all.get(user), now_all.get(user)
        if then == now:
            continue
        lines.append(
            f"{when}: {user}'s flag answer is {_answer(now)}, recorded "
            f"{_answer(then)} at {RECORDED_AT}"
        )
        if _served(then) != _served(now):
            served += 1
    if lines:
        lines.insert(
            0,
            f"{when}: {len(lines)} of {len(users)} flag answers changed, {served} of "
            "them to another value",
        )
    return lines


def compare(recorded: Dict[str, Any], observed: Dict[str, Any], when: str) -> List[str]:
    """Every difference of *observed* from *recorded*, in the order the job
    reads them: the assignments, the results, the flag answers."""
    return (
        _assignment_lines(recorded["assignments"], observed["assignments"], when)
        + _results_lines(recorded["results"], observed["results"], when)
        + _flag_lines(recorded["flags"], observed["flags"], when)
    )


def seeded(current: Dict[str, Any]) -> List[str]:
    """What is not as the seed makes it in *current*, N-1's document: every
    user assigned once, the experiment active, the results counting each
    variant's users and converting users of the plan exactly, and every flag
    answer decided by the rule for a user in the rule's country and by the
    rollout for every other."""
    when = SEEDED
    lines = []
    assigned: Dict[str, str] = {}
    for user, _country, _converts in PLAN:
        mine = current["assignments"].get(user) or []
        if len(mine) == 1:
            assigned[user] = str(mine[0].get("variant_name"))
        else:
            lines.append(f"{when}: {user} has {len(mine)} assignments, expected 1")
    results = current["results"]
    expected = {
        "status": "active",
        "total_users": len(PLAN),
        "total_conversions": sum(1 for _user, _country, converts in PLAN if converts),
    }
    for field, want in expected.items():
        if results.get(field) != want:
            lines.append(
                f"{when}: results {field} {_show(results.get(field))}, expected {_show(want)}"
            )
    if not results.get("variants"):
        lines.append(f"{when}: results have no variants of a primary metric")
    converting = {user for user, _country, converts in PLAN if converts}
    variants = results.get("variants") or {}
    for name in sorted(set(variants) | set(assigned.values())):
        users = {user for user, variant in assigned.items() if variant == name}
        want_counts = {
            "sample_size": len(users),
            "conversions": len(users & converting),
        }
        got = variants.get(name) or {}
        for field in VARIANT_FIELDS:
            if got.get(field) != want_counts[field]:
                lines.append(
                    f"{when}: results {name} {field} {_show(got.get(field))}, expected "
                    f"{want_counts[field]} (the seeded users assigned {name})"
                )
    for user, country, _converts in PLAN:
        answer = current["flags"].get(user) or {}
        if country == RULE_COUNTRY:
            want_answer: Dict[str, Any] = {"enabled": True, "reason": "targeting_rule"}
        else:
            want_answer = {"reason": "rollout"}
        for field, value in want_answer.items():
            if answer.get(field) != value:
                lines.append(
                    f"{when}: {user}'s flag answer is {_answer(answer)}, expected "
                    f"{field} {_show(value)} (country {country})"
                )
    return lines


def repeat(recorded: Dict[str, Any], directory: Path, when: str) -> List[str]:
    """Each repeat assign saved in *directory*/repeat that does not answer
    ``assigned: true`` with the variant recorded at N-1."""
    lines = []
    for user, _country, _converts in PLAN:
        answer = _read(directory / "repeat" / f"{user}.json")
        if not isinstance(answer, dict):
            raise Unreadable(f"the repeat assign of {user}: not an object")
        then = recorded["assignments"].get(user) or []
        got = {
            "variant_id": answer.get("variant_id"),
            "variant_name": answer.get("variant_name"),
        }
        if answer.get("assigned") is True and then == [got]:
            continue
        lines.append(
            f"{when}: assigning {user} again gave {_show(got['variant_name'])} "
            f"(assigned {_show(answer.get('assigned'))}, reason "
            f"{_show(answer.get('reason'))}), recorded {_names(then)} at {RECORDED_AT}"
        )
    if lines:
        lines.insert(
            0,
            f"{when}: {len(lines)} of {len(PLAN)} repeat assigns did not give the "
            f"variant recorded at {RECORDED_AT}",
        )
    return lines


# ---------------------------------------------------------------------------


def _report(lines: List[str]) -> int:
    for line in lines:
        print(line)
    return 1 if lines else 0


USAGE = (
    "usage: compose_upgrade_state.py plan | flag-rules | state DIR EXPERIMENT_ID"
    " | seeded STATE | compare RECORDED OBSERVED WHEN | repeat RECORDED DIR WHEN"
)


def main(argv: Sequence[str]) -> int:
    command, args = (argv[0], list(argv[1:])) if argv else ("", [])
    try:
        if command == "plan" and not args:
            for user, country, converts in PLAN:
                print(user, country, "yes" if converts else "no")
            return 0
        if command == "flag-rules" and not args:
            print(json.dumps(FLAG_RULES, sort_keys=True))
            return 0
        if command == "state" and len(args) == 2:
            print(json.dumps(state(Path(args[0]), args[1]), indent=2, sort_keys=True))
            return 0
        if command == "seeded" and len(args) == 1:
            return _report(seeded(load_state(Path(args[0]))))
        if command == "compare" and len(args) == 3:
            recorded, observed = load_state(Path(args[0])), load_state(Path(args[1]))
            return _report(compare(recorded, observed, args[2]))
        if command == "repeat" and len(args) == 3:
            return _report(repeat(load_state(Path(args[0])), Path(args[1]), args[2]))
    except Unreadable as exc:
        print(f"compose_upgrade_state.py {command}: {exc}", file=sys.stderr)
        return 2
    print(USAGE, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
