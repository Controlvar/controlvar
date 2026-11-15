"""Load the optional modules into the core application, if present.

This is the only module in ``backend/app`` that knows the ``modules`` package
can exist, and it never imports anything from it by name: it imports the
top-level package and calls its ``register(hooks)`` entry point, which fills in
the hooks in :mod:`backend.app.core.hooks`.  A core build has no ``modules``
package, :func:`load_modules` finds nothing, and every hook keeps its core
default -- that is the core profile.  With the package present and registered
the process runs the full profile.

Called from the four places that build an application or a schema:

* ``backend/app/main.py``       — the running API,
* ``backend/app/db/bootstrap.py`` — ``create_all`` on a fresh database,
* ``backend/app/db/migrations/env.py`` — alembic's ``target_metadata``,
* ``backend/tests/conftest.py`` — the test schema.

Loading is idempotent and never raises: a modules package that fails to import
must not take the core API down with it, so the failure is logged at ERROR --
with the traceback where the traceback carries no secret (see
:func:`describe_exception`), because a full-profile deployment silently
degrading to the core profile is not a state an operator should have to
discover from a 501 -- and the process continues on the core profile.

Mounting what the registration added is part of the same promise and lives here
too (:func:`mount_module_routers`), not in the router build: registering a
router and mounting it are one operation, and both fail the same way.

Degrading *silently* is a development convenience, not a deployment one, so
each caller then says what a failure means for it.  A schema builder refuses
outright (:func:`require_modules_or_absent`), the running API refuses outside
development and test (:func:`abort_if_modules_broken`), and only a core tree
-- no ``modules/`` at all -- is a clean start everywhere.

The entry point is looked for as ``modules.register`` first and
``modules.backend.app.register.register`` second; the package
(``modules/__init__.py``) re-exports the latter as the former.  There is no
other registration path: a tree without ``modules/`` is a core tree.

Two of those four callers build an application and two build a schema, and a
schema needs much less: :func:`require_modules_or_absent` asks for a
registration ``with_routers=False``, which skips the API graph (see
:func:`load_modules`).  A registration that does not accept the keyword is
called exactly as before.
"""

from __future__ import annotations

import importlib
import inspect
import logging
import os
import threading
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)

#: Top-level package name of the optional modules.
MODULES_PACKAGE = "modules"

_lock = threading.RLock()
_state: Optional[bool] = None
#: Set when module code was *found* and did not end up installed -- it failed
#: to register, or its routers failed to mount; None when it loaded or when
#: there was nothing to load.  Schema builders and the health probe read this.
_failure: Optional[str] = None
#: Whether a registration has imported the API graph (see
#: :func:`load_modules`).  A schema-only load does not, and a later load that
#: wants it has to run the registration again.
_routers: bool = False
#: Whether such a second, routers-only run was tried and failed.  It stops the
#: retry loop -- every later ``load_modules()`` would otherwise ask for the
#: routers again and pay the endpoint imports again -- while leaving ``_state``
#: True, because the first registration still stands.
_routers_failed: bool = False


# ---------------------------------------------------------------------------
# Describing a failure without echoing a secret
# ---------------------------------------------------------------------------
# The first thing ``modules.register(hooks)`` does is build and validate the
# modules' settings, so the likeliest failure here is a rejected secret -- a
# too-short AUDIT_HMAC_KEY, say.  By default pydantic writes the rejected
# value into the error as ``input_value='...'``, in all three renderings:
# ``repr(exc)``, ``str(exc)`` and the formatted traceback.  The core and
# modules settings set ``hide_input_in_errors``, so theirs carry no value; the
# redaction below is defence in depth for any model that does not.  Whatever
# this module builds travels a long way -- the ERROR log,
# ``modules_failure()``, the RuntimeError ``require_modules_or_absent()``
# raises, and from there ``pytest.exit()`` and CI output -- so none of the
# three may be used raw.


def _validation_errors(exc: BaseException) -> Optional[list]:
    """``exc.errors()`` when *exc* is a pydantic ValidationError, else ``None``."""
    try:
        from pydantic import ValidationError
    except Exception:  # pragma: no cover - pydantic is a hard dependency
        return None
    if not isinstance(exc, ValidationError):
        return None
    try:
        return list(exc.errors())
    except Exception:  # pragma: no cover - defensive
        return []


