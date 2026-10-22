from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

import httpx
from filelock import AsyncFileLock, Timeout
from pydantic import BaseModel

from app.util.logging import settings


class SearchIngest(BaseModel):
    ingest_id: str
    query_id: int
    azure_link: str  # Link with auth key to result file in azure blob storage
    is_queueing: bool  # Status on transfer to ingestion queue (started transfer file to queue)
    is_queued: bool  # Status on transfer to ingestion queue (completed transfer file to queue)
    checkpoint_bytes: int | None = None
    checkpoint_rows: int | None = None


async def get_transfer_requests(
    offset: int = 0,
    limit: int = 5,
    only_open: bool = False,
    is_queued: bool | None = None,
    is_queueing: bool | None = None,
) -> list[SearchIngest]:
    async with httpx.AsyncClient() as client:
        response = await client.get(
            f"{settings.api_wrapper_endpoint}/ingest/",
            params={
                "only_open": only_open,
                "is_queued": is_queued,
                "is_queueing": is_queueing,
                "offset": offset,
                "limit": limit,
            },
            headers={
                "api-token": settings.api_wrapper_api_key,
            },
        )
        return [SearchIngest.model_validate(req) for req in response.json()]


async def mark_queueing(ingest_id: str) -> SearchIngest:
    async with httpx.AsyncClient() as client:
        response = await client.put(
            f"{settings.api_wrapper_endpoint}/ingest/queueing",
            params={
                "ingest_id": ingest_id,
            },
            headers={
                "api-token": settings.api_wrapper_api_key,
            },
        )
        return SearchIngest.model_validate(response.json())


async def mark_queued(ingest_id: str) -> SearchIngest:
    async with httpx.AsyncClient() as client:
        response = await client.put(
            f"{settings.api_wrapper_endpoint}/ingest/queued",
            params={
                "ingest_id": ingest_id,
            },
            headers={
                "api-token": settings.api_wrapper_api_key,
            },
        )
        return SearchIngest.model_validate(response.json())


async def log_checkpoint(ingest_id: str, checkpoint_bytes: int, checkpoint_rows: int) -> SearchIngest:
    async with httpx.AsyncClient() as client:
        response = await client.put(
            f"{settings.api_wrapper_endpoint}/ingest/checkpoint",
            params={
                "ingest_id": ingest_id,
                "checkpoint_bytes": checkpoint_bytes,
                "checkpoint_rows": checkpoint_rows,
            },
            headers={
                "api-token": settings.api_wrapper_api_key,
            },
        )
        return SearchIngest.model_validate(response.json())


@asynccontextmanager
async def file_lock(lock_id: str, timeout: float = -1) -> AsyncGenerator[AsyncFileLock]:  # noqa: ASYNC109
    """
    Acquire an OS-level file lock anchored at `settings.locks_path`.

    :param lock_id: Identifier or relative subpath for the lock (e.g. "models/bert-base/a1b2c3")
    :param timeout: Max seconds to wait for lock acquisition (-1 for infinite wait)
    """
    lock_file = settings.transfer_locks_path / f"{lock_id}.lock"
    lock_file.parent.mkdir(parents=True, exist_ok=True)

    lock = AsyncFileLock(lock_file, timeout=timeout)
    async with lock:
        yield lock


async def is_file_locked(lock_id: str) -> bool:
    """Returns True if the file lock is held by any process."""
    try:
        # non-blocking acquire check
        async with file_lock(lock_id, timeout=0):
            return False  # We successfully acquired it, so it was NOT locked
    except Timeout:
        return True  # Another process is holding it


@asynccontextmanager
async def exclusive_transfer_request() -> AsyncGenerator[SearchIngest]:
    async def find_open_request() -> AsyncGenerator[SearchIngest]:
        # Test open transfer requests first
        for req in await get_transfer_requests(limit=5, only_open=True):
            yield req
        # Continue testing open but unfinished transfer requests
        for req in await get_transfer_requests(limit=5, is_queued=False):
            yield req

    async for request in find_open_request():
        try:
            async with file_lock(request.ingest_id, timeout=0):
                await mark_queueing(ingest_id=request.ingest_id)
                yield request
                # note: not marking end of queued here because we might have lost the lock due to an error.
                break
        except Timeout:
            pass
