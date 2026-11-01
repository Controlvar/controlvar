"""A split URL configuration that can never split is refused, over HTTP (#393).

Before this, create and update checked only that a configuration had two URL
variants whose allocations added up to 100, and start checked nothing about
it. A split URL experiment could be stored, and started, with URL variants
named after none of its variants, with another split, with a relative or
``javascript:`` URL, with a variant URL equal to its control URL, or with a
control page another running split URL experiment already used. These tests
send real requests and read what was stored:

* create answers 422 at the rule's exact ``loc`` with its fixed text, and
  writes nothing. In the core profile too: the body is refused before the
  route answers 501;
* update checks the result of the update: a request that sends ``variants``
  alone is checked against the stored configuration;
* start answers 400, in this order: no configuration, a configuration that
  fails the rules, the same control page (host and path) as another active
  split URL experiment, and a chain (one's control page is the other's
  variant page, either way round). The same path on another host starts;
* another active split URL experiment whose stored configuration fails the
  rules (one written before them) is skipped, with one warning, and start of
  a valid experiment still answers 200, never 500;
* the preview of a stored configuration that breaks the per-field rules
  answers 409 (full profile).

Experiments that must already be split URL experiments are written as an a_b
create followed by a direct change of the row, so the same tests run in the
core profile, where creating one answers 501. The database is shared by the
session, so each test uses its own host names and counts only its own rows.
"""

from __future__ import annotations

import copy
import logging
import uuid
from typing import Any, Dict, List, Optional

import pytest
from sqlalchemy import Text, cast

from backend.app.models.experiment import (
    Experiment,
    ExperimentStatus,
    ExperimentType,
    Variant,
)

pytestmark = [pytest.mark.integration, pytest.mark.requires_db, pytest.mark.regression]

EXPERIMENTS = "/api/v1/experiments"
VARIANTS = [
    {"name": "Control", "is_control": True, "traffic_allocation": 50},
    {"name": "Treatment", "is_control": False, "traffic_allocation": 50},
]
FIX_HINT = (
    "Change it with PUT /api/v1/experiments/{id}; the dashboard cannot edit "
    "split_url_config."
)
NO_CONFIG = (
    "This split URL experiment has no split_url_config, so it cannot start. Add "
    "one with PUT /api/v1/experiments/{id}; the dashboard cannot edit "
    "split_url_config."
)
SAME_ENTRY = (
    "Another active split URL experiment already uses the same control page (host "
    "and path) as this experiment. Two active split URL experiments cannot share "
    "a control page. Pause or complete the other one, or change this "
    "experiment's control URL with PUT /api/v1/experiments/{id}; the dashboard "
    "cannot edit split_url_config."
)
CHAIN = (
    "Split URL experiments must not chain: this experiment's control page is a "
    "variant page of another active split URL experiment, or one of its variant "
    "pages is the other's control page, so a visitor could be sent from one test "
    "into the other. Pause or complete the other experiment, or change the URLs "
    "here with PUT /api/v1/experiments/{id}; the dashboard cannot edit "
    "split_url_config."
)
UNKNOWN_B = (
    "URL variant 'B' is not one of this experiment's variants: Control, Treatment."
)
NOT_ABSOLUTE = (
    "The URL for variant 'Control' must be a full URL that starts with http:// or "
    "https:// and has a host name."
)
SKIPPED = (
    "Split URL start check skipped experiment {id}: its stored split_url_config "
    "is not valid"
)
PREVIEW_409 = (
    "split_url_config is not valid; update it with PUT /api/v1/experiments/{id}"
)


def _host() -> str:
    """A host name no other test uses."""
    return f"s{uuid.uuid4().hex[:12]}.example"


def _name() -> str:
    return f"split url rules {uuid.uuid4().hex[:8]}"


def _uv(name: str, url: str, allocation: float = 50) -> Dict[str, Any]:
    return {"name": name, "url": url, "traffic_allocation": allocation}


def _config(control: str, treatment: str) -> Dict[str, Any]:
    return {"variants": [_uv("Control", control), _uv("Treatment", treatment)]}


