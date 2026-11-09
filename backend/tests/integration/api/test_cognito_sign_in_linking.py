"""A Cognito sign-in is linked to its account by the Cognito user ID.

Under ``AUTH_PROVIDER=cognito`` the account is the row whose ``external_id``
is ``cognito:<sub>``; a first sign-in creates it. An account that already
uses the identity's username or email address is never used for it until an
administrator sets ``external_id`` (``backend.app.services.cognito_accounts``
has the full table of cases).

Two harnesses, both against real Postgres:

* ``signin`` stubs ``deps.auth_service.get_user_with_groups`` at the service
  boundary, inside a moto fence, so each case controls the identity exactly.
  The stub's payload is built by ``cognito_identity.identity``, whose shape
  ``backend/tests/integration/auth/test_cognito_identity_shape.py`` pins to
  what the real service returns through moto.
* ``pool`` drives the real ``CognitoAuthService`` through moto, for the role
  taken from the token's ``cognito:groups`` claim.

Every refusal makes the same five assertions (R1-R5):

* R1 the response is the generic 401, and the refusal record on the
  ``backend.app.auth.cognito_sign_in`` logger carries exactly the expected
  ``reason`` -- so an error that fell into the catch-all (no record) or a
  token the stub never saw cannot pass as a refusal;
* R2 the Cognito boundary was called exactly once -- the request really took
  the Cognito path;
* R3 every affected row, read through a separate connection, is unchanged;
* R4 the exact row count is unchanged;
* R5 the request's session holds nothing new or changed, and committing it
  from the test changes nothing either.
"""

from __future__ import annotations

import logging
import os
import uuid
from typing import Any, Callable, Dict, Iterator, List, NamedTuple, Optional

import boto3
import pytest
from fastapi.testclient import TestClient
from moto import mock_cognitoidp
from sqlalchemy import event, func, text
from sqlalchemy.orm import Session

from backend.app.api import deps
from backend.app.core.config import settings
from backend.app.db.session import get_db as session_get_db
from backend.app.main import app
from backend.app.models.user import User, UserRole
from backend.app.services import cognito_accounts
from backend.tests.integration.cognito_identity import identity
from backend.tests.integration.conftest import HASHED_PASSWORD
from backend.tests.integration.email_lower_index import (
    email_lower_index_survives_the_session,
    without_email_lower_index,
)

pytestmark = [pytest.mark.integration, pytest.mark.regression]

SIGN_IN_LOGGER = "backend.app.auth.cognito_sign_in"
REFUSED_BODY = {"detail": "Could not validate credentials"}
REGION = "us-east-1"
PASSWORD = "Perm-pass1!"


# --------------------------------------------------------------------------
# Shared helpers
# --------------------------------------------------------------------------


def _suffix() -> str:
    return uuid.uuid4().hex[:10]


