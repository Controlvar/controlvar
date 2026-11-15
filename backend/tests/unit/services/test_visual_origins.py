"""The page origins a visual experiment may use (#1545, C4, V84).

``allowed = [o for o in cors_allowed_origins if o not in dashboard_origins]``,
exactly, with ``Settings`` built from explicit values (``_env_file=None``)
for each environment; then the four refusals in their order. Each planted
defect that fails a row:

* ``_configured_cors_origins()`` alone instead of ``cors_allowed_origins``:
  development allows nothing;
* no subtraction: a page on the dashboard's own address is accepted;
* no empty-list refusal: a test API with nothing set accepts a page;
* a substring or prefix comparison: ``https://shop.example.com.evil.test`` is
  accepted;
* ``== ["*"]`` instead of membership: a list holding ``*`` among others is
  accepted.
"""

from __future__ import annotations

import pytest

from backend.app.core.config import DevSettings, ProdSettings, TestSettings
from backend.app.services import visual_origins as vo

pytestmark = [pytest.mark.unit]

PROD_SECRET = "p" * 64
SHOP = "https://shop.example.com"
DASH = "https://dash.example.com"


@pytest.fixture(autouse=True)
def _no_env(monkeypatch):
    for name in (
        "BACKEND_CORS_ORIGINS",
        "CORS_ORIGINS",
        "DASHBOARD_ORIGINS",
        "PUBLIC_BASE_URL",
        "ALLOWED_HOSTS",
        "TESTING",
    ):
        monkeypatch.delenv(name, raising=False)


def _production(**kwargs) -> ProdSettings:
    return ProdSettings(
        ENVIRONMENT="production",
        SECRET_KEY=PROD_SECRET,
        FIRST_SUPERUSER_PASSWORD="Str0ng-first-admin",
        PUBLIC_BASE_URL=kwargs.pop("PUBLIC_BASE_URL", DASH),
        _env_file=None,
        **kwargs,
    )


def _config(*page_urls, editor_url=None):
    pages = [{"match": "prefix", "url": url} for url in page_urls]
    return {
        "version": 1,
        "editor_url": editor_url or page_urls[0],
        "pages": pages,
        "goals": [],
    }


# --- the allowed list, per environment ------------------------------------------


@pytest.mark.regression
def test_development_with_nothing_set_allows_the_local_apps_but_the_dashboards():
    settings = DevSettings(_env_file=None)
    # DevSettings' own CORS_ORIGINS is the five local apps and the API's port;
    # 3100 and 3000 are the development dashboard origins.
    assert vo.allowed_page_origins(settings) == [
        "http://localhost:3001",
        "http://localhost:3200",
        "http://localhost:3300",
        "http://localhost:8000",
    ]
    assert vo.visual_origin_problem(_config("http://localhost:3200/"), settings) is None
    for dashboard in ("http://localhost:3100/", "http://localhost:3000/"):
        assert (
            vo.visual_origin_problem(_config(dashboard), settings)
            == vo.PAGE_ON_DASHBOARD
        )


@pytest.mark.regression
def test_test_with_nothing_set_refuses_every_page():
    """No dashboard origin is known, so the dashboard's own address cannot be
    told apart: every page is refused with the dashboard-address text."""
    settings = TestSettings(_env_file=None)
    assert settings.dashboard_origins == []
    assert "http://localhost:3100" in settings.cors_allowed_origins
    for url in ("http://localhost:3100/", "http://localhost:3200/", SHOP + "/"):
        assert (
            vo.visual_origin_problem(_config(url), settings) == vo.NO_DASHBOARD_ORIGIN
        )


def test_test_with_a_dashboard_origin_allows_the_other_local_apps():
    settings = TestSettings(DASHBOARD_ORIGINS="http://localhost:3100", _env_file=None)
    assert vo.allowed_page_origins(settings) == [
        "http://localhost:3000",
        "http://localhost:3001",
        "http://localhost:3200",
        "http://localhost:3300",
    ]
    assert (
        vo.visual_origin_problem(_config("http://localhost:3100/"), settings)
        == vo.PAGE_ON_DASHBOARD
    )
    assert vo.visual_origin_problem(_config("http://localhost:3300/"), settings) is None


def test_production_allows_the_configured_sites_only():
    settings = _production(CORS_ORIGINS=SHOP)
    assert vo.allowed_page_origins(settings) == [SHOP]
    assert vo.visual_origin_problem(_config(SHOP + "/"), settings) is None
    assert vo.experiment_origins(_config(SHOP + "/")) == [SHOP]


