"""The founder-run real-account check: configuration, spend caps, evidence.

The tests beside this module (``test_bigquery_live.py``,
``test_snowflake_live.py``) call the real connector adapters and the run
executor against a real BigQuery project or Snowflake account.  They are run
by hand, never by CI::

    pytest -m warehouse_live modules/backend/tests/live_warehouse/

``modules/backend/tests/unit/warehouse/test_live_check_harness.py``, which CI
runs, exercises what here needs no network -- configuration, caps, the
evidence writer, the recording wrapper, the fixture data and the comparisons
-- but not ``git_state`` or ``package_versions``, which only fill in the
evidence.

Credentials
-----------
Read only from environment variables and from a key file whose path an
environment variable names (:func:`load_config`).  Nothing is read from the
repository and nothing is written back to it.  A missing variable stops the
session before any test runs, naming every variable that is missing.

Spend caps
----------
* **BigQuery**: every query is dry-run first (the adapter does this), each
  query is capped at the connection's ``maximumBytesBilled``
  (:data:`BQ_DEFAULT_MAX_BYTES`, at most :data:`BQ_HARD_MAX_BYTES`), and the
  whole session at :data:`BQ_SESSION_BUDGET_BYTES` of dry-run estimates
  (:class:`ByteBudget`).
* **Snowflake**: the warehouse must be under a resource monitor, which the
  check reads with ``SHOW WAREHOUSES`` before anything runs on it, and every
  statement is cancelled by Snowflake at :data:`SF_DEFAULT_TIMEOUT_SECONDS`
  (at most :data:`SF_HARD_MAX_TIMEOUT_SECONDS`).

Evidence
--------
:class:`EvidenceRecorder` collects what each check saw and writes one JSON
file (and the raw responses, as fixtures in the recorded-fixture format) to a
directory outside the repository.  Before anything is written, the whole
document is searched for every credential the session loaded and for the
shapes of a private key, a signed token and an access token; if any is found
nothing is written (:class:`EvidenceRefused`).
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import platform
import re
import secrets
import sys
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import (
    Any,
    Callable,
    Dict,
    Iterable,
    List,
    Mapping,
    Optional,
    Sequence,
    Tuple,
)
from urllib.parse import urlsplit

import numpy as np

from modules.backend.app.warehouse.egress import OutboundResponse
from modules.backend.tests.live_warehouse.selection import MARKER

#: Where this file lives; the evidence directory may not be under it.
REPO_ROOT = Path(__file__).resolve().parents[4]

EVIDENCE_SCHEMA = "experimently.warehouse_live_check/1"

# -- environment ------------------------------------------------------------------

ENV_TARGET = "WAREHOUSE_LIVE_TARGET"
ENV_EVIDENCE_DIR = "WAREHOUSE_LIVE_EVIDENCE_DIR"

ENV_BQ_KEY_FILE = "WAREHOUSE_LIVE_BQ_KEY_FILE"
ENV_BQ_PROJECT = "WAREHOUSE_LIVE_BQ_PROJECT"
ENV_BQ_DATASET = "WAREHOUSE_LIVE_BQ_DATASET"
ENV_BQ_LOCATION = "WAREHOUSE_LIVE_BQ_LOCATION"
ENV_BQ_MAX_BYTES = "WAREHOUSE_LIVE_BQ_MAX_BYTES"

ENV_SF_ACCOUNT = "WAREHOUSE_LIVE_SF_ACCOUNT"
ENV_SF_USER = "WAREHOUSE_LIVE_SF_USER"
ENV_SF_ROLE = "WAREHOUSE_LIVE_SF_ROLE"
ENV_SF_WAREHOUSE = "WAREHOUSE_LIVE_SF_WAREHOUSE"
ENV_SF_DATABASE = "WAREHOUSE_LIVE_SF_DATABASE"
ENV_SF_SCHEMA = "WAREHOUSE_LIVE_SF_SCHEMA"
ENV_SF_KEY_FILE = "WAREHOUSE_LIVE_SF_KEY_FILE"
ENV_SF_KEY_PASSPHRASE = "WAREHOUSE_LIVE_SF_KEY_PASSPHRASE"
ENV_SF_RESOURCE_MONITOR = "WAREHOUSE_LIVE_SF_RESOURCE_MONITOR"
ENV_SF_TIMEOUT = "WAREHOUSE_LIVE_SF_TIMEOUT_SECONDS"

TARGETS = ("bigquery", "snowflake")

BQ_REQUIRED = (ENV_BQ_KEY_FILE, ENV_BQ_PROJECT, ENV_BQ_DATASET)
SF_REQUIRED = (
    ENV_SF_ACCOUNT,
    ENV_SF_USER,
    ENV_SF_ROLE,
    ENV_SF_WAREHOUSE,
    ENV_SF_DATABASE,
    ENV_SF_SCHEMA,
    ENV_SF_KEY_FILE,
    ENV_SF_RESOURCE_MONITOR,
)

# -- spend caps -------------------------------------------------------------------

MIB = 1024 * 1024
#: The per-query byte cap when none is given.  The two fixture CSVs are 22.2 MB
#: and 23.5 MB (``write_fixture_csvs``); what BigQuery bills for reading the
#: loaded tables is not measured here -- the check records it.
BQ_DEFAULT_MAX_BYTES = 100 * MIB
#: No per-query cap above this is accepted.
BQ_HARD_MAX_BYTES = 1024 * MIB
#: The most the whole session's dry-run estimates may add up to.
BQ_SESSION_BUDGET_BYTES = 2048 * MIB
#: The cap the refusal checks run under (QA v1: 10 MB).
BQ_TAMPER_MAX_BYTES = 10 * MIB
#: A US public table expected to read far more than :data:`BQ_TAMPER_MAX_BYTES`
#: (not measured here: the check asserts it from the dry-run estimate first).
BQ_LARGE_PUBLIC_TABLE = "bigquery-public-data.usa_names.usa_1910_current"
BQ_LARGE_PUBLIC_SQL = f"SELECT COUNT(DISTINCT name) AS n FROM `{BQ_LARGE_PUBLIC_TABLE}`"

SF_DEFAULT_TIMEOUT_SECONDS = 60
SF_HARD_MAX_TIMEOUT_SECONDS = 120
#: The statement limit the time-limit check runs under (QA v1: 10 s).
SF_TAMPER_TIMEOUT_SECONDS = 10
SF_LONG_RUNNING_SQL = (
    "SELECT COUNT(*) AS n FROM TABLE(GENERATOR(ROWCOUNT => 1000000000000))"
)

#: Fixed names of the fixture tables the founder loads (Snowflake folds them to
#: upper case; the check resolves case through the table's own metadata).
EXPOSURES_TABLE = "exposures"
EVENTS_TABLE = "events"
EXPERIMENT_KEY = "live-check"
#: A table name no role in the check should be able to create.
PROBE_TABLE = "experimently_live_check_probe"

#: The wire probe: 0.1 + 0.2 in binary64 must come back as exactly this.
WIRE_PROBE_TEXT = "0.30000000000000004"
WIRE_PROBE_VALUE = 0.1 + 0.2


class LiveConfigError(Exception):
    """The live check cannot start; the message says exactly what to set."""


@dataclass(frozen=True, repr=False)
class BigQueryLiveConfig:
    key_json: str
    key_file: str
    project: str
    dataset: str
    location: str
    max_bytes_per_query: int

    target = "bigquery"

    def __repr__(self) -> str:  # never the key
        return f"BigQueryLiveConfig(project={self.project!r}, dataset={self.dataset!r})"

    def public(self) -> Dict[str, Any]:
        """What the evidence records about the connection: nothing secret."""
        return {
            "billing_project": self.project,
            "dataset": self.dataset,
            "location": self.location,
            "max_bytes_per_query": self.max_bytes_per_query,
        }


@dataclass(frozen=True, repr=False)
class SnowflakeLiveConfig:
    account: str
    user: str
    role: str
    warehouse: str
    database: str
    schema: str
    key_pem: bytes
    key_file: str
    key_passphrase: Optional[str]
    resource_monitor: str
    query_timeout_seconds: int

    target = "snowflake"

    def __repr__(self) -> str:  # never the key
        return f"SnowflakeLiveConfig(account={self.account!r}, user={self.user!r})"

    def public(self) -> Dict[str, Any]:
        return {
            "account": self.account,
            "user": self.user,
            "role": self.role,
            "warehouse": self.warehouse,
            "database": self.database,
            "schema": self.schema,
            "resource_monitor": self.resource_monitor,
            "query_timeout_seconds": self.query_timeout_seconds,
        }


LiveConfig = Any  # BigQueryLiveConfig | SnowflakeLiveConfig


def _missing(environ: Mapping[str, str], names: Iterable[str]) -> List[str]:
    return [name for name in names if not (environ.get(name) or "").strip()]


def _read_key_file(name: str, path_text: str) -> bytes:
    path = Path(path_text).expanduser()
    if not path.is_file():
        raise LiveConfigError(f"{name} names {path}, which is not a file.")
    if is_inside(path, REPO_ROOT):
        raise LiveConfigError(
            f"{name} names a file inside the repository ({REPO_ROOT}). Keep the key "
            "outside it, so it cannot be committed."
        )
    data = path.read_bytes()
    if not data.strip():
        raise LiveConfigError(f"{name} names an empty file.")
    return data


def _int_setting(
    environ: Mapping[str, str], name: str, default: int, ceiling: int
) -> int:
    text = (environ.get(name) or "").strip()
    if not text:
        return default
    if not text.isdigit() or int(text) <= 0:
        raise LiveConfigError(f"{name} must be a positive whole number, got {text!r}.")
    value = int(text)
    if value > ceiling:
        raise LiveConfigError(
            f"{name}={value} is above this check's hard cap of {ceiling}. "
            "The cap is in the code on purpose; do not raise it for a check."
        )
    return value


def load_config(environ: Optional[Mapping[str, str]] = None) -> LiveConfig:
    """Read the target and its settings from the environment, or refuse clearly."""
    env = os.environ if environ is None else environ
    target = (env.get(ENV_TARGET) or "").strip().lower()
    if target not in TARGETS:
        raise LiveConfigError(
            f"Set {ENV_TARGET} to one of {', '.join(TARGETS)} (got "
            f"{target or 'nothing'}). The live check runs against one real "
            "warehouse at a time; see the runbook for the other variables."
        )
    required = BQ_REQUIRED if target == "bigquery" else SF_REQUIRED
    missing = _missing(env, required)
    if missing:
        raise LiveConfigError(
            f"The {target} live check needs these environment variables, which "
            f"are not set: {', '.join(missing)}. No test has run."
        )
    if target == "bigquery":
        key_bytes = _read_key_file(ENV_BQ_KEY_FILE, env[ENV_BQ_KEY_FILE])
        try:
            key_json = key_bytes.decode("utf-8")
        except UnicodeDecodeError:
            raise LiveConfigError(
                f"{ENV_BQ_KEY_FILE} is not a UTF-8 JSON key file."
            ) from None
        return BigQueryLiveConfig(
            key_json=key_json,
            key_file=str(Path(env[ENV_BQ_KEY_FILE]).expanduser()),
            project=env[ENV_BQ_PROJECT].strip(),
            dataset=env[ENV_BQ_DATASET].strip(),
            location=(env.get(ENV_BQ_LOCATION) or "US").strip(),
            max_bytes_per_query=_int_setting(
                env, ENV_BQ_MAX_BYTES, BQ_DEFAULT_MAX_BYTES, BQ_HARD_MAX_BYTES
            ),
        )
    key_pem = _read_key_file(ENV_SF_KEY_FILE, env[ENV_SF_KEY_FILE])
    passphrase = env.get(ENV_SF_KEY_PASSPHRASE) or None
    return SnowflakeLiveConfig(
        account=env[ENV_SF_ACCOUNT].strip(),
        user=env[ENV_SF_USER].strip(),
        role=env[ENV_SF_ROLE].strip(),
        warehouse=env[ENV_SF_WAREHOUSE].strip(),
        database=env[ENV_SF_DATABASE].strip(),
        schema=env[ENV_SF_SCHEMA].strip(),
        key_pem=key_pem,
        key_file=str(Path(env[ENV_SF_KEY_FILE]).expanduser()),
        key_passphrase=passphrase,
        resource_monitor=env[ENV_SF_RESOURCE_MONITOR].strip(),
        query_timeout_seconds=_int_setting(
            env, ENV_SF_TIMEOUT, SF_DEFAULT_TIMEOUT_SECONDS, SF_HARD_MAX_TIMEOUT_SECONDS
        ),
    )


def config_secrets(config: LiveConfig) -> List[str]:
    """Every secret string the session holds, for the evidence search."""
    found: List[str] = []
    if isinstance(config, BigQueryLiveConfig):
        found.append(config.key_json)
        try:
            data = json.loads(config.key_json)
        except ValueError:
            data = {}
        if isinstance(data, dict):
            for name in ("private_key", "private_key_id"):
                value = data.get(name)
                if isinstance(value, str) and value:
                    found.append(value)
    elif isinstance(config, SnowflakeLiveConfig):
        found.append(config.key_pem.decode("utf-8", errors="replace"))
        if config.key_passphrase:
            found.append(config.key_passphrase)
    return found


# -- where evidence goes ----------------------------------------------------------


def is_inside(path: Path, root: Path) -> bool:
    resolved = Path(path).expanduser().resolve()
    root = Path(root).resolve()
    return resolved == root or root in resolved.parents


def evidence_root(environ: Optional[Mapping[str, str]] = None) -> Path:
    """The evidence directory: outside the repository, always."""
    env = os.environ if environ is None else environ
    text = (env.get(ENV_EVIDENCE_DIR) or "").strip()
    root = (
        Path(text).expanduser()
        if text
        else Path.home() / "experimently-warehouse-evidence"
    )
    if is_inside(root, REPO_ROOT):
        raise LiveConfigError(
            f"{ENV_EVIDENCE_DIR} ({root}) is inside the repository. Evidence holds "
            "account identifiers; write it outside the checkout."
        )
    return root


def new_run_id(target: str, now: Optional[datetime] = None) -> str:
    """``wl-<target>-<UTC time>-<8 hex>``: what a real recording's provenance names."""
    stamp = (now or datetime.now(timezone.utc)).strftime("%Y%m%dT%H%M%SZ")
    return f"wl-{target}-{stamp}-{secrets.token_hex(4)}"


