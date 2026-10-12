"""No route puts an exception's text in a response.

A failure nobody planned for answers a fixed sentence with the request ID
(``backend.app.core.logger.unexpected_failure`` / ``failure_detail``); the
error's own text goes to the server log only.  This walks the AST of
``backend/app`` and ``modules/backend/app`` and, inside every ``except``
block, flags any *sink* whose value derives from the caught exception:

* a response constructor -- ``HTTPException``, ``JSONResponse`` and the other
  ``*Response`` classes -- given the value as any argument but
  ``status_code``;
* a dict literal key or a call keyword named ``error``, ``errors``,
  ``detail``, ``details``, ``message``, ``msg``, ``reason``,
  ``error_message`` or ``description`` given the value (result objects and
  payload dicts that a route may return).

"Derives from" is a taint walk inside the handler: the bound name, and any
name assigned from a tainted expression in that handler (``msg = str(e)``,
then ``detail=msg``; repeated to a fixed point), through attribute access,
subscripts, calls and f-strings (``e.args[0]``, ``e.orig``, ``str(e)``,
``repr(e)``, ``f"{e}"``).  ``traceback.format_exc()`` and ``sys.exc_info()``
are tainted in every handler, named or not.  ``type(e)`` and
``isinstance(e, ...)`` are not: the type's name is not the error's text.

A sink under a BROAD exception type -- ``Exception``, ``BaseException``, a
bare ``except``, the SQLAlchemy/DBAPI errors, ``ClientError``,
``HTTPError``, ``OSError`` and kin -- always fails: those carry whatever the
driver, SDK or library put in them.  A sink under a typed exception the code
raises itself (a domain error, an authored ``ValueError``) is listed in
``ALLOWED`` by (file, function, exception types) with the exact number of
sinks and the reason its text is safe to show.  A new site fails until it is
classified here; an entry that matches nothing fails as stale.

Runs with no ``.git`` and with ``modules/`` deleted (``scripts/core_build.sh``):
entries under ``modules/`` are skipped when that directory is absent.
"""

from __future__ import annotations

import ast
import pathlib
import textwrap
from collections import Counter
from typing import Dict, Iterator, List, NamedTuple, Optional, Set, Tuple

import pytest

from backend.tests.smoke.modules_manifest import MODULES_DIR, REPO_ROOT

pytestmark = [pytest.mark.smoke, pytest.mark.regression]

SCAN_ROOTS = ("backend/app", f"{MODULES_DIR}/backend/app")

#: Never allow-listed: their text is whatever a driver, SDK or library wrote.
BROAD_TYPES = frozenset(
    {
        "<bare>",
        "Exception",
        "BaseException",
        # SQLAlchemy and DBAPI
        "SQLAlchemyError",
        "DBAPIError",
        "StatementError",
        "OperationalError",
        "DatabaseError",
        "IntegrityError",
        "DataError",
        "ProgrammingError",
        "InterfaceError",
        "InternalError",
        "NotSupportedError",
        "Psycopg2Error",
        # botocore
        "ClientError",
        "BotoCoreError",
        # HTTP clients
        "HTTPError",
        "HTTPStatusError",
        "RequestError",
        "RequestException",
        "URLError",
        # OS and I/O
        "OSError",
        "IOError",
        "EnvironmentError",
        "ConnectionError",
        "TimeoutError",
    }
)

RESPONSE_CALLS = frozenset(
    {
        "HTTPException",
        "StarletteHTTPException",
        "WebSocketException",
        "Response",
        "JSONResponse",
        "ORJSONResponse",
        "UJSONResponse",
        "PlainTextResponse",
        "HTMLResponse",
        "RedirectResponse",
        "StreamingResponse",
    }
)

VALUE_KEYS = frozenset(
    {
        "error",
        "errors",
        "detail",
        "details",
        "message",
        "msg",
        "reason",
        "error_message",
        "description",
    }
)

#: Calls that hand back the exception currently being handled.
EXC_INFO_CALLS = frozenset(
    {"format_exc", "format_exception", "format_exception_only", "exc_info"}
)

#: Calls whose result says nothing about the exception's text.
UNTAINTING_CALLS = frozenset({"type", "isinstance", "issubclass", "id"})


class Sink(NamedTuple):
    file: str
    function: str
    types: str
    line: int
    kind: str
    #: The ``try`` builds a pydantic model and no earlier handler of that
    #: ``try`` catches ``ValidationError`` -- which is a ``ValueError``, so an
    #: ``except ValueError`` there would answer with pydantic's text, and
    #: pydantic's text repeats the input.
    model_in_try: bool = False


