from typing import Any, Dict, Generator, Optional, Union
from uuid import UUID

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import (
    APIKeyHeader,
)
from loguru import logger
from pydantic import BaseModel
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from backend.app.core.config import settings
from backend.app.core.logger import unexpected_failure
from backend.app.core.permissions import (
    Action,
    ResourceType,
    check_ownership,
    check_permission,
    get_permission_error_message,
)
from backend.app.core.security import (
    InvalidTokenError,
    decode_local_token,
    hash_api_key,
    oauth2_scheme,
)
from backend.app.db import request_sessions
from backend.app.db.session import SessionLocal
from backend.app.models.api_key import APIKey
from backend.app.models.experiment import Experiment, ExperimentStatus
from backend.app.models.feature_flag import FeatureFlag
from backend.app.models.report import Report
from backend.app.models.user import User, UserRole
from backend.app.services.auth_service import (
    CognitoTokenRefused,
    auth_service,
    log_token_refused,
)
from backend.app.services.cognito_accounts import (
    CognitoSignInRefused,
    log_sign_in_refused,
    resolve_cognito_user,
)

# Try to import Redis, handle gracefully if not installed
try:
    import redis.asyncio as redis

    REDIS_AVAILABLE = True
except ImportError:
    REDIS_AVAILABLE = False

    # Create a dummy redis class to avoid None.Redis error
    class redis:
        class Redis:
            pass


# OAuth2 scheme for token authentication
# oauth2_scheme is imported from backend.app.core.security.
# Do NOT redefine here — the imported version has auto_error=False so that a
# missing token reaches get_current_user (dev bypass vs. 401 is decided there).

# API key header extraction
API_KEY_HEADER = APIKeyHeader(name="X-API-Key", auto_error=False)


# Cache control model
class CacheControl(BaseModel):
    """Cache control settings."""

    enabled: bool = False
    skip: bool = False
    redis: Optional[object] = None


# Redis pool
_redis_pool = None


async def get_redis_pool():
    """Get Redis connection pool."""
    global _redis_pool
    if not REDIS_AVAILABLE:
        logger.warning("Redis not available, cache disabled")
        return None

    if _redis_pool is None:
        try:
            from backend.app.core.redis_client import create_async_redis_client

            _redis_pool = create_async_redis_client(decode_responses=True)
        except Exception as e:
            logger.error(f"Redis connection error: {e}")
            return None
    return _redis_pool


async def get_db() -> Generator[Session, None, None]:
    """
    Get database session.

    The session is registered as one of the request's sessions
    (``backend.app.db.request_sessions``) until it is closed.

    Yields:
        Session: Database session
    """
    try:
        db = SessionLocal()
        request_sessions.register(db)
        yield db
    finally:
        try:
            db.close()
        finally:
            request_sessions.unregister(db)


def get_token(request: Request) -> str:
    """
    Extract the access token from the Authorization header.

    Args:
        request (Request): FastAPI request object

    Returns:
        str: Access token

    Raises:
        HTTPException: If the token is not found
    """
    authorization = request.headers.get("Authorization")

    if not authorization:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authorization header missing",
            headers={"WWW-Authenticate": "Bearer"},
        )

    scheme, _, token = authorization.partition(" ")

    if scheme.lower() != "bearer":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid authentication scheme",
            headers={"WWW-Authenticate": "Bearer"},
        )

    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token missing",
            headers={"WWW-Authenticate": "Bearer"},
        )

    return token


DEV_BYPASS_USERNAME = "dev-admin"
DEV_BYPASS_EMAIL = "dev@localhost"


def dev_auth_bypass_active() -> bool:
    """
    True only when the dev-admin bypass is enabled *and* permitted.

    Both conditions are read from ``settings`` at call time so tests can
    toggle them, and both must hold: ``DEV_AUTH_BYPASS`` must be exactly
    ``True`` and ``ENVIRONMENT`` must be ``development`` or ``test``.  No
    other environment variable (``TESTING``, ``DEBUG`` ...) unlocks it.
    """
    # ``is True`` also guards against a mocked settings object, whose
    # attribute would be a truthy MagicMock.
    return getattr(settings, "dev_auth_bypass_active", False) is True


