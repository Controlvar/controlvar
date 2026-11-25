"""The Snowflake connector: a key pair we generate, the SQL API v2, and limits.

Enabled (:mod:`.connectors`): the check against a real Snowflake account
passed on 2026-10-04 (run ``wl-snowflake-20261004T225624Z-3787b33c``), and
:data:`~.connectors.ENABLED_CONNECTORS` names it.

The connection
--------------
:class:`SnowflakeConnection` holds the account, user, role and warehouse, each
checked with ``fullmatch`` before anything else sees it:

* ``account`` -- the organisation-account identifier only (``MYORG-MYACCOUNT``,
  :data:`~.egress.SNOWFLAKE_ACCOUNT`).  The host is always built from it
  (:func:`~.egress.snowflake_host`); nothing else can name a host.
* ``user``, ``role``, ``warehouse`` -- ``[A-Za-z_][A-Za-z0-9_$]{0,254}``.
* ``role`` may not be one of the administrative roles in
  :data:`ADMIN_ROLES`, compared without regard to case.

The key pair
------------
There is no password field.  :func:`generate_key_pair` makes an RSA-2048 key
pair; the private key is kept only as the PKCS#8 DER bytes the caller
encrypts and stores (:attr:`GeneratedKeyPair.private_blob`).  What the
customer is shown (:class:`PublicKeyInstructions`) is the public key, its
``SHA256:`` fingerprint and the exact ``ALTER USER ... SET RSA_PUBLIC_KEY``
statement to run -- ``RSA_PUBLIC_KEY_2`` for a replacement key, so the key in
use keeps working until the new one has passed a connection test.

Sign-in
-------
Every request carries a JWT signed with PyJWT (RS256): ``iss`` is
``ACCOUNT.USER.SHA256:<fingerprint>``, ``sub`` is ``ACCOUNT.USER`` (both upper
case, ``.`` in the account written as ``-``), and it expires
:data:`JWT_LIFETIME_SECONDS` after it is issued (Snowflake accepts at most an
hour).  ``X-Snowflake-Authorization-Token-Type: KEYPAIR_JWT`` is always sent.

Statements
----------
``POST /api/v2/statements?async=true`` with the statement, the connection's
warehouse and role, and these session parameters on every request:

* ``multi_statement_count = 1`` -- Snowflake refuses a request that holds more
  than one statement;
* ``timezone = UTC`` -- ``TIMESTAMP_NTZ`` columns and ``CURRENT_TIMESTAMP()``
  are read in UTC, and each analysis statement returns the session's offset,
  which must be one of the UTC spellings in
  :data:`~modules.backend.app.services.warehouse_query_builder.UTC_SESSION_OFFSETS`
  -- ``Z``, which is how Snowflake writes it, or ``+00:00`` -- else
  ``timezone_not_utc``;
* ``query_tag`` -- so the customer can find our statements in their history.

The body's ``timeout`` makes Snowflake cancel the statement itself at the
connection's query limit, or earlier if less of the operation's total is
left; it is never 0, which Snowflake reads as its maximum of seven days.  That
server-side limit is what stops a statement whose handle we never learned.

The answer is a statement handle (202).  It is polled with ``GET
/api/v2/statements/{handle}`` until it finishes; the URL is built from the
account's host and the handle, which must be a UUID -- the
``statementStatusUrl`` Snowflake sends is never used.  Every result partition
after the first is read with ``?partition=n``.  If the operation's deadline
arrives first, ``POST /api/v2/statements/{handle}/cancel`` is sent and the
query fails with ``time_limit``.

Result columns come back named as Snowflake stores them, which for our
unquoted aliases is upper case; they are read in lower case, and two columns
that differ only in case are refused.  A name is an identifier
(``[A-Za-z_][A-Za-z0-9_$]*``), optionally ending in one ``?``: ``SHOW
COLUMNS`` names one of its output columns ``null?``.

Wire format
-----------
Our SQL serialises every floating-point result as
``CAST(CAST(x AS DECFLOAT) AS VARCHAR)`` (see
:mod:`~modules.backend.app.services.warehouse_query_builder`); nothing here
depends on how Snowflake renders a FLOAT.  A real account returned the wire
probe ``0.1 + 0.2`` as ``3.0000000000000004440892098500626161695e-1``: the
binary64 value's exact decimal expansion rounded to 38 significant digits, in
exponent form, not the shortest form.  The reader
(:func:`~modules.backend.app.services.warehouse_sufficient_stats.parse_float`)
accepts the exponent form and converts through ``Decimal``, which gives back
exactly ``0.30000000000000004``; that is what the probe checks.  Whether ``SUM``
over these values matches the reference within tolerance is settled by the
parity check on a real account, not here.  The live check also records the
explicit 17-digit format ``TO_VARCHAR(x, 'S9.9999999999999999EE')`` for
comparison; the connector does not use it.

Errors
------
Mapped from Snowflake's structured ``code`` (:data:`SNOWFLAKE_CODES`), then its
``sqlState`` (:data:`SQLSTATE_CODES`), then the HTTP status
(:func:`~.errors.error_for_status`).  No message text from Snowflake is read
into anything we raise, return or log.

Every request goes through :class:`~.egress.OutboundClient`, to the account's
``<org>-<account>.snowflakecomputing.com`` host only.
"""

