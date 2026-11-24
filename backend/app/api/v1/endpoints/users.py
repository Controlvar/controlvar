"""
User management endpoints.

This module provides API endpoints for user management operations
such as creating, retrieving, updating, and deleting users.
"""

import uuid
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from pydantic import EmailStr, SecretStr, TypeAdapter, ValidationError
from sqlalchemy import func, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from backend.app.api import deps
from backend.app.api.off_loop_route import OffLoopRoute
from backend.app.api.query_params import MAX_OFFSET, describe_as_int64
from backend.app.api.two_factor_answers import (
    ALREADY_ENABLED_DETAIL,
    NO_PENDING_SECRET_DETAIL,
    NOT_ENABLED_DETAIL,
    REFUSALS,
    TWO_FACTOR_REQUIRED_FOR_ROLE,
    refusal_answer,
)
from backend.app.api.v1.endpoints.auth import user_to_me
from backend.app.core.config import settings
from backend.app.core.security import (
    create_local_access_token,
    get_password_hash,
    unwrap_secret,
)
from backend.app.core.two_factor_policy import two_factor_required
from backend.app.models.audit_log import ActionType, EntityType
from backend.app.models.user import User, UserRole
from backend.app.schemas.auth import LoginResponse
from backend.app.schemas.two_factor import (
    RecoveryCodesResponse,
    TwoFactorCodeRequest,
    TwoFactorDisableRequest,
    TwoFactorEnrolRequest,
    TwoFactorEnrolResponse,
    TwoFactorStatusResponse,
)
from backend.app.schemas.user import (
    PasswordChange,
    UserCreate,
    UserListResponse,
    UserResponse,
    UserUpdate,
    check_password_strength,
)
from backend.app.services.audit_service import (
    AuditActor,
    AuditService,
    audit_identity,
    audit_snapshot,
    role_value,
)
from backend.app.services.local_auth_service import (
    AccountLockedError,
    CurrentPasswordMissingError,
    InvalidCredentialsError,
    NoLocalPasswordError,
    account_may_sign_in,
    local_auth_service,
)
from backend.app.services.two_factor_service import (
    UNAVAILABLE_TEXT,
    WRONG_CODE_TEXT,
    two_factor_service,
)

router = APIRouter(route_class=OffLoopRoute)

#: The answer when a non-superuser's request would change an account's email
#: address or username. An administrator changes those through
#: ``/api/v1/admin/users/{id}``.
ADMIN_ONLY_IDENTITY_DETAIL = (
    "Only an administrator can change an account's email address or username."
)

#: The answer when a request to ``PUT /users/{id}`` or ``PUT /admin/users/{id}``
#: would set the caller's OWN password. Those routes do not ask for the
#: current password, so this is refused for every caller, superusers included.
OWN_PASSWORD_DETAIL = (
    "To change your own password, use POST /api/v1/users/me/password, "
    "which asks for your current password."
)

#: Refusals when a superuser's request would change their OWN account's role,
#: superuser access or active status. ``PATCH /admin/users/{id}`` gives the
#: first and the last; both PUT routes give the last two
#: (``refuse_if_own_access_removed``). The dashboard shows them as they are,
#: so they are written for the person at the screen.
OWN_ROLE_REFUSED = "You can't change your own role. Ask another administrator to do it."
OWN_SUPERUSER_REFUSED = (
    "You can't remove your own superuser access. Ask another administrator to do it."
)
OWN_DEACTIVATION_REFUSED = "You can't deactivate your own account."

#: Answers of ``POST /api/v1/users/me/password``.
CURRENT_PASSWORD_INCORRECT_DETAIL = "The current password is incorrect."
CURRENT_PASSWORD_MISSING_DETAIL = (
    "Enter your current password (current_password) to set a new one."
)
NO_LOCAL_PASSWORD_DETAIL = (
    "This account does not sign in with a password, so it has no password to change."
)

#: Answers of ``POST /api/v1/users/me/2fa/enroll``, which asks for the current
#: password as ``/me/password`` does; a wrong one answers
#: ``CURRENT_PASSWORD_INCORRECT_DETAIL`` on both. The dashboard matches these
#: texts (``frontend/src/services/twoFactor.ts``).
TWO_FACTOR_PASSWORD_MISSING_DETAIL = (
    "Enter your current password (current_password) to set up two-step sign-in."
)
TWO_FACTOR_NO_PASSWORD_DETAIL = (
    "This account does not sign in with a password, so two-step sign-in does "
    "not apply to it."
)
LOCAL_PROVIDER_ONLY_DETAIL = "Endpoint not available: AUTH_PROVIDER is not 'local'"

#: The answer when another account already holds the email address, in any
#: letter case. The dashboard matches this text; keep it as it is.
EMAIL_TAKEN_DETAIL = "Email already registered"

#: The answer when another account already has the username (exact match, as
#: the unique constraint compares it).
USERNAME_TAKEN_DETAIL = "Username already registered"

#: The answer of GET (a superuser's), PUT and DELETE /users/{user_id} when the
#: id is not a UUID, ``me`` included (#959).
INVALID_USER_ID_DETAIL = "Invalid user ID format"

#: SQLSTATE ``unique_violation``.
_UNIQUE_VIOLATION = "23505"

