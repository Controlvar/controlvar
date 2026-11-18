"""Re-encrypt every stored warehouse credential under the newest key.

Step 4 of rotating ``WAREHOUSE_CREDENTIALS_KEYS`` (docs: Self-hosting ›
Warehouse):

1. generate the new key K2;
2. set the secret to ``K2,K1`` -- the new key first -- and deploy, so every
   task can decrypt under both;
3. run this command once, as a one-off task of the new deployment::

       python -m modules.backend.app.scripts.rotate_warehouse_credentials

   It re-encrypts every stored credential (current and pending) under K2 and
   prints ``rotated=<n> undecryptable=<m>``;
4. check ``undecryptable=0``;
5. set the secret to ``K2`` alone and deploy.

A credential no configured key opens is left exactly as it is and counted;
the command then exits 1 and lists the connection ids (never a key, a token
or a credential), so that step 5 is not taken while something still needs
K1.  Without usable keys it changes nothing and exits 2.

Everything happens in one transaction with the rows locked, so a connection
saved while this runs is either rotated or written under K2 already.
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass, field
from typing import Iterable, Optional, Sequence
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from modules.backend.app.core.credential_crypto import (
    CredentialKeysUnavailable,
    CredentialUndecryptable,
    encrypt,
    rotate,
)
from modules.backend.app.models.warehouse_connection import WarehouseConnection

#: The two columns that hold a credential token.
TOKEN_COLUMNS: tuple[str, ...] = (
    "credentials_ciphertext",
    "pending_credentials_ciphertext",
)

EXIT_OK = 0
EXIT_UNDECRYPTABLE = 1
EXIT_KEYS_UNAVAILABLE = 2


@dataclass
class RotationReport:
    """What one rotation did.

    The two counts are in different units, on purpose:

    * ``rotated`` counts **tokens** (columns): a connection with a current and
      a pending credential contributes 2;
    * ``undecryptable`` lists **connections**: each id once, however many of
      its tokens no key opens -- it is the list an operator acts on.
    """

    rotated: int = 0
    undecryptable: list[UUID] = field(default_factory=list)

    def summary(self) -> str:
        return f"rotated={self.rotated} undecryptable={len(self.undecryptable)}"


def rotate_all(
    db: Session,
    *,
    keys: Optional[str] = None,
    connection_ids: Optional[Iterable[UUID]] = None,
) -> RotationReport:
    """Re-encrypt every stored token under the first configured key.

    Commits on success.  Raises ``CredentialKeysUnavailable`` -- having
    changed nothing -- when no usable keys are configured.  *connection_ids*
    limits the rotation to those rows (all rows when None).
    """
    statement = (
        select(WarehouseConnection).order_by(WarehouseConnection.id).with_for_update()
    )
    if connection_ids is not None:
        statement = statement.where(WarehouseConnection.id.in_(list(connection_ids)))

    # Refuse before touching anything, even when no row holds a token: a
    # rotation "done" with no keys configured is not a rotation.
    encrypt(b"", keys=keys)

    report = RotationReport()
    try:
        for connection in db.execute(statement).scalars():
            failed = False
            for column in TOKEN_COLUMNS:
                token = getattr(connection, column)
                if token is None:
                    continue
                try:
                    setattr(connection, column, rotate(bytes(token), keys=keys))
                except CredentialUndecryptable:
                    failed = True
                    continue
                report.rotated += 1
            if failed:
                report.undecryptable.append(connection.id)
        db.commit()
    except BaseException:
        # CredentialKeysUnavailable included: nothing is half-rotated.
        db.rollback()
        raise
    return report


def _session() -> Session:
    """A session on the database the ``POSTGRES_*`` variables name."""
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from backend.app.db.bootstrap import database_url
    from backend.app.models import register_core_models
    from backend.app.modules_loader import require_modules_or_absent

    # The mappers resolve their foreign keys against the core tables.
    register_core_models()
    require_modules_or_absent()
    # hide_parameters: errors omit SQLAlchemy's "[parameters: ...]" listing.
    engine = create_engine(database_url(), hide_parameters=True, pool_pre_ping=True)
    return sessionmaker(bind=engine, autoflush=False)()


def main(argv: Optional[Sequence[str]] = None, db: Optional[Session] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m modules.backend.app.scripts.rotate_warehouse_credentials",
        description=(
            "Re-encrypt every stored warehouse credential under the first key "
            "in WAREHOUSE_CREDENTIALS_KEYS."
        ),
    )
    parser.parse_args(argv)

    session = db if db is not None else _session()
    try:
        report = rotate_all(session)
    except CredentialKeysUnavailable as exc:
        # The message names the setting and, for a malformed value, the
        # position of the bad entry -- never a key.
        sys.stderr.write(f"not rotated: {exc}\n")
        return EXIT_KEYS_UNAVAILABLE
    finally:
        if db is None:
            session.close()

    sys.stdout.write(report.summary() + "\n")
    if report.undecryptable:
        ids = ", ".join(str(i) for i in report.undecryptable)
        sys.stderr.write(
            "These connections hold a credential no configured key can decrypt; "
            f"keep the old key until they are re-entered: {ids}\n"
        )
        return EXIT_UNDECRYPTABLE
    return EXIT_OK


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