# -- the search for secrets -------------------------------------------------------

#: Shapes no evidence may contain, whatever the session loaded.
SECRET_SHAPES: Tuple[Tuple[str, "re.Pattern[str]"], ...] = (
    ("a PEM private key", re.compile(r"PRIVATE KEY-----")),
    (
        "a signed token (JWT)",
        re.compile(r"eyJ[A-Za-z0-9_-]{8,}\.eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"),
    ),
    ("a Google access token", re.compile(r"ya29\.[A-Za-z0-9._-]{8,}")),
    ("a bearer credential", re.compile(r"Bearer\s+[A-Za-z0-9._~+/=-]{8,}")),
    ("a service-account private key field", re.compile(r'"private_key"\s*:')),
)

#: Shorter fragments are not searched for: too many false alarms, and no
#: secret this check holds is that short.
MIN_SECRET_FRAGMENT = 16


def _forms(secret: str) -> List[str]:
    """A secret and the encoded forms it could appear in."""
    forms = [secret]
    raw = secret.encode("utf-8")
    forms.append(base64.b64encode(raw).decode("ascii"))
    forms.append(base64.urlsafe_b64encode(raw).decode("ascii").rstrip("="))
    forms.append(json.dumps(secret)[1:-1])  # as it appears inside a JSON string
    forms.append(repr(secret)[1:-1])
    # A PEM's body lines (and the whole body joined) stand on their own.
    lines = [
        line.strip()
        for line in secret.splitlines()
        if line.strip() and not line.startswith("-----")
    ]
    if len(lines) > 1:
        forms.append("".join(lines))
    forms.extend(lines)
    return [f for f in forms if len(f) >= MIN_SECRET_FRAGMENT]