# ---------------------------------------------------------------------------
# The analyser
# ---------------------------------------------------------------------------


def _call_name(func: ast.expr) -> str:
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return ""


def _types_of(handler: ast.ExceptHandler) -> List[str]:
    if handler.type is None:
        return ["<bare>"]
    elts = handler.type.elts if isinstance(handler.type, ast.Tuple) else [handler.type]
    out = []
    for e in elts:
        if isinstance(e, ast.Name):
            out.append(e.id)
        elif isinstance(e, ast.Attribute):
            out.append(e.attr)
        else:
            out.append("<expr>")
    return sorted(set(out))


def _is_tainted(node: Optional[ast.AST], tainted: Set[str]) -> bool:
    """Whether ``node`` carries the text of the exception being handled."""
    if node is None:
        return False
    if isinstance(node, ast.Name):
        return node.id in tainted
    if isinstance(node, ast.Call):
        name = _call_name(node.func)
        if name in UNTAINTING_CALLS:
            return False
        if name in EXC_INFO_CALLS:
            return True
    if isinstance(node, (ast.Lambda, ast.FunctionDef, ast.AsyncFunctionDef)):
        return False
    return any(_is_tainted(child, tainted) for child in ast.iter_child_nodes(node))


def _target_names(target: ast.expr) -> Iterator[str]:
    for n in ast.walk(target):
        if isinstance(n, ast.Name):
            yield n.id


def _taint_set(handler: ast.ExceptHandler) -> Set[str]:
    """The bound name plus every name assigned from it in the handler."""
    tainted: Set[str] = {handler.name} if handler.name else set()
    body = ast.Module(body=handler.body, type_ignores=[])
    changed = True
    while changed:
        changed = False
        for node in ast.walk(body):
            pairs: List[Tuple[List[ast.expr], Optional[ast.expr]]] = []
            if isinstance(node, ast.Assign):
                pairs.append((node.targets, node.value))
            elif isinstance(node, (ast.AnnAssign, ast.AugAssign)):
                pairs.append(([node.target], node.value))
            elif isinstance(node, ast.NamedExpr):
                pairs.append(([node.target], node.value))
            elif isinstance(node, (ast.For, ast.AsyncFor)):
                pairs.append(([node.target], node.iter))
            elif isinstance(node, (ast.With, ast.AsyncWith)):
                for item in node.items:
                    if item.optional_vars is not None:
                        pairs.append(([item.optional_vars], item.context_expr))
            for targets, value in pairs:
                if _is_tainted(value, tainted):
                    for t in targets:
                        for name in _target_names(t):
                            if name not in tainted:
                                tainted.add(name)
                                changed = True
            # ``d["error"] = str(e)`` / ``d.update(error=...)`` taint ``d`` too.
            if isinstance(node, ast.Assign):
                for t in node.targets:
                    if isinstance(t, ast.Subscript) and _is_tainted(
                        node.value, tainted
                    ):
                        for name in _target_names(t.value):
                            if name not in tainted:
                                tainted.add(name)
                                changed = True
    return tainted


def _sinks_in(
    handler: ast.ExceptHandler, tainted: Set[str]
) -> Iterator[Tuple[int, str]]:
    body = ast.Module(body=handler.body, type_ignores=[])
    for node in ast.walk(body):
        if isinstance(node, ast.Call):
            name = _call_name(node.func)
            if name in RESPONSE_CALLS:
                for kw in node.keywords:
                    if kw.arg != "status_code" and _is_tainted(kw.value, tainted):
                        yield node.lineno, f"{name}({kw.arg}=)"
                # The first positional argument of HTTPException is the status.
                args = node.args[1:] if name.endswith("Exception") else node.args
                for a in args:
                    if _is_tainted(a, tainted):
                        yield node.lineno, f"{name}(positional)"
                continue
            for kw in node.keywords:
                if kw.arg in VALUE_KEYS and _is_tainted(kw.value, tainted):
                    yield node.lineno, f"keyword {kw.arg}="
        elif isinstance(node, ast.Dict):
            for k, v in zip(node.keys, node.values):
                if (
                    isinstance(k, ast.Constant)
                    and k.value in VALUE_KEYS
                    and _is_tainted(v, tainted)
                ):
                    yield node.lineno, f"dict[{k.value!r}]"
        elif isinstance(node, ast.Assign):
            for t in node.targets:
                if (
                    isinstance(t, ast.Subscript)
                    and isinstance(t.slice, ast.Constant)
                    and t.slice.value in VALUE_KEYS
                    and _is_tainted(node.value, tainted)
                ):
                    yield node.lineno, f"item[{t.slice.value!r}]"