from __future__ import annotations

import base64
import hashlib
import json
import math
import re
import threading
import time
import uuid
from dataclasses import dataclass
from typing import Any, Callable, Dict, Final, List, Mapping, Optional, Tuple
from urllib.parse import quote

import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.asymmetric.rsa import RSAPrivateKey

from modules.backend.app.core.warehouse_identifiers import (
    SNOWFLAKE,
    render_table,
    validate_table_parts,
)
from modules.backend.app.services.warehouse_query_builder import (
    BuiltQuery,
    is_utc_session_offset,
)
from modules.backend.app.warehouse.deadlines import Deadline, Sleeper
from modules.backend.app.warehouse.egress import (
    SNOWFLAKE_ACCOUNT,
    OutboundClient,
    OutboundResponse,
    snowflake_host,
)
from modules.backend.app.warehouse.errors import (
    WarehouseError,
    WarehouseErrorCode,
    error_for_status,
    log_warehouse_error,
    sanitised,
)

WAREHOUSE: Final = "snowflake"

#: The SQL API's statements resource, on the account's host.
STATEMENTS_PATH: Final = "/api/v2/statements"
#: Seconds a JWT is valid (Snowflake accepts at most 3600).
JWT_LIFETIME_SECONDS: Final = 3000
#: A JWT is signed again once it is this old, well inside its lifetime.
JWT_REUSE_SECONDS: Final = 1800
#: The session parameters sent with every statement.
SESSION_PARAMETERS: Final = {
    "multi_statement_count": "1",
    "timezone": "UTC",
    "query_tag": "experimently-analysis",
}
#: Generated keys, and the smallest stored key accepted.
RSA_KEY_BITS: Final = 2048
#: Waits between status polls, in order; the last one repeats.
POLL_PAUSES_SECONDS: Final = (0.25, 0.5, 1.0, 2.0)
#: Polls stop this many seconds before the deadline, to leave time to cancel.
CANCEL_RESERVE_SECONDS: Final = 5.0
#: Result partitions and rows read for one statement (a result is at most 51 rows,
#: a column listing a few thousand).
MAX_PARTITIONS: Final = 5
MAX_RESULT_ROWS: Final = 10_000
#: The connection test: who the session is, and in which time zone.
CONNECTION_TEST_SQL: Final = (
    "SELECT CURRENT_ROLE() AS role_name, CURRENT_WAREHOUSE() AS warehouse_name, "
    "TO_CHAR(CURRENT_TIMESTAMP(), 'TZH:TZM') AS session_offset"
)
#: Roles a connection may not use: they can do far more than read.
ADMIN_ROLES: Final = frozenset(
    {"ACCOUNTADMIN", "SECURITYADMIN", "SYSADMIN", "ORGADMIN", "USERADMIN"}
)

#: User, role and warehouse names; applied only with fullmatch.
OBJECT_NAME: Final = re.compile(r"[A-Za-z_][A-Za-z0-9_$]{0,254}")
_HANDLE: Final = re.compile(
    r"[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}"
)
_COLUMN_NAME: Final = re.compile(r"[A-Za-z_][A-Za-z0-9_$]{0,254}")
#: A result column's name: an identifier, optionally ending in one ``?``
#: (``SHOW COLUMNS`` returns a column named ``null?``).
_RESULT_COLUMN_NAME: Final = re.compile(r"[A-Za-z_][A-Za-z0-9_$]{0,254}\??")
_TYPE_NAME: Final = re.compile(r"[A-Z][A-Z0-9_]{0,31}")

