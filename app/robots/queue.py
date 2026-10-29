"""Task for single loop of the queue runner."""

import json
from collections.abc import AsyncGenerator

from azure.storage.blob.aio import BlobClient

from app.db.engine import Persist
from app.db.ingest import IngestTracker
from app.db.schemas import Format, Record
from app.util import Runner, async_batched, get_settings

settings = get_settings()


async def _read_from_blob_storage(
    sas_url: str,
    byte_offset: int = 0,
    encoding: str = "utf-8",
) -> AsyncGenerator[tuple[str, int]]:
    """
    Asynchronously streams a file line by line from an Azure Blob SAS URL.

    :param sas_url: Azure Blob URL containing the SAS token.
    :param byte_offset: The byte position to resume reading from (default 0).
    :param encoding: Character encoding of the file (default 'utf-8').
    :yields: Tuple of (line_text, next_byte_offset).
    """
    async with BlobClient.from_blob_url(sas_url) as blob_client:
        downloader = await blob_client.download_blob(offset=byte_offset)

        current_offset = byte_offset
        buffer = b""

        async for chunk in downloader.chunks():
            buffer += chunk
            while b"\n" in buffer:
                line_bytes, buffer = buffer.split(b"\n", 1)
                current_offset += len(line_bytes) + 1
                line_str = line_bytes.decode(encoding).rstrip("\r")
                yield line_str, current_offset

        if buffer:
            current_offset += len(buffer)
            yield buffer.decode(encoding), current_offset


class IngestionQueueRunner(Runner):
    """Runner for transferring raw source data to ingestion queue."""

    async def _loop_task(self) -> None:
        tracking = IngestTracker(settings=self.settings, logger=self.loop_logger.getChild("tracking-db"))
        num_rows = 0
        latest_offset = 0
        async with (
            tracking.exclusive_transfer_request() as request,
            self.store.db.session() as session,
        ):
            if request is None:
                self.loop_logger.info("Found no open transfer request")
                return

            self.loop_logger.info(
                f"Received lock on transfer request ("
                f"id={request.ingest_id} "
                f"| is_queueing={request.is_queueing} "
                f"| is_queued={request.is_queued} "
                f"| checkpoint_bytes={request.checkpoint_bytes} "
                f"| checkpoint_rows={request.checkpoint_rows})",
            )

            async for batch in async_batched(
                _read_from_blob_storage(sas_url=request.azure_link, byte_offset=request.checkpoint_bytes or 0),
                batch_size=settings.api_wrapper_checkpoint_interval,
            ):
                for line, offset in batch:
                    self.total_entries_processed += 1
                    num_rows += 1
                    latest_offset = offset
                    session.add(
                        Record(
                            format=Format.SCOPUS_API,  # TODO: adapt the format to the actual one
                            raw=json.loads(line),
                            openalex_id=None,
                            doi=None,
                            wos_id=None,
                            scopus_id=None,
                            s2_id=None,
                            pubmed_id=None,
                            dimensions_id=None,
                        ),
                    )

                # Log checkpoint so we can recover in case of a crash
                await tracking.log_checkpoint(
                    ingest_id=request.ingest_id,
                    checkpoint_bytes=latest_offset,
                    checkpoint_rows=num_rows + (request.checkpoint_rows or 0),
                    session=session,
                    commit=Persist.FLUSH,
                )
                # commit latest additions to database
                await session.commit()

                self.loop_logger.info(
                    f"[Transfer {request.ingest_id}] Transferred {num_rows:,} rows to queue at byte {latest_offset:,}.",
                )

        await tracking.mark_queued(ingest_id=request.ingest_id)

        self.loop_logger.info(f"[Total: {self.total_entries_processed:,} entries] Transferred {num_rows:,} rows to queue.")