def _blank_aws(monkeypatch) -> None:
    # Nothing here may reach AWS: moto intercepts every call, and the
    # credential chain is blanked as a second line.
    for name in ("AWS_PROFILE", "AWS_SESSION_TOKEN", "AWS_DEFAULT_PROFILE"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_CONFIG_FILE", os.devnull)
    monkeypatch.setenv("AWS_SHARED_CREDENTIALS_FILE", os.devnull)
    monkeypatch.setenv("AWS_EC2_METADATA_DISABLED", "true")
    monkeypatch.setenv("AWS_REGION", REGION)


def _route_db_to(db_session: Session) -> dict:
    def override_get_db():
        yield db_session

    saved = dict(app.dependency_overrides)
    app.dependency_overrides[deps.get_db] = override_get_db
    app.dependency_overrides[session_get_db] = override_get_db
    return saved


def _restore_overrides(saved: dict) -> None:
    app.dependency_overrides.clear()
    app.dependency_overrides.update(saved)


def _reasons(caplog) -> list:
    """The ``reason`` of every record on the sign-in logger, in order."""
    return [
        getattr(record, "reason", None)
        for record in caplog.records
        if record.name == SIGN_IN_LOGGER
    ]


def _assert_generic_401(response) -> None:
    assert response.status_code == 401, response.text
    assert response.json() == REFUSED_BODY
    assert response.headers.get("www-authenticate") == "Bearer"


class Rows:
    """Reads ``users`` through its own connection, never the test's session:
    the request shares that session, so the session would show its own
    uncommitted changes."""

    def __init__(self, engine) -> None:
        self.engine = engine

    def snapshot(self, user_id) -> Optional[str]:
        with self.engine.connect() as conn:
            return conn.execute(
                text("SELECT row_to_json(u)::text FROM users u WHERE u.id = :id"),
                {"id": str(user_id)},
            ).scalar()

    def column(self, user_id, name: str) -> Any:
        assert name.isidentifier()
        with self.engine.connect() as conn:
            return conn.execute(
                text(f"SELECT {name} FROM users WHERE id = :id"), {"id": str(user_id)}
            ).scalar()

    def count(self) -> int:
        with self.engine.connect() as conn:
            return conn.execute(text("SELECT count(*) FROM users")).scalar()

    def id_with_external_id(self, external_id: str):
        with self.engine.connect() as conn:
            return conn.execute(
                text("SELECT id FROM users WHERE external_id = :x"),
                {"x": external_id},
            ).scalar()


# --------------------------------------------------------------------------
# Harness 1: the Cognito boundary stubbed
# --------------------------------------------------------------------------


class SignIn(NamedTuple):
    client: TestClient
    rows: Rows
    calls: List[str]
    use: Callable[..., None]
    db: Session


@pytest.fixture
def signin(db_session, test_db, monkeypatch) -> Iterator[SignIn]:
    """Cognito mode with ``get_user_with_groups`` answering ``use(...)``."""
    _blank_aws(monkeypatch)
    monkeypatch.setattr(settings, "AUTH_PROVIDER", "cognito")
    monkeypatch.setattr(settings, "SYNC_ROLES_ON_LOGIN", True)

    calls: List[str] = []
    answer: Dict[str, Any] = {}

    def stub(token: str) -> Dict[str, Any]:
        calls.append(token)
        if isinstance(answer.get("value"), Exception):
            raise answer["value"]
        return answer["value"]

    def use(value) -> None:
        answer["value"] = value

    saved = _route_db_to(db_session)
    try:
        with mock_cognitoidp():
            monkeypatch.setattr(deps.auth_service, "get_user_with_groups", stub)
            yield SignIn(
                TestClient(app, raise_server_exceptions=False),
                Rows(test_db),
                calls,
                use,
                db_session,
            )
    finally:
        _restore_overrides(saved)


def _sign_in(h: SignIn, caplog):
    with caplog.at_level(logging.WARNING, logger=SIGN_IN_LOGGER):
        return h.client.get(
            "/api/v1/users/me", headers={"Authorization": "Bearer a-token"}
        )


def _add_user(db: Session, **fields) -> User:
    user = User(**fields)
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def _assert_refused(h: SignIn, response, caplog, reason: str, before: dict, count):
    """R1-R5. ``before`` maps each affected row id to its snapshot."""
    # R1
    _assert_generic_401(response)
    assert _reasons(caplog) == [reason]
    # R2
    assert len(h.calls) == 1
    # R3
    for row_id, snap in before.items():
        assert h.rows.snapshot(row_id) == snap
    # R4
    assert h.rows.count() == count
    # R5
    assert not h.db.new
    assert not h.db.dirty
    h.db.commit()
    for row_id, snap in before.items():
        assert h.rows.snapshot(row_id) == snap
    assert h.rows.count() == count


def _admin_row(db: Session) -> User:
    """The first administrator as bootstrap creates it: username ``admin``,
    a password, superuser, ADMIN. Other tests in the session share the row,
    so it is put back into that state every time."""
    user = db.query(User).filter(User.username == "admin").first()
    if user is None:
        user = User(username="admin", email="admin@example.com")
        db.add(user)
    user.hashed_password = HASHED_PASSWORD
    user.is_superuser = True
    user.is_active = True
    user.role = UserRole.ADMIN
    user.external_id = None
    db.commit()
    db.refresh(user)
    return user


# --------------------------------------------------------------------------
# The refusal record and the harness itself
# --------------------------------------------------------------------------


def test_the_refusal_record_is_captured(caplog):
    """Positive control: a record on the sign-in logger reaches caplog with
    its ``reason``, so an empty capture below means no record was written."""
    with caplog.at_level(logging.WARNING, logger=SIGN_IN_LOGGER):
        logging.getLogger(SIGN_IN_LOGGER).warning(
            "planted", extra={"reason": "planted"}
        )
    assert _reasons(caplog) == ["planted"]


def test_a_new_cognito_identity_gets_an_account_linked_to_its_sub(signin, caplog):
    """Case 7 (QA row 6), and the positive control for the stub harness."""
    sfx = _suffix()
    sub = str(uuid.uuid4())
    count = signin.rows.count()
    signin.use(
        identity(
            f"new_{sfx}",
            sub,
            f"new.{sfx}@example.com",
            ["Developers"],
            given_name="Ada",
            family_name="Lee",
        )
    )

    response = _sign_in(signin, caplog)

    assert response.status_code == 200, response.text
    assert response.json()["username"] == f"new_{sfx}"
    assert _reasons(caplog) == []
    assert signin.calls == ["a-token"]
    assert signin.rows.count() == count + 1
    row_id = signin.rows.id_with_external_id(f"cognito:{sub}")
    assert str(row_id) == response.json()["id"]
    assert signin.rows.column(row_id, "hashed_password") is None
    assert signin.rows.column(row_id, "role") == "DEVELOPER"
    assert signin.rows.column(row_id, "is_superuser") is False
    assert signin.rows.column(row_id, "email") == f"new.{sfx}@example.com"


def test_a_new_identity_in_an_admin_group_gets_an_administrator_account(signin, caplog):
    sfx = _suffix()
    sub = str(uuid.uuid4())
    signin.use(identity(f"boss_{sfx}", sub, f"boss.{sfx}@example.com", ["Admins"]))

    response = _sign_in(signin, caplog)

    assert response.status_code == 200, response.text
    row_id = signin.rows.id_with_external_id(f"cognito:{sub}")
    assert signin.rows.column(row_id, "role") == "ADMIN"
    assert signin.rows.column(row_id, "is_superuser") is True


# --------------------------------------------------------------------------
# Case 2: the username names an account with a password (rows 1 and 2)
# --------------------------------------------------------------------------


@pytest.mark.parametrize("sync", [True, False], ids=["sync_on", "sync_off"])
def test_a_cognito_sign_in_named_like_an_account_with_a_password_is_refused(
    signin, caplog, monkeypatch, sync
):
    monkeypatch.setattr(settings, "SYNC_ROLES_ON_LOGIN", sync)
    admin = _admin_row(signin.db)
    assert (admin.role, admin.is_superuser, admin.external_id) == (
        UserRole.ADMIN,
        True,
        None,
    )
    before = {admin.id: signin.rows.snapshot(admin.id)}
    count = signin.rows.count()
    signin.use(identity("admin", str(uuid.uuid4()), f"x.{_suffix()}@example.com"))

    response = _sign_in(signin, caplog)

    _assert_refused(signin, response, caplog, "local_password", before, count)


# --------------------------------------------------------------------------
# Case 3: the username names an account linked to someone else (rows 8, 8b)
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "linked_to",
    ["cognito:another-sub", "an-identity-provider-subject"],
    ids=["another_cognito_user", "single_sign_on_subject"],
)
def test_an_account_linked_to_another_identity_is_refused(signin, caplog, linked_to):
    sfx = _suffix()
    other = _add_user(
        signin.db,
        username=f"linked_{sfx}",
        email=f"linked.{sfx}@example.com",
        external_id=f"{linked_to}-{sfx}",
        role=UserRole.DEVELOPER,
    )
    before = {other.id: signin.rows.snapshot(other.id)}
    count = signin.rows.count()
    signin.use(
        identity(f"linked_{sfx}", str(uuid.uuid4()), f"linked.{sfx}@example.com")
    )

    response = _sign_in(signin, caplog)

    _assert_refused(signin, response, caplog, "linked_elsewhere", before, count)


