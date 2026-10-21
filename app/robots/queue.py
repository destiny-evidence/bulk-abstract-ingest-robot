"""Task for single loop of the queue runner."""

from app.db.schemas import Record
from app.util import Runner
from app.util.wrapper_api import get_transfer_requests, mark_queueing, mark_queued


class IngestionQueueRunner(Runner):
    """Runner for transferring raw source data to ingestion queue."""

    async def _loop_task(self) -> None:
        open_requests = await get_transfer_requests(limit=1)
        if len(open_requests) == 0:
            return
        request = open_requests[0]

        # TODO: open file stream from `request.azure_link`
        async  with self.store.db.session() as session:
            await mark_queueing(ingest_id=request.ingest_id)
            # TODO: for each line create
            session.add(Record())  # TODO: populate values
            self.total_entries_processed += 1
            # note: only commit in the very end, maybe flush every now and then
            await mark_queued(ingest_id=request.ingest_id)

        self.loop_logger.info(f"[Total: {self.total_entries_processed:,} entries] Submitted {len(cache_entries):,} enhancements.")
