"""Two-step sign-in's service layer, on PostgreSQL (#1812).

Setting it up, a code used once, recovery codes, a secret that cannot be
opened failing closed, the cap on wrong codes, and the one code check's
bookkeeping: each refusal is counted on the failure counter the account shares
with its password and in ``failed_attempts``.
The races are forced, never left to timing: session A takes the row lock and
stays open, session B is seen waiting on that lock in ``pg_stat_activity``,
then A commits (``two_factor_fixtures.lock_wait``).

Every time is the test's own clock (``Clock``); nothing reads the wall clock.
"""

from __future__ import annotations

import json
import logging
import re

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError

from backend.app.core import totp
from backend.app.core.config import settings
from backend.app.models.audit_log import AuditLog
from backend.app.models.user_totp import UserTOTP
from backend.app.services.audit_service import AuditActor
from backend.app.services.local_auth_service import (
    AccountLockedError,
    LoginAttemptTracker,
)
from backend.app.services.two_factor_service import (
    CODE_STEP_CAP,
    CODE_STEP_LOCKED_TEXT,
    CONFIRM,
    SIGN_IN,
    UNAVAILABLE_TEXT,
    WRONG_CODE_TEXT,
    AlreadyEnabledError,
    CodeStepLockedError,
    NotEnabledError,
    TwoFactorInputError,
    TwoFactorService,
    TwoFactorUnavailableError,
    WrongCodeError,
)
from backend.tests.integration.services.two_factor_fixtures import (
    Clock,
    bcrypt_calls,
    clock,
    code_at,
    enabled_user,
    entries,
    lock_wait,
    make_user,
    row,
    secret_of,
    service,
    session_factory,
    tracker,
)

pytestmark = [pytest.mark.integration, pytest.mark.requires_db]


def _key(user) -> str:
    return user.email.lower()


# ---------------------------------------------------------------------------
# Setting it up
# ---------------------------------------------------------------------------


@pytest.mark.regression
def test_a_waiting_secret_changes_nothing_at_sign_in(db_session, service, clock):
    user = make_user(db_session)
    secret = secret_of(service.enrol(db_session, user))
    assert service.is_enabled(db_session, user.id) is False
    assert service.status(db_session, user).enabled is False
    with pytest.raises(NotEnabledError):
        service.verify_sign_in(db_session, user, code=code_at(secret, clock.step()))


@pytest.mark.regression
def test_enrolling_again_replaces_the_waiting_secret(db_session, service, clock):
    user = make_user(db_session)
    old = secret_of(service.enrol(db_session, user))
    new = secret_of(service.enrol(db_session, user))
    assert old != new
    with pytest.raises(WrongCodeError):
        service.confirm(db_session, user, code_at(old, clock.step()))
    assert row(db_session, user.id).confirmed_at is None
    service.confirm(db_session, user, code_at(new, clock.step()))
    assert service.is_enabled(db_session, user.id) is True


@pytest.mark.regression
def test_enrolling_once_it_is_on_is_refused_and_keeps_the_secret(
    db_session, service, clock
):
    user, _, _ = enabled_user(db_session, service, clock)
    sealed = row(db_session, user.id).secret_enc
    with pytest.raises(AlreadyEnabledError):
        service.enrol(db_session, user)
    assert row(db_session, user.id).secret_enc == sealed


@pytest.mark.regression
def test_a_wrong_confirm_code_leaves_the_secret_waiting(db_session, service, clock):
    user = make_user(db_session)
    secret = secret_of(service.enrol(db_session, user))
    with pytest.raises(WrongCodeError) as refused:
        service.confirm(db_session, user, code_at(secret, clock.step() + 5))
    assert str(refused.value) == WRONG_CODE_TEXT
    stored = row(db_session, user.id)
    assert stored.confirmed_at is None
    assert stored.failed_attempts == 1
    assert service.is_enabled(db_session, user.id) is False


@pytest.mark.regression
def test_confirm_stores_the_confirm_codes_step(db_session, service, clock):
    """The confirm code's step is stored, so that code cannot sign in."""
    user = make_user(db_session)
    secret = secret_of(service.enrol(db_session, user))
    step = clock.step()
    service.confirm(db_session, user, code_at(secret, step))
    stored = row(db_session, user.id)
    assert stored.last_used_step == step
    assert stored.confirmed_at is not None
    with pytest.raises(WrongCodeError):
        service.verify_sign_in(db_session, user, code=code_at(secret, step))


