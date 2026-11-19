"""Which local accounts must use two-step sign-in (#1812).

An operator lists roles in ``LOCAL_AUTH_2FA_REQUIRED_ROLES``
(``settings_rules.parse_two_factor_roles`` decides what it accepts). Every
account of a listed role that signs in with a password must use two-step
sign-in, superusers included:

* its password step answers ``enrol: true`` until it is set up, and the
  account then sets it up before it gets a session
  (``POST /api/v1/auth/mfa/enroll`` and ``/auth/mfa/enroll/confirm``);
* it cannot turn it off (``POST /api/v1/users/me/2fa/disable`` answers 403);
* ``GET /api/v1/users/me/2fa`` says ``required: true``.

An account with no password of its own (one that signs in through SSO) is
never asked: the requirement applies where the password is checked, and SSO
asks for no code. The role is read from the account as the database holds it
at the moment of each check.
"""

from __future__ import annotations

from typing import Any

from backend.app.core.config import settings
from backend.app.core.security import role_name


def two_factor_required(user: Any) -> bool:
    """Whether ``user`` must use two-step sign-in: its role is listed in
    ``LOCAL_AUTH_2FA_REQUIRED_ROLES`` and it has a password of its own."""
    required = settings.LOCAL_AUTH_2FA_REQUIRED_ROLES
    if not required:
        return False
    if not getattr(user, "hashed_password", None):
        return False
    return role_name(getattr(user, "role", None)) in required