def _body(name: str, **extra) -> Dict[str, Any]:
    return {
        "name": name,
        "experiment_type": "a_b",
        "variants": copy.deepcopy(VARIANTS),
        "metrics": [
            {
                "name": "Purchase",
                "event_name": "purchase",
                "metric_type": "conversion",
                "is_primary": True,
            }
        ],
    } | extra


def _split_url_row(
    client,
    db_session,
    config: Optional[Dict[str, Any]],
    status: Optional[ExperimentStatus] = None,
    experiment_type: ExperimentType = ExperimentType.SPLIT_URL,
) -> str:
    """A draft made through the API, then turned into a split URL experiment
    with *config* (stored as given, as a row written before the rules)."""
    created = client.post(f"{EXPERIMENTS}/", json=_body(_name()))
    assert created.status_code == 201, created.text
    experiment_id = created.json()["id"]
    values: Dict[str, Any] = {
        "experiment_type": experiment_type,
        "split_url_config": copy.deepcopy(config),
    }
    if status is not None:
        values["status"] = status
    db_session.query(Experiment).filter(
        Experiment.id == uuid.UUID(experiment_id)
    ).update(values)
    db_session.commit()
    return experiment_id


def _row(db_session, experiment_id: str) -> Experiment:
    db_session.expire_all()
    return db_session.get(Experiment, uuid.UUID(experiment_id))


def _stored_text(db_session, experiment_id: str) -> str:
    db_session.expire_all()
    return (
        db_session.query(cast(Experiment.split_url_config, Text))
        .filter(Experiment.id == uuid.UUID(experiment_id))
        .scalar()
    )


def _variant_names(db_session, experiment_id: str) -> List[str]:
    db_session.expire_all()
    return [
        v.name
        for v in db_session.query(Variant)
        .filter(Variant.experiment_id == uuid.UUID(experiment_id))
        .order_by(Variant.created_at, Variant.id)
    ]


def _start(client, experiment_id: str):
    return client.post(f"{EXPERIMENTS}/{experiment_id}/start")


def _refused(response, loc: List[Any], msg: str) -> None:
    assert response.status_code == 422, response.text
    assert response.json() == {
        "detail": [{"type": "split_url_config", "loc": loc, "msg": msg}]
    }


# ---------------------------------------------------------------------------
# Create
# ---------------------------------------------------------------------------

THREE = VARIANTS + [{"name": "Third", "is_control": False, "traffic_allocation": 0}]
NAMED_ALIKE = [
    VARIANTS[0],
    {"name": "Control", "is_control": False, "traffic_allocation": 50},
]