@pytest.mark.regression
def test_confirm_gives_ten_recovery_codes_and_stores_only_their_hashes(
    db_session, service, clock
):
    user, secret, codes = enabled_user(db_session, service, clock)
    assert len(codes) == 10 and len(set(codes)) == 10
    assert all(re.fullmatch(r"[A-Z2-7]{10}", c) for c in codes)
    stored = row(db_session, user.id)
    assert len(stored.recovery_hashes) == 10
    assert all(h.startswith("$2b$") for h in stored.recovery_hashes)
    assert not any(c in json.dumps(stored.recovery_hashes) for c in codes)
    assert totp.secret_text(secret) not in stored.secret_enc
    (enabled,) = entries(db_session, user.id, "user_2fa_enable")
    assert enabled.user_id == user.id and enabled.user_email == user.email
    assert json.loads(enabled.new_value) == {"recovery_codes": 10}
    assert service.status(db_session, user).recovery_codes_remaining == 10


# ---------------------------------------------------------------------------
# A code is used once
# ---------------------------------------------------------------------------


@pytest.mark.regression
def test_the_same_code_twice_is_refused(db_session, service, clock):
    user, secret, _ = enabled_user(db_session, service, clock)
    code = code_at(secret, clock.step())
    assert service.verify_sign_in(db_session, user, code=code).method == "totp"
    with pytest.raises(WrongCodeError):
        service.verify_sign_in(db_session, user, code=code)
    assert row(db_session, user.id).failed_attempts == 1


@pytest.mark.regression
def test_an_earlier_step_is_refused_after_a_later_one(db_session, service, clock):
    user, secret, _ = enabled_user(db_session, service, clock)
    step = clock.step()
    service.verify_sign_in(db_session, user, code=code_at(secret, step + 1))
    for earlier in (step - 1, step):
        with pytest.raises(WrongCodeError):
            service.verify_sign_in(db_session, user, code=code_at(secret, earlier))
    assert row(db_session, user.id).last_used_step == step + 1


@pytest.mark.regression
def test_the_next_step_is_accepted_and_typed_spaces_are_ignored(
    db_session, service, clock
):
    user, secret, _ = enabled_user(db_session, service, clock)
    code = code_at(secret, clock.step())
    typed = f" {code[:3]} {code[3:]}\t"
    assert service.verify_sign_in(db_session, user, code=typed).step == clock.step()
    clock.now += totp.STEP_SECONDS
    nxt = code_at(secret, clock.step())
    assert service.verify_sign_in(db_session, user, code=f"{nxt[:3]}-{nxt[3:]}")


@pytest.mark.regression
def test_one_code_raced_in_two_transactions_wins_once(
    db_session, service, clock, session_factory
):
    """A stores the step and holds the row; B, the same step, waits on
    A's lock, then finds nothing to update."""
    user, _, _ = enabled_user(db_session, service, clock)
    step = clock.step()
    outcome = lock_wait(
        session_factory,
        lambda a: service._accept_totp_step(a, user.id, step, pending=False),
        lambda b: service._accept_totp_step(b, user.id, step, pending=False),
    )
    assert outcome["waited"] is True, "B never waited for A's uncommitted write"
    assert outcome["a"] is True
    assert outcome["b"] is False
    assert row(db_session, user.id).last_used_step == step


# ---------------------------------------------------------------------------
# Recovery codes
# ---------------------------------------------------------------------------


@pytest.mark.regression
def test_a_recovery_code_signs_in_once(db_session, service, clock):
    user, _, codes = enabled_user(db_session, service, clock)
    result = service.verify_sign_in(db_session, user, recovery_code=codes[0])
    assert result.method == "recovery"
    assert len(row(db_session, user.id).recovery_hashes) == 9
    with pytest.raises(WrongCodeError):
        service.verify_sign_in(db_session, user, recovery_code=codes[0])
    assert len(row(db_session, user.id).recovery_hashes) == 9
    (used,) = entries(db_session, user.id, "user_2fa_recovery_used")
    assert json.loads(used.new_value) == {"recovery_codes_remaining": 9}
    assert codes[0] not in (used.new_value or "")


