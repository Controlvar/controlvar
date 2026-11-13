"""
A visual experiment's page changes, pages and goals (#1545).

A visual experiment is an ordinary ``a_b``, ``mv`` or ``bandit`` experiment
that carries ``visual_config`` (the pages it runs on, the page the editor
opens, and its goals); each non-control variant may carry ``visual_changes``,
a versioned list of changes from a fixed set, each aimed at one CSS selector.
Nothing here executes, renders or fetches anything: these are the shapes the
API stores, and the browser script applies them with text-only writes.

Every model is strict and refuses unknown keys, so a key the server does not
know (``set_html``, ``onclick``, ``__proto__``), a ``version`` other than the
integer 1, a missing ``op`` and a number sent as a string are all refused with
422. Every refusal worded here is fixed text: none repeats the value sent.

``tests/sdk-contract/visual-changes-vectors.json`` is generated from these
models (``python -m backend.scripts.generate_visual_vectors``), so the script
that applies the changes is held to the same answers.
"""

from __future__ import annotations

import json
import re
import unicodedata
from typing import (
    Annotated,
    Any,
    Dict,
    Iterable,
    List,
    Literal,
    Optional,
    Sequence,
    Tuple,
    Union,
)
from urllib.parse import urlsplit

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    Strict,
    ValidationError,
    field_validator,
    model_validator,
)
from pydantic_core import InitErrorDetails, PydanticCustomError

from backend.app.schemas.split_url_config import (
    normalize_host,
    normalize_path,
    url_problem,
)

#: The error ``type`` of a refused change and of a refused configuration.
VISUAL_CHANGES_ERROR = "visual_changes"
VISUAL_CONFIG_ERROR = "visual_config"

#: The one format version this server reads and writes.
VISUAL_FORMAT_VERSION = 1

# Limits (S1, S2, S10). Each is checked exactly: N is accepted, N + 1 refused.
MAX_CHANGES = 100
#: The stored ``visual_changes`` of one variant, as compact UTF-8 JSON.
MAX_CHANGES_BYTES = 64 * 1024
MAX_SELECTOR_LENGTH = 512
MAX_TEXT_LENGTH = 2000
MAX_ATTRIBUTE_VALUE_LENGTH = 2048
MAX_ALT_LENGTH = 500
MAX_STYLES = 20
MAX_STYLE_VALUE_LENGTH = 200
MAX_PAGES = 10
MAX_GOALS = 10
MAX_EVENT_NAME_LENGTH = 100

#: The operations, exactly. A new one is a new format version.
VISUAL_OPS = ("set_text", "set_attribute", "set_image", "hide", "set_style")

#: The attributes ``set_attribute`` may write. ``href`` is a link: see
#: :func:`link_problem`. Everything else (``on*``, ``style``, ``srcdoc``,
#: ``formaction``, ``id``, ``name``, ``is``...) is refused.
ALLOWED_ATTRIBUTES = ("alt", "title", "aria-label", "placeholder", "href")

#: The margin properties (C13): only 1-4 tokens of ``0``, ``auto`` or a
#: non-negative length, with no function at all.
MARGIN_PROPERTIES = frozenset(
    {
        "margin",
        "margin-top",
        "margin-right",
        "margin-bottom",
        "margin-left",
        "margin-block",
        "margin-block-start",
        "margin-block-end",
        "margin-inline",
        "margin-inline-start",
        "margin-inline-end",
    }
)
_PADDING_PROPERTIES = frozenset(
    {
        "padding",
        "padding-top",
        "padding-right",
        "padding-bottom",
        "padding-left",
        "padding-block",
        "padding-block-start",
        "padding-block-end",
        "padding-inline",
        "padding-inline-start",
        "padding-inline-end",
    }
)