def test_an_account_created_by_single_sign_on_is_refused(signin, caplog):
    """Row 8b: the shape ``sso_service.provision_user`` writes -- a
    random-suffixed username, no password, the provider's subject."""
    sfx = _suffix()
    sso = _add_user(
        signin.db,
        username=f"pat_{sfx[:6]}",
        email=f"pat.{sfx}@example.com",
        external_id=f"00u{sfx}",
        role=UserRole.VIEWER,
    )
    before = {sso.id: signin.rows.snapshot(sso.id)}
    count = signin.rows.count()
    signin.use(identity(sso.username, str(uuid.uuid4()), sso.email, ["Admins"]))

    response = _sign_in(signin, caplog)

    _assert_refused(signin, response, caplog, "linked_elsewhere", before, count)


# --------------------------------------------------------------------------
# Case 4: an account with neither a password nor a link (rows 8c and 9)
# --------------------------------------------------------------------------


def test_an_account_from_an_earlier_cognito_sign_in_is_refused_until_linked(
    signin, caplog
):
    """Row 9: no password, no ``external_id``, the same username and email."""
    sfx = _suffix()
    legacy = _add_user(
        signin.db,
        username=f"early_{sfx}",
        email=f"early.{sfx}@example.com",
        role=UserRole.ANALYST,
    )
    before = {legacy.id: signin.rows.snapshot(legacy.id)}
    count = signin.rows.count()
    signin.use(identity(legacy.username, str(uuid.uuid4()), legacy.email, ["Admins"]))

    response = _sign_in(signin, caplog)

    _assert_refused(signin, response, caplog, "legacy_unlinked", before, count)


def test_an_unlinked_account_created_by_single_sign_on_is_refused(signin, caplog):
    """Row 8c: single sign-on with no subject from the provider leaves the
    same shape -- random-suffixed username, no password, ``external_id``
    NULL."""
    sfx = _suffix()
    sso = _add_user(
        signin.db,
        username=f"sam_{sfx[:6]}",
        email=f"sam.{sfx}@example.com",
        role=UserRole.VIEWER,
    )
    assert signin.rows.column(sso.id, "external_id") is None
    before = {sso.id: signin.rows.snapshot(sso.id)}
    count = signin.rows.count()
    signin.use(identity(sso.username, str(uuid.uuid4()), sso.email))

    response = _sign_in(signin, caplog)

    _assert_refused(signin, response, caplog, "legacy_unlinked", before, count)


# --------------------------------------------------------------------------
# Case 0 and 5: no sub, no email (row 11)
# --------------------------------------------------------------------------


@pytest.mark.parametrize("sub", [None, "", "   "], ids=["absent", "empty", "blank"])
def test_an_identity_without_a_cognito_user_id_is_refused(signin, caplog, sub):
    sfx = _suffix()
    count = signin.rows.count()
    signin.use(identity(f"nosub_{sfx}", sub, f"nosub.{sfx}@example.com"))

    response = _sign_in(signin, caplog)

    _assert_refused(signin, response, caplog, "no_sub", {}, count)


@pytest.mark.parametrize("email", [None, "", "   "], ids=["absent", "empty", "blank"])
def test_an_identity_with_no_email_is_refused(signin, caplog, email):
    sfx = _suffix()
    count = signin.rows.count()
    signin.use(identity(f"noemail_{sfx}", str(uuid.uuid4()), email))

    response = _sign_in(signin, caplog)

    _assert_refused(signin, response, caplog, "no_email", {}, count)