@pytest.mark.regression
def test_a_refused_sign_in_consumes_no_recovery_code(db_session, service, clock):
    user, secret, codes = enabled_user(db_session, service, clock)
    with pytest.raises(WrongCodeError):
        service.verify_sign_in(db_session, user, recovery_code="A" * 10)
    with pytest.raises(WrongCodeError):
        service.verify_sign_in(db_session, user, code=code_at(secret, clock.step() + 7))
    assert len(row(db_session, user.id).recovery_hashes) == 10
    assert entries(db_session, user.id, "user_2fa_recovery_used") == []


def test_a_recovery_code_is_read_in_any_case_with_spaces_or_hyphens(
    db_session, service, clock
):
    user, _, codes = enabled_user(db_session, service, clock)
    typed = f"{codes[1][:5].lower()}-{codes[1][5:]} "
    assert service.verify_sign_in(db_session, user, recovery_code=typed)
    assert len(row(db_session, user.id).recovery_hashes) == 9


@pytest.mark.regression
def test_code_and_recovery_code_are_not_interchangeable(db_session, service, clock):
    user, secret, codes = enabled_user(db_session, service, clock)
    with pytest.raises(WrongCodeError):
        service.verify_sign_in(db_session, user, code=codes[0])
    with pytest.raises(WrongCodeError):
        service.verify_sign_in(
            db_session, user, recovery_code=code_at(secret, clock.step())
        )
    with pytest.raises(TwoFactorInputError):
        service.verify_sign_in(
            db_session, user, code=code_at(secret, clock.step()), recovery_code=codes[0]
        )
    with pytest.raises(TwoFactorInputError):
        service.verify_sign_in(db_session, user)
    with pytest.raises(TwoFactorInputError):
        service.check_code(db_session, user, recovery_code=codes[0], purpose=CONFIRM)
    stored = row(db_session, user.id)
    assert len(stored.recovery_hashes) == 10
    assert stored.failed_attempts == 2


@pytest.mark.regression
def test_an_oversized_input_is_refused_before_any_bcrypt(
    db_session, service, clock, bcrypt_calls
):
    """10 kB, and one character over the cap: refused uncounted with no
    bcrypt run (a count of calls, not a timing); 64 is read as a wrong code."""
    user, _, _ = enabled_user(db_session, service, clock)
    bcrypt_calls.calls = 0
    for oversized in ("A" * 10240, "A" * 65):
        with pytest.raises(TwoFactorInputError):
            service.verify_sign_in(db_session, user, recovery_code=oversized)
        with pytest.raises(TwoFactorInputError):
            service.verify_sign_in(db_session, user, code=oversized)
    assert bcrypt_calls.calls == 0
    assert row(db_session, user.id).failed_attempts == 0
    with pytest.raises(WrongCodeError):
        service.verify_sign_in(db_session, user, recovery_code="A" * 64)
    assert bcrypt_calls.calls == 0  # not a recovery code's shape: no bcrypt
    assert row(db_session, user.id).failed_attempts == 1


@pytest.mark.regression
def test_one_recovery_code_raced_in_two_transactions_wins_once(
    db_session, service, clock, session_factory
):
    """The same hash, removed by two sessions at once: one wins."""
    user, _, _ = enabled_user(db_session, service, clock)
    hashed = row(db_session, user.id).recovery_hashes[0]
    outcome = lock_wait(
        session_factory,
        lambda a: service._consume_recovery_hash(a, user, hashed),
        lambda b: service._consume_recovery_hash(b, user, hashed),
    )
    assert outcome["waited"] is True, "B never waited for A's uncommitted write"
    assert outcome["a"] == 9
    assert outcome["b"] is None
    stored = row(db_session, user.id)
    assert len(stored.recovery_hashes) == 9 and hashed not in stored.recovery_hashes
    assert len(entries(db_session, user.id, "user_2fa_recovery_used")) == 1