#: The properties ``set_style`` may set. Position, z-index, top, left,
#: transform and pointer-events are not among them, nor is ``order``: each
#: lets one element take another's place or its clicks.
ALLOWED_STYLE_PROPERTIES = (
    frozenset(
        {
            "color",
            "background-color",
            "font-size",
            "font-weight",
            "font-style",
            "font-family",
            "text-align",
            "text-decoration",
            "text-transform",
            "line-height",
            "letter-spacing",
            "border",
            "border-color",
            "border-width",
            "border-style",
            "border-radius",
            "width",
            "height",
            "min-width",
            "max-width",
            "min-height",
            "max-height",
            "display",
            "visibility",
            "opacity",
            "gap",
            "justify-content",
            "align-items",
            "flex-direction",
        }
    )
    | MARGIN_PROPERTIES
    | _PADDING_PROPERTIES
)

#: ``flex-direction`` may not reverse an order (``row-reverse``,
#: ``column-reverse`` reorder visually, as ``order`` would).
FLEX_DIRECTIONS = frozenset({"row", "column"})

#: The only CSS functions a style value may call.
ALLOWED_STYLE_FUNCTIONS = frozenset(
    {"rgb", "rgba", "hsl", "hsla", "calc", "var", "min", "max", "clamp"}
)

#: A goal's event name. It may never be an exposure event type (see
#: :func:`exposure_event_types`): the tracking API stores an event's name as
#: its type, and those types mark an exposure.
EVENT_NAME_PATTERN = re.compile(r"^[a-z0-9_]{1,100}$")


def exposure_event_types() -> Tuple[str, ...]:
    """The event types that mark an exposure: the analysis's own
    ``event_matching.EXPOSURE_EVENT_TYPES``, imported, never copied.

    Imported here, not at the top of the module: ``backend.app.services``
    imports ``ExperimentService`` when the package loads, which imports
    ``schemas.experiment``, which imports this module, so a module-level
    import is circular for anything that imports this module first (the
    vectors generator does)."""
    from backend.app.services.event_matching import EXPOSURE_EVENT_TYPES

    return EXPOSURE_EVENT_TYPES


# --- the refusals (fixed text) -------------------------------------------------

SELECTOR_REFUSED = (
    "A selector is 1 to 512 characters, and cannot contain { } ; \\ @ <, the "
    "comment marks /* and */, a control character such as NUL, or a lone "
    "surrogate."
)
TEXT_REFUSED = "A NUL character cannot be part of the text."
ATTRIBUTE_VALUE_REFUSED = "An attribute value cannot contain a control character."
LINK_REFUSED = (
    "A link must be a full http:// or https:// URL without a user name or "
    "password, or a path on this site that starts with a single /."
)
IMAGE_REFUSED = (
    "An image must be a full http:// or https:// URL without a user name or "
    "password, or a path on this site that starts with a single /."
)
STYLE_COUNT_REFUSED = "A style change sets 1 to 20 properties."
STYLE_PROPERTY_REFUSED = (
    "This property cannot be changed. The properties a change may set are "
    "colours, fonts, text alignment and decoration, spacing, borders, sizes, "
    "display, visibility, opacity and flex alignment."
)
STYLE_VALUE_REFUSED = (
    "A style value is at most 200 characters, cannot contain ; { } < > \\ @ ! or "
    "a comment, and may call only rgb, rgba, hsl, hsla, calc, var, min, max and "
    "clamp."
)
MARGIN_REFUSED = "Spacing cannot be less than 0."
FLEX_DIRECTION_REFUSED = "Only row or column is accepted as a flex direction."
CHANGES_TOO_LARGE = "A variant's page changes are at most 64 KiB."
VERSION_REFUSED = "The format version must be 1."
PAGE_URL_REFUSED = (
    "A page is a full http:// or https:// URL with an ASCII host, without a "
    "user name, password, query, # fragment, backslash or space."
)
EDITOR_URL_REFUSED = (
    "The page the editor opens must be a full http:// or https:// URL with an "
    "ASCII host, without a user name, password, # fragment, backslash or "
    "space. It may carry a query."
)
EDITOR_URL_NOT_A_PAGE = (
    "The page the editor opens must be one of the experiment's pages."
)
EVENT_NAME_REFUSED = (
    "A goal's event_name is 1 to 100 lower-case letters, digits and "
    "underscores, and cannot be exposure or experiment_exposure."
)
DUPLICATE_GOAL_EVENT = "Each goal needs its own event_name."
GOAL_PAGE_HOST_REFUSED = "A goal's page must be on one of the experiment's sites."