#: Methods that build (and validate) a pydantic model.
MODEL_BUILDERS = frozenset(
    {"model_validate", "model_validate_json", "model_validate_strings", "parse_obj"}
)


def _model_names(tree: ast.Module) -> Set[str]:
    """Names in this file that are (or may be) pydantic models: any class
    name (capitalised) imported from a ``schemas`` module or from pydantic,
    and classes defined here on top of one of those."""
    names: Set[str] = {"BaseModel"}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            if "schemas" in node.module.split(".") or node.module == "pydantic":
                for alias in node.names:
                    if alias.name[:1].isupper():
                        names.add(alias.asname or alias.name)
    changed = True
    while changed:
        changed = False
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef) and node.name not in names:
                if any(_call_name(b) in names for b in node.bases):
                    names.add(node.name)
                    changed = True
    names.discard("ValidationError")
    names.discard("Field")
    return names


def _builds_model(
    stmts: List[ast.stmt],
    models: Set[str],
    helpers: Optional[Dict[str, ast.AST]] = None,
) -> bool:
    """Whether ``stmts`` build a pydantic model -- directly, or through a
    function defined in the same file (one hop: ``_role_to_response(role)``)."""
    for node in ast.walk(ast.Module(body=stmts, type_ignores=[])):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Name) and func.id in models:
            return True
        if isinstance(func, ast.Attribute) and func.attr in MODEL_BUILDERS:
            return True
        if helpers and isinstance(func, ast.Name) and func.id in helpers:
            if _builds_model(helpers[func.id].body, models):
                return True
    return False


