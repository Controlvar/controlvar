#!/usr/bin/env python3
"""Decide an API fuzzing pass from its Schemathesis report.

The fuzz arm of ``.github/workflows/_platform.yml`` and ``make fuzz`` both run
Schemathesis against a running API and then ask this script for the verdict.
Schemathesis's own exit status is never the verdict: it is 1 on any server
error, known or not, and 0 on a run that reached nothing at all.

Four subcommands, reading the served OpenAPI document and the lists under
``tests/fuzz/``:

``args``
    The Schemathesis filter arguments for one pass, one argument per line: the
    ``sdk`` pass is restricted to the SDK routes, and every pass leaves out the
    operations ``tests/fuzz/exclusions.toml`` names for it.

``requests``
    The requests of the pass's ndjson report that were answered 5xx, as a
    JSON file: each one's method, the path as it was sent, its status and the
    Schemathesis label it was generated under. Standard library only.

``routes``
    Which route of the application answered each of those requests, resolved
    by the application's own router: it imports ``backend.app.main`` (from
    ``--app-root``), refuses unless that application's operations are exactly
    the served document's, and dispatches each request's method and path
    through ``app.router`` with every route handler replaced by one that only
    records which route the router chose (see ``resolve_routes``). It runs
    where the API's packages are installed: on the CI runner, and inside the
    API container for ``make fuzz``, both times with the database and Redis
    pointed at a closed port, so that nothing could be written even if a
    handler did run.

``evaluate``
    Decide the pass. The pass selects the served document's operations, minus
    its exclusions (and, for ``sdk``, only the SDK routes). For each one, only
    the interactions whose method is the operation's own count, so the
    coverage phase's method probes (answered 405) do not. An operation is
    REACHED when one counted interaction answered a status outside
    ``NOT_REACHED``; otherwise, or when the report has no interaction for it,
    it is UNREACHED. A 5xx is counted against the route that ANSWERED it, not
    the label it was generated under: a ``DELETE`` probe sent to the path of
    ``GET /users/me`` is answered by ``DELETE /users/{user_id}``. The pass is
    green only when:

    * every (answering route, status) that answered 5xx is listed for this
      pass in ``tests/fuzz/known-5xx.toml``, and every entry listed for this
      pass answered 5xx in this run (an entry that no longer fires is stale);
    * every UNREACHED operation is listed for this pass in
      ``tests/fuzz/unreached.toml``, and no listed operation was reached (a
      stale entry);
    * Schemathesis selected exactly the operations this pass selects, and
      fuzzed nothing else;
    * the report exists and holds at least one interaction, and every 5xx
      request's answering route was resolved.

Everything this script prints, the step summary it renders (through
``scripts/qa_render.py`` from ``.github/qa-templates/fuzz-*.tmpl``) and the
values it writes for the workflow's issue are counts, the pass, the date, the
commit and the seed. Which operations failed or were not reached, and the
requests behind each 5xx, go only to the ``--details`` file, which ``make
fuzz`` writes into its reproduction directory and the workflow never asks for,
and to the ``requests`` and ``routes`` files, which stay on the runner.
Nothing here ever reads a request or response body.

Exit status: 0 green, 1 red, 2 a list or an argument it refuses.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import re
import sys
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import (
    Any,
    Dict,
    Iterable,
    List,
    Mapping,
    NamedTuple,
    Optional,
    Sequence,
    Set,
    Tuple,
)
from urllib.parse import unquote, urlsplit

ROOT = Path(__file__).resolve().parents[1]
EXCLUSIONS = ROOT / "tests" / "fuzz" / "exclusions.toml"
UNREACHED = ROOT / "tests" / "fuzz" / "unreached.toml"
KNOWN_5XX = ROOT / "tests" / "fuzz" / "known-5xx.toml"

PASSES = ("superuser", "viewer", "sdk")

#: The seed the two lists were built from. fuzz.yml's scheduled run uses it
#: and a dispatch defaults to it: Schemathesis runs with
#: --generation-deterministic, so at one commit this seed sends the same
#: requests every night and the lists hold from one night to the next. Another
#: seed changes the fuzzing phase's requests, and so may move both lists for a
#: reason that is not the API.
LISTS_SEED = 20261006

#: The SDK routes, the ``sdk`` pass's whole selection. The same prefixes the
#: rate limiter gives the SDK limit (``SDK_PATH_PREFIXES`` in
#: ``backend/app/middleware/rate_limiter.py``); a test holds the two equal.
SDK_PATH_PREFIXES = (
    "/api/v1/tracking/",
    "/api/v1/feature-flags/evaluate/",
    "/api/v1/feature-flags/user/",
    "/api/v1/sdk/",
)
SDK_PATH_REGEX = "^(?:" + "|".join(re.escape(p) for p in SDK_PATH_PREFIXES) + ")"

#: Answers that say the request never got past validation, authentication,
#: routing or the rate limiter: an operation that only ever answered these
#: was not reached.
NOT_REACHED = frozenset({400, 401, 403, 404, 405, 422, 429})

#: A known-5xx entry names a public issue, or one of these causes, each with
#: the one status it may list: ``aws``, an AWS call the stack points at a
#: closed port, which fails as 500; ``config``, a setting the stack
#: deliberately leaves unset, which the route answers with 503. Any other
#: status of such a route is a defect, and needs an issue.
KNOWN_CAUSES = {"aws": 500, "config": 503}

HTTP_METHODS = ("get", "put", "post", "delete", "options", "head", "patch", "trace")
LABEL = re.compile(r"(GET|PUT|POST|DELETE|OPTIONS|HEAD|PATCH|TRACE) /\S*")
METHOD = re.compile(r"[A-Z]{1,16}")

#: The answering-route key of a 5xx request that no route matched.
NO_ROUTE = "no route"


class ListError(ValueError):
    """A list, a file or an argument this script refuses."""


class ReportError(ValueError):
    """The report cannot be read, or holds no interaction at all."""


@dataclass(frozen=True)
class Operation:
    method: str  # upper case
    path: str
    has_auth: bool  # the served document lists an auth scheme for it

    @property
    def label(self) -> str:
        """The operation's name, as Schemathesis labels it."""
        return f"{self.method} {self.path}"


