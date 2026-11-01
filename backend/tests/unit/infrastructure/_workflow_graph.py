"""Read the workflows the way GitHub turns them into check names.

Shared by ``test_required_check_producers.py`` and ``test_ci_aggregators.py``.
Every YAML file is loaded with the duplicate-key-refusing loader from
``test_docs_only_gate.py``: ``safe_load`` keeps the last of two ``name:`` or
``if:`` keys silently, which would let a test read one value while GitHub
reads the other.

Anything this module cannot model -- a negated branch filter, a matrix on a
reusable-workflow caller -- raises rather than guessing, so a new construct
fails the tests until it is understood.
"""

from __future__ import annotations

import itertools
import re
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, List, Optional, Set, Tuple

from backend.tests.unit.infrastructure.test_docs_only_gate import _load

REPO_ROOT = Path(__file__).resolve().parents[4]
WORKFLOWS = REPO_ROOT / ".github" / "workflows"
CONFIGURE_REPO = REPO_ROOT / "scripts" / "configure-repo.sh"

#: The branch a same-repository pull request's head commit is pushed to. Any
#: name that is not `main` would do; it has a slash so `*` and `**` differ.
PR_HEAD_BRANCH = "feature/topic"


def required_checks(path: Path = CONFIGURE_REPO) -> List[str]:
    """The ``REQUIRED_CHECKS=( ... )`` array in ``scripts/configure-repo.sh``."""
    text = path.read_text(encoding="utf-8")
    match = re.search(r"^REQUIRED_CHECKS=\(\n(.*?)^\)", text, re.S | re.M)
    assert match, f"no REQUIRED_CHECKS=( ... ) array in {path}"
    names = re.findall(r'^\s*"([^"]+)"\s*$', match.group(1), re.M)
    body_lines = [
        line
        for line in match.group(1).splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]
    assert len(names) == len(body_lines), (
        f"unparsed REQUIRED_CHECKS lines: {body_lines}"
    )
    return names


def load(path: Path) -> Dict[str, Any]:
    return _load(path)


def triggers(workflow: Dict[str, Any]) -> Dict[str, Any]:
    # The bare key `on` is the boolean True to a YAML 1.1 loader; a quoted
    # "on" is the string. GitHub reads both as `on`, so a workflow with both
    # is refused rather than read one way (load() refuses it too; this covers
    # a mapping that came from another loader).
    if "on" in workflow and any(key is True for key in workflow):
        raise ValueError("both forms of the trigger key (bare and quoted `on`)")
    on = workflow.get("on", workflow.get(True))
    if isinstance(on, str):
        return {on: None}
    if isinstance(on, list):
        return dict.fromkeys(on)
    return on or {}


# --------------------------------------------------------------------------
# Expressions: the subset job names use
# --------------------------------------------------------------------------


class Unresolved:
    """A value only known at run time (a dynamic matrix, `github.ref`)."""

    def __init__(self, what: str) -> None:
        self.what = what

    def __repr__(self) -> str:
        return f"<unresolved {self.what}>"


_TOKEN = re.compile(
    r"\s*(?:(==|!=|&&|\|\||!|\(|\))|'((?:[^']|'')*)'|([A-Za-z_][\w.\-]*))"
)


def _tokens(expr: str) -> List[Tuple[str, str]]:
    out: List[Tuple[str, str]] = []
    pos = 0
    expr = expr.strip()
    while pos < len(expr):
        m = _TOKEN.match(expr, pos)
        if not m or m.end() == pos:
            raise ValueError(f"cannot read expression {expr!r} at {expr[pos:]!r}")
        op, string, ident = m.groups()
        if op:
            out.append(("op", op))
        elif string is not None:
            out.append(("str", string.replace("''", "'")))
        else:
            out.append(("id", ident))
        pos = m.end()
    return out


def _truthy(value: Any) -> bool:
    if isinstance(value, Unresolved):
        raise _Undecidable(value)
    return value not in (None, False, "", 0)


class _Undecidable(Exception):
    def __init__(self, value: Unresolved) -> None:
        self.value = value


