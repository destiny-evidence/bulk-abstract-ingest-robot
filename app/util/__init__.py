"""Utility module."""

from collections.abc import AsyncGenerator, AsyncIterable
from typing import TypeVar

from .config import Settings, get_settings
from .logging import init_logging
from .repository import Repository
from .runner import Runner

T = TypeVar("T")


async def async_batched[T](iterable: AsyncIterable[T], batch_size: int) -> AsyncGenerator[list[T]]:
    """Batch data from an async iterable into tuples of length n."""
    if batch_size < 1:
        raise ValueError("n must be at least one")

    # Retrieve items in chunks using an inner buffer
    batch: list[T] = []
    async for item in iterable:
        batch.append(item)
        if len(batch) == batch_size:
            yield batch
            batch = []
    if batch:
        yield batch


__all__ = [
    "Repository",
    "Runner",
    "Settings",
    "async_batched",
    "get_settings",
    "init_logging",
]
