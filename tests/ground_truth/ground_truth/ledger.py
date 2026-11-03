"""Ledger v1: one JSON object per line for every answer the server gave.

This module is the one implementation of the ledger format (#1544): the
replay's driver writes it, and anything else that records traffic for a
reconciler (the demo simulators, a seeder) writes and reads it through this
module. Changing the format means changing this module.

Format
------
UTF-8, one JSON object per line, at most 4 KiB per line, append-only. Every
record carries ``"v": 1`` and a ``kind``:

``start``   first, once: ``source``, ``seed``, ``experiments`` (each ``exp``,
            the experiment's public key, and ``variants``, its variants'
            ``name`` and ``allocation`` in creation order, as the test sent
            them), ``flags``, ``planted`` (a list of the planted rates, each with its arm's ``name``) and ``declared``
            (what the run declares about the server and the stack).
``assign``  ``user``, ``exp``, ``status``, ``assigned``, ``variant``,
            ``reason``, ``attempts``: written from the answer's body only.
``flag``    ``user``, ``flag``, ``status``, ``enabled``, ``reason``,
            ``attempts``.
``event``   ``user``, ``exp``, ``flag``, ``event_type``, ``event_name``,
            ``value``, ``status``, ``attempts``. An event sent in a ``/batch``
            carries the batch's status.
``batch``   ``size``, ``status``, ``success_count``, ``failure_count``,
            ``errors`` (the positions the answer reported, or null),
            ``attempts``.
``alias``   ``user``, ``alias``, ``status``, ``attempts``.
``action``  ``action`` (``create``, ``start``, ``pause``, ...), ``exp``,
            ``status``, ``attempts``.
``end``     last, once: ``counts`` (records of each kind, and ``"429"``, the
            records answered 429) and ``errors`` (empty unless the run was
            refused).

Those are the format's **required fields** (``REQUIRED``). An answer that
never came has ``status`` 0 and an ``error`` (``timeout``, ``reset`` or
``refused``). An experiment entry of ``start`` may also give ``status`` (the
experiment's status when the run begins), ``metrics``, ``correction_method``
and ``confidence_level``.

The replay adds four **optional fields** (``ADDITIVE``), never required, so a
stream written without them is still read: ``route`` (``/assign``,
``/assign/batch``, ``/track``, ``/batch``), ``seq`` (the request's number:
the records of one ``/batch`` or ``/assign/batch`` share it),
``batch_index`` (the position in that request) and ``event_id`` (as sent;
null when none was). Where a rule of the event contract needs one of them,
``contract.valid_events`` refuses by name; this module only reads.

Refused, which fails the run (``LedgerRefused``):

* a version other than 1, an unknown kind, a missing or ill-typed required
  field, an unknown field, a line that is not one UTF-8 JSON object, or one
  over 4 KiB;
* ``assigned: true`` with no variant; a second ``start``; anything after
  ``end``; no ``end`` (a truncated ledger); ``end.counts`` that disagree with
  the records; an ``end`` that lists errors;
* any 5xx answer, and any answer that never came: a transport error or a
  timeout leaves it unknown whether the server stored anything;
* a 409 to ``/assign/batch``, which may follow a partial assignment;
* an answer with no send, a send with no answer, and a batch whose events do
  not match its size;
* any credential or header in any record: a field named like one, a value
  shaped like one, or a value the run registered as secret (``secrets``).

A 429 is recorded, counted in ``end.counts["429"]``, and stores nothing.

S1's injected-fault mode (indeterminate answers excluded and counted) is not
implemented here: this module refuses them, as listed above. D210 v3 adds it
here.

Ids are meant to be synthetic; nothing checks it. The ledger of a failed run is the failure dump: it is
written for the person reproducing the failure and is never uploaded.
Request bodies' ``metadata`` and ``timestamp`` are not recorded: no number of
``GET /api/v1/results/{experiment_id}`` reads them.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import IO, Any, NoReturn

VERSION = 1
MAX_LINE_BYTES = 4096
KINDS = ("start", "assign", "flag", "event", "batch", "alias", "action", "end")
#: The kinds ``end.counts`` counts, beside "429".
COUNTED = ("assign", "flag", "event", "batch", "alias", "action")
TRANSPORT_ERRORS = ("timeout", "reset", "refused")

ASSIGN = "/api/v1/tracking/assign"
ASSIGN_BATCH = "/api/v1/tracking/assign/batch"
TRACK = "/api/v1/tracking/track"
BATCH = "/api/v1/tracking/batch"
#: The routes a send may name, and the ``route`` its records carry.
ROUTES = {
    ASSIGN: "/assign",
    ASSIGN_BATCH: "/assign/batch",
    TRACK: "/track",
    BATCH: "/batch",
}

_STATUS = ("int",)
_ANSWER = {"status": _STATUS, "attempts": ("int",)}

#: The format's required fields per kind, with the JSON types each may take.
REQUIRED: dict[str, dict[str, tuple[str, ...]]] = {
    "start": {
        "source": ("str",),
        "seed": ("int", "str"),
        "experiments": ("list",),
        "flags": ("list",),
        "planted": ("list",),
        "declared": ("object",),
    },
    "assign": {
        "user": ("str",),
        "exp": ("str",),
        "assigned": ("bool", "null"),
        "variant": ("str", "null"),
        "reason": ("str", "null"),
        **_ANSWER,
    },
    "flag": {
        "user": ("str",),
        "flag": ("str",),
        "enabled": ("bool", "null"),
        "reason": ("str", "null"),
        **_ANSWER,
    },
    "event": {
        "user": ("str",),
        "exp": ("str", "null"),
        "flag": ("str", "null"),
        "event_type": ("str",),
        "event_name": ("str", "null"),
        "value": ("number", "null"),
        **_ANSWER,
    },
    "batch": {
        "size": ("int",),
        "success_count": ("int", "null"),
        "failure_count": ("int", "null"),
        "errors": ("list", "null"),
        **_ANSWER,
    },
    "alias": {"user": ("str",), "alias": ("str",), **_ANSWER},
    "action": {"action": ("str",), "exp": ("str", "null"), **_ANSWER},
    "end": {"counts": ("object",), "errors": ("list",)},
}

#: The format's optional field: why an answer never came (with status 0).
OPTIONAL: dict[str, dict[str, tuple[str, ...]]] = {
    kind: {"error": ("str",)} for kind in COUNTED
}

#: The replay's optional fields (#1544). Never required: see the docstring.
ADDITIVE: dict[str, dict[str, tuple[str, ...]]] = {
    "assign": {"route": ("str",), "seq": ("int",), "batch_index": ("int",)},
    "event": {
        "route": ("str",),
        "seq": ("int",),
        "batch_index": ("int",),
        "event_id": ("str", "null"),
    },
    "batch": {"route": ("str",), "seq": ("int",)},
    "action": {"seq": ("int",)},
}

_EXPERIMENT_REQUIRED = {"exp": ("str",), "variants": ("list",)}
_EXPERIMENT_OPTIONAL = {
    "status": ("str",),
    "metrics": ("list",),
    "correction_method": ("str",),
    "confidence_level": ("number",),
}
_VARIANT_FIELDS = {"name": ("str",), "allocation": ("number",)}
_METRIC_FIELDS = {"name": ("str",), "event_name": ("str",), "type": ("str",)}

#: A field name no record may carry: a credential or a header.
_SECRET_NAME = re.compile(
    r"api[_-]?key|apikey|token|passw|secret|authori[sz]ation|header|cookie"
    r"|bearer|credential|session",
    re.IGNORECASE,
)
#: A value shaped like a credential or a header line. Every quantifier is
#: bounded, and a line is at most 4 KiB, so this cannot backtrack for long.
_SECRET_VALUE = re.compile(
    r"\b(?:bearer|basic)\s{1,8}[A-Za-z0-9._~+/=-]{16,512}"
    r"|\beyJ[A-Za-z0-9_-]{4,512}\.eyJ[A-Za-z0-9_-]{4,512}"
    r"|\b(?:x-api-key|authorization|proxy-authorization|cookie|set-cookie)\s{0,8}:"
    r"|-----BEGIN [A-Z ]{1,40}-----",
    re.IGNORECASE,
)
_AWS_KEY_ID = re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b")
#: A registered secret shorter than this would match ordinary text.
MIN_SECRET_LENGTH = 8


class LedgerRefused(Exception):
    """The ledger, or the run it records, is refused; the message says why."""


def _matches(value: Any, types: tuple[str, ...]) -> bool:
    for name in types:
        if name == "null" and value is None:
            return True
        if name == "bool" and isinstance(value, bool):
            return True
        if isinstance(value, bool):
            continue
        if name == "int" and isinstance(value, int):
            return True
        if name == "number" and isinstance(value, (int, float)):
            return True
        if name == "str" and isinstance(value, str):
            return True
        if name == "list" and isinstance(value, list):
            return True
        if name == "object" and isinstance(value, dict):
            return True
    return False


def _fields(
    where: str,
    record: Mapping[str, Any],
    required: Mapping[str, tuple[str, ...]],
    optional: Mapping[str, tuple[str, ...]],
) -> None:
    for name, types in required.items():
        if name not in record:
            raise LedgerRefused(f"{where} lacks required field {name!r}")
        if not _matches(record[name], types):
            raise LedgerRefused(f"{where}: {name!r} is not {' or '.join(types)}")
    for name, value in record.items():
        if name in required:
            continue
        if name not in optional:
            raise LedgerRefused(f"{where} has unknown field {name!r}")
        if not _matches(value, optional[name]):
            raise LedgerRefused(
                f"{where}: {name!r} is not {' or '.join(optional[name])}"
            )


#: A key short and plain enough to name in a path.
_PLAIN_KEY = re.compile(r"[A-Za-z0-9_-]{1,40}")


def _label(name: str, index: int, secrets: Sequence[str]) -> str:
    """How a key appears in a refusal's path: itself when it is plain text that
    is not a credential's name, shape or registered value, else its position.
    A refusal never repeats the text of a key the caller supplied."""
    if (
        _PLAIN_KEY.fullmatch(name)
        and not _SECRET_NAME.search(name)
        and not _AWS_KEY_ID.search(name)
        and not any(secret in name for secret in secrets)
    ):
        return name
    return f"<key {index}>"


def _scan(where: str, value: Any, secrets: Sequence[str], path: str = "") -> None:
    """Refuse a credential or a header anywhere in *value*, naming only where."""
    if isinstance(value, dict):
        for index, (name, inner) in enumerate(value.items()):
            label = _label(name, index, secrets)
            if _SECRET_NAME.search(name):
                raise LedgerRefused(
                    f"{where} carries a field named like a credential or a header:"
                    f" {path}{label}"
                )
            _scan(where, name, secrets, f"{path}{label}.")
            _scan(where, inner, secrets, f"{path}{label}.")
    elif isinstance(value, list):
        for index, inner in enumerate(value):
            _scan(where, inner, secrets, f"{path}{index}.")
    elif isinstance(value, str):
        shaped = _SECRET_VALUE.search(value) or _AWS_KEY_ID.search(value)
        if shaped or any(secret in value for secret in secrets):
            raise LedgerRefused(
                f"{where} carries a credential or a header in {path.rstrip('.')!r}"
            )


def _reject_constant(name: str) -> NoReturn:
    raise ValueError(f"{name} is not JSON")


def _no_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    record: dict[str, Any] = {}
    for name, value in pairs:
        if name in record:
            raise ValueError(f"field {name!r} appears twice")
        record[name] = value
    return record


def _registered(secrets: Iterable[str]) -> tuple[str, ...]:
    found = tuple(secrets)
    for secret in found:
        if not isinstance(secret, str) or len(secret) < MIN_SECRET_LENGTH:
            raise LedgerRefused(
                f"a registered secret must be text of at least {MIN_SECRET_LENGTH}"
                " characters"
            )
    return found


class _Checker:
    """Every rule a record must meet, in order. The reader and the writer share
    it, so the writer cannot write what the reader would refuse."""

    def __init__(self, secrets: Sequence[str]) -> None:
        self.secrets = tuple(secrets)
        self.records: list[dict[str, Any]] = []
        self.ended = False
        self.counts = dict.fromkeys(COUNTED, 0)
        self.counts["429"] = 0
        #: seq -> (size, members seen) for each ``batch`` answer with a seq.
        self.batches: dict[int, list[int]] = {}
        self.members: set[tuple[str, int, int]] = set()

    def parse(self, number: int, line: str) -> dict[str, Any]:
        where = f"line {number}"
        text = line[:-1] if line.endswith("\n") else line
        try:
            size = len(text.encode("utf-8"))
        except UnicodeEncodeError:
            raise LedgerRefused(f"{where} is not UTF-8") from None
        if size > MAX_LINE_BYTES:
            raise LedgerRefused(f"{where} is {size} bytes, over {MAX_LINE_BYTES}")
        try:
            record = json.loads(
                text,
                object_pairs_hook=_no_duplicate_keys,
                parse_constant=_reject_constant,
            )
        except ValueError as exc:
            raise LedgerRefused(f"{where} is not one JSON object: {exc}") from None
        if not isinstance(record, dict):
            raise LedgerRefused(f"{where} is not a JSON object")
        try:
            json.dumps(record, ensure_ascii=False).encode("utf-8")
        except UnicodeEncodeError:
            raise LedgerRefused(f"{where} is not UTF-8") from None
        return record

    def check(self, number: int, record: Mapping[str, Any]) -> None:
        where = f"line {number}"
        if self.ended:
            raise LedgerRefused(f"{where} comes after end")
        if record.get("v") != VERSION:
            raise LedgerRefused(f"{where} has version {record.get('v')!r}, not 1")
        kind = record.get("kind")
        if kind not in KINDS:
            raise LedgerRefused(f"{where} has unknown kind {kind!r}")
        where = f"line {number} ({kind})"
        body = {k: v for k, v in record.items() if k not in ("v", "kind")}
        optional = {**OPTIONAL.get(kind, {}), **ADDITIVE.get(kind, {})}
        _fields(where, body, REQUIRED[kind], optional)
        _scan(where, body, self.secrets)
        if kind == "start" and self.records:
            raise LedgerRefused(f"{where}: a second start")
        if kind != "start" and not self.records:
            raise LedgerRefused(f"{where}: the first record must be start")
        getattr(self, f"_check_{kind}", lambda *_: None)(where, body)
        if kind in COUNTED:
            self.counts[kind] += 1
            if body["status"] == 429:
                self.counts["429"] += 1
        self.records.append(dict(record))

    def _check_answer(self, where: str, body: Mapping[str, Any]) -> None:
        status = body["status"]
        if status == 0:
            error = body.get("error")
            if error not in TRANSPORT_ERRORS:
                raise LedgerRefused(
                    f"{where}: status 0 needs an error, one of {TRANSPORT_ERRORS}"
                )
            raise LedgerRefused(
                f"{where}: no answer ({error}), so whether the server stored"
                " anything is unknown"
            )
        if "error" in body:
            raise LedgerRefused(f"{where}: an error with status {status}")
        if not 100 <= status <= 599:
            raise LedgerRefused(f"{where}: status {status} is not an HTTP status")
        if status >= 500:
            raise LedgerRefused(f"{where}: the server answered {status}")
        if body["attempts"] < 1:
            raise LedgerRefused(f"{where}: attempts must be at least 1")
        if "batch_index" in body and body["batch_index"] < 0:
            raise LedgerRefused(f"{where}: batch_index must not be negative")

    def _check_start(self, where: str, body: Mapping[str, Any]) -> None:
        keys = set()
        for index, entry in enumerate(body["experiments"]):
            at = f"{where} experiments[{index}]"
            if not isinstance(entry, dict):
                raise LedgerRefused(f"{at} is not an object")
            _fields(at, entry, _EXPERIMENT_REQUIRED, _EXPERIMENT_OPTIONAL)
            if entry["exp"] in keys:
                raise LedgerRefused(f"{at}: experiment {entry['exp']!r} appears twice")
            keys.add(entry["exp"])
            if not entry["variants"]:
                raise LedgerRefused(f"{at} has no variants")
            names = set()
            for position, variant in enumerate(entry["variants"]):
                vat = f"{at} variants[{position}]"
                if not isinstance(variant, dict):
                    raise LedgerRefused(f"{vat} is not an object")
                _fields(vat, variant, _VARIANT_FIELDS, {})
                if variant["name"] in names:
                    raise LedgerRefused(f"{vat}: variant {variant['name']!r} repeats")
                names.add(variant["name"])
            for position, metric in enumerate(entry.get("metrics", [])):
                mat = f"{at} metrics[{position}]"
                if not isinstance(metric, dict):
                    raise LedgerRefused(f"{mat} is not an object")
                _fields(mat, metric, _METRIC_FIELDS, {})

    def _check_assign(self, where: str, body: Mapping[str, Any]) -> None:
        self._check_answer(where, body)
        if body["assigned"] is True and body["variant"] is None:
            raise LedgerRefused(f"{where}: assigned true with no variant")

    def _check_flag(self, where: str, body: Mapping[str, Any]) -> None:
        self._check_answer(where, body)

    def _check_alias(self, where: str, body: Mapping[str, Any]) -> None:
        self._check_answer(where, body)

    def _check_action(self, where: str, body: Mapping[str, Any]) -> None:
        self._check_answer(where, body)

    def _check_batch(self, where: str, body: Mapping[str, Any]) -> None:
        self._check_answer(where, body)
        size, errors = body["size"], body["errors"]
        if size < 1:
            raise LedgerRefused(f"{where}: a batch holds at least one event")
        if errors is not None:
            if not all(_matches(i, ("int",)) and 0 <= i < size for i in errors):
                raise LedgerRefused(f"{where}: errors must be positions in the batch")
            if len(set(errors)) != len(errors):
                raise LedgerRefused(f"{where}: a position repeats in errors")
        if 200 <= body["status"] < 300:
            counts = (body["success_count"], body["failure_count"])
            if None in counts or sum(counts) != size:
                raise LedgerRefused(
                    f"{where}: success_count and failure_count do not add up to"
                    f" the {size} events sent"
                )
        if "seq" in body:
            if body["seq"] in self.batches:
                raise LedgerRefused(
                    f"{where}: a second batch answer for seq {body['seq']}"
                )
            self.batches[body["seq"]] = [size, 0]

    def _check_event(self, where: str, body: Mapping[str, Any]) -> None:
        self._check_answer(where, body)
        if body.get("route") == "/batch" and "seq" in body:
            batch = self.batches.get(body["seq"])
            if batch is None:
                raise LedgerRefused(
                    f"{where}: an event of /batch seq {body['seq']} with no batch"
                    " answer before it"
                )
            batch[1] += 1
            if batch[1] > batch[0]:
                raise LedgerRefused(
                    f"{where}: more events than the {batch[0]} the batch answered"
                )
        if "seq" in body and "batch_index" in body:
            member = (body.get("route", ""), body["seq"], body["batch_index"])
            if member in self.members:
                raise LedgerRefused(
                    f"{where}: seq {member[1]} position {member[2]} appears twice"
                )
            self.members.add(member)

    def _check_end(self, where: str, body: Mapping[str, Any]) -> None:
        if body["errors"]:
            raise LedgerRefused(
                f"{where}: the run was refused: {'; '.join(map(str, body['errors']))}"
            )
        for seq, (size, seen) in self.batches.items():
            if seen != size:
                raise LedgerRefused(
                    f"{where}: the batch of seq {seq} answered for {size} events and"
                    f" {seen} were recorded"
                )
        if body["counts"] != self.counts:
            raise LedgerRefused(
                f"{where}: counts {body['counts']} disagree with the records"
                f" {self.counts}"
            )
        self.ended = True


def dumps(record: Mapping[str, Any]) -> str:
    """One record as its line, ``v`` and ``kind`` first."""
    ordered = {"v": VERSION, **record}
    return json.dumps(
        ordered, ensure_ascii=False, separators=(",", ":"), allow_nan=False
    )


def read(lines: Iterable[str], *, secrets: Iterable[str] = ()) -> list[dict[str, Any]]:
    """Every record of a ledger, in order, once the whole ledger is accepted."""
    checker = _Checker(_registered(secrets))
    number = 0
    try:
        for number, line in enumerate(lines, 1):
            checker.check(number, checker.parse(number, line))
    except UnicodeDecodeError:
        raise LedgerRefused(f"line {number + 1} is not UTF-8") from None
    if not checker.records:
        raise LedgerRefused("the ledger is empty")
    if not checker.ended:
        raise LedgerRefused("the ledger has no end: it was cut short")
    return checker.records


def read_path(path: str | Path, *, secrets: Iterable[str] = ()) -> list[dict[str, Any]]:
    with open(path, encoding="utf-8", newline="\n") as stream:
        return read(stream, secrets=secrets)


class Writer:
    """Writes a ledger as the requests are made.

    ``send`` before a request goes out, ``answer`` when its answer arrives (or
    ``transport_error`` when none does), ``end`` when the run is over. A
    refusal writes an ``end`` whose ``errors`` say why, so the file explains
    itself, and raises ``LedgerRefused``; every call after that is refused too.
    """

    def __init__(
        self,
        stream: IO[str],
        *,
        source: str,
        seed: int | str,
        experiments: Sequence[Mapping[str, Any]],
        flags: Sequence[Any] = (),
        planted: Sequence[Mapping[str, Any]] | None = None,
        declared: Mapping[str, Any] | None = None,
        secrets: Iterable[str] = (),
    ) -> None:
        self._stream = stream
        self._checker = _Checker(_registered(secrets))
        self._lines = 0
        self._next_seq = 1
        self._pending: dict[int, tuple[str, Mapping[str, Any]]] = {}
        self._closed = False
        self._write(
            {
                "kind": "start",
                "source": source,
                "seed": seed,
                "experiments": [dict(e) for e in experiments],
                "flags": list(flags),
                "planted": list(planted or ()),
                "declared": dict(declared or {}),
            }
        )

    # -- the refusal path --------------------------------------------------
    def _refuse(self, message: str) -> NoReturn:
        try:
            _scan("a refusal", message, self._checker.secrets)
        except LedgerRefused:
            message = "a refusal's message was withheld: it carried a credential"
        if not self._closed:
            self._closed = True
            self._stream.write(
                dumps(
                    {"kind": "end", "counts": self._checker.counts, "errors": [message]}
                )
                + "\n"
            )
            self._stream.flush()
        raise LedgerRefused(message)

    def _write(self, record: Mapping[str, Any]) -> None:
        if self._closed:
            raise LedgerRefused("a record after end")
        number = self._lines + 1
        problem = None
        try:
            line = dumps(record)
            self._checker.check(number, self._checker.parse(number, line))
        except (TypeError, ValueError) as exc:
            problem = f"a {record.get('kind')} record is not JSON: {exc}"
        except LedgerRefused as exc:
            problem = str(exc)
        if problem is not None:
            self._refuse(problem)
        self._stream.write(line + "\n")
        self._lines = number

    # -- sends --------------------------------------------------------------
    def _new_send(self, route: str, body: Mapping[str, Any]) -> int:
        if self._closed:
            raise LedgerRefused("a send after end")
        seq = self._next_seq
        self._next_seq += 1
        self._pending[seq] = (route, body)
        return seq

    def send(self, route: str, body: Mapping[str, Any]) -> int:
        """Note a tracking request before it goes out; its number, for ``answer``."""
        if route not in ROUTES:
            self._refuse(f"a send to {route}, which the ledger does not record")
        need = {
            ASSIGN: ("experiment_key", "user_id"),
            ASSIGN_BATCH: ("experiment_key", "users"),
            TRACK: ("event_type", "user_id"),
            BATCH: ("events",),
        }[route]
        if not isinstance(body, Mapping) or any(k not in body for k in need):
            self._refuse(f"a send to {route} needs {', '.join(need)}")
        return self._new_send(route, body)

    def send_action(self, action: str, exp: str | None) -> int:
        """Note a lifecycle request (``create``, ``start``, ``pause``, ...)."""
        return self._new_send("action", {"action": action, "exp": exp})

    # -- answers ------------------------------------------------------------
    def _take(self, seq: int) -> tuple[str, Mapping[str, Any]]:
        if self._closed:
            raise LedgerRefused("an answer after end")
        if seq not in self._pending:
            self._refuse(f"an answer with no send (seq {seq})")
        return self._pending.pop(seq)

    def transport_error(self, seq: int, error: str) -> NoReturn:
        """The request got no answer: the run is refused."""
        route, _ = self._take(seq)
        self._refuse(
            f"send {seq} ({route}) got no answer ({error}), so whether the server"
            " stored anything is unknown"
        )

    def answer(
        self, seq: int, status: int, body: Any = None, *, attempts: int = 1
    ) -> None:
        """Record the server's answer to send *seq*: its status and its body."""
        route, sent = self._take(seq)
        if not _matches(status, ("int",)) or not 100 <= status <= 599:
            self._refuse(f"send {seq} ({route}): status {status!r} is not HTTP")
        if status >= 500:
            self._refuse(f"send {seq} ({route}): the server answered {status}")
        if route == ASSIGN_BATCH and status == 409:
            self._refuse(
                f"send {seq} ({route}): 409, after which some users may be"
                " assigned and others not"
            )
        ok = 200 <= status < 300
        common = {"status": status, "attempts": attempts}
        if route == "action":
            self._write({"kind": "action", **sent, **common, "seq": seq})
        elif route == ASSIGN:
            self._answer_assign(seq, sent, body, ok, common)
        elif route == ASSIGN_BATCH:
            self._answer_assign_batch(seq, sent, body, ok, common)
        elif route == TRACK:
            self._write(self._event(sent, common, {"route": "/track", "seq": seq}))
        else:
            self._answer_batch(seq, sent, body, ok, common)

    def _assign_record(
        self,
        user: Any,
        exp: Any,
        answer: Mapping[str, Any] | None,
        variant: Any,
        common: Mapping[str, Any],
        extra: Mapping[str, Any],
    ) -> dict[str, Any]:
        return {
            "kind": "assign",
            "user": user,
            "exp": exp,
            "assigned": None if answer is None else answer.get("assigned"),
            "variant": variant,
            "reason": None if answer is None else answer.get("reason"),
            **common,
            **extra,
        }

    def _answer_assign(self, seq, sent, body, ok, common) -> None:
        user, exp = sent["user_id"], sent["experiment_key"]
        extra = {"route": "/assign", "seq": seq}
        if not ok:
            self._write(self._assign_record(user, exp, None, None, common, extra))
            return
        if not isinstance(body, Mapping) or not isinstance(body.get("assigned"), bool):
            self._refuse(f"send {seq} (/assign): the answer is not an assignment")
        if body.get("user_id", user) != user:
            self._refuse(f"send {seq} (/assign): the answer names another user")
        self._write(
            self._assign_record(
                user, exp, body, body.get("variant_name"), common, extra
            )
        )

    def _answer_assign_batch(self, seq, sent, body, ok, common) -> None:
        users = [
            u.get("user_id") if isinstance(u, Mapping) else None for u in sent["users"]
        ]
        exp = sent["experiment_key"]
        if not ok:
            for index, user in enumerate(users):
                extra = {"route": "/assign/batch", "seq": seq, "batch_index": index}
                self._write(self._assign_record(user, exp, None, None, common, extra))
            return
        assignments = body.get("assignments") if isinstance(body, Mapping) else None
        variants = body.get("variants") if isinstance(body, Mapping) else None
        if not isinstance(assignments, list) or not isinstance(variants, Mapping):
            self._refuse(f"send {seq} (/assign/batch): the answer is not an assignment")
        if len(assignments) != len(users):
            self._refuse(
                f"send {seq} (/assign/batch): {len(assignments)} assignments for"
                f" {len(users)} users"
            )
        for index, (user, answer) in enumerate(zip(users, assignments, strict=True)):
            if not isinstance(answer, Mapping) or answer.get("user_id") != user:
                self._refuse(
                    f"send {seq} (/assign/batch): position {index} answers another user"
                )
            variant_id = answer.get("variant_id")
            variant = variants.get(variant_id) if isinstance(variant_id, str) else None
            name = variant.get("name") if isinstance(variant, Mapping) else None
            extra = {"route": "/assign/batch", "seq": seq, "batch_index": index}
            self._write(self._assign_record(user, exp, answer, name, common, extra))

    @staticmethod
    def _event(
        sent: Mapping[str, Any], common: Mapping[str, Any], extra: Mapping[str, Any]
    ) -> dict[str, Any]:
        return {
            "kind": "event",
            "user": sent.get("user_id"),
            "exp": sent.get("experiment_key"),
            "flag": sent.get("feature_flag_key"),
            "event_type": sent.get("event_type"),
            "event_name": sent.get("event_name"),
            "value": sent.get("value"),
            **common,
            **extra,
            "event_id": sent.get("event_id"),
        }

    def _answer_batch(self, seq, sent, body, ok, common) -> None:
        events = list(sent["events"])
        record: dict[str, Any] = {
            "kind": "batch",
            "size": len(events),
            "success_count": None,
            "failure_count": None,
            "errors": None,
            **common,
            "route": "/batch",
            "seq": seq,
        }
        if ok:
            if not isinstance(body, Mapping):
                self._refuse(f"send {seq} (/batch): the answer is not a batch result")
            reported = body.get("errors") or []
            positions = [
                e.get("index") if isinstance(e, Mapping) else None for e in reported
            ]
            record.update(
                success_count=body.get("success_count"),
                failure_count=body.get("failure_count"),
                errors=sorted(positions, key=lambda p: (p is None, p)),
            )
        self._write(record)
        for index, event in enumerate(events):
            if not isinstance(event, Mapping):
                self._refuse(f"send {seq} (/batch): event {index} is not an object")
            extra = {"route": "/batch", "seq": seq, "batch_index": index}
            self._write(self._event(event, common, extra))

    def end(self) -> dict[str, int]:
        """Close the ledger; the counts its ``end`` record carries."""
        if self._closed:
            raise LedgerRefused("end after end")
        if self._pending:
            seq = min(self._pending)
            self._refuse(
                f"send {seq} ({self._pending[seq][0]}) has no answer, so whether the"
                " server stored anything is unknown"
            )
        counts = dict(self._checker.counts)
        self._write({"kind": "end", "counts": counts, "errors": []})
        self._closed = True
        self._stream.flush()
        return counts