def _requires_a_scheme(requirements: Any) -> bool:
    """True when an OpenAPI requirement list names at least one auth scheme
    (``[{}]`` names none: the operation may be called without one)."""
    return isinstance(requirements, list) and any(
        isinstance(requirement, dict) and requirement for requirement in requirements
    )


def load_operations(document: Mapping[str, Any]) -> Dict[str, Operation]:
    """Every operation of an OpenAPI document, by label."""
    top = document.get("security")
    operations: Dict[str, Operation] = {}
    for path, item in (document.get("paths") or {}).items():
        if not isinstance(item, dict):
            continue
        for method, operation in item.items():
            if method not in HTTP_METHODS or not isinstance(operation, dict):
                continue
            requirements = operation.get("security", top)
            op = Operation(method.upper(), path, _requires_a_scheme(requirements))
            operations[op.label] = op
    return operations


def _read_toml(path: Path) -> Dict[str, Any]:
    try:
        return tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as error:
        raise ListError(f"{path.name}: cannot be read ({type(error).__name__})")


def _entries(data: Mapping[str, Any], table: str, name: str) -> List[Dict[str, Any]]:
    unknown = sorted(set(data) - {table})
    if unknown:
        raise ListError(f"{name}: unknown top-level keys {unknown}")
    entries = data.get(table, [])
    if not isinstance(entries, list) or not all(isinstance(e, dict) for e in entries):
        raise ListError(f"{name}: [[{table}]] must be a list of tables")
    return entries


def _reason(entry: Mapping[str, Any], where: str) -> None:
    reason = entry.get("reason")
    if not isinstance(reason, str) or len(reason.strip()) < 10:
        raise ListError(f"{where}: every entry needs a reason (a sentence)")
    if "\n" in reason:
        raise ListError(f"{where}: a reason is one line")


def _operation(
    entry: Mapping[str, Any], where: str, operations: Mapping[str, Operation]
) -> str:
    label = entry.get("operation")
    if not isinstance(label, str) or LABEL.fullmatch(label) is None:
        raise ListError(f"{where}: operation must be 'METHOD /path'")
    if label not in operations:
        raise ListError(f"{where}: {label} is not an operation of the API document")
    return label