def find_secrets(text: str, known: Sequence[str]) -> List[str]:
    """What in ``text`` looks like a credential: descriptions, never the value."""
    problems: List[str] = []
    for index, secret in enumerate(known):
        if not secret:
            continue
        for form in _forms(secret):
            if form in text:
                problems.append(
                    f"loaded credential #{index + 1} (or an encoding of it)"
                )
                break
    for description, pattern in SECRET_SHAPES:
        if pattern.search(text):
            problems.append(description)
    return problems


class EvidenceRefused(Exception):
    """The evidence contains something that looks like a credential; nothing was written."""


# -- the evidence -----------------------------------------------------------------

#: The plan's NOT VERIFIED rows each check settles (plan v2, NOT VERIFIED).
NOT_VERIFIED_ROWS: Dict[str, Dict[str, Any]] = {
    "1": {
        "item": "Service-account key creation in the founder's sandbox project",
        "warehouse": "bigquery",
        "checks": ["bq_sign_in"],
    },
    "2": {
        "item": "Snowflake trial supports SQL-API key-pair auth",
        "warehouse": "snowflake",
        "checks": ["sf_spend_cap", "sf_sign_in"],
    },
    "3": {
        "item": "FLOAT digits over the Snowflake JSON wire (DECFLOAT to VARCHAR)",
        "warehouse": "snowflake",
        "checks": ["sf_wire_probe", "sf_mean_parity"],
    },
    "4": {
        "item": "BigQuery jobs.insert dry-run statementType for our statement",
        "warehouse": "bigquery",
        "checks": ["bq_dry_run_statement_type"],
    },
    "5": {
        "item": "Athena cutoff without Enforce",
        "warehouse": "athena",
        "checks": [],
        "note": "Athena is post-launch; its own check settles this row.",
    },
    "6": {
        "item": "Staging CDK bootstrap DenyExternalId",
        "warehouse": "athena",
        "checks": [],
        "note": "Read before the post-launch Athena check; not part of this one.",
    },
    "7": {
        "item": "BigQuery FORMAT('%.17g') round-trip",
        "warehouse": "bigquery",
        "checks": ["bq_wire_probe", "bq_mean_parity"],
    },
    "8": {
        "item": "Snowflake host built from the account identifier",
        "warehouse": "snowflake",
        "checks": ["sf_sign_in"],
    },
    "11": {
        "item": "The connection's read-only role refuses writes",
        "warehouse": "both",
        "checks": ["bq_create_refused", "sf_create_refused"],
    },
    "snowflake_time_zone": {
        "item": "Snowflake session time zone and TIMESTAMP_NTZ reading",
        "warehouse": "snowflake",
        "checks": ["sf_timezone"],
    },
}


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _jsonable(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_jsonable(v) for v in value]
    if isinstance(value, float):
        return value if np.isfinite(value) else repr(value)
    if isinstance(value, (str, int, bool)) or value is None:
        return value
    if isinstance(value, (np.integer,)):
        return int(value)
    if isinstance(value, (np.floating,)):
        return float(value)
    if isinstance(value, uuid.UUID):
        return str(value)
    return repr(value)