@pytest.mark.regression
def test_two_recovery_codes_raced_in_two_transactions_are_both_used(
    db_session, service, clock, session_factory
):
    """Two different hashes at once: both go, none survives."""
    user, _, _ = enabled_user(db_session, service, clock)
    first, second = row(db_session, user.id).recovery_hashes[:2]
    outcome = lock_wait(
        session_factory,
        lambda a: service._consume_recovery_hash(a, user, first),
        lambda b: service._consume_recovery_hash(b, user, second),
    )
    assert outcome["waited"] is True, "B never waited for A's uncommitted write"
    assert outcome["a"] == 9 and outcome["b"] == 8
    stored = row(db_session, user.id)
    assert len(stored.recovery_hashes) == 8
    assert first not in stored.recovery_hashes
    assert second not in stored.recovery_hashes


# ---------------------------------------------------------------------------
# A secret that cannot be opened fails closed
# ---------------------------------------------------------------------------


@pytest.mark.regression
def test_a_secret_sealed_under_another_secret_key_fails_closed(
    db_session, service, clock, monkeypatch
):
    user, secret, codes = enabled_user(db_session, service, clock)
    monkeypatch.setattr(settings, "SECRET_KEY", settings.SECRET_KEY + "-rotated")
    with pytest.raises(TwoFactorUnavailableError) as refused:
        service.verify_sign_in(db_session, user, code=code_at(secret, clock.step()))
    assert str(refused.value) == UNAVAILABLE_TEXT
    stored = row(db_session, user.id)
    assert stored.last_used_step == clock.step() - 1  # the confirm step only
    assert stored.failed_attempts == 0


# ---------------------------------------------------------------------------
# The cap on wrong codes
# ---------------------------------------------------------------------------


@pytest.fixture
def roomy_tracker(service):
    """A shared counter that never locks, so the cap is what is tested."""
    service.tracker = LoginAttemptTracker(max_attempts=10_000, window_seconds=900)
    return service.tracker


@pytest.mark.regression
def test_after_100_wrong_codes_even_the_right_one_is_refused(
    db_session, service, clock, roomy_tracker, caplog
):
    user, secret, codes = enabled_user(db_session, service, clock)
    wrong = code_at(secret, clock.step() + 9)
    for _ in range(CODE_STEP_CAP):
        with pytest.raises(WrongCodeError):
            service.verify_sign_in(db_session, user, code=wrong)
    assert row(db_session, user.id).failed_attempts == CODE_STEP_CAP
    caplog.clear()
    caplog.set_level(logging.WARNING)
    with pytest.raises(CodeStepLockedError) as capped:
        service.verify_sign_in(db_session, user, code=code_at(secret, clock.step()))
    assert str(capped.value) == CODE_STEP_LOCKED_TEXT
    with pytest.raises(CodeStepLockedError):
        service.verify_sign_in(db_session, user, recovery_code=codes[0])
    reasons = [
        r.reason
        for r in caplog.records
        if getattr(r, "event", "") == "two_factor_refused"
    ]
    assert reasons == ["cap", "cap"]
    stored = row(db_session, user.id)
    assert stored.failed_attempts == CODE_STEP_CAP
    assert len(stored.recovery_hashes) == 10


@pytest.mark.regression
def test_a_reset_clears_the_cap_and_leaves_no_count(
    db_session, service, clock, roomy_tracker
):
    user, secret, _ = enabled_user(db_session, service, clock)
    wrong = code_at(secret, clock.step() + 9)
    for _ in range(CODE_STEP_CAP):
        with pytest.raises(WrongCodeError):
            service.verify_sign_in(db_session, user, code=wrong)
    operator = AuditActor(id=None, email="system:operator-cli")
    assert service.reset(db_session, [user], operator) == 1
    db_session.commit()
    assert row(db_session, user.id) is None
    fresh = secret_of(service.enrol(db_session, user))
    service.confirm(db_session, user, code_at(fresh, clock.step()))
    assert row(db_session, user.id).failed_attempts == 0
    clock.now += totp.STEP_SECONDS
    assert service.verify_sign_in(db_session, user, code=code_at(fresh, clock.step()))


@pytest.mark.regression
def test_a_right_code_before_the_cap_sets_the_count_to_zero(
    db_session, service, clock, roomy_tracker
):
    user, secret, _ = enabled_user(db_session, service, clock)
    for _ in range(5):
        with pytest.raises(WrongCodeError):
            service.verify_sign_in(db_session, user, code="000000")
    assert row(db_session, user.id).failed_attempts == 5
    service.verify_sign_in(db_session, user, code=code_at(secret, clock.step()))
    assert row(db_session, user.id).failed_attempts == 0


