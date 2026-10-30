"""Database utilities."""

import json
import logging
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from datetime import datetime
from enum import Enum, StrEnum, auto
from functools import lru_cache
from json import JSONEncoder
from pathlib import Path
from typing import Any
from uuid import UUID

from pydantic import BaseModel
from sqlalchemy import URL, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncConnection, create_async_engine
from sqlmodel import SQLModel
from sqlmodel.ext.asyncio.session import AsyncSession

from ..util.config import Settings, get_settings

logger = logging.getLogger("ingest-robot.db")


class Persist(Enum):
    COMMIT = auto()
    FLUSH = auto()


class DictLikeEncoder(JSONEncoder):
    """Encode a dictionary to JSON."""

    def default(self, o: object) -> Any:  # noqa: ANN401
        # Translate datetime into a string
        if isinstance(o, datetime):
            return o.strftime("%Y-%m-%dT%H:%M:%S")

        # Translate Path into a string
        if isinstance(o, Path):
            return str(o)

        # Translate pydantic models into dict
        if isinstance(o, BaseModel):
            return o.model_dump()
        if isinstance(o, UUID):
            return str(o)
        if isinstance(o, Enum):
            return str(o.value)
        if isinstance(o, StrEnum):
            return o.value
        return json.JSONEncoder.default(self, o)


class ExtendedAsyncSession(AsyncSession):
    async def flush_or_commit(self, persist: Persist) -> None:
        """Commits and refreshes entities if commit is True, otherwise flushes to DB."""
        if persist == Persist.COMMIT:
            await self.commit()
        else:
            await self.flush()


class DatabaseEngine:
    """Utility class to handles the database connection, engine, and session."""

    def __init__(
        self,
        settings: Settings | None = None,
        kw_engine: dict[str, Any] | None = None,
        kw_session: dict[str, Any] | None = None,
    ) -> None:
        """Initialise the database engine."""
        self.settings = settings or get_settings()

        connection_str = URL.create(
            drivername="postgresql+psycopg",
            username=self.settings.db_user,
            password=self.settings.db_password,
            host=self.settings.db_host,
            port=self.settings.db_port,
            database=self.settings.db_name,
        )
        logger.info(f"Preparing database engine for {connection_str}")

        self.engine = create_async_engine(
            connection_str,
            **{"future": True, "json_serializer": DictLikeEncoder().encode, **(kw_engine or {})},
        )
        self.kw_session = {
            "bind": self.engine,
            "autoflush": False,
            "autocommit": False,
            "expire_on_commit": True,
            **(kw_session or {}),
        }

    async def check_connection(self) -> None:
        """Call this function to initialise the database engine."""
        try:
            logger.info("Testing database connection...")
            async with self.session() as session:
                await session.execute(text("SELECT 1;"))
                logger.info("Database connection seems ready.")
        except OperationalError as e:
            logger.error("Connection failed!")
            logger.exception(e)

    @asynccontextmanager
    async def session(self, existing_session: ExtendedAsyncSession | None = None) -> AsyncGenerator[ExtendedAsyncSession]:
        if existing_session is not None:
            # Re-use the outer session; do not close or rollback here (parent owns lifecycle)
            yield existing_session
        else:
            # Standard lifecycle for self-managed sessions
            session = ExtendedAsyncSession(**self.kw_session)
            try:
                yield session
            except Exception:
                await session.rollback()
                raise
            finally:
                await session.close()

    @asynccontextmanager
    async def connection(self) -> AsyncGenerator[AsyncConnection]:
        async with self.engine.begin() as conn:
            yield conn

    async def initialise(self) -> None:
        # engine = await make_engine()
        # SQLModel.metadata.create_all(engine)
        # https://github.com/fastapi/sqlmodel/pull/58
        logger.info("Initialising database models...")
        async with self.connection() as conn:
            await conn.run_sync(SQLModel.metadata.create_all)


@lru_cache(maxsize=1)
def get_engine() -> DatabaseEngine:
    """Get an engine helper."""
    return DatabaseEngine()