@dataclass
class EvidenceRecorder:
    """Collects what the checks saw; :meth:`write` puts it outside the repository."""

    target: str
    run_id: str
    root: Path
    connection: Mapping[str, Any] = field(default_factory=dict)
    secrets: List[str] = field(default_factory=list)
    started_at: str = field(default_factory=_utc_now)
    checks: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    outcomes: Dict[str, str] = field(default_factory=dict)
    exchanges: List[Dict[str, Any]] = field(default_factory=list)
    code: Dict[str, Any] = field(default_factory=dict)

    @property
    def directory(self) -> Path:
        return self.root / self.run_id

    def register_secret(self, value: Optional[str]) -> None:
        if value and value not in self.secrets:
            self.secrets.append(value)

    def record(self, check: str, **data: Any) -> Dict[str, Any]:
        entry = self.checks.setdefault(check, {})
        entry.update(_jsonable(data))
        return entry

    def outcome(self, check: str, outcome: str) -> None:
        self.outcomes[check] = outcome

    def add_exchange(self, exchange: Dict[str, Any]) -> None:
        self.exchanges.append(exchange)

    def document(self) -> Dict[str, Any]:
        expected = list(EXPECTED_CHECKS.get(self.target, ()))
        rows: Dict[str, Any] = {}
        for key, row in NOT_VERIFIED_ROWS.items():
            names = [
                n
                for n in row["checks"]
                if n.startswith("bq_" if self.target == "bigquery" else "sf_")
            ]
            if not names:
                rows[key] = {**row, "settled_by_this_run": None}
                continue
            rows[key] = {
                **row,
                "checks": names,
                "settled_by_this_run": all(
                    self.outcomes.get(n) == "passed" for n in names
                ),
                "outcomes": {n: self.outcomes.get(n, "not run") for n in names},
            }
        all_passed = bool(self.outcomes) and all(
            outcome == "passed" for outcome in self.outcomes.values()
        )
        missing = sorted(set(expected) - set(self.outcomes))
        return {
            "schema": EVIDENCE_SCHEMA,
            "run_id": self.run_id,
            "warehouse": self.target,
            "marker": MARKER,
            "started_at": self.started_at,
            "finished_at": _utc_now(),
            "outcome": "passed" if all_passed and not missing else "failed",
            "expected_checks_not_run": missing,
            "code": _jsonable(self.code),
            "environment": {
                "python": sys.version.split()[0],
                "platform": platform.platform(terse=True),
            },
            "connection": _jsonable(dict(self.connection)),
            "spend_caps": _jsonable(spend_caps(self.target)),
            "tests": dict(sorted(self.outcomes.items())),
            "checks": self.checks,
            "not_verified_rows": rows,
            "exchanges": self.exchanges,
        }

    def write(self) -> Path:
        """Write ``evidence.json`` (and the recordings); refuse if a secret is in them."""
        document = self.document()
        text = json.dumps(document, indent=2, sort_keys=False)
        recordings = self._recordings()
        problems = find_secrets(text, self.secrets)
        for name, payload in recordings:
            problems.extend(f"{name}: {p}" for p in find_secrets(payload, self.secrets))
        if problems:
            raise EvidenceRefused(
                "The evidence was NOT written: it contains "
                + "; ".join(sorted(set(problems)))
                + ". Report this; do not work around it."
            )
        directory = self.directory
        (directory / "recordings" / self.target).mkdir(parents=True, exist_ok=True)
        path = directory / "evidence.json"
        path.write_text(text + "\n", encoding="utf-8")
        for name, payload in recordings:
            (directory / "recordings" / self.target / name).write_text(
                payload + "\n", encoding="utf-8"
            )
        return path

    def _recordings(self) -> List[Tuple[str, str]]:
        """Each recorded answer as a fixture in the recorded-fixture format."""
        out: List[Tuple[str, str]] = []
        for index, exchange in enumerate(self.exchanges, start=1):
            if "body" not in exchange:
                continue
            fixture = {
                "_provenance": {
                    "source": "real account",
                    "real_run_id": self.run_id,
                    "check": exchange.get("check"),
                    "request": exchange.get("request"),
                    "note": "Recorded by the warehouse_live check; see evidence.json.",
                },
                "status": exchange["status"],
                "body": exchange["body"],
            }
            name = f"{index:03d}_{exchange.get('label', 'exchange')}.json"
            out.append((name, json.dumps(fixture, indent=2)))
        return out