#: The same parser ``UserUpdate.email`` applies to the request body.
_EMAIL = TypeAdapter(EmailStr)


def _user_uuid(user_id: str) -> uuid.UUID:
    """The path's ``{user_id}`` as a UUID, or 400 ``INVALID_USER_ID_DETAIL``.

    Every id is parsed, so one that is not a UUID never reaches the query: an
    id without a ``-`` used to go to it as text, and the database's refusal
    answered 500 (#959). The dashes stay optional, as they were.
    """
    try:
        return uuid.UUID(user_id)
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=INVALID_USER_ID_DETAIL
        )


def _parsed_email(value: Optional[str]) -> Optional[str]:
    """``value`` as ``UserUpdate.email`` would parse it; None if it cannot be."""
    if value is None:
        return None
    try:
        return _EMAIL.validate_python(value)
    except ValidationError:
        return None


def _identity_unchanged(user: User, user_in: UserUpdate) -> bool:
    """True when the request resends the stored email address and username.

    The request's email has been through EmailStr, which lower-cases the
    domain (among other things) but keeps the case of the local part. The
    stored value is put through the same parser and the two are compared
    as they are: no lower-casing or other folding of either side, so a change
    of case in the local part, or a look-alike character, is a change. A
    stored address that is missing or cannot be parsed never matches. The
    username is compared exactly.
    """
    stored_email = _parsed_email(user.email)
    return (
        stored_email is not None
        and stored_email == user_in.email
        and user_in.username == user.username
    )


def apply_password_change(
    target: User, caller: User, update_data: Dict[str, Any]
) -> None:
    """Turn a ``password`` in *update_data* into ``hashed_password``, or refuse it.

    Shared by ``PUT /api/v1/users/{id}`` and ``PUT /api/v1/admin/users/{id}``;
    the caller has already been authorised to update *target*. Mutates
    *update_data*: ``password`` is always removed.

    * absent or ``null``: nothing changes;
    * *target* is the *caller*: 403, for EVERY caller including superusers.
      Your own password is changed with ``POST /api/v1/users/me/password``,
      which asks for the current one;
    * otherwise (only a superuser gets this far): the password rule is
      applied again and the password is hashed.

    ``get_password_hash`` is looked up in this module's namespace, so the
    tests that patch ``backend.app.api.v1.endpoints.users.get_password_hash``
    see both routes.
    """
    if "password" not in update_data:
        return
    password = update_data.pop("password")
    if password is None:
        return
    if str(target.id) == str(caller.id):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=OWN_PASSWORD_DETAIL,
        )
    plain = unwrap_secret(password)
    # ``UserUpdate`` has already applied the rule; applying it again here
    # keeps this function correct for a caller that builds *update_data* some
    # other way. The messages are fixed and carry no part of the value.
    try:
        check_password_strength(plain)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        ) from None
    update_data["hashed_password"] = get_password_hash(plain)


def refuse_if_own_access_removed(
    target: User, caller: User, update_data: Dict[str, Any]
) -> None:
    """Refuse a request that takes away the caller's OWN superuser access or
    deactivates the caller's OWN account, with 400 (#652).

    Shared by ``PUT /api/v1/users/{id}`` and ``PUT /api/v1/admin/users/{id}``.
    Call it after ``apply_password_change`` (and, on the users route, after a
    non-superuser's ``is_superuser``/``is_active`` have been dropped) and
    before anything is set on *target*, so a refused request writes nothing.

    A key counts only when its value differs from the stored one: resending
    the stored values (a client that GETs the account and PUTs it back) is
    not refused. When both would change, the superuser text is the answer.
    Another account is not affected: one superuser can still demote or
    deactivate another, and that is how access is removed.
    """
    if str(target.id) != str(caller.id):
        return
    if (
        "is_superuser" in update_data
        and update_data["is_superuser"] != target.is_superuser
    ):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=OWN_SUPERUSER_REFUSED
        )
    if "is_active" in update_data and update_data["is_active"] != target.is_active:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=OWN_DEACTIVATION_REFUSED
        )


#: How long a request waits for another request's lock on the superuser rows
#: (``lock_active_superusers``) before it gives up with an error (a 500). The
#: API runs these handlers on one worker, so a wait holds every other request
#: for as long as it lasts; this bounds it.
SUPERUSER_LOCK_TIMEOUT = "5s"


def lock_active_superusers(db: Session) -> set:
    """Lock every active superuser row, in id order, and return their ids.

    Two superusers deactivating each other at the same moment would otherwise
    both succeed and leave no active superuser. Taking the rows in one order
    makes the second request wait for the first, and then see its result: a
    caller the first request deactivated is no longer in the set. Held until
    the caller's commit or rollback. A wait longer than
    ``SUPERUSER_LOCK_TIMEOUT`` ends the request with a database error.
    """
    # The value is the module constant above, never request data.
    db.execute(text(f"SET LOCAL lock_timeout = '{SUPERUSER_LOCK_TIMEOUT}'"))  # nosemgrep: python.sqlalchemy.security.audit.avoid-sqlalchemy-text.avoid-sqlalchemy-text  # fmt: skip
    rows = (
        db.query(User.id)
        .filter(User.is_superuser.is_(True), User.is_active.is_(True))
        .order_by(User.id)
        .with_for_update()
        .all()
    )
    return {row.id for row in rows}


