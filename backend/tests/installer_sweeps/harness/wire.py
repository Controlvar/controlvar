"""A response as each AWS protocol puts it on the wire, for the real CLI to parse.

scripts/check_installer_cli.py serves the fake's own response through a local stub
and compares what the real AWS CLI prints with what the fake prints (gate H6).
``serialize`` writes the success body, ``serialize_error`` an error, for the
protocol the CLI's request used:

* ``json`` and ``rest-json``: a JSON body, members under their serialized names,
  timestamps as epoch seconds;
* ``smithy-rpc-v2-cbor``: a CBOR body (newer CLIs reach CloudWatch this way),
  timestamps as CBOR tag 1;
* ``query``: ``<OpResponse><OpResult>...</OpResult>`` XML, lists of ``member``;
* ``ec2``: ``<OpResponse>`` XML with a requestId, lists of ``item``;
* ``rest-xml``: the output's members (or its payload member) as the root's
  children.

The shapes are the document ``shapes.extract`` writes. Only what the fake's
responses use is written: structures, lists, maps, strings, numbers, booleans and
timestamps.
"""

from __future__ import annotations

import datetime
import json
import struct
from xml.sax.saxutils import escape

REQUEST_ID = "6f5c4c3e-0000-4000-8000-0123456789ab"


def _moment(text: str) -> datetime.datetime:
    if not text.endswith("Z"):
        raise ValueError(f"timestamp {text!r} must end in Z")
    return datetime.datetime.fromisoformat(text[:-1]).replace(
        tzinfo=datetime.timezone.utc
    )


def _epoch(text: str) -> float:
    return _moment(text).timestamp()


# --- JSON -----------------------------------------------------------------------------


def _to_json(value, ref: dict, shapes: dict):
    shape = shapes[ref["shape"]]
    kind = shape["type"]
    if kind == "structure":
        return {
            members_ref.get("locationName", name): _to_json(
                value[name], members_ref, shapes
            )
            for name, members_ref in shape.get("members", {}).items()
            if name in value
        }
    if kind == "list":
        return [_to_json(v, shape["member"], shapes) for v in value]
    if kind == "map":
        return {k: _to_json(v, shape["value"], shapes) for k, v in value.items()}
    if kind == "timestamp":
        return _epoch(value)
    return value


# --- CBOR -------------------------------------------------------------------------------


def _cbor_head(major: int, length: int) -> bytes:
    if length < 24:
        return bytes([major << 5 | length])
    for info, size in ((24, 1), (25, 2), (26, 4), (27, 8)):
        if length < 1 << (8 * size):
            return bytes([major << 5 | info]) + length.to_bytes(size, "big")
    raise ValueError(f"{length} is too long for CBOR")


def _cbor(value) -> bytes:
    if value is None:
        return b"\xf6"
    if value is True:
        return b"\xf5"
    if value is False:
        return b"\xf4"
    if isinstance(value, int):
        return _cbor_head(0, value) if value >= 0 else _cbor_head(1, -1 - value)
    if isinstance(value, float):
        return b"\xfb" + struct.pack(">d", value)
    if isinstance(value, str):
        raw = value.encode()
        return _cbor_head(3, len(raw)) + raw
    if isinstance(value, list):
        return _cbor_head(4, len(value)) + b"".join(_cbor(v) for v in value)
    if isinstance(value, dict):
        return _cbor_head(5, len(value)) + b"".join(
            _cbor(k) + _cbor(v) for k, v in value.items()
        )
    if isinstance(value, _Epoch):
        return b"\xc1" + _cbor(value.seconds)  # tag 1: epoch-based date and time
    raise TypeError(f"no CBOR for {value!r}")


class _Epoch:
    def __init__(self, seconds: float) -> None:
        self.seconds = seconds


def _to_cbor_value(value, ref: dict, shapes: dict):
    shape = shapes[ref["shape"]]
    kind = shape["type"]
    if kind == "structure":
        return {
            name: _to_cbor_value(value[name], member, shapes)
            for name, member in shape.get("members", {}).items()
            if name in value
        }
    if kind == "list":
        return [_to_cbor_value(v, shape["member"], shapes) for v in value]
    if kind == "map":
        return {k: _to_cbor_value(v, shape["value"], shapes) for k, v in value.items()}
    if kind == "timestamp":
        return _Epoch(_epoch(value))
    return value


# --- XML -------------------------------------------------------------------------------


def _scalar_text(value, kind: str) -> str:
    if kind == "boolean":
        return "true" if value else "false"
    return escape(str(value))


def _xml_member_name(name: str, ref: dict, shapes: dict) -> str:
    shape = shapes[ref["shape"]]
    if shape["type"] == "list" and shape.get("flattened"):
        return shape["member"].get("locationName") or ref.get("locationName") or name
    return ref.get("locationName", name)