@pytest.mark.regression
def test_two_refusals_at_once_both_count(db_session, service, clock, session_factory):
    """The count is added in the database: B waits on A's row lock and adds
    to what A committed, so neither refusal is lost."""
    user, _, _ = enabled_user(db_session, service, clock)
    outcome = lock_wait(
        session_factory,
        lambda a: service._count_refusal(a, user.id),
        lambda b: service._count_refusal(b, user.id),
    )
    assert outcome["waited"] is True, "B never waited for A's uncommitted write"
    assert row(db_session, user.id).failed_attempts == 2


# ---------------------------------------------------------------------------
# The failure counter shared with the password
# ---------------------------------------------------------------------------


@pytest.mark.regression
def test_each_refusal_counts_in_the_shared_counter_and_the_column(
    db_session, service, clock, tracker
):
    """Ten wrong codes lock the address the password step uses (its key is
    the lower-cased address), and the eleventh try is refused before any
    code is checked, even a right one."""
    user, secret, _ = enabled_user(db_session, service, clock)
    assert user.email != user.email.lower()
    for _ in range(tracker.max_attempts):
        with pytest.raises(WrongCodeError):
            service.verify_sign_in(db_session, user, code="000000")
    assert tracker.status(_key(user)).failures == tracker.max_attempts
    with pytest.raises(AccountLockedError):
        service.verify_sign_in(db_session, user, code=code_at(secret, clock.step()))
    assert row(db_session, user.id).failed_attempts == tracker.max_attempts


@pytest.mark.regression
def test_a_password_lockout_refuses_the_code_step(db_session, service, clock, tracker):
    user, secret, _ = enabled_user(db_session, service, clock)
    for _ in range(tracker.max_attempts):
        tracker.record_failure(_key(user))
    with pytest.raises(AccountLockedError):
        service.verify_sign_in(db_session, user, code=code_at(secret, clock.step()))
    stored = row(db_session, user.id)
    assert stored.failed_attempts == 0
    assert stored.last_used_step == clock.step() - 1


@pytest.mark.regression
def test_a_right_code_clears_the_shared_counter(db_session, service, clock, tracker):
    user, secret, _ = enabled_user(db_session, service, clock)
    for _ in range(3):
        tracker.record_failure(_key(user))
    service.verify_sign_in(db_session, user, code=code_at(secret, clock.step()))
    assert tracker.status(_key(user)).failures == 0


@pytest.mark.regression
def test_only_an_accepted_sign_in_code_clears_the_password_failure_count(
    db_session, service, clock, tracker
):
    """A code or recovery code accepted to confirm a new secret, to make new
    recovery codes or to turn two-step sign-in off leaves the password failure
    count as it was. Only an accepted sign-in code or sign-in recovery code
    clears it, as an accepted password does for an account without two-step
    sign-in."""

    def nine_failures():
        for _ in range(9):
            tracker.record_failure(_key(user))
        assert tracker.status(_key(user)).failures == 9

    def failures():
        return tracker.status(_key(user)).failures

    user = make_user(db_session)
    secret = secret_of(service.enrol(db_session, user))
    nine_failures()
    service.confirm(db_session, user, code_at(secret, clock.step()))
    assert failures() == 9
    clock.now += totp.STEP_SECONDS
    codes = service.regenerate_recovery_codes(
        db_session, user, code_at(secret, clock.step())
    )
    assert failures() == 9
    clock.now += totp.STEP_SECONDS
    service.verify_sign_in(db_session, user, code=code_at(secret, clock.step()))
    assert failures() == 0
    nine_failures()
    service.verify_sign_in(db_session, user, recovery_code=codes[0])
    assert failures() == 0
    nine_failures()
    service.disable(db_session, user, recovery_code=codes[1])
    assert failures() == 9
    secret = secret_of(service.enrol(db_session, user))
    clock.now += totp.STEP_SECONDS
    service.confirm(db_session, user, code_at(secret, clock.step()))
    assert failures() == 9
    clock.now += totp.STEP_SECONDS
    service.disable(db_session, user, code=code_at(secret, clock.step()))
    assert failures() == 9


