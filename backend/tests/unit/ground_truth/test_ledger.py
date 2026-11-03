"""L1 and L2: the ledger format, read and written by one module.

L1: a replayed plan round-trips; a stream written with only the format's
required fields (as the demo simulators would write it, without the replay's
optional ``route``, ``seq``, ``batch_index`` and ``event_id``) is read; and
where a rule of the contract needs one of those fields, ``valid_events``
refuses by name. Making an optional field required refuses that stream.

L2: the ledger refuses a 5xx, a transport error, an answer with no send and a
header, whether it is writing or reading; and every other refusal of the
format. Dropping a refusal lets its case through.
"""

from __future__ import annotations

import copy
import io
import json

import pytest

from backend.tests.unit.ground_truth.library import (
    contract,
    generator,
    hashing,
    ledger,
    records,
    replay,
)

pytestmark = pytest.mark.unit

g = generator
EXP = "hero"
ARMS = [("control", 50), ("video", 50)]


def _variant(user: str) -> str:
    return hashing.variant_for(user, EXP, ARMS)


#: A stream with the format's required fields only, every kind once or more.
MINIMAL = [
    {
        "kind": "start",
        "source": "a-simulator",
        "seed": 7,
        "experiments": [
            {
                "exp": EXP,
                "variants": [{"name": n, "allocation": a} for n, a in ARMS],
            }
        ],
        "flags": [],
        "planted": {"control": 0.1, "video": 0.12},
        "declared": {},
    },
    {"kind": "action", "action": "start", "exp": EXP, "status": 200, "attempts": 1},
    {
        "kind": "assign",
        "user": "sim-0001",
        "exp": EXP,
        "status": 200,
        "assigned": True,
        "variant": _variant("sim-0001"),
        "reason": "assigned",
        "attempts": 1,
    },
    {
        "kind": "flag",
        "user": "sim-0001",
        "flag": "new-nav",
        "status": 200,
        "enabled": True,
        "reason": "rollout",
        "attempts": 1,
    },
    {
        "kind": "event",
        "user": "sim-0001",
        "exp": EXP,
        "flag": None,
        "event_type": "purchase",
        "event_name": None,
        "value": 12.5,
        "status": 200,
        "attempts": 1,
    },
    {
        "kind": "batch",
        "size": 1,
        "status": 200,
        "success_count": 1,
        "failure_count": 0,
        "errors": None,
        "attempts": 1,
    },
    {
        "kind": "event",
        "user": "sim-0001",
        "exp": EXP,
        "flag": None,
        "event_type": "purchase",
        "event_name": None,
        "value": 3.25,
        "status": 200,
        "attempts": 1,
    },
    {
        "kind": "alias",
        "user": "sim-0001",
        "alias": "anon-9",
        "status": 200,
        "attempts": 1,
    },
    {
        "kind": "end",
        "counts": {
            "assign": 1,
            "flag": 1,
            "event": 2,
            "batch": 1,
            "alias": 1,
            "action": 1,
            "429": 0,
        },
        "errors": [],
    },
]


def _lines(stream: list[dict]) -> list[str]:
    return [ledger.dumps(record) + "\n" for record in stream]


def _read(stream: list[dict], **kwargs) -> list[dict]:
    return ledger.read(_lines(stream), **kwargs)


SMALL = g.Scenario(
    name="ledger",
    experiment_key="gt-ledger",
    arms=(
        g.Arm("control", 50, 0.3, repeat=0.2, values=g.Values(2.0, 0.5)),
        g.Arm("treatment", 50, 0.3, repeat=0.2, values=g.Values(2.0, 0.5)),
    ),
    users=300,
    event_name="purchase",
    metrics=(g.Metric("revenue", "purchase", "revenue"),),
    assign_batch_size=100,
    event_batch_size=25,
)


