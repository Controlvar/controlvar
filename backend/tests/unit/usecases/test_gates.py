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


def _record(seq, t, path, status=200, etag=None, answered=None) -> Record:
    return Record(
        seq=seq,
        t_recv=t,
        t_answer=t + 0.01 if answered is None else answered,
        method="GET",
        path=path,
        status=status,
        etag=etag,
        if_none_match=False,
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
