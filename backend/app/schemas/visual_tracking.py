"""
What a web page sends to ask for its visual changes, what it gets back, and
the website details a visitor's context may carry (#1545, S3, S4, S9).

``POST /api/v1/tracking/visual`` takes a :class:`VisualTrackingRequest` and
answers a :class:`VisualTrackingResponse`; ``POST /api/v1/tracking/visual/
preview`` takes a :class:`VisualPreviewRequest` and answers a
:class:`VisualPreviewResponse`. Neither request carries a query string: the
page sends its host and path only, and neither is stored.

The website details are flat ``web_*`` keys the browser script adds to the
context (:data:`WEB_DETAIL_RULES` lists them, each with the one form it may
take). :func:`clean_web_details` keeps a key only in that form and drops every
other ``web_*`` key, so what reaches an exposure's ``event_metadata`` is
exactly what the docs say the script records. A dropped detail never refuses
the request: the visitor is assigned as if it had not been sent. Every other
context key, the site's own ``data-attributes``, passes through unchanged.

Nothing here is applied to ``POST /api/v1/tracking/assign`` or any other
route: the cleaning is this route's, so an SDK's context is stored as it was
sent.
"""

from __future__ import annotations

import re
import unicodedata
from typing import Any, Callable, Dict, List, Literal, Mapping, Optional, Tuple

from pydantic import BaseModel, ConfigDict, Field, StrictInt, field_validator

from backend.app.schemas.storable_text import StorableTextModel
from backend.app.schemas.visual import VisualGoal

#: The prefix of every website detail.
WEB_PREFIX = "web_"

#: The device classes the script reports (``web_device``).
WEB_DEVICES = ("phone", "tablet", "desktop")
#: The browsers the script names (``web_browser``); everything else is ``other``.
WEB_BROWSERS = ("chrome", "edge", "firefox", "safari", "samsung", "opera", "other")
#: Whether the visitor was seen before this visit (``web_visitor``).
WEB_VISITORS = ("new", "returning")

#: The longest ``web_referrer``: a host name's limit.
MAX_REFERRER_LENGTH = 253
#: The longest campaign (``utm``) value kept. A longer one is dropped, never cut.
MAX_UTM_LENGTH = 100
#: The longest ``web_landing_path`` kept. A longer one is dropped, never cut,
#: so no two paths are stored as one.
MAX_LANDING_PATH_LENGTH = 255

#: ``web_language``: a primary language subtag, lower case.
LANGUAGE_PATTERN = re.compile(r"[a-z]{2,3}")
#: ``web_referrer``: a lower-case host, never a path or a query.
REFERRER_PATTERN = re.compile(r"[a-z0-9.-]{1,%d}" % MAX_REFERRER_LENGTH)
#: A campaign value: RFC 3986's unreserved characters only. No ``+`` (it
#: decodes to a space in one parser and not in another), no ``@`` (an email
#: address in a link is never stored), no ``%`` and no space.
UTM_PATTERN = re.compile(r"[A-Za-z0-9._~-]{1,%d}" % MAX_UTM_LENGTH)

#: The three campaign keys. ``utm_term`` and ``utm_content`` are never kept.
UTM_KEYS = ("web_utm_source", "web_utm_medium", "web_utm_campaign")

#: Characters a landing path may not contain, besides whitespace and control
#: characters: the start of a query or a fragment, and ``@``.
_LANDING_PATH_FORBIDDEN = frozenset("?#@")


def _in(choices: Tuple[str, ...]) -> Callable[[str], bool]:
    return lambda value: value in choices


def _matches(pattern: "re.Pattern[str]") -> Callable[[str], bool]:
    # fullmatch: ``$`` would also accept a value ending in a new line.
    return lambda value: pattern.fullmatch(value) is not None


def landing_path_is_kept(value: str) -> bool:
    """Whether *value* is a ``web_landing_path`` that is kept (S9).

    A path only: it starts with ``/``, is at most
    :data:`MAX_LANDING_PATH_LENGTH` characters, and has no ``?``, ``#``,
    ``@`` or ``%40``, no whitespace, no control character and no lone
    surrogate. A longer path is not kept at all: it is never cut.
    """
    if not 1 <= len(value) <= MAX_LANDING_PATH_LENGTH or not value.startswith("/"):
        return False
    if "%40" in value:
        return False
    for character in value:
        if character in _LANDING_PATH_FORBIDDEN or character.isspace():
            return False
        if unicodedata.category(character) in ("Cc", "Cs", "Zs", "Zl", "Zp"):
            return False
    return True


#: Every website detail the server keeps, and the one form each may take. A
#: ``web_*`` key not listed here (``web_country`` among them: the server sets
#: that one itself, never the page) is dropped.
WEB_DETAIL_RULES: Dict[str, Callable[[str], bool]] = {
    "web_device": _in(WEB_DEVICES),
    "web_browser": _in(WEB_BROWSERS),
    "web_language": _matches(LANGUAGE_PATTERN),
    "web_visitor": _in(WEB_VISITORS),
    "web_referrer": _matches(REFERRER_PATTERN),
    "web_utm_source": _matches(UTM_PATTERN),
    "web_utm_medium": _matches(UTM_PATTERN),
    "web_utm_campaign": _matches(UTM_PATTERN),
    "web_landing_path": landing_path_is_kept,
}


def web_detail_is_kept(key: str, value: Any) -> bool:
    """Whether the website detail *key* with *value* is kept: a known key,
    and a string in its form. ``null``, ``""``, a number or a list is not."""
    rule = WEB_DETAIL_RULES.get(key)
    return rule is not None and isinstance(value, str) and rule(value)