def require_still_active_superuser(db: Session, current_user: User) -> None:
    """Lock the active superuser rows and refuse a caller no longer among them.

    Called before a write that takes an active superuser out of that set
    (demoting, deactivating or deleting one), with no ``await`` between this
    call and the commit. Two superusers doing that to each other at once then
    run one after the other, and the second sees the first one's result, so
    they cannot leave no active superuser between them (#652).

    A caller who is not in the set is read again, after the lock, with a
    column query: the request's session already holds the caller's object
    from sign-in, and ``db.get`` or ``query(User).first()`` would return that
    stale object. The texts are the ones sign-in uses: 400 "Inactive user"
    for a deactivated caller, and also (a choice) for a caller whose row is
    gone; 403 "Not enough permissions" for a caller who is active but no
    longer a superuser.
    """
    if current_user.id in lock_active_superusers(db):
        return
    row = (
        db.query(User.is_active, User.is_superuser)
        .filter(User.id == current_user.id)
        .one_or_none()
    )
    if row is None or not row.is_active:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Inactive user"
        )
    # Active but not in the locked set: no longer a superuser. (Promoted
    # again after the lock is answered the same way: the row is not locked.)
    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN, detail="Not enough permissions"
    )


def removes_an_active_superuser(target: User, update_data: Dict[str, Any]) -> bool:
    """Whether a PUT's *update_data* takes *target* out of the active
    superusers: *target* is one, and the write clears ``is_superuser`` or
    ``is_active``."""
    if not (target.is_superuser and target.is_active):
        return False
    return any(
        key in update_data and not update_data[key]
        for key in ("is_superuser", "is_active")
    )


def email_held_by_another(
    db: Session, email: Optional[str], exclude_id: Any = None
) -> bool:
    """True when an account other than ``exclude_id`` holds ``email`` in any case.

    Both sides are lower-cased by PostgreSQL's ``lower()``, never by Python's
    ``str.lower()``: the two disagree outside ASCII, and the database is what
    decides whether two addresses collide.
    """
    if email is None:
        return False
    query = db.query(User.id).filter(func.lower(User.email) == func.lower(email))
    if exclude_id is not None:
        query = query.filter(User.id != exclude_id)
    return query.first() is not None


def refuse_if_email_held(
    db: Session, email: Optional[str], exclude_id: Any = None
) -> None:
    """409 ``EMAIL_TAKEN_DETAIL`` when another account holds ``email``."""
    if email_held_by_another(db, email, exclude_id):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail=EMAIL_TAKEN_DETAIL
        )


def changed_email(user: User, update_data: Dict[str, Any]) -> Optional[str]:
    """The email address an update sets, or None if it leaves it as stored.

    The request's email has been through EmailStr, which lower-cases the
    domain, so it is compared with the stored value put through the same
    parser (as ``_identity_unchanged`` does), not with the raw column. An
    address resent as stored is not a change: it is not checked, and it is
    removed from *update_data* so the row keeps its bytes. So an account whose
    address another account already shares (rows created by an
    administrator, SQL or a restore) can still be edited, whatever the case
    of its stored domain.
    """
    email = update_data.get("email")
    if email is None:
        return None
    if email == _parsed_email(user.email):
        update_data.pop("email")
        return None
    return email


def username_held_by_another(
    db: Session, username: Optional[str], exclude_id: Any = None
) -> bool:
    """True when an account other than ``exclude_id`` has exactly ``username``."""
    if username is None:
        return False
    query = db.query(User.id).filter(User.username == username)
    if exclude_id is not None:
        query = query.filter(User.id != exclude_id)
    return query.first() is not None


def refuse_if_username_held(
    db: Session, username: Optional[str], exclude_id: Any = None
) -> None:
    """409 ``USERNAME_TAKEN_DETAIL`` when another account has ``username``."""
    if username_held_by_another(db, username, exclude_id):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail=USERNAME_TAKEN_DETAIL
        )


def changed_username(user: User, update_data: Dict[str, Any]) -> Optional[str]:
    """The username an update sets, or None if it leaves it as stored."""
    username = update_data.get("username")
    if username is None or username == user.username:
        return None
    return username


def commit_user_write(
    db: Session,
    email: Optional[str],
    exclude_id: Any = None,
    username: Optional[str] = None,
) -> None:
    """Commit a write to ``users``; a unique violation on the email becomes 409.

    The pre-check in each route cannot see a row committed between the check
    and this commit. When the commit fails with a unique violation, the
    transaction is rolled back and the same email query is run again
    (excluding the row being updated), then the username query. A hit on the
    email answers 409 ``EMAIL_TAKEN_DETAIL``, a hit on the username 409
    ``USERNAME_TAKEN_DETAIL``; anything else is raised as it was. The decision does not
    depend on the name of any index, which differs from one schema to another.
    The exception's text is neither logged nor returned: it carries the
    values of the row.

    ``email`` is the address this write sets, or None when it sets none;
    ``username`` likewise.
    """
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        orig = exc.orig
        code = getattr(orig, "pgcode", None) or getattr(orig, "sqlstate", None)
        if code == _UNIQUE_VIOLATION and email_held_by_another(db, email, exclude_id):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT, detail=EMAIL_TAKEN_DETAIL
            ) from None
        if code == _UNIQUE_VIOLATION and username_held_by_another(
            db, username, exclude_id
        ):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT, detail=USERNAME_TAKEN_DETAIL
            ) from None
        raise


