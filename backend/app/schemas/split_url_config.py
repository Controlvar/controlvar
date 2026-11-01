"""
Pydantic schemas for Split URL experiment *configuration* — EP-036 Batch 1.

Defines the data structures for split URL experiment configuration, and the
rules a configuration that can split traffic follows (#393).

CORE. This module is pure declarative validation with no routing behaviour,
and it types ``ExperimentCreate.split_url_config`` /
``ExperimentUpdate.split_url_config``. Keeping it in the core is what stops
that field degrading to unvalidated JSON when the split_url module is not
installed. The routing itself — ``services/split_url_service`` and the
preview endpoint — is the module's. The rules come in three layers:

* **Per field**, wherever a configuration is parsed: at least two URL
  variants with unique names; each ``traffic_allocation`` from 0 to 100, the
  allocations summing to 100; each ``url`` an absolute ``http://`` or
  ``https://`` URL of at most 2048 characters with an ASCII host name, and
  without user information, a fragment, a backslash, whitespace or control
  characters. A URL is stored exactly as sent.
* **Across fields**, for a ``split_url`` experiment only
  (:func:`split_url_config_problem`): the URL variants name the experiment's
  variants one to one with the same allocations, and no other URL variant has
  the control URL's host and path.
* **Across experiments**, at ``POST /experiments/{id}/start`` only
  (:func:`split_url_conflict`): two active split URL experiments may not
  share an entry, and one's entry may not be another's destination.

Every refusal worded here is fixed text that names variants, never a URL.
"""

import unicodedata
from typing import Any, FrozenSet, List, NamedTuple, Optional, Sequence, Tuple, Union
from urllib.parse import urlsplit

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    ValidationInfo,
    field_validator,
    model_validator,
)
from pydantic_core import PydanticCustomError

#: The error ``type`` of every refusal below, in a 422 and wherever else a
#: problem is reported.
SPLIT_URL_CONFIG_ERROR = "split_url_config"

#: The longest URL a variant may have, in characters.
MAX_URL_LENGTH = 2048

#: How far a URL variant's ``traffic_allocation`` may be from its experiment
#: variant's.
ALLOCATION_TOLERANCE = 0.01

#: The description of a field that is kept with the configuration and that
#: nothing reads.
STORED_NOT_READ = "Stored; not read: nothing uses the stored value."

# The refusals, per field. ``{name}`` is the URL variant's name.
DUPLICATE_URL_VARIANT_NAME = (
    "URL variant names must be unique, but '{name}' is used more than once."
)
URL_NOT_ABSOLUTE = (
    "The URL for variant '{name}' must be a full URL that starts with http:// "
    "or https:// and has a host name."
)
URL_TOO_LONG = (
    "The URL for variant '{name}' is longer than 2048 characters. Use a shorter URL."
)
URL_HAS_USER_INFO = (
    "The URL for variant '{name}' must not contain a user name or password."
)
URL_HAS_FORBIDDEN_CHARACTER = (
    "The URL for variant '{name}' must not contain a # fragment, a backslash, "
    "a space or a control character."
)
URL_HOST_NOT_ASCII = (
    "The host name in the URL for variant '{name}' must be ASCII. Write an "
    "internationalised name in its xn-- form."
)

# The refusals across fields, for a split_url experiment. ``{names}`` is the
# experiment's variant names in order, separated by ", ".
UNKNOWN_URL_VARIANT = (
    "URL variant '{name}' is not one of this experiment's variants: {names}."
)
MISSING_URL_VARIANT = (
    "Experiment variant '{name}' has no URL variant. Add a URL variant named '{name}'."
)
DUPLICATE_VARIANT_NAME = (
    "This experiment's variant names must be unique, but '{name}' is used more "
    "than once."
)
ALLOCATION_MISMATCH = (
    "URL variant '{name}' has a different traffic_allocation from the "
    "experiment variant of the same name. Make them equal; the experiment's "
    "variants decide the split."
)
DESTINATION_IS_ENTRY = (
    "The URL for variant '{name}' has the same host and path as the control "
    "URL, so a visitor sent there would be sent again. Give it a different "
    "path or host; a different query string is not enough."
)

