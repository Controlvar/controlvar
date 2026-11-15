"""The invitation preview shows the invited address in full only to that account.

``GET /api/v1/workspaces/invites/{token}`` needs no sign-in. When the request
carries credentials for the account the invite was sent to (compared as
accepting compares it: trimmed, ASCII, without regard to case), ``email`` is
the address as stored; for everyone else -- no credentials, credentials that
do not resolve to an account, an inactive account, any other account including
a superuser or the workspace's owner -- it is masked, ``a•••@example.com``.
``inviter_username`` follows the same rule (null for anyone but the invitee);
``workspace_name`` is returned to everyone. Credentials never turn the
preview into a 401.

The requests carry real bearer tokens and go through the real authentication
dependencies: ``dependency_overrides`` on ``get_current_user`` would not reach
the preview's own optional-viewer dependency, so only ``get_db`` is overridden.
"""

from __future__ import annotations

import uuid
from typing import Dict, Iterator, Optional

import jwt
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, text
from sqlalchemy.orm import Session, sessionmaker

from backend.app.api import deps
from backend.app.core.config import settings
from backend.app.core.security import create_local_access_token
from backend.app.db.session import get_db as _session_get_db
from backend.app.main import app
from backend.app.models.user import User, UserRole
from modules.backend.app.services.workspace_service import (
    invite_email_for_viewer,
    mask_invite_email,
    workspace_service,
)

pytestmark = [pytest.mark.integration, pytest.mark.regression]

HASHED_PASSWORD = "$2b$12$EixZaYVK1fsbw1ZfbX3OXePaWxn96p36WQoeG6Lruj3vjPGga31lW"
PREVIEW = "/api/v1/workspaces/invites/{token}"


# ── Helpers ─────────────────────────────────────────────────────────────────