_PYDANTIC_VALUE_ERROR = "Value error, "


def describe_exception(exc: BaseException) -> str:
    """A short description of *exc* that names the problem but no secret.

    For a settings ``ValidationError`` this is built from ``exc.errors()`` with
    ``input`` and ``ctx`` dropped: what is left names the *field* that failed
    and the validator's own message, which is what an operator needs
    ("AUDIT_HMAC_KEY must be at least 32 characters") and nothing more.  Any
    other exception is rendered as its type and ``str()``; a validator that
    puts the value it rejected into its own message would defeat this, so the
    ones in ``config.py`` and ``modules/backend/app/settings.py`` deliberately
    do not.
    """
    errors = _validation_errors(exc)
    if errors is None:
        text = str(exc).strip()
        return f"{type(exc).__name__}: {text}" if text else type(exc).__name__
    fields = []
    for error in errors:
        message = str(error.get("msg", "invalid"))
        # pydantic prefixes a validator's own ValueError with this; the rest is
        # the validator's sentence, which names the setting.
        if message.startswith(_PYDANTIC_VALUE_ERROR):
            message = message[len(_PYDANTIC_VALUE_ERROR) :]
        loc = ".".join(str(part) for part in error.get("loc", ()))
        # A model-level validator (the PUBLIC_BASE_URL/ALLOWED_HOSTS check)
        # has no field location; its message starts with the setting's name,
        # so it is given without a label rather than under a placeholder one.
        fields.append(f"{loc}: {message}" if loc else message)
    joined = "; ".join(fields) or "invalid settings"
    return f"{type(exc).__name__}: {joined}"


def _traceback_is_safe(exc: BaseException) -> bool:
    """Whether the traceback of *exc* may be logged.

    Every ``logger`` call in this module that reports a failure passes this as
    its ``exc_info``; none of them uses ``logger.exception``, which is
    ``exc_info=True`` unconditionally and would print the rejected value of a
    settings ValidationError into the start-up log -- and, under the JSON
    logger, into CloudWatch.

    False for a settings ``ValidationError``: its final line is ``str(exc)``,
    rejected value and all.  The traceback is worth nothing there anyway (it
    is pydantic's own frames), while for an ImportError or a SyntaxError in
    module code it is the whole diagnosis, so those keep it.

    The whole ``__cause__``/``__context__`` chain is checked, not just *exc*:
    a formatted traceback prints every exception in the chain, so a
    ValidationError re-raised as something else would otherwise print its
    rejected value under a "direct cause" banner.
    """
    seen = set()
    current: Optional[BaseException] = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if _validation_errors(current) is not None:
            return False
        current = current.__cause__ or current.__context__
    return True


def load_modules(force: bool = False, with_routers: bool = True) -> bool:
    """Register the modules' hooks. Returns True when they loaded.

    Idempotent: the result is cached so the call sites can each call it
    unconditionally.  Pass ``force=True`` to re-run it (tests).  Never raises
    -- see :func:`require_modules_or_absent` for the callers that must not
    continue on a failure.

    ``with_routers=False`` asks the registration for everything a *schema*
    needs -- the settings, the model modules, the audit signer -- and not for
    the API graph.  Importing the endpoint modules is almost the whole cost of
    a registration (3.5 s here against 0.03 s for the seven model modules), and
    the three schema builders never touch a router.  The request is passed on
    to ``register(hooks, with_routers=...)`` when the registration accepts it;
    one that does not is simply called as before, so a modules package written
    against the older signature still works, just as slowly.

    The cached result records whether the routers were imported, so a later
    ``load_modules()`` -- the running API after a bootstrap, or a test that
    builds the app after ``conftest`` has built the schema -- runs the
    registration again rather than serving no module routes.  Every hook
    registrar is idempotent, so running it twice changes nothing else.  That
    is the *only* reason a cached result is ever recomputed: a load that found
    nothing, or one that failed, still answers from the cache exactly as
    before.

    That second run is an *upgrade*, and it is treated as one: a registration
    has already succeeded, so its failure adds nothing and takes nothing away.
    It leaves the state True, leaves the hooks the first run installed alone --
    the audit signer above all -- records the cause, and is not tried again.
    The alternative (what this used to do) was to run it like a first load: on
    failure ``hooks.reset()``, which quietly swapped a real HMAC signer for
    ``NullAuditSigner`` in a process that had been signing compliance events,
    so later events were written with a NULL signature that ``verify()`` calls
    intact while the rows signed *earlier* began to verify False.
    """
    global _state, _routers, _routers_failed
    with _lock:
        routers_still_wanted = (
            with_routers and _state is True and not _routers and not _routers_failed
        )
        if _state is not None and not force and not routers_still_wanted:
            return _state

        if routers_still_wanted and not force:
            if _load(with_routers=True, keep_registration=True):
                _routers = True
            else:
                _routers_failed = True
            return _state

        _state = _load(with_routers=with_routers)
        _routers = with_routers and _state
        _routers_failed = False
        return _state