_C = WarehouseErrorCode

#: Snowflake's structured error ``code`` -> ours.
SNOWFLAKE_CODES: Final[Mapping[str, WarehouseErrorCode]] = {
    "390144": _C.AUTH_FAILED,  # the JWT was not accepted
    "000630": _C.TIME_LIMIT,  # the statement reached its time limit
    "000604": _C.CANCELLED,  # the statement was cancelled
    "002003": _C.OBJECT_NOT_FOUND,  # object does not exist or not authorized
    "000904": _C.OBJECT_NOT_FOUND,  # invalid identifier (a column)
    "003001": _C.PERMISSION_DENIED,  # insufficient privileges
    "000606": _C.PERMISSION_DENIED,  # no warehouse the role can use
    "000008": _C.NOT_A_SELECT,  # more statements than multi_statement_count
}

#: Snowflake's ``sqlState`` -> ours, when its ``code`` is not in the table above.
SQLSTATE_CODES: Final[Mapping[str, WarehouseErrorCode]] = {
    "42501": _C.PERMISSION_DENIED,
    "42S02": _C.OBJECT_NOT_FOUND,
    "57014": _C.CANCELLED,
}


# -- the connection -----------------------------------------------------------


class SnowflakeConfigRefused(ValueError):
    """A Snowflake connection field was refused (422).  ``code`` is from a fixed set.

    The message is fixed per code and never contains what was submitted.
    """

    MESSAGES: Final = {
        "invalid_account": (
            "Use the organisation-account identifier (Snowsight > Admin > Accounts), "
            "e.g. MYORG-MYACCOUNT."
        ),
        "invalid_user": (
            "Use the Snowflake user's name: letters, digits, _ and $, starting "
            "with a letter or _."
        ),
        "invalid_role": (
            "Use the Snowflake role's name: letters, digits, _ and $, starting "
            "with a letter or _."
        ),
        "role_not_allowed": (
            "Use a role made for this connection with read-only grants. "
            "ACCOUNTADMIN, SECURITYADMIN, SYSADMIN, ORGADMIN and USERADMIN "
            "aren't accepted."
        ),
        "invalid_warehouse": (
            "Use the Snowflake warehouse's name: letters, digits, _ and $, "
            "starting with a letter or _."
        ),
    }

    def __init__(self, code: str, field: str) -> None:
        if code not in self.MESSAGES:
            raise ValueError("unknown refusal code")
        self.code = code
        self.field = field
        self.message = self.MESSAGES[code]
        super().__init__(self.message)

    def to_body(self) -> Dict[str, str]:
        return {"code": self.code, "field": self.field, "message": self.message}


def _fullmatch(pattern: "re.Pattern[str]", value: object) -> bool:
    return isinstance(value, str) and pattern.fullmatch(value) is not None


@dataclass(frozen=True)
class SnowflakeConnection:
    """The non-secret parameters and limit of one Snowflake connection."""

    account: str
    user: str
    role: str
    warehouse: str
    query_timeout_seconds: int

    def __post_init__(self) -> None:
        if not _fullmatch(SNOWFLAKE_ACCOUNT, self.account):
            raise SnowflakeConfigRefused("invalid_account", "account")
        if not _fullmatch(OBJECT_NAME, self.user):
            raise SnowflakeConfigRefused("invalid_user", "user")
        if not _fullmatch(OBJECT_NAME, self.role):
            raise SnowflakeConfigRefused("invalid_role", "role")
        if self.role.upper() in ADMIN_ROLES:
            raise SnowflakeConfigRefused("role_not_allowed", "role")
        if not _fullmatch(OBJECT_NAME, self.warehouse):
            raise SnowflakeConfigRefused("invalid_warehouse", "warehouse")
        value = self.query_timeout_seconds
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise ValueError("query_timeout_seconds must be a positive integer")

    @property
    def host(self) -> str:
        """``<org>-<account>.snowflakecomputing.com``, from the account alone."""
        return snowflake_host(self.account)

    @property
    def jwt_account(self) -> str:
        """The account as the JWT names it: upper case, ``.`` written as ``-``."""
        return self.account.upper().replace(".", "-")

    @property
    def jwt_user(self) -> str:
        return self.user.upper()