def spend_caps(target: str) -> Dict[str, Any]:
    if target == "bigquery":
        return {
            "default_max_bytes_per_query": BQ_DEFAULT_MAX_BYTES,
            "hard_max_bytes_per_query": BQ_HARD_MAX_BYTES,
            "session_budget_bytes": BQ_SESSION_BUDGET_BYTES,
            "refusal_check_max_bytes": BQ_TAMPER_MAX_BYTES,
            "dry_run_first": True,
        }
    return {
        "default_query_timeout_seconds": SF_DEFAULT_TIMEOUT_SECONDS,
        "hard_max_query_timeout_seconds": SF_HARD_MAX_TIMEOUT_SECONDS,
        "refusal_check_timeout_seconds": SF_TAMPER_TIMEOUT_SECONDS,
        "resource_monitor_required": True,
    }


# -- the session byte budget (BigQuery) -------------------------------------------


class BudgetExceeded(Exception):
    """The session's dry-run estimates would pass :data:`BQ_SESSION_BUDGET_BYTES`."""


class ByteBudget:
    def __init__(self, limit: int = BQ_SESSION_BUDGET_BYTES) -> None:
        self.limit = limit
        self.spent = 0

    def charge(self, estimate: int) -> None:
        if estimate < 0:
            raise ValueError("a byte estimate cannot be negative")
        if self.spent + estimate > self.limit:
            raise BudgetExceeded(
                f"this query's dry-run estimate ({estimate} bytes) would take the "
                f"session past its {self.limit}-byte budget ({self.spent} used); "
                "it was not run"
            )
        self.spent += estimate


def capped_bigquery_adapter(*args: Any, budget: ByteBudget, **kwargs: Any) -> Any:
    """A :class:`~modules.backend.app.warehouse.bigquery.BigQueryAdapter` whose
    dry runs are charged to ``budget``.

    Every query the adapter runs is dry-run first (``run_query`` does that);
    this adds the session total.  An estimate within the connection's own cap
    is charged, and :class:`BudgetExceeded` is raised before the query runs if
    the session would pass its budget.  An estimate above the connection's cap
    is not charged: the adapter refuses that query itself (``bytes_limit``).
    """
    from modules.backend.app.warehouse.bigquery import BigQueryAdapter

    class CappedBigQueryAdapter(BigQueryAdapter):
        dry_runs: List[Any]

        def dry_run(self, client: Any, sql: str) -> Any:
            dry = super().dry_run(client, sql)
            self.dry_runs.append(dry)
            if dry.total_bytes_processed <= self.connection.max_bytes_per_query:
                budget.charge(dry.total_bytes_processed)
            return dry

    adapter = CappedBigQueryAdapter(*args, **kwargs)
    adapter.dry_runs = []
    adapter.budget = budget
    return adapter


class CurrentCheck:
    """The check now running, so each recorded exchange names it."""

    def __init__(self) -> None:
        self.name: Optional[str] = None

    def __call__(self) -> Optional[str]:
        return self.name


#: The checks each warehouse's run must report, in the order they run.  A
#: test in the live directory is named ``test_<check>``;
#: ``test_live_check_harness.py`` pins this list against the files.
EXPECTED_CHECKS: Dict[str, Tuple[str, ...]] = {
    "bigquery": (
        "bq_sign_in",
        "bq_wire_probe",
        "bq_table_columns",
        "bq_load_fidelity",
        "bq_dry_run_statement_type",
        "bq_mean_parity",
        "bq_runner_parity",
        "bq_create_refused",
        "bq_dry_run_refuses_over_cap",
        "bq_bytes_billed_cap_enforced",
    ),
    "snowflake": (
        "sf_spend_cap",
        "sf_sign_in",
        "sf_timezone",
        "sf_wire_probe",
        "sf_table_columns",
        "sf_load_fidelity",
        "sf_mean_parity",
        "sf_runner_parity",
        "sf_multi_statement_refused",
        "sf_create_refused",
        "sf_time_limit",
    ),
}


# -- recording the wire -----------------------------------------------------------

#: Response bodies from these hosts are never kept: they carry access tokens.
UNRECORDED_HOSTS = frozenset({"oauth2.googleapis.com"})


def _describe_request(method: str, url: str, json_body: Any) -> Dict[str, Any]:
    parts = urlsplit(url)
    described: Dict[str, Any] = {
        "method": method,
        "host": parts.hostname,
        "path": parts.path,
    }
    if isinstance(json_body, dict):
        configuration = json_body.get("configuration")
        if isinstance(configuration, dict) and "dryRun" in configuration:
            described["dry_run"] = bool(configuration.get("dryRun"))
        if isinstance(configuration, dict):
            query = configuration.get("query")
            if isinstance(query, dict) and "maximumBytesBilled" in query:
                described["maximum_bytes_billed"] = query["maximumBytesBilled"]
        if "timeout" in json_body:
            described["timeout"] = json_body["timeout"]
    return described


