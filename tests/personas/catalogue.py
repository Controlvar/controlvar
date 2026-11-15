"""The persona catalogue and the UI/API role matrix, read and checked statically.

Two kinds of file live beside this module:

* ``personas/<name>.yaml``: one persona's card (who, which account, and the
  steps of the day with the state oracle that decides each step);
* ``role-matrix.yaml``: per role and action, what the dashboard offers (``ui``)
  and what the API answers (``api``), each against its own documented cell.

Nothing here runs a browser, starts a server or reads a network. Loading a
file refuses, by name, every shape the checks in
``backend/tests/unit/personas/`` plant: a step with no oracle, a write
decided only by a screen, a number decided only by the page it came from, a
source that does not exist, a matrix row that is not an API operation.

The catalogue carries no URL field: the base URL belongs to the job that runs
a day, never to a card.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator

#: The eight kinds of oracle a step may name; nothing else is a check.
ORACLE_KINDS: tuple[str, ...] = (
    "api_state",
    "api_refused",
    "ui_matches_api",
    "count_delta",
    "hash_vector",
    "audit_entry",
    "text_exact",
    "network_allowlist",
)

#: What decides a write. A step that writes needs at least one of these;
#: ``text_exact`` and ``network_allowlist`` are additional, never alone.
STATE_ORACLE_KINDS: frozenset[str] = frozenset(
    {"api_state", "count_delta", "audit_entry", "hash_vector", "api_refused"}
)

#: The one page a documented UI/API divergence may be anchored in.
DIVERGENCE_DOC = "docs/rbac/README.md"

ROLES: tuple[str, ...] = ("OWNER", "ADMIN", "DEVELOPER", "ANALYST", "VIEWER")

_PATH = re.compile(r"^[A-Za-z0-9_.][A-Za-z0-9_./-]*$")

OracleKind = Literal[
    "api_state",
    "api_refused",
    "ui_matches_api",
    "count_delta",
    "hash_vector",
    "audit_entry",
    "text_exact",
    "network_allowlist",
]


class CatalogueError(ValueError):
    """A card or the matrix was refused; the message lists every problem."""


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Source(_Strict):
    """A citation: a repository file and a quoted anchor that file contains.

    No line numbers: a line number goes stale silently, a quoted anchor that
    stops matching fails the check.
    """

    path: str
    anchor: str = Field(min_length=3)

    @field_validator("path")
    @classmethod
    def _plain_relative_path(cls, value: str) -> str:
        if not _PATH.match(value) or ".." in value.split("/"):
            raise ValueError(
                f"source path {value!r} is not a plain repository-relative path "
                "(no line number, no '..', no leading '/')"
            )
        return value


class Oracle(_Strict):
    """What decides a step: an id, a kind, the expectation, and its source."""

    id: str = Field(pattern=r"^[a-z0-9][a-z0-9._-]*$")
    kind: OracleKind
    expect: str = Field(min_length=1)
    source: Source | None = None


class Step(_Strict):
    n: int = Field(ge=1)
    intent: str = Field(min_length=1)
    layer: Literal["A", "B", "covered"]
    covered_by: str | None = None
    oracles: list[Oracle] = Field(default_factory=list)
    writes: bool = False
    numeric: bool = False
    closes: str | None = None
    security: bool = False
    profile: Literal["core", "full"] = "core"


class Persona(_Strict):
    id: str = Field(pattern=r"^U-[A-Z]+$")
    role: Literal["ADMIN", "DEVELOPER", "ANALYST", "VIEWER", "SDK"]
    account: str = Field(
        pattern=r"^(setup:[a-z0-9_-]+|[^@\s/]+@[^@\s/]+)$",
        description="a seeded email address, or setup:<name> for a made one",
    )
    goals: list[str] = Field(min_length=1)
    day: list[Step] = Field(min_length=1)


class Cell(_Strict):
    """What one role gets from one action: the dashboard's cell and the API's.

    ``ui``: ``offered`` (the control or page works), ``not_offered`` (the
    control is absent) or ``refusal_page`` (the page renders its 403 view).
    ``api``: ``2xx`` or ``403``. When the two disagree the cell must cite the
    documented divergence it is an instance of.
    """

    ui: Literal["offered", "not_offered", "refusal_page"]
    api: Literal["2xx", "403"]
    divergence: Source | None = None


class Cells(_Strict):
    OWNER: Cell
    ADMIN: Cell
    DEVELOPER: Cell
    ANALYST: Cell
    VIEWER: Cell


class MatrixRow(_Strict):
    id: str = Field(pattern=r"^[a-z0-9][a-z0-9._-]*$")
    action: str = Field(min_length=1)
    method: Literal["GET", "POST", "PUT", "PATCH", "DELETE"]
    path: str = Field(pattern=r"^/")
    query: str | None = None
    profile: Literal["core", "full"]
    source: Source
    cells: Cells


class Matrix(_Strict):
    rows: list[MatrixRow] = Field(min_length=1)


def agrees(cell: Cell) -> bool:
    """Whether the dashboard and the API tell the same story for a cell."""
    return (cell.ui == "offered") == (cell.api == "2xx")


# --------------------------------------------------------------------- loading


def _read_yaml(path: Path) -> Any:
    try:
        return yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise CatalogueError(f"{path.name}: cannot read: {exc}") from exc


def _shape_problems(path: Path, error: Exception) -> CatalogueError:
    lines = []
    errors = getattr(error, "errors", None)
    if callable(errors):
        for item in errors():
            where = ".".join(str(p) for p in item["loc"])
            lines.append(f"{path.name}: {where}: {item['msg']}")
    else:
        lines.append(f"{path.name}: {error}")
    return CatalogueError("\n".join(lines))


def parse_persona(data: Any, name: str = "persona") -> Persona:
    """Validate a card's shape; raises ``CatalogueError``."""
    try:
        return Persona.model_validate(data)
    except ValueError as exc:
        raise _shape_problems(Path(name), exc) from exc