#: (id, URL variants, experiment variants, loc after "body", text)
CREATE_REFUSED = [
    (
        "F1-url-variant-names-repeat",
        [_uv("Control", "https://h.example/a"), _uv("Control", "https://h.example/b")],
        VARIANTS,
        ["split_url_config", "variants"],
        "URL variant names must be unique, but 'Control' is used more than once.",
    ),
    (
        "F2-relative-url",
        [_uv("Control", "/a"), _uv("Treatment", "https://h.example/b")],
        VARIANTS,
        ["split_url_config", "variants", 0, "url"],
        NOT_ABSOLUTE,
    ),
    (
        "F2-javascript-url",
        [
            _uv("Control", "https://h.example/a"),
            _uv("Treatment", "javascript:alert(1)"),
        ],
        VARIANTS,
        ["split_url_config", "variants", 1, "url"],
        NOT_ABSOLUTE.replace("Control", "Treatment"),
    ),
    (
        "F3-longer-than-2048",
        [
            _uv("Control", "https://h.example/" + "a" * 2032),
            _uv("Treatment", "https://h.example/b"),
        ],
        VARIANTS,
        ["split_url_config", "variants", 0, "url"],
        "The URL for variant 'Control' is longer than 2048 characters. Use a "
        "shorter URL.",
    ),
    (
        "F4-user-information",
        [
            _uv("Control", "https://u:p@h.example/a"),
            _uv("Treatment", "https://h.example/b"),
        ],
        VARIANTS,
        ["split_url_config", "variants", 0, "url"],
        "The URL for variant 'Control' must not contain a user name or password.",
    ),
    (
        "F5-backslash",
        [
            _uv("Control", "https://h.example/a"),
            _uv("Treatment", "https://evil.example\\@shop.example/checkout-v2"),
        ],
        VARIANTS,
        ["split_url_config", "variants", 1, "url"],
        "The URL for variant 'Treatment' must not contain a # fragment, a "
        "backslash, a space or a control character.",
    ),
    (
        "F6-host-not-ascii",
        [
            _uv("Control", "https://bücher.example/a"),
            _uv("Treatment", "https://h.example/b"),
        ],
        VARIANTS,
        ["split_url_config", "variants", 0, "url"],
        "The host name in the URL for variant 'Control' must be ASCII. Write an "
        "internationalised name in its xn-- form.",
    ),
    (
        "X1-not-an-experiment-variant",
        [_uv("Control", "https://h.example/a"), _uv("B", "https://h.example/b")],
        VARIANTS,
        ["split_url_config", "variants", 1, "name"],
        UNKNOWN_B,
    ),
    (
        "X2-experiment-variant-without-a-url",
        [
            _uv("Control", "https://h.example/a"),
            _uv("Treatment", "https://h.example/b"),
        ],
        THREE,
        ["split_url_config", "variants"],
        "Experiment variant 'Third' has no URL variant. Add a URL variant named "
        "'Third'.",
    ),
    (
        "X3-experiment-variant-names-repeat",
        [
            _uv("Control", "https://h.example/a"),
            _uv("Treatment", "https://h.example/b"),
        ],
        NAMED_ALIKE,
        ["split_url_config"],
        "This experiment's variant names must be unique, but 'Control' is used more "
        "than once.",
    ),
    (
        "X4-another-split",
        [
            _uv("Control", "https://h.example/a", 90),
            _uv("Treatment", "https://h.example/b", 10),
        ],
        VARIANTS,
        ["split_url_config", "variants", 0, "traffic_allocation"],
        "URL variant 'Control' has a different traffic_allocation from the "
        "experiment variant of the same name. Make them equal; the experiment's "
        "variants decide the split.",
    ),
    (
        "X5-variant-url-is-the-control-page",
        [
            _uv("Control", "https://h.example/checkout"),
            _uv("Treatment", "https://H.example/checkout/?v=2"),
        ],
        VARIANTS,
        ["split_url_config", "variants", 1, "url"],
        "The URL for variant 'Treatment' has the same host and path as the control "
        "URL, so a visitor sent there would be sent again. Give it a different path "
        "or host; a different query string is not enough.",
    ),
]


@pytest.mark.parametrize(
    "url_variants, variants, loc, text",
    [r[1:] for r in CREATE_REFUSED],
    ids=[r[0] for r in CREATE_REFUSED],
)
def test_create_refuses_a_config_that_can_never_split_at_its_loc(
    admin_client, db_session, url_variants, variants, loc, text
):
    name = _name()
    body = _body(
        name,
        experiment_type="split_url",
        variants=variants,
        split_url_config={"variants": url_variants},
    )

    response = admin_client.post(f"{EXPERIMENTS}/", json=body)

    _refused(response, ["body", *loc], text)
    assert db_session.query(Experiment).filter_by(name=name).count() == 0


@pytest.mark.modules
def test_create_accepts_a_variant_with_the_control_path_on_another_host(admin_client):
    host = _host()
    response = admin_client.post(
        f"{EXPERIMENTS}/",
        json=_body(
            _name(),
            experiment_type="split_url",
            split_url_config=_config(
                f"https://{host}/checkout", f"https://landing.{host}/checkout"
            ),
        ),
    )
    assert response.status_code == 201, response.text


# ---------------------------------------------------------------------------
# Update: the result of the update is checked
# ---------------------------------------------------------------------------


def test_update_refuses_a_config_that_does_not_match_and_writes_nothing(
    admin_client, db_session
):
    host = _host()
    experiment_id = _split_url_row(
        admin_client, db_session, _config(f"https://{host}/a", f"https://{host}/b")
    )
    before = _stored_text(db_session, experiment_id)

    response = admin_client.put(
        f"{EXPERIMENTS}/{experiment_id}",
        json={
            "split_url_config": {
                "variants": [
                    _uv("Control", f"https://{host}/a"),
                    _uv("B", f"https://{host}/b"),
                ]
            }
        },
    )

    _refused(response, ["body", "split_url_config", "variants", 1, "name"], UNKNOWN_B)
    assert _stored_text(db_session, experiment_id) == before