def evaluate(
    expr: str, context: Dict[str, Any], null_under: Tuple[str, ...] = ()
) -> Any:
    """Evaluate ``==``, ``!=``, ``&&``, ``||``, ``!``, parentheses, literals and
    dotted context lookups, with GitHub's short-circuit semantics (``a && b``
    is ``b`` when ``a`` is truthy, else ``a``).

    A lookup that is not in ``context`` is Unresolved, unless it starts with
    one of the ``null_under`` prefixes: there it is null, which is what GitHub
    gives for a property the payload does not have (``github.event.pull_request
    .numbr`` is null on GitHub, not an error)."""
    toks = _tokens(expr)
    pos = 0

    def peek() -> Optional[Tuple[str, str]]:
        return toks[pos] if pos < len(toks) else None

    def take() -> Tuple[str, str]:
        nonlocal pos
        tok = toks[pos]
        pos += 1
        return tok

    def primary() -> Any:
        kind, value = take()
        if kind == "str":
            return value
        if kind == "id":
            if value in ("true", "false"):
                return value == "true"
            if value == "null":
                return None
            if peek() == ("op", "("):
                raise ValueError(f"function call {value}() is not modelled in {expr!r}")
            cur: Any = context
            for part in value.split("."):
                if isinstance(cur, dict) and part in cur:
                    cur = cur[part]
                else:
                    if any(value.startswith(p) for p in null_under):
                        return None
                    return Unresolved(value)
            return cur
        if (kind, value) == ("op", "("):
            inner = disjunction()
            assert take() == ("op", ")"), f"unbalanced parentheses in {expr!r}"
            return inner
        if (kind, value) == ("op", "!"):
            operand = primary()
            try:
                return not _truthy(operand)
            except _Undecidable as exc:
                return exc.value
        raise ValueError(f"unexpected {value!r} in {expr!r}")

    def comparison() -> Any:
        left = primary()
        while peek() in (("op", "=="), ("op", "!=")):
            op = take()[1]
            right = primary()
            if isinstance(left, Unresolved) or isinstance(right, Unresolved):
                left = left if isinstance(left, Unresolved) else right
                continue
            # GitHub compares strings case-insensitively.
            lv = left.lower() if isinstance(left, str) else left
            rv = right.lower() if isinstance(right, str) else right
            left = (lv == rv) if op == "==" else (lv != rv)
        return left

    def conjunction() -> Any:
        left = comparison()
        while peek() == ("op", "&&"):
            take()
            right = comparison()
            try:
                left = right if _truthy(left) else left
            except _Undecidable as exc:
                left = exc.value
        return left

    def disjunction() -> Any:
        left = conjunction()
        while peek() == ("op", "||"):
            take()
            right = conjunction()
            try:
                left = left if _truthy(left) else right
            except _Undecidable as exc:
                left = exc.value
        return left

    result = disjunction()
    assert pos == len(toks), f"trailing tokens in {expr!r}"
    return result


_TEMPLATE = re.compile(r"\$\{\{(.*?)\}\}", re.S)


def render(
    template: str, context: Dict[str, Any], null_under: Tuple[str, ...] = ()
) -> Any:
    """A ``name:`` with every ``${{ }}`` evaluated; an Unresolved if any part is."""
    parts: List[str] = []
    last = 0
    for m in _TEMPLATE.finditer(template):
        parts.append(template[last : m.start()])
        value = evaluate(m.group(1), context, null_under)
        if isinstance(value, Unresolved):
            return Unresolved(f"{template!r} ({value.what})")
        parts.append(
            ""
            if value is None
            else str(value).lower()
            if isinstance(value, bool)
            else str(value)
        )
        last = m.end()
    parts.append(template[last:])
    return "".join(parts)


# --------------------------------------------------------------------------
# Events: which workflows run, for which event, on a pull request's head
# --------------------------------------------------------------------------