#: :func:`split_url_conflict`'s answers.
SAME_ENTRY = "same_entry"
CHAIN = "chain"

#: Characters no variant URL may contain, besides whitespace and controls.
_FORBIDDEN_CHARACTERS = frozenset("#\\")


def _forbidden(character: str) -> bool:
    return (
        character in _FORBIDDEN_CHARACTERS
        or character.isspace()
        or unicodedata.category(character) == "Cc"
    )


def url_problem(url: str) -> Optional[str]:
    """The per-field refusal *url* earns, unformatted, or ``None``."""
    if len(url) > MAX_URL_LENGTH:
        return URL_TOO_LONG
    if any(_forbidden(character) for character in url):
        return URL_HAS_FORBIDDEN_CHARACTER
    try:
        parts = urlsplit(url)
        parts.port  # a port that is not a number raises ValueError
    except ValueError:
        return URL_NOT_ABSOLUTE
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return URL_NOT_ABSOLUTE
    if "@" in parts.netloc:
        return URL_HAS_USER_INFO
    if not parts.hostname.isascii():
        return URL_HOST_NOT_ASCII
    return None


class SplitUrlVariant(BaseModel):
    """A single URL variant in a split URL experiment."""

    # The config is stored as JSONB, which cannot hold NaN or an infinity
    # (and NaN passes the sum-to-100 check below): refuse them with 422 (#559).
    model_config = ConfigDict(from_attributes=True, allow_inf_nan=False)

    name: str = Field(
        ...,
        description=(
            "The name of the experiment variant this URL is for. In a split_url "
            "experiment each URL variant names one of the experiment's variants, "
            "and each of those has exactly one URL variant."
        ),
    )
    url: str = Field(
        ...,
        description=(
            "The page for this variant: an absolute http:// or https:// URL of "
            "at most 2048 characters, with an ASCII host name (an "
            "internationalised name in its xn-- form), and without a user name "
            "or password, a # fragment, a backslash, whitespace or control "
            "characters. Stored exactly as sent."
        ),
    )
    # A percentage, from 0 to 100 inclusive (#1194): -50 and 150 used to pass
    # while the sum was right.
    traffic_allocation: float = Field(
        ...,
        ge=0,
        le=100,
        description=(
            "Percentage of traffic, from 0 to 100. In a split_url experiment it "
            "must equal, within 0.01, the traffic_allocation of the experiment "
            "variant with the same name: the experiment's variants decide the "
            "split."
        ),
    )

    @field_validator("url")
    @classmethod
    def validate_url(cls, url: str, info: ValidationInfo) -> str:
        """Refuse a URL no visitor could be sent to; return it unchanged."""
        name = info.data.get("name")
        if name is None:
            # The name was refused on its own, so the variant is too.
            return url
        problem = url_problem(url)
        if problem is not None:
            raise PydanticCustomError(SPLIT_URL_CONFIG_ERROR, problem.format(name=name))
        return url


class SplitUrlConfig(BaseModel):
    """
    Configuration for a split URL experiment.

    Requires at least 2 URL variants with unique names, whose
    traffic_allocation values are each from 0 to 100 and sum to 100, and whose
    URLs are absolute http or https URLs (see each field).
    """

    model_config = ConfigDict(from_attributes=True)

    variants: List[SplitUrlVariant] = Field(
        ...,
        description=(
            "One URL per variant: at least two, with unique names, whose "
            "traffic_allocation values add up to 100. In a split_url experiment "
            "the URL variant named like the experiment's control variant gives "
            "the control URL, and no other URL variant may have the control "
            "URL's host and path."
        ),
    )
    cookie_name: Optional[str] = Field(None, description=STORED_NOT_READ)
    cookie_ttl_days: int = Field(30, description=STORED_NOT_READ)
    canonical_url: Optional[str] = Field(None, description=STORED_NOT_READ)

    @field_validator("variants")
    @classmethod
    def validate_variants(cls, v: List[SplitUrlVariant]) -> List[SplitUrlVariant]:
        """Ensure at least 2 URL variants are provided, with unique names."""
        if len(v) < 2:
            raise ValueError("At least 2 URL variants required")
        seen = set()
        for variant in v:
            if variant.name in seen:
                raise PydanticCustomError(
                    SPLIT_URL_CONFIG_ERROR,
                    DUPLICATE_URL_VARIANT_NAME.format(name=variant.name),
                )
            seen.add(variant.name)
        return v

    @model_validator(mode="after")
    def validate_traffic_sum(self) -> "SplitUrlConfig":
        """Ensure traffic allocations sum to 100 (within floating-point tolerance)."""
        total = sum(v.traffic_allocation for v in self.variants)
        if abs(total - 100.0) > 0.01:
            raise ValueError(f"Traffic allocations must sum to 100, got {total}")
        return self


