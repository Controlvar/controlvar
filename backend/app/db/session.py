"""
Database session and connection management.

This module provides database session functionality and connection pooling
for the application.
"""

import os
from typing import Generator

from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.schema import CreateSchema

from backend.app.core.config import settings
from backend.app.core.database_config import get_schema_name
from backend.app.db import request_sessions
from backend.app.db.url import postgres_url
from backend.app.models.base import Base

# Check for DATABASE_URI environment variable, otherwise use settings
database_uri = os.getenv("DATABASE_URI")
if not database_uri:
    # Fall back to SQLALCHEMY_DATABASE_URI if set
    if (
        hasattr(settings, "SQLALCHEMY_DATABASE_URI")
        and settings.SQLALCHEMY_DATABASE_URI
    ):
        database_uri = str(settings.SQLALCHEMY_DATABASE_URI)
    # Fall back to DATABASE_URI if set
    elif hasattr(settings, "DATABASE_URI") and settings.DATABASE_URI:
        database_uri = str(settings.DATABASE_URI)
    # Fall back to constructing from components
    else:
        # Password percent-encoded (#146): see backend/app/db/url.py.
        database_uri = postgres_url(
            user=settings.POSTGRES_USER,
            password=settings.POSTGRES_PASSWORD,
            host=settings.POSTGRES_SERVER,
            port=settings.POSTGRES_PORT,
            database=settings.POSTGRES_DB,
        )

# Create engine with appropriate configuration
engine = create_engine(
    database_uri,
    pool_pre_ping=True,  # Test connections before using them
    pool_size=20,  # Increased pool size
    max_overflow=20,  # Increased overflow
    pool_recycle=300,  # Recycle connections after 5 minutes
    pool_timeout=30,  # Wait up to 30 seconds for a connection
    connect_args={
        "connect_timeout": 10,  # Connection timeout in seconds
        "keepalives": 1,  # Enable keepalive
        "keepalives_idle": 30,  # Idle time before sending keepalive
        "keepalives_interval": 10,  # Interval between keepalives
        "keepalives_count": 5,  # Number of keepalive attempts
    },
)

# Session factory
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


async def get_db() -> Generator:
    """
    Get a database session.

    The session is registered as one of the request's sessions
    (``backend.app.db.request_sessions``) until it is closed.

    Yields:
        Generator: A SQLAlchemy session
    """
    db = SessionLocal()
    request_sessions.register(db)
    try:
        yield db
    finally:
        try:
            db.close()
        finally:
            request_sessions.unregister(db)


def init_db() -> None:
    """Initialize the database by creating all tables."""
    # Import all models here to ensure they are registered with the metadata

    # Get schema name
    schema_name = get_schema_name()

    # Create schema if it doesn't exist and point search_path at it
    _ensure_schema(schema_name)

    # Set schema for all tables
    Base.metadata.schema = schema_name

    # Create tables
    Base.metadata.create_all(bind=engine)


def _ensure_schema(schema_name: str) -> None:
    """Create *schema_name* if missing and set it as the search_path.

    Uses the SQLAlchemy DDL construct and a bound parameter instead of
    string-formatted SQL so the identifier is quoted by the dialect.
    """
    with engine.connect() as conn:
        conn.execute(CreateSchema(schema_name, if_not_exists=True))
        conn.execute(text("COMMIT"))

        conn.execute(text("SET search_path TO :schema").bindparams(schema=schema_name))
        conn.execute(text("COMMIT"))


def reset_db() -> None:
    """Reset the database by dropping and recreating all tables."""
    # Import all models here to ensure they are registered with the metadata

    # Get schema name
    schema_name = get_schema_name()

    # Set schema for all tables
    Base.metadata.schema = schema_name

    # Drop and recreate tables
    Base.metadata.drop_all(bind=engine)

    # Create schema if it doesn't exist and point search_path at it
    _ensure_schema(schema_name)

    # Create tables
    Base.metadata.create_all(bind=engine)
