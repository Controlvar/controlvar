"""Use case #5 of D210 over the wire: a flag with targeting rules serves the
users the docs say, however an application asks. This checkout's Python and
JavaScript SDKs, in server mode and in local mode, and a raw request to the
API, against a running Controlvar (``_platform.yml``'s ``usecases-flags``
suite, nightly).

Two flags, each active at a global rollout of 0, both made through the API:
the documented example under "Add targeting rules" in
``docs/feature-flags/create.md`` (11 users, ``DOC``) and one semantic-version
condition (8 users, ``SEM``). The table of who gets what is
``ground_truth.targeting`` (O-TARGET): written from the docs, importing
nothing from the product, and checked against the server's evaluator, with no
server running, in the backend's unit job. Here the same rows are asked over
the wire, each by its own user, so no cache can answer one for another:

* five legs: a raw ``GET`` of the evaluate route; the Python and the
  JavaScript SDK in server mode (cache 300 s, the default); and both in
  local mode (refresh every R = 5 s), each started on the ruleset that holds
  both flags. Every user sends its attributes as ``context``.
* **L1, answers.** Every leg answers every row, with no error, as the table
  says: ``(enabled, reason)``.
* **L2, the source** (local legs only): ``local`` on the ten DOC rows an SDK
  evaluates from the ruleset, ``server`` on d10 (a boolean against a string
  rule value: the SDK defers) and on every SEM row (semantic version is not a
  local operator). A server-mode driver reports ``server`` whatever it did,
  so that half would be a gate that cannot fail; it is not read.
* **L3, evaluate requests**, counted by the proxy in front of each driver, by
  flag: a server leg sends one per row (DOC 11, SEM 8); a local leg sends DOC
  exactly 1 (d10) and SEM exactly 8, and at least one ruleset 200 went through
  its proxy (else it was not behind it). A request for a flag this case did
  not name fails.
* **L4.** No answer through any proxy is a 429.

Wall clock is reported in the step summary, never gated. The signed-in run,
Node, the proxied drivers and the helpers are ``conftest.py``'s. Nothing here
skips.
"""

from __future__ import annotations

import copy
import time
from collections.abc import Mapping
from typing import Any

from ground_truth import targeting
from usecases import gates, proxy, runner, sdks

from conftest import Live, _created, _fail_on, _synced, proxied

#: The ruleset refresh interval of a local driver (the SDKs' minimum), and
#: the cache lifetime of every driver (the SDKs' default, stated).
REFRESH = 5
CACHE_TTL = 300
#: Rows per flag, and the rows a local leg asks the server about: the ones
#: the table says an SDK defers (d10 for DOC, every SEM row).
DOC_COUNT = len(targeting.DOC_ROWS)
SEM_COUNT = len(targeting.SEM_ROWS)
DOC_DEFERRED = sum(1 for row in targeting.DOC_ROWS if row.local == "server")
SEM_DEFERRED = sum(1 for row in targeting.SEM_ROWS if row.local == "server")
TABLE: tuple[gates.TableRow, ...] = tuple(
    (row.id, row.enabled, row.reason) for row in targeting.ROWS
)
LOCAL_ROWS = frozenset(row.id for row in targeting.ROWS if row.local == "local")
RAW = "raw GET"


def _ask(
    session: runner.Runner, flag: str, user: str, context: Mapping[str, Any] | None
) -> gates.Poll:
    """One ``evaluate`` command with the row's attributes (``_poll`` of the
    other cases sends none)."""
    args: dict[str, Any] = {"flag": flag, "user": user, "default": None}
    if context is not None:
        args["context"] = dict(context)
    sent = time.monotonic()
    answer = session.call("evaluate", **args)
    return gates.Poll(
        sent=sent,
        answered=time.monotonic(),
        enabled=answer.get("enabled"),
        source=answer.get("source"),
        reason=answer.get("reason"),
        error=answer.get("error"),
    )


def test_g5_targeting_rules_serve_the_users_the_docs_say_on_every_leg(
    live: Live, node: sdks.Node
) -> None:
    started = time.monotonic()
    findings: list[str] = []
    outcome = "did not complete"
    try:
        doc_key = f"uc-g5-doc-{live.run}"
        sem_key = f"uc-g5-sem-{live.run}"
        for key, rules in (
            (doc_key, targeting.DOCUMENTED_RULES),
            (sem_key, targeting.SEMVER_RULES),
        ):
            _created(
                live.admin.create_flag(
                    key,
                    rollout_percentage=targeting.ROLLOUT_PERCENTAGE,
                    is_active=True,
                    targeting_rules=copy.deepcopy(rules),
                ),
                targeting.ROLLOUT_PERCENTAGE,
            )
        # Read once both flags exist: the version a local driver must hold.
        version = live.admin.ruleset()["version"]
        asked = [(row, doc_key) for row in targeting.DOC_ROWS] + [
            (row, sem_key) for row in targeting.SEM_ROWS
        ]
        users = {row.id: f"uc-g5-{live.run}-{row.id}" for row, _ in asked}

        answers: dict[str, dict[str, gates.Poll]] = {RAW: {}}
        for row, key in asked:
            sent = time.monotonic()
            answer = live.admin.evaluate(key, users[row.id], row.context)
            answers[RAW][row.id] = gates.Poll(
                sent=sent,
                answered=time.monotonic(),
                enabled=answer.get("enabled"),
                reason=answer.get("reason"),
            )

        records: dict[str, list[proxy.Record]] = {}
        for sdk in sdks.SDKS:
            for mode in ("server", "local"):
                leg = f"{sdk} {mode}"
                options: dict[str, Any] = {"cache_ttl_seconds": CACHE_TTL}
                if mode == "local":
                    options["refresh_seconds"] = REFRESH
                with proxied(
                    live, sdk, node if sdk == "js" else None, mode=mode, **options
                ) as (session, relay):
                    if mode == "local":
                        _synced(
                            session,
                            relay,
                            version,
                            f"the {sdk} local driver (the ruleset holding the G5"
                            " flags)",
                            caller="G5 targeting (setup)",
                            deadline=time.monotonic() + 3 * REFRESH,
                        )
                    answers[leg] = {
                        row.id: _ask(session, key, users[row.id], row.context)
                        for row, key in asked
                    }
                    relay.wait_idle(5.0)
                    records[leg] = relay.records()

        reports = []
        for leg, polls in answers.items():
            local = leg.endswith(" local")
            findings += gates.table_findings(leg, TABLE, polls)
            if local:
                findings += gates.source_findings(leg, polls, LOCAL_ROWS)
            if leg == RAW:
                continue
            findings += gates.evaluate_count_findings(
                records[leg],
                leg,
                expected={
                    doc_key: DOC_DEFERRED if local else DOC_COUNT,
                    sem_key: SEM_DEFERRED if local else SEM_COUNT,
                },
                require_ruleset=local,
            )
            findings += gates.pacing_findings(records[leg], f"{leg}'s proxy")
            calls = sum(
                1 for r in records[leg] if r.path.startswith(gates.EVALUATE_PREFIX)
            )
            reports.append(f"{leg} {calls}")
        outcome = (
            f"{len(TABLE)} rows on {len(answers)} legs, {len(findings)} findings;"
            f" evaluate requests: {', '.join(reports)}"
        )
    finally:
        live.summary.append(
            f"G5 targeting (refresh {REFRESH} s, cache {CACHE_TTL} s): {outcome};"
            f" {time.monotonic() - started:.0f} s"
        )
    _fail_on(findings, "G5 targeting")