def _get_or_create_dev_user(db: Session) -> User:
    """Return the synthetic dev-admin user used by the dev-auth bypass."""
    user = db.query(User).filter(User.username == DEV_BYPASS_USERNAME).first()
    if not user:
        user = User(
            username=DEV_BYPASS_USERNAME,
            email=DEV_BYPASS_EMAIL,
            full_name="Dev Admin",
            hashed_password="not-a-real-hash",
            is_active=True,
            role=UserRole.ADMIN,
            is_superuser=True,
        )
        db.add(user)
        db.commit()
        db.refresh(user)
    return user


def _credentials_exception(
    detail: str = "Could not validate credentials",
) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail=detail,
        headers={"WWW-Authenticate": "Bearer"},
    )


def _authenticate_local_token(token: str, db: Session) -> User:
    """
    Resolve a locally issued JWT to its ``User`` row.

    401 for any decode/lookup failure, 400 when the account is inactive.
    """
    try:
        claims = decode_local_token(token)
        user_id = UUID(str(claims.get("sub")))
    except (InvalidTokenError, ValueError, TypeError) as exc:
        logger.debug(f"Local token rejected: {exc}")
        raise _credentials_exception()

    try:
        user = db.query(User).filter(User.id == user_id).first()
    except (
        Exception
    ) as exc:  # pragma: no cover - defensive; DB errors are not auth errors
        logger.error(f"User lookup failed during local token auth: {exc}")
        raise _credentials_exception()

    if user is None:
        raise _credentials_exception()
    if not user.is_active:
        raise HTTPException(status_code=400, detail="Inactive user")
    return user


def get_current_user(
    token: Optional[str] = Depends(oauth2_scheme), db: Session = Depends(get_db)
) -> User:
    """
    Get the current authenticated user from the provided bearer token.

    Resolution order:

    1. Dev-admin bypass (``DEV_AUTH_BYPASS=true`` **and** ``ENVIRONMENT`` in
       development/test): returns the synthetic ``dev-admin`` superuser.
    2. No token: 401.
    3. ``AUTH_PROVIDER=local``: decode the HS256 JWT issued by
       ``/api/v1/auth/login`` and load the user by id (401 on any failure,
       400 if the account is inactive).
    4. ``AUTH_PROVIDER=cognito``: validate the token with Cognito, then find
       the account linked to the Cognito user ID, or create it on a first
       sign-in (``services.cognito_accounts.resolve_cognito_user``). Every
       refusal is the same 401; its reason goes to the
       ``backend.app.auth.cognito_sign_in`` logger.

    Args:
        token (str): Bearer access token (may be None)
        db (Session): Database session

    Returns:
        User: Current authenticated user

    Raises:
        HTTPException: If authentication fails
    """
    if dev_auth_bypass_active():
        try:
            return _get_or_create_dev_user(db)
        except Exception as dev_err:
            raise unexpected_failure(
                dev_err,
                "Dev user creation",
                "Could not sign in the development user",
                db=db,
            )

    if not token:
        raise _credentials_exception("Not authenticated")

    if settings.AUTH_PROVIDER == "local":
        return _authenticate_local_token(token, db)

    try:
        user_data = auth_service.get_user_with_groups(token)
    except CognitoTokenRefused as refusal:
        log_token_refused(refusal, "sign-in")
        raise _credentials_exception()
    except Exception as e:
        # The type only: the message can carry what the caller sent.
        logger.error(f"Cognito token not accepted: {type(e).__name__}")
        raise _credentials_exception()

    try:
        return resolve_cognito_user(db, user_data)
    except CognitoSignInRefused as refusal:
        db.rollback()
        log_sign_in_refused(refusal)
        raise _credentials_exception()
    except Exception as e:
        db.rollback()
        logger.error(f"Cognito sign-in failed: {type(e).__name__}")
        raise _credentials_exception()


def require_active_user(user: User) -> User:
    """
    Return ``user`` if the account is active; 400 "Inactive user" otherwise.

    The check behind :func:`get_current_active_user`, for code that resolves
    the user itself rather than through ``Depends``:
    ``read_scope.get_current_reader``, ``GET /api/v1/auth/me`` and the results
    WebSocket. Call this, never the dependency: the dependency is ``async``, so
    calling it as a plain function returns a coroutine and checks nothing.
    """
    if not user.is_active:
        raise HTTPException(status_code=400, detail="Inactive user")
    return user


def require_superuser(user: User) -> User:
    """
    Return ``user`` if it is a superuser; 403 "Not enough permissions"
    otherwise. The check behind :func:`get_current_superuser`.
    """
    if not user.is_superuser:
        raise HTTPException(status_code=403, detail="Not enough permissions")
    return user