# ---------------------------------------------------------------------------
# L1
# ---------------------------------------------------------------------------
def test_a_replayed_plan_round_trips():
    plan = g.simulate(3, SMALL)
    text = replay(plan, declared={"event_dedup": True})
    found = records(text)
    assert "".join(ledger.dumps(r) + "\n" for r in found) == text
    start = {k: v for k, v in found[0].items() if k not in ("v", "kind")}
    assert start == json.loads(json.dumps(plan.start({"event_dedup": True})))
    kinds = {r["kind"] for r in found}
    assert kinds == {"start", "action", "assign", "event", "batch", "end"}
    assert all(r["v"] == 1 for r in found)


def test_a_stream_with_only_the_required_fields_is_read():
    found = _read(MINIMAL)
    assert [r["kind"] for r in found] == [r["kind"] for r in MINIMAL]


def test_the_optional_fields_are_exactly_the_four_and_never_required():
    optional = {name for fields in ledger.ADDITIVE.values() for name in fields}
    assert optional == {"route", "seq", "batch_index", "event_id"}
    for kind, fields in ledger.ADDITIVE.items():
        assert not set(fields) & set(ledger.REQUIRED[kind]), kind


def _with(stream: list[dict], index: int, **fields) -> list[dict]:
    changed = copy.deepcopy(stream)
    changed[index].update(fields)
    return changed


_ROUTED_ASSIGN = _with(MINIMAL, 2, route="/assign")
_ROUTED_EVENTS = _with(
    _with(_ROUTED_ASSIGN, 4, route="/track", event_id=None), 6, route="/batch"
)
_BATCHED = _with(
    _with(_ROUTED_EVENTS, 5, seq=9, route="/batch"), 6, seq=9, batch_index=0
)
_DEDUP = _with(_BATCHED, 0, declared={"event_dedup": True})


@pytest.mark.parametrize(
    "stream, message",
    [
        (MINIMAL, "R1 needs `route` for an assign record (record 3)"),
        (
            _with(_ROUTED_ASSIGN, 4, event_id=None),
            "R0 needs `route` for an event (record 5)",
        ),
        (_ROUTED_EVENTS, "R0 needs `batch_index` for a /batch event (record 7)"),
        (
            _BATCHED,
            "R4 needs `event_id` (null when none was sent) for an event"
            " (record 7), or `event_dedup` in start's `declared`",
        ),
        (
            _DEDUP,
            "R4 needs `event_id` (null when none was sent) for an event (record 7)",
        ),
    ],
    ids=["route-assign", "route-event", "batch_index", "event_id", "dedup"],
)
def test_valid_events_refuses_by_name_where_a_rule_needs_an_optional_field(
    stream, message
):
    found = _read(stream)
    with pytest.raises(contract.ContractRefused) as refused:
        contract.valid_events(found)
    text = str(refused.value)
    assert text == message or text.startswith(message + ":"), text


def test_with_the_optional_fields_the_same_stream_is_counted():
    stream = _with(_DEDUP, 6, event_id="order-1")
    expected = contract.valid_events(_read(stream))
    hero = expected.experiments[EXP]
    variant = _variant("sim-0001")
    assert hero.counts(variant, "purchase") == [2]
    assert hero.value_totals(variant, "purchase") == [15.75]
    assert hero.total_events == 3  # the exposure and the two purchases


# ---------------------------------------------------------------------------
# L2: the writer
# ---------------------------------------------------------------------------
def _writer(**kwargs) -> tuple[ledger.Writer, io.StringIO]:
    stream = io.StringIO()
    writer = ledger.Writer(
        stream,
        source="test",
        seed=1,
        experiments=[
            {"exp": EXP, "variants": [{"name": n, "allocation": a} for n, a in ARMS]}
        ],
        **kwargs,
    )
    return writer, stream


def _assign_body(user: str) -> dict:
    return {
        "user_id": user,
        "variant_name": _variant(user),
        "assigned": True,
        "reason": "assigned",
    }


def _refused_then_explained(stream: io.StringIO, message: str) -> None:
    """The file ends with an end that says why, and reading it is refused."""
    last = json.loads(stream.getvalue().splitlines()[-1])
    assert last["kind"] == "end" and message in last["errors"][0], last
    with pytest.raises(ledger.LedgerRefused, match="the run was refused"):
        ledger.read(io.StringIO(stream.getvalue()))