def test_backend_cors_origins_decides_when_it_is_set():
    settings = _production(
        BACKEND_CORS_ORIGINS='["https://other.example.com"]', CORS_ORIGINS=SHOP
    )
    assert vo.allowed_page_origins(settings) == ["https://other.example.com"]
    assert vo.visual_origin_problem(_config(SHOP + "/"), settings) == (
        vo.ORIGIN_NOT_ALLOWED.format(origin=SHOP)
    )


# --- the refusals ----------------------------------------------------------------


@pytest.mark.regression
@pytest.mark.parametrize(
    "url",
    [
        "https://evil.test/",
        "https://shop.example.com.evil.test/",
        "http://shop.example.com/",
        "https://shop.example.com:8443/",
    ],
)
def test_an_origin_the_server_does_not_accept_is_refused_by_name(url):
    settings = _production(CORS_ORIGINS=SHOP)
    origin = url.rstrip("/")
    assert vo.visual_origin_problem(_config(SHOP + "/", url), settings) == (
        vo.ORIGIN_NOT_ALLOWED.format(origin=origin)
    )


def test_the_default_port_is_the_same_origin():
    settings = _production(CORS_ORIGINS=SHOP)
    assert (
        vo.visual_origin_problem(_config("https://shop.example.com:443/a"), settings)
        is None
    )
    assert vo.experiment_origins(_config("https://shop.example.com:443/a")) == [SHOP]


def test_a_page_on_the_dashboard_is_refused():
    settings = _production(CORS_ORIGINS=f"{SHOP},{DASH}")
    assert DASH in settings.cors_allowed_origins
    assert vo.visual_origin_problem(_config(SHOP + "/", DASH + "/app"), settings) == (
        vo.PAGE_ON_DASHBOARD
    )


@pytest.mark.regression
@pytest.mark.parametrize("cors", ["*", f"{SHOP},*"])
def test_every_site_allowed_turns_the_editor_off(cors):
    settings = _production(CORS_ORIGINS=cors)
    assert (
        vo.visual_origin_problem(_config(SHOP + "/"), settings) == vo.EVERY_SITE_ALLOWED
    )


def test_the_editor_page_counts_as_well_as_the_pages():
    settings = _production(CORS_ORIGINS=SHOP)
    config = _config(SHOP + "/", editor_url="https://staging.example.com/")
    assert vo.visual_origin_problem(config, settings) == vo.ORIGIN_NOT_ALLOWED.format(
        origin="https://staging.example.com"
    )


def test_site_origins_are_each_origin_once_editor_first():
    config = _config(
        "https://b.example.com/x",
        "https://a.example.com/",
        "https://b.example.com/y",
        editor_url="https://a.example.com/start",
    )
    assert vo.experiment_origins(config) == [
        "https://a.example.com",
        "https://b.example.com",
    ]


def test_the_order_of_the_refusals():
    """The empty dashboard list first, then *, then each origin in turn."""
    star_no_dashboard = TestSettings(CORS_ORIGINS="*", _env_file=None)
    assert star_no_dashboard.dashboard_origins == []
    assert "*" in star_no_dashboard.cors_allowed_origins
    assert (
        vo.visual_origin_problem(_config(SHOP + "/"), star_no_dashboard)
        == vo.NO_DASHBOARD_ORIGIN
    )
    star = _production(CORS_ORIGINS="*")
    config = _config(SHOP + "/", DASH + "/")
    assert vo.visual_origin_problem(config, star) == vo.EVERY_SITE_ALLOWED
    first_bad = _production(CORS_ORIGINS=SHOP)
    config = _config(SHOP + "/", "https://a.test/", "https://b.test/")
    assert vo.visual_origin_problem(config, first_bad) == (
        vo.ORIGIN_NOT_ALLOWED.format(origin="https://a.test")
    )


def test_the_texts():
    assert vo.NO_DASHBOARD_ORIGIN == (
        "The visual editor needs this dashboard's address: set DASHBOARD_ORIGINS "
        "or PUBLIC_BASE_URL."
    )
    assert vo.EVERY_SITE_ALLOWED == (
        "The visual editor is off on this server, because CORS_ORIGINS is * (every "
        "site). Ask whoever runs Controlvar to list your sites in CORS_ORIGINS "
        "instead."
    )
    assert vo.PAGE_ON_DASHBOARD == (
        "A visual experiment's pages cannot be on the Controlvar dashboard's own "
        "address."
    )
    assert vo.ORIGIN_NOT_ALLOWED.format(origin="https://staging.example.com") == (
        "Controlvar cannot open the editor on https://staging.example.com: your "
        "Controlvar server does not accept requests from that site. Ask whoever "
        "runs Controlvar to add it to CORS_ORIGINS and restart it."
    )