class RecordingClient:
    """Wraps an :class:`~modules.backend.app.warehouse.egress.OutboundClient`.

    It records each exchange -- the request's method, host and path (never its
    headers or body), and the answer's status and JSON body -- into the
    evidence.  Token-endpoint answers are recorded without their body, and the
    token in them is registered as a secret so the evidence search looks for it.
    """

    def __init__(
        self,
        inner: Any,
        recorder: EvidenceRecorder,
        check: Callable[[], Optional[str]],
    ) -> None:
        self._inner = inner
        self._recorder = recorder
        self._check = check

    def __enter__(self) -> "RecordingClient":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def close(self) -> None:
        self._inner.close()

    def request(
        self,
        method: str,
        url: str,
        *,
        headers: Optional[Mapping[str, str]] = None,
        params: Optional[Mapping[str, str]] = None,
        content: Optional[bytes] = None,
        json_body: Any = None,
    ) -> OutboundResponse:
        response = self._inner.request(
            method,
            url,
            headers=headers,
            params=params,
            content=content,
            json_body=json_body,
        )
        request = _describe_request(method, url, json_body)
        exchange: Dict[str, Any] = {
            "check": self._check(),
            "request": request,
            "status": response.status_code,
            "label": _label(request),
        }
        body: Any = None
        try:
            body = json.loads(response.content)
        except (ValueError, UnicodeDecodeError):
            body = None
        if request.get("host") in UNRECORDED_HOSTS:
            if isinstance(body, dict):
                self._recorder.register_secret(
                    body.get("access_token")
                    if isinstance(body.get("access_token"), str)
                    else None
                )
                exchange["body_keys"] = sorted(str(k) for k in body)
                if isinstance(body.get("error"), str):
                    exchange["error"] = body["error"]
        elif body is not None:
            exchange["body"] = body
        self._recorder.add_exchange(exchange)
        return response


def _label(request: Mapping[str, Any]) -> str:
    path = str(request.get("path") or "")
    tail = [p for p in path.split("/") if p][-2:]
    words = [str(request.get("method", "")).lower(), *tail]
    if request.get("dry_run"):
        words.append("dry_run")
    text = "_".join(words)
    return re.sub(r"[^A-Za-z0-9_]+", "_", text)[:80] or "exchange"


def recording_factory(
    recorder: EvidenceRecorder,
    warehouse: str,
    check: Callable[[], Optional[str]],
    inner_factory: Optional[Callable[..., Any]] = None,
) -> Callable[[Any], RecordingClient]:
    """A connector ``client_factory`` whose clients record into ``recorder``."""
    if inner_factory is None:
        from modules.backend.app.warehouse.egress import OutboundClient

        def inner_factory(deadline: Any) -> Any:
            return OutboundClient(deadline, warehouse=warehouse)

    def factory(deadline: Any) -> RecordingClient:
        return RecordingClient(inner_factory(deadline), recorder, check)

    return factory


# -- the fixture data ---------------------------------------------------------------

EXPOSED_AT = "2026-09-02 00:00:00"
EVENT_AT = "2026-09-02 01:00:00"
WINDOW_START = datetime(2026, 9, 1, tzinfo=timezone.utc)
WINDOW_END = datetime(2026, 9, 10, tzinfo=timezone.utc)
#: The proportion metric counts an event whose ``kind`` is this.
CONVERTING_KIND = "paid"


def live_fixture(n_per_arm: Optional[int] = None) -> Tuple[np.ndarray, np.ndarray]:
    """F-MEAN-1 (plan SPEC 5), with a smaller ``n`` only for the offline tests."""
    from modules.backend.tests.unit.warehouse import numeric_fixtures as nf

    n = nf.N_PER_ARM if n_per_arm is None else int(n_per_arm)
    rng = np.random.default_rng(20260927)
    control = 1e4 + rng.normal(0.0, 1.0, n)
    treatment = 1e4 + rng.normal(0.005, 1.0, n)
    return control, treatment


def fixture_rows(
    control: np.ndarray, treatment: np.ndarray
) -> Tuple[List[Tuple[str, ...]], List[Tuple[str, ...]]]:
    """(exposure rows, event rows) as the CSV text each column is loaded from.

    Every amount is written with ``repr``, the shortest text that reads back as
    the same binary64 value, so a correctly rounding loader stores exactly the
    fixture's values.
    """
    exposures: List[Tuple[str, ...]] = []
    events: List[Tuple[str, ...]] = []
    for arm, values in (("control", control), ("treatment", treatment)):
        for i, value in enumerate(values.tolist()):
            unit = f"{arm}-{i}"
            exposures.append((unit, EXPERIMENT_KEY, arm, EXPOSED_AT))
            kind = CONVERTING_KIND if value > 1e4 else "view"
            events.append((unit, EVENT_AT, repr(float(value)), kind))
    return exposures, events


EXPOSURE_COLUMNS = ("user_id", "experiment_key", "variant", "exposed_at")
EVENT_COLUMNS = ("user_id", "event_at", "amount", "kind")