async def get_current_active_user(
    current_user: User = Depends(get_current_user),
) -> User:
    """
    Get the current active user.

    ``async`` on purpose: it only reads the user ``get_current_user`` has
    already loaded, so it runs on the event loop and opens no connection.
    Code that is not a ``Depends`` calls :func:`require_active_user`.

    Args:
        current_user (User): Current user

    Returns:
        User: Current active user

    Raises:
        HTTPException: If the user is inactive
    """
    return require_active_user(current_user)


async def get_current_superuser(
    current_user: User = Depends(get_current_active_user),
) -> User:
    """
    Get the current superuser.

    ``async`` for the reason :func:`get_current_active_user` is; code that is
    not a ``Depends`` calls :func:`require_superuser`.

    Args:
        current_user (User): Current active user

    Returns:
        User: Current superuser

    Raises:
        HTTPException: If the user is not a superuser
    """
    return require_superuser(current_user)


async def get_current_superuser_or_none(
    current_user: User = Depends(get_current_active_user),
) -> Optional[User]:
    """
    Get current superuser or None.

    ``async`` for the reason :func:`get_current_active_user` is.

    Args:
        current_user: Current authenticated user

    Returns:
        User: Current user if they are a superuser, None otherwise
    """
    if current_user.is_superuser:
        return current_user
    return None


def get_experiment_read_access(
    experiment: Union[Experiment, Dict[str, Any]],
    current_user: User = Depends(get_current_active_user),
) -> Union[Experiment, Dict[str, Any]]:
    """
    Admit a caller who may read this experiment and its results.

    A superuser, or a user whose role holds ``Action.READ`` on experiments.
    Who created the experiment is not considered.

    Called directly from endpoints with the experiment already loaded, before
    anything else the route does, a cache read included.

    Args:
        experiment: The experiment being read
        current_user: The current authenticated user

    Returns:
        The experiment, unchanged

    Raises:
        HTTPException 403: If the role does not hold READ on experiments
    """
    if current_user.is_superuser:
        return experiment

    if not check_permission(current_user, ResourceType.EXPERIMENT, Action.READ):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=get_permission_error_message(ResourceType.EXPERIMENT, Action.READ),
        )

    return experiment


async def require_experiment_read(
    current_user: User = Depends(get_current_active_user),
) -> User:
    """
    Admit a caller who may read experiments, by the rule of
    ``GET /api/v1/experiments/{id}``: a superuser, or a role, custom role or
    grant holding READ on experiments.

    A route dependency, so it runs before the handler: before the experiment
    is looked up, the request body is validated or a cached answer is read.
    ``get_experiment_read_access`` never reads its experiment argument, so
    None is passed.

    Raises:
        HTTPException 403: If the caller does not hold READ on experiments
    """
    get_experiment_read_access(None, current_user)
    return current_user


async def require_experiment_list(
    current_user: User = Depends(get_current_active_user),
) -> User:
    """
    Admit a caller who may list experiments, as ``GET /api/v1/experiments/``
    does: a superuser, or a role, custom role or grant holding LIST on
    experiments.

    Raises:
        HTTPException 403: If the caller does not hold LIST on experiments
    """
    if not check_permission(current_user, ResourceType.EXPERIMENT, Action.LIST):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=get_permission_error_message(ResourceType.EXPERIMENT, Action.LIST),
        )
    return current_user


async def require_feature_flag_list(
    current_user: User = Depends(get_current_active_user),
) -> User:
    """
    Admit a caller who may list feature flags, as ``GET /api/v1/feature-flags/``
    does: a superuser, or a role, custom role or grant holding LIST on feature
    flags.

    Raises:
        HTTPException 403: If the caller does not hold LIST on feature flags
    """
    if not check_permission(current_user, ResourceType.FEATURE_FLAG, Action.LIST):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=get_permission_error_message(ResourceType.FEATURE_FLAG, Action.LIST),
        )
    return current_user


def get_experiment_change_access(
    experiment: Union[Experiment, Dict[str, Any]],
    current_user: User,
    action: Action = Action.UPDATE,
) -> Union[Experiment, Dict[str, Any]]:
    """
    Admit a caller who may change this experiment.

    A superuser, or a user whose role holds both ``Action.READ`` and
    ``action`` on experiments. Who created the experiment is not considered,
    so a role without ``action`` is refused on an experiment it created too.

    Used by update, schedule, start, pause, complete, archive and metadata,
    and by delete with ``Action.DELETE``.

    Args:
        experiment: The experiment being changed
        current_user: The current authenticated user
        action: The action the route performs (UPDATE unless stated)

    Returns:
        The experiment, unchanged

    Raises:
        HTTPException 403: If the role does not hold READ and ``action``
    """
    get_experiment_read_access(experiment, current_user)

    if current_user.is_superuser:
        return experiment

    if not check_permission(current_user, ResourceType.EXPERIMENT, action):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=get_permission_error_message(ResourceType.EXPERIMENT, action),
        )

    return experiment