def parse_matrix(data: Any, name: str = "role-matrix.yaml") -> Matrix:
    """Validate the matrix's shape; raises ``CatalogueError``."""
    try:
        return Matrix.model_validate(data)
    except ValueError as exc:
        raise _shape_problems(Path(name), exc) from exc


# ---------------------------------------------------------- semantic problems


def _anchor_problem(root: Path, source: Source, what: str) -> str | None:
    target = root / source.path
    if not target.is_file():
        return f"{what} source {source.path}: file not found"
    text = target.read_text(encoding="utf-8", errors="replace")
    if source.anchor not in text:
        return f'{what} source {source.path}: anchor "{source.anchor}" not found'
    return None


def _covered_by_problem(root: Path, covered_by: str, what: str) -> str | None:
    path, _, job = covered_by.partition("#")
    target = root / path
    if not _PATH.match(path) or ".." in path.split("/") or not target.is_file():
        return f"{what} covered_by {covered_by}: file not found"
    if not job:
        return None
    if not path.startswith(".github/workflows/"):
        return f"{what} covered_by {covered_by}: '#job' needs a workflow file"
    try:
        workflow = yaml.safe_load(target.read_text(encoding="utf-8"))
    except yaml.YAMLError:
        return f"{what} covered_by {covered_by}: workflow does not parse"
    jobs = workflow.get("jobs") if isinstance(workflow, dict) else None
    if not isinstance(jobs, dict) or job not in jobs:
        return f"{what} covered_by {covered_by}: no job {job!r} in {path}"
    return None


def persona_problems(persona: Persona, root: Path) -> list[str]:
    """Every reason this card is refused, as text; empty when it is sound."""
    problems: list[str] = []
    seen_n: set[int] = set()
    seen_oracles: set[str] = set()
    for step in persona.day:
        what = f"{persona.id} day {step.n}"
        if step.n in seen_n:
            problems.append(f"{what}: duplicate step number")
        seen_n.add(step.n)
        if step.layer == "covered":
            if not step.covered_by:
                problems.append(f"{what}: layer covered names no covered_by")
        else:
            if step.covered_by:
                problems.append(f"{what}: covered_by on a layer {step.layer} step")
            if not step.oracles:
                problems.append(f"{what}: no oracle")
        if step.covered_by:
            problem = _covered_by_problem(root, step.covered_by, what)
            if problem:
                problems.append(problem)
        kinds = {o.kind for o in step.oracles}
        if step.writes and step.oracles and not kinds & STATE_ORACLE_KINDS:
            problems.append(
                f"{what}: writes, no state oracle (text_exact is additional only)"
            )
        if step.numeric and step.oracles and kinds == {"ui_matches_api"}:
            problems.append(f"{what}: numeric, only oracle is ui_matches_api")
        for oracle in step.oracles:
            if oracle.id in seen_oracles:
                problems.append(f"{what}: duplicate oracle id {oracle.id}")
            seen_oracles.add(oracle.id)
            if oracle.source:
                problem = _anchor_problem(root, oracle.source, f"{what} oracle")
                if problem:
                    problems.append(problem)
    return problems