# --------------------------------------------------------------------------
# Case 5: a first sign-in needs a verified email address
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "email_verified", ["false", None, "", "1"], ids=["false", "absent", "empty", "one"]
)
def test_a_first_sign_in_needs_a_verified_email(signin, caplog, email_verified):
    sfx = _suffix()
    count = signin.rows.count()
    signin.use(
        identity(
            f"unverified_{sfx}",
            str(uuid.uuid4()),
            f"unverified.{sfx}@example.com",
            ["Developers"],
            email_verified=email_verified,
        )
    )

    response = _sign_in(signin, caplog)

    _assert_refused(signin, response, caplog, "email_unverified", {}, count)


def test_an_unverified_email_is_refused_before_the_address_is_looked_up(signin, caplog):
    """The cases are evaluated in order: with an address another account
    holds, an unverified email is still refused as ``email_unverified``, and
    the record names no row."""
    sfx = _suffix()
    held = f"held.{sfx}@example.com"
    holder = _add_user(
        signin.db,
        username=f"holder_{sfx}",
        email=held,
        hashed_password=HASHED_PASSWORD,
        role=UserRole.DEVELOPER,
    )
    before = {holder.id: signin.rows.snapshot(holder.id)}
    count = signin.rows.count()
    signin.use(identity(f"cog_{sfx}", str(uuid.uuid4()), held, email_verified="false"))

    response = _sign_in(signin, caplog)

    _assert_refused(signin, response, caplog, "email_unverified", before, count)
    (record,) = [r for r in caplog.records if r.name == SIGN_IN_LOGGER]
    assert record.row_id is None


@pytest.mark.parametrize("email_verified", ["true", "True"], ids=["true", "True"])
def test_a_first_sign_in_with_a_verified_email_gets_an_account(
    signin, caplog, email_verified
):
    """The control for the test above: the same identity, verified."""
    sfx = _suffix()
    sub = str(uuid.uuid4())
    count = signin.rows.count()
    signin.use(
        identity(
            f"verified_{sfx}",
            sub,
            f"verified.{sfx}@example.com",
            ["Developers"],
            email_verified=email_verified,
        )
    )

    response = _sign_in(signin, caplog)

    assert response.status_code == 200, response.text
    assert _reasons(caplog) == []
    assert signin.rows.count() == count + 1
    row_id = signin.rows.id_with_external_id(f"cognito:{sub}")
    assert str(row_id) == response.json()["id"]


@pytest.mark.parametrize("email_verified", ["false", None], ids=["false", "absent"])
def test_a_linked_account_signs_in_whatever_email_verified_says(
    signin, caplog, email_verified
):
    """The requirement is for a first sign-in only: an account already linked
    to the Cognito user is used as before."""
    sfx = _suffix()
    sub = str(uuid.uuid4())
    linked = _add_user(
        signin.db,
        username=f"kept_{sfx}",
        email=f"kept.{sfx}@example.com",
        external_id=f"cognito:{sub}",
        role=UserRole.DEVELOPER,
    )
    count = signin.rows.count()
    signin.use(
        identity(
            linked.username,
            sub,
            linked.email,
            ["Developers"],
            email_verified=email_verified,
        )
    )

    response = _sign_in(signin, caplog)

    assert response.status_code == 200, response.text
    assert response.json()["id"] == str(linked.id)
    assert _reasons(caplog) == []
    assert signin.rows.count() == count


# --------------------------------------------------------------------------
# Case 6: the address belongs to another account (row 12)
# --------------------------------------------------------------------------


def _email_taken_case(signin, caplog, holder_email: str, identity_email: str):
    sfx = _suffix()
    holder = _add_user(
        signin.db,
        username=f"holder_{sfx}",
        email=holder_email,
        hashed_password=HASHED_PASSWORD,
        role=UserRole.DEVELOPER,
    )
    before = {holder.id: signin.rows.snapshot(holder.id)}
    count = signin.rows.count()
    signin.use(identity(f"cog_{sfx}", str(uuid.uuid4()), identity_email, ["Admins"]))

    response = _sign_in(signin, caplog)

    _assert_refused(signin, response, caplog, "email_taken", before, count)
    return holder


@pytest.mark.parametrize("same_case", [True, False], ids=["same_case", "other_case"])
def test_an_identity_whose_email_another_account_holds_is_refused(
    signin, caplog, same_case
):
    """With the ``lower(email)`` index in place (main since #653)."""
    sfx = _suffix()
    held = f"Pat.X.{sfx}@Example.com"
    _email_taken_case(signin, caplog, held, held if same_case else held.lower())


def test_an_identity_whose_email_differs_only_in_case_is_refused_without_the_index(
    signin, caplog
):
    """The refusal is the sign-in's own check, not the database's: it holds
    with the ``lower(email)`` index dropped."""
    sfx = _suffix()
    held = f"Pat.Y.{sfx}@Example.com"
    with without_email_lower_index(signin.db) as added:
        try:
            _email_taken_case(signin, caplog, held, held.lower())
        finally:
            # Every row with the address -- including one a broken check
            # created -- goes, so the index can be re-created.
            signin.db.rollback()
            added.extend(
                row.id
                for row in signin.db.query(User.id).filter(
                    func.lower(User.email) == held.lower()
                )
            )