def get_api_key(
    db: Session = Depends(get_db), api_key_header: str = Depends(API_KEY_HEADER)
) -> Optional[User]:
    """
    Validate API key from header and return associated user if valid.

    Args:
        db: Database session
        api_key_header: API key from header

    Returns:
        User: User associated with the API key

    Raises:
        HTTPException: If API key is invalid or inactive
    """
    if not api_key_header:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="API key missing",
            headers={"WWW-Authenticate": "APIKey"},
        )

    # Look up by hashed key only. API keys must never be stored as plaintext.
    api_key_hash = hash_api_key(api_key_header)
    api_key = db.query(APIKey).filter(APIKey.key == api_key_hash).first()
    if not api_key or not api_key.is_valid:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid API Key",
            headers={"WWW-Authenticate": "APIKey"},
        )

    # Get associated user
    user = db.query(User).filter(User.id == api_key.user_id).first()
    if not user or not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid API Key",
            headers={"WWW-Authenticate": "APIKey"},
        )

    # Issue #198: record the use, throttled inside update_last_used. A failure
    # here is bookkeeping, not authentication, so it never fails the request.
    try:
        api_key.update_last_used(db)
    except SQLAlchemyError as exc:
        db.rollback()
        logger.warning(f"Could not record API key use for key {api_key.id}: {exc}")

    # Hold a connection for the rest of the request whether or not the use was recorded.
    db.connection()

    return user


def get_experiment_by_key(
    experiment_key: str,
    db: Session = Depends(get_db),
    required_status: Optional[ExperimentStatus] = None,
) -> Experiment:
    """
    Get experiment by key or ID.

    Args:
        experiment_key (str): Experiment key or ID
        db (Session): Database session
        required_status (Optional[ExperimentStatus]): If provided, the experiment must have this status

    Returns:
        Experiment: Experiment with the given key or ID

    Raises:
        HTTPException: If experiment not found or has incorrect status
    """
    experiment = None

    # First try to lookup by ID (UUID)
    try:
        from uuid import UUID

        experiment_id = UUID(experiment_key)
        experiment = db.query(Experiment).filter(Experiment.id == experiment_id).first()
    except (ValueError, TypeError):
        # Not a valid UUID, try the original lookup method
        pass

    # If not found by ID, try the original lookup method
    if not experiment:
        experiment = (
            db.query(Experiment)
            .filter(getattr(Experiment, "key", None) == experiment_key)
            .first()
        )

    if not experiment:
        raise HTTPException(status_code=404, detail="Experiment not found")

    # Check for required status if specified
    if (
        required_status is not None
        and hasattr(experiment, "status")
        and experiment.status != required_status
    ):
        raise HTTPException(
            status_code=400, detail=f"Experiment not in {required_status.value} status"
        )
    # Default check for active status (only if required_status is not specified)
    elif (
        required_status is None
        and hasattr(experiment, "status")
        and experiment.status != ExperimentStatus.ACTIVE
    ):
        raise HTTPException(status_code=400, detail="Inactive experiment")

    return experiment


async def get_cache_control(skip_cache: bool = False) -> CacheControl:
    """
    Get cache control settings.

    Args:
        skip_cache (bool): Whether to skip cache

    Returns:
        CacheControl: Cache control settings
    """
    cache_control = CacheControl(skip=skip_cache)

    if skip_cache:
        return cache_control

    if not hasattr(settings, "CACHE_ENABLED") or not settings.CACHE_ENABLED:
        return cache_control

    if not REDIS_AVAILABLE:
        return cache_control

    try:
        redis_client = await get_redis_pool()
        if redis_client:
            # Test connection
            await redis_client.ping()
            cache_control.redis = redis_client
            cache_control.enabled = True
    except Exception as e:
        logger.warning(f"Redis connection failed: {e}")

    return cache_control