# ---------------------------------------------------------------------------
# Entries: a URL's host and normalised path
# ---------------------------------------------------------------------------


def normalize_host(host: str) -> str:
    """A host as entries compare it: lower case, without a ``:port``.

    *host* is a Host header's value or a URL's ``host[:port]``. An IPv6
    literal keeps its brackets.
    """
    value = host.lower()
    if value.startswith("["):
        end = value.find("]")
        return value[: end + 1] if end != -1 else value
    return value.split(":", 1)[0]


def normalize_path(path: str) -> str:
    """A path as entries compare it: one trailing ``/`` removed except at the
    root, an empty path read as ``/``. Case and percent-encoding are kept."""
    if not path:
        return "/"
    if len(path) > 1 and path.endswith("/"):
        return path[:-1]
    return path


def entry(url: str) -> Tuple[str, str]:
    """A variant URL's entry: its normalised host and path. The query is not
    part of it. *url* is one the per-field rules accept."""
    parts = urlsplit(url)
    return normalize_host(parts.netloc), normalize_path(parts.path)


# ---------------------------------------------------------------------------
# Across fields: the configuration against the experiment's variants
# ---------------------------------------------------------------------------


class SplitUrlConfigProblem(NamedTuple):
    """Why a configuration cannot split: where, inside ``split_url_config``,
    and the fixed text that says so."""

    loc: Tuple[Union[str, int], ...]
    message: str


def _get(variant: Any, field: str) -> Any:
    """A field of an experiment variant given as a dict, a schema or a row."""
    if isinstance(variant, dict):
        return variant.get(field)
    return getattr(variant, field, None)


def _first_problem(error: ValidationError) -> SplitUrlConfigProblem:
    """A stored configuration's first per-field refusal, as a problem."""
    first = error.errors(include_url=False, include_input=False)[0]
    loc = tuple(first["loc"])
    if first["type"] == SPLIT_URL_CONFIG_ERROR:
        return SplitUrlConfigProblem(loc, first["msg"])
    message = first["msg"].removeprefix("Value error, ")
    where = ".".join(str(part) for part in loc)
    text = f"{where}: {message}" if where else message
    if not text.endswith("."):
        text += "."
    return SplitUrlConfigProblem(loc, text)


def _parse(config: Any) -> Union[SplitUrlConfig, SplitUrlConfigProblem]:
    if isinstance(config, SplitUrlConfig):
        return config
    try:
        return SplitUrlConfig.model_validate(config)
    except ValidationError as error:
        return _first_problem(error)


def _control_name(variants: Sequence[Any]) -> Optional[str]:
    """The name of the first experiment variant marked as the control."""
    return next((_get(v, "name") for v in variants if _get(v, "is_control")), None)