def modules_active() -> bool:
    """Whether the modules are registered *now*, without registering them.

    For the callers that only *report* the profile of a running process --
    ``GET /api/v1/modules`` and the ``/health/ready`` probe.  They must not go
    through :func:`load_modules`: a process that loaded the modules for a
    schema (``with_routers=False``) answers that call by running the whole
    registration again, importing the eleven endpoint modules -- ~3.5 s, on
    the event loop, holding this module's lock -- from inside an
    unauthenticated request handler.

    Answering from the cache costs nothing and is accurate wherever these two
    callers run: serving a request means ``backend.app.api.api`` was imported,
    and that module loads the modules while it builds the v1 router.  Before
    any load has run at all the answer is False, the same "core" that a core
    build reports.
    """
    with _lock:
        return _state is True


def mount_module_routers(router: Any) -> int:
    """Mount the routers the registration added on *router*, under the guard.

    The last step of the seam's API surface, and the second half of what
    :func:`load_modules` promises: registering the module routers and mounting
    them are one operation, so they fail the same way.  ``hooks.apply_routers``
    runs module code -- ``modules.backend.app.register.mount_routers`` -- and
    every way it can fail (an endpoint module with no ``public_router``, a
    ``KeyError`` when its module list and its mounting code drift apart, a
    router FastAPI refuses) used to happen *outside* the try/except, after
    "Modules loaded" had already been logged: the failure came out of
    ``import backend.app.api.api``, so ``import backend.app.main`` died and
    uvicorn never bound a port.  A modules package that fails must cost the
    deployment its module routes, not its API.

    Returns the number of registrars applied -- 0 on the core profile, and 0
    when mounting failed.

    The registrars run against a staging router that is included into *router*
    only once all of them have succeeded, so a registrar that mounts six
    routers and raises on the seventh leaves nothing behind: the alternative is
    a v1 router carrying half a profile.  Inclusion preserves each route's
    path, tags and dependencies, and costs one nesting level that nothing but
    ``router.routes`` can see.

    A failure is recorded like any other (:func:`modules_failure`), which is
    what ``main.py``'s :func:`abort_if_modules_broken` refuses to start on
    outside development and test, and what ``/health/ready`` reports.  The
    hooks are *not* reset: the registration itself succeeded, and taking the
    audit signer away from a process because a router would not mount is the
    silent downgrade this module exists to avoid.
    """
    global _failure
    from fastapi import APIRouter

    from backend.app.api.off_loop_route import OffLoopRoute
    from backend.app.core import hooks

    staging = APIRouter(route_class=OffLoopRoute)
    try:
        applied = hooks.apply_routers(staging)
    except Exception as exc:
        with _lock:
            _failure = f"mounting the modules' routers raised {describe_exception(exc)}"
        logger.error(
            "Mounting the modules' routers failed; continuing with the core "
            "API surface. The modules' routes will answer 404 until this is "
            "fixed. Cause: %s",
            describe_exception(exc),
            exc_info=_traceback_is_safe(exc),
        )
        return 0
    if applied:
        router.include_router(staging)
    return applied