def write_fixture_csvs(
    out_dir: Path, n_per_arm: Optional[int] = None
) -> Dict[str, Any]:
    """Write ``exposures.csv`` and ``events.csv`` and return their manifest."""
    import csv

    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    control, treatment = live_fixture(n_per_arm)
    exposures, events = fixture_rows(control, treatment)
    manifest: Dict[str, Any] = {"fixture": "F-MEAN-1", "n_per_arm": len(control)}
    for name, header, rows in (
        ("exposures.csv", EXPOSURE_COLUMNS, exposures),
        ("events.csv", EVENT_COLUMNS, events),
    ):
        path = out_dir / name
        with path.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.writer(handle, lineterminator="\n")
            writer.writerow(header)
            writer.writerows(rows)
        manifest[name] = {
            "rows": len(rows),
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        }
    (out_dir / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    return manifest


def main(argv: Optional[Sequence[str]] = None) -> int:
    """``python -m modules.backend.tests.live_warehouse.harness --out DIR``."""
    import argparse

    parser = argparse.ArgumentParser(description="Write the live check's fixture CSVs.")
    parser.add_argument(
        "--out", required=True, help="a directory outside the repository"
    )
    args = parser.parse_args(argv)
    out = Path(args.out).expanduser()
    if is_inside(out, REPO_ROOT):
        print(f"refusing: {out} is inside the repository", file=sys.stderr)
        return 2
    manifest = write_fixture_csvs(out)
    print(json.dumps(manifest, indent=2))
    return 0


# -- helpers the live tests share ---------------------------------------------------


def exact_reference(control: np.ndarray, treatment: np.ndarray) -> Dict[str, Any]:
    from modules.backend.tests.unit.warehouse import numeric_fixtures as nf

    return {"control": nf.exact_arm(control), "treatment": nf.exact_arm(treatment)}


def mean_parity_errors(stats: Any, reference: Mapping[str, Any]) -> Dict[str, float]:
    """Relative errors of what a metric statement returned against the exact reference.

    The same quantities, the same way, as
    ``unit/warehouse/test_mean_sums_duckdb.py``.
    """
    from modules.backend.tests.unit.warehouse import numeric_fixtures as nf

    arms = {v.variant: v for v in stats.variants}
    c, t = arms["control"], arms["treatment"]
    exact_c, exact_t = reference["control"], reference["treatment"]
    if (c.n, t.n) != (exact_c.n, exact_t.n):
        raise AssertionError(
            f"unit counts differ: warehouse {(c.n, t.n)}, fixture "
            f"{(exact_c.n, exact_t.n)}"
        )

    def mean(arm: Any) -> float:
        return stats.k + arm.sum_d / arm.n

    def variance(arm: Any) -> float:
        return (arm.sum_d2 - arm.sum_d * arm.sum_d / arm.n) / (arm.n - 1)

    return {
        "mean_control": nf.relative_error(mean(c), exact_c.mean),
        "mean_treatment": nf.relative_error(mean(t), exact_t.mean),
        "variance_control": nf.relative_error(variance(c), exact_c.variance),
        "variance_treatment": nf.relative_error(variance(t), exact_t.variance),
        "difference": nf.relative_error(
            t.sum_d / t.n - c.sum_d / c.n, exact_t.mean - exact_c.mean
        ),
    }


#: F-MEAN-1's tolerance (plan errata 1).
F_MEAN_1_TOLERANCE = 1e-12


def wire_probe_sql(dialect: Any) -> str:
    """``SELECT <serialise>(CAST(0.1 AS DOUBLE) + CAST(0.2 AS DOUBLE))`` in ``dialect``."""
    d = dialect.double_type
    sql = f"SELECT {dialect.serialise(f'CAST(0.1 AS {d}) + CAST(0.2 AS {d})')} AS v"
    if dialect.session_offset:
        sql += f", {dialect.session_offset} AS session_offset"
    return sql


class NullSession:
    """What the runner's heartbeat opens: a session that stores nothing."""

    def __enter__(self) -> "NullSession":
        return self

    def __exit__(self, *exc_info: object) -> None:
        return None

    def execute(self, *args: Any, **kwargs: Any) -> None:
        return None

    def commit(self) -> None:
        return None


def run_plan(
    client: Any,
    exposures: Tuple[str, ...],
    events: Tuple[str, ...],
    columns: Mapping[str, Mapping[str, str]],
) -> Any:
    """The runner's plan for one proportion metric over the fixture tables."""
    from modules.backend.app.core.warehouse_identifiers import SourceFilter
    from modules.backend.app.services import warehouse_runner as runner
    from modules.backend.app.services.warehouse_query_builder import (
        AnalysisWindow,
        AssignmentMapping,
        MetricMapping,
        build_diagnostics_query,
        build_metric_query,
    )

    a, e = columns["exposures"], columns["events"]
    assignment = AssignmentMapping(
        exposures, a["user_id"], a["experiment_key"], a["variant"], a["exposed_at"]
    )
    metric = MetricMapping(
        events,
        e["user_id"],
        e["event_at"],
        "proportion",
        filters=(SourceFilter(e["kind"], "eq", CONVERTING_KIND),),
    )
    window = AnalysisWindow(WINDOW_START, WINDOW_END)
    namespace = uuid.UUID("6f1c9a53-3f5e-4d0e-9a57-5b8f3c1d2e40")
    control = runner.VariantInfo(uuid.uuid5(namespace, "control"), "control", True, 0.5)
    treatment = runner.VariantInfo(
        uuid.uuid5(namespace, "treatment"), "treatment", False, 0.5
    )
    return runner.RunPlan(
        client=client,
        diagnostics=build_diagnostics_query(
            client.dialect, assignment, EXPERIMENT_KEY, window
        ),
        metrics=(
            runner.MetricPlan(
                source_id=uuid.uuid5(namespace, "paid"),
                name="Paid",
                metric_type="proportion",
                is_primary=True,
                lower_is_better=False,
                query=build_metric_query(
                    client.dialect, assignment, metric, EXPERIMENT_KEY, window
                ),
            ),
        ),
        variants=(control, treatment),
        variant_map={"control": control.id, "treatment": treatment.id},
        fixed_allocation=True,
        alpha=0.05,
        correction_method="none",
        session_factory=NullSession,
    )


def execute_plan(plan: Any, deadline: Any) -> Dict[str, Any]:
    """The runner's execution step, without a database row to write to."""
    from modules.backend.app.services import warehouse_runner as runner

    return runner._execute(plan, deadline, uuid.UUID(int=0))


def compare_results(
    warehouse: Mapping[str, Any], oracle: Mapping[str, Any], rel: float = 1e-9
) -> List[str]:
    """Differences between two runner ``results``: counts exact, floats within ``rel``.

    ``results.diagnostics.session_offset`` is left out: only a dialect with a
    session time zone (Snowflake) reports it, and the connector has already
    refused any value but a UTC spelling (``Z`` or ``+00:00``).
    """
    problems: List[str] = []
    skip = {"results.diagnostics.session_offset"}

    def walk(a: Any, b: Any, path: str) -> None:
        if path in skip:
            return
        if isinstance(a, dict) and isinstance(b, dict):
            a = {k: v for k, v in a.items() if f"{path}.{k}" not in skip}
            b = {k: v for k, v in b.items() if f"{path}.{k}" not in skip}
            if set(a) != set(b):
                problems.append(f"{path}: keys {sorted(a)} != {sorted(b)}")
                return
            for key in a:
                walk(a[key], b[key], f"{path}.{key}")
        elif isinstance(a, list) and isinstance(b, list):
            if len(a) != len(b):
                problems.append(f"{path}: length {len(a)} != {len(b)}")
                return
            for i, (x, y) in enumerate(zip(a, b)):
                walk(x, y, f"{path}[{i}]")
        elif isinstance(a, float) or isinstance(b, float):
            if isinstance(a, bool) or isinstance(b, bool) or a is None or b is None:
                if a != b:
                    problems.append(f"{path}: {a!r} != {b!r}")
                return
            x, y = float(a), float(b)
            scale = max(abs(x), abs(y))
            if scale and abs(x - y) / scale > rel:
                problems.append(f"{path}: {x!r} != {y!r}")
        elif a != b:
            problems.append(f"{path}: {a!r} != {b!r}")

    walk(dict(warehouse), dict(oracle), "results")
    return problems


def duckdb_oracle(
    control: np.ndarray, treatment: np.ndarray
) -> Tuple[Any, Tuple[str, ...], Tuple[str, ...], Dict[str, Dict[str, str]]]:
    """A local DuckDB warehouse holding the same fixture, as a runner client."""
    import pandas as pd

    from modules.backend.app.services.warehouse_clients import WarehouseClient
    from modules.backend.tests.unit.warehouse.duckdb_adapter import (
        DUCKDB_SQL,
        DuckDBWarehouse,
    )

    exposures, events = fixture_rows(control, treatment)
    wh = DuckDBWarehouse()
    wh.create_from_frame(
        "exposures",
        pd.DataFrame(
            {
                "user_id": [r[0] for r in exposures],
                "experiment_key": [r[1] for r in exposures],
                "variant": [r[2] for r in exposures],
                "exposed_at": pd.Timestamp(EXPOSED_AT, tz="UTC"),
            }
        ),
    )
    wh.create_from_frame(
        "events",
        pd.DataFrame(
            {
                "user_id": [r[0] for r in events],
                "event_at": pd.Timestamp(EVENT_AT, tz="UTC"),
                "amount": [float(r[2]) for r in events],
                "kind": [r[3] for r in events],
            }
        ),
    )

    class _Adapter:
        def run_query(self, built: Any, deadline: Any) -> Any:
            from types import SimpleNamespace

            return SimpleNamespace(rows=tuple(wh.fetch(built)))

    client = WarehouseClient("duckdb", _Adapter(), DUCKDB_SQL)
    identity = {name: name for name in EXPOSURE_COLUMNS + EVENT_COLUMNS}
    columns = {
        "exposures": {n: identity[n] for n in EXPOSURE_COLUMNS},
        "events": {n: identity[n] for n in EVENT_COLUMNS},
    }
    return client, ("main", "exposures"), ("main", "events"), columns


def resolve_fixture_columns(
    client: Any, reported: Sequence[Tuple[str, str]], names: Sequence[str]
) -> Dict[str, str]:
    """Each fixture column's name as the warehouse reports it (case resolved).

    Uses the product's own resolution, so a type the product would refuse
    (a DATETIME time column, say) fails here as it would in a real source.
    """
    from types import SimpleNamespace

    from modules.backend.app.services import warehouse_source_service as sources

    source = SimpleNamespace(
        column_mapping={name: {"name": name, "type": None} for name in names},
        filters=[],
    )
    mapping, _ = sources.resolve_columns(source, client, list(reported))
    return {name: mapping[name]["name"] for name in names}


def git_state(root: Path = REPO_ROOT) -> Dict[str, Any]:
    """The commit the check ran from (best effort; the check runs in a checkout)."""
    import subprocess  # nosec B404 - fixed argv, no shell

    state: Dict[str, Any] = {}
    try:
        state["commit"] = subprocess.run(  # nosec B603 B607
            ["git", "rev-parse", "HEAD"],
            cwd=root,
            capture_output=True,
            text=True,
            timeout=10,
            check=True,
        ).stdout.strip()
        status = subprocess.run(  # nosec B603 B607
            ["git", "status", "--porcelain", "--untracked-files=no"],
            cwd=root,
            capture_output=True,
            text=True,
            timeout=10,
            check=True,
        ).stdout
        state["tree_clean"] = not status.strip()
    except Exception as exc:  # the evidence says it could not tell
        state["unavailable"] = type(exc).__name__
    return state


def package_versions() -> Dict[str, Optional[str]]:
    from importlib import metadata

    found: Dict[str, Optional[str]] = {}
    for name in (
        "httpx",
        "httpcore",
        "PyJWT",
        "cryptography",
        "sqlglot",
        "numpy",
        "duckdb",
    ):
        try:
            found[name] = metadata.version(name)
        except metadata.PackageNotFoundError:
            found[name] = None
    return found


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
