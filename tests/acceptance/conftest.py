"""A pytest run started from the repository root over ``tests/acceptance``.

The showcase tool's browser checks (``showcase_browser/``) are their own pytest
root: their ``pytest.ini`` puts ``tests/acceptance`` on the import path, and
they need Playwright and Chromium. A run started from the repository root, such
as ``python -m pytest tests/acceptance --collect-only``
(``backend/tests/unit/docs/test_docs_journey_runner.py``), uses neither, so it
leaves them out. Run them with their own ``pytest.ini`` (see
``showcase_browser/conftest.py``); this file is not loaded there.
"""

collect_ignore = ["showcase_browser"]
