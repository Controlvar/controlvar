"""
The page origins a visual experiment may use, checked when a preview link is
created and when the editor is opened (#1545, C4).

A visual experiment's origins are the origin of its ``editor_url`` and of each
of its pages, through ``normalise_origin``. Each must be one this server
accepts requests from, and none may be the dashboard's own address:

    allowed = [o for o in settings.cors_allowed_origins
               if o not in settings.dashboard_origins]

:func:`visual_origin_problem` returns the text the route answers 422 with, or
``None``; then :func:`experiment_origins` is the experiment's
``site_origins``. The checks, in order:

1. ``settings.dashboard_origins`` is empty: the server does not know the
   dashboard's address, so it cannot tell a page on it apart
   (:data:`NO_DASHBOARD_ORIGIN`).
2. ``*`` is among the allowed origins: every site is, so the visual editor is
   off (:data:`EVERY_SITE_ALLOWED`).
3. An origin is a dashboard origin (:data:`PAGE_ON_DASHBOARD`).
4. An origin is not allowed (:data:`ORIGIN_NOT_ALLOWED`, naming it).

The origins are checked when the link or the editor is made, not when the
experiment is saved: CORS_ORIGINS changes when the server restarts, and the
browser script needs the same list to reach this server at all.

The visual editor's routes also check the ``Origin`` header a request carries
(``normalise_origin``, then an exact comparison; ``null`` is never an origin):

* :func:`dashboard_origin_refusal`: the editor's own routes accept only the
  dashboard's origins. A ``GET`` or ``HEAD`` may come with no ``Origin``,
  since a browser sends none to its own origin; any other method must carry
  one.
* :func:`editor_link_origin_refusal`: opening the editor from the dashboard
  is the same rule for a ``POST``, with a text that names an origin that is
  not a dashboard origin and says how to add it.
* :func:`site_origin_refusal`: the one route a site's page calls accepts only
  the experiment's :func:`site_origins`.
"""

from __future__ import annotations

from typing import Any, List, Optional, Tuple
from urllib.parse import urlsplit

from backend.app.core.config import normalise_origin
from backend.app.core.config import settings as app_settings
from backend.app.schemas.visual import get_field

#: The dashboard's own address is unknown (an empty DASHBOARD_ORIGINS with no
#: PUBLIC_BASE_URL).
NO_DASHBOARD_ORIGIN = (
    "The visual editor needs this dashboard's address: set DASHBOARD_ORIGINS or "
    "PUBLIC_BASE_URL."
)
#: CORS_ORIGINS (or BACKEND_CORS_ORIGINS) holds ``*``.
EVERY_SITE_ALLOWED = (
    "The visual editor is off on this server, because CORS_ORIGINS is * (every "
    "site). Ask whoever runs Controlvar to list your sites in CORS_ORIGINS "
    "instead."
)
#: A page or the editor's page is on a dashboard origin.
PAGE_ON_DASHBOARD = (
    "A visual experiment's pages cannot be on the Controlvar dashboard's own address."
)
#: An origin the server does not accept requests from; ``{origin}`` is it.
ORIGIN_NOT_ALLOWED = (
    "Controlvar cannot open the editor on {origin}: your Controlvar server does "
    "not accept requests from that site. Ask whoever runs Controlvar to add it "
    "to CORS_ORIGINS and restart it."
)

#: A request to the visual editor's routes whose ``Origin`` is not one of the
#: dashboard's, or a write that carries none.
NOT_FROM_DASHBOARD = (
    "The visual editor accepts requests only from the Controlvar dashboard."
)
#: Opening the editor from an origin that is not a dashboard origin; ``{origin}``
#: is it, normalised.
DASHBOARD_NOT_KNOWN = (
    "Your Controlvar server does not know this dashboard's address ({origin}), "
    "so it will not open the editor. Ask whoever runs Controlvar to add it to "
    "DASHBOARD_ORIGINS."
)
#: A request from a page that is not on one of the experiment's sites.
SITE_NOT_LISTED = (
    "The visual editor opens only on the experiment's own sites. Open it again "
    "from the experiment's page in Controlvar."
)

#: The methods a browser sends to its own origin with no ``Origin`` header.
METHODS_WITHOUT_ORIGIN = frozenset({"GET", "HEAD"})


def allowed_page_origins(settings: Any = None) -> List[str]:
    """The origins a visual experiment's pages may be on: the CORS list
    without the dashboard's origins, in the CORS list's order."""
    settings = settings or app_settings
    dashboard = set(settings.dashboard_origins)
    return [o for o in settings.cors_allowed_origins if o not in dashboard]