def record_access_changes(
    db: Session,
    actor: Any,
    user: User,
    role_before: Dict[str, Any],
    active_before: Optional[bool],
) -> None:
    """Add the audit entries for a change to a user's role, superuser flag or
    active status, in the caller's transaction (both are kept or neither is).

    ``role_before`` is ``role_value(user)`` and ``active_before`` the user's
    ``is_active``, both read before the change. A changed role or superuser
    flag adds one ``role_assign`` with ``{role, is_superuser}`` before and
    after; a changed active status adds ``user_activate`` or
    ``user_deactivate``. Nothing changed, nothing added.
    """
    name = user.username or str(user.id)
    role_after = role_value(user)
    if role_after != role_before:
        AuditService.record(
            db,
            actor=actor,
            action=ActionType.ROLE_ASSIGN,
            entity_type=EntityType.USER,
            entity_id=user.id,
            entity_name=name,
            before=role_before,
            after=role_after,
        )
    if bool(user.is_active) != bool(active_before):
        AuditService.record(
            db,
            actor=actor,
            action=(
                ActionType.USER_ACTIVATE
                if user.is_active
                else ActionType.USER_DEACTIVATE
            ),
            entity_type=EntityType.USER,
            entity_id=user.id,
            entity_name=name,
            before={"is_active": bool(active_before)},
            after={"is_active": bool(user.is_active)},
        )


def require_local_provider() -> None:
    """404 unless ``AUTH_PROVIDER`` is ``local``.

    A route dependency, so it answers before authentication and before the
    body is validated against the schema.
    """
    if settings.AUTH_PROVIDER != "local":
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=LOCAL_PROVIDER_ONLY_DETAIL,
        )


@router.get("/", response_model=UserListResponse)
async def list_users(
    db: Session = Depends(deps.get_db),
    current_user: User = Depends(deps.get_current_active_user),
    skip: int = Query(0, ge=0, le=MAX_OFFSET, json_schema_extra=describe_as_int64),
    limit: int = Query(100, ge=1, le=100),
):
    """
    List users.

    For superusers: retrieves all users
    For regular users: retrieves only their own user
    """
    if current_user.is_superuser:
        # Superusers can see all users. Ordered newest first: a paginated query
        # with no ORDER BY can repeat or drop rows between pages, and a just-
        # created account should be on page one.
        # (No `.all()`: the unit tests mock the query chain and return a list.)
        query = db.query(User).order_by(User.created_at.desc(), User.id)
        query_with_offset = query.offset(skip)
        users = query_with_offset.limit(limit)
        total = db.query(User).count()
    else:
        # Regular users can only see themselves
        users = [current_user]
        total = 1

    # `UserResponse` reads the ORM objects directly (`from_attributes`), so the
    # response cannot silently lose a field the way a hand-built dict did: the
    # role was missing from that dict, and because `role` is optional every
    # user came back with `"role": null`.
    return UserListResponse(items=users, total=total, skip=skip, limit=limit)


@router.post("/", response_model=None, status_code=status.HTTP_201_CREATED)
async def create_user(
    user_in: UserCreate,
    db: Session = Depends(deps.get_db),
    current_user: User = Depends(deps.get_current_active_user),
):
    """
    Create new user.

    Only superusers can create new users.
    """
    # Check if user has permission to create users
    if not current_user.is_superuser:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Not enough permissions",
        )

    # Check if username or email already exists. The email is refused when
    # another account holds it in any letter case.
    refuse_if_email_held(db, user_in.email)

    refuse_if_username_held(db, user_in.username)

    # Create new user.  ``UserCreate.password`` is a ``SecretStr``; bcrypt
    # needs the plain text behind it.
    hashed_password = get_password_hash(unwrap_secret(user_in.password))
    user_id = uuid.uuid4()

    # Create the user with required fields
    user_data = {
        "id": user_id,
        "username": user_in.username,
        "email": user_in.email,
        "hashed_password": hashed_password,
        "full_name": user_in.full_name,
        "is_active": user_in.is_active,
        "is_superuser": user_in.is_superuser,
    }
    if user_in.role is not None:
        # ``UserCreate.role`` is the upper-case enum *name* (ADMIN, ...); the
        # column stores the ``UserRole`` member.
        user_data["role"] = UserRole[user_in.role]

    user = User(**user_data)
    db.add(user)
    commit_user_write(db, user_in.email, username=user_in.username)
    db.refresh(user)

    # The account is committed; a failed audit write does not undo it.
    AuditService.record_after_commit(
        db,
        actor=current_user,
        action=ActionType.USER_CREATE,
        entity_type=EntityType.USER,
        entity_id=user.id,
        entity_name=user.username or str(user.id),
        after=audit_identity(audit_snapshot(EntityType.USER, user)),
    )
    db.refresh(user)

    role = getattr(user, "role", None)

    # Return response directly without schema validation
    # This is necessary to work with the testing mock expectations
    return {
        "id": str(user.id),
        "username": user.username,
        "email": user.email,
        "full_name": user.full_name,
        "is_active": user.is_active,
        "is_superuser": user.is_superuser,
        "role": role.name if isinstance(role, UserRole) else "VIEWER",
        "created_at": user.created_at,
        "updated_at": user.updated_at,
    }