def load_exclusions(
    path: Path, operations: Mapping[str, Operation]
) -> Dict[str, Set[str]]:
    """``{pass: {excluded label}}`` from ``exclusions.toml``, every pass present."""
    name = path.name
    excluded: Dict[str, Set[str]] = {p: set() for p in PASSES}
    seen: Set[str] = set()
    for index, entry in enumerate(_entries(_read_toml(path), "exclude", name), 1):
        where = f"{name} entry {index}"
        unknown = sorted(set(entry) - {"operation", "passes", "reason"})
        if unknown:
            raise ListError(f"{where}: unknown keys {unknown}")
        label = _operation(entry, where, operations)
        if label in seen:
            raise ListError(f"{where}: {label} is listed twice")
        seen.add(label)
        passes = entry.get("passes")
        if (
            not isinstance(passes, list)
            or not passes
            or any(p not in PASSES for p in passes)
            or len(set(passes)) != len(passes)
        ):
            raise ListError(f"{where}: passes must be a list drawn from {PASSES}")
        _reason(entry, where)
        for pass_name in passes:
            excluded[pass_name].add(label)
    return excluded


def selection(
    pass_name: str,
    operations: Mapping[str, Operation],
    excluded: Mapping[str, Set[str]],
) -> Set[str]:
    """The labels a pass selects: the document, minus its exclusions, and for
    ``sdk`` only the SDK routes."""
    chosen = set(operations) - excluded[pass_name]
    if pass_name == "sdk":
        chosen = {
            label
            for label in chosen
            if operations[label].path.startswith(SDK_PATH_PREFIXES)
        }
    return chosen


def load_unreached(
    path: Path,
    pass_name: str,
    operations: Mapping[str, Operation],
    excluded: Mapping[str, Set[str]],
) -> Set[str]:
    """The labels ``unreached.toml`` lists for *pass_name*.

    Every entry of every pass is checked, so a broken entry fails all three
    passes, not only its own."""
    name = path.name
    listed: Dict[str, Set[str]] = {p: set() for p in PASSES}
    for index, entry in enumerate(_entries(_read_toml(path), "unreached", name), 1):
        where = f"{name} entry {index}"
        unknown = sorted(set(entry) - {"operation", "pass", "reason"})
        if unknown:
            raise ListError(f"{where}: unknown keys {unknown}")
        entry_pass = entry.get("pass")
        if entry_pass not in PASSES:
            raise ListError(f"{where}: pass must be one of {PASSES}")
        label = _operation(entry, where, operations)
        if label not in selection(entry_pass, operations, excluded):
            raise ListError(f"{where}: the {entry_pass} pass does not select {label}")
        if label in listed[entry_pass]:
            raise ListError(f"{where}: {label} is listed twice for {entry_pass}")
        _reason(entry, where)
        listed[entry_pass].add(label)
    return listed[pass_name]


#: A known-5xx key: (the answering route's label, the status it answered).
Known = Tuple[str, int]


def load_known(
    path: Path, pass_name: str, operations: Mapping[str, Operation]
) -> Set[Known]:
    """The (answering route, status) pairs ``known-5xx.toml`` lists for
    *pass_name*.

    An entry names its pass, the operation of the route that ANSWERED (any
    operation of the document: a probe can be answered by a route the pass
    does not select), a 5xx status, and either the public issue that tracks it
    or a cause from ``KNOWN_CAUSES`` (only at that cause's status), with a
    one-line reason. Every entry of
    every pass is checked, so a broken entry fails all three passes."""
    name = path.name
    listed: Dict[str, Set[Known]] = {p: set() for p in PASSES}
    for index, entry in enumerate(_entries(_read_toml(path), "known", name), 1):
        where = f"{name} entry {index}"
        unknown = sorted(
            set(entry) - {"pass", "operation", "status", "issue", "cause", "reason"}
        )
        if unknown:
            raise ListError(f"{where}: unknown keys {unknown}")
        entry_pass = entry.get("pass")
        if entry_pass not in PASSES:
            raise ListError(f"{where}: pass must be one of {PASSES}")
        label = _operation(entry, where, operations)
        status = entry.get("status")
        if (
            not isinstance(status, int)
            or isinstance(status, bool)
            or not 500 <= status <= 599
        ):
            raise ListError(f"{where}: status must be a 5xx status code")
        issue, cause = entry.get("issue"), entry.get("cause")
        if (issue is None) == (cause is None):
            raise ListError(f"{where}: name either the issue or the cause, not both")
        if issue is not None and (
            not isinstance(issue, int) or isinstance(issue, bool) or issue < 1
        ):
            raise ListError(f"{where}: issue must be a public issue number")
        if cause is not None and cause not in KNOWN_CAUSES:
            raise ListError(f"{where}: cause must be one of {sorted(KNOWN_CAUSES)}")
        if cause is not None and status != KNOWN_CAUSES[cause]:
            raise ListError(
                f"{where}: cause {cause} lists only status {KNOWN_CAUSES[cause]};"
                " any other status needs an issue"
            )
        _reason(entry, where)
        key = (label, status)
        if key in listed[entry_pass]:
            raise ListError(
                f"{where}: {label} {status} is listed twice for {entry_pass}"
            )
        listed[entry_pass].add(key)
    return listed[pass_name]