#: The characters a selector may not contain (S1). Every other character is
#: accepted: ``/``, ``?``, ``&``, quotes inside an attribute value and letters
#: outside ASCII among them.
_SELECTOR_FORBIDDEN = frozenset("{};\\@<")
#: A CSS comment's opener and closer, each refused wherever it appears.
_COMMENT_MARKS = ("/*", "*/")
_STYLE_FORBIDDEN = frozenset(";{}<>\\@!")
#: Words that never belong in a value, whatever surrounds them.
_STYLE_FORBIDDEN_WORDS = ("expression", "-moz-binding", "behavior", "javascript")
_FUNCTION = re.compile(r"([A-Za-z_-][A-Za-z0-9_-]{0,63})\s{0,8}\(")
#: One margin token: 0, auto, or a non-negative number with px, em, rem or %.
_MARGIN_TOKEN = re.compile(
    r"^(?:0|auto|(?:\d{1,6}(?:\.\d{1,6})?|\.\d{1,6})(?:px|em|rem|%))$", re.I
)

#: Strict ``int``: ``True`` and ``1.0`` are refused where ``1`` is meant.
StrictInt = Annotated[int, Strict()]


def _visual_error(message: str, error_type: str = VISUAL_CHANGES_ERROR):
    return PydanticCustomError(error_type, message)


def _has_control(value: str, allow_whitespace: bool = False) -> bool:
    for character in value:
        if character == "\x00":
            return True
        if unicodedata.category(character) == "Cc":
            if allow_whitespace and character in "\n\t\r":
                continue
            return True
    return False


def selector_problem(selector: str) -> Optional[str]:
    """The refusal for *selector*, or ``None`` (S1).

    Refused: a selector that is not 1 to 512 characters (counted as Unicode
    code points), one that contains any of ``{ } ; \\ @ <``, the comment
    opener ``/*`` or closer ``*/``, a control character (NUL, tab and new
    line among them), or a lone surrogate (Unicode category Cs, which a JSON
    ``\\ud800`` escape can carry and UTF-8 cannot encode). Everything else is
    accepted. Each of the comment marks is its own check: ``/`` and ``*``
    alone are ordinary characters here."""
    if not isinstance(selector, str):
        return SELECTOR_REFUSED
    if not 1 <= len(selector) <= MAX_SELECTOR_LENGTH:
        return SELECTOR_REFUSED
    if any(character in _SELECTOR_FORBIDDEN for character in selector):
        return SELECTOR_REFUSED
    if any(mark in selector for mark in _COMMENT_MARKS):
        return SELECTOR_REFUSED
    if _has_control(selector):
        return SELECTOR_REFUSED
    if any(unicodedata.category(character) == "Cs" for character in selector):
        return SELECTOR_REFUSED
    return None


def _site_path_problem(value: str) -> bool:
    """Whether *value* is not a path on the page's own site: it must start with
    one ``/`` (``//evil`` and ``/\\evil`` are other sites) and pass the URL
    rules as the path of an https URL would."""
    if not value.startswith("/") or value[1:2] in ("/", "\\"):
        return True
    return url_problem("https://site.invalid" + value) is not None


