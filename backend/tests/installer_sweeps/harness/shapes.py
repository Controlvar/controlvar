"""Output shapes and paginators of the operations the fake ``aws`` answers.

``extract`` copies, from botocore's service models (the pip botocore the tests run
with), each operation's output shape with every shape it reaches, and its
paginator's result keys, into one small JSON document. The fake reads that file on
every call and checks each response against it (``validate``), so a response with a
member the model does not have, or a value of the wrong type, fails the call
instead of printing. Loading the full models on each call would cost far more than
the call itself.

The same document drives the wire serializers that answer the real CLI in
scripts/check_installer_cli.py (``wire.py``).
"""

from __future__ import annotations

import json
from pathlib import Path

#: The CLI's service names, and botocore's model names where they differ.
MODEL_NAMES = {"deploy": "codedeploy", "configservice": "config", "s3api": "s3"}

#: Metadata the serializers and the timestamp rule read.
METADATA_KEYS = (
    "apiVersion",
    "endpointPrefix",
    "jsonVersion",
    "protocol",
    "protocols",
    "serviceId",
    "signingName",
    "targetPrefix",
    "xmlNamespace",
)

#: Shape keys that carry no meaning for a response.
DROPPED_KEYS = ("documentation", "documentationUrl", "deprecated", "deprecatedMessage")


def model_name(cli_service: str) -> str:
    return MODEL_NAMES.get(cli_service, cli_service)


def cli_operation(name: str) -> str:
    """The CLI's spelling of an operation name: DescribeVpcs is describe-vpcs."""
    from botocore import xform_name

    return xform_name(name, "-")


def _copy(shape: dict) -> dict:
    out = {k: v for k, v in shape.items() if k not in DROPPED_KEYS}
    if "members" in out:
        out["members"] = {
            name: {k: v for k, v in ref.items() if k not in DROPPED_KEYS}
            for name, ref in out["members"].items()
        }
    return out


def _closure(shapes: dict, name: str | None, into: dict) -> None:
    if not name or name in into:
        return
    shape = _copy(shapes[name])
    into[name] = shape
    for ref in shape.get("members", {}).values():
        _closure(shapes, ref["shape"], into)
    for key in ("member", "key", "value"):
        if key in shape:
            _closure(shapes, shape[key]["shape"], into)


def extract(operations: list[tuple[str, str]]) -> dict:
    """``{cli service: {"metadata", "operations": {cli op: ...}, "shapes"}}``.

    An operation the model does not have raises KeyError: the fake must never
    answer for an operation the CLI would refuse.
    """
    from botocore.exceptions import DataNotFoundError
    from botocore.session import get_session

    loader = get_session().get_component("data_loader")
    out: dict = {}
    for service in sorted({s for s, _ in operations}):
        model = loader.load_service_model(model_name(service), "service-2")
        try:
            pages = loader.load_service_model(model_name(service), "paginators-1")
            pagination = pages["pagination"]
        except DataNotFoundError:
            pagination = {}
        names = {cli_operation(n): n for n in model["operations"]}
        entry: dict = {
            "metadata": {
                k: model["metadata"][k] for k in METADATA_KEYS if k in model["metadata"]
            },
            "operations": {},
            "shapes": {},
        }
        for wanted_service, op in operations:
            if wanted_service != service:
                continue
            name = names[op]
            spec = model["operations"][name]
            paginator = pagination.get(name)
            entry["operations"][op] = {
                "name": name,
                "http": spec.get("http", {}),
                "output": spec.get("output"),
                "paginator": None
                if paginator is None
                else {
                    k: paginator[k]
                    for k in ("result_key", "non_aggregate_keys")
                    if k in paginator
                },
            }
            _closure(
                model["shapes"],
                (spec.get("output") or {}).get("shape"),
                entry["shapes"],
            )
        out[service] = entry
    return out


def write(path: Path, operations: list[tuple[str, str]]) -> Path:
    path.write_text(json.dumps(extract(operations), sort_keys=True))
    return path


# --- the check ---------------------------------------------------------------------


def validate(value, ref: dict, shapes: dict, where: str = "$") -> list[str]:
    """Every way ``value`` differs from the shape ``ref`` names; empty when it fits.

    Unknown members, missing required members and wrong types are reported. An
    enum is not: services answer values their models do not list (EC2 reports an
    attached internet gateway as ``available``, outside AttachmentStatus), and
    botocore passes them through. A timestamp is the string the CLI prints, a blob
    its base64 text.
    """
    shape = shapes[ref["shape"]]
    kind = shape["type"]
    errors: list[str] = []
    if kind == "structure":
        if not isinstance(value, dict):
            return [
                f"{where}: {type(value).__name__}, not a structure ({ref['shape']})"
            ]
        members = shape.get("members", {})
        for key in value:
            if key not in members:
                errors.append(f"{where}.{key}: not a member of {ref['shape']}")
        for key in shape.get("required", []):
            if key not in value:
                errors.append(f"{where}.{key}: required by {ref['shape']}, absent")
        for key, item in value.items():
            if key in members:
                errors += validate(item, members[key], shapes, f"{where}.{key}")
    elif kind == "list":
        if not isinstance(value, list):
            return [f"{where}: {type(value).__name__}, not a list ({ref['shape']})"]
        for n, item in enumerate(value):
            errors += validate(item, shape["member"], shapes, f"{where}[{n}]")
    elif kind == "map":
        if not isinstance(value, dict):
            return [f"{where}: {type(value).__name__}, not a map ({ref['shape']})"]
        for key, item in value.items():
            errors += validate(key, shape["key"], shapes, f"{where}<key>")
            errors += validate(item, shape["value"], shapes, f"{where}[{key!r}]")
    elif kind == "string":
        if not isinstance(value, str):
            return [f"{where}: {value!r} is not a string ({ref['shape']})"]
    elif kind in ("integer", "long"):
        if isinstance(value, bool) or not isinstance(value, int):
            errors.append(f"{where}: {value!r} is not an integer ({ref['shape']})")
    elif kind in ("float", "double"):
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            errors.append(f"{where}: {value!r} is not a number ({ref['shape']})")
    elif kind == "boolean":
        if not isinstance(value, bool):
            errors.append(f"{where}: {value!r} is not a boolean ({ref['shape']})")
    elif kind in ("timestamp", "blob"):
        if not isinstance(value, str):
            errors.append(f"{where}: {value!r} is not text ({ref['shape']}, {kind})")
    else:
        errors.append(f"{where}: shape type {kind} is not modelled ({ref['shape']})")
    return errors


def member_order(value, ref: dict, shapes: dict):
    """``value`` with every structure's keys in the model's member order, as the
    CLI's parsers build them, so JSON output matches byte for byte."""
    shape = shapes[ref["shape"]]
    kind = shape["type"]
    if kind == "structure" and isinstance(value, dict):
        members = shape.get("members", {})
        return {
            key: member_order(value[key], members[key], shapes)
            for key in members
            if key in value
        }
    if kind == "list" and isinstance(value, list):
        return [member_order(item, shape["member"], shapes) for item in value]
    if kind == "map" and isinstance(value, dict):
        return {k: member_order(v, shape["value"], shapes) for k, v in value.items()}
    return value