def test_update_of_variants_alone_is_checked_against_the_stored_config(
    admin_client, db_session
):
    host = _host()
    experiment_id = _split_url_row(
        admin_client, db_session, _config(f"https://{host}/a", f"https://{host}/b")
    )
    renamed = [VARIANTS[0], {**VARIANTS[1], "name": "Variant B"}]

    response = admin_client.put(
        f"{EXPERIMENTS}/{experiment_id}", json={"variants": renamed}
    )

    _refused(
        response,
        ["body", "variants"],
        "URL variant 'Treatment' is not one of this experiment's variants: "
        "Control, Variant B.",
    )
    assert _variant_names(db_session, experiment_id) == ["Control", "Treatment"]


def test_update_of_variants_and_config_together_is_accepted(admin_client, db_session):
    host = _host()
    experiment_id = _split_url_row(
        admin_client, db_session, _config(f"https://{host}/a", f"https://{host}/b")
    )
    renamed = [VARIANTS[0], {**VARIANTS[1], "name": "Variant B"}]

    response = admin_client.put(
        f"{EXPERIMENTS}/{experiment_id}",
        json={
            "variants": renamed,
            "split_url_config": {
                "variants": [
                    _uv("Control", f"https://{host}/a"),
                    _uv("Variant B", f"https://{host}/b"),
                ]
            },
        },
    )

    assert response.status_code == 200, response.text
    assert _variant_names(db_session, experiment_id) == ["Control", "Variant B"]


def test_update_accepts_a_variant_with_the_control_path_on_another_host(
    admin_client, db_session
):
    host = _host()
    experiment_id = _split_url_row(
        admin_client, db_session, _config(f"https://{host}/a", f"https://{host}/b")
    )

    response = admin_client.put(
        f"{EXPERIMENTS}/{experiment_id}",
        json={
            "split_url_config": _config(f"https://{host}/a", f"https://other.{host}/a")
        },
    )

    assert response.status_code == 200, response.text


def test_a_put_resending_a_stored_config_that_breaks_the_rules_answers_422(
    admin_client, db_session
):
    """Stored before the rules: kept, and a PUT that does not send it, or the
    variants, still works; one that sends it back must correct it."""
    host = _host()
    stored = _config("/a", f"https://{host}/b")
    experiment_id = _split_url_row(admin_client, db_session, stored)
    before = _stored_text(db_session, experiment_id)

    edited = admin_client.put(
        f"{EXPERIMENTS}/{experiment_id}", json={"description": "edited"}
    )
    assert edited.status_code == 200, edited.text
    assert _stored_text(db_session, experiment_id) == before

    resent = admin_client.put(
        f"{EXPERIMENTS}/{experiment_id}", json={"split_url_config": stored}
    )
    _refused(resent, ["body", "split_url_config", "variants", 0, "url"], NOT_ABSOLUTE)
    assert _stored_text(db_session, experiment_id) == before

    corrected = admin_client.put(
        f"{EXPERIMENTS}/{experiment_id}",
        json={"split_url_config": _config(f"https://{host}/a", f"https://{host}/b")},
    )
    assert corrected.status_code == 200, corrected.text


def test_an_experiment_of_another_type_is_held_to_the_per_field_rules_only(
    admin_client, db_session
):
    host = _host()
    experiment_id = _split_url_row(
        admin_client, db_session, None, experiment_type=ExperimentType.A_B
    )

    response = admin_client.put(
        f"{EXPERIMENTS}/{experiment_id}",
        json={
            "split_url_config": {
                "variants": [
                    _uv("A", f"https://{host}/a"),
                    _uv("B", f"https://{host}/a"),
                ]
            }
        },
    )

    assert response.status_code == 200, response.text