# --------------------------------------------------------------------------
# Case 1: the account linked to the sub (rows 7, 7b, 10)
# --------------------------------------------------------------------------


def test_a_second_sign_in_with_the_same_sub_reaches_the_same_account_after_a_rename(
    signin, caplog
):
    """Row 7. The account keeps the username it was created with: nothing is
    rewritten from Cognito."""
    sfx = _suffix()
    sub = str(uuid.uuid4())
    signin.use(identity(f"first_{sfx}", sub, f"first.{sfx}@example.com"))
    first = _sign_in(signin, caplog)
    assert first.status_code == 200, first.text
    count = signin.rows.count()

    signin.use(identity(f"renamed_{sfx}", sub, f"renamed.{sfx}@example.com"))
    second = _sign_in(signin, caplog)

    assert second.status_code == 200, second.text
    assert second.json()["id"] == first.json()["id"]
    assert signin.rows.count() == count
    assert second.json()["username"] == f"first_{sfx}"
    assert signin.rows.column(first.json()["id"], "email") == f"first.{sfx}@example.com"
    assert _reasons(caplog) == []


def test_the_sub_is_looked_up_before_the_username(signin, caplog):
    """Row 7b: an account with a password holds the identity's new username;
    the sign-in still reaches the account linked to its sub."""
    sfx = _suffix()
    sub = str(uuid.uuid4())
    linked = _add_user(
        signin.db,
        username=f"mine_{sfx}",
        email=f"mine.{sfx}@example.com",
        external_id=f"cognito:{sub}",
        role=UserRole.VIEWER,
    )
    _add_user(
        signin.db,
        username=f"theirs_{sfx}",
        email=f"theirs.{sfx}@example.com",
        hashed_password=HASHED_PASSWORD,
        role=UserRole.DEVELOPER,
    )
    signin.use(identity(f"theirs_{sfx}", sub, f"mine.{sfx}@example.com"))

    response = _sign_in(signin, caplog)

    assert response.status_code == 200, response.text
    assert response.json()["id"] == str(linked.id)
    assert _reasons(caplog) == []


def test_an_account_an_administrator_linked_signs_in_whatever_its_password(
    signin, caplog, monkeypatch
):
    """The documented link: an account with a password and ``external_id``
    set to ``cognito:<sub>`` is used for that Cognito user."""
    monkeypatch.setattr(settings, "SYNC_ROLES_ON_LOGIN", False)
    sfx = _suffix()
    sub = str(uuid.uuid4())
    linked = _add_user(
        signin.db,
        username=f"local_{sfx}",
        email=f"local.{sfx}@example.com",
        hashed_password=HASHED_PASSWORD,
        external_id=f"cognito:{sub}",
        role=UserRole.DEVELOPER,
    )
    before = signin.rows.snapshot(linked.id)
    signin.use(identity(f"someone_{sfx}", sub, f"other.{sfx}@example.com"))

    response = _sign_in(signin, caplog)

    assert response.status_code == 200, response.text
    assert response.json()["id"] == str(linked.id)
    assert signin.rows.snapshot(linked.id) == before


@pytest.mark.parametrize("sync", [True, False], ids=["sync_on", "sync_off"])
def test_a_linked_account_takes_its_role_from_the_groups_when_sync_is_on(
    signin, caplog, monkeypatch, sync
):
    """Row 10."""
    monkeypatch.setattr(settings, "SYNC_ROLES_ON_LOGIN", sync)
    sfx = _suffix()
    sub = str(uuid.uuid4())
    linked = _add_user(
        signin.db,
        username=f"viewer_{sfx}",
        email=f"viewer.{sfx}@example.com",
        external_id=f"cognito:{sub}",
        role=UserRole.VIEWER,
    )
    signin.use(identity(linked.username, sub, linked.email, ["Admins"]))

    response = _sign_in(signin, caplog)

    assert response.status_code == 200, response.text
    expected = ("ADMIN", True) if sync else ("VIEWER", False)
    assert (
        signin.rows.column(linked.id, "role"),
        signin.rows.column(linked.id, "is_superuser"),
    ) == expected


# --------------------------------------------------------------------------
# Case 8: the create's commit fails (row 15)
# --------------------------------------------------------------------------


def _insert_before_first_write(db: Session, rows: Rows, values: dict) -> None:
    """Commit ``values`` as a row from another connection just before the
    request's session first writes -- what a concurrent request that gets
    there first does.

    It runs before the session's first flush, not at its commit: the new
    account is flushed before the commit (its audit entry refers to it), and
    a row inserted from another connection after that would wait on this
    session's uncommitted row, which waits on the hook.
    """

    fired: List[bool] = []

    def insert(session, *_):
        if fired:
            return
        fired.append(True)
        with rows.engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO users (id, username, email, external_id, "
                    "is_active, is_superuser, role, created_at, updated_at) "
                    "VALUES (:id, :username, :email, :external_id, true, false, "
                    "'VIEWER', now(), now())"
                ),
                values,
            )

    event.listen(db, "before_flush", insert)


