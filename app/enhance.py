"""Task for single loop of the enhancement runner."""

from .util import Runner


class EnhancementRunner(Runner):
    """Runner for writing abstract enhancements to repository.."""

    async def _loop_task(self) -> None:
        """Task for single loop of the enhancement runner."""
        # Poll for approved requests for enhancements
        batch_info, references = await self.repository.get_next_batch()

        if batch_info is None or references is None:
            self.loop_logger.debug("No batches available")
            return

        # Get matching data from store (could be different to what we just asked to enhance)
        cache_entries = await self.store.get_entries(
            destiny_ids={reference.id for reference in references},
            ensure_overlap=True,
        )

        # Submit to repository
        await self.repository.submit_enhancements(batch_info=batch_info, cache_entries=cache_entries)

        # Remember we submitted these
        await self.store.log_submission(cache_entries=cache_entries)

        self.total_entries_processed += len(cache_entries)
        self.loop_logger.info(
            f"[Total: {self.total_entries_processed:,} entries] "
            f"Submitted {len(cache_entries):,} enhancements."
        )