def cross_card_problems(personas: list[Persona]) -> list[str]:
    """Problems between cards: a persona id or an oracle id used twice."""
    problems: list[str] = []
    ids: set[str] = set()
    oracle_ids: set[str] = set()
    for persona in personas:
        if persona.id in ids:
            problems.append(f"{persona.id}: duplicate persona id")
        ids.add(persona.id)
        for step in persona.day:
            for oracle in step.oracles:
                if oracle.id in oracle_ids:
                    problems.append(
                        f"{persona.id} day {step.n}: oracle id {oracle.id} "
                        "is used by another card"
                    )
                oracle_ids.add(oracle.id)
    return problems


def _operations(openapi: dict[str, Any]) -> set[tuple[str, str]]:
    return {
        (method.upper(), path)
        for path, item in openapi.get("paths", {}).items()
        for method in item
        if method in {"get", "post", "put", "patch", "delete"}
    }


def matrix_problems(matrix: Matrix, root: Path) -> list[str]:
    """Every reason the matrix is refused, as text; empty when it is sound."""
    problems: list[str] = []
    operations = _operations(
        json.loads((root / "docs/api/openapi-v1.full.json").read_text("utf-8"))
    )
    seen_ids: set[str] = set()
    seen_ops: set[tuple[str, str, str | None]] = set()
    for n, row in enumerate(matrix.rows, start=1):
        where = f"role-matrix row {n}"
        if row.id in seen_ids:
            problems.append(f"{where}: duplicate row id {row.id}")
        seen_ids.add(row.id)
        key = (row.method, row.path, row.query)
        if key in seen_ops:
            problems.append(f"{where}: {row.method} {row.path} listed twice")
        seen_ops.add(key)
        if (row.method, row.path) not in operations:
            problems.append(f"{where}: {row.method} {row.path} not in OpenAPI")
        problem = _anchor_problem(root, row.source, where)
        if problem:
            problems.append(problem)
        for role in ROLES:
            cell: Cell = getattr(row.cells, role)
            if not agrees(cell) and cell.divergence is None:
                problems.append(
                    f"{where} {role}: ui and api differ, no documented divergence"
                )
            if agrees(cell) and cell.divergence is not None:
                problems.append(
                    f"{where} {role}: a divergence is cited but ui and api agree"
                )
            if cell.divergence is not None:
                if cell.divergence.path != DIVERGENCE_DOC:
                    problems.append(
                        f"{where} {role}: a divergence is anchored in "
                        f"{DIVERGENCE_DOC}, not {cell.divergence.path}"
                    )
                problem = _anchor_problem(root, cell.divergence, f"{where} {role}")
                if problem:
                    problems.append(problem)
    return problems


# ------------------------------------------------------------------ the loaders


def load_personas(directory: Path, root: Path) -> list[Persona]:
    """Every card in *directory*, refused as a whole if any is unsound."""
    personas: list[Persona] = []
    problems: list[str] = []
    for path in sorted(directory.glob("*.yaml")):
        try:
            persona = parse_persona(_read_yaml(path), path.name)
        except CatalogueError as exc:
            problems.append(str(exc))
            continue
        problems.extend(persona_problems(persona, root))
        personas.append(persona)
    problems.extend(cross_card_problems(personas))
    if problems:
        raise CatalogueError("\n".join(problems))
    return personas


def load_matrix(path: Path, root: Path) -> Matrix:
    """The role matrix, refused if any row is unsound."""
    matrix = parse_matrix(_read_yaml(path), path.name)
    problems = matrix_problems(matrix, root)
    if problems:
        raise CatalogueError("\n".join(problems))
    return matrix
