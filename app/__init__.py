"""Robot's main entry point and loop."""

import asyncio
import contextlib
import signal
import sys
from typing import TYPE_CHECKING

from .config import get_logger, get_settings
from .repository import Repository
from .store import AbstractStore

if TYPE_CHECKING:
    from types import FrameType

    from .types import Record


async def main(db_debug: bool = False, loglevel: str | int = "INFO") -> None:  # noqa: PLR0915
    """Robot's core working method."""
    settings = get_settings()
    logger = get_logger("abstracts-robot", init_logging=True, base_level=loglevel)

    repository = Repository(settings=settings, logger=logger.getChild("repository"))
    store = AbstractStore(
        settings=settings,
        logger=logger.getChild("store"),
        debug_db=db_debug,
    )

    def should_enhance(cache_entry: Record, reference: Record) -> bool:
        """Submittable iff following rules hold true: (abstract is missing in repository or not matching known abstract) and publication year +/-N."""
        if cache_entry.abstract is None or len(cache_entry.abstract) < settings.min_abstract_length:
            # Empty and short abstracts are not good //  We should not end up here, this check is just in case...
            return False

        if reference.destiny_id is None:
            # Missing DESTinY repository ID //  We should not end up here, this check is just in case...
            return False

        if (
            cache_entry.publication_year is not None
            and reference.publication_years is not None
            and all(abs(cache_entry.publication_year - py) > settings.publication_year_tolerance for py in reference.publication_years)
        ):
            # None of the known publication years is within the tolerance
            return False

        if cache_entry.abstract is not None and reference.abstracts is not None:
            for abstract in reference.abstracts:
                if cache_entry.abstract == abstract:
                    # No need to submit this abstract as an enhancement, we have it already in the repository
                    return False

        # All checks passed, we can submit this abstract enhancement to the repository
        return True

    async def main_loop() -> None:
        loop_logger = logger.getChild("loop")
        while True:
            try:
                # Sleep for graceful API use
                await asyncio.sleep(settings.poll_interval_seconds)

                # Get eligible entries from meta-cache
                cache_entries = await store.get_next_request_batch()

                # Query repository for meta-cache entries
                matched_references = await repository.query_repository(cache_entries=cache_entries)

                # Remember we queried those
                await store.log_request(cache_entries=cache_entries)

                # Filter results
                matched_references = [
                    (cache_entry, reference) for cache_entry, reference in matched_references if should_enhance(cache_entry=cache_entry, reference=reference)
                ]

                # Remember DESTinY IDs
                await store.write_matches(matched_references)

                # Request to enhance references
                repository.request_to_enhance(
                    destiny_ids=[reference.destiny_id for _cache_entry, reference in matched_references if reference.destiny_id is not None],
                )

                # Poll for approved requests for enhancements
                batch_info, references = await repository.get_next_batch()

                if batch_info is None or references is None:
                    loop_logger.debug("No batches available")
                    continue

                # Get matching data from store (could be different to what we just asked to enhance)
                cache_entries = await store.get_entries(
                    destiny_ids={reference.id for reference in references},
                    ensure_overlap=True,
                )

                # Submit to repository
                await repository.submit_enhancements(batch_info=batch_info, cache_entries=cache_entries)

                # Remember we submitted these
                await store.log_submission(destiny_ids={reference.id for reference in references})

            except Exception as e:
                loop_logger.error(f"Encountered an error: {e}")
                loop_logger.exception(e)

    logger.info(
        f"Initialising main loop for {settings.robot_name} with a {settings.poll_interval_seconds}s polling interval "
        f"and batch sizes {settings.request_batch_size:,} (poll) and {settings.fulfil_batch_size} (fulfil)",
    )

    shutdown_event = asyncio.Event()

    def shutdown_handler(signum: int, _frame: FrameType | None) -> None:
        logger.info(f"Received signal {signum}, initiating graceful shutdown...")
        shutdown_event.set()

    # Register signal handlers for graceful shutdown
    signal.signal(signal.SIGINT, shutdown_handler)
    signal.signal(signal.SIGTERM, shutdown_handler)

    try:
        main_loop_task = asyncio.create_task(main_loop())
        shutdown_task = asyncio.create_task(shutdown_event.wait())

        # Wait for either the polling task to complete or shutdown signal
        _done, pending = await asyncio.wait(
            [main_loop_task, shutdown_task],
            return_when=asyncio.FIRST_COMPLETED,
        )

        # Cancel remaining tasks
        for task in pending:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

        logger.info("Shutdown complete")

    except KeyboardInterrupt:
        logger.info("Received keyboard interrupt, shutting down...")
        sys.exit(0)
    except Exception as e:
        logger.error(f"Fatal error occurred: {e}")
        logger.exception(e)
        sys.exit(1)