# -- the key pair -------------------------------------------------------------


def public_key_fingerprint(public_der: bytes) -> str:
    """``SHA256:`` and the base64 SHA-256 of the DER public key, as Snowflake shows it."""
    return "SHA256:" + base64.b64encode(hashlib.sha256(public_der).digest()).decode(
        "ascii"
    )


def set_public_key_statement(user: str, public_key: str, *, slot: int = 1) -> str:
    """The ``ALTER USER`` statement the customer runs to register a public key.

    ``slot`` 1 sets ``RSA_PUBLIC_KEY``; 2 sets ``RSA_PUBLIC_KEY_2``, for a
    replacement key while the first is still in use.
    """
    if not _fullmatch(OBJECT_NAME, user):
        raise SnowflakeConfigRefused("invalid_user", "user")
    if slot not in (1, 2) or isinstance(slot, bool):
        raise ValueError("slot must be 1 or 2")
    if not isinstance(public_key, str) or not re.fullmatch(
        r"[A-Za-z0-9+/]{1,1024}={0,2}", public_key
    ):
        raise ValueError("not a base64 public key")
    name = "RSA_PUBLIC_KEY" if slot == 1 else "RSA_PUBLIC_KEY_2"
    return f"ALTER USER {user} SET {name}='{public_key}';"


@dataclass(frozen=True)
class PublicKeyInstructions:
    """What the customer is shown for a generated key: never the private key."""

    public_key: str
    fingerprint: str
    statement: str

    def to_body(self) -> Dict[str, str]:
        return {
            "public_key": self.public_key,
            "public_key_fingerprint": self.fingerprint,
            "statement": self.statement,
        }


@dataclass(frozen=True, repr=False)
class GeneratedKeyPair:
    """A new key pair: the private key to encrypt and store, and what to show."""

    private_blob: bytes
    shown: PublicKeyInstructions

    def __repr__(self) -> str:
        return f"GeneratedKeyPair(fingerprint={self.shown.fingerprint!r})"


def generate_key_pair(user: str, *, slot: int = 1) -> GeneratedKeyPair:
    """A new RSA-2048 key pair for ``user``, and the statement that registers it."""
    private = rsa.generate_private_key(public_exponent=65537, key_size=RSA_KEY_BITS)
    key = SnowflakeKey(private)
    return GeneratedKeyPair(
        private_blob=key.to_blob(),
        shown=PublicKeyInstructions(
            public_key=key.public_key_base64,
            fingerprint=key.fingerprint,
            statement=set_public_key_statement(user, key.public_key_base64, slot=slot),
        ),
    )


class SnowflakeKey:
    """A stored private key: PKCS#8 DER, RSA, at least :data:`RSA_KEY_BITS` bits."""

    def __init__(self, private_key: RSAPrivateKey) -> None:
        if (
            not isinstance(private_key, RSAPrivateKey)
            or private_key.key_size < RSA_KEY_BITS
        ):
            raise WarehouseError(_C.INTERNAL, warehouse=WAREHOUSE)
        self._private = private_key
        self._public_der = private_key.public_key().public_bytes(
            serialization.Encoding.DER,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        )

    def to_blob(self) -> bytes:
        """The plaintext to encrypt and store: the PKCS#8 DER private key."""
        return self._private.private_bytes(
            serialization.Encoding.DER,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )

    @classmethod
    def from_blob(cls, blob: bytes) -> "SnowflakeKey":
        """Read a stored blob back.  Anything but an RSA key of 2048+ bits is ``internal``."""
        loaded: Any = None
        try:
            loaded = serialization.load_der_private_key(bytes(blob), password=None)
        except Exception:
            # Any parse failure refuses; its text is not kept.
            loaded = None
        if not isinstance(loaded, RSAPrivateKey):
            raise WarehouseError(_C.INTERNAL, warehouse=WAREHOUSE)
        return cls(loaded)

    @property
    def public_key_base64(self) -> str:
        """The public key as ``ALTER USER`` takes it: base64 DER, no delimiter lines."""
        return base64.b64encode(self._public_der).decode("ascii")

    @property
    def fingerprint(self) -> str:
        return public_key_fingerprint(self._public_der)

    def sign(self, claims: Mapping[str, Any]) -> str:
        return jwt.encode(dict(claims), self._private, algorithm="RS256")

    def __repr__(self) -> str:
        return f"SnowflakeKey(fingerprint={self.fingerprint!r})"