def filter_args(pass_name: str, excluded: Mapping[str, Set[str]]) -> List[str]:
    """The Schemathesis arguments that make it select exactly ``selection()``."""
    args: List[str] = []
    if pass_name == "sdk":
        args += ["--include-path-regex", SDK_PATH_REGEX]
    for label in sorted(excluded[pass_name]):
        args += ["--exclude-name", label]
    return args


# ---------------------------------------------------------------------------
# The report
# ---------------------------------------------------------------------------


class Interaction(NamedTuple):
    method: str  # upper case, as sent
    status: Optional[int]  # None: no answer
    path: str = ""  # the path as sent (still percent-encoded), no query


@dataclass
class Report:
    #: label -> its interactions
    interactions: Dict[str, List[Interaction]] = field(default_factory=dict)
    #: How many operations Schemathesis itself selected (None: not reported).
    selected: Optional[int] = None


def _interaction_items(interactions: Any) -> Iterable[Any]:
    if isinstance(interactions, dict):
        return interactions.values()
    if isinstance(interactions, list):
        return interactions
    return ()


def _path_of(uri: Any) -> str:
    if not isinstance(uri, str):
        return ""
    try:
        return urlsplit(uri).path
    except ValueError:
        return ""


def read_report(report_dir: Path) -> Report:
    """The interactions of the one ndjson report in *report_dir*."""
    files = sorted(Path(report_dir).glob("*.ndjson"))
    if len(files) != 1:
        raise ReportError(f"expected one ndjson report, found {len(files)}")
    report = Report()
    try:
        lines = files[0].read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError) as error:
        raise ReportError(f"the report cannot be read ({type(error).__name__})")
    for number, line in enumerate(lines, 1):
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except ValueError:
            raise ReportError(f"report line {number} is not JSON")
        if not isinstance(event, dict):
            continue
        loading = event.get("LoadingFinished")
        if isinstance(loading, dict):
            try:
                report.selected = int(loading["statistic"]["operations"]["selected"])
            except (KeyError, TypeError, ValueError):
                report.selected = None
        finished = event.get("ScenarioFinished")
        if not isinstance(finished, dict):
            continue
        recorder = finished.get("recorder") or {}
        label = recorder.get("label")
        if not isinstance(label, str):
            continue
        bucket = report.interactions.setdefault(label, [])
        for interaction in _interaction_items(recorder.get("interactions")):
            if not isinstance(interaction, dict):
                continue
            request = interaction.get("request") or {}
            response = interaction.get("response")
            status = response.get("status_code") if isinstance(response, dict) else None
            method = request.get("method") if isinstance(request, dict) else None
            uri = request.get("uri") if isinstance(request, dict) else None
            bucket.append(
                Interaction(
                    str(method).upper(),
                    status if isinstance(status, int) else None,
                    _path_of(uri),
                )
            )
    if not any(report.interactions.values()):
        raise ReportError("the report holds no interactions")
    return report


class ServerError(NamedTuple):
    """One distinct request that answered 5xx."""

    method: str
    path: str  # as sent
    status: int
    label: str  # the Schemathesis label it was generated under


def server_error_requests(report: Report) -> List[ServerError]:
    """Every distinct (method, path, status, label) that answered 5xx."""
    found: Set[ServerError] = set()
    for label, items in report.interactions.items():
        for item in items:
            if item.status is not None and 500 <= item.status <= 599:
                found.add(ServerError(item.method, item.path, item.status, label))
    return sorted(found)


# ---------------------------------------------------------------------------
# The answering route
# ---------------------------------------------------------------------------


class _Chosen(Exception):
    """Raised in place of a route's handler: the router chose this route."""

    def __init__(self, path: str, methods: Any) -> None:
        super().__init__(path)
        self.path = path
        self.methods = set(methods) if methods else None  # None: every method