def test_the_writer_refuses_a_5xx():
    writer, stream = _writer()
    seq = writer.send(ledger.TRACK, {"event_type": "purchase", "user_id": "u-1"})
    with pytest.raises(ledger.LedgerRefused, match="the server answered 503"):
        writer.answer(seq, 503, {"detail": "unavailable"})
    _refused_then_explained(stream, "the server answered 503")
    with pytest.raises(ledger.LedgerRefused, match="after end"):
        writer.send(ledger.TRACK, {"event_type": "purchase", "user_id": "u-1"})


def test_the_writer_refuses_a_transport_error():
    writer, stream = _writer()
    seq = writer.send(ledger.ASSIGN, {"experiment_key": EXP, "user_id": "u-1"})
    with pytest.raises(ledger.LedgerRefused, match=r"got no answer \(timeout\)"):
        writer.transport_error(seq, "timeout")
    _refused_then_explained(stream, "got no answer (timeout)")


def test_the_writer_refuses_an_answer_with_no_send():
    writer, stream = _writer()
    with pytest.raises(ledger.LedgerRefused, match=r"an answer with no send \(seq 4\)"):
        writer.answer(4, 200, _assign_body("u-1"))
    _refused_then_explained(stream, "an answer with no send")


def test_the_writer_refuses_a_header():
    writer, stream = _writer()
    seq = writer.send(
        ledger.TRACK, {"event_type": "X-API-Key: from-a-header", "user_id": "u-1"}
    )
    with pytest.raises(ledger.LedgerRefused, match="credential or a header"):
        writer.answer(seq, 200, {})
    assert "from-a-header" not in stream.getvalue()
    _refused_then_explained(stream, "credential or a header in 'event_type'")


def test_the_writer_refuses_a_registered_secret():
    writer, stream = _writer(secrets=["registered-key-value"])
    seq = writer.send(
        ledger.TRACK, {"event_type": "purchase", "user_id": "u-registered-key-value"}
    )
    with pytest.raises(ledger.LedgerRefused, match="credential or a header in 'user'"):
        writer.answer(seq, 200, {})
    assert "registered-key-value" not in stream.getvalue()


_SECRET = "sk-live-abcdef123456"


@pytest.mark.parametrize(
    "where",
    [
        {"declared": {f"token {_SECRET}": 1}},
        {"declared": {f"note {_SECRET}": 1}},
        {"planted": {f"arm {_SECRET}": {"conversion": 0.1}}},
        {"declared": {"a": {f"{_SECRET}": 1}}},
    ],
    ids=["secret-named-key", "key-with-secret", "planted-key", "nested-key"],
)
def test_a_refusal_names_a_field_by_its_path_never_by_the_text_of_its_key(where):
    stream = io.StringIO()
    with pytest.raises(ledger.LedgerRefused) as refused:
        ledger.Writer(
            stream,
            source="test",
            seed=1,
            experiments=[
                {"exp": EXP, "variants": [{"name": "control", "allocation": 100}]}
            ],
            secrets=[_SECRET],
            **where,
        )
    assert _SECRET not in str(refused.value)
    assert _SECRET not in stream.getvalue()
    assert "<key 0>" in str(refused.value)  # the path, by position


def test_a_refusal_message_carrying_a_registered_secret_is_withheld():
    writer, stream = _writer(secrets=[_SECRET])
    with pytest.raises(ledger.LedgerRefused, match="message was withheld"):
        writer.send(f"/not-a-route-{_SECRET}", {})
    assert _SECRET not in stream.getvalue()
    _refused_then_explained(stream, "message was withheld")


def test_the_writer_refuses_a_send_with_no_answer_at_the_end():
    writer, stream = _writer()
    writer.send(ledger.TRACK, {"event_type": "purchase", "user_id": "u-1"})
    with pytest.raises(ledger.LedgerRefused, match="send 1 .* has no answer"):
        writer.end()
    _refused_then_explained(stream, "has no answer")


