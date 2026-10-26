"""
Azure Blob download doubles — the `BlobClient` surface that `app/robots/queue.py` uses.

`fake_blob_client(storage)` builds a class replacement for `app.robots.queue.BlobClient`:
`from_blob_url()` returns an async-context-managed client whose `download_blob(offset=...)`
serves `storage.data` from that offset in sequential blocks, exactly like the real SDK streams
a `StorageStreamDownloader.chunks()`. `FakeBlobStorage` records every download request (so tests
can assert where a restart resumed) and can inject the failure modes of a real transfer:
empty blocks between the data blocks, or the connection dying after a given number of blocks.
"""

from collections.abc import AsyncIterator
from typing import Self

from azure.core.exceptions import ServiceResponseError


class FakeBlobStorage:
    """The bytes behind a SAS URL: served in `block_size` blocks, optionally interrupted or padded with empty blocks."""

    def __init__(
        self,
        data: bytes,
        *,
        block_size: int = 4096,
        empty_blocks: bool = False,
        fail_after_blocks: int | None = None,
    ) -> None:
        self.data = data
        self.block_size = block_size
        self.empty_blocks = empty_blocks
        self.fail_after_blocks = fail_after_blocks
        self.urls: list[str] = []  # SAS URL of every client opened against this blob
        self.requests: list[int] = []  # byte offset of every download request, in order (resume position of a restart is visible here)

    def blocks(self, offset: int) -> list[bytes]:
        """The download blocks the real SDK would serve for a request starting at `offset`."""
        if not 0 <= offset <= len(self.data):
            raise AssertionError(f"download requested outside the blob: offset={offset:,} > size={len(self.data):,}")
        blocks = [self.data[i : i + self.block_size] for i in range(offset, len(self.data), self.block_size)]
        if self.empty_blocks:
            # Azure really does hand out empty chunks between (and after) the data blocks of a stream
            blocks = [chunk for block in blocks for chunk in (b"", block)] + [b""]
        return blocks


def fake_blob_client(storage: FakeBlobStorage) -> type:
    """Build the `BlobClient` replacement class that tests monkeypatch into `app.robots.queue`."""

    class FakeDownloader:
        def __init__(self, offset: int) -> None:
            self._blocks = storage.blocks(offset)

        async def chunks(self) -> AsyncIterator[bytes]:
            for index, block in enumerate(self._blocks, start=1):
                if storage.fail_after_blocks is not None and index > storage.fail_after_blocks:
                    raise ServiceResponseError("The connection was closed by the service mid-transfer.")
                yield block

    class FakeBlobClient:
        def __init__(self, sas_url: str) -> None:
            self.sas_url = sas_url

        @classmethod
        def from_blob_url(cls, blob_url: str) -> FakeBlobClient:
            storage.urls.append(blob_url)
            return cls(blob_url)

        async def __aenter__(self) -> Self:
            return self

        async def __aexit__(self, *_exc_info: object) -> None:
            return None

        async def download_blob(self, offset: int = 0) -> FakeDownloader:
            storage.requests.append(offset)
            return FakeDownloader(offset)

    return FakeBlobClient