def resolve_routes(
    app: Any, requests: Iterable[Tuple[str, str]]
) -> Dict[Tuple[str, str], Optional[str]]:
    """``{(method, path as sent): the answering route's label, or None}``.

    Each request is dispatched through the application's own router
    (``app.router``, without the middleware), so the router's own rules pick
    the route: its order, its included routers, a method no route on the path
    takes (405, None here), a slash redirect (307, None here). The handlers of
    ``starlette.routing.Route`` and ``fastapi.routing.APIRoute`` are replaced,
    for the length of this call, by one that records the route the router
    chose and stops: no endpoint runs. The route's path is the full path the
    API document names it by (for a route of an included router, FastAPI's
    effective path). The path is percent-decoded first, as the server decodes
    it before routing."""
    import asyncio

    from fastapi import routing as fastapi_routing
    from starlette import routing as starlette_routing

    effective = getattr(fastapi_routing, "_get_scope_effective_route_context", None)

    async def chosen(self: Any, scope: Any, receive: Any, send: Any) -> None:
        context = effective(scope) if effective is not None else None
        if context is not None and getattr(context, "original_route", None) is self:
            raise _Chosen(context.path, context.methods)
        raise _Chosen(self.path, getattr(self, "methods", None))

    async def receive() -> Dict[str, Any]:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: Mapping[str, Any]) -> None:
        return None

    async def dispatch(method: str, path: str) -> Optional[str]:
        scope = {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": method,
            "scheme": "http",
            "path": path,
            "raw_path": path.encode("utf-8", "surrogateescape"),
            "root_path": "",
            "query_string": b"",
            "headers": [(b"host", b"localhost")],
            "client": ("127.0.0.1", 1),
            "server": ("127.0.0.1", 80),
            "app": app,
        }
        try:
            await app.router(scope, receive, send)
        except _Chosen as route:
            if route.methods is None or method in route.methods:
                return f"{method} {route.path}"
            return None
        except Exception:  # the router's own 404 or 405
            return None
        return None  # the router answered itself (a redirect)

    handlers = {
        cls: cls.__dict__["handle"]
        for cls in (starlette_routing.Route, fastapi_routing.APIRoute)
        if "handle" in cls.__dict__
    }
    for cls in handlers:
        cls.handle = chosen
    try:
        resolved: Dict[Tuple[str, str], Optional[str]] = {}
        for method, sent in requests:
            resolved[(method, sent)] = asyncio.run(dispatch(method, unquote(sent)))
        return resolved
    finally:
        for cls, handle in handlers.items():
            cls.handle = handle


def import_app(app_root: Path) -> Any:
    """The API application, imported from *app_root*."""
    sys.path.insert(0, str(app_root))
    from backend.app.main import app

    return app


def app_operations(app: Any) -> Dict[str, Operation]:
    """The operations of the document *app* serves."""
    from fastapi.openapi.utils import get_openapi

    return load_operations(
        get_openapi(title=app.title, version=app.version, routes=app.routes)
    )


# ---------------------------------------------------------------------------
# The verdict
# ---------------------------------------------------------------------------


class Answered(NamedTuple):
    """A 5xx request and the route that answered it."""

    method: str
    path: str
    status: int
    label: str
    route: Optional[str]  # None: no route matched

    @property
    def key(self) -> Known:
        return (self.route or f"{NO_ROUTE} {self.method} {self.path}", self.status)


@dataclass
class Verdict:
    selected: Set[str]
    fuzzed: Set[str]
    outside: Set[str]
    reached: Set[str]
    unreached: Set[str]
    unlisted: Set[str]
    stale: Set[str]
    answered: List[Answered]
    known: Set[Known]
    no_answer: Set[str]
    auth_declared: Set[str]
    schemathesis_selected: Optional[int]
    report_problem: Optional[str] = None

    @property
    def errors(self) -> Set[Known]:
        """Every (answering route, status) that answered 5xx."""
        return {item.key for item in self.answered}

    @property
    def error_routes(self) -> Set[str]:
        return {route for route, _status in self.errors}

    @property
    def errors_unlisted(self) -> Set[Known]:
        return self.errors - self.known

    @property
    def known_quiet(self) -> Set[Known]:
        """Listed entries that answered no 5xx in this run: stale."""
        return self.known - self.errors

    @property
    def selection_mismatch(self) -> bool:
        return self.schemathesis_selected != len(self.selected)

    @property
    def green(self) -> bool:
        return not (
            self.report_problem
            or self.errors_unlisted
            or self.known_quiet
            or self.unlisted
            or self.stale
            or self.outside
            or self.selection_mismatch
        )


