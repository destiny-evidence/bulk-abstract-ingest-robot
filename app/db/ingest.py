"""Utility class to interact with ingest table."""

import logging
from collections.abc import AsyncGenerator, Sequence
from contextlib import asynccontextmanager
from uuid import UUID

from filelock import AsyncFileLock, Timeout
from sqlmodel import desc, select

from ..util.config import Settings
from .engine import DatabaseEngine
from .schemas import SearchIngest


class IngestTracker:
    def __init__(
        self,
        settings: Settings,
        logger: logging.Logger,
    ) -> None:
        """Initialise the store."""
        self.settings = settings
        self.logger = logger
        self.db = DatabaseEngine(settings=settings)

    async def list_ingests(
        self,
        offset: int = 0,
        limit: int = 5,
        only_open: bool = False,
        is_queued: bool | None = None,
        is_queueing: bool | None = None,
    ) -> Sequence[SearchIngest]:
        async with self.db.session() as session:
            stmt = select(SearchIngest).order_by(desc(SearchIngest.ingest_id)).limit(limit).offset(offset)
            if only_open:
                stmt = stmt.where(
                    SearchIngest.is_queued == False,
                    SearchIngest.is_queueing == False,
                )
            else:
                if is_queued is not None:
                    stmt = stmt.where(SearchIngest.is_queued == is_queued)
                if is_queueing is not None:
                    stmt = stmt.where(SearchIngest.is_queueing == is_queueing)
            return (await session.exec(stmt)).all()

    async def mark_queueing(
        self,
        ingest_id: str | UUID,
    ) -> SearchIngest:
        async with self.db.session() as session:
            ingest = await session.get(SearchIngest, ingest_id)
            if ingest is None:
                raise AssertionError(f"No info found for ingest_id={ingest_id}")

            if ingest.is_queueing:
                # Already marked as queueing
                return ingest

            ingest.is_queueing = True
            ingest.is_queued = False  # explicitly (re-)set this
            await session.commit()
            await session.refresh(ingest)
            return ingest

    async def mark_queued(
        self,
        ingest_id: str | UUID,
    ) -> SearchIngest:
        async with self.db.session() as session:
            ingest = await session.get(SearchIngest, ingest_id)
            if ingest is None:
                raise AssertionError(f"No info found for ingest_id={ingest_id}")

            if not ingest.is_queueing:
                raise AssertionError(f"Trying to mark queued before queueing ingest_id={ingest_id}")

            if ingest.is_queued:
                return ingest

            ingest.is_queued = True
            await session.commit()
            await session.refresh(ingest)
            return ingest

    async def log_checkpoint(
        self,
        ingest_id: str | UUID,
        checkpoint_bytes: int,
        checkpoint_rows: int,
    ) -> SearchIngest:
        async with self.db.session() as session:
            ingest = await session.get(SearchIngest, ingest_id)
            if ingest is None:
                raise AssertionError(f"No info found for ingest_id={ingest_id}")

            if not ingest.is_queueing or ingest.is_queued:
                raise AssertionError(f"Trying to write checkpoint for unqueued for fully queued ingest_id={ingest_id}")

            ingest.checkpoint_bytes = checkpoint_bytes
            ingest.checkpoint_rows = checkpoint_rows
            await session.commit()
            await session.refresh(ingest)
            return ingest

    async def add_ingest(
        self,
        azure_link: str,
    ) -> SearchIngest:
        async with self.db.session() as session:
            ingest = SearchIngest(azure_link=azure_link)
            session.add(ingest)
            await session.commit()
            await session.refresh(ingest)
            return ingest

    @asynccontextmanager
    async def file_lock(self, lock_id: str | UUID, timeout: float = -1) -> AsyncGenerator[AsyncFileLock]:  # noqa: ASYNC109
        """
        Acquire an OS-level file lock anchored at `settings.locks_path`.

        :param lock_id: Identifier or relative subpath for the lock (e.g. "models/bert-base/a1b2c3")
        :param timeout: Max seconds to wait for lock acquisition (-1 for infinite wait)
        """
        lock_file = self.settings.transfer_locks_path / f"{lock_id}.lock"
        lock_file.parent.mkdir(parents=True, exist_ok=True)

        lock = AsyncFileLock(lock_file, timeout=timeout)
        async with lock:
            yield lock

    async def is_file_locked(self, lock_id: str) -> bool:
        """Returns True if the file lock is held by any process."""
        try:
            # non-blocking acquire check
            async with self.file_lock(lock_id, timeout=0):
                return False  # We successfully acquired it, so it was NOT locked
        except Timeout:
            return True  # Another process is holding it

    @asynccontextmanager
    async def exclusive_transfer_request(self) -> AsyncGenerator[SearchIngest]:
        async def find_open_request() -> AsyncGenerator[SearchIngest]:
            # Test open transfer requests first
            for req in await self.list_ingests(limit=5, only_open=True):
                yield req
            # Continue testing open but unfinished transfer requests
            for req in await self.list_ingests(limit=5, is_queued=False):
                yield req

        async for request in find_open_request():
            try:
                async with self.file_lock(request.ingest_id, timeout=0):
                    await self.mark_queueing(ingest_id=request.ingest_id)
                    yield request
                    # note: not marking end of queued here because we might have lost the lock due to an error.
                    break
            except Timeout:
                pass