def test_the_writer_refuses_a_409_to_assign_batch():
    writer, stream = _writer()
    seq = writer.send(
        ledger.ASSIGN_BATCH, {"experiment_key": EXP, "users": [{"user_id": "u-1"}]}
    )
    with pytest.raises(ledger.LedgerRefused, match="409"):
        writer.answer(seq, 409, {"detail": "changed"})


def test_the_writer_refuses_an_answer_that_names_another_user():
    writer, _ = _writer()
    seq = writer.send(ledger.ASSIGN, {"experiment_key": EXP, "user_id": "u-1"})
    with pytest.raises(ledger.LedgerRefused, match="another user"):
        writer.answer(seq, 200, _assign_body("u-2"))


def test_a_429_is_recorded_counted_and_stores_nothing():
    writer, stream = _writer()
    action = writer.send_action("start", EXP)
    writer.answer(action, 200)
    seq = writer.send(ledger.ASSIGN, {"experiment_key": EXP, "user_id": "u-1"})
    writer.answer(seq, 200, _assign_body("u-1"))
    seq = writer.send(
        ledger.TRACK,
        {"event_type": "purchase", "user_id": "u-1", "experiment_key": EXP},
    )
    writer.answer(seq, 429, {"detail": "Too Many Requests"}, attempts=1)
    counts = writer.end()
    assert counts["429"] == 1 and counts["event"] == 1
    expected = contract.valid_events(ledger.read(io.StringIO(stream.getvalue())))
    hero = expected.experiments[EXP]
    assert expected.rate_limited == 1 and expected.rejected_events == 1
    assert hero.total_events == 1  # the exposure only
    assert hero.counts(_variant("u-1"), "purchase") == [0]


# ---------------------------------------------------------------------------
# L2: the reader
# ---------------------------------------------------------------------------
def _end_counts(stream: list[dict]) -> list[dict]:
    """*stream* with its end's counts recomputed, so one refusal is tested."""
    fixed = copy.deepcopy(stream)
    counts = dict.fromkeys(ledger.COUNTED, 0) | {"429": 0}
    for record in fixed:
        if record["kind"] in ledger.COUNTED:
            counts[record["kind"]] += 1
            counts["429"] += record["status"] == 429
    fixed[-1]["counts"] = counts
    return fixed


_EVENT = 4  # the index of MINIMAL's first event


@pytest.mark.parametrize(
    "stream, message",
    [
        (_with(MINIMAL, _EVENT, status=503), "the server answered 503"),
        (_with(MINIMAL, _EVENT, status=0, error="timeout"), r"no answer \(timeout\)"),
        (
            _with(MINIMAL, 0, declared={"request_headers": {"accept": "json"}}),
            "field named like a credential or a header: declared.<key 0>",
        ),
        (
            _with(MINIMAL, 3, reason="Authorization: Bearer abcdefghijklmnopqrstuvwx"),
            "credential or a header in 'reason'",
        ),
    ],
    ids=["5xx", "transport-error", "header-name", "header-value"],
)
def test_the_reader_refuses_a_5xx_a_transport_error_and_a_header(stream, message):
    with pytest.raises(ledger.LedgerRefused, match=message):
        _read(_end_counts(stream))


def test_the_reader_refuses_a_registered_secret():
    stream = _with(MINIMAL, _EVENT, event_type="purchase-registered-key-value")
    with pytest.raises(ledger.LedgerRefused, match="credential or a header"):
        _read(stream, secrets=["registered-key-value"])
    assert _read(stream)  # unregistered, the same stream is read


def test_the_reader_refuses_an_event_of_a_batch_with_no_answer():
    stream = _with(_BATCHED, 5, seq=8)
    with pytest.raises(
        ledger.LedgerRefused, match="an event of /batch seq 9 with no batch answer"
    ):
        _read(stream)


def _lines_with(index: int, line: str) -> list[str]:
    lines = _lines(MINIMAL)
    lines[index] = line
    return lines


