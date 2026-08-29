"""Engine, session factory and declarative base.

Sync SQLAlchemy on purpose: `with_for_update()` semantics are unambiguous and
there is no async session/greenlet surface to get wrong under time pressure.
FastAPI runs sync endpoints in a worker threadpool, so concurrency is real —
which is exactly what the race test exercises.
"""

from collections.abc import Iterator

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.config import settings

engine = create_engine(
    settings.database_url,
    pool_pre_ping=True,   # survives a Postgres restart during the build day
    pool_size=10,
    max_overflow=20,
    future=True,
)

SessionLocal = sessionmaker(
    bind=engine,
    autocommit=False,
    autoflush=False,
    expire_on_commit=False,   # returned ORM objects stay usable after commit()
    class_=Session,
)


class Base(DeclarativeBase):
    """Declarative base. Alembic owns the schema; models never create_all()."""


def get_db() -> Iterator[Session]:
    """FastAPI dependency. One session per request, always closed."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