def decide(
    report: Report,
    operations: Mapping[str, Operation],
    selected: Set[str],
    listed: Set[str],
    known: Set[Known] = frozenset(),
    routes: Optional[Mapping[Tuple[str, str], Optional[str]]] = None,
    report_problem: Optional[str] = None,
) -> Verdict:
    interactions = report.interactions
    fuzzed = {label for label, items in interactions.items() if items}
    reached = set()
    for label in selected:
        own = operations[label].method
        if any(
            item.method == own
            and item.status is not None
            and item.status not in NOT_REACHED
            for item in interactions.get(label, [])
        ):
            reached.add(label)
    unreached = selected - reached
    answered = []
    for error in server_error_requests(report):
        route = None
        if routes is not None:
            route = routes.get((error.method, error.path))
        elif report_problem is None:
            report_problem = "the routes that answered 5xx were not resolved"
        answered.append(Answered(*error, route))
    return Verdict(
        selected=set(selected),
        fuzzed=fuzzed,
        outside=fuzzed - selected,
        reached=reached,
        unreached=unreached,
        unlisted=unreached - listed,
        stale=listed & reached,
        answered=answered,
        known=set(known),
        no_answer={
            label
            for label, items in interactions.items()
            if any(item.status is None for item in items)
        },
        auth_declared={label for label in selected if operations[label].has_auth},
        schemathesis_selected=report.selected,
        report_problem=report_problem,
    )


def counts_line(
    verdict: Verdict, pass_name: str, date: str, sha: str, seed: str
) -> str:
    """The one line a run prints: the pass, date, commit, seed and counts."""
    v = verdict
    by_tool = "none" if v.schemathesis_selected is None else v.schemathesis_selected
    lists_seed = "the lists' seed" if seed == str(LISTS_SEED) else "not the lists' seed"
    return (
        f"fuzz pass {pass_name}, {date}, commit {sha}, seed {seed} ({lists_seed}):"
        f" {'GREEN' if v.green else 'RED'}"
        f" | operations selected {len(v.selected)} (Schemathesis {by_tool}),"
        f" fuzzed {len(v.fuzzed)}, outside the selection {len(v.outside)}"
        f" | 5xx operations {len(v.error_routes)}"
        f" (not listed {len({r for r, _ in v.errors_unlisted})},"
        f" known entries {len(v.known)}, of them with no 5xx {len(v.known_quiet)})"
        f" | reached {len(v.reached)}, unreached {len(v.unreached)}"
        f" (not listed {len(v.unlisted)}, listed but reached {len(v.stale)})"
        f" | declaring an auth scheme {len(v.auth_declared)},"
        f" of them reached {len(v.auth_declared & v.reached)}"
        f" | with a request that got no answer {len(v.no_answer)}"
    )


def summary_values(
    verdict: Verdict,
    pass_name: str,
    date: str,
    sha: str,
    seed: str,
    run_link: Optional[str],
) -> Tuple[str, Dict[str, str]]:
    """The template and its values for this verdict."""
    v = verdict
    values = {
        "check": pass_name,
        "date": date,
        "sha": sha,
        "seed": seed,
        "count_fuzzed": str(len(v.fuzzed)),
        "count_operations": str(len(v.selected)),
        "count_5xx": str(len(v.error_routes)),
        "count_reached": str(len(v.reached)),
    }
    if v.green:
        values["count_unreached"] = str(len(v.unreached))
        return "fuzz-green.tmpl", values
    if run_link is None:
        raise ListError("a red summary needs --run-link")
    values.update(
        {
            "count_5xx_unlisted": str(len({r for r, _ in v.errors_unlisted})),
            "count_known_quiet": str(len(v.known_quiet)),
            "count_unreached_unlisted": str(len(v.unlisted)),
            "count_unreached_stale": str(len(v.stale)),
            "run_link": run_link,
        }
    )
    return "fuzz-red.tmpl", values


def quiet_entries_text(verdict: Verdict, pass_name: str) -> str:
    """The known entries that answered no 5xx, one ``METHOD path (pass)`` per
    line, for the step summary and the job log only.

    The labels are operations of the public API document, and the entries are
    already public in ``tests/fuzz/known-5xx.toml``; the text is not part of
    ``values.json``, so the issue the report posts is unchanged. Empty when
    no entry went quiet.
    """
    labels = sorted({label for label, _ in verdict.known_quiet})
    if not labels:
        return ""
    lines = "".join(
        f"- {label} ({pass_name})\n".encode("ascii", "backslashreplace").decode()
        for label in labels
    )
    return f"\nKnown entries with no 5xx in this run:\n\n{lines}"


