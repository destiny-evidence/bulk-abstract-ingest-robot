"""
Real `AbstractStore` against a throwaway SQLite database.

`app/db/store.py` decides which queued records the match runner gets to see and writes the
verdicts back onto them (`has_eligible_abstract`, `encountered_parsing_error`). The
`MockStore`-based tests cannot cover those rules — a mock that mirrored the query would
mirror its bugs as well — so `tests/test_store.py` runs the real store against a temporary
SQLite file instead of a live Postgres:

- the `raw` column's Postgres `JSONB` type compiles to SQLite `JSON` instead (see `_compile_jsonb`);
- `NullPool` hands out a fresh connection per session, so a test may call `asyncio.run`
  repeatedly without the pool ever carrying one event loop's connection into the next;
- `seed` and `fetch_rows` insert and read back `records` rows within the test's own flow,
  next to the store calls whose effects they assert on (`fetch_matches` reads back the
  `m2m_record_ods` rows the store created for them).
"""

import logging
from pathlib import Path
from typing import Any

from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.pool import NullPool
from sqlmodel import SQLModel, select

from app.db.engine import ExtendedAsyncSession
from app.db.schemas import Record, RecordMatch
from app.db.store import AbstractStore
from app.util.config import Settings


@compiles(JSONB, "sqlite")
def _compile_jsonb(type_: Any, compiler: Any, **kw: Any) -> str:  # noqa: ANN401, ARG001 — fixed signature required by SQLAlchemy's compiles hook
    """Store the `raw` JSONB column as plain SQLite JSON."""
    return "JSON"


def sqlite_store(settings: Settings, path: Path) -> AbstractStore:
    """Build a real `AbstractStore` whose database engine talks to a SQLite file instead of Postgres."""
    store = AbstractStore(settings=settings, logger=logging.getLogger("test.store"))
    store.db.engine = create_async_engine(f"sqlite+aiosqlite:///{path}", poolclass=NullPool)
    store.db.kw_session["bind"] = store.db.engine
    return store


async def create_tables(store: AbstractStore) -> None:
    """Create the schema (every table, as production startup does)."""
    async with store.db.engine.begin() as connection:
        await connection.run_sync(SQLModel.metadata.create_all)


async def seed(store: AbstractStore, *records: Record) -> None:
    """Insert already-queued records, the way the queue runner leaves them behind."""
    # Not via `store.db.session()`: with `expire_on_commit=False` the test keeps its handle on the rows it seeded,
    # and no store transaction can be entangled with the seeding.
    session = ExtendedAsyncSession(bind=store.db.engine, autoflush=False, expire_on_commit=False)
    try:
        for record in records:
            session.add(record)
        await session.commit()
    finally:
        await session.close()


async def fetch_rows(store: AbstractStore) -> list[Record]:
    """Read back all `records` rows (detached) in seed order, to inspect the verdicts the store committed."""
    async with store.db.session() as session:
        rows = (await session.exec(select(Record))).all()
        session.expunge_all()
        return list(rows)


async def fetch_matches(store: AbstractStore) -> list[RecordMatch]:
    """Read back all `m2m_record_ods` rows (detached), to inspect the matches the store committed."""
    async with store.db.session() as session:
        rows = (await session.exec(select(RecordMatch))).all()
        session.expunge_all()
        return list(rows)