@pytest.mark.regression
def test_with_a_demo_account_published_only_the_column_counts(
    db_session, service, clock, tracker, monkeypatch
):
    user, secret, _ = enabled_user(db_session, service, clock)
    monkeypatch.setattr(settings, "DEMO_MODE", True)
    monkeypatch.setattr(settings, "DEMO_ACCOUNT_EMAIL", "demo@example.com")
    monkeypatch.setattr(settings, "DEMO_ACCOUNT_PASSWORD", "published-demo-password")
    assert settings.demo_account_published is True
    for _ in range(tracker.max_attempts + 2):
        with pytest.raises(WrongCodeError):
            service.verify_sign_in(db_session, user, code="000000")
    assert tracker.status(_key(user)).failures == 0
    assert row(db_session, user.id).failed_attempts == tracker.max_attempts + 2


# ---------------------------------------------------------------------------
# Turning it off, new recovery codes, a reset: one audit entry each, and no
# entry or log line holds a secret or a code
# ---------------------------------------------------------------------------


@pytest.mark.regression
def test_turning_it_off_with_a_code(db_session, service, clock):
    user, secret, _ = enabled_user(db_session, service, clock)
    service.disable(db_session, user, code=code_at(secret, clock.step()))
    assert row(db_session, user.id) is None
    (off,) = entries(db_session, user.id, "user_2fa_disable")
    assert off.user_id == user.id
    assert json.loads(off.new_value) == {"second_step": "totp"}


@pytest.mark.regression
def test_turning_it_off_with_a_recovery_code(db_session, service, clock):
    user, _, codes = enabled_user(db_session, service, clock)
    service.disable(db_session, user, recovery_code=codes[4])
    assert row(db_session, user.id) is None
    assert len(entries(db_session, user.id, "user_2fa_recovery_used")) == 1
    (off,) = entries(db_session, user.id, "user_2fa_disable")
    assert json.loads(off.new_value) == {"second_step": "recovery"}


@pytest.mark.regression
def test_a_wrong_code_does_not_turn_it_off(db_session, service, clock):
    user, _, _ = enabled_user(db_session, service, clock)
    with pytest.raises(WrongCodeError):
        service.disable(db_session, user, code="123456")
    assert row(db_session, user.id) is not None
    assert entries(db_session, user.id, "user_2fa_disable") == []


@pytest.mark.regression
def test_new_recovery_codes_void_the_old_set(db_session, service, clock):
    user, secret, old = enabled_user(db_session, service, clock)
    new = service.regenerate_recovery_codes(
        db_session, user, code_at(secret, clock.step())
    )
    assert len(new) == 10 and not set(new) & set(old)
    with pytest.raises(WrongCodeError):
        service.verify_sign_in(db_session, user, recovery_code=old[0])
    assert service.verify_sign_in(db_session, user, recovery_code=new[0])
    assert len(entries(db_session, user.id, "user_2fa_recovery_regenerate")) == 1


@pytest.mark.regression
def test_a_reset_records_one_entry_per_account_and_never_commits(
    db_session, service, clock, session_factory
):
    first, _, _ = enabled_user(db_session, service, clock)
    second = make_user(db_session)
    service.enrol(db_session, second)  # waiting, not on
    actor = AuditActor(id=None, email="system:operator-cli")
    assert service.reset(db_session, [first, second], actor) == 2
    db_session.rollback()
    assert row(db_session, first.id) is not None  # nothing was committed
    assert service.reset(db_session, [first, second], actor) == 2
    db_session.commit()
    for user, enabled in ((first, True), (second, False)):
        assert row(db_session, user.id) is None
        (entry,) = entries(db_session, user.id, "user_2fa_reset")
        assert entry.user_id is None and entry.user_email == "system:operator-cli"
        assert json.loads(entry.old_value) == {"enabled": enabled}


