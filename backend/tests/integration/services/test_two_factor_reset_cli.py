"""``python -m backend.app.db.two_factor_reset`` on PostgreSQL (#1812).

The command deletes the rows and writes one ``user_2fa_reset`` per account by
``system:operator-cli`` in the same transaction; refuses with no argument, an
address no account has, or ``--all`` without its confirmation flag, changing
nothing; and when the audit entry cannot be written, deletes nothing either.
"""

from __future__ import annotations

import io
import json
import subprocess
import sys
from pathlib import Path

import pytest

from backend.app.db import two_factor_reset
from backend.app.services.audit_service import AuditService
from backend.tests.integration.services.two_factor_fixtures import (
    bcrypt_calls,
    clock,
    enabled_user,
    entries,
    make_user,
    row,
    service,
    tracker,
)

pytestmark = [pytest.mark.integration, pytest.mark.requires_db]

SCHEMA = "test_experimentation"
REPO_ROOT = Path(__file__).resolve().parents[4]


def _run(test_db, *argv):
    out = io.StringIO()
    status = two_factor_reset.run(list(argv), engine=test_db, schema=SCHEMA, out=out)
    return status, out.getvalue()


@pytest.mark.regression
def test_one_account_by_its_address_in_any_case(test_db, db_session, service, clock):
    user, _, _ = enabled_user(db_session, service, clock)
    other, _, _ = enabled_user(db_session, service, clock)
    status, said = _run(test_db, "--email", f"  {user.email.upper()} ")
    assert status == 0, said
    assert said.strip() == "Reset two-step sign-in for 1 account."
    assert row(db_session, user.id) is None
    assert row(db_session, other.id) is not None
    (entry,) = entries(db_session, user.id, "user_2fa_reset")
    assert entry.user_id is None and entry.user_email == "system:operator-cli"
    assert json.loads(entry.old_value) == {"enabled": True}
    assert entries(db_session, other.id, "user_2fa_reset") == []


@pytest.mark.regression
def test_every_account_with_the_confirmation_flag(test_db, db_session, service, clock):
    users = [enabled_user(db_session, service, clock)[0] for _ in range(2)]
    waiting = make_user(db_session)
    service.enrol(db_session, waiting)
    status, said = _run(test_db, "--all", "--yes-reset-every-account")
    assert status == 0, said
    for user in (*users, waiting):
        assert row(db_session, user.id) is None
        assert len(entries(db_session, user.id, "user_2fa_reset")) == 1
    count = int(said.split(" for ")[1].split()[0])
    assert count >= 3


@pytest.mark.regression
def test_all_without_the_confirmation_flag_changes_nothing(
    test_db, db_session, service, clock
):
    user, _, _ = enabled_user(db_session, service, clock)
    status, said = _run(test_db, "--all")
    assert status != 0
    assert "--yes-reset-every-account" in said and "Nothing was changed." in said
    assert row(db_session, user.id) is not None
    assert entries(db_session, user.id, "user_2fa_reset") == []


@pytest.mark.regression
def test_an_address_no_account_has_is_refused(test_db, db_session):
    status, said = _run(test_db, "--email", "nobody-has-this@example.com")
    assert status != 0
    assert "no account has that email address" in said


def test_an_account_with_nothing_to_reset_is_not_an_error(test_db, db_session):
    user = make_user(db_session)
    status, said = _run(test_db, "--email", user.email)
    assert status == 0
    assert said.strip() == "Reset two-step sign-in for 0 accounts."
    assert entries(db_session, user.id, "user_2fa_reset") == []


@pytest.mark.regression
@pytest.mark.parametrize(
    "argv",
    [[], ["--yes-reset-every-account"], ["--email", "a@example.com", "--all"]],
    ids=["nothing", "flag-alone", "both"],
)
def test_wrong_arguments_are_refused(test_db, argv):
    status, _ = _run(test_db, *argv)
    assert status != 0


@pytest.mark.regression
def test_when_the_entry_cannot_be_written_nothing_is_reset(
    test_db, db_session, service, clock, monkeypatch
):
    """The delete and its entry share one transaction: an entry that fails
    to save takes the delete with it. Written after the commit instead, the
    rows would be gone with no entry."""
    user, _, _ = enabled_user(db_session, service, clock)
    real = AuditService.record

    def failing_record(db, **entry):
        added = real(db, **entry)
        added.entity_type = None  # NOT NULL: the flush fails
        return added

    monkeypatch.setattr(AuditService, "record", staticmethod(failing_record))
    status, said = _run(test_db, "--email", user.email)
    assert status != 0
    assert "Nothing was changed." in said
    assert row(db_session, user.id) is not None
    assert entries(db_session, user.id, "user_2fa_reset") == []


def test_the_module_runs_as_a_command(test_db):
    """``python -m``: an argument error exits 2 before any database is used."""
    result = subprocess.run(
        [sys.executable, "-m", "backend.app.db.two_factor_reset"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=120,
        stdin=subprocess.DEVNULL,
    )
    assert result.returncode == 2, result.stderr[-2000:]
    assert "--email" in result.stderr