# Feature Flag permissions
async def get_feature_flag_by_key(
    key: str,
    db: Session = Depends(get_db),
) -> FeatureFlag:
    """Get a feature flag by key."""
    feature_flag = db.query(FeatureFlag).filter(FeatureFlag.key == key).first()
    if not feature_flag:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Feature flag with key {key} not found",
        )
    return feature_flag


async def get_feature_flag_access(
    feature_flag: FeatureFlag = Depends(get_feature_flag_by_key),
    current_user: User = Depends(get_current_user),
) -> FeatureFlag:
    """Check if user has access to the feature flag."""
    if current_user.is_superuser:
        return feature_flag

    # Check if user has permission to read feature flags
    if not check_permission(current_user, ResourceType.FEATURE_FLAG, Action.READ):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=get_permission_error_message(ResourceType.FEATURE_FLAG, Action.READ),
        )

    # Check ownership for non-admin users for modification actions
    if not check_permission(
        current_user, ResourceType.FEATURE_FLAG, Action.UPDATE
    ) and not check_ownership(current_user, feature_flag):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You don't have permission to access this feature flag",
        )

    return feature_flag


async def can_create_feature_flag(
    current_user: User = Depends(get_current_user),
) -> bool:
    """Check if user can create a feature flag."""
    if not check_permission(current_user, ResourceType.FEATURE_FLAG, Action.CREATE):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=get_permission_error_message(
                ResourceType.FEATURE_FLAG, Action.CREATE
            ),
        )
    return True


async def can_update_feature_flag(
    feature_flag: FeatureFlag = Depends(get_feature_flag_access),
    current_user: User = Depends(get_current_user),
) -> bool:
    """Check if user can update a feature flag."""
    if not check_permission(current_user, ResourceType.FEATURE_FLAG, Action.UPDATE):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=get_permission_error_message(
                ResourceType.FEATURE_FLAG, Action.UPDATE
            ),
        )
    return True


async def can_delete_feature_flag(
    feature_flag: FeatureFlag = Depends(get_feature_flag_access),
    current_user: User = Depends(get_current_user),
) -> bool:
    """Check if user can delete a feature flag."""
    if not check_permission(current_user, ResourceType.FEATURE_FLAG, Action.DELETE):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=get_permission_error_message(
                ResourceType.FEATURE_FLAG, Action.DELETE
            ),
        )
    return True


# Report permissions
async def get_report_by_id(
    report_id: int,
    db: Session = Depends(get_db),
) -> Report:
    """Get a report by ID."""
    report = db.query(Report).filter(Report.id == report_id).first()
    if not report:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Report with id {report_id} not found",
        )
    return report


async def get_report_access(
    report: Report = Depends(get_report_by_id),
    current_user: User = Depends(get_current_user),
) -> Report:
    """Check if user has access to the report."""
    if current_user.is_superuser:
        return report

    # Check if user has permission to read reports
    if not check_permission(current_user, ResourceType.REPORT, Action.READ):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=get_permission_error_message(ResourceType.REPORT, Action.READ),
        )

    # Check ownership for non-admin users for modification actions
    if not check_permission(
        current_user, ResourceType.REPORT, Action.UPDATE
    ) and not check_ownership(current_user, report):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You don't have permission to access this report",
        )

    return report


async def can_create_report(
    current_user: User = Depends(get_current_user),
) -> bool:
    """Check if user can create a report."""
    if not check_permission(current_user, ResourceType.REPORT, Action.CREATE):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=get_permission_error_message(ResourceType.REPORT, Action.CREATE),
        )
    return True


async def can_update_report(
    report: Report = Depends(get_report_access),
    current_user: User = Depends(get_current_user),
) -> bool:
    """Check if user can update a report."""
    if not check_permission(current_user, ResourceType.REPORT, Action.UPDATE):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=get_permission_error_message(ResourceType.REPORT, Action.UPDATE),
        )
    return True


async def can_delete_report(
    report: Report = Depends(get_report_access),
    current_user: User = Depends(get_current_user),
) -> bool:
    """Check if user can delete a report."""
    if not check_permission(current_user, ResourceType.REPORT, Action.DELETE):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=get_permission_error_message(ResourceType.REPORT, Action.DELETE),
        )
    return True


def can_create_experiment(
    current_user: User = Depends(get_current_user),
) -> bool:
    """Check if user can create an experiment."""
    if not check_permission(current_user, ResourceType.EXPERIMENT, Action.CREATE):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=get_permission_error_message(ResourceType.EXPERIMENT, Action.CREATE),
        )
    return True