def modules_failure() -> Optional[str]:
    """Why the modules did not load, or None."""
    with _lock:
        return _failure


def require_modules_or_absent() -> bool:
    """Load the modules, and raise if they were found but failed.

    For the callers that build a *schema* from ``Base.metadata`` -- alembic's
    ``env.py``, ``db/bootstrap.py``, the test conftest.  The running API may
    degrade to the core profile when the registration fails; a schema tool
    must not: with only the core models registered, ``alembic revision
    --autogenerate`` proposes dropping every module table and ``upgrade head``
    then does it, and a bootstrap creates a database the module routers cannot
    serve.  Silence there is data loss.

    Returns True when the modules loaded and False when there was nothing to
    load (a core tree); raises RuntimeError otherwise.

    Asks for a schema-only registration: ``Base.metadata`` needs the model
    modules, not the routers (see :func:`load_modules`).
    """
    loaded = load_modules(with_routers=False)
    failure = modules_failure()
    if failure is not None:
        raise RuntimeError(
            "The modules package is present but failed to register, so "
            "Base.metadata holds only the core tables; refusing to build a "
            f"schema from it. Cause: {failure}"
        )
    return loaded


def abort_if_modules_broken() -> None:
    """Refuse to start when the modules package is present but did not install.

    For the *running API* (``backend/app/main.py``), the one caller that keeps
    serving traffic after a failure.  The loader's promise that it never raises
    is about the core API starting whatever state ``modules/`` is in; it is not
    a promise that a **full-profile** deployment may serve traffic in a state
    nobody asked for.  A registration that failed leaves the process on the
    core profile: every module route 404s, the audit signer is
    ``NullAuditSigner`` so compliance events are written with a NULL signature
    and ``verify()`` calls them intact, and nothing but one ERROR line says so
    -- ``/health/ready`` stays green, because readiness has no notion of the
    profile.  A deployment that supplied a bad ``AUDIT_HMAC_KEY`` used to be
    refused at start-up, by the core settings class, before those settings
    moved to the modules (issue #91); this puts that back where it belongs.

    It reads :func:`modules_failure`, not the load's return value, so it covers
    every way the package can fail to install: the registration raising, a
    later routers-only run raising, and :func:`mount_module_routers` failing to
    mount what was registered.  The last two leave the state True -- the first
    registration stands -- and are still a deployment serving routes nobody
    asked it to drop.  ``main.py`` therefore calls this unconditionally.

    *Absent* modules are not a failure -- that is the core profile, and it
    starts cleanly in every environment.  Only ``modules_failure()`` aborts,
    and only outside development and test, where a half-finished module is a
    normal state of a working tree (``settings.dev_fallbacks_allowed``).
    """
    failure = modules_failure()
    if failure is None:
        return

    from backend.app.core.config import settings

    if settings.dev_fallbacks_allowed:
        logger.error(
            "The %s package is present but did not install; continuing "
            "degraded because ENVIRONMENT=%s. Cause: %s",
            MODULES_PACKAGE,
            settings.ENVIRONMENT,
            failure,
        )
        return

    raise RuntimeError(
        f"The {MODULES_PACKAGE} package is present but did not install, so "
        f"this process would serve less than the profile it was deployed as, "
        f"with ENVIRONMENT={settings.ENVIRONMENT!r}: the modules' routes would "
        "answer 404, and a registration that failed outright would also write "
        "compliance audit events unsigned. Refusing to start. "
        f"Cause: {failure}"
    )