def _user(
    db: Session,
    email: Optional[str],
    *,
    role: UserRole = UserRole.VIEWER,
    is_superuser: bool = False,
    is_active: bool = True,
    username: Optional[str] = None,
) -> User:
    user = User(
        username=username or f"prev_{uuid.uuid4().hex[:10]}",
        email=email,
        full_name="Invite Preview",
        hashed_password=HASHED_PASSWORD,
        is_active=is_active,
        is_superuser=is_superuser,
        role=role,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def _local(s: str) -> str:
    return f"{s}.{uuid.uuid4().hex[:8]}"


@pytest.fixture
def client(db_session: Session) -> Iterator[TestClient]:
    """A client with a fresh session per request and NO authentication override."""
    factory = sessionmaker(
        bind=db_session.get_bind(), autocommit=False, autoflush=False
    )

    def override_get_db():
        session = factory()
        session.execute(text("SET search_path TO test_experimentation"))
        try:
            yield session
        finally:
            session.close()

    app.dependency_overrides[deps.get_db] = override_get_db
    app.dependency_overrides[_session_get_db] = override_get_db
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()


@pytest.fixture
def owner(db_session: Session) -> User:
    return _user(db_session, f"{_local('owner')}@corp-example.com", role=UserRole.ADMIN)


@pytest.fixture
def workspace(db_session: Session, owner: User):
    suffix = uuid.uuid4().hex[:8]
    return workspace_service.create_workspace(
        db_session, name=f"Preview {suffix}", slug=f"prev-{suffix}", owner_id=owner.id
    )


@pytest.fixture
def invited() -> str:
    return f"{_local('alice')}@corp-example.com"


@pytest.fixture
def invite(db_session: Session, workspace, owner: User, invited: str):
    return workspace_service.create_invite(
        db_session,
        workspace_id=workspace.id,
        email=invited,
        role="DEVELOPER",
        invited_by=owner.id,
    )


def _bearer(user: User, **kw) -> Dict[str, str]:
    return {"Authorization": f"Bearer {create_local_access_token(user, **kw)}"}


def _preview(client: TestClient, token: str, headers: Optional[Dict[str, str]] = None):
    return client.get(PREVIEW.format(token=token), headers=headers or {})


def _masked(email: str) -> str:
    return email[0] + "•••" + email[email.rfind("@") :]


def _assert_masked(resp, invited: str) -> None:
    """Masked address, and no inviter: both are for the invitee only."""
    assert resp.status_code == 200, resp.text
    assert resp.json()["email"] == _masked(invited)
    assert invited not in resp.text
    assert invited.split("@")[0] not in resp.text
    assert "inviter_username" in resp.json()
    assert resp.json()["inviter_username"] is None


# ── mask_invite_email and invite_email_for_viewer ───────────────────────────


@pytest.mark.unit
@pytest.mark.parametrize(
    "email, expected",
    [
        ("alice@example.com", "a•••@example.com"),
        ("Alice@Example.COM", "A•••@Example.COM"),
        ("a@b.co", "a•••@b.co"),
        ("x@y@z.com", "x•••@z.com"),
        ("@example.com", "•••"),
        ("no-at-sign", "•••"),
        ("", "•••"),
        ("  alice@example.com ", "a•••@example.com"),
        (None, "•••"),
    ],
    ids=[
        "plain",
        "case-kept",
        "one-letter",
        "last-at",
        "nothing-before-at",
        "no-at",
        "empty",
        "trimmed",
        "none",
    ],
)
def test_mask_invite_email(email, expected):
    assert mask_invite_email(email) == expected


@pytest.mark.unit
@pytest.mark.parametrize(
    "email",
    ["alice@example.com", "x@y@z.com", "no-at-sign", "", "a@b.co"],
)
def test_masking_a_masked_address_returns_it_unchanged(email):
    once = mask_invite_email(email)
    assert mask_invite_email(once) == once


@pytest.mark.unit
@pytest.mark.parametrize(
    "viewer, expected",
    [
        (None, "A•••@Example.com"),
        ("bob@example.com", "A•••@Example.com"),
        ("alice+x@example.com", "A•••@Example.com"),
        ("  ALICE@example.com ", "Alice@Example.com"),
        ("A•••@Example.com", "A•••@Example.com"),
    ],
    ids=["no-viewer", "other", "alias", "same-address-other-case", "masked-form"],
)
def test_invite_email_for_viewer(viewer, expected):
    assert invite_email_for_viewer("Alice@Example.com", viewer) == expected


# ── Who sees the address in full ────────────────────────────────────────────


def test_an_anonymous_preview_is_masked(client, workspace, invite, invited):
    resp = _preview(client, invite.token)

    _assert_masked(resp, invited)
    assert resp.json()["token"] == invite.token
    assert resp.json()["role"] == "DEVELOPER"
    # The workspace's name is for every holder of the link.
    assert resp.json()["workspace_name"] == workspace.name


def test_the_preview_is_not_stored_by_shared_caches(
    client, db_session, invite, invited
):
    invitee = _user(db_session, invited)

    for headers in ({}, _bearer(invitee)):
        resp = _preview(client, invite.token, headers)
        assert resp.status_code == 200, resp.text
        assert resp.headers["cache-control"] == "private, no-store"


def test_the_invitee_sees_the_address_as_stored(client, db_session, workspace, owner):
    """Compared without regard to case; returned as the invite stores it."""
    s = uuid.uuid4().hex[:8]
    stored_on_invite = f"alice.{s}@corp-example.com"
    invitee = _user(db_session, f"Alice.{s}@Corp-Example.com")
    invite = workspace_service.create_invite(
        db_session,
        workspace_id=workspace.id,
        email=stored_on_invite,
        role="VIEWER",
        invited_by=owner.id,
    )

    resp = _preview(client, invite.token, _bearer(invitee))

    assert resp.status_code == 200, resp.text
    assert resp.json()["email"] == stored_on_invite
    assert resp.json()["inviter_username"] == owner.username


@pytest.mark.parametrize(
    "who",
    ["other-account", "superuser", "workspace-owner"],
)
def test_any_other_account_sees_the_address_masked(
    client, db_session, owner, invite, invited, who
):
    if who == "other-account":
        viewer = _user(db_session, f"{_local('bob')}@corp-example.com")
    elif who == "superuser":
        viewer = _user(
            db_session,
            f"{_local('root')}@corp-example.com",
            role=UserRole.ADMIN,
            is_superuser=True,
        )
    else:
        viewer = owner

    _assert_masked(_preview(client, invite.token, _bearer(viewer)), invited)


def _wrong_signature_token(user: User) -> str:
    good = jwt.decode(
        create_local_access_token(user), options={"verify_signature": False}
    )
    return jwt.encode(good, "not-the-server-secret-" + "x" * 32, algorithm="HS256")


@pytest.mark.parametrize(
    "credentials",
    [
        "none",
        "basic",
        "empty-bearer",
        "garbage-bearer",
        "expired-token",
        "wrong-signature",
    ],
)
def test_credentials_that_do_not_resolve_still_get_a_masked_preview(
    client, db_session, invite, invited, credentials
):
    """Never a 401: the preview is answered without an account too."""
    invitee = _user(db_session, invited)
    headers = {
        "none": {},
        "basic": {"Authorization": "Basic YWxpY2U6c2VjcmV0"},
        "empty-bearer": {"Authorization": "Bearer "},
        "garbage-bearer": {"Authorization": "Bearer abc.def.ghi"},
        "expired-token": _bearer(invitee, expires_minutes=-5),
        "wrong-signature": {
            "Authorization": f"Bearer {_wrong_signature_token(invitee)}"
        },
    }[credentials]

    _assert_masked(_preview(client, invite.token, headers), invited)


def test_an_unknown_token_is_still_404(client, db_session, invited):
    invitee = _user(db_session, invited)
    assert _preview(client, "no-such-token").status_code == 404
    assert _preview(client, "no-such-token", _bearer(invitee)).status_code == 404


# ── Cognito-shaped sign-ins ─────────────────────────────────────────────────
#
# With AUTH_PROVIDER=local an inactive account is already refused inside
# get_current_user (400), so the preview's own is_active check is reached only
# on the Cognito path, which returns the linked row as it is. The Cognito call
# is stubbed; nothing here talks to AWS.


@pytest.fixture
def cognito(monkeypatch):
    """Switch to the Cognito path; ``cognito.returns(...)`` sets the stubbed user."""
    monkeypatch.setattr(settings, "AUTH_PROVIDER", "cognito")

    class _Stub:
        user_data: dict = {}

        def returns(self, username: str, email: str, sub: str) -> None:
            self.user_data = {
                "username": username,
                "attributes": {"sub": sub, "email": email, "email_verified": "true"},
                "groups": [],
            }

    stub = _Stub()
    monkeypatch.setattr(
        deps.auth_service, "get_user_with_groups", lambda token: stub.user_data
    )
    return stub


COGNITO_HEADERS = {"Authorization": "Bearer cognito-access-token"}


def _link(db: Session, user: User) -> str:
    """Link ``user`` to a Cognito user ID, as an administrator does; return it."""
    sub = str(uuid.uuid4())
    user.external_id = f"cognito:{sub}"
    db.commit()
    return sub


def test_an_active_invitee_signed_in_through_cognito_sees_the_address(
    client, db_session, cognito, invite, invited
):
    """The positive control for the two tests below: the stub does sign in."""
    invitee = _user(db_session, invited)
    cognito.returns(invitee.username, invited, _link(db_session, invitee))

    resp = _preview(client, invite.token, COGNITO_HEADERS)

    assert resp.status_code == 200, resp.text
    assert resp.json()["email"] == invited
    assert resp.json()["inviter_username"] is not None


def test_an_inactive_invitee_sees_the_address_masked(
    client, db_session, cognito, invite, invited
):
    invitee = _user(db_session, invited, is_active=False)
    cognito.returns(invitee.username, invited, _link(db_session, invitee))

    _assert_masked(_preview(client, invite.token, COGNITO_HEADERS), invited)


def test_a_refused_first_sign_in_still_gets_the_preview(
    client, db_session, cognito, invite, invited
):
    """A first Cognito sign-in whose address another account holds is refused.

    get_current_user then answers 401; the preview goes on with the same
    request session, so it has to be usable afterwards. 200, masked, and no
    row written.
    """
    _user(db_session, invited)
    new_username = f"cog_{uuid.uuid4().hex[:10]}"
    cognito.returns(new_username, invited, str(uuid.uuid4()))

    _assert_masked(_preview(client, invite.token, COGNITO_HEADERS), invited)

    db_session.expire_all()
    assert db_session.query(User).filter(User.username == new_username).count() == 0
    assert (
        db_session.query(User).filter(func.lower(User.email) == invited.lower()).count()
        == 1
    )


# ── The workspace and inviter names ─────────────────────────────────────────


def test_the_invitee_sees_the_workspace_and_inviter_names(
    client, db_session, workspace, owner, invite, invited
):
    invitee = _user(db_session, invited)

    resp = _preview(client, invite.token, _bearer(invitee))

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["workspace_name"] == workspace.name
    assert body["workspace_name"] != workspace.slug
    assert body["inviter_username"] == owner.username
    assert body["email"] == invited
    assert body["accepted_at"] is None


def test_creating_an_invite_returns_the_link_details(client, workspace, owner):
    """POST answers the creator with the token, the address in full and both names."""
    address = f"{_local('carol')}@corp-example.com"

    resp = client.post(
        f"/api/v1/workspaces/{workspace.id}/invites",
        json={"email": address, "role": "ANALYST"},
        headers=_bearer(owner),
    )

    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["email"] == address
    assert body["role"] == "ANALYST"
    assert body["token"]
    assert body["workspace_name"] == workspace.name
    assert body["inviter_username"] == owner.username


def test_an_invite_whose_inviter_was_deleted_names_no_inviter(
    client, db_session, workspace, invited
):
    """``invited_by`` becomes NULL when that user is deleted; still 200."""
    inviter = _user(db_session, f"{_local('gone')}@corp-example.com")
    invite = workspace_service.create_invite(
        db_session,
        workspace_id=workspace.id,
        email=invited,
        role="VIEWER",
        invited_by=inviter.id,
    )
    db_session.delete(inviter)
    db_session.commit()
    invitee = _user(db_session, invited)

    resp = _preview(client, invite.token, _bearer(invitee))

    assert resp.status_code == 200, resp.text
    assert resp.json()["email"] == invited
    assert resp.json()["inviter_username"] is None
    assert resp.json()["workspace_name"] == workspace.name


def test_an_invite_to_a_deleted_workspace_is_404(
    client, db_session, workspace, invite, invited
):
    """Characterisation, not a guard: deleting a workspace deletes its invites."""
    invitee = _user(db_session, invited)
    workspace_service.delete_workspace(db_session, workspace.id)

    assert _preview(client, invite.token, _bearer(invitee)).status_code == 404


# ── The OpenAPI document ────────────────────────────────────────────────────


def test_the_preview_is_documented_as_sign_in_optional():
    """The bearer scheme plus an empty requirement: credentials are optional."""
    op = app.openapi()["paths"]["/api/v1/workspaces/invites/{token}"]["get"]
    assert op["security"] == [{"OAuth2PasswordBearer": []}, {}]