# -- results ------------------------------------------------------------------


@dataclass(frozen=True)
class QueryResult:
    """What a finished statement returned: the rows and the statement's metadata."""

    rows: Tuple[Dict[str, Optional[str]], ...]
    statement_handle: str
    warehouse: str
    elapsed_ms: int


@dataclass(frozen=True)
class ConnectionCheck:
    """What the connection test saw: the session's role, warehouse and UTC offset."""

    role: Optional[str]
    warehouse: Optional[str]
    session_offset: str


def _invalid() -> WarehouseError:
    return WarehouseError(_C.RESULT_INVALID, warehouse=WAREHOUSE)


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, dict) else {}


def error_for_body(body: Mapping[str, Any], status: int) -> WarehouseError:
    """Our code for a Snowflake error answer, from ``code``, ``sqlState`` or the status."""
    code = body.get("code")
    state = body.get("sqlState")
    if isinstance(code, str) and code in SNOWFLAKE_CODES:
        mapped: Optional[WarehouseErrorCode] = SNOWFLAKE_CODES[code]
    elif isinstance(state, str) and state in SQLSTATE_CODES:
        mapped = SQLSTATE_CODES[state]
    else:
        mapped = None
    if mapped is None:
        return error_for_status(status, warehouse=WAREHOUSE, vendor_code=code)
    return WarehouseError(
        mapped, warehouse=WAREHOUSE, vendor_code=code, http_status=status
    )


def decode_result(
    first: Mapping[str, Any],
) -> Tuple[List[str], List[List[Optional[str]]], int]:
    """(lower-case column names, partition 0's rows, number of partitions).

    Every cell must be a string or null, which is how the SQL API carries
    every type in the ``jsonv2`` format.
    """
    meta = _mapping(first.get("resultSetMetaData"))
    if meta.get("format") not in (None, "jsonv2"):
        raise _invalid()
    row_type = meta.get("rowType")
    if not isinstance(row_type, list) or not row_type:
        raise _invalid()
    names: List[str] = []
    for column in row_type:
        name = _mapping(column).get("name")
        if not _fullmatch(_RESULT_COLUMN_NAME, name) or name.lower() in names:
            raise _invalid()
        names.append(name.lower())
    partitions = meta.get("partitionInfo", [{}])
    if not isinstance(partitions, list) or not 1 <= len(partitions) <= MAX_PARTITIONS:
        raise _invalid()
    return names, decode_data(first, len(names)), len(partitions)


def decode_data(page: Mapping[str, Any], width: int) -> List[List[Optional[str]]]:
    rows = page.get("data", [])
    if not isinstance(rows, list) or len(rows) > MAX_RESULT_ROWS:
        raise _invalid()
    decoded: List[List[Optional[str]]] = []
    for row in rows:
        if not isinstance(row, list) or len(row) != width:
            raise _invalid()
        if not all(cell is None or isinstance(cell, str) for cell in row):
            raise _invalid()
        decoded.append(list(row))
    return decoded


# -- the adapter --------------------------------------------------------------


ClientFactory = Callable[[Deadline], OutboundClient]


def _default_client(deadline: Deadline) -> OutboundClient:
    return OutboundClient(deadline, warehouse=WAREHOUSE)


class _Pending(Exception):
    """The statement is still running (a 202)."""


def _is_zero_count(value: object) -> bool:
    """True for a count Snowflake wrote as digits that are all zero (``"0"``)."""
    return isinstance(value, str) and value.strip().isdigit() and int(value) == 0


