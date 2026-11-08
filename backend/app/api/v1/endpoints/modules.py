"""
Modules endpoint -- ``GET /api/v1/modules``.

Unauthenticated on purpose: it drives the dashboard's chrome (which nav groups
render, which pages are stubs) and the browser has to be able to ask before
anyone has logged in.

Because it is public it exposes only what the chrome needs: the profile, the
installed module names and the version.  Nothing about the deployment's
configuration, secrets or data.
"""

from typing import List, Literal

from fastapi import APIRouter
from pydantic import BaseModel, Field

from backend.app.api.off_loop_route import OffLoopRoute
from backend.app.core import hooks
from backend.app.core.config import settings
from backend.app.modules_loader import modules_active, modules_failure

router = APIRouter(route_class=OffLoopRoute)


class ModulesResponse(BaseModel):
    """What the dashboard needs to decide what to render."""

    profile: Literal["core", "full"] = Field(
        ..., description="'full' when the modules package is installed, else 'core'"
    )
    modules: List[str] = Field(
        default_factory=list,
        description="Names of the installed modules; empty on the core profile.",
    )
    version: str


@router.get(
    "/modules",
    response_model=ModulesResponse,
    summary="Profile and installed modules",
    # No `tags=` here: api.py already includes this router under ["Modules"],
    # and FastAPI concatenates the two, so the OpenAPI operation would list
    # the tag twice and the docs page render a duplicate group.
)
async def get_modules() -> ModulesResponse:
    """Report the running profile and the installed modules.

    The core profile (no ``modules`` package) answers::

        {"profile": "core", "modules": [], "version": "..."}
    """
    # This docstring is the route's OpenAPI `description` and is pinned by
    # backend/tests/smoke/test_openapi_snapshot.py, so the reasoning lives
    # here instead.
    #
    # `modules_active()`, not `load_modules()`: this route is unauthenticated
    # and must not be able to make the process re-run a registration (~3.5 s
    # of endpoint imports on the event loop).  Whoever serves this request
    # imported `backend.app.api.api`, which loaded.
    #
    # ...and `modules_failure()` with it, so a *degraded* full-profile process
    # answers exactly what a core one does.  The question this route is asked
    # is "what is this API serving", and the answer has to come from the API
    # surface rather than from the registries: when `mount_module_routers`
    # fails, `hooks.installed_modules()` still holds all ten names --
    # `register_modules` is the last line of a registration that *succeeded*
    # -- while not one module route is mounted, and a dashboard told "full"
    # then renders every module page against a 404, which is the state its
    # `error` path exists to avoid.
    #
    # `/health/ready` is where the difference stays visible: its `modules`
    # check reports `unhealthy` with the cause and says how far the
    # registration got (see core/health.py).  This route is public and says
    # only what the chrome needs.
    loaded = modules_active() and modules_failure() is None
    return ModulesResponse(
        profile="full" if loaded else "core",
        modules=list(hooks.installed_modules()) if loaded else [],
        version=settings.VERSION,
    )