@pytest.mark.modules
def test_changing_the_type_to_split_url_checks_the_stored_config(
    admin_client, db_session
):
    host = _host()
    experiment_id = _split_url_row(
        admin_client,
        db_session,
        {
            "variants": [
                _uv("Control", f"https://{host}/a"),
                _uv("B", f"https://{host}/b"),
            ]
        },
        experiment_type=ExperimentType.A_B,
    )

    response = admin_client.put(
        f"{EXPERIMENTS}/{experiment_id}", json={"experiment_type": "split_url"}
    )

    _refused(response, ["body", "experiment_type"], UNKNOWN_B)
    assert _row(db_session, experiment_id).experiment_type == ExperimentType.A_B


# ---------------------------------------------------------------------------
# Start
# ---------------------------------------------------------------------------


def _assert_start_refused(client, db_session, experiment_id: str, detail: str):
    response = _start(client, experiment_id)
    assert response.status_code == 400, response.text
    assert response.json() == {"detail": detail}
    assert _row(db_session, experiment_id).status == ExperimentStatus.DRAFT


def test_start_refuses_a_split_url_experiment_with_no_config(admin_client, db_session):
    experiment_id = _split_url_row(admin_client, db_session, None)
    _assert_start_refused(admin_client, db_session, experiment_id, NO_CONFIG)


@pytest.mark.parametrize(
    "stored, problem",
    [
        (
            {"variants": [_uv("Control", "https://{h}/a"), _uv("B", "https://{h}/b")]},
            UNKNOWN_B,
        ),
        (
            {"variants": [_uv("Control", "/a"), _uv("Treatment", "https://{h}/b")]},
            NOT_ABSOLUTE,
        ),
        (
            {
                "variants": [
                    _uv("Control", "https://{h}/a", 95),
                    _uv("Treatment", "https://{h}/b", 5),
                ]
            },
            "URL variant 'Control' has a different traffic_allocation from the "
            "experiment variant of the same name. Make them equal; the "
            "experiment's variants decide the split.",
        ),
    ],
    ids=["names", "relative-url", "another-split"],
)
def test_start_refuses_a_config_that_cannot_split_saying_why(
    admin_client, db_session, stored, problem
):
    host = _host()
    config = copy.deepcopy(stored)
    for variant in config["variants"]:
        variant["url"] = variant["url"].replace("{h}", host)
    experiment_id = _split_url_row(admin_client, db_session, config)

    _assert_start_refused(
        admin_client,
        db_session,
        experiment_id,
        "The split_url_config is not valid: " + problem + " " + FIX_HINT,
    )


def test_start_refuses_the_same_control_page_as_an_active_experiment(
    admin_client, db_session
):
    host = _host()
    first = _split_url_row(
        admin_client, db_session, _config(f"https://{host}/a", f"https://{host}/b")
    )
    assert _start(admin_client, first).status_code == 200
    second = _split_url_row(
        admin_client,
        db_session,
        _config(f"https://{host.upper()}/a/", f"https://{host}/c"),
    )

    _assert_start_refused(admin_client, db_session, second, SAME_ENTRY)


def test_start_accepts_the_same_path_on_another_host(admin_client, db_session):
    host = _host()
    first = _split_url_row(
        admin_client, db_session, _config(f"https://{host}/a", f"https://{host}/b")
    )
    assert _start(admin_client, first).status_code == 200
    other = _host()
    second = _split_url_row(
        admin_client, db_session, _config(f"https://{other}/a", f"https://{other}/b")
    )

    response = _start(admin_client, second)

    assert response.status_code == 200, response.text
    assert _row(db_session, second).status == ExperimentStatus.ACTIVE


@pytest.mark.parametrize(
    "paths",
    [("/b", "/c"), ("/c", "/a")],
    ids=["its-control-page-is-a-variant-page", "its-variant-page-is-a-control-page"],
)
def test_start_refuses_a_chain_either_way(admin_client, db_session, paths):
    host = _host()
    active = _split_url_row(
        admin_client, db_session, _config(f"https://{host}/a", f"https://{host}/b")
    )
    assert _start(admin_client, active).status_code == 200
    control, treatment = paths
    chained = _split_url_row(
        admin_client,
        db_session,
        _config(f"https://{host}{control}", f"https://{host}{treatment}"),
    )

    _assert_start_refused(admin_client, db_session, chained, CHAIN)