def details(verdict: Verdict) -> Dict[str, Any]:
    """Which operations and requests, for the local reproduction directory only."""
    v = verdict
    return {
        "green": v.green,
        "report_problem": v.report_problem,
        "selected_by_schemathesis": v.schemathesis_selected,
        "selected": len(v.selected),
        "server_errors": [
            {
                "method": item.method,
                "path": item.path,
                "status": item.status,
                "route": item.route,
                "label": item.label,
            }
            for item in v.answered
        ],
        "server_error_routes": sorted(f"{r} {s}" for r, s in v.errors),
        "server_errors_unlisted": sorted(f"{r} {s}" for r, s in v.errors_unlisted),
        "known_with_no_5xx": sorted(f"{r} {s}" for r, s in v.known_quiet),
        "unreached_unlisted": sorted(v.unlisted),
        "listed_but_reached": sorted(v.stale),
        "fuzzed_outside_the_selection": sorted(v.outside),
        "no_answer": sorted(v.no_answer),
        "unreached": sorted(v.unreached),
    }


# ---------------------------------------------------------------------------
# Command line
# ---------------------------------------------------------------------------


def _document(path: Path) -> Dict[str, Any]:
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise ListError(f"the API document cannot be read ({type(error).__name__})")
    if not isinstance(document, dict) or not load_operations(document):
        raise ListError("the API document has no operations")
    return document


def _render(template: str, values: Mapping[str, str]) -> str:
    """The step summary, through the one reader of .github/qa-templates/."""
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import qa_render

    try:
        return qa_render.render(template, values).body
    except qa_render.RenderError as error:
        # Its messages name placeholders and kinds, never a value.
        raise ListError(f"the summary was refused: {error}")


def write_requests(report: Report, out: Path) -> int:
    """``out``: the 5xx requests as JSON; returns how many."""
    errors = server_error_requests(report)
    out.write_text(
        json.dumps({"requests": [list(e) for e in errors]}, indent=1) + "\n",
        encoding="utf-8",
    )
    return len(errors)


def read_requests(path: Path) -> List[ServerError]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        items = [ServerError(*item) for item in data["requests"]]
    except (OSError, ValueError, KeyError, TypeError) as error:
        raise ListError(f"the requests file cannot be read ({type(error).__name__})")
    for item in items:
        if (
            METHOD.fullmatch(str(item.method)) is None
            or not isinstance(item.path, str)
            or not isinstance(item.status, int)
        ):
            raise ListError("the requests file holds an entry that is not a request")
    return items


def write_routes(
    app: Any, document: Mapping[str, Any], requests: Sequence[ServerError], out: Path
) -> int:
    """Resolve each request's answering route; returns how many matched none."""
    served = set(load_operations(document))
    imported = set(app_operations(app))
    if served != imported:
        raise ListError(
            "the application imported here does not serve the document the run"
            f" fuzzed ({len(served ^ imported)} operations differ)"
        )
    resolved = resolve_routes(app, {(r.method, r.path) for r in requests})
    out.write_text(
        json.dumps(
            {
                "routes": [
                    [m, p, route]
                    for (m, p), route in sorted(resolved.items(), key=lambda kv: kv[0])
                ]
            },
            indent=1,
        )
        + "\n",
        encoding="utf-8",
    )
    return sum(1 for route in resolved.values() if route is None)