def link_problem(value: str) -> bool:
    """Whether *value* cannot be a link: an absolute ``http(s)`` URL that
    passes ``url_problem`` (no user information, control characters,
    whitespace, backslash or fragment), or a path on this site. Every other
    scheme -- ``javascript:`` in any case or spelling, ``data:``,
    ``vbscript:``, ``blob:`` -- is refused."""
    if len(value) > MAX_ATTRIBUTE_VALUE_LENGTH:
        return True
    if value.startswith("/"):
        return _site_path_problem(value)
    if url_problem(value) is not None:
        return True
    return urlsplit(value).scheme not in ("http", "https")


def style_problem(prop: str, value: Any) -> Optional[str]:
    """The refusal for one ``set_style`` entry, or ``None`` (S1, C13)."""
    if prop not in ALLOWED_STYLE_PROPERTIES:
        return STYLE_PROPERTY_REFUSED
    if not isinstance(value, str) or not (1 <= len(value) <= MAX_STYLE_VALUE_LENGTH):
        return STYLE_VALUE_REFUSED
    if _has_control(value) or "/*" in value or "*/" in value:
        return STYLE_VALUE_REFUSED
    if any(character in _STYLE_FORBIDDEN for character in value):
        return STYLE_VALUE_REFUSED
    lowered = value.lower()
    if any(word in lowered for word in _STYLE_FORBIDDEN_WORDS):
        return STYLE_VALUE_REFUSED
    functions = [name.lower() for name in _FUNCTION.findall(value)]
    if any(name not in ALLOWED_STYLE_FUNCTIONS for name in functions):
        return STYLE_VALUE_REFUSED
    if prop in MARGIN_PROPERTIES:
        # No function here at all, not even var(): the page's own CSS can give
        # a variable a negative value (C13).
        tokens = value.split()
        if not (1 <= len(tokens) <= 4) or "(" in value:
            return MARGIN_REFUSED
        if not all(_MARGIN_TOKEN.match(token) for token in tokens):
            return MARGIN_REFUSED
    if prop == "flex-direction" and value.strip().lower() not in FLEX_DIRECTIONS:
        return FLEX_DIRECTION_REFUSED
    return None


# --- S1: the changes ---------------------------------------------------------


class _Change(BaseModel):
    """What every change has: the element it is aimed at."""

    model_config = ConfigDict(strict=True, extra="forbid")

    selector: str = Field(
        ...,
        description=(
            "A CSS selector for exactly one element: 1 to 512 characters, "
            "without { } ; \\ @ <, the comment marks /* and */, or a control "
            "character. A change whose selector matches no element, or more "
            "than one, is not applied."
        ),
    )

    @field_validator("selector")
    @classmethod
    def _selector(cls, value: str) -> str:
        problem = selector_problem(value)
        if problem is not None:
            raise _visual_error(problem)
        return value


class SetText(_Change):
    """Replace the element's text. Written as text, never as HTML."""

    op: Literal["set_text"]
    text: str = Field(..., max_length=MAX_TEXT_LENGTH)

    @field_validator("text")
    @classmethod
    def _text(cls, value: str) -> str:
        if "\x00" in value:
            raise _visual_error(TEXT_REFUSED)
        return value


class SetAttribute(_Change):
    """Set one of five attributes. ``href`` takes a link (see ``link_problem``)."""

    op: Literal["set_attribute"]
    name: Literal["alt", "title", "aria-label", "placeholder", "href"]
    value: str = Field(..., max_length=MAX_ATTRIBUTE_VALUE_LENGTH)

    @model_validator(mode="after")
    def _value(self) -> "SetAttribute":
        if _has_control(self.value):
            raise _change_error(("value",), ATTRIBUTE_VALUE_REFUSED)
        if self.name == "href" and link_problem(self.value):
            raise _change_error(("value",), LINK_REFUSED)
        return self