def test_two_first_sign_ins_of_one_identity_reach_the_same_account(signin, caplog):
    sfx = _suffix()
    sub = str(uuid.uuid4())
    winner = uuid.uuid4()
    _insert_before_first_write(
        signin.db,
        signin.rows,
        {
            "id": str(winner),
            "username": f"race_{sfx}",
            "email": f"race.{sfx}@example.com",
            "external_id": f"cognito:{sub}",
        },
    )
    count = signin.rows.count()
    signin.use(identity(f"race_{sfx}", sub, f"race.{sfx}@example.com"))

    response = _sign_in(signin, caplog)

    assert response.status_code == 200, response.text
    assert response.json()["id"] == str(winner)
    assert signin.rows.count() == count + 1  # the concurrent row, nothing else
    assert _reasons(caplog) == []
    assert not signin.db.new and not signin.db.dirty


def test_a_create_that_cannot_be_saved_is_refused(signin, caplog):
    """The concurrent row takes the username for another identity: the
    re-look-up finds nothing, so the sign-in is refused."""
    sfx = _suffix()
    other = uuid.uuid4()
    _insert_before_first_write(
        signin.db,
        signin.rows,
        {
            "id": str(other),
            "username": f"clash_{sfx}",
            "email": f"clash.other.{sfx}@example.com",
            "external_id": f"cognito:{uuid.uuid4()}",
        },
    )
    count = signin.rows.count()
    signin.use(identity(f"clash_{sfx}", str(uuid.uuid4()), f"clash.{sfx}@example.com"))

    response = _sign_in(signin, caplog)

    _assert_refused(
        signin, response, caplog, "commit_failed", {}, count + 1
    )  # +1: the concurrent row


# --------------------------------------------------------------------------
# Case 7: a value longer than its column (#709)
# --------------------------------------------------------------------------

#: The over-long values, 101 characters (one over the 100-character columns),
#: and 248 for the sub (``cognito:`` + 248 is one over ``external_id``'s 255).
_LONG = "L" * 101
_LONG_SUB = "s" * 248


def _long_identity(sfx: str, field: str) -> Dict[str, Any]:
    username = f"long_{sfx}"
    sub = str(uuid.uuid4())
    email = f"long.{sfx}@example.com"
    names = {"given_name": "Ada", "family_name": "Lee"}
    if field == "username":
        username = _LONG
    elif field == "email":
        email = f"{_LONG}@example.com"
    elif field == "first_name":
        names["given_name"] = _LONG
    elif field == "last_name":
        names["family_name"] = _LONG
    elif field == "external_id":
        sub = _LONG_SUB
    return identity(username, sub, email, ["Developers"], **names)


@pytest.mark.parametrize(
    "field", ["username", "email", "first_name", "last_name", "external_id"]
)
def test_an_identity_with_a_value_longer_than_its_column_is_refused(
    signin, caplog, field
):
    """Refused as ``field_too_long`` naming the column -- it was the insert
    failing and being recorded as ``commit_failed`` -- and the record never
    carries the over-long value."""
    sfx = _suffix()
    count = signin.rows.count()
    signin.use(_long_identity(sfx, field))

    response = _sign_in(signin, caplog)

    _assert_refused(signin, response, caplog, "field_too_long", {}, count)
    (record,) = [r for r in caplog.records if r.name == SIGN_IN_LOGGER]
    assert record.field == field
    over_long = _LONG_SUB if field == "external_id" else _LONG
    assert over_long not in record.getMessage()
    assert over_long not in repr(vars(record))


def test_an_identity_with_every_value_at_its_column_length_gets_an_account(
    signin, caplog
):
    """The boundary: 100 characters in each 100-character column is stored."""
    sfx = _suffix()
    username = f"edge_{sfx}".ljust(100, "u")
    local = f"e{sfx}"
    email = f"{local}@{'d' * (100 - len(local) - len('@.example.com'))}.example.com"
    assert len(username) == len(email) == 100
    count = signin.rows.count()
    signin.use(
        identity(
            username,
            str(uuid.uuid4()),
            email,
            ["Developers"],
            given_name="g" * 100,
            family_name="f" * 100,
        )
    )

    response = _sign_in(signin, caplog)

    assert response.status_code == 200, response.text
    assert _reasons(caplog) == []
    assert signin.rows.count() == count + 1
    row_id = response.json()["id"]
    assert signin.rows.column(row_id, "first_name") == "g" * 100
    assert signin.rows.column(row_id, "last_name") == "f" * 100


# --------------------------------------------------------------------------
# Errors that are not refusals
# --------------------------------------------------------------------------


def test_a_token_cognito_does_not_accept_creates_nothing(signin, caplog):
    count = signin.rows.count()
    signin.use(ValueError("NotAuthorizedException"))

    response = _sign_in(signin, caplog)

    _assert_generic_401(response)
    assert _reasons(caplog) == []
    assert signin.calls == ["a-token"]
    assert signin.rows.count() == count


def test_an_unexpected_error_is_rolled_back_and_answers_the_same_401(
    signin, caplog, monkeypatch
):
    """The catch-all: whatever the account lookup raises, the session is
    rolled back and the answer is the same 401, with no refusal record."""
    sfx = _suffix()
    sub = str(uuid.uuid4())
    linked = _add_user(
        signin.db,
        username=f"boom_{sfx}",
        email=f"boom.{sfx}@example.com",
        external_id=f"cognito:{sub}",
        role=UserRole.VIEWER,
    )
    before = signin.rows.snapshot(linked.id)

    def change_then_fail(db, user_data):
        row = db.query(User).filter(User.id == linked.id).one()
        row.role = UserRole.ADMIN
        raise RuntimeError("unexpected")

    monkeypatch.setattr(deps, "resolve_cognito_user", change_then_fail)
    signin.use(identity(linked.username, sub, linked.email))

    response = _sign_in(signin, caplog)

    _assert_generic_401(response)
    assert _reasons(caplog) == []
    assert not signin.db.dirty
    signin.db.commit()
    assert signin.rows.snapshot(linked.id) == before


