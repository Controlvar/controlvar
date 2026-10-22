"""Every field a ``--query`` names, checked against what the CLI prints for the call.

A misspelled key reads as "nothing there" (``null``, then an empty file), and the CLI
and the fake print the same ``null``, so only a static check sees it. ``problems``
parses the expression with jmespath and walks it against botocore's output shape:

* the top of the expression meets what the CLI prints, which for a call the CLI
  pages through is the paginator's result keys (and non-aggregate keys) only, down
  their whole paths (CloudFront's ``DistributionList.Items`` prints no other member
  of ``DistributionList``); given a paging option by hand (its input token or limit
  key) or ``--no-paginate``, the CLI makes one request and prints the whole page,
  next-page token included;
* every field must be a member of the structure it is read from; a projection or a
  filter reads its elements; a function's arguments and a comparison's sides are
  checked the same way.

``cut_short`` names a read whose answer could leave items out: one that passes the
CLI's ``--max-items`` or ``--starting-token``, which cap or skip what it prints, and
one the CLI does not page through (paging off, or no paginator) whose output can
carry a next-page token, unless its query, run on a page that carries the token,
answers something the sweep refuses (not a list, not null).
"""

from __future__ import annotations

from dataclasses import dataclass

from botocore import xform_name
from botocore.exceptions import DataNotFoundError
from botocore.session import get_session

from . import shapes as _shapes

#: Output members that mean "there is more": a call whose output has one and no
#: paginator to follow it can be cut short.
NEXT_PAGE_MEMBERS = {
    "NextToken",
    "nextToken",
    "Marker",
    "NextMarker",
    "PaginationToken",
    "ContinuationToken",
    "IsTruncated",
    "Truncated",
}


@dataclass
class Printed:
    """What the CLI prints of a structure: the output shape, or only the
    paginator's keys of it. A member on a result key's path is itself a Printed,
    holding only the next part of the path."""

    shape: object
    members: dict


class _Session:
    def __init__(self) -> None:
        self.session = get_session()
        self.models: dict = {}

    def operation(self, service: str, cli_op: str):
        name = _shapes.model_name(service)
        if name not in self.models:
            self.models[name] = self.session.get_service_model(name)
        model = self.models[name]
        names = {xform_name(o, "-"): o for o in model.operation_names}
        op = names[cli_op]
        try:
            paginator = self.session.get_paginator_model(name).get_paginator(op)
        except (DataNotFoundError, ValueError):
            paginator = None
        return model.operation_model(op).output_shape, paginator


_SESSION: _Session | None = None


def _session() -> _Session:
    global _SESSION
    if _SESSION is None:
        _SESSION = _Session()
    return _SESSION


def _keys(value) -> list[str]:
    if value is None:
        return []
    return list(value) if isinstance(value, list) else [value]


def _paging_off(paginator, args: list[str]) -> bool:
    if paginator is None or _shapes.NO_PAGINATE in args:
        return True
    manual = _shapes.manual_paging_flags(paginator)
    return bool(set(manual) & set(args))


def _path_tree(paths: list[str]) -> dict:
    """``["A.B", "A.C", "D"]`` as ``{"A": {"B": {}, "C": {}}, "D": {}}``."""
    tree: dict = {}
    for path in paths:
        node = tree
        for part in path.split("."):
            node = node.setdefault(part, {})
    return tree


def _restricted(shape, tree: dict) -> Printed:
    """``shape`` (a structure) with only the members ``tree`` names; a member
    whose path goes on is restricted in turn."""
    members = {}
    for name, rest in tree.items():
        if shape is None or getattr(shape, "type_name", None) != "structure":
            continue
        if name not in shape.members:
            continue
        member = shape.members[name]
        members[name] = _restricted(member, rest) if rest else member
    return Printed(shape, members)


def printed(service: str, cli_op: str, args: list[str]) -> Printed:
    output, paginator = _session().operation(service, cli_op)
    if _paging_off(paginator, args):
        members = dict(output.members) if output is not None else {}
        return Printed(output, members)
    paths = _keys(paginator.get("result_key")) + _keys(
        paginator.get("non_aggregate_keys")
    )
    return _restricted(output, _path_tree(paths))


class _List:
    """A projection's result: a list of ``element``."""

    def __init__(self, element) -> None:
        self.element = element


def _element(shape):
    if isinstance(shape, _List):
        return shape.element
    if shape is not None and getattr(shape, "type_name", None) == "list":
        return shape.member
    return None