def clean_web_details(
    context: Optional[Mapping[str, Any]],
) -> Tuple[Optional[Dict[str, Any]], List[str]]:
    """The context with every ``web_*`` key not in its documented form
    removed, and the names of the keys removed, in the order they were sent.

    A key that does not start with ``web_`` is kept as it is, whatever it
    holds. ``None`` stays ``None``. Pure: *context* is not changed.
    """
    if context is None:
        return None, []
    cleaned: Dict[str, Any] = {}
    dropped: List[str] = []
    for key, value in context.items():
        if isinstance(key, str) and key.startswith(WEB_PREFIX):
            if not web_detail_is_kept(key, value):
                dropped.append(key)
                continue
        cleaned[key] = value
    return cleaned, dropped


# --- the requests ---------------------------------------------------------------

#: The longest host a page sends: a 253-character name and a port.
MAX_HOST_LENGTH = 261
#: The longest path a page sends.
MAX_PATH_LENGTH = 2048

HOST_REFUSED = (
    "The page's host is 1 to 261 characters, with no whitespace or control character."
)
PATH_REFUSED = (
    "The page's path is 1 to 2048 characters, with no whitespace or control character."
)


def _plain_text(value: str) -> bool:
    return not any(
        character.isspace() or unicodedata.category(character) == "Cc"
        for character in value
    )


class _Page(StorableTextModel):
    """Where the page is: its host and path, never its query."""

    model_config = ConfigDict(extra="forbid")

    host: str = Field(
        ...,
        description=(
            "The page's host, as `location.host` gives it: 1 to 261 characters, "
            "a port allowed. Not stored."
        ),
    )
    path: str = Field(
        ...,
        description=(
            "The page's path, as `location.pathname` gives it: 1 to 2048 "
            "characters, with no query. Not stored."
        ),
    )

    @field_validator("host")
    @classmethod
    def _host(cls, value: str) -> str:
        if not 1 <= len(value) <= MAX_HOST_LENGTH or not _plain_text(value):
            raise ValueError(HOST_REFUSED)
        return value

    @field_validator("path")
    @classmethod
    def _path(cls, value: str) -> str:
        if not 1 <= len(value) <= MAX_PATH_LENGTH or not _plain_text(value):
            raise ValueError(PATH_REFUSED)
        return value


class VisualTrackingRequest(_Page):
    """``POST /api/v1/tracking/visual``: which visual changes and goals this
    visitor gets on this page."""

    user_id: str = Field(
        ...,
        min_length=1,
        max_length=255,
        description=(
            "The visitor's id: the script's random cookie id, or the site's own "
            "`data-user-id`."
        ),
    )
    format_version: StrictInt = Field(
        ...,
        ge=1,
        description=(
            "The newest page-change format the script applies. An experiment "
            "stored in a newer format is left out, and nobody is assigned to it."
        ),
    )
    context: Optional[Dict[str, Any]] = Field(
        None,
        description=(
            "Attributes for targeting, as in POST /api/v1/tracking/assign: the "
            "site's own and the script's `web_*` website details. A `web_*` key "
            "not in its documented form is dropped, never refused. Stored on "
            "each exposure."
        ),
    )


class VisualPreviewRequest(_Page):
    """``POST /api/v1/tracking/visual/preview``: one variant, by preview link."""

    token: str = Field(
        ...,
        min_length=1,
        max_length=128,
        description="The token from the preview link's `#controlvar-preview=` fragment.",
    )


# --- the answers ------------------------------------------------------------------


class VisualPageRule(BaseModel):
    """A page rule on the page's host: ``exact`` matches one path, ``prefix``
    the path and every path below it."""

    match: Literal["exact", "prefix"]
    path: str


class VisualPages(BaseModel):
    """The page rules of every running visual experiment on this host, which
    the script may keep for ``max_age`` seconds and use to skip a page none
    of them matches."""

    host: str = Field(..., description="The host, lower case, without a port.")
    rules: List[VisualPageRule]
    max_age: int = Field(60, description="Seconds the rules may be kept.")


class VisualExperimentAnswer(BaseModel):
    """One running visual experiment whose pages include this page."""

    experiment_key: str
    variant_id: str
    variant_name: str
    assigned: bool = Field(
        ...,
        description=(
            "False when the visitor is not in the experiment; `changes` and "
            "`goals` are then null."
        ),
    )
    reason: str = Field(
        ...,
        description=(
            "As POST /api/v1/tracking/assign answers it: assigned, holdout, "
            "mutual_exclusion or targeting."
        ),
    )
    changes: Optional[Dict[str, Any]] = Field(
        None,
        description=(
            "The variant's page changes, `{version, changes}`, or null (the "
            "control, a variant with none, or a visitor not in the experiment)."
        ),
    )
    goals: Optional[List[VisualGoal]] = Field(
        None,
        description=(
            "The experiment's goals, to record for every assigned visitor, the "
            "control's included; null when the visitor is not in the experiment."
        ),
    )


class VisualTrackingResponse(BaseModel):
    """``POST /api/v1/tracking/visual``'s answer. Sent with
    ``Cache-Control: no-store``."""

    experiments: List[VisualExperimentAnswer] = Field(
        ...,
        description=(
            "At most 10 running experiments whose pages include this page, the "
            "one that started first first."
        ),
    )
    pages: VisualPages


class VisualPreviewResponse(BaseModel):
    """``POST /api/v1/tracking/visual/preview``'s answer: one variant's page
    changes. A preview records nothing. Sent with ``Cache-Control: no-store``."""

    experiment_key: Optional[str] = Field(
        None,
        description="The experiment's key; null only for an experiment that has none.",
    )
    variant_name: str
    changes: Optional[Dict[str, Any]] = Field(
        None,
        description="The variant's page changes, or null for the control or a variant with none.",
    )
    pages: VisualPages