def test_a_refusal_after_a_change_is_rolled_back(signin, caplog, monkeypatch):
    """The refusal handler rolls back: a resolver that changes a row and then
    refuses leaves nothing in the session, and nothing is saved by a later
    commit. (The real resolver decides every refusal before writing, so only
    a planted one reaches the handler with a change pending.)"""
    sfx = _suffix()
    sub = str(uuid.uuid4())
    linked = _add_user(
        signin.db,
        username=f"undo_{sfx}",
        email=f"undo.{sfx}@example.com",
        external_id=f"cognito:{sub}",
        role=UserRole.VIEWER,
    )
    before = {linked.id: signin.rows.snapshot(linked.id)}
    count = signin.rows.count()

    def change_then_refuse(db, user_data):
        row = db.query(User).filter(User.id == linked.id).one()
        row.role = UserRole.ADMIN
        row.is_superuser = True
        raise cognito_accounts.CognitoSignInRefused(
            cognito_accounts.REASON_LOCAL_PASSWORD, row_id=linked.id
        )

    monkeypatch.setattr(deps, "resolve_cognito_user", change_then_refuse)
    signin.use(identity(linked.username, sub, linked.email))

    response = _sign_in(signin, caplog)

    _assert_refused(signin, response, caplog, "local_password", before, count)


# --------------------------------------------------------------------------
# Harness 2: the real service through moto -- roles from the token's groups
# --------------------------------------------------------------------------


class Pool(NamedTuple):
    client: TestClient
    idp: Any
    pool_id: str
    app_client: str
    rows: Rows


@pytest.fixture
def pool(db_session, test_db, monkeypatch) -> Iterator[Pool]:
    """Cognito mode against one moto user pool, configured for this
    deployment; nothing stubs the service."""
    _blank_aws(monkeypatch)
    monkeypatch.setattr(settings, "AUTH_PROVIDER", "cognito")
    saved = _route_db_to(db_session)
    try:
        with mock_cognitoidp():
            idp = boto3.client("cognito-idp", region_name=REGION)
            pool_id = idp.create_user_pool(PoolName="ours")["UserPool"]["Id"]
            app_client = idp.create_user_pool_client(
                UserPoolId=pool_id,
                ClientName="app",
                ExplicitAuthFlows=[
                    "ALLOW_ADMIN_USER_PASSWORD_AUTH",
                    "ALLOW_REFRESH_TOKEN_AUTH",
                ],
            )["UserPoolClient"]["ClientId"]
            idp.create_group(GroupName="Admins", UserPoolId=pool_id)
            monkeypatch.setattr(deps.auth_service, "user_pool_id", pool_id)
            monkeypatch.setattr(deps.auth_service, "client_id", app_client)
            monkeypatch.setattr(deps.auth_service, "_client", idp)
            # /auth/me builds its own service from the environment.
            monkeypatch.setenv("COGNITO_USER_POOL_ID", pool_id)
            monkeypatch.setenv("COGNITO_CLIENT_ID", app_client)
            yield Pool(
                TestClient(app, raise_server_exceptions=False),
                idp,
                pool_id,
                app_client,
                Rows(test_db),
            )
    finally:
        _restore_overrides(saved)


def _pool_user(p: Pool, groups=(), verified: bool = True) -> str:
    """A Cognito user with an email address, verified unless ``verified`` is
    false (then the user has no ``email_verified`` attribute)."""
    username = f"ops{_suffix()}"
    attributes = [{"Name": "email", "Value": f"{username}@example.com"}]
    if verified:
        attributes.append({"Name": "email_verified", "Value": "true"})
    p.idp.admin_create_user(
        UserPoolId=p.pool_id,
        Username=username,
        UserAttributes=attributes,
        TemporaryPassword="Tmp-pass1!",
        MessageAction="SUPPRESS",
    )
    p.idp.admin_set_user_password(
        UserPoolId=p.pool_id, Username=username, Password=PASSWORD, Permanent=True
    )
    for group in groups:
        p.idp.admin_add_user_to_group(
            UserPoolId=p.pool_id, Username=username, GroupName=group
        )
    return username


def _pool_sign_in(p: Pool, username: str):
    token = p.idp.admin_initiate_auth(
        UserPoolId=p.pool_id,
        ClientId=p.app_client,
        AuthFlow="ADMIN_USER_PASSWORD_AUTH",
        AuthParameters={"USERNAME": username, "PASSWORD": PASSWORD},
    )["AuthenticationResult"]["AccessToken"]
    return p.client.get(
        "/api/v1/users/me", headers={"Authorization": f"Bearer {token}"}
    )


def _role(p: Pool, response) -> tuple:
    user_id = response.json()["id"]
    return (p.rows.column(user_id, "role"), p.rows.column(user_id, "is_superuser"))


