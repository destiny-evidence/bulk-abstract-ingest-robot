"""Database utilities."""

import json
import logging
from contextlib import asynccontextmanager
from datetime import datetime
from json import JSONEncoder
from pathlib import Path
from typing import (
    TYPE_CHECKING,
    Any,
)

from pydantic import BaseModel
from sqlalchemy import URL, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from .config import Settings, get_settings

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

logger = logging.getLogger("ingest-robot.db")


class DictLikeEncoder(JSONEncoder):
    """Encode a dictionary to JSON."""

    def default(self, o: Any) -> Any:  # noqa: ANN401, D102
        # Translate datetime into a string
        if isinstance(o, datetime):
            return o.strftime("%Y-%m-%dT%H:%M:%S")

        # Translate Path into a string
        if isinstance(o, Path):
            return str(o)

        # Translate pydantic models into dict
        if isinstance(o, BaseModel):
            return o.model_dump()

        return json.JSONEncoder.default(self, o)


class DatabaseEngine:
    """Utility class to handles the database connection, engine, and session."""

    def __init__(  # noqa: PLR0913
        self,
        host: str,
        port: int,
        user: str,
        password: str,
        database: str,
        debug: bool = False,
        kw_engine: dict[str, Any] | None = None,
        kw_session: dict[str, Any] | None = None,
    ) -> None:
        """Initialise the database engine."""
        self._host = host
        self._port = port
        self._user = user
        self._password = password
        self._database = database

        self._connection_str = URL.create(
            drivername="postgresql+psycopg",
            username=self._user,
            password=self._password,
            host=self._host,
            port=self._port,
            database=self._database,
        )
        self.engine = create_async_engine(
            self._connection_str,
            **{
                "echo": debug,
                "future": True,
                "json_serializer": DictLikeEncoder().encode,
                **(kw_engine or {}),
            },
        )
        self._session: async_sessionmaker[AsyncSession] = async_sessionmaker(
            **{
                "bind": self.engine,
                "autoflush": False,
                "autocommit": False,
                "expire_on_commit": True,
                **(kw_session or {}),
            },
        )

    async def check_connection(self) -> None:
        """Call this function to initialise the database engine."""
        try:
            logger.debug("AsyncEngine starting up...")
            logger.info(
                f"AsyncEngine connecting to {self._user}:****@{self._host}:{self._port}/{self._database}",
            )
            async with self._session() as session:
                await session.execute(text("SELECT 1;"))
                logger.info("Connection seems to be ready.")
        except OperationalError as e:
            logger.error("Connection failed!")
            logger.exception(e)

    @asynccontextmanager
    async def session(self) -> AsyncIterator[AsyncSession]:
        """Get a managed session."""
        session: AsyncSession = self._session()

        if logger.isEnabledFor(logging.DEBUG):
            import inspect  # noqa: PLC0415

            curframe = inspect.currentframe()
            calframe = inspect.getouterframes(curframe, 2)
            logger.debug(
                f"New session for: {' <- '.join([fr[3] for fr in calframe[2:6]])}",
            )

        try:
            yield session
            # await session.commit()  # noqa: ERA001
        except Exception as e:
            await session.rollback()
            raise e  # noqa: TRY201
        finally:
            await session.close()


def get_engine(debug: bool = False, settings: Settings | None = None) -> DatabaseEngine:
    """Get an engine helper."""
    if not settings:
        settings = get_settings()

    return DatabaseEngine(
        host=settings.pik_db_host,
        port=settings.pik_db_port,
        user=settings.pik_db_user,
        password=settings.pik_db_password,
        database=settings.pik_db_name,
        debug=debug,
    )