def _xml_children(value, ref: dict, shapes: dict, item_name: str) -> str:
    """The inside of the element that holds ``value``."""
    shape = shapes[ref["shape"]]
    kind = shape["type"]
    if kind == "structure":
        out = []
        for name, member in shape.get("members", {}).items():
            if name not in value:
                continue
            out.append(
                _xml_element(
                    _xml_member_name(name, member, shapes),
                    value[name],
                    member,
                    shapes,
                    item_name,
                )
            )
        return "".join(out)
    if kind == "list":
        member = shape["member"]
        tag = member.get("locationName", item_name)
        return "".join(_xml_element(tag, v, member, shapes, item_name) for v in value)
    if kind == "map":
        key, val = (
            shape["key"].get("locationName", "key"),
            shape["value"].get("locationName", "value"),
        )
        return "".join(
            f"<entry><{key}>{escape(k)}</{key}>"
            f"{_xml_element(val, v, shape['value'], shapes, item_name)}</entry>"
            for k, v in value.items()
        )
    return _scalar_text(value, kind)


def _xml_element(tag: str, value, ref: dict, shapes: dict, item_name: str) -> str:
    shape = shapes[ref["shape"]]
    if shape["type"] == "list" and shape.get("flattened"):
        member = shape["member"]
        return "".join(_xml_element(tag, v, member, shapes, item_name) for v in value)
    return f"<{tag}>{_xml_children(value, ref, shapes, item_name)}</{tag}>"


# --- the answers --------------------------------------------------------------------------


def serialize(protocol: str, service: dict, operation: dict, response: dict):
    """``(status, headers, body)`` of a success, as ``protocol`` sends it."""
    shapes = service["shapes"]
    ref = operation["output"]
    name = operation["name"]
    namespace = service["metadata"].get("xmlNamespace", "")
    if protocol in ("json", "rest-json"):
        version = service["metadata"].get("jsonVersion", "1.1")
        kind = (
            f"application/x-amz-json-{version}"
            if protocol == "json"
            else "application/json"
        )
        body = json.dumps(_to_json(response, ref, shapes)).encode()
        return 200, {"Content-Type": kind, "x-amzn-RequestId": REQUEST_ID}, body
    if protocol == "smithy-rpc-v2-cbor":
        body = _cbor(_to_cbor_value(response, ref, shapes))
        headers = {
            "Content-Type": "application/cbor",
            "smithy-protocol": "rpc-v2-cbor",
            "x-amzn-RequestId": REQUEST_ID,
        }
        return 200, headers, body
    if protocol == "query":
        wrapper = ref.get("resultWrapper", f"{name}Result")
        inner = _xml_children(response, ref, shapes, "member")
        body = (
            f'<{name}Response xmlns="{namespace}"><{wrapper}>{inner}</{wrapper}>'
            f"<ResponseMetadata><RequestId>{REQUEST_ID}</RequestId></ResponseMetadata>"
            f"</{name}Response>"
        )
        return (
            200,
            {"Content-Type": "text/xml", "x-amzn-RequestId": REQUEST_ID},
            body.encode(),
        )
    if protocol == "ec2":
        inner = _xml_children(response, ref, shapes, "item")
        body = (
            f'<{name}Response xmlns="http://ec2.amazonaws.com/doc/{service["metadata"]["apiVersion"]}/">'
            f"<requestId>{REQUEST_ID}</requestId>{inner}</{name}Response>"
        )
        return 200, {"Content-Type": "text/xml;charset=UTF-8"}, body.encode()
    if protocol == "rest-xml":
        output = shapes[ref["shape"]]
        space = f' xmlns="{namespace}"' if namespace else ""
        if "payload" in output:
            member = output["members"][output["payload"]]
            root = member.get("locationName", output["payload"])
            inner = _xml_children(
                response.get(output["payload"], {}), member, shapes, "member"
            )
        else:
            root = f"{name}Result"
            inner = _xml_children(response, ref, shapes, "member")
        body = (
            f'<?xml version="1.0" encoding="UTF-8"?>\n<{root}{space}>{inner}</{root}>'
        )
        return (
            200,
            {"Content-Type": "application/xml", "x-amz-request-id": REQUEST_ID},
            body.encode(),
        )
    raise ValueError(f"protocol {protocol} is not written here")


def serialize_error(
    protocol: str, service: dict, code: str, message: str, status: int = 400
):
    """``(status, headers, body)`` of an error answer."""
    if protocol in ("json", "rest-json"):
        body = json.dumps({"__type": code, "message": message}).encode()
        headers = {
            "Content-Type": "application/x-amz-json-1.1",
            "x-amzn-RequestId": REQUEST_ID,
        }
        if protocol == "rest-json":
            headers["x-amzn-ErrorType"] = code
        return status, headers, body
    if protocol == "smithy-rpc-v2-cbor":
        body = _cbor({"__type": code, "message": message})
        return (
            status,
            {"Content-Type": "application/cbor", "smithy-protocol": "rpc-v2-cbor"},
            body,
        )
    if protocol == "ec2":
        body = (
            f"<Response><Errors><Error><Code>{escape(code)}</Code><Message>{escape(message)}"
            f"</Message></Error></Errors><RequestID>{REQUEST_ID}</RequestID></Response>"
        )
        return status, {"Content-Type": "text/xml;charset=UTF-8"}, body.encode()
    body = (
        f"<ErrorResponse><Error><Type>Sender</Type><Code>{escape(code)}</Code>"
        f"<Message>{escape(message)}</Message></Error><RequestId>{REQUEST_ID}</RequestId>"
        "</ErrorResponse>"
    )
    return status, {"Content-Type": "text/xml"}, body.encode()