def find_sinks(source: str, relpath: str) -> List[Sink]:
    """Every sink in ``source`` whose value derives from a caught exception."""
    tree = ast.parse(source)
    parents: Dict[ast.AST, ast.AST] = {}
    for parent in ast.walk(tree):
        for child in ast.iter_child_nodes(parent):
            parents[child] = parent
    models = _model_names(tree)
    helpers: Dict[str, ast.AST] = {
        node.name: node
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }

    def model_in_try(handler: ast.ExceptHandler) -> bool:
        try_node = parents.get(handler)
        if not isinstance(try_node, ast.Try) or not _builds_model(
            try_node.body, models, helpers
        ):
            return False
        for earlier in try_node.handlers:
            if earlier is handler:
                return True
            if "ValidationError" in _types_of(earlier):
                return False
        return True

    def qualname(node: ast.AST) -> str:
        names = []
        cur = parents.get(node)
        while cur is not None:
            if isinstance(cur, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                names.append(cur.name)
            cur = parents.get(cur)
        return ".".join(reversed(names)) or "<module>"

    # One sink per source line: ``HTTPException(detail={"error": str(e)})`` is
    # one site, not two.  A line inside a nested handler is reported against
    # the innermost handler (ast.walk visits it after the outer one).
    found: Dict[int, Sink] = {}
    for handler in ast.walk(tree):
        if not isinstance(handler, ast.ExceptHandler):
            continue
        tainted = _taint_set(handler)
        types = "|".join(_types_of(handler))
        kinds: Dict[int, Set[str]] = {}
        for line, kind in _sinks_in(handler, tainted):
            kinds.setdefault(line, set()).add(kind)
        for line, ks in kinds.items():
            found[line] = Sink(
                relpath,
                qualname(handler),
                types,
                line,
                " + ".join(sorted(ks)),
                model_in_try(handler),
            )
    return [found[line] for line in sorted(found)]


def _scan_files() -> Iterator[Tuple[str, pathlib.Path]]:
    for root in SCAN_ROOTS:
        base = REPO_ROOT / root
        if not base.is_dir():
            continue
        for path in sorted(base.rglob("*.py")):
            yield path.relative_to(REPO_ROOT).as_posix(), path


def scan_tree() -> List[Sink]:
    sinks: List[Sink] = []
    for rel, path in _scan_files():
        sinks.extend(find_sinks(path.read_text(encoding="utf-8"), rel))
    return sinks


def is_broad(types: str) -> bool:
    return any(t in BROAD_TYPES for t in types.split("|"))


# ---------------------------------------------------------------------------
# The allow-list: typed exceptions whose text the code wrote itself.
# (file, function, exception types) -> (number of sinks, reason)
#
# A ValueError entry also needs the try to build no pydantic model before a
# ValidationError handler (test_no_valueerror_sink_wraps_a_model checks it).
# ---------------------------------------------------------------------------

E = "backend/app/api/v1/endpoints"
M = f"{MODULES_DIR}/backend/app/api/v1/endpoints"

SERVICE_VALUEERROR = (
    "a ValueError the called service raises with its own sentence (not found, "
    "wrong state, a refused value); no pydantic model is built in the try"
)
COGNITO = (
    "Cognito mode only: the auth service's ValueError sentences; local mode "
    "never raises here"
)
LOCKED = "AccountLockedError: the lockout sentence and Retry-After the service wrote"
AUDIT_PAGE = "the audit service's page/limit refusals; ValidationError is caught first"
KEY_NOT_FOUND = "LookupError naming the key the caller sent ('... not found')"
FIELD_NAMES = "ValidationError reduced to the names of the refused fields"
WIZARD_STEP = "WizardStepDataError: the wizard's authored step refusal"
ANALYSIS_CONFIG = "AnalysisConfigError: the authored field name and sentence (422)"
GEMINI = (
    "GeminiError: the Gemini client's own sentences (HTTP status, finish "
    "reason, missing key), pinned by test_llm_experiments_api.py"
)
POWER = "the power calculator's authored range refusals for the caller's numbers"
PASSWORD_RULE = "check_password_strength's fixed sentences; none carries the value"
WIZARD_SUBMIT = (
    "loc and msg of the caller's own wizard payload (no input values); the "
    "payload itself is returned alongside"
)
PATTERN = "PatternUnevaluable's str() is a fixed sentence with no pattern or value"
TIME_WINDOW = "TimeWindowUnreadable's str() is a fixed sentence with no rule or value"
HIPAA_CONFIG = "RuntimeError: the HIPAA service's configuration sentence (503)"
HIPAA_DECRYPT = "ValueError: the HIPAA service's decryption-refused sentence"
RERAISE = "re-raises an HTTPException the route itself raised, unchanged"
VALUES_OMITTED = "keeps only type, loc and msg of each error, never the input"
DOMAIN = "a domain exception whose message the module wrote"
SEGMENT_CHANGE_REFUSED = (
    "SegmentChangeRefused: the audience service's fixed sentence and status "
    "(kind, archived, the member cap with counts), or the segment_in_use "
    "detail (the referencing flags' and experiments' ids, keys and names, "
    "which the caller may read); never a request value"
)

ALLOWED: Dict[Tuple[str, str, str], Tuple[int, str]] = {
    (f"{E}/audit_logs.py", "list_audit_logs", "ValueError"): (1, AUDIT_PAGE),
    (f"{E}/auth.py", "_issue_local_login", "AccountLockedError"): (1, LOCKED),
    (f"{E}/auth.py", "confirm_signup", "ValueError"): (1, COGNITO),
    (f"{E}/auth.py", "forgot_password", "ValueError"): (1, COGNITO),
    (f"{E}/auth.py", "get_user_info", "ValueError"): (1, COGNITO),
    (f"{E}/auth.py", "login", "ValueError"): (1, COGNITO),
    (f"{E}/auth.py", "refresh_token", "ValueError"): (1, COGNITO),
    (f"{E}/auth.py", "reset_password", "ValueError"): (1, COGNITO),
    (f"{E}/auth.py", "signup", "ValueError"): (1, COGNITO),
    (f"{E}/client_errors.py", "report_client_error", "LookupError"): (1, KEY_NOT_FOUND),
    (f"{E}/client_errors.py", "report_client_errors_batch", "LookupError|ValueError"): (
        1,
        KEY_NOT_FOUND,
    ),
    (f"{E}/client_errors.py", "report_client_errors_batch", "ValidationError"): (
        1,
        FIELD_NAMES,
    ),
    (f"{E}/experiment_wizard.py", "update_draft_step", "WizardStepDataError"): (
        1,
        WIZARD_STEP,
    ),
    (f"{E}/experiments.py", "get_daily_experiment_results", "ValueError"): (
        1,
        SERVICE_VALUEERROR,
    ),
    (f"{E}/experiments.py", "get_segmented_experiment_results", "ValueError"): (
        1,
        SERVICE_VALUEERROR,
    ),
    (f"{E}/experiments.py", "update_experiment", "AnalysisConfigError"): (
        2,
        ANALYSIS_CONFIG,
    ),
    (f"{E}/experiments.py", "update_experiment_schedule", "ValueError"): (
        1,
        SERVICE_VALUEERROR,
    ),
    (f"{E}/feature_flags.py", "create_feature_flag", "ValueError"): (
        1,
        SERVICE_VALUEERROR,
    ),
    (f"{E}/feature_flags.py", "update_feature_flag", "ValueError"): (
        1,
        SERVICE_VALUEERROR,
    ),
    (f"{E}/llm_experiments.py", "create_llm_experiment", "ValueError"): (
        1,
        SERVICE_VALUEERROR,
    ),
    (f"{E}/llm_experiments.py", "pause_llm_experiment", "ValueError"): (
        1,
        SERVICE_VALUEERROR,
    ),
    (f"{E}/llm_experiments.py", "start_llm_experiment", "ValueError"): (
        1,
        SERVICE_VALUEERROR,
    ),
    (f"{E}/llm_experiments.py", "update_llm_experiment", "ValueError"): (
        1,
        SERVICE_VALUEERROR,
    ),
    (f"{E}/llm_proxy.py", "get_llm_results", "ValueError"): (1, SERVICE_VALUEERROR),
    (f"{E}/llm_proxy.py", "llm_complete", "GeminiError"): (1, GEMINI),
    (f"{E}/llm_proxy.py", "llm_complete", "ValueError"): (1, SERVICE_VALUEERROR),
    (f"{E}/mutual_exclusion_groups.py", "add_experiment_to_group", "ValueError"): (
        1,
        SERVICE_VALUEERROR,
    ),
    (f"{E}/mutual_exclusion_groups.py", "remove_experiment_from_group", "ValueError"): (
        1,
        SERVICE_VALUEERROR,
    ),
    (f"{E}/power_calculator.py", "compute_mde", "ValueError"): (1, POWER),
    (f"{E}/power_calculator.py", "compute_runtime", "ValueError"): (1, POWER),
    (f"{E}/power_calculator.py", "compute_sample_size", "ValueError"): (1, POWER),
    (f"{E}/power_calculator.py", "get_power_curve", "ValueError"): (1, POWER),
    (f"{E}/rollout_schedules.py", "activate_rollout_schedule", "ValueError"): (
        1,
        SERVICE_VALUEERROR,
    ),
    (f"{E}/rollout_schedules.py", "add_rollout_stage", "ValueError"): (
        1,
        SERVICE_VALUEERROR,
    ),
    (f"{E}/rollout_schedules.py", "cancel_rollout_schedule", "ValueError"): (
        1,
        SERVICE_VALUEERROR,
    ),
    (f"{E}/rollout_schedules.py", "create_rollout_schedule", "ValueError"): (
        1,
        SERVICE_VALUEERROR,
    ),
    (f"{E}/rollout_schedules.py", "delete_rollout_schedule", "ValueError"): (
        1,
        SERVICE_VALUEERROR,
    ),
    (f"{E}/rollout_schedules.py", "delete_rollout_stage", "ValueError"): (
        1,
        SERVICE_VALUEERROR,
    ),
    (f"{E}/rollout_schedules.py", "manually_advance_stage", "ValueError"): (
        1,
        SERVICE_VALUEERROR,
    ),
    (f"{E}/rollout_schedules.py", "pause_rollout_schedule", "ValueError"): (
        1,
        SERVICE_VALUEERROR,
    ),
    (f"{E}/rollout_schedules.py", "update_rollout_schedule", "ValueError"): (
        1,
        SERVICE_VALUEERROR,
    ),
    (f"{E}/rollout_schedules.py", "update_rollout_stage", "ValueError"): (
        1,
        SERVICE_VALUEERROR,
    ),
    # The segment's audit snapshot before an update or archive: the same
    # ``AudienceService.get_segment`` "not found" text the routes answered.
    (f"{E}/segments.py", "_segment_before", "ValueError"): (1, SERVICE_VALUEERROR),
    # #440: the member routes and update. ``SegmentChangeRefused.message`` is
    # fixed text the audience service writes (counts at most, never an id or
    # any other request value); ValueError is the service's "not found".
    (f"{E}/segments.py", "add_segment_members", "SegmentChangeRefused"): (
        1,
        SEGMENT_CHANGE_REFUSED,
    ),
    (f"{E}/segments.py", "add_segment_members", "ValueError"): (
        1,
        SERVICE_VALUEERROR,
    ),
    (f"{E}/segments.py", "remove_segment_members", "SegmentChangeRefused"): (
        1,
        SEGMENT_CHANGE_REFUSED,
    ),
    (f"{E}/segments.py", "remove_segment_members", "ValueError"): (
        1,
        SERVICE_VALUEERROR,
    ),
    (f"{E}/segments.py", "update_segment", "SegmentChangeRefused"): (
        1,
        SEGMENT_CHANGE_REFUSED,
    ),
    (f"{E}/segments.py", "delete_segment", "SegmentChangeRefused"): (
        1,
        SEGMENT_CHANGE_REFUSED,
    ),
    (f"{E}/segments.py", "delete_segment", "ValueError"): (1, SERVICE_VALUEERROR),
    (f"{E}/segments.py", "evaluate_segment_membership", "ValueError"): (
        1,
        SERVICE_VALUEERROR,
    ),
    (f"{E}/segments.py", "get_segment", "ValueError"): (1, SERVICE_VALUEERROR),
    (f"{E}/segments.py", "get_segment_experiments", "ValueError"): (
        1,
        SERVICE_VALUEERROR,
    ),
    (f"{E}/segments.py", "preview_audience_size", "ValueError"): (
        1,
        SERVICE_VALUEERROR,
    ),
    (f"{E}/segments.py", "update_segment", "ValueError"): (1, SERVICE_VALUEERROR),
    (f"{E}/tracking.py", "assign_user_to_experiment", "ValueError"): (
        1,
        SERVICE_VALUEERROR,
    ),
    (f"{E}/tracking.py", "track_event", "ValidationError"): (1, FIELD_NAMES),
    (f"{E}/tracking.py", "track_event", "ValueError"): (1, SERVICE_VALUEERROR),
    (f"{E}/tracking.py", "track_event_by_ids", "ValidationError"): (1, FIELD_NAMES),
    (f"{E}/tracking.py", "track_event_by_ids", "ValueError"): (1, SERVICE_VALUEERROR),
    (f"{E}/tracking.py", "track_events_batch", "ValidationError"): (1, FIELD_NAMES),
    (f"{E}/tracking.py", "track_events_batch", "ValueError"): (1, SERVICE_VALUEERROR),
    (f"{E}/users.py", "apply_password_change", "ValueError"): (1, PASSWORD_RULE),
    (f"{E}/users.py", "change_own_password", "AccountLockedError"): (1, LOCKED),
    (
        "backend/app/services/experiment_wizard_service.py",
        "ExperimentWizardService.validate_and_submit",
        "ValidationError",
    ): (1, WIZARD_SUBMIT),
    (
        "backend/app/services/rules_evaluation_service.py",
        "RulesEvaluationService.evaluate",
        "PatternUnevaluable",
    ): (1, PATTERN),
    (
        "backend/app/services/rules_evaluation_service.py",
        "RulesEvaluationService.evaluate_rules_with_validation",
        "PatternUnevaluable",
    ): (1, PATTERN),
    (
        "backend/app/services/rules_evaluation_service.py",
        "RulesEvaluationService.evaluate",
        "TimeWindowUnreadable",
    ): (1, TIME_WINDOW),
    (
        "backend/app/services/rules_evaluation_service.py",
        "RulesEvaluationService.evaluate_rules_with_validation",
        "TimeWindowUnreadable",
    ): (1, TIME_WINDOW),
    (f"{M}/hipaa.py", "decrypt_phi", "RuntimeError"): (1, HIPAA_CONFIG),
    (f"{M}/hipaa.py", "decrypt_phi", "ValueError"): (1, HIPAA_DECRYPT),
    (f"{M}/hipaa.py", "encrypt_phi", "RuntimeError"): (1, HIPAA_CONFIG),
    (f"{M}/rbac.py", "assign_role", "ValueError"): (1, SERVICE_VALUEERROR),
    (f"{M}/rbac.py", "create_role", "ValueError"): (1, SERVICE_VALUEERROR),
    (f"{M}/rbac.py", "delete_role", "ValueError"): (1, SERVICE_VALUEERROR),
    (f"{M}/rbac.py", "revoke_role", "ValueError"): (1, SERVICE_VALUEERROR),
    (f"{M}/rbac.py", "update_role", "ValueError"): (1, SERVICE_VALUEERROR),
    (f"{M}/sso.py", "oidc_callback", "HTTPException"): (1, RERAISE),
    (
        f"{M}/warehouse_analysis.py",
        "_ValuesOmittedRoute.get_route_handler.handler",
        "RequestValidationError",
    ): (1, VALUES_OMITTED),
    (f"{M}/warehouse_analysis.py", "_service_account", "BigQueryConfigRefused"): (
        1,
        DOMAIN,
    ),
    (f"{M}/warehouse_analysis.py", "require_enabled", "ConnectorDisabled"): (1, DOMAIN),
    (f"{M}/workspaces.py", "_get_workspace_or_404", "WorkspaceNotFound"): (1, DOMAIN),
    (f"{M}/workspaces.py", "accept_invite", "AlreadyMember"): (1, DOMAIN),
    (f"{M}/workspaces.py", "accept_invite", "InviteAlreadyAccepted"): (1, DOMAIN),
    (f"{M}/workspaces.py", "accept_invite", "InviteExpired"): (1, DOMAIN),
    (f"{M}/workspaces.py", "accept_invite", "InviteNotFound"): (1, DOMAIN),
    (f"{M}/workspaces.py", "add_member", "AlreadyMember"): (1, DOMAIN),
    (f"{M}/workspaces.py", "create_workspace", "WorkspaceSlugInvalid"): (1, DOMAIN),
    (f"{M}/workspaces.py", "create_workspace", "WorkspaceSlugTaken"): (1, DOMAIN),
    (f"{M}/workspaces.py", "get_invite", "InviteNotFound"): (1, DOMAIN),
    (f"{M}/workspaces.py", "remove_member", "CannotRemoveLastOwner"): (1, DOMAIN),
    (f"{M}/workspaces.py", "remove_member", "WorkspaceMemberNotFound"): (1, DOMAIN),
    (f"{M}/workspaces.py", "update_member_role", "CannotDemoteLastOwner"): (1, DOMAIN),
    (f"{M}/workspaces.py", "update_member_role", "WorkspaceMemberNotFound"): (
        1,
        DOMAIN,
    ),
}


# ---------------------------------------------------------------------------
# The gate
# ---------------------------------------------------------------------------


def _present(path: str) -> bool:
    """Whether an allow-list entry's file is part of this tree."""
    return not path.startswith(f"{MODULES_DIR}/") or (REPO_ROOT / MODULES_DIR).is_dir()


@pytest.fixture(scope="module")
def sinks() -> List[Sink]:
    return scan_tree()


def _show(items) -> str:
    return "\n".join(
        f"  {s.file}:{s.line} {s.function} [{s.types}] {s.kind}" for s in items
    )


def test_the_scan_is_not_vacuous(sinks: List[Sink]) -> None:
    files = [rel for rel, _ in _scan_files()]
    assert sum(f.startswith("backend/app/") for f in files) > 200, files[:5]
    if (REPO_ROOT / MODULES_DIR).is_dir():
        assert any(f.startswith(f"{MODULES_DIR}/backend/app/") for f in files)
    # The typed 4xx sites below exist in every build of this tree.
    assert any(s.file.endswith("/rollout_schedules.py") for s in sinks)


def test_no_broad_exception_reaches_a_response(sinks: List[Sink]) -> None:
    broad = [s for s in sinks if is_broad(s.types)]
    assert not broad, (
        "A broad exception's text reaches a response value. Answer "
        "unexpected_failure(...) / failure_detail(<fixed sentence>) instead:\n"
        + _show(broad)
    )


def test_the_allow_list_names_no_broad_type() -> None:
    bad = [key for key in ALLOWED if is_broad(key[2])]
    assert not bad, bad


def test_every_typed_site_is_classified_exactly(sinks: List[Sink]) -> None:
    found = Counter(
        (s.file, s.function, s.types) for s in sinks if not is_broad(s.types)
    )
    expected = {k: v[0] for k, v in ALLOWED.items() if _present(k[0])}
    new = sorted(k for k in found if k not in expected)
    stale = sorted(k for k in expected if k not in found)
    moved = sorted(
        (k, expected[k], found[k])
        for k in found
        if k in expected and found[k] != expected[k]
    )
    problems = []
    if new:
        problems.append(
            "Unclassified: an exception's text reaches a response. Answer a fixed "
            "sentence, or (typed, authored text only) add an ALLOWED entry with "
            "the reason:\n"
            + _show(s for s in sinks if (s.file, s.function, s.types) in set(new))
        )
    if stale:
        problems.append(f"Stale ALLOWED entries (nothing matches): {stale}")
    if moved:
        problems.append(f"Sink count changed (key, allowed, found): {moved}")
    assert not problems, "\n\n".join(problems)


def test_no_valueerror_sink_wraps_a_model(sinks: List[Sink]) -> None:
    """``ValidationError`` is a ``ValueError``: an ``except ValueError`` around
    a model build answers pydantic's text, which repeats the input."""
    bad = [s for s in sinks if "ValueError" in s.types.split("|") and s.model_in_try]
    assert not bad, (
        "Catch pydantic.ValidationError before ValueError here, with a fixed "
        "answer:\n" + _show(bad)
    )


def test_the_reasons_are_written() -> None:
    assert all(len(reason) > 20 for _, reason in ALLOWED.values())


# ---------------------------------------------------------------------------
# The analyser on planted code (no files, no git: runs in any tree)
# ---------------------------------------------------------------------------

FLAGGED = {
    "str": "raise HTTPException(500, detail=str(e))",
    "fstring": 'raise HTTPException(status_code=500, detail=f"failed: {e}")',
    "fstring_conversion": 'raise HTTPException(500, detail=f"failed: {e!s}")',
    "repr": "raise HTTPException(500, detail=repr(e))",
    "args": "raise HTTPException(500, detail=e.args[0])",
    "orig": "raise HTTPException(500, detail=str(e.orig))",
    "one_hop": "msg = str(e)\n    raise HTTPException(500, detail=msg)",
    "two_hops": "a = e.args\n    b = a[0]\n    raise HTTPException(500, detail=b)",
    "json_response": 'return JSONResponse(status_code=500, content={"error": str(e)})',
    "dict_key": 'return {"status": "failed", "message": str(e)}',
    "keyword": "results.append(Result(ok=False, error=str(e)))",
    "item": 'body = {}\n    body["detail"] = str(e)\n    return body',
    "format_exc": "raise HTTPException(500, detail=traceback.format_exc())",
    "positional": "raise HTTPException(500, str(e))",
}

NOT_FLAGGED = {
    "fixed": 'raise HTTPException(500, detail=failure_detail("Could not save"))',
    "type_name": "raise HTTPException(500, detail=type(e).__name__)",
    "helper": 'raise unexpected_failure(e, "Save", "Could not save", db=db)',
    "logged_only": 'logger.error("save failed: %s", e)\n    raise HTTPException(500, detail="x")',
    "status_only": "raise HTTPException(status_code=e.status_code, detail='Refused')",
}


def _wrap(body: str, exc: str = "Exception") -> str:
    return textwrap.dedent(
        f"""
        def route(db):
            try:
                work()
            except {exc} as e:
                {body.replace(chr(10) + "    ", chr(10) + "                ")}
        """
    )


@pytest.mark.parametrize("name", sorted(FLAGGED))
def test_the_analyser_flags(name: str) -> None:
    found = find_sinks(_wrap(FLAGGED[name]), "x.py")
    assert found and all(is_broad(s.types) for s in found), name


@pytest.mark.parametrize("name", sorted(NOT_FLAGGED))
def test_the_analyser_passes(name: str) -> None:
    assert find_sinks(_wrap(NOT_FLAGGED[name]), "x.py") == [], name


def test_the_analyser_reports_the_function_and_types() -> None:
    (sink,) = find_sinks(
        _wrap("raise HTTPException(400, detail=str(e))", "(KeyError, ValueError)"),
        "x.py",
    )
    assert (sink.function, sink.types) == ("route", "KeyError|ValueError")


def test_the_analyser_sees_a_model_built_in_the_try() -> None:
    source = textwrap.dedent(
        """
        from backend.app.schemas.thing import ThingResponse

        def _to_response(row):
            return ThingResponse(id=row.id)

        def direct(row):
            try:
                return ThingResponse(id=row.id)
            except ValueError as e:
                raise HTTPException(400, detail=str(e))

        def one_hop(row):
            try:
                return _to_response(row)
            except ValueError as e:
                raise HTTPException(400, detail=str(e))

        def guarded(row):
            try:
                return ThingResponse(id=row.id)
            except ValidationError:
                raise HTTPException(500, detail="x")
            except ValueError as e:
                raise HTTPException(400, detail=str(e))

        def no_model(row):
            try:
                service.do(row)
            except ValueError as e:
                raise HTTPException(400, detail=str(e))
        """
    )
    flags = {s.function: s.model_in_try for s in find_sinks(source, "x.py")}
    assert flags == {
        "direct": True,
        "one_hop": True,
        "guarded": False,
        "no_model": False,
    }