def _find_register() -> tuple[Optional[Callable[..., None]], Optional[str]]:
    """The ``register(hooks)`` callable to run, and where it came from.

    Never raises.  Importing the modules package runs module code, and a
    SyntaxError or a RuntimeError from a missing optional dependency must not
    take ``import backend.app.main`` down with it.  A ``ModuleNotFoundError``
    for *another* module raised from inside the package is a broken install,
    not the absence of one, and is logged as such rather than mistaken for the
    core profile.
    """
    global _failure
    try:
        package = importlib.import_module(MODULES_PACKAGE)
    except ModuleNotFoundError as exc:
        if exc.name == MODULES_PACKAGE:
            logger.debug("No %s package on the path", MODULES_PACKAGE)
            package = None
        else:
            # A module *inside* the package is missing: a broken install, not
            # the absence of one.
            _failure = f"{MODULES_PACKAGE} imports {exc.name!r}, which is not installed"
            logger.error(
                "%s is present but importing it failed on %r; continuing on "
                "the core profile. Cause: %s",
                MODULES_PACKAGE,
                exc.name,
                describe_exception(exc),
                exc_info=_traceback_is_safe(exc),
            )
            return None, None
    except Exception as exc:
        _failure = f"{MODULES_PACKAGE} could not be imported: {describe_exception(exc)}"
        logger.error(
            "%s is present but could not be imported; continuing on the core "
            "profile. Cause: %s",
            MODULES_PACKAGE,
            describe_exception(exc),
            exc_info=_traceback_is_safe(exc),
        )
        return None, None
    else:
        register = getattr(package, "register", None)
        if register is not None:
            return register, MODULES_PACKAGE
        # The entry point may live in the package's application module rather
        # than be re-exported from the top level: modules/backend/app/register.py.
        try:
            module = importlib.import_module(f"{MODULES_PACKAGE}.backend.app.register")
        except ModuleNotFoundError as exc:
            if exc.name not in (
                f"{MODULES_PACKAGE}.backend",
                f"{MODULES_PACKAGE}.backend.app",
                f"{MODULES_PACKAGE}.backend.app.register",
            ):
                _failure = (
                    f"{MODULES_PACKAGE}.backend.app.register imports {exc.name!r}"
                )
                logger.error(
                    "%s.backend.app.register failed to import. Cause: %s",
                    MODULES_PACKAGE,
                    describe_exception(exc),
                    exc_info=_traceback_is_safe(exc),
                )
                return None, None
        except Exception as exc:
            _failure = (
                f"{MODULES_PACKAGE}.backend.app.register could not be imported: "
                f"{describe_exception(exc)}"
            )
            logger.error(
                "%s.backend.app.register failed to import. Cause: %s",
                MODULES_PACKAGE,
                describe_exception(exc),
                exc_info=_traceback_is_safe(exc),
            )
            return None, None
        else:
            register = getattr(module, "register", None)
            if register is not None:
                return register, f"{MODULES_PACKAGE}.backend.app.register"
        # A bare `modules/` directory (a core checkout that kept an empty
        # directory, say) imports as a namespace package.  That is a normal
        # core state.  A package that *has* code but no entry point anywhere
        # is module code that was found and did not register, which is the
        # definition of a failure here -- not "there was nothing to load".
        # It used to be only a WARNING, with `_failure` left at None, so
        # `require_modules_or_absent()` answered "core tree" and
        # `abort_if_modules_broken()` returned early even in production: a
        # one-character typo in the entry point's name (`regsiter`) silently
        # degraded a full deployment to the core profile, autogenerate then
        # proposed dropping every module table, a bootstrap built a core-only
        # schema, and compliance events were written unsigned.
        if _has_python_code(package):
            _failure = (
                f"{MODULES_PACKAGE} has code but exposes no register(hooks) "
                f"entry point ({MODULES_PACKAGE}.register and "
                f"{MODULES_PACKAGE}.backend.app.register)"
            )
            logger.error(
                "%s has code but exposes no register(hooks) entry point (looked "
                "at %s.register and %s.backend.app.register); continuing on the "
                "core profile",
                MODULES_PACKAGE,
                MODULES_PACKAGE,
                MODULES_PACKAGE,
            )
        else:
            logger.debug(
                "%s is a bare directory; running the core profile", MODULES_PACKAGE
            )

    return None, None


def _has_python_code(package: Any) -> bool:
    """Whether *package* (a namespace package, usually) holds any ``.py`` file."""
    for location in getattr(package, "__path__", None) or ():
        for root, _dirs, files in os.walk(location):
            if any(name.endswith(".py") for name in files):
                return True
    return False