def _glob(pattern: str) -> "re.Pattern[str]":
    if pattern.startswith("!"):
        raise ValueError(f"negated branch filter {pattern!r} is not modelled")
    out = ""
    i = 0
    while i < len(pattern):
        if pattern.startswith("**", i):
            out += ".*"
            i += 2
        elif pattern[i] == "*":
            out += "[^/]*"
            i += 1
        elif pattern[i] == "?":
            out += "."
            i += 1
        else:
            out += re.escape(pattern[i])
            i += 1
    return re.compile(out + r"\Z")


def _branch_matches(filters: Dict[str, Any], branch: str, *, push: bool) -> bool:
    branches = filters.get("branches")
    ignore = filters.get("branches-ignore")
    if branches is None and ignore is None:
        # A push filtered to tags alone never fires for a branch.
        return not (push and ("tags" in filters or "tags-ignore" in filters))
    if branches is not None:
        return any(_glob(p).match(branch) for p in branches)
    return not any(_glob(p).match(branch) for p in ignore)


#: The events whose check runs land on a pull request's head commit, and the
#: branch each one's filter is matched against: a pull request event filters on
#: its BASE (main), a push on the branch pushed (the head branch).
HEAD_COMMIT_EVENTS = {
    "pull_request": "main",
    "pull_request_target": "main",
    "push": PR_HEAD_BRANCH,
}


def fires(workflow: Dict[str, Any], event: str) -> bool:
    on = triggers(workflow)
    if event not in on:
        return False
    filters = on[event] or {}
    return _branch_matches(filters, HEAD_COMMIT_EVENTS[event], push=event == "push")


# --------------------------------------------------------------------------
# Check names
# --------------------------------------------------------------------------


def _matrix_combos(job: Dict[str, Any]) -> List[Optional[Dict[str, Any]]]:
    matrix = (job.get("strategy") or {}).get("matrix")
    if matrix is None:
        return [None]
    if isinstance(matrix, str):
        return [{"__dynamic__": matrix}]
    keys = [k for k in matrix if k not in ("include", "exclude")]
    combos: List[Dict[str, Any]] = (
        [
            dict(zip(keys, values))
            for values in itertools.product(*(matrix[k] for k in keys))
        ]
        if keys
        else []
    )
    for excluded in matrix.get("exclude") or []:
        combos = [
            c for c in combos if not all(c.get(k) == v for k, v in excluded.items())
        ]
    # An `include` entry extends each ORIGINAL combination whose values it
    # does not overwrite; one that extends none becomes a combination of its
    # own. Combinations added by `include` are never extended.
    originals = list(combos)
    for extra in matrix.get("include") or []:
        matched = False
        for combo in originals:
            if all(combo.get(k) == v for k, v in extra.items() if k in keys):
                combo.update({k: v for k, v in extra.items() if k not in keys})
                matched = True
        if not matched:
            combos.append(dict(extra))
    return combos  # type: ignore[return-value]


def _job_name(
    job_id: str,
    job: Dict[str, Any],
    context: Dict[str, Any],
    combo: Optional[Dict[str, Any]],
) -> Any:
    ctx = dict(context)
    if combo is not None and "__dynamic__" in combo:
        ctx["matrix"] = {}
    elif combo is not None:
        ctx["matrix"] = combo
    template = job.get("name")
    if template is None:
        if combo is None:
            return job_id
        if "__dynamic__" in combo:
            return Unresolved(f"{job_id} dynamic matrix")
        return f"{job_id} ({', '.join(str(v) for v in combo.values())})"
    name = render(str(template), ctx)
    if (
        combo is not None
        and "matrix." not in str(template)
        and not isinstance(name, Unresolved)
    ):
        if "__dynamic__" in combo:
            return Unresolved(f"{name} dynamic matrix suffix")
        # GitHub appends the matrix values to a name that does not use them.
        name = f"{name} ({', '.join(str(v) for v in combo.values())})"
    return name