def test_a_pool_user_in_the_admin_group_gets_an_administrator_account(
    pool, monkeypatch
):
    monkeypatch.setattr(settings, "SYNC_ROLES_ON_LOGIN", True)
    username = _pool_user(pool, ["Admins"])
    sub = pool.idp.admin_get_user(UserPoolId=pool.pool_id, Username=username)
    sub = next(a["Value"] for a in sub["UserAttributes"] if a["Name"] == "sub")

    response = _pool_sign_in(pool, username)

    assert response.status_code == 200, response.text
    assert _role(pool, response) == ("ADMIN", True)
    assert pool.rows.id_with_external_id(f"cognito:{sub}") is not None
    # Still in the group: a second sign-in keeps the role.
    again = _pool_sign_in(pool, username)
    assert again.json()["id"] == response.json()["id"]
    assert _role(pool, again) == ("ADMIN", True)


def test_a_cognito_user_gets_an_account_once_their_email_is_verified(pool, caplog):
    """Through the real service: no account while the user's email is not
    verified, then one at the first sign-in after it is."""
    username = _pool_user(pool, verified=False)
    record = pool.idp.admin_get_user(UserPoolId=pool.pool_id, Username=username)
    sub = next(a["Value"] for a in record["UserAttributes"] if a["Name"] == "sub")
    count = pool.rows.count()

    with caplog.at_level(logging.WARNING, logger=SIGN_IN_LOGGER):
        refused = _pool_sign_in(pool, username)

    _assert_generic_401(refused)
    assert _reasons(caplog) == ["email_unverified"]
    assert pool.rows.count() == count
    assert pool.rows.id_with_external_id(f"cognito:{sub}") is None

    pool.idp.admin_update_user_attributes(
        UserPoolId=pool.pool_id,
        Username=username,
        UserAttributes=[{"Name": "email_verified", "Value": "true"}],
    )
    response = _pool_sign_in(pool, username)

    assert response.status_code == 200, response.text
    assert pool.rows.count() == count + 1
    assert str(pool.rows.id_with_external_id(f"cognito:{sub}")) == response.json()["id"]


def test_a_linked_administrator_still_in_the_admin_group_stays_an_administrator(
    pool, db_session, monkeypatch
):
    """An account an administrator linked, whose user is in the admin group:
    with SYNC on, every sign-in keeps it ADMIN and superuser."""
    monkeypatch.setattr(settings, "SYNC_ROLES_ON_LOGIN", True)
    username = _pool_user(pool, ["Admins"])
    record = pool.idp.admin_get_user(UserPoolId=pool.pool_id, Username=username)
    sub = next(a["Value"] for a in record["UserAttributes"] if a["Name"] == "sub")
    linked = _add_user(
        db_session,
        username=f"first_admin_{_suffix()}",
        email=f"first.admin.{_suffix()}@example.com",
        hashed_password=HASHED_PASSWORD,
        external_id=f"cognito:{sub}",
        is_superuser=True,
        role=UserRole.ADMIN,
    )

    response = _pool_sign_in(pool, username)

    assert response.status_code == 200, response.text
    assert response.json()["id"] == str(linked.id)
    assert _role(pool, response) == ("ADMIN", True)


@pytest.mark.parametrize("sync", [True, False], ids=["sync_on", "sync_off"])
def test_leaving_the_admin_group_changes_the_role_at_the_next_token(
    pool, monkeypatch, sync
):
    monkeypatch.setattr(settings, "SYNC_ROLES_ON_LOGIN", sync)
    username = _pool_user(pool, ["Admins"])
    first = _pool_sign_in(pool, username)
    assert _role(pool, first) == ("ADMIN", True)

    pool.idp.admin_remove_user_from_group(
        UserPoolId=pool.pool_id, Username=username, GroupName="Admins"
    )
    second = _pool_sign_in(pool, username)

    assert second.status_code == 200, second.text
    assert second.json()["id"] == first.json()["id"]
    expected = ("VIEWER", False) if sync else ("ADMIN", True)
    assert _role(pool, second) == expected


def test_auth_me_answers_for_a_sign_in_that_users_me_refuses(pool, db_session, caplog):
    """The page tells operators to check with ``GET /api/v1/users/me``:
    ``/auth/me`` returns the pool's record and does not look at accounts."""
    username = _pool_user(pool)
    _add_user(
        db_session,
        username=username,
        email=f"local.{username}@example.com",
        hashed_password=HASHED_PASSWORD,
        role=UserRole.DEVELOPER,
    )
    token = pool.idp.admin_initiate_auth(
        UserPoolId=pool.pool_id,
        ClientId=pool.app_client,
        AuthFlow="ADMIN_USER_PASSWORD_AUTH",
        AuthParameters={"USERNAME": username, "PASSWORD": PASSWORD},
    )["AuthenticationResult"]["AccessToken"]
    headers = {"Authorization": f"Bearer {token}"}

    with caplog.at_level(logging.WARNING, logger=SIGN_IN_LOGGER):
        users_me = pool.client.get("/api/v1/users/me", headers=headers)
        auth_me = pool.client.get("/api/v1/auth/me", headers=headers)

    _assert_generic_401(users_me)
    assert _reasons(caplog) == ["local_password"]
    assert auth_me.status_code == 200, auth_me.text
    assert auth_me.json()["username"] == username