class SnowflakeAdapter:
    """One Snowflake connection's calls, each made within the caller's Deadline.

    Call it from the warehouse executor's worker thread: every method takes
    the :class:`~.deadlines.Deadline` the executor created for the job.
    """

    def __init__(
        self,
        connection: SnowflakeConnection,
        key: SnowflakeKey,
        *,
        client_factory: Optional[ClientFactory] = None,
        wall_clock: Optional[Callable[[], float]] = None,
        monotonic: Optional[Callable[[], float]] = None,
        request_id_factory: Optional[Callable[[], str]] = None,
        sleeper: Optional[Sleeper] = None,
    ) -> None:
        if not isinstance(key, SnowflakeKey):
            raise WarehouseError(_C.INTERNAL, warehouse=WAREHOUSE)
        self.connection = connection
        self._key = key
        self._client_factory = client_factory or _default_client
        self._wall_clock = wall_clock or time.time
        self._monotonic = monotonic or time.monotonic
        self._request_id = request_id_factory or (lambda: str(uuid.uuid4()))
        self._sleeper = sleeper
        self._jwt_lock = threading.Lock()
        self._jwt: Optional[Tuple[str, int]] = None

    def __repr__(self) -> str:
        return f"SnowflakeAdapter(account={self.connection.account!r})"

    # -- sign-in ---------------------------------------------------------

    def _token(self) -> str:
        now = int(self._wall_clock())
        with self._jwt_lock:
            if self._jwt is not None and 0 <= now - self._jwt[1] < JWT_REUSE_SECONDS:
                return self._jwt[0]
            subject = f"{self.connection.jwt_account}.{self.connection.jwt_user}"
            claims = {
                "iss": f"{subject}.{self._key.fingerprint}",
                "sub": subject,
                "iat": now,
                "exp": now + JWT_LIFETIME_SECONDS,
            }
            signed: Optional[str] = None
            try:
                signed = self._key.sign(claims)
            except Exception:
                signed = None
            if signed is None:
                # A stored key that no longer signs: nothing the caller can fix here.
                raise WarehouseError(_C.INTERNAL, warehouse=WAREHOUSE)
            self._jwt = (signed, now)
            return signed

    def _headers(self) -> Dict[str, str]:
        return {
            "Authorization": f"Bearer {self._token()}",
            "X-Snowflake-Authorization-Token-Type": "KEYPAIR_JWT",
            "Accept": "application/json",
            "User-Agent": "Experimently",
        }

    # -- the API ---------------------------------------------------------

    def _url(self, path: str) -> str:
        return "https://" + self.connection.host + path

    @staticmethod
    def _statement_path(handle: str) -> str:
        if not _fullmatch(_HANDLE, handle):
            raise _invalid()
        return STATEMENTS_PATH + "/" + quote(handle, safe="-")

    def _send(
        self,
        client: OutboundClient,
        method: str,
        path: str,
        *,
        params: Optional[Mapping[str, str]] = None,
        body: Any = None,
    ) -> OutboundResponse:
        return client.request(
            method,
            self._url(path),
            headers=self._headers(),
            params=params,
            json_body=body,
        )

    @staticmethod
    def _body(response: OutboundResponse) -> Mapping[str, Any]:
        parsed = response.json()
        if not isinstance(parsed, dict):
            raise _invalid()
        return parsed

    def _error(self, response: OutboundResponse) -> WarehouseError:
        body: Mapping[str, Any] = {}
        try:
            body = _mapping(response.json())
        except WarehouseError:
            body = {}
        return error_for_body(body, response.status_code)

    def _handle_of(self, body: Mapping[str, Any]) -> str:
        handle = body.get("statementHandle")
        if not _fullmatch(_HANDLE, handle):
            raise _invalid()
        return handle

    def _submit(
        self,
        client: OutboundClient,
        sql: str,
        deadline: Deadline,
        database: Optional[str],
    ) -> Tuple[str, Optional[Mapping[str, Any]]]:
        """Send the statement: (its handle, its first result page if already done)."""
        # Snowflake cancels the statement itself at the query limit, or
        # earlier if less of the operation's total is left.  Never 0, which
        # Snowflake reads as its maximum.
        seconds = min(
            self.connection.query_timeout_seconds,
            max(1, math.floor(deadline.remaining())),
        )
        body: Dict[str, Any] = {
            "statement": sql,
            "timeout": seconds,
            "warehouse": self.connection.warehouse,
            "role": self.connection.role,
            "parameters": dict(SESSION_PARAMETERS),
        }
        if database is not None:
            body["database"] = database
        response = self._send(
            client,
            "POST",
            STATEMENTS_PATH,
            params={"async": "true", "requestId": self._request_id()},
            body=body,
        )
        if response.status_code == 202:
            return self._handle_of(self._body(response)), None
        if response.status_code == 200:
            answer = self._body(response)
            return self._handle_of(answer), answer
        raise self._error(response)

    def _status(
        self, client: OutboundClient, handle: str, partition: Optional[int] = None
    ) -> Mapping[str, Any]:
        """One ``GET`` of the statement; :class:`_Pending` while it runs."""
        params = None if partition is None else {"partition": str(partition)}
        response = self._send(
            client, "GET", self._statement_path(handle), params=params
        )
        if response.status_code == 202 and partition is None:
            answer = self._body(response)
            if answer.get("statementHandle") not in (None, handle):
                raise _invalid()
            raise _Pending()
        if response.status_code == 429 and partition is None:
            # Snowflake's rate limit: wait and ask again, within the deadline.
            raise _Pending()
        if response.status_code != 200:
            raise self._error(response)
        answer = self._body(response)
        if answer.get("statementHandle") not in (None, handle):
            raise _invalid()
        return answer

    def _wait(
        self, client: OutboundClient, handle: str, deadline: Deadline
    ) -> Mapping[str, Any]:
        """Poll until the statement finishes; its first result page."""
        polls = 0
        while True:
            if deadline.remaining() - CANCEL_RESERVE_SECONDS <= 0:
                raise WarehouseError(_C.TIME_LIMIT, warehouse=WAREHOUSE)
            try:
                return self._status(client, handle)
            except _Pending:
                pass
            pause = POLL_PAUSES_SECONDS[min(polls, len(POLL_PAUSES_SECONDS) - 1)]
            polls += 1
            if deadline.remaining() - CANCEL_RESERVE_SECONDS <= pause:
                raise WarehouseError(_C.TIME_LIMIT, warehouse=WAREHOUSE)
            deadline.sleep(pause, sleeper=self._sleeper)

    def _rows(
        self, client: OutboundClient, handle: str, first: Mapping[str, Any]
    ) -> List[Dict[str, Optional[str]]]:
        names, rows, partitions = decode_result(first)
        for partition in range(1, partitions):
            rows.extend(
                decode_data(self._status(client, handle, partition), len(names))
            )
            if len(rows) > MAX_RESULT_ROWS:
                raise _invalid()
        return [dict(zip(names, row)) for row in rows]

    def _cancel(self, client: OutboundClient, handle: str, deadline: Deadline) -> None:
        """Ask Snowflake to cancel the statement; best effort, never raises."""
        failure: Optional[WarehouseError] = None
        own: Optional[OutboundClient] = None
        try:
            target = client
            if deadline.remaining() < 1.0:
                # The reserve was used up by a slow answer: the cancel gets
                # its own short budget rather than not being sent.
                own = self._client_factory(Deadline(CANCEL_RESERVE_SECONDS))
                target = own
            response = self._send(
                target, "POST", self._statement_path(handle) + "/cancel"
            )
            if response.status_code != 200:
                failure = self._error(response)
        except WarehouseError as exc:
            failure = exc
        except Exception:
            failure = WarehouseError(_C.INTERNAL, warehouse=WAREHOUSE)
        finally:
            if own is not None:
                own.close()
        if failure is not None:
            log_warehouse_error(sanitised(failure), event="snowflake cancel failed")

    def _execute(
        self, sql: str, deadline: Deadline, database: Optional[str] = None
    ) -> Tuple[str, List[Dict[str, Optional[str]]], int]:
        """Run one statement: (handle, rows, elapsed ms)."""
        started = self._monotonic()
        with self._client_factory(deadline) as client:
            failure: Optional[WarehouseError] = None
            handle: Optional[str] = None
            rows: List[Dict[str, Optional[str]]] = []
            try:
                handle, first = self._submit(client, sql, deadline, database)
                if first is None:
                    first = self._wait(client, handle, deadline)
                rows = self._rows(client, handle, first)
            except WarehouseError as exc:
                failure = exc
            if failure is not None:
                # The statement may still be running: stop it unless Snowflake
                # itself reported how it ended.
                if handle is not None and failure.code in (
                    _C.TIME_LIMIT,
                    _C.UNREACHABLE,
                    _C.RESULT_INVALID,
                    _C.INTERNAL,
                ):
                    self._cancel(client, handle, deadline)
                raise sanitised(failure)
        if handle is None:  # unreachable: _submit returns a handle or raises
            raise WarehouseError(_C.INTERNAL, warehouse=WAREHOUSE)
        elapsed = max(0, int(round((self._monotonic() - started) * 1000)))
        return handle, rows, elapsed

    @staticmethod
    def _check_utc(
        rows: List[Dict[str, Optional[str]]], *, preview: bool = False
    ) -> None:
        """Every row must report a UTC offset.

        A preview of a window with no rows reports a NULL offset (``MIN`` over
        no rows); only a preview row whose ``total_rows`` is 0 may do that.
        """
        for row in rows:
            offset = row.get("session_offset")
            if is_utc_session_offset(offset):
                continue
            if preview and offset is None and _is_zero_count(row.get("total_rows")):
                continue
            raise WarehouseError(_C.TIMEZONE_NOT_UTC, warehouse=WAREHOUSE)

    # -- operations ------------------------------------------------------

    def run_query(self, query: BuiltQuery, deadline: Deadline) -> QueryResult:
        """Run and read one generated statement, within ``deadline``.

        Every row must report a UTC session offset (``Z`` or ``+00:00``).
        """
        if not isinstance(query, BuiltQuery) or query.dialect != WAREHOUSE:
            raise WarehouseError(_C.INTERNAL, warehouse=WAREHOUSE)
        databases = {table[0] for table in query.tables if table}
        database = next(iter(databases)) if len(databases) == 1 else None
        handle, rows, elapsed = self._execute(query.sql, deadline, database)
        self._check_utc(rows, preview=query.kind == "preview")
        return QueryResult(
            rows=tuple(rows),
            statement_handle=handle,
            warehouse=self.connection.warehouse,
            elapsed_ms=elapsed,
        )

    def check_connection(self, deadline: Deadline) -> ConnectionCheck:
        """Sign in and run a query that reads no table.

        Proves the key signs in for the user, the role is granted and the
        warehouse is usable, and that the session runs in UTC.  The warehouse
        resumes if it was suspended, which Snowflake bills.
        """
        _, rows, _ = self._execute(CONNECTION_TEST_SQL, deadline)
        if len(rows) != 1:
            raise _invalid()
        self._check_utc(rows)
        row = rows[0]
        return ConnectionCheck(
            role=row.get("role_name"),
            warehouse=row.get("warehouse_name"),
            # The spelling Snowflake returned; _check_utc has accepted it.
            session_offset=str(row.get("session_offset")),
        )

    def table_columns(
        self, table: Tuple[str, ...], deadline: Deadline
    ) -> Tuple[Tuple[str, str], ...]:
        """``SHOW COLUMNS IN TABLE``: the column names and types of a table or view.

        The reference is checked against the Snowflake identifier rules and
        quoted before anything is sent (``WarehouseQueryRefused``,
        ``invalid_identifier``), so its parts are matched exactly as written.
        Reads metadata only; no data is queried.
        """
        parts = validate_table_parts(SNOWFLAKE, table)
        statement = "SHOW COLUMNS IN TABLE " + render_table(SNOWFLAKE, parts)
        _, rows, _ = self._execute(statement, deadline, parts[0])
        columns: List[Tuple[str, str]] = []
        for row in rows:
            name = row.get("column_name")
            kind = _column_type(row.get("data_type"))
            if not _fullmatch(_COLUMN_NAME, name) or kind is None:
                raise _invalid()
            columns.append((name, kind))
        return tuple(columns)


def _column_type(value: Any) -> Optional[str]:
    """The ``type`` of ``SHOW COLUMNS``' ``data_type`` JSON, e.g. ``TIMESTAMP_NTZ``."""
    if not isinstance(value, str) or len(value) > 4096:
        return None
    parsed: Any = None
    try:
        parsed = json.loads(value)
    except (ValueError, RecursionError):
        return None
    kind = _mapping(parsed).get("type")
    return kind if _fullmatch(_TYPE_NAME, kind) else None


__all__ = [
    "ADMIN_ROLES",
    "CONNECTION_TEST_SQL",
    "SESSION_PARAMETERS",
    "SNOWFLAKE_CODES",
    "SQLSTATE_CODES",
    "STATEMENTS_PATH",
    "ConnectionCheck",
    "GeneratedKeyPair",
    "PublicKeyInstructions",
    "QueryResult",
    "SnowflakeAdapter",
    "SnowflakeConfigRefused",
    "SnowflakeConnection",
    "SnowflakeKey",
    "decode_result",
    "error_for_body",
    "generate_key_pair",
    "public_key_fingerprint",
    "set_public_key_statement",
]