#: json.dumps writes NaN where strict JSON has no such value.
_NAN_EVENT = json.dumps({"v": 1, **MINIMAL[_EVENT], "value": float("nan")}) + "\n"


@pytest.mark.parametrize(
    "lines, message",
    [
        (_lines(_with(MINIMAL, 1, v=2)), "version 2, not 1"),
        (_lines(_with(MINIMAL, 1, kind="restart")), "unknown kind 'restart'"),
        (_lines(_with(MINIMAL, 1, note="x")), "unknown field 'note'"),
        (_lines(_with(MINIMAL, 1, status="200")), "'status' is not int"),
        (_lines(_with(MINIMAL, _EVENT, value=True)), "'value' is not number"),
        (
            _lines(
                [
                    MINIMAL[0],
                    {k: v for k, v in MINIMAL[1].items() if k != "exp"},
                    *MINIMAL[2:],
                ]
            ),
            "lacks required field 'exp'",
        ),
        (_lines(_with(MINIMAL, 1, action="x" * 5000)), "over 4096"),
        (_lines_with(1, "not json\n"), "not one JSON object"),
        (_lines_with(1, '{"v":1,"v":1}\n'), "appears twice"),
        (_lines_with(1, "[1]\n"), "not a JSON object"),
        (_lines_with(_EVENT, _NAN_EVENT), "NaN is not JSON"),
        (_lines([MINIMAL[0], MINIMAL[0], *MINIMAL[1:]]), "a second start"),
        (_lines(MINIMAL[1:]), "the first record must be start"),
        (_lines([*MINIMAL, MINIMAL[1]]), "comes after end"),
        (_lines(MINIMAL[:-1]), "no end: it was cut short"),
        ([], "empty"),
        (_lines(_with(MINIMAL, -1, counts={"assign": 9})), "disagree with the records"),
        (_lines(_with(MINIMAL, 2, variant=None)), "assigned true with no variant"),
        (
            _lines(_with(_BATCHED, 5, size=2, success_count=2)),
            "answered for 2 events and 1 were recorded",
        ),
        (_lines(_with(MINIMAL, 5, success_count=0)), "do not add up"),
        (_lines(_with(MINIMAL, 5, errors=[3])), "positions in the batch"),
        (
            _lines(_with(MINIMAL, -1, errors=["send 4 answered 502"])),
            "the run was refused: send 4 answered 502",
        ),
        (_lines(_with(MINIMAL, 1, status=99)), "not an HTTP status"),
        (_lines(_with(MINIMAL, 1, attempts=0)), "attempts must be at least 1"),
    ],
    ids=[
        "version",
        "kind",
        "unknown-field",
        "type",
        "bool-value",
        "missing",
        "4kib",
        "json",
        "duplicate-key",
        "array",
        "nan",
        "second-start",
        "no-start",
        "after-end",
        "no-end",
        "empty",
        "counts",
        "assigned-no-variant",
        "batch-size",
        "batch-sum",
        "batch-errors",
        "refused-run",
        "status",
        "attempts",
    ],
)
def test_the_reader_refuses(lines, message):
    with pytest.raises(ledger.LedgerRefused, match=message):
        ledger.read(lines)


def test_the_ledger_records_no_timestamp_and_no_metadata():
    """R7: nothing that reads a ledger can read a client's clock."""
    fields = {
        name
        for table in (ledger.REQUIRED, ledger.OPTIONAL, ledger.ADDITIVE)
        for kind_fields in table.values()
        for name in kind_fields
    }
    assert not fields & {"timestamp", "metadata", "properties"}
    writer, stream = _writer()
    seq = writer.send(
        ledger.TRACK,
        {
            "event_type": "purchase",
            "user_id": "u-1",
            "timestamp": "2099-01-01T00:00:00Z",
            "metadata": {"note": "x"},
        },
    )
    writer.answer(seq, 404, {"detail": "not found"})
    writer.end()
    assert "2099" not in stream.getvalue() and "note" not in stream.getvalue()
