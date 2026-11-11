"""Fixtures for every unit test.

``modules/backend/tests/unit/conftest.py`` re-exports them for the modules'
unit tests, since pytest does not apply a sibling tree's conftest.
"""

import logging

import pytest

#: The names in ``backend.app.core.logger.HTTP_CLIENT_LOGGERS``.
_HTTP_CLIENT_LOGGERS = ("httpx", "httpcore", "botocore", "boto3", "s3transfer")


@pytest.fixture(autouse=True)
def restore_root_logger():
    """Hand the root logger to the next test as this one found it.

    Code under test reconfigures the root logger: ``setup_logging()`` and
    ``configure_logging()`` replace its handlers and set its level, and
    importing ``backend.app.core.logging`` or ``backend.app.main`` runs one of
    them. The handlers and the level are put back after each test, and so are
    the levels of the loggers in ``HTTP_CLIENT_LOGGERS`` (httpx, httpcore,
    botocore, boto3 and s3transfer), which both of those functions hold at
    WARNING (``quiet_http_client_loggers``).

    ``logging.getLogger`` itself is not replaced. ``caplog`` attaches its
    handler to whatever ``logging.getLogger()`` returns, so a mock there leaves
    ``caplog`` empty in every unit test (pytest 9.1 and later).
    """
    root = logging.getLogger()
    handlers, level = list(root.handlers), root.level
    client_levels = {n: logging.getLogger(n).level for n in _HTTP_CLIENT_LOGGERS}
    yield
    root.handlers[:] = handlers
    root.setLevel(level)
    for name, client_level in client_levels.items():
        logging.getLogger(name).setLevel(client_level)