def _origin_of(url: Any) -> Optional[str]:
    """``scheme://host[:port]`` of *url*, normalised, or ``None``."""
    if not isinstance(url, str):
        return None
    try:
        parts = urlsplit(url)
    except ValueError:
        return None
    if not parts.scheme or not parts.netloc:
        return None
    return normalise_origin(f"{parts.scheme}://{parts.netloc}")


def experiment_origins(visual_config: Any) -> List[str]:
    """The origins of *visual_config*'s ``editor_url`` and pages, each once,
    in that order. A URL that has no origin is given as it is, so a refusal
    can name it."""
    urls = [get_field(visual_config, "editor_url")]
    urls += [get_field(page, "url") for page in get_field(visual_config, "pages") or []]
    origins: List[str] = []
    for url in urls:
        origin = _origin_of(url) or str(url)
        if origin not in origins:
            origins.append(origin)
    return origins


def visual_origin_problem(visual_config: Any, settings: Any = None) -> Optional[str]:
    """The 422 text for *visual_config*'s origins, or ``None`` when every one
    may be used; the first refusal, in the order the module docstring gives.
    When it is ``None``, the experiment's ``site_origins`` are
    :func:`experiment_origins`."""
    settings = settings or app_settings
    dashboard = list(settings.dashboard_origins)
    if not dashboard:
        return NO_DASHBOARD_ORIGIN
    allowed = allowed_page_origins(settings)
    if "*" in allowed:
        return EVERY_SITE_ALLOWED
    for origin in experiment_origins(visual_config):
        if origin in dashboard:
            return PAGE_ON_DASHBOARD
        if origin not in allowed:
            return ORIGIN_NOT_ALLOWED.format(origin=origin)
    return None


def site_origins(visual_config: Any, settings: Any = None) -> List[str]:
    """The experiment's sites as the editor uses them: each of
    :func:`experiment_origins` that is an allowed page origin now, in that
    order. Computed from the row at each use, never stored, so a site taken
    out of CORS_ORIGINS stops counting at the next restart. Never a dashboard
    origin, and empty for an experiment with no visual settings."""
    if visual_config is None:
        return []
    allowed = allowed_page_origins(settings)
    return [o for o in experiment_origins(visual_config) if o in allowed]


def request_origin(value: Optional[str]) -> Optional[str]:
    """An ``Origin`` header's value through ``normalise_origin``, or ``None``
    when it is absent, ``null``, ``*`` or not an origin."""
    if value is None:
        return None
    origin = normalise_origin(value)
    if origin is None or origin == "*":
        return None
    return origin


def dashboard_origin_refusal(
    origin_header: Optional[str], method: str, settings: Any = None
) -> Optional[str]:
    """``None`` when a request with this ``Origin`` header (``None`` if it
    has none) and *method* may reach the editor's own routes, else
    :data:`NOT_FROM_DASHBOARD`. Only a ``GET`` or ``HEAD`` may come without
    one; an ``Origin`` that is present must be a dashboard origin."""
    settings = settings or app_settings
    if origin_header is None:
        if method.upper() in METHODS_WITHOUT_ORIGIN:
            return None
        return NOT_FROM_DASHBOARD
    origin = request_origin(origin_header)
    if origin is None or origin not in settings.dashboard_origins:
        return NOT_FROM_DASHBOARD
    return None


def editor_link_origin_refusal(
    origin_header: Optional[str], settings: Any = None
) -> Optional[Tuple[int, str]]:
    """For opening the editor (a ``POST``): ``None`` when the ``Origin`` is a
    dashboard origin, else the status and text to refuse with, in this order:
    422 :data:`NO_DASHBOARD_ORIGIN` when the server knows no dashboard origin;
    403 :data:`NOT_FROM_DASHBOARD` when there is no ``Origin`` or it is not an
    origin; 403 :data:`DASHBOARD_NOT_KNOWN` naming it when it is an origin but
    not the dashboard's."""
    settings = settings or app_settings
    dashboard = list(settings.dashboard_origins)
    if not dashboard:
        return 422, NO_DASHBOARD_ORIGIN
    origin = request_origin(origin_header)
    if origin is None:
        return 403, NOT_FROM_DASHBOARD
    if origin not in dashboard:
        return 403, DASHBOARD_NOT_KNOWN.format(origin=origin)
    return None


def site_origin_refusal(
    origin_header: Optional[str], sites: List[str]
) -> Optional[str]:
    """``None`` when the ``Origin`` is one of *sites* (:func:`site_origins`),
    else :data:`SITE_NOT_LISTED`, also when there is none."""
    origin = request_origin(origin_header)
    if origin is None or origin not in sites:
        return SITE_NOT_LISTED
    return None
