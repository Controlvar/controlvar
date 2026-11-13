"""Reset two-step sign-in from where the database is reachable (#1812).

    python -m backend.app.db.two_factor_reset --email <address>
    python -m backend.app.db.two_factor_reset --all --yes-reset-every-account

Deletes the account's (or every account's) two-step sign-in: the stored
secret, the recovery codes and the count of wrong codes. Those accounts then
sign in with their password alone and can set two-step sign-in up again. It
is the way back for an administrator who lost their phone with no other
administrator to reset it, and after ``SECRET_KEY`` changes (every stored
secret was sealed under the old key and can no longer be read).

It signs nobody out: anyone signed in before the reset stays signed in until
that sign-in expires.

Runs wherever ``python -m backend.app.db.bootstrap`` runs, with the same
``POSTGRES_*`` settings. Each account reset writes one ``user_2fa_reset``
audit entry by ``system:operator-cli``, in the same transaction as the
delete, so both are kept or neither is. Prints how many accounts were reset.

Exit status: 0 when done (also when the account had nothing to reset); 2 when
the arguments are wrong (neither ``--email`` nor ``--all``, both, or
``--all`` without ``--yes-reset-every-account``), when no account has that
address, or when the database refuses the change, which then deletes nothing.
"""

from __future__ import annotations

import argparse
import logging
import sys
from typing import List, Optional, Sequence

from sqlalchemy import func, select, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

logger = logging.getLogger(__name__)

#: The flag ``--all`` needs as well, so that no one resets every account by a
#: slip of the keyboard.
CONFIRM_ALL_FLAG = "--yes-reset-every-account"

EXIT_OK = 0
EXIT_REFUSED = 2


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m backend.app.db.two_factor_reset",
        description=(
            "Reset two-step sign-in for one account, or for every account. "
            "The accounts then sign in with their password alone."
        ),
    )
    which = parser.add_mutually_exclusive_group(required=True)
    which.add_argument("--email", help="the account's email address, any letter case")
    which.add_argument(
        "--all",
        action="store_true",
        help=f"every account with two-step sign-in; needs {CONFIRM_ALL_FLAG} too",
    )
    parser.add_argument(
        CONFIRM_ALL_FLAG,
        dest="confirm_all",
        action="store_true",
        help="confirms --all",
    )
    return parser


def _search_path(session: Session, engine: Engine, schema: str) -> None:
    quoted = engine.dialect.identifier_preparer.quote_schema(schema)
    # `schema` comes from POSTGRES_SCHEMA, never from a request, and an
    # identifier cannot be a bind parameter: quoted through the dialect.
    search_path = text(f"SET search_path TO {quoted}, public")  # nosemgrep: python.sqlalchemy.security.audit.avoid-sqlalchemy-text.avoid-sqlalchemy-text  # fmt: skip
    session.execute(search_path)


def run(
    argv: Optional[Sequence[str]] = None,
    engine: Optional[Engine] = None,
    schema: Optional[str] = None,
    out=None,
) -> int:
    """The command, returning its exit status (see the module text)."""
    out = out or sys.stdout
    try:
        args = _parser().parse_args(list(argv) if argv is not None else None)
    except SystemExit as exit_:
        return EXIT_OK if exit_.code == 0 else EXIT_REFUSED
    if args.all and not args.confirm_all:
        print(
            f"Refused: --all resets every account's two-step sign-in; add "
            f"{CONFIRM_ALL_FLAG} to do that. Nothing was changed.",
            file=out,
        )
        return EXIT_REFUSED
    if args.confirm_all and not args.all:
        print(f"Refused: {CONFIRM_ALL_FLAG} goes with --all only.", file=out)
        return EXIT_REFUSED

    from sqlalchemy import create_engine

    from backend.app.db import bootstrap
    from backend.app.models.user import User
    from backend.app.models.user_totp import UserTOTP
    from backend.app.services.audit_service import SYSTEM_OPERATOR_CLI
    from backend.app.services.two_factor_service import TwoFactorService

    schema = schema or bootstrap.schema_name()
    # The ORM path, as the bootstrap's first administrator takes it: the
    # mapped classes name ``schema`` whatever they were imported under.
    bootstrap._point_models_at_schema(schema)
    engine = engine or create_engine(bootstrap.database_url())
    session = sessionmaker(bind=engine)()
    try:
        _search_path(session, engine, schema)
        if args.all:
            users: List[User] = list(
                session.scalars(
                    select(User).where(User.id.in_(select(UserTOTP.user_id)))
                ).all()
            )
        else:
            typed = args.email.strip()
            users = list(
                session.scalars(
                    select(User)
                    .where(func.lower(User.email) == func.lower(typed))
                    .limit(2)
                ).all()
            )
            if len(users) != 1:
                print(
                    "Refused: no account has that email address. Nothing was changed.",
                    file=out,
                )
                return EXIT_REFUSED
        count = TwoFactorService().reset(session, users, SYSTEM_OPERATOR_CLI)
        session.commit()
    except Exception as exc:
        session.rollback()
        # The type only: a database message can quote a value.
        print(
            f"Refused: the database did not take the change ({type(exc).__name__}). "
            "Nothing was changed.",
            file=out,
        )
        return EXIT_REFUSED
    finally:
        session.close()
    noun = "account" if count == 1 else "accounts"
    print(f"Reset two-step sign-in for {count} {noun}.", file=out)
    return EXIT_OK


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    raise SystemExit(run())