def find_split_url_config_problem(
    config: Any, variants: Sequence[Any]
) -> Optional[SplitUrlConfigProblem]:
    """The first reason *config* cannot split a split_url experiment whose
    variants are *variants*, with where it is, or ``None``.

    *config* is a :class:`SplitUrlConfig`, or a stored one as JSON, which is
    held to the per-field rules first. *variants* are the experiment's
    variants in order, as dicts, schemas or rows with ``name``, ``is_control``
    and ``traffic_allocation``. The rules, in order: the experiment's variant
    names are unique; every URL variant names an experiment variant; every
    experiment variant has a URL variant; each URL variant's allocation equals
    its experiment variant's within 0.01; no other URL variant has the control
    URL's host and path. Pure: it reads nothing else and changes nothing.
    """
    parsed = _parse(config)
    if isinstance(parsed, SplitUrlConfigProblem):
        return parsed

    names = [_get(v, "name") for v in variants]
    seen = set()
    for name in names:
        if name in seen:
            return SplitUrlConfigProblem((), DUPLICATE_VARIANT_NAME.format(name=name))
        seen.add(name)
    by_name = dict(zip(names, variants))

    listed = ", ".join(str(name) for name in names)
    for index, url_variant in enumerate(parsed.variants):
        if url_variant.name not in by_name:
            return SplitUrlConfigProblem(
                ("variants", index, "name"),
                UNKNOWN_URL_VARIANT.format(name=url_variant.name, names=listed),
            )

    url_names = {url_variant.name for url_variant in parsed.variants}
    for name in names:
        if name not in url_names:
            return SplitUrlConfigProblem(
                ("variants",), MISSING_URL_VARIANT.format(name=name)
            )

    for index, url_variant in enumerate(parsed.variants):
        expected = _get(by_name[url_variant.name], "traffic_allocation")
        if (
            not isinstance(expected, (int, float))
            or abs(url_variant.traffic_allocation - expected) > ALLOCATION_TOLERANCE
        ):
            return SplitUrlConfigProblem(
                ("variants", index, "traffic_allocation"),
                ALLOCATION_MISMATCH.format(name=url_variant.name),
            )

    control = _control_name(variants)
    if control is None:
        return None
    control_url = next(v.url for v in parsed.variants if v.name == control)
    control_entry = entry(control_url)
    for index, url_variant in enumerate(parsed.variants):
        if url_variant.name != control and entry(url_variant.url) == control_entry:
            return SplitUrlConfigProblem(
                ("variants", index, "url"),
                DESTINATION_IS_ENTRY.format(name=url_variant.name),
            )
    return None


def split_url_config_problem(config: Any, variants: Sequence[Any]) -> Optional[str]:
    """The text of :func:`find_split_url_config_problem`'s answer, or ``None``."""
    problem = find_split_url_config_problem(config, variants)
    return None if problem is None else problem.message


# ---------------------------------------------------------------------------
# Across experiments: entries and destinations
# ---------------------------------------------------------------------------


class SplitUrlEntries(NamedTuple):
    """Where a split URL experiment takes visitors from, and to."""

    #: The control URL's entry.
    entry: Tuple[str, str]
    #: The other URL variants' entries.
    destinations: FrozenSet[Tuple[str, str]]


def split_url_entries(
    config: Any, variants: Sequence[Any]
) -> Optional[SplitUrlEntries]:
    """The entry and destinations of a split_url experiment, or ``None`` when
    its configuration fails the rules above (or it has no control variant),
    so that a caller can skip it."""
    if not isinstance(config, SplitUrlConfig) and not config:
        return None
    if find_split_url_config_problem(config, variants) is not None:
        return None
    control = _control_name(variants)
    if control is None:
        return None
    parsed = _parse(config)
    control_url = next(v.url for v in parsed.variants if v.name == control)
    return SplitUrlEntries(
        entry=entry(control_url),
        destinations=frozenset(
            entry(v.url) for v in parsed.variants if v.name != control
        ),
    )


def split_url_conflict(mine: SplitUrlEntries, theirs: SplitUrlEntries) -> Optional[str]:
    """Whether two split URL experiments may be active together.

    :data:`SAME_ENTRY` when they have the same entry; :data:`CHAIN` when one's
    entry is the other's destination, either way round (refusing every such
    link refuses chains and cycles of any length); otherwise ``None``.
    """
    if mine.entry == theirs.entry:
        return SAME_ENTRY
    if mine.entry in theirs.destinations or theirs.entry in mine.destinations:
        return CHAIN
    return None