def check_names(workflow_path: Path, event: str) -> Iterator[Tuple[str, Any]]:
    """(job id, check name) for every check run a workflow produces for `event`.

    A job's own ``if:`` is deliberately ignored: a job whose condition is false
    still reports, as skipped, under its name -- and protection accepts a
    skipped check. So it is a producer.
    """
    workflow = load(workflow_path)
    context = {"github": {"event_name": event}}
    for job_id, job in (workflow.get("jobs") or {}).items():
        uses = job.get("uses")
        if uses and uses.startswith("./"):
            if (job.get("strategy") or {}).get("matrix") is not None:
                raise ValueError(
                    f"{workflow_path.name}:{job_id}: a matrix on a reusable caller is not modelled"
                )
            caller = _job_name(job_id, job, context, None)
            callee = load(REPO_ROOT / uses)
            inputs = {
                k: (spec or {}).get("default")
                for k, spec in (
                    (triggers(callee).get("workflow_call") or {}).get("inputs") or {}
                ).items()
            }
            inputs.update(job.get("with") or {})
            callee_context = {"github": {"event_name": event}, "inputs": inputs}
            for callee_id, callee_job in (callee.get("jobs") or {}).items():
                for combo in _matrix_combos(callee_job):
                    inner = _job_name(callee_id, callee_job, callee_context, combo)
                    if isinstance(caller, Unresolved) or isinstance(inner, Unresolved):
                        yield job_id, Unresolved(f"{caller} / {inner}")
                    else:
                        yield job_id, f"{caller} / {inner}"
            continue
        for combo in _matrix_combos(job):
            yield job_id, _job_name(job_id, job, context, combo)


def workflow_files() -> List[Path]:
    return sorted(p for p in WORKFLOWS.glob("*.y*ml") if p.is_file())


Producer = Tuple[str, str, str]  # (event, workflow file, job id)


def head_commit_producers(
    paths: Optional[List[Path]] = None,
) -> Tuple[Dict[str, List[Producer]], List[Producer]]:
    """On a pull request's head commit: check name -> producers, and the
    producers whose name is only known at run time (a dynamic matrix)."""
    producers: Dict[str, List[Producer]] = {}
    unresolved: List[Producer] = []
    for path in workflow_files() if paths is None else paths:
        workflow = load(path)
        for event in HEAD_COMMIT_EVENTS:
            if not fires(workflow, event):
                continue
            for job_id, name in check_names(path, event):
                if isinstance(name, Unresolved):
                    unresolved.append((event, path.name, job_id))
                else:
                    producers.setdefault(name, []).append((event, path.name, job_id))
    return producers, unresolved


# --------------------------------------------------------------------------
# Summary jobs: found by the check name they report
# --------------------------------------------------------------------------

#: An `if:` under which a job runs after one of its needs failed. GitHub's
#: expression functions are case-insensitive (`ALWAYS()` is `always()`), so
#: both patterns are too.
RUNS_AFTER_FAILURE = re.compile(r"always\(\)|!\s*cancelled\(\)", re.I)

#: Any `if:` that CAN run a job after a failed need: one that calls a status
#: function other than a plain ``success()`` -- ``failure()``, ``cancelled()``
#: (``!cancelled()`` included), ``always()`` -- or negates ``success()``.
#: Wider than RUNS_AFTER_FAILURE on purpose: R3 reads this one, so a job on
#: ``success() || failure()`` is not a summary that escaped every check.
CAN_RUN_AFTER_FAILURE = re.compile(
    r"\b(?:always|failure|cancelled)\s*\(\s*\)|!\s*success\s*\(\s*\)", re.I
)

#: The only `if:` a required summary may have. Anything added to it (a
#: docs-only lane, say) can make it false, and a skipped required check is
#: accepted as passing.
SUMMARY_IF = ("always()", "${{ always() }}")

#: (workflow file name, job id) -> True when that job is a classified work job
#: (test_ci_classification.CLASSIFIED) that needs the classifier alone.
ClassifiedWork = Callable[[str, str, Dict[str, Any]], bool]

#: (workflow file name, job id, job, every job of that workflow)
Summary = Tuple[str, str, Dict[str, Any], Dict[str, Any]]


def needs_of(job: Dict[str, Any]) -> List[str]:
    needs = job.get("needs") or []
    return [needs] if isinstance(needs, str) else list(needs)