def _call_register(
    register: Callable[..., None], hooks: Any, with_routers: bool
) -> None:
    """Call ``register``, passing ``with_routers`` only if it accepts it.

    The documented entry point is ``register(hooks)``; the keyword is an
    optimisation this loader offers, not a requirement it imposes.  The
    signature is inspected rather than the call retried on ``TypeError``,
    because a ``TypeError`` raised from *inside* a registration would
    otherwise run half of it twice.
    """
    if not with_routers and _accepts_with_routers(register):
        register(hooks, with_routers=False)
        return
    register(hooks)


def _accepts_with_routers(register: Callable[..., None]) -> bool:
    """Whether *register* takes a ``with_routers`` keyword (or ``**kwargs``)."""
    try:
        parameters = list(inspect.signature(register).parameters.values())
    except (TypeError, ValueError):  # pragma: no cover - exotic callables
        return False
    return any(
        parameter.name == "with_routers"
        or parameter.kind is inspect.Parameter.VAR_KEYWORD
        for parameter in parameters
    )


def _load(with_routers: bool = True, keep_registration: bool = False) -> bool:
    """Run one registration.  ``keep_registration`` marks a routers-only upgrade.

    An upgrade runs on top of a registration that already succeeded (see
    :func:`load_modules`), so a failure rolls nothing back: the hooks the
    first run installed are the best state available, and clearing them would
    take a working audit signer away from a process that is already signing.
    """
    global _failure
    from backend.app.core import hooks

    _failure = None
    register, origin = _find_register()
    if register is None:
        # INFO, symmetric with the "Modules loaded" line below: this fires
        # while `backend.app.api.api` is imported, before main.py has
        # configured logging, and an operator reading the start-up log should
        # see which profile the process is.
        logger.info("Running the core profile")
        return False

    # Everything from here on is module code running inside the core process.
    # *Any* exception -- ImportError from a model module, a missing optional
    # dependency raised as RuntimeError, a SyntaxError -- is caught: the
    # loader's one promise is that the core API starts.
    try:
        _call_register(register, hooks, with_routers)
        # Model modules are imported here rather than by the package so that
        # a loader run before `Base.metadata` is consumed still registers the
        # tables.
        hooks.import_registered_models()
    except Exception as exc:
        _failure = f"{origin}.register(hooks) raised {describe_exception(exc)}"
        if keep_registration:
            # An upgrade (see the docstring): the earlier registration stands.
            # Say so in the recorded cause too -- it is what `/health/ready`
            # reports and what `abort_if_modules_broken()` prints, and "the
            # modules did not register" would be false here.
            _failure += " while adding the module routers (the registration "
            _failure += "that already succeeded stands)"
            logger.error(
                "%s.register(hooks) failed while adding the module routers; the "
                "registration that already succeeded stands -- the audit signer "
                "and the model modules keep theirs -- and the modules' routes "
                "will answer 404/501 until this is fixed. Cause: %s",
                origin,
                describe_exception(exc),
                exc_info=_traceback_is_safe(exc),
            )
            return False
        if _validation_errors(exc) is not None:
            # A refused setting. What happens next depends on the environment
            # -- abort_if_modules_broken() refuses to start outside development
            # and test -- so this line says only what is certain: which
            # setting, and that the modules are not installed.
            logger.error(
                "%s.register(hooks) did not install the modules: a setting was "
                "refused. %s",
                origin,
                describe_exception(exc),
                exc_info=_traceback_is_safe(exc),
            )
        else:
            logger.error(
                "%s.register(hooks) failed; continuing on the core profile. "
                "The modules' routes will answer 404/501 until this is fixed. "
                "Cause: %s",
                origin,
                describe_exception(exc),
                exc_info=_traceback_is_safe(exc),
            )
        # Clear the registries so no router is served against tables that do
        # not exist.  This cannot un-import a model module that was imported
        # before the failing one -- Python has no such thing -- so
        # Base.metadata may still carry some module tables; that is why schema
        # builders go through require_modules_or_absent() and refuse to build
        # from a metadata in this state.
        hooks.reset()
        return False

    logger.info("Modules loaded (from %s)", origin)
    return True


def reset() -> None:
    """Forget the cached load result (tests only)."""
    global _state, _failure, _routers, _routers_failed
    with _lock:
        _state = None
        _failure = None
        _routers = False
        _routers_failed = False
