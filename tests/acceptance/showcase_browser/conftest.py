"""The showcase tool's browser checks: their own pytest session (#1066).

The capture reads pages with JavaScript that runs in the browser, for example
``director.VIEWPORT_TEXT_JS``, which gates U4 and G2 compare with. The unit
tests in ``backend/tests/unit/showcase`` stub the browser out: the CI job that
runs them installs ``backend/requirements.txt`` only, and fails if any of them
is skipped. The tests here run that JavaScript in headless Chromium instead,
in the showcase tool's own environment. Run them from the repository root::

    python -m pytest -c tests/acceptance/showcase_browser/pytest.ini tests/acceptance/showcase_browser

with ``tests/acceptance/requirements.txt`` installed, and Chromium installed
for Playwright (``python -m playwright install chromium``). No CI job runs
them, since none has a browser for them. Without Playwright or Chromium they
fail; they are never skipped.

This directory is its own pytest root (``pytest.ini``), so the repository's
root ``conftest.py`` is not loaded. As in every other suite,
``backend/tests/no_real_aws.py`` is loaded in ``pytest_configure``
(``backend/tests/unit/test_no_real_aws.py`` checks that it is).
"""

from __future__ import annotations

from typing import Any, Iterator

import pytest


def pytest_configure(config: pytest.Config) -> None:
    config.pluginmanager.import_plugin("backend.tests.no_real_aws")


@pytest.fixture(scope="session")
def browser() -> Iterator[Any]:
    # Imported here, not at the top: loading this conftest (as the no-real-AWS
    # check does, in an environment without Playwright) must not need it.
    from playwright.sync_api import sync_playwright

    with sync_playwright() as playwright:
        chromium = playwright.chromium.launch(headless=True)
        try:
            yield chromium
        finally:
            chromium.close()


@pytest.fixture
def page(browser: Any) -> Iterator[Any]:
    tab = browser.new_page(viewport={"width": 1280, "height": 720})
    try:
        yield tab
    finally:
        tab.close()