@pytest.mark.regression
def test_no_entry_or_log_line_holds_a_secret_or_a_code(
    db_session, service, clock, caplog
):
    caplog.set_level(logging.DEBUG)
    user = make_user(db_session)
    enrolment = service.enrol(db_session, user)
    secret = secret_of(enrolment)
    confirm_code = code_at(secret, clock.step())
    codes = service.confirm(db_session, user, confirm_code, client_host="203.0.113.7")
    clock.now += totp.STEP_SECONDS
    wrong = code_at(secret, clock.step() + 9)
    with pytest.raises(WrongCodeError):
        service.verify_sign_in(db_session, user, code=wrong, client_host="203.0.113.7")
    with pytest.raises(WrongCodeError):
        service.verify_sign_in(db_session, user, recovery_code="BBBBBBBBBB")
    service.verify_sign_in(db_session, user, recovery_code=codes[2])
    renewed = service.regenerate_recovery_codes(
        db_session, user, code_at(secret, clock.step())
    )
    clock.now += totp.STEP_SECONDS
    service.disable(db_session, user, code=code_at(secret, clock.step()))

    stored = " ".join(
        f"{e.old_value} {e.new_value} {e.reason} {e.entity_name}"
        for e in db_session.execute(
            select(AuditLog).where(AuditLog.entity_id == user.id)
        ).scalars()
    )
    logged = " ".join(r.getMessage() + " " + repr(r.__dict__) for r in caplog.records)
    forbidden = [enrolment.secret, confirm_code, wrong, "BBBBBBBBBB", *codes, *renewed]
    for haystack in (stored, logged):
        found = [value for value in forbidden if value in haystack]
        assert found == []
    refusals = [
        r for r in caplog.records if getattr(r, "event", "") == "two_factor_refused"
    ]
    assert [(r.reason, r.client_host) for r in refusals] == [
        ("wrong_code", "203.0.113.7"),
        ("wrong_recovery_code", None),
    ]
    assert all(r.user_id == str(user.id) for r in refusals)


@pytest.mark.regression
def test_an_entry_that_cannot_be_written_takes_its_change_with_it(
    db_session, service, clock, monkeypatch
):
    """Turning it on, turning it off and new recovery codes are saved with
    their entry or not at all."""
    from backend.app.services.audit_service import AuditService

    user = make_user(db_session)
    secret = secret_of(service.enrol(db_session, user))
    real = AuditService.record

    def failing_record(db, **entry):
        added = real(db, **entry)
        added.entity_type = None  # NOT NULL: the commit fails
        return added

    monkeypatch.setattr(AuditService, "record", staticmethod(failing_record))
    with pytest.raises(IntegrityError):
        service.confirm(db_session, user, code_at(secret, clock.step()))
    db_session.rollback()
    assert row(db_session, user.id).confirmed_at is None
    monkeypatch.setattr(AuditService, "record", staticmethod(real))
    service.confirm(db_session, user, code_at(secret, clock.step() + 1))
    monkeypatch.setattr(AuditService, "record", staticmethod(failing_record))
    clock.now += 2 * totp.STEP_SECONDS
    with pytest.raises(IntegrityError):
        service.disable(db_session, user, code=code_at(secret, clock.step()))
    db_session.rollback()
    assert row(db_session, user.id) is not None
    clock.now += totp.STEP_SECONDS
    old = row(db_session, user.id).recovery_hashes
    with pytest.raises(IntegrityError):
        service.regenerate_recovery_codes(
            db_session, user, code_at(secret, clock.step())
        )
    db_session.rollback()
    assert row(db_session, user.id).recovery_hashes == old


def test_the_purposes_need_the_matching_row(db_session, service, clock):
    user = make_user(db_session)
    with pytest.raises(NotEnabledError):
        service.check_code(db_session, user, code="123456", purpose=SIGN_IN)
    with pytest.raises(NotEnabledError):
        service.confirm(db_session, user, "123456")
    enabled, secret, _ = enabled_user(db_session, service, clock)
    with pytest.raises(NotEnabledError):
        service.confirm(db_session, enabled, code_at(secret, clock.step()))
    assert db_session.execute(text("SELECT 1")).scalar_one() == 1


def test_a_new_service_reads_the_same_rows(db_session, service, clock, tracker):
    """Nothing is kept in the service object: another instance (another
    process) sees the same state."""
    user, secret, _ = enabled_user(db_session, service, clock)
    other = TwoFactorService(tracker=tracker, clock=Clock(clock.now))
    assert other.is_enabled(db_session, user.id) is True
    assert other.verify_sign_in(db_session, user, code=code_at(secret, clock.step()))
    with pytest.raises(WrongCodeError):
        service.verify_sign_in(db_session, user, code=code_at(secret, clock.step()))
    assert db_session.execute(select(UserTOTP.user_id)).first() is not None