class SetImage(_Change):
    """Show another image: an ``http(s)`` URL or a path on this site."""

    op: Literal["set_image"]
    src: str = Field(..., max_length=MAX_ATTRIBUTE_VALUE_LENGTH)
    alt: Optional[str] = Field(None, max_length=MAX_ALT_LENGTH)

    @field_validator("src")
    @classmethod
    def _src(cls, value: str) -> str:
        if link_problem(value):
            raise _visual_error(IMAGE_REFUSED)
        return value

    @field_validator("alt")
    @classmethod
    def _alt(cls, value: Optional[str]) -> Optional[str]:
        if value is not None and _has_control(value):
            raise _visual_error(ATTRIBUTE_VALUE_REFUSED)
        return value


class Hide(_Change):
    """Hide the element, from assistive technology too."""

    op: Literal["hide"]


class SetStyle(_Change):
    """Set 1 to 20 properties from a fixed list, through the CSSOM."""

    op: Literal["set_style"]
    styles: Dict[str, str]

    @field_validator("styles")
    @classmethod
    def _styles(cls, value: Dict[str, str]) -> Dict[str, str]:
        if not (1 <= len(value) <= MAX_STYLES):
            raise _visual_error(STYLE_COUNT_REFUSED)
        for prop, style in value.items():
            problem = style_problem(prop, style)
            if problem is not None:
                raise _visual_error(problem)
        return value


def _change_error(loc: Tuple[Union[str, int], ...], message: str) -> ValidationError:
    return ValidationError.from_exception_data(
        "VisualChange",
        [
            InitErrorDetails(
                type=PydanticCustomError(VISUAL_CHANGES_ERROR, message),
                loc=loc,
                input=None,
            )
        ],
    )


VisualChange = Annotated[
    Union[SetText, SetAttribute, SetImage, Hide, SetStyle],
    Field(discriminator="op"),
]


class VisualChanges(BaseModel):
    """A variant's page changes: ``{"version": 1, "changes": [...]}``."""

    model_config = ConfigDict(strict=True, extra="forbid")

    version: StrictInt = Field(..., description="The format version: 1.")
    changes: List[VisualChange] = Field(
        ...,
        max_length=MAX_CHANGES,
        description=(
            "At most 100 changes, and at most 64 KiB in all. Each is one of "
            "set_text, set_attribute, set_image, hide and set_style."
        ),
    )

    @field_validator("version")
    @classmethod
    def _version(cls, value: int) -> int:
        if value != VISUAL_FORMAT_VERSION:
            raise _visual_error(VERSION_REFUSED)
        return value

    @model_validator(mode="after")
    def _size(self) -> "VisualChanges":
        if stored_size(self.model_dump(mode="json")) > MAX_CHANGES_BYTES:
            raise _visual_error(CHANGES_TOO_LARGE)
        return self


def stored_size(value: Any) -> int:
    """How many bytes *value* takes as compact UTF-8 JSON."""
    text = json.dumps(value, separators=(",", ":"), ensure_ascii=False)
    return len(text.encode("utf-8"))


# --- S2 and S10: the pages and the goals ------------------------------------


def page_url_problem(url: str) -> bool:
    """Whether *url* cannot be a page rule's URL (S2): it passes
    ``url_problem`` and carries no query."""
    return url_problem(url) is not None or "?" in url


class VisualPage(BaseModel):
    """A page rule: ``exact`` matches one path, ``prefix`` the path and every
    path below it. Matching ignores the scheme, the port, the query, the
    fragment and one trailing ``/``; the host is compared in lower case and
    the path exactly. No regular expressions."""

    model_config = ConfigDict(strict=True, extra="forbid")

    match: Literal["exact", "prefix"]
    url: str = Field(..., description=PAGE_URL_REFUSED)

    @field_validator("url")
    @classmethod
    def _url(cls, value: str) -> str:
        if page_url_problem(value):
            raise _visual_error(PAGE_URL_REFUSED, VISUAL_CONFIG_ERROR)
        return value


