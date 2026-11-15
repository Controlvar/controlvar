"""
Split URL routing: where a server or edge sends each visitor (#393).

MODULE: split_url. The API decides in one place, the code that answers
``POST /api/v1/tracking/assign``; this module adds only what a redirect needs
around that answer, as pure helpers and two reads:

* :func:`routes_table`: every ACTIVE split URL experiment's entry (the
  control URL's host and path), for ``GET /api/v1/tracking/split-url/routes``.
  An experiment whose stored ``split_url_config`` fails the rules is left
  out, and so is one whose entry another listed experiment already has, or
  that would chain with one (the rules ``POST /experiments/{id}/start``
  applies), taking the experiments in ``start_date`` order, ties by id.
* :func:`stored_or_new_variant`: the variant ``/tracking/assign`` gives a
  visitor now, without recording anything: their stored assignment, else the
  variant a new visitor with that id gets (the bandit weights for a bandit,
  else the traffic allocation). It checks no eligibility rule. The preview
  answers with it.
* :func:`location`, :func:`merge_query`, :func:`is_bot`: pure.

The rules a configuration follows, and the host and path normalisation that
entries are compared with, are the core's
(``backend/app/schemas/split_url_config.py``), so ``/start`` and these routes
compare entries identically.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, NamedTuple, Optional, Tuple
from urllib.parse import urlsplit

from sqlalchemy.orm import Session, joinedload

from backend.app.models.experiment import (
    Experiment,
    ExperimentStatus,
    ExperimentType,
    Variant,
)
from backend.app.schemas.split_url_config import (
    SplitUrlConfig,
    SplitUrlEntries,
    normalize_host,
    split_url_conflict,
    split_url_entries,
)
from backend.app.services.assignment_service import AssignmentService
from backend.app.services.bandit_routing import bandit_chooser

logger = logging.getLogger(__name__)

#: How long a routes table may be kept, in seconds: the ``max_age`` of the
#: answer and of its ``Cache-Control: private, max-age=``.
ROUTES_MAX_AGE = 60

#: The case-insensitive substrings that mark a User-Agent as a crawler. Plain
#: substrings, no pattern: only the first :data:`BOT_INSPECTED_CHARACTERS`
#: characters of a User-Agent are looked at.
BOT_SUBSTRINGS: Tuple[str, ...] = (
    "googlebot",
    "bingbot",
    "yandexbot",
    "duckduckbot",
    "applebot",
    "slurp",
    "facebookexternalhit",
    "twitterbot",
    "linkedinbot",
    "ahrefsbot",
    "semrushbot",
    "petalbot",
    "crawler",
    "spider",
)

#: How much of a User-Agent :func:`is_bot` reads.
BOT_INSPECTED_CHARACTERS = 512

#: The one warning for each experiment the routes table leaves out. The
#: experiment id is the only value in either.
ROUTE_OMITTED_NOT_VALID = (
    "Split URL routes table left out experiment %s: its stored split_url_config "
    "is not valid"
)
ROUTE_OMITTED_CONFLICT = (
    "Split URL routes table left out experiment %s: an experiment started "
    "before it has the same control page, or the two would chain"
)


class SplitUrlRoute(NamedTuple):
    """One entry of the routes table."""

    experiment_key: str
    experiment_id: str
    host: str
    path: str


def is_bot(user_agent: Optional[str]) -> bool:
    """Whether *user_agent* names a crawler: one of :data:`BOT_SUBSTRINGS`,
    in any case, within its first :data:`BOT_INSPECTED_CHARACTERS`
    characters."""
    if not user_agent:
        return False
    head = user_agent[:BOT_INSPECTED_CHARACTERS].lower()
    return any(substring in head for substring in BOT_SUBSTRINGS)


def merge_query(url: str, query: Optional[str]) -> str:
    """*url* with *query* added after its own parameters.

    ``?`` when *url* has no query, ``&`` when it has one; an empty *query*
    leaves *url* as it is. Neither is decoded or re-encoded.
    """
    if not query:
        return url
    if url.endswith("?"):
        return url + query
    if urlsplit(url).query:
        return f"{url}&{query}"
    return f"{url}?{query}"


def same_host(url: str, other_url: str) -> bool:
    """Whether two URLs name the same host, compared as entries compare
    hosts: without case, and without the port."""
    return normalize_host(urlsplit(url).netloc) == normalize_host(
        urlsplit(other_url).netloc
    )


def location(variant_url: str, control_url: str, query: Optional[str]) -> str:
    """Where a visitor assigned to the variant at *variant_url* is sent.

    The variant's stored URL, plus the visitor's *query* when the variant is
    on the control URL's host (a different port is the same host). A variant
    on another host gets its stored URL alone. Nothing from the request's
    path or host is used.
    """
    if same_host(variant_url, control_url):
        return merge_query(variant_url, query)
    return variant_url


def parsed_config(experiment: Experiment) -> SplitUrlConfig:
    """The experiment's stored configuration, parsed. Call it only once
    :func:`entries_of` has answered, which means it passes the rules."""
    config: Any = experiment.split_url_config
    if isinstance(config, SplitUrlConfig):
        return config
    return SplitUrlConfig.model_validate(config)


def variant_urls(experiment: Experiment) -> Dict[str, str]:
    """The URL of each URL variant of the experiment, by variant name."""
    return {variant.name: variant.url for variant in parsed_config(experiment).variants}


def control_variant(experiment: Experiment) -> Optional[Variant]:
    """The experiment variant marked as the control, or ``None``."""
    return next((v for v in experiment.variants or [] if v.is_control), None)


def entries_of(experiment: Experiment) -> Optional[SplitUrlEntries]:
    """The experiment's entry and destinations, or ``None`` when its stored
    configuration fails the rules (or it has no control variant)."""
    return split_url_entries(experiment.split_url_config, experiment.variants)


def routes_table(db: Session) -> List[SplitUrlRoute]:
    """Every ACTIVE split URL experiment with a key whose configuration passes
    the rules, as ``(experiment_key, experiment_id, host, path)``, ordered by
    host, then path.

    Taken in ``start_date`` order (ties by the lowest id), an experiment is
    left out, with one warning, when its configuration fails the rules, or
    when an experiment already listed has the same entry or would chain with
    it: the predicate ``POST /experiments/{id}/start`` refuses with, applied
    here too because a scheduled start or resume, or two starts at the same
    moment, are not checked against each other there.
    """
    experiments = (
        db.query(Experiment)
        .options(joinedload(Experiment.variants))
        .filter(
            Experiment.experiment_type == ExperimentType.SPLIT_URL,
            Experiment.status == ExperimentStatus.ACTIVE,
            Experiment.key.isnot(None),
        )
        .order_by(Experiment.start_date.asc().nullslast(), Experiment.id.asc())
        .all()
    )
    listed: List[Tuple[Experiment, SplitUrlEntries]] = []
    for experiment in experiments:
        entries = entries_of(experiment)
        if entries is None:
            logger.warning(ROUTE_OMITTED_NOT_VALID, experiment.id)
            continue
        if any(split_url_conflict(entries, other) for _, other in listed):
            logger.warning(ROUTE_OMITTED_CONFLICT, experiment.id)
            continue
        listed.append((experiment, entries))
    routes = [
        SplitUrlRoute(
            experiment_key=experiment.key,
            experiment_id=str(experiment.id),
            host=entries.entry[0],
            path=entries.entry[1],
        )
        for experiment, entries in listed
    ]
    return sorted(routes, key=lambda route: (route.host, route.path))


def new_visitor_variant(
    db: Session, experiment: Experiment, user_id: str
) -> Optional[Variant]:
    """The variant ``/tracking/assign`` gives *user_id* as a new visitor:
    the bandit weights' choice for a bandit experiment, else the traffic
    allocation's hash. Reads only; checks no eligibility rule."""
    chosen = bandit_chooser(db, experiment)(user_id)
    if chosen is None:
        chosen = AssignmentService(db)._hash_user_to_variant(user_id, experiment)
    return next(
        (v for v in experiment.variants or [] if str(v.id) == str(chosen)), None
    )


def stored_or_new_variant(
    db: Session, experiment: Experiment, user_id: str
) -> Optional[Variant]:
    """The variant *user_id* gets now, without recording anything: their
    stored assignment's, else :func:`new_visitor_variant`."""
    stored = AssignmentService(db)._stored_assignment(user_id, experiment.id)
    if stored is not None:
        return next(
            (
                v
                for v in experiment.variants or []
                if str(v.id) == str(stored.variant_id)
            ),
            None,
        )
    return new_visitor_variant(db, experiment, user_id)