def reported_names(path: Path) -> Dict[str, Set[str]]:
    """job id -> every check name that job reports, for any head-commit event.

    A job with no ``name:`` reports under its id (``integration-tests``), so
    reading ``job["name"]`` misses it; this reads what GitHub shows.
    """
    names: Dict[str, Set[str]] = {}
    for event in HEAD_COMMIT_EVENTS:
        for job_id, name in check_names(path, event):
            if not isinstance(name, Unresolved):
                names.setdefault(job_id, set()).add(name)
    return names


def select_summaries(
    paths: List[Path], required: List[str], classified_work: ClassifiedWork
) -> List[Summary]:
    """Every job that reports a required check name, has needs, runs after a
    failed need, and is not a classified work job (R1)."""
    wanted = set(required)
    found: List[Summary] = []
    for path in paths:
        jobs = load(path).get("jobs") or {}
        names = reported_names(path)
        for job_id, job in jobs.items():
            if not needs_of(job) or not RUNS_AFTER_FAILURE.search(
                str(job.get("if", ""))
            ):
                continue
            if classified_work(path.name, job_id, job):
                continue
            if names.get(job_id, set()) & wanted:
                found.append((path.name, job_id, job, jobs))
    return found


def unguarded_required_names(
    paths: List[Path], required: List[str], classified_work: ClassifiedWork
) -> List[str]:
    """R2: a required name whose producer has needs, and is neither a summary
    that runs on exactly ``always()`` nor a classified job that needs only the
    classifier. If a job it needs fails, such a producer is skipped -- and
    branch protection accepts a skipped check. Empty when every one is sound."""
    by_name = {p.name: p for p in paths}
    selected = {
        (w, j) for w, j, _, _ in select_summaries(paths, required, classified_work)
    }
    producers, _ = head_commit_producers(paths)
    problems = []
    for name in required:
        for _event, workflow, job_id in producers.get(name, []):
            job = (load(by_name[workflow]).get("jobs") or {})[job_id]
            needs = needs_of(job)
            if not needs:
                continue
            if (workflow, job_id) in selected and job.get("if") in SUMMARY_IF:
                continue
            if classified_work(workflow, job_id, job):
                continue
            problems.append(
                f"{name!r} ({workflow}:{job_id}) needs {needs} with if: "
                f"{job.get('if')!r}; it must be a summary whose if: is exactly "
                "always(), or a classified job that needs only changes "
                "(a skipped required check is green)"
            )
    return problems


def unselected_runs_after_failure(
    path: Path, required: List[str], classified_work: ClassifiedWork
) -> List[str]:
    """R3: in `path`, a job whose `if:` can run it after a failed need
    (CAN_RUN_AFTER_FAILURE) and that needs more than the classifier must be a
    summary R1 selects, so that it carries the summary checks (toJSON(needs),
    closed needs). Empty when sound."""
    selected = {j for _, j, _, _ in select_summaries([path], required, classified_work)}
    problems = []
    for job_id, job in (load(path).get("jobs") or {}).items():
        if not CAN_RUN_AFTER_FAILURE.search(str(job.get("if", ""))):
            continue
        extra = sorted(set(needs_of(job)) - {"changes"})
        if extra and job_id not in selected:
            problems.append(
                f"{path.name}:{job_id} runs after a failed need (if: "
                f"{job.get('if')!r}) and needs {extra} beyond changes, but is "
                "not a required summary"
            )
    return problems


def r3_workflows(
    paths: List[Path], required: List[str], classified_work: ClassifiedWork
) -> List[Path]:
    """The workflows R3 applies to: every one with a classifier (a ``changes``
    job) or a summary R1 selects. In the others a job after a failed need is
    a notifier (``nightly-qa``'s, ``chart-kind``'s failure issues), not a
    stand-in for a required check."""
    out = []
    for path in paths:
        jobs = load(path).get("jobs") or {}
        if "changes" in jobs or select_summaries([path], required, classified_work):
            out.append(path)
    return out