class _Goal(BaseModel):
    """What every goal has: the metric event it records, and its page."""

    model_config = ConfigDict(strict=True, extra="forbid")

    event_name: str = Field(..., description=EVENT_NAME_REFUSED)
    page: VisualPage

    @field_validator("event_name")
    @classmethod
    def _event_name(cls, value: str) -> str:
        if not EVENT_NAME_PATTERN.match(value) or value in exposure_event_types():
            raise _visual_error(EVENT_NAME_REFUSED, VISUAL_CONFIG_ERROR)
        return value


class ClickGoal(_Goal):
    """A click on the one element *selector* matches, on page *page*."""

    kind: Literal["click"]
    selector: str

    @field_validator("selector")
    @classmethod
    def _selector(cls, value: str) -> str:
        problem = selector_problem(value)
        if problem is not None:
            raise _visual_error(problem, VISUAL_CONFIG_ERROR)
        return value


class PageViewGoal(_Goal):
    """A view of page *page*."""

    kind: Literal["page_view"]


VisualGoal = Annotated[Union[ClickGoal, PageViewGoal], Field(discriminator="kind")]


class VisualConfig(BaseModel):
    """``experiments.visual_config``: the pages, the editor's page and the goals."""

    model_config = ConfigDict(strict=True, extra="forbid")

    version: StrictInt = Field(..., description="The format version: 1.")
    editor_url: str = Field(
        ...,
        description=(
            "The page the visual editor opens: on one of the pages, and like "
            "them except that it may carry a query."
        ),
    )
    pages: List[VisualPage] = Field(..., min_length=1, max_length=MAX_PAGES)
    goals: List[VisualGoal] = Field(
        default_factory=list,
        max_length=MAX_GOALS,
        description=(
            "0 to 10 goals. Each event_name must be the event_name of one of "
            "the experiment's metrics."
        ),
    )

    @field_validator("version")
    @classmethod
    def _version(cls, value: int) -> int:
        if value != VISUAL_FORMAT_VERSION:
            raise _visual_error(VERSION_REFUSED, VISUAL_CONFIG_ERROR)
        return value

    @field_validator("editor_url")
    @classmethod
    def _editor_url(cls, value: str) -> str:
        if url_problem(value) is not None:
            raise _visual_error(EDITOR_URL_REFUSED, VISUAL_CONFIG_ERROR)
        return value

    @model_validator(mode="after")
    def _across(self) -> "VisualConfig":
        parts = urlsplit(self.editor_url)
        if not any(page_matches(parts.netloc, parts.path, p) for p in self.pages):
            raise _config_error(("editor_url",), EDITOR_URL_NOT_A_PAGE)
        hosts = {normalize_host(urlsplit(p.url).netloc) for p in self.pages}
        seen = set()
        for index, goal in enumerate(self.goals):
            if goal.event_name in seen:
                raise _config_error(
                    ("goals", index, "event_name"), DUPLICATE_GOAL_EVENT
                )
            seen.add(goal.event_name)
            if normalize_host(urlsplit(goal.page.url).netloc) not in hosts:
                raise _config_error(("goals", index, "page"), GOAL_PAGE_HOST_REFUSED)
        return self


def _config_error(loc: Tuple[Union[str, int], ...], message: str) -> ValidationError:
    return ValidationError.from_exception_data(
        "VisualConfig",
        [
            InitErrorDetails(
                type=PydanticCustomError(VISUAL_CONFIG_ERROR, message),
                loc=loc,
                input=None,
            )
        ],
    )


# --- across fields: the design an experiment would end up with ----------------

CONTROL_CHANGES_REFUSED = (
    "The control variant shows the page as it is, so it cannot have page changes."
)
CHANGES_WITHOUT_CONFIG_REFUSED = (
    "A variant's page changes belong to a visual experiment: send "
    "visual_config, the pages the experiment runs on, with them."
)
GOAL_WITHOUT_METRIC_REFUSED = (
    "A goal's event_name must be the event_name of one of the experiment's metrics."
)
SPLIT_URL_VISUAL_REFUSED = (
    "A split URL experiment cannot be a visual experiment: it sends visitors to "
    "other pages instead of changing one."
)