def _walk(node: dict, shape, errors: list[str]):
    kind = node["type"]
    children = node.get("children", [])
    if kind == "field":
        name = node["value"]
        if isinstance(shape, Printed):
            if name not in shape.members:
                errors.append(
                    f"{name!r} is not printed for this call (it prints {sorted(shape.members)})"
                )
                return None
            return shape.members[name]
        if shape is None:
            return None
        if isinstance(shape, _List):
            errors.append(f"field {name!r} read from a list")
            return None
        if shape.type_name == "map":
            return shape.value
        if shape.type_name != "structure":
            errors.append(f"field {name!r} read from a {shape.type_name}")
            return None
        if name not in shape.members:
            errors.append(f"no member {name!r} in {shape.name}")
            return None
        return shape.members[name]
    if kind in ("subexpression", "pipe"):
        current = shape
        for child in children:
            current = _walk(child, current, errors)
        return current
    if kind == "projection":
        left = _walk(children[0], shape, errors)
        element = _element(left)
        if children[1]["type"] == "identity":
            return _List(element)
        return _List(_walk(children[1], element, errors))
    if kind == "filter_projection":
        left = _walk(children[0], shape, errors)
        element = _element(left)
        _walk(children[2], element, errors)
        if children[1]["type"] == "identity":
            return _List(element)
        return _List(_walk(children[1], element, errors))
    if kind == "value_projection":
        _walk(children[0], shape, errors)
        return _List(None)
    if kind == "flatten":
        inner = _walk(children[0], shape, errors)
        element = _element(inner)
        nested = _element(element)
        return _List(nested if nested is not None else element)
    if kind == "index_expression":
        left = _walk(children[0], shape, errors)
        return _element(left)
    if kind in ("multi_select_list",):
        for child in children:
            _walk(child, shape, errors)
        return None
    if kind == "multi_select_dict":
        for pair in children:
            _walk(pair["children"][0], shape, errors)
        return None
    if kind in ("comparator", "and_expression", "or_expression", "not_expression"):
        for child in children:
            _walk(child, shape, errors)
        return None
    if kind == "function_expression":
        for child in children:
            _walk(child, shape, errors)
        return None
    if kind in ("identity", "current"):
        return shape
    if kind in ("literal", "index", "slice", "expref"):
        return None
    errors.append(f"unhandled expression node {kind}")
    return None


def problems(service: str, cli_op: str, args: list[str], query: str) -> list[str]:
    """Every field ``query`` names that the call does not print."""
    import jmespath

    errors: list[str] = []
    tree = jmespath.compile(query).parsed
    _walk(tree, printed(service, cli_op, args), errors)
    return errors


def _sample(shape, depth: int = 0):
    """A small value of ``shape``: every member present, each list one long."""
    if shape is None or depth > 6:
        return None
    kind = shape.type_name
    if kind == "structure":
        return {name: _sample(m, depth + 1) for name, m in shape.members.items()}
    if kind == "list":
        return [_sample(shape.member, depth + 1)]
    if kind == "map":
        return {"k": _sample(shape.value, depth + 1)}
    if kind == "boolean":
        return True
    if kind in ("integer", "long", "float", "double"):
        return 1
    return "x"


def _answer_on_a_page_with_more(output, tokens: set[str], query: str):
    """The query's answer on a page of ``output`` that carries each of ``tokens``."""
    import jmespath

    page = _sample(output) or {}
    for token in tokens:
        head, _, rest = token.partition(".")
        value = True if (rest or head) in ("IsTruncated", "Truncated") else "t"
        if rest and isinstance(page.get(head), dict):
            page[head][rest] = value
        else:
            page[head] = value
    return jmespath.search(query, page)


def cut_short(service: str, cli_op: str, args: list[str], query: str) -> str | None:
    """Why this read's answer could leave items out, or None."""
    capped = sorted(set(_shapes.CLI_PAGING_OPTIONS) & set(args))
    if capped:
        return f"{', '.join(capped)} caps or skips what the CLI prints"
    output, paginator = _session().operation(service, cli_op)
    members = set(output.members) if output is not None else set()
    if paginator is None:
        tokens = NEXT_PAGE_MEMBERS & members
        why = "there is no paginator"
    elif _paging_off(paginator, args):
        tokens = set(_keys(paginator.get("output_token"))) | (
            NEXT_PAGE_MEMBERS & members
        )
        why = "a paging option given by hand, or --no-paginate, turns the CLI's paging off"
    else:
        return None
    if not tokens:
        return None
    answer = _answer_on_a_page_with_more(output, tokens, query)
    if answer is None or isinstance(answer, list):
        kind = "null" if answer is None else "a list"
        return (
            f"{why}, and on a page that says there is more ({sorted(tokens)}) the "
            f"query still answers {kind}, which the sweep takes for every item"
        )
    return None