def read_routes(path: Path) -> Dict[Tuple[str, str], Optional[str]]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        routes = {(m, p): route for m, p, route in data["routes"]}
    except (OSError, ValueError, KeyError, TypeError) as error:
        raise ReportError(f"the routes file cannot be read ({type(error).__name__})")
    for (method, sent), route in routes.items():
        if not isinstance(sent, str) or METHOD.fullmatch(str(method)) is None:
            raise ReportError("the routes file holds an entry that is not a request")
        if route is not None and (
            not isinstance(route, str) or LABEL.fullmatch(route) is None
        ):
            raise ReportError(
                "the routes file holds a route that is not 'METHOD /path'"
            )
    return routes


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("args")
    p.add_argument("--pass", dest="pass_name", required=True, choices=PASSES)
    p.add_argument("--schema", required=True, type=Path)
    p.add_argument("--exclusions", type=Path, default=EXCLUSIONS)
    p = sub.add_parser("requests")
    p.add_argument("--report-dir", required=True, type=Path)
    p.add_argument("--out", required=True, type=Path)
    p = sub.add_parser("routes")
    p.add_argument("--schema", required=True, type=Path)
    p.add_argument("--requests", required=True, type=Path)
    p.add_argument("--out", required=True, type=Path)
    p.add_argument("--app-root", type=Path, default=ROOT)
    p = sub.add_parser("evaluate")
    p.add_argument("--pass", dest="pass_name", required=True, choices=PASSES)
    p.add_argument("--schema", required=True, type=Path)
    p.add_argument("--exclusions", type=Path, default=EXCLUSIONS)
    p.add_argument("--unreached", type=Path, default=UNREACHED)
    p.add_argument("--known", type=Path, default=KNOWN_5XX)
    p.add_argument("--report-dir", required=True, type=Path)
    p.add_argument("--routes", required=True, type=Path)
    p.add_argument("--seed", required=True)
    p.add_argument("--sha", required=True)
    p.add_argument("--date", default=None)
    p.add_argument("--run-link", default=None)
    p.add_argument("--summary", type=Path, default=None)
    p.add_argument("--values", type=Path, default=None)
    p.add_argument("--details", type=Path, default=None)
    args = parser.parse_args(argv)

    try:
        if args.command == "requests":
            count = write_requests(read_report(args.report_dir), args.out)
            print(f"fuzz_check: {count} distinct requests answered 5xx")
            return 0
        if args.command == "routes":
            document = _document(args.schema)
            unmatched = write_routes(
                import_app(args.app_root),
                document,
                read_requests(args.requests),
                args.out,
            )
            print(
                f"fuzz_check: answering routes resolved; {unmatched} matched no route"
            )
            return 0
        operations = load_operations(_document(args.schema))
        excluded = load_exclusions(args.exclusions, operations)
        if args.command == "args":
            print("\n".join(filter_args(args.pass_name, excluded)))
            return 0
        if re.fullmatch(r"[0-9]{1,10}", args.seed) is None:
            raise ListError("--seed must be an integer of one to ten digits")
        if re.fullmatch(r"[0-9a-f]{7,40}", args.sha) is None:
            raise ListError("--sha must be a commit")
        listed = load_unreached(args.unreached, args.pass_name, operations, excluded)
        known = load_known(args.known, args.pass_name, operations)
        selected = selection(args.pass_name, operations, excluded)
        date = args.date or _dt.datetime.now(_dt.timezone.utc).date().isoformat()
        problem: Optional[str] = None
        try:
            report = read_report(args.report_dir)
        except ReportError as error:
            report, problem = Report(), str(error)
        routes: Optional[Dict[Tuple[str, str], Optional[str]]] = None
        try:
            routes = read_routes(args.routes)
        except ReportError as error:
            problem = problem or str(error)
        verdict = decide(report, operations, selected, listed, known, routes, problem)
        if args.summary is not None or args.values is not None:
            template, values = summary_values(
                verdict, args.pass_name, date, args.sha, args.seed, args.run_link
            )
        if args.summary is not None:
            args.summary.write_text(
                _render(template, values) + quiet_entries_text(verdict, args.pass_name),
                encoding="ascii",
            )
        if args.values is not None:
            # For fuzz.yml's issue: the same values, which qa_render checks
            # again before anything is posted.
            args.values.write_text(
                json.dumps({"template": template, "values": values}, sort_keys=True)
                + "\n",
                encoding="ascii",
            )
        if args.details is not None:
            args.details.write_text(
                json.dumps(details(verdict), indent=2) + "\n", encoding="utf-8"
            )
    except ListError as error:
        print(f"fuzz_check: refused: {error}", file=sys.stderr)
        return 2
    except ReportError as error:
        print(f"fuzz_check: {error}", file=sys.stderr)
        return 2
    if verdict.report_problem:
        print(f"fuzz_check: {verdict.report_problem}")
    print(counts_line(verdict, args.pass_name, date, args.sha, args.seed))
    return 0 if verdict.green else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except Exception as error:
        # A traceback could quote a value from the report; the class is enough.
        print(f"fuzz_check: failed ({type(error).__name__})", file=sys.stderr)
        sys.exit(2)