@router.get("/me", response_model=UserResponse)
async def get_user_me(
    current_user: User = Depends(deps.get_current_active_user),
):
    """
    Get current user.
    """
    # ``role`` falls back to VIEWER for a row with none, as ``create_user``
    # and ``GET /auth/me`` do.
    role = getattr(current_user, "role", None)
    # Ensure the response conforms to the UserResponse schema
    response_data = {
        "id": current_user.id,
        "username": current_user.username,
        "email": current_user.email,
        "full_name": current_user.full_name,
        "is_active": current_user.is_active,
        "is_superuser": current_user.is_superuser,
        "role": role.name if isinstance(role, UserRole) else "VIEWER",
        "created_at": current_user.created_at,
        "updated_at": current_user.updated_at,
        "totp_enabled": current_user.totp_enabled,
    }

    # Add optional fields if they exist
    if hasattr(current_user, "last_login"):
        response_data["last_login"] = current_user.last_login
    if hasattr(current_user, "preferences"):
        response_data["preferences"] = current_user.preferences

    return UserResponse(**response_data)


def _check_own_password(
    db: Session,
    user: User,
    password: Optional[SecretStr],
    *,
    missing_detail: str,
    no_password_detail: str,
) -> None:
    """Raise unless ``password`` is ``user``'s current password.

    ``local_auth_service.verify_current_password`` as HTTP, in its order: 423
    with ``Retry-After`` while the address is locked; 403 ``no_password_detail``
    for an account with no password of its own; 403 ``missing_detail`` when no
    password, or an empty one, was sent; 403
    ``CURRENT_PASSWORD_INCORRECT_DETAIL`` for a wrong one, which counts toward
    the same lockout as sign-in. Only the wrong password is counted.
    """
    current = unwrap_secret(password) if password is not None else None
    try:
        local_auth_service.verify_current_password(db, user, current)
    except AccountLockedError as exc:
        raise HTTPException(
            status_code=status.HTTP_423_LOCKED,
            detail=(
                "Too many failed attempts; account temporarily locked. "
                f"Retry in {exc.retry_after_seconds} seconds."
            ),
            headers={"Retry-After": str(exc.retry_after_seconds)},
        )
    except NoLocalPasswordError:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail=no_password_detail
        )
    except CurrentPasswordMissingError:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail=missing_detail
        )
    except InvalidCredentialsError:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=CURRENT_PASSWORD_INCORRECT_DETAIL,
        )


def _signs_in_with_password_alone(db: Session, user: User) -> bool:
    """Whether a sign-in with the password would issue ``user`` an access token
    at once: it may sign in, it has no two-step sign-in on, its role does not
    require it, and its email address is not NULL, because sign-in finds an
    account by its address. It needs no username (#2067). Read from the row as
    the database holds it after the change.

    The first three are what sign-in (``auth._issue_local_login``) applies
    once the password matches. Of the address this asks only that it is not
    NULL; sign-in refuses an empty one as well, so the two rules are close and
    not the same. ``test_password_change_ends_sessions.py`` compares the two
    answers for each kind of account it lists.
    """
    return (
        account_may_sign_in(user)
        and not two_factor_service.is_enabled(db, user.id)
        and not two_factor_required(user)
        and user.email is not None
    )


@router.post(
    "/me/password",
    response_model=LoginResponse,
    dependencies=[Depends(require_local_provider)],
    responses={
        200: {
            "description": (
                "The password is changed. A new access token for this account, "
                "as POST /api/v1/auth/login answers: every token issued before "
                "the change is refused, the one that sent this request included."
            )
        },
        204: {
            "description": (
                "The password is changed and every session of the account has "
                "ended, this one too. The answer for an account that the "
                "password alone does not sign in, such as one with two-step "
                "sign-in on or required for its role: sign in again with the "
                "new password."
            )
        },
        403: {
            "description": (
                "The current password is missing or incorrect, or the account "
                "has no password of its own."
            )
        },
        404: {"description": "AUTH_PROVIDER is not 'local'."},
        423: {
            "description": (
                "Too many failed attempts for this account (counted together "
                "with sign-in); see Retry-After."
            )
        },
    },
)
def change_own_password(
    body: PasswordChange,
    db: Session = Depends(deps.get_db),
    current_user: User = Depends(deps.get_current_active_user),
) -> Any:
    """
    Change your own password.

    Requires your current password. The new password must be at least 8
    characters and at most 72 bytes (UTF-8), with an upper-case letter, a
    lower-case letter and a digit.

    A wrong current password answers 403 (not 401, which would read as a
    signed-out session) and counts toward the same lockout as sign-in; after
    too many failures both answer 423 until ``Retry-After`` has passed.

    Changing the password signs out every session of the account: every
    access token issued before the change answers 401 from then on, the one
    that sent this request included, and a sign-in waiting for its code
    starts again. An account that signs in with its password alone gets 200
    with a new access token and the profile, as ``POST /api/v1/auth/login``
    answers; use that token from then on. Any other account, such as one with
    two-step sign-in on or required for its role, gets 204 with no body and
    signs in again with the new password.
    """
    _check_own_password(
        db,
        current_user,
        body.current_password,
        missing_detail=CURRENT_PASSWORD_MISSING_DETAIL,
        no_password_detail=NO_LOCAL_PASSWORD_DETAIL,
    )

    user = db.query(User).filter(User.id == current_user.id).first()
    if user is None:
        # The row went away between authentication and here.
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="User not found"
        )
    user.hashed_password = get_password_hash(unwrap_secret(body.new_password))
    db.commit()
    # The row as the database holds it after the change: the new token's
    # stamp is computed from the new hash, and the gate reads the account as
    # sign-in would now.
    db.refresh(user)
    if not _signs_in_with_password_alone(db, user):
        return Response(status_code=status.HTTP_204_NO_CONTENT)
    answer = LoginResponse(
        access_token=create_local_access_token(user),
        token_type="bearer",
        expires_in=settings.LOCAL_AUTH_TOKEN_TTL_MINUTES * 60,
        user=user_to_me(user),
    )
    # A new access token is a sign-in: recorded as one, after the change has
    # been committed (a failed audit write does not undo it). The 204 answer
    # issues nothing and records nothing.
    AuditService.record_after_commit(
        db,
        actor=user,
        action=ActionType.USER_LOGIN,
        entity_type=EntityType.USER,
        entity_id=user.id,
        entity_name=user.username or str(user.id),
        after={"provider": "local", "via": "password_change"},
    )
    return answer