def get_field(source: Any, field: str) -> Any:
    """A field of a dict, a schema or a row."""
    if isinstance(source, dict):
        return source.get(field)
    return getattr(source, field, None)


def is_split_url(experiment_type: Any) -> bool:
    return getattr(experiment_type, "value", experiment_type) == "split_url"


def _goal_event_names(visual_config: Any) -> List[str]:
    if isinstance(visual_config, VisualConfig):
        return [goal.event_name for goal in visual_config.goals]
    goals = get_field(visual_config, "goals") or []
    return [get_field(goal, "event_name") for goal in goals]


def visual_write_problem(
    experiment_type: Any,
    visual_config: Any,
    variants: Sequence[Any],
    metrics: Iterable[Any],
) -> Optional[Tuple[Tuple[Union[str, int], ...], str, str]]:
    """The first rule the design an experiment would end up with breaks, as
    ``(loc, message, error type)``, or ``None``.

    The arguments are the result of the write: what the request sent and,
    for what it left out, what is stored. ``variants`` and ``metrics`` are
    dicts, schemas or rows. The rules: a split URL experiment has no visual
    settings; page changes need visual settings and never sit on the
    control; every goal's event name is the event name of one of the metrics
    (S10). Each value has already passed its own schema. Pure.
    """
    if visual_config is not None and is_split_url(experiment_type):
        return ("visual_config",), SPLIT_URL_VISUAL_REFUSED, VISUAL_CONFIG_ERROR
    for index, variant in enumerate(variants):
        if get_field(variant, "visual_changes") is None:
            continue
        loc: Tuple[Union[str, int], ...] = ("variants", index, "visual_changes")
        if get_field(variant, "is_control"):
            return loc, CONTROL_CHANGES_REFUSED, VISUAL_CHANGES_ERROR
        if visual_config is None:
            return loc, CHANGES_WITHOUT_CONFIG_REFUSED, VISUAL_CHANGES_ERROR
    if visual_config is not None:
        measured = {get_field(metric, "event_name") for metric in metrics}
        for index, name in enumerate(_goal_event_names(visual_config)):
            if name not in measured:
                loc = ("visual_config", "goals", index, "event_name")
                return loc, GOAL_WITHOUT_METRIC_REFUSED, VISUAL_CONFIG_ERROR
    return None


def visual_write_error(
    title: str, problem: Tuple[Tuple[Union[str, int], ...], str, str]
) -> ValidationError:
    """:func:`visual_write_problem`'s answer as a 422 at its place."""
    loc, message, error_type = problem
    return ValidationError.from_exception_data(
        title,
        [
            InitErrorDetails(
                type=PydanticCustomError(error_type, message), loc=loc, input=None
            )
        ],
    )


def page_matches(host: str, path: str, rule: Any) -> bool:
    """Whether a page at *host* and *path* is one *rule* covers (S2).

    *rule* is a :class:`VisualPage` or its stored dict. *host* may carry a
    port; *path* may carry a query or a fragment: both are ignored. ``exact``
    means the same normalised path; ``prefix`` the same path or one that
    continues after a ``/`` (a rule path of ``/`` covers the whole host).
    Pure: no regular expression is built from either side.
    """
    match = rule.match if isinstance(rule, VisualPage) else rule["match"]
    url = rule.url if isinstance(rule, VisualPage) else rule["url"]
    parts = urlsplit(url)
    if normalize_host(host) != normalize_host(parts.netloc):
        return False
    wanted = normalize_path(parts.path)
    path = path.split("#", 1)[0].split("?", 1)[0]
    actual = normalize_path(path)
    if match == "exact":
        return actual == wanted
    return actual == wanted or wanted == "/" or actual.startswith(wanted + "/")
