"""The over-the-wire gates (``usecases/gates.py``) against synthetic traces,
with no server and no SDK: each gate holds on a trace an SDK behaving as
documented would leave, and fires on the trace of the defect it exists for.

* G2-1's traces come from ``remote_trace``: an SDK whose cache really lives
  ``lifetime`` seconds, polled as the case polls it. With the lifetime the
  case configured the gate holds; with 1.5 times it (the cache outliving its
  lifetime) or half of it (asking again too early) it fires.
* G2-2's come from ``local_trace``: refreshes every 5 s, the toggle between
  two, the first 200 after it carrying the new version, unless the trace is
  told to serve the old one (a stale ruleset) or to answer off early.
* The ETag comparison is ``etag_of``'s: ``status()`` gives the bare version,
  the wire carries it in quotes.
* Use case #4's traces: ``outage_trace`` lays out the ruleset requests of an
  SDK through a 503 outage, each retry its backoff step after the previous
  failure's answer (plus a few ms, as a runner adds); with a plain interval,
  short steps or an early recovery the outage gate fires. The other #4 gates
  get small hand-made traces, each with the defect it exists for.
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from backend.tests.unit.usecases.harness import gates, proxy

pytestmark = pytest.mark.unit

Record = proxy.Record
ROUTE = "/api/v1/feature-flags/evaluate/uc-g21"
RULESET = proxy.RULESET_PATH
#: The lifetime the case configures (C).
TTL = 10.0


def _record(
    seq,
    t,
    path,
    status=200,
    etag=None,
    answered=None,
    *,
    fault=None,
    if_none_match=False,
    client_closed=False,
) -> Record:
    return Record(
        seq=seq,
        t_recv=t,
        t_answer=t + 0.01 if answered is None else answered,
        method="GET",
        path=path,
        status=status,
        etag=etag,
        if_none_match=if_none_match,
        fault=fault,
        client_closed=client_closed,
    )


# -- G2-1 -------------------------------------------------------------------------------
def remote_trace(lifetime: float, *, latency: float = 0.02):
    """What a server-mode SDK whose cache lives *lifetime* seconds leaves when
    polled as the case polls: command 1 at t1 (true, from the API), the flag
    turned off, then a poll every second until the first false or t1 + 19 s.
    Returns (records, t1, a1, polls)."""
    t1 = 100.0
    records = [_record(1, t1 + 0.005, ROUTE)]
    stored = t1 + latency
    a1 = stored + 0.001
    expires = stored + lifetime
    polls = []
    step = 1
    while t1 + step <= t1 + 2 * TTL - 1:
        sent = t1 + step
        handled = sent + 0.0005
        if handled >= expires:
            records.append(_record(len(records) + 1, sent + 0.001, ROUTE))
            polls.append(gates.Poll(sent, sent + latency, False, "server", "inactive"))
            break
        polls.append(gates.Poll(sent, sent + 0.001, True, "server", "rollout"))
        step += 1
    return records, t1, a1, polls


def _remote(lifetime: float, **changes):
    records, t1, a1, polls = remote_trace(lifetime)
    return gates.remote_refetch_findings(
        changes.get("records", records),
        ROUTE,
        t1=t1,
        a1=a1,
        ttl=TTL,
        polls=changes.get("polls", polls),
    )


def test_g2_1_holds_for_an_sdk_whose_cache_lives_its_lifetime():
    assert _remote(TTL) == []


def test_g2_1_fires_when_the_cache_outlives_its_lifetime():
    """The cache configured to 10 s but living 15: the poll at a1 + 10 s is
    still answered from it."""
    (finding,) = _remote(TTL * 1.5)
    assert "the cache outlived its 10 s lifetime" in finding


def test_g2_1_fires_when_the_sdk_asks_again_before_its_lifetime_ends():
    findings = _remote(TTL * 0.5)
    assert any("before its 10 s cache lifetime ended" in f for f in findings)
    assert any(
        "0 requests to the flag's route from command 1 + 10 s" in f for f in findings
    )


def test_g2_1_fires_when_the_sdk_never_asks_again():
    findings = _remote(1000.0)
    assert "no poll answered false" in findings
    assert any("0 requests" in f for f in findings)


def test_g2_1_fires_on_a_second_request_after_the_lifetime():
    records, *_ = remote_trace(TTL)
    extra = replace(records[-1], seq=len(records) + 1, t_recv=records[-1].t_recv + 0.5)
    assert any("2 requests" in f for f in _remote(TTL, records=[*records, extra]))


def test_g2_1_fires_when_the_poll_that_asked_again_answered_true():
    _, _, _, polls = remote_trace(TTL)
    stale = [*polls[:-1], replace(polls[-1], enabled=True)]
    findings = _remote(TTL, polls=stale)
    assert any("answered True, not false" in f for f in findings)


def test_g2_1_fires_when_a_cached_poll_did_not_answer_true():
    _, _, _, polls = remote_trace(TTL)
    flipped = [replace(polls[0], enabled=False), *polls[1:]]
    assert any("from the cache" in f for f in _remote(TTL, polls=flipped))


def test_g2_1_is_void_when_the_sdk_is_not_behind_the_proxy():
    (finding,) = _remote(TTL, records=[])
    assert finding.startswith("void:")


# -- G2-2 -------------------------------------------------------------------------------
V1, V2 = "1" * 64, "2" * 64


def local_trace(*, served_after: str = V2, off_at: float = 12.5):
    """A local-mode SDK refreshing every 5 s (a 200 with V1 at 2 s, a 304 at
    7 s), the toggle written at 10 s, the next refresh at 12 s answered 200
    with *served_after*, and polls every half second from 10 s answering true
    until *off_at* (false, ``inactive``). Returns (records, polls)."""
    records = [
        _record(1, 2.0, RULESET, 200, gates.etag_of(V1)),
        _record(2, 7.0, RULESET, 304, gates.etag_of(V1)),
        _record(3, 12.0, RULESET, 200, gates.etag_of(served_after)),
        _record(4, 13.0, "/api/v1/tracking/evaluations", 202),
    ]
    polls = [gates.Poll(9.0, 9.001, True, "local", "rollout")]
    sent = 10.5
    while sent <= 25.0:
        off = sent >= off_at
        polls.append(
            gates.Poll(
                sent, sent + 0.001, not off, "local", "inactive" if off else "rollout"
            )
        )
        if off:
            break
        sent += 0.5
    return records, polls


def _local(records, polls, *, before=V1, after=V2):
    return gates.local_kill_findings(
        records,
        version_before=before,
        toggle_sent=10.0,
        version_after=after,
        polls=polls,
    )


def test_g2_2_holds_for_an_sdk_that_turns_off_with_its_next_ruleset():
    assert _local(*local_trace()) == []


def test_g2_2_fires_on_a_ruleset_200_still_carrying_the_old_version():
    findings = _local(*local_trace(served_after=V1, off_at=99.0))
    assert any("still carry" in f for f in findings)
    assert any("the server serves" in f for f in findings)
    assert "no poll answered false" in findings


def test_g2_2_compares_the_wire_s_etag_with_status_s_version_in_quotes():
    """status() says the bare version; the ETag is that version in quotes.
    A trace whose wire and status() agree holds; a status() naming another
    version fires."""
    records, polls = local_trace()
    assert records[0].etag == f'"{V1}"' != V1
    findings = _local(records, polls, before="0" * 64)
    assert any("but status() says version" in f for f in findings)


def test_g2_2_fires_when_the_server_s_version_did_not_move():
    findings = _local(*local_trace(), after=V1)
    assert "the server's ruleset version did not change with the toggle" in findings


def test_g2_2_fires_when_off_is_answered_before_the_new_ruleset_arrived():
    records, polls = local_trace(off_at=11.0)
    assert any("before the first new ruleset" in f for f in _local(records, polls))


def test_g2_2_fires_on_an_evaluate_call_through_the_proxy():
    records, polls = local_trace()
    called = [*records, _record(9, 11.0, "/api/v1/feature-flags/evaluate/uc-g22")]
    assert any("1 evaluate calls" in f for f in _local(called, polls))


def test_g2_2_fires_on_an_answer_that_was_not_local():
    records, polls = local_trace()
    remote = [*polls[:2], replace(polls[2], source="server"), *polls[3:]]
    assert any("not answered locally" in f for f in _local(records, remote))


def test_g2_2_fires_on_the_wrong_reason():
    records, polls = local_trace()
    wrong = [*polls[:-1], replace(polls[-1], reason="rollout")]
    assert any("the reason 'rollout'" in f for f in _local(records, wrong))


def test_g2_2_is_void_without_a_ruleset_before_the_toggle():
    records, polls = local_trace()
    findings = _local(records[2:], polls)
    assert any(f.startswith("void:") for f in findings)


# -- the shared gates -------------------------------------------------------------------
def test_etag_of_quotes_the_version():
    assert gates.etag_of("abc") == '"abc"'
    assert gates.etag_of(None) is None


def test_429_anywhere_fires_the_pacing_guard():
    records = [_record(1, 1.0, RULESET), _record(2, 2.0, RULESET, 429)]
    assert gates.pacing_findings(records[:1], "p") == []
    (finding,) = gates.pacing_findings(records, "p")
    assert "1 answers were 429" in finding and RULESET in finding


def test_a_local_driver_must_be_behind_its_proxy_and_never_evaluate_remotely():
    ruleset = _record(1, 1.0, RULESET, 200, '"v"')
    assert gates.local_proxy_findings([ruleset], "d") == []
    (void,) = gates.local_proxy_findings([], "d")
    assert void.startswith("void:")
    called = [ruleset, _record(2, 2.0, gates.EVALUATE_PREFIX + "f")]
    (finding,) = gates.local_proxy_findings(called, "d")
    assert "1 calls to the evaluate route" in finding


def _bucket(user: str, flag: str) -> int:
    return int(user[1:]) % 100


def test_o_flag_holds_when_every_answer_is_the_oracle_s():
    users = [f"u{i}" for i in range(200)]
    answers = [(u, _bucket(u, "f") < 30) for u in users]
    assert gates.o_flag_findings("raw", "f", 30, answers, users, _bucket) == []


@pytest.mark.parametrize(
    ("change", "fragment"),
    [
        (lambda a: [*a[:5], (a[5][0], not a[5][1]), *a[6:]], "1 of 200 answers"),
        (
            lambda a: [*a[:5], (a[5][0], ("not local", "server")), *a[6:]],
            "1 of 200 answers",
        ),
        (lambda a: a[:-1], "199 answers for 200 users"),
        (lambda a: [a[1], a[0], *a[2:]], "or not in their order"),
    ],
    ids=["flipped", "not-local", "missing", "misordered"],
)
def test_o_flag_fires_on_a_wrong_missing_or_misordered_answer(change, fragment):
    users = [f"u{i}" for i in range(200)]
    answers = [(u, _bucket(u, "f") < 30) for u in users]
    (finding,) = gates.o_flag_findings("raw", "f", 30, change(answers), users, _bucket)
    assert fragment in finding


# -- relayed ruleset 200s only (local_proxy and local_kill) ----------------------------
def test_a_corrupt_200_is_not_the_proof_that_a_driver_is_behind_its_proxy():
    corrupt = _record(1, 1.0, RULESET, 200, fault="corrupt:truncated")
    (void,) = gates.local_proxy_findings([corrupt], "d")
    assert void.startswith("void:")
    relayed = _record(2, 2.0, RULESET, 200, '"v"')
    assert gates.local_proxy_findings([corrupt, relayed], "d") == []


def _with_corrupt_after_the_toggle(records):
    """*records* (``local_trace``'s) with a corrupt 200 at 11 s, between the
    toggle (10 s) and the next relayed ruleset (12 s), in sequence order as
    ``records()`` returns them."""
    corrupt = _record(3, 11.0, RULESET, 200, fault="corrupt:truncated")
    later = [replace(r, seq=r.seq + 1) for r in records[2:]]
    return [*records[:2], corrupt, *later]


def test_g2_2_ignores_a_corrupt_200_and_still_sees_a_stale_one():
    """A corrupt 200 between the toggle and the new ruleset carries no ETag:
    it is not "the first ruleset after the toggle". A relayed stale 200 still
    fires with it present."""
    records, polls = local_trace()
    assert _local(_with_corrupt_after_the_toggle(records), polls) == []
    stale, _ = local_trace(served_after=V1, off_at=99.0)
    findings = _local(_with_corrupt_after_the_toggle(stale), polls)
    assert any("still carry" in f for f in findings)


# -- use case #4: the outage ---------------------------------------------------------------
#: The refresh interval (R) and the documented backoff after 1-4 failures.
R = 5.0
STEPS = (5.0, 10.0, 20.0, 40.0)


def outage_trace(
    delays=STEPS, *, faulted=5, recovery=80.0, latency=0.02, overshoot=0.003
):
    """A relayed 200 at 95 s; *faulted* ruleset requests answered 503 by the
    proxy from 102 s, each the previous answer + its delay + *overshoot*
    later; then, unless *recovery* is None, a relayed 304 (sent with
    If-None-Match) *recovery* + *overshoot* after the last answer."""
    records = [_record(1, 95.0, RULESET, 200, '"v1"')]
    t = 102.0
    for k in range(faulted):
        records.append(
            _record(
                len(records) + 1,
                t,
                RULESET,
                503,
                answered=t + latency,
                fault="status:503",
                if_none_match=True,
            )
        )
        if k < faulted - 1:
            t += latency + delays[k] + overshoot
    if recovery is not None:
        start = records[-1].t_answer + recovery + overshoot
        records.append(
            _record(len(records) + 1, start, RULESET, 304, '"v1"', if_none_match=True)
        )
    return records


def test_the_outage_gate_holds_for_the_documented_backoff():
    assert gates.outage_findings(outage_trace(), "d") == []
    # Steps measured from the end of each failure, so latency does not matter.
    assert gates.outage_findings(outage_trace(latency=0.3), "d") == []


def test_the_outage_gate_fires_on_a_plain_interval():
    """Retrying every R s through the 120 s cut: 24 faulted, not 5."""
    findings = gates.outage_findings(
        outage_trace((R,) * 23, faulted=24, recovery=R), "d"
    )
    assert "d: 24 faulted ruleset requests during the cut, expected 5" in findings
    assert any("22 retries came before their backoff step" in f for f in findings)
    assert any("first ruleset request after the outage" in f for f in findings)


def test_the_outage_gate_fires_on_short_steps():
    findings = gates.outage_findings(outage_trace(tuple(s / 2 for s in STEPS)), "d")
    assert findings == [
        "d: 4 retries came before their backoff step: retry 2 came 2.50 s after"
        " failure 1, expected at least 5 s"
    ]


def test_the_outage_gate_fires_on_an_early_recovery():
    (finding,) = gates.outage_findings(outage_trace(recovery=40.0), "d")
    assert "came 40.00 s after the last failure, expected at least 80 s" in finding


def test_the_outage_gate_fires_when_recovery_was_not_seen():
    (finding,) = gates.outage_findings(outage_trace(recovery=None), "d")
    assert "recovery was not seen" in finding


def test_the_outage_gate_fires_on_a_relayed_request_inside_the_outage():
    records = outage_trace()
    inside = _record(99, records[2].t_answer + 1.0, RULESET, 200, '"v1"')
    renumbered = [*records[:3], inside, *records[3:]]
    renumbered = [replace(r, seq=i + 1) for i, r in enumerate(renumbered)]
    findings = gates.outage_findings(renumbered, "d")
    assert any("1 relayed ruleset requests came between" in f for f in findings)


def test_the_outage_gate_fires_on_another_answer_than_503():
    records = outage_trace()
    records[3] = replace(records[3], status=502, fault="status:502")
    (finding,) = gates.outage_findings(records, "d")
    assert "[(502, 'status:502')], expected every one 503" in finding


def test_the_outage_gate_is_void_when_nothing_was_faulted():
    (finding,) = gates.outage_findings(outage_trace(faulted=0, recovery=None), "d")
    assert finding.startswith("void:")


def test_the_outage_report_gives_f_the_excess_and_the_recovery():
    report = gates.outage_report(outage_trace(), t_cut=100.0)
    assert report.startswith("f 2.00 s after the cut")
    assert "[+0.00, +0.00, +0.00, +0.00]" in report
    assert "next request after the last failure 80.00 s" in report


def corrupt_trace(*, faulted=3, recovery=20.0):
    """``outage_trace`` with each faulted request a synthesized corrupt 200
    (G4-2 warm): the SDK backs off from it as from a 503."""
    return [
        replace(r, status=200, fault="corrupt:truncated") if r.fault else r
        for r in outage_trace(faulted=faulted, recovery=recovery)
    ]


def test_the_outage_gate_holds_a_corrupt_window_by_its_tag():
    findings = gates.outage_findings(
        corrupt_trace(), "d", expected=3, fault="corrupt:truncated"
    )
    assert findings == []


def test_the_outage_gate_fires_on_a_corrupt_window_cleared_late():
    """clear() after the 4th corrupt answer, not the 3rd: 4 faulted."""
    findings = gates.outage_findings(
        corrupt_trace(faulted=4, recovery=40.0),
        "d",
        expected=3,
        fault="corrupt:truncated",
    )
    assert findings == ["d: 4 faulted ruleset requests during the cut, expected 3"]


def test_the_outage_gate_holds_a_corrupt_window_only_to_the_tag_it_is_given():
    (finding,) = gates.outage_findings(corrupt_trace(), "d", expected=3)
    assert "expected every one 503 (status:503)" in finding
    (finding,) = gates.outage_findings(
        corrupt_trace(), "d", expected=3, fault="corrupt:html"
    )
    assert finding.endswith(
        "[(200, 'corrupt:truncated')], expected every one corrupt:html"
    )


def test_the_retry_deadline_is_the_last_failure_plus_its_step_plus_r():
    records = outage_trace(recovery=None)
    # No fault yet: the first one is due by since + first_wait.
    assert gates.retry_deadline(
        records[:1], refresh=R, since=96.0, first_wait=10.5
    ) == pytest.approx(106.5)
    # After the 3rd failure the 4th comes 20 s (4R) after its answer.
    third = records[3]
    assert third.fault is not None
    assert gates.retry_deadline(
        records[:4], refresh=R, since=0.0, first_wait=0.0
    ) == pytest.approx(third.t_answer + 20.0 + R)
    # A relayed ruleset request ends the streak.
    after = _record(9, 300.0, RULESET, 304, '"v1"', if_none_match=True)
    assert gates.retry_deadline(
        [*records, after], refresh=R, since=301.0, first_wait=7.0
    ) == pytest.approx(308.0)


def test_the_retry_deadline_never_ends_before_the_next_documented_retry():
    """For every prefix of an outage, the next ruleset request (each retry,
    then the recovery 80 s after the 5th failure) comes before the deadline.
    _synced's fixed 3R from the last failure would end 5 s before the retry
    after the 3rd failure and 25 s before the one after the 4th."""
    records = outage_trace(recovery=80.0)
    for count in range(2, len(records)):
        deadline = gates.retry_deadline(
            records[:count], refresh=R, since=0.0, first_wait=0.0
        )
        assert records[count].t_recv < deadline, count
    for k in (3, 4):
        assert records[k + 1].t_recv > records[k].t_answer + 3 * R


def _statuses(*reads):
    """``status()`` reads: (time, ready, version, with last_error)."""
    return [
        (
            t,
            {
                "ready": ready,
                "ruleset_version": version,
                "last_error": {"type": "E", "status": 503} if failed else None,
            },
        )
        for t, ready, version, failed in reads
    ]


def test_serving_holds_when_every_read_is_ready_on_the_old_ruleset():
    records = outage_trace()
    last = records[5].t_answer
    statuses = _statuses(
        (101.0, True, "v1", False),
        (110.0, True, "v1", True),
        (last + 1, True, "v1", True),
    )
    assert gates.serving_findings(statuses, records, "d", version="v1") == []


def test_serving_fires_on_a_discarded_or_unusable_ruleset():
    """A cache can keep a discarded ruleset's answers; status() cannot. A
    ruleset kept but no longer used (too stale, say) still has its version:
    only ``ready`` shows it."""
    records = outage_trace()
    last = records[5].t_answer
    for ready, version in ((False, None), (False, "v1")):
        statuses = _statuses(
            (101.0, True, "v1", False),
            (110.0, ready, version, True),
            (last + 1, True, "v1", True),
        )
        (finding,) = gates.serving_findings(statuses, records, "d", version="v1")
        assert finding == (
            "d: 1 of 3 status() reads were not ready on version 'v1' (the first:"
            f" ready False, version {version!r})"
        )


def test_serving_is_void_when_no_read_came_after_the_last_fault():
    records = outage_trace()
    last = records[5].t_answer
    early = _statuses((101.0, True, "v1", False), (last - 1, True, "v1", True))
    (void,) = gates.serving_findings(early, records, "d", version="v1")
    assert void.startswith("void: d: no status() read after the last faulted")
    unseen = _statuses((last + 1, True, "v1", False))
    (void,) = gates.serving_findings(unseen, records, "d", version="v1")
    assert void.startswith("void: d: no status() read after the last faulted")


def test_serving_is_void_without_reads_or_faults():
    (void,) = gates.serving_findings([], outage_trace(), "d", version="v1")
    assert void.startswith("void:")
    statuses = _statuses((101.0, True, "v1", True))
    (void,) = gates.serving_findings(
        statuses, outage_trace(faulted=0), "d", version="v1"
    )
    assert void == "void: d: no ruleset request was faulted"


def test_the_backoff_step_doubles_from_r_up_to_the_cap():
    assert [gates.backoff_step(R, k) for k in range(1, 9)] == [
        5.0,
        10.0,
        20.0,
        40.0,
        80.0,
        160.0,
        300.0,
        300.0,
    ]


# -- use case #4: the error tally ------------------------------------------------------------
NO_ERRORS = dict.fromkeys(gates.ERROR_KEYS, 0)


def test_the_error_tally_holds_when_it_counts_every_faulted_request():
    records = outage_trace()
    assert gates.error_tally_findings({**NO_ERRORS, "refresh": 5}, records, "d") == []
    flushes = [
        replace(
            _record(i, float(i), "/api/v1/tracking/evaluations", 503),
            fault="status:503",
        )
        for i in (1, 2)
    ]
    assert (
        gates.error_tally_findings(
            {**NO_ERRORS, "flush": 2},
            flushes,
            "d",
            operation="flush",
            path="/api/v1/tracking/evaluations",
        )
        == []
    )


def test_the_error_tally_fires_on_a_driver_that_counts_nothing():
    """A driver whose client has no on_error counts 0 through the outage."""
    (finding,) = gates.error_tally_findings(NO_ERRORS, outage_trace(), "d")
    assert finding == "d: on_error(refresh) 0, the proxy faulted 5"


@pytest.mark.parametrize(
    "errors", [None, {}, {"refresh": 5}, {**NO_ERRORS, "value": 0}], ids=repr
)
def test_the_error_tally_fires_when_the_status_carries_no_tally(errors):
    (finding,) = gates.error_tally_findings(errors, outage_trace(), "d")
    assert "carries no error tally" in finding


def test_the_error_tally_is_void_when_nothing_was_faulted():
    (finding,) = gates.error_tally_findings(NO_ERRORS, outage_trace(faulted=0), "d")
    assert finding.startswith("void:")


# -- use case #4: the last known answers ------------------------------------------------------
A0 = {("f1", "u1"): True, ("f1", "u2"): False, ("f2", "u1"): True}


def _round():
    """One round answering every pair of A0 as A0 does, locally."""
    return {
        pair: gates.Poll(1.0, 1.001, enabled, "local", "rollout")
        for pair, enabled in A0.items()
    }


def test_last_known_holds_when_every_round_answers_a0_locally():
    assert gates.last_known_findings(A0, [_round(), _round()], "d") == []


@pytest.mark.parametrize(
    ("poll", "fragment"),
    [
        (
            gates.Poll(1.0, 1.1, False, "local", "rollout"),
            "1 of 3 answers differ from A0",
        ),
        (gates.Poll(1.0, 1.1, True, "server", "rollout"), "1 answers were not local"),
        (
            gates.Poll(1.0, 1.1, True, "local", None, {"type": "E", "status": None}),
            "1 answers were not local or carried an error",
        ),
    ],
    ids=["changed", "server", "error"],
)
def test_last_known_fires_on_an_answer_that_is_not_a0_s_local_one(poll, fragment):
    second = _round()
    second[("f1", "u1")] = poll
    (finding,) = gates.last_known_findings(A0, [_round(), second], "d")
    assert finding.startswith("d: round 2") and fragment in finding


def test_last_known_fires_on_a_missing_pair_and_is_void_without_rounds():
    short = _round()
    del short[("f2", "u1")]
    (finding,) = gates.last_known_findings(A0, [short], "d")
    assert "(1 missing, 0 not in A0)" in finding
    (void,) = gates.last_known_findings(A0, [], "d")
    assert void.startswith("void:")


# -- use case #4: deferral ----------------------------------------------------------------------
def _evaluates(*keys):
    return [
        _record(i, float(i), gates.EVALUATE_PREFIX + key)
        for i, key in enumerate(keys, 1)
    ]


def test_deferral_holds_at_one_request_per_distinct_user_for_remote_flags_only():
    records = [_record(1, 0.5, RULESET, 200, '"v"'), *_evaluates("r1", "r1", "r2")]
    remote = {"r1": ["u1", "u2", "u2"], "r2": ["u1"]}
    assert gates.deferral_findings(records, "d", remote=remote, local={"l1"}) == []


@pytest.mark.parametrize(
    ("keys", "fragment"),
    [
        (("r1", "r1", "l1"), "1 evaluate requests for the local flag 'l1', expected 0"),
        (
            ("r1", "r1", "r1"),
            "3 evaluate requests for the remote flag 'r1', expected 2",
        ),
        (("r1",), "1 evaluate requests for the remote flag 'r1', expected 2"),
        (("r1", "r1", "x9"), "flags the case did not name: ['x9']"),
    ],
    ids=["local-deferred", "over-deferred", "under-deferred", "unnamed"],
)
def test_deferral_fires_on_the_wrong_count(keys, fragment):
    findings = gates.deferral_findings(
        _evaluates(*keys), "d", remote={"r1": ["u1", "u2"]}, local={"l1"}
    )
    assert any(fragment in f for f in findings), findings


def test_deferral_is_void_with_no_flag_named():
    (void,) = gates.deferral_findings(_evaluates("r1"), "d", remote={}, local=())
    assert void.startswith("void:")


# -- use case #4: the kept ETag -----------------------------------------------------------------
def _corrupt_then(first_after):
    records = [_record(1, 1.0, RULESET, 200, '"v1"')]
    for seq in (2, 3, 4):
        records.append(
            _record(
                seq,
                float(seq),
                RULESET,
                200,
                fault="corrupt:truncated",
                if_none_match=True,
            )
        )
    if first_after is not None:
        records.append(first_after)
    return records


def test_kept_etag_holds_on_a_304_to_if_none_match_after_the_faults():
    after = _record(5, 30.0, RULESET, 304, '"v1"', if_none_match=True)
    assert gates.kept_etag_findings(_corrupt_then(after), "d") == []


def test_kept_etag_fails_when_the_sdk_asked_again_without_its_etag():
    """The SDK discarded its ruleset: the next refresh carries no
    If-None-Match and the server answers 200."""
    after = _record(5, 30.0, RULESET, 200, '"v1"')
    (finding,) = gates.kept_etag_findings(_corrupt_then(after), "d")
    assert "was 200 to a request without If-None-Match, expected a 304" in finding


def test_kept_etag_is_void_without_faults_and_fails_without_a_request_after():
    (void,) = gates.kept_etag_findings([_record(1, 1.0, RULESET, 200, '"v1"')], "d")
    assert void.startswith("void:")
    (finding,) = gates.kept_etag_findings(_corrupt_then(None), "d")
    assert "was not seen" in finding


# -- use case #4: a hung refresh ---------------------------------------------------------------
def _held(client_closed=True):
    return _record(
        2,
        10.0,
        RULESET,
        0,
        answered=13.0,
        fault="blackhole",
        if_none_match=True,
        client_closed=client_closed,
    )


def _polls_during(count, *, start=10.1, every=0.2):
    """*count* polls of one pair of A0, every *every* s from *start*, each
    answered A0's way, locally, in a millisecond."""
    pair = ("f1", "u1")
    return [
        (pair, gates.Poll(start + i * every, start + i * every + 0.001, True, "local"))
        for i in range(count)
    ]


def test_a_hung_refresh_holds_when_evaluation_never_waited():
    records = [_record(1, 1.0, RULESET, 200, '"v1"'), _held()]
    assert gates.hung_refresh_findings(records, A0, _polls_during(12), "d") == []


def test_a_hung_refresh_fires_when_evaluation_waited_on_it():
    """Polls that waited for the refresh are answered after the hold ended:
    none counts as inside it."""
    blocked = [(pair, replace(poll, answered=13.5)) for pair, poll in _polls_during(12)]
    (finding,) = gates.hung_refresh_findings([_held()], A0, blocked, "d")
    assert (
        "0 polls were sent and answered while the ruleset request was held" in finding
    )


def test_a_hung_refresh_fires_when_the_sdk_never_gave_up_the_request():
    (finding,) = gates.hung_refresh_findings(
        [_held(client_closed=False)], A0, _polls_during(12), "d"
    )
    assert "did not give up the held ruleset request" in finding


def test_a_hung_refresh_fires_on_an_answer_that_was_not_a0_s_local_one():
    polls = _polls_during(12)
    polls[3] = (polls[3][0], replace(polls[3][1], source="server"))
    (finding,) = gates.hung_refresh_findings([_held()], A0, polls, "d")
    assert "1 of the polls during the hold were not A0's local answer" in finding


def test_a_hung_refresh_is_void_without_a_held_request():
    (void,) = gates.hung_refresh_findings([], A0, _polls_during(12), "d")
    assert void.startswith("void:")


# -- use case #4: an upstream failure is not a fault ---------------------------------------------
def test_an_unfaulted_502_is_its_own_finding():
    injected = _record(1, 1.0, RULESET, 502, fault="status:502")
    assert gates.upstream_failure_findings([injected], "d") == []
    failed = _record(2, 2.0, RULESET, 502)
    (finding,) = gates.upstream_failure_findings([injected, failed], "d")
    assert "1 unfaulted 502 answers (/api/v1/sdk/ruleset)" in finding