# ---------------------------------------------------------------------------
# Your own two-step sign-in (#1812). Local provider only; any role.
# ---------------------------------------------------------------------------

_TWO_FACTOR_LOCAL_ONLY = {404: {"description": "AUTH_PROVIDER is not 'local'."}}
_TWO_FACTOR_CODE_REFUSALS = {
    403: {"description": f"{WRONG_CODE_TEXT} (wrong, used or malformed)"},
    423: {
        "description": (
            "Too many failed attempts for this account, passwords and codes "
            "counted together (see Retry-After); or two-step sign-in is locked "
            "after too many wrong codes until an administrator resets it"
        )
    },
    503: {"description": UNAVAILABLE_TEXT},
}


def _client_host(request: Request) -> Optional[str]:
    return request.client.host if request.client else None


@router.get(
    "/me/2fa",
    response_model=TwoFactorStatusResponse,
    dependencies=[Depends(require_local_provider)],
    responses=_TWO_FACTOR_LOCAL_ONLY,
)
def read_own_two_factor(
    db: Session = Depends(deps.get_db),
    current_user: User = Depends(deps.get_current_active_user),
) -> Any:
    """
    Whether your two-step sign-in is on, and how many recovery codes are left.

    `required` is true when your role must use two-step sign-in
    (`LOCAL_AUTH_2FA_REQUIRED_ROLES`): it cannot be turned off, and a sign-in
    asks you to set it up while it is off. It is false for an account that
    signs in without a password of its own.
    """
    found = two_factor_service.status(db, current_user)
    return TwoFactorStatusResponse(
        enabled=found.enabled,
        required=two_factor_required(current_user),
        recovery_codes_remaining=found.recovery_codes_remaining,
    )


# The actions are declared in alphabetical order: confirm, disable, enroll,
# recovery-codes.


@router.post(
    "/me/2fa/confirm",
    response_model=RecoveryCodesResponse,
    dependencies=[Depends(require_local_provider)],
    responses={
        409: {"description": NO_PENDING_SECRET_DETAIL},
        **_TWO_FACTOR_CODE_REFUSALS,
        **_TWO_FACTOR_LOCAL_ONLY,
    },
)
def confirm_own_two_factor(
    body: TwoFactorCodeRequest,
    request: Request,
    db: Session = Depends(deps.get_db),
    current_user: User = Depends(deps.get_current_active_user),
) -> Any:
    """
    Turn two-step sign-in on with the first code from your authenticator app.

    Answers 10 recovery codes, shown this once: each signs in once when the
    app is not at hand. A wrong code answers 403, leaves the secret waiting,
    and counts toward the same lockout as a wrong password.
    """
    try:
        codes = two_factor_service.confirm(
            db, current_user, body.code, client_host=_client_host(request)
        )
    except REFUSALS as exc:
        raise refusal_answer(
            exc, signed_in=True, not_enabled_detail=NO_PENDING_SECRET_DETAIL
        )
    return RecoveryCodesResponse(recovery_codes=codes)