def test_start_says_same_page_before_chain(admin_client, db_session):
    host = _host()
    for control, treatment in (("/a", "/b"), ("/c", "/d")):
        active = _split_url_row(
            admin_client,
            db_session,
            _config(f"https://{host}{control}", f"https://{host}{treatment}"),
        )
        assert _start(admin_client, active).status_code == 200
    # The same control page as the first, and a chain into the second.
    both = _split_url_row(
        admin_client, db_session, _config(f"https://{host}/a", f"https://{host}/c")
    )

    _assert_start_refused(admin_client, db_session, both, SAME_ENTRY)


def test_a_paused_experiment_does_not_hold_its_control_page(admin_client, db_session):
    host = _host()
    paused = _split_url_row(
        admin_client, db_session, _config(f"https://{host}/a", f"https://{host}/b")
    )
    assert _start(admin_client, paused).status_code == 200
    assert admin_client.post(f"{EXPERIMENTS}/{paused}/pause").status_code == 200
    second = _split_url_row(
        admin_client, db_session, _config(f"https://{host}/a", f"https://{host}/c")
    )

    assert _start(admin_client, second).status_code == 200


def test_start_skips_an_active_experiment_whose_stored_config_breaks_the_rules(
    admin_client, db_session, caplog
):
    """Legacy rows: each is skipped with one warning, never compared and never
    a 500. They name this experiment's own control page, so comparing them
    would refuse the start."""
    caplog.set_level(logging.WARNING)
    host = _host()
    legacy = [
        # Names that are not its variants'.
        {
            "variants": [
                _uv("Control", f"https://{host}/a"),
                _uv("B", f"https://{host}/b"),
            ]
        },
        # A relative control URL.
        {"variants": [_uv("Control", "/a"), _uv("Treatment", f"https://{host}/a")]},
        # Not a configuration at all.
        ["not", "a", "config"],
        # None.
        None,
    ]
    legacy_ids = [
        _split_url_row(admin_client, db_session, config, status=ExperimentStatus.ACTIVE)
        for config in legacy
    ]
    valid = _split_url_row(
        admin_client, db_session, _config(f"https://{host}/a", f"https://{host}/b")
    )

    response = _start(admin_client, valid)

    assert response.status_code == 200, response.text
    assert _row(db_session, valid).status == ExperimentStatus.ACTIVE
    for legacy_id in legacy_ids:
        warnings = [
            r.getMessage()
            for r in caplog.records
            if r.levelno == logging.WARNING and legacy_id in r.getMessage()
        ]
        assert warnings == [SKIPPED.format(id=legacy_id)]


def test_start_of_an_experiment_of_another_type_ignores_its_config(
    admin_client, db_session
):
    experiment_id = _split_url_row(
        admin_client,
        db_session,
        {"variants": [_uv("A", "/a"), _uv("B", "/a")]},
        experiment_type=ExperimentType.A_B,
    )
    assert _start(admin_client, experiment_id).status_code == 200


# ---------------------------------------------------------------------------
# The preview of a stored config that breaks the per-field rules
# ---------------------------------------------------------------------------


@pytest.mark.modules
def test_the_preview_of_a_stored_config_breaking_a_url_rule_answers_409(
    admin_client, db_session
):
    host = _host()
    experiment_id = _split_url_row(
        admin_client, db_session, _config(f"https://{host}/a", f"https://{host}/b")
    )
    previewed = admin_client.get(
        f"{EXPERIMENTS}/{experiment_id}/split-url/preview", params={"user_id": "u1"}
    )
    assert previewed.status_code == 200, previewed.text

    # Saved before the URL rules.
    db_session.query(Experiment).filter(
        Experiment.id == uuid.UUID(experiment_id)
    ).update({"split_url_config": _config("javascript:alert(1)", f"https://{host}/b")})
    db_session.commit()

    response = admin_client.get(
        f"{EXPERIMENTS}/{experiment_id}/split-url/preview", params={"user_id": "u1"}
    )

    assert response.status_code == 409, response.text
    assert response.json() == {"detail": PREVIEW_409}
    for stored_value in ("javascript", host):
        assert stored_value not in response.text