@router.post(
    "/me/2fa/disable",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
    dependencies=[Depends(require_local_provider)],
    responses={
        409: {"description": NOT_ENABLED_DETAIL},
        **_TWO_FACTOR_CODE_REFUSALS,
        403: {
            "description": (
                f"{WRONG_CODE_TEXT} (wrong, used or malformed); or "
                f"{TWO_FACTOR_REQUIRED_FOR_ROLE} (LOCAL_AUTH_2FA_REQUIRED_ROLES "
                "names your role; no code is checked or counted)"
            )
        },
        **_TWO_FACTOR_LOCAL_ONLY,
    },
)
def disable_own_two_factor(
    body: TwoFactorDisableRequest,
    request: Request,
    db: Session = Depends(deps.get_db),
    current_user: User = Depends(deps.get_current_active_user),
) -> Response:
    """
    Turn your two-step sign-in off, with a code from the app or a recovery
    code (exactly one).

    Answers 204. Sign-in then asks for the password alone. A wrong code
    answers 403 and counts toward the same lockout as a wrong password.
    When your role must use two-step sign-in (`LOCAL_AUTH_2FA_REQUIRED_ROLES`)
    it answers 403 without checking the code; an administrator's reset is
    the way to start again.
    """
    if two_factor_required(current_user):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail=TWO_FACTOR_REQUIRED_FOR_ROLE
        )
    try:
        two_factor_service.disable(
            db,
            current_user,
            code=body.code,
            recovery_code=body.recovery_code,
            client_host=_client_host(request),
        )
    except REFUSALS as exc:
        raise refusal_answer(exc, signed_in=True)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post(
    "/me/2fa/enroll",
    response_model=TwoFactorEnrolResponse,
    dependencies=[Depends(require_local_provider)],
    responses={
        403: {
            "description": (
                "The current password is missing or incorrect, or the account "
                "has no password of its own."
            )
        },
        409: {"description": ALREADY_ENABLED_DETAIL},
        423: {
            "description": (
                "Too many failed attempts for this account (counted together "
                "with sign-in); see Retry-After."
            )
        },
        **_TWO_FACTOR_LOCAL_ONLY,
    },
)
def enrol_own_two_factor(
    body: Optional[TwoFactorEnrolRequest] = None,
    db: Session = Depends(deps.get_db),
    current_user: User = Depends(deps.get_current_active_user),
) -> Any:
    """
    Start setting up two-step sign-in: a new secret for your authenticator app.

    Asks for your current password (`current_password`), as changing your
    password does. Without it, or with an empty one, it answers 403. A wrong
    one answers 403 and counts toward the same lockout as sign-in; after too
    many failures it answers 423 until `Retry-After` has passed. An account
    with no password of its own answers 403: it does not sign in with a
    password, so two-step sign-in does not apply to it.

    Answers the secret in base32 (to type in) and as an `otpauth://` link (for
    a QR code). Nothing changes at sign-in until a code from the app confirms
    it (`POST /api/v1/users/me/2fa/confirm`). Asking again replaces a secret
    that is still waiting, so a code from the earlier one no longer works.
    409 once two-step sign-in is on.
    """
    _check_own_password(
        db,
        current_user,
        body.current_password if body is not None else None,
        missing_detail=TWO_FACTOR_PASSWORD_MISSING_DETAIL,
        no_password_detail=TWO_FACTOR_NO_PASSWORD_DETAIL,
    )
    try:
        enrolment = two_factor_service.enrol(db, current_user)
    except REFUSALS as exc:
        raise refusal_answer(exc, signed_in=True)
    return TwoFactorEnrolResponse(
        secret=enrolment.secret, otpauth_uri=enrolment.otpauth_uri
    )


@router.post(
    "/me/2fa/recovery-codes",
    response_model=RecoveryCodesResponse,
    dependencies=[Depends(require_local_provider)],
    responses={
        409: {"description": NOT_ENABLED_DETAIL},
        **_TWO_FACTOR_CODE_REFUSALS,
        **_TWO_FACTOR_LOCAL_ONLY,
    },
)
def regenerate_own_recovery_codes(
    body: TwoFactorCodeRequest,
    request: Request,
    db: Session = Depends(deps.get_db),
    current_user: User = Depends(deps.get_current_active_user),
) -> Any:
    """
    A new set of 10 recovery codes, for a code from your authenticator app.

    Shown this once; every earlier recovery code stops working. A wrong code
    answers 403 and counts toward the same lockout as a wrong password.
    """
    try:
        codes = two_factor_service.regenerate_recovery_codes(
            db, current_user, body.code, client_host=_client_host(request)
        )
    except REFUSALS as exc:
        raise refusal_answer(exc, signed_in=True)
    return RecoveryCodesResponse(recovery_codes=codes)


@router.get("/{user_id}", response_model=UserResponse)
async def get_user(
    user_id: str,
    db: Session = Depends(deps.get_db),
    current_user: User = Depends(deps.get_current_active_user),
):
    """
    Get user by ID.

    For superusers: can get any user
    For regular users: can only get their own user
    """
    # If the requested user is the current user, return directly
    if str(user_id) == str(current_user.id):
        # Ensure the response conforms to the UserResponse schema
        response_data = {
            "id": current_user.id,
            "username": current_user.username,
            "email": current_user.email,
            "full_name": current_user.full_name,
            "is_active": current_user.is_active,
            "is_superuser": current_user.is_superuser,
            "created_at": current_user.created_at,
            "updated_at": current_user.updated_at,
            "totp_enabled": current_user.totp_enabled,
        }

        # Add optional fields if they exist
        if hasattr(current_user, "last_login"):
            response_data["last_login"] = current_user.last_login
        if hasattr(current_user, "preferences"):
            response_data["preferences"] = current_user.preferences

        return UserResponse(**response_data)

    # If not own user, check permissions
    if not current_user.is_superuser:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Not enough permissions",
        )

    # Get and return the requested user
    user = db.query(User).filter(User.id == _user_uuid(user_id)).first()

    if not user:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="User not found",
        )

    # Ensure the response conforms to the UserResponse schema
    response_data = {
        "id": user.id,
        "username": user.username,
        "email": user.email,
        "full_name": user.full_name,
        "is_active": user.is_active,
        "is_superuser": user.is_superuser,
        "created_at": user.created_at,
        "updated_at": user.updated_at,
        "totp_enabled": user.totp_enabled,
    }

    # Add optional fields if they exist
    if hasattr(user, "last_login"):
        response_data["last_login"] = user.last_login
    if hasattr(user, "preferences"):
        response_data["preferences"] = user.preferences

    return UserResponse(**response_data)


@router.put("/{user_id}", response_model=UserResponse)
async def update_user(
    user_id: str,
    user_in: UserUpdate,
    db: Session = Depends(deps.get_db),
    current_user: User = Depends(deps.get_current_active_user),
):
    """
    Update user.

    For superusers: can update any user with all fields
    For regular users: can only update their own user and only non-privileged fields
    """
    # Get the user to update
    user = db.query(User).filter(User.id == _user_uuid(user_id)).first()

    if not user:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="User not found",
        )

    # Check permissions
    if str(user.id) != str(current_user.id) and not current_user.is_superuser:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Not enough permissions",
        )

    # Convert input model to dict, excluding unset fields
    update_data = user_in.model_dump(exclude_unset=True)

    # Before anything is written: refuses the caller's own password (every
    # caller), hashes another account's (a superuser's reset).
    apply_password_change(user, current_user, update_data)

    if not current_user.is_superuser:
        # Only a superuser changes an account's email address or username,
        # whatever the caller's role. Anyone else must resend the stored
        # values; they are then dropped, so this branch never writes either
        # column -- not even the parser's spelling of an unchanged address.
        if not _identity_unchanged(user, user_in):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=ADMIN_ONLY_IDENTITY_DETAIL,
            )
        update_data.pop("email", None)
        update_data.pop("username", None)

        # Regular users cannot change is_superuser or is_active
        update_data.pop("is_superuser", None)
        update_data.pop("is_active", None)

    # A superuser's own superuser access or active status is not removed
    # here (400, #652); another superuser does it. Before anything is written.
    refuse_if_own_access_removed(user, current_user, update_data)

    # Another account holding the new address in any letter case is a 409.
    # Re-casing the account's own address is not refused.
    new_email = changed_email(user, update_data)
    refuse_if_email_held(db, new_email, exclude_id=user.id)
    # Another account with the new username is a 409 "Username already
    # registered", as on create (#610). It was a 500.
    new_username = changed_username(user, update_data)
    refuse_if_username_held(db, new_username, exclude_id=user.id)

    # Demoting or deactivating another superuser: the caller must still be an
    # active superuser once those rows are locked (#652). No await between
    # this and the commit.
    if removes_an_active_superuser(user, update_data):
        require_still_active_superuser(db, current_user)

    role_before, active_before = role_value(user), user.is_active

    # Update user attributes
    for field in update_data:
        if hasattr(user, field):
            setattr(user, field, update_data[field])

    # The audit entries go in the same transaction as the change: both are
    # written or neither is. No await between the lock above and the commit.
    record_access_changes(db, current_user, user, role_before, active_before)
    commit_user_write(db, new_email, exclude_id=user.id, username=new_username)
    db.refresh(user)

    # Ensure the response conforms to the UserResponse schema
    response_data = {
        "id": user.id,
        "username": user.username,
        "email": user.email,
        "full_name": user.full_name,
        "is_active": user.is_active,
        "is_superuser": user.is_superuser,
        "created_at": user.created_at,
        "updated_at": user.updated_at,
        "totp_enabled": user.totp_enabled,
    }

    # Add optional fields if they exist
    if hasattr(user, "last_login"):
        response_data["last_login"] = user.last_login
    if hasattr(user, "preferences"):
        response_data["preferences"] = user.preferences

    return UserResponse(**response_data)


@router.delete("/{user_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_user(
    user_id: str,
    db: Session = Depends(deps.get_db),
    current_user: User = Depends(deps.get_current_active_user),
):
    """
    Delete user.

    For superusers: can delete any user except themselves
    For regular users: can only delete themselves
    """
    # Get the user to delete
    user = db.query(User).filter(User.id == _user_uuid(user_id)).first()

    if not user:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="User not found",
        )

    # Check if trying to delete superuser
    if str(user.id) == str(current_user.id) and current_user.is_superuser:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Superusers cannot delete themselves",
        )

    # Check permissions for non-self deletion
    if str(user.id) != str(current_user.id) and not current_user.is_superuser:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Not enough permissions",
        )

    # Deleting an active superuser: the caller must still be one once those
    # rows are locked (#652). No await between this and the commit.
    if user.is_superuser and user.is_active:
        require_still_active_superuser(db, current_user)

    # Read before the delete: the deleted row may be the caller's own. An
    # entry about deleting your own account has no user_id to point at.
    actor = AuditActor.of(current_user)
    if actor.id == user.id:
        actor = AuditActor(id=None, email=actor.email, username=actor.username)
    deleted = audit_identity(audit_snapshot(EntityType.USER, user))
    deleted_id = user.id

    db.delete(user)
    db.commit()

    AuditService.record_after_commit(
        db,
        actor=actor,
        action=ActionType.USER_DELETE,
        entity_type=EntityType.USER,
        entity_id=deleted_id,
        entity_name=deleted["username"] or str(deleted_id),
        before=deleted,
    )
