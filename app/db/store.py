"""Utility class to interact with metadata cache database."""

import logging
from collections.abc import Sequence
from uuid import UUID

from sqlalchemy import update
from sqlmodel import col, select

from ..util.config import Settings
from .engine import DatabaseEngine
from .schemas import ODSRecord, ParsedRecord, ParsedRecordExtra, Record


class AbstractStore:
    """Utility class to interact with metadata cache database."""

    def __init__(
        self,
        settings: Settings,
        logger: logging.Logger,
    ) -> None:
        """Initialise the store."""
        self.settings = settings
        self.logger = logger
        self.db = DatabaseEngine(settings=settings)

    async def get_next_request_batch(self) -> Sequence[ParsedRecordExtra]:
        """Get entries that we might want to check next in the repository."""
        self.logger.debug(f"Querying cache DB for batch of at most {self.settings.request_batch_size:,} entries to check in the repository")
        parsed_records = []
        async with self.db.session() as session:
            batch = (
                await session.exec(
                    select(Record)
                    .where(
                        col(Record.evaluated).isnot(other=True),
                        col(Record.has_eligible_abstract).isnot(other=False),
                    )
                    .limit(self.settings.request_batch_size),
                )
            ).all()

            for record in batch:
                parsed_record = ParsedRecordExtra.from_record(record)
                if len(parsed_record.abstract or "") < self.settings.min_abstract_length:
                    parsed_record.has_eligible_abstract = False
                else:
                    parsed_records.append(parsed_record)
            await session.commit()
        return parsed_records

    async def get_entries(self, destiny_ids: set[UUID], ensure_overlap: bool = False) -> Sequence[Record]:
        """Get entries for DESTinY IDs and optionally check the sets overlap."""
        self.logger.debug(f"Querying cache DB for {len(destiny_ids):,} DESTinY repository IDs...")
        async with self.db.session() as session:
            results = (
                await session.exec(
                    select(Record).distinct(col(Record.destiny_id)).where(col(Record.destiny_id).in_(destiny_ids)),
                )
            ).all()

            if ensure_overlap and {record.destiny_id for record in results} != destiny_ids:
                raise RuntimeError("Did not find submittable record for all requested IDs!")
            session.expunge_all()  # detach ORM
            return results

    async def get_parsed_entries(self, destiny_ids: set[UUID], ensure_overlap: bool = False) -> Sequence[ParsedRecord]:
        """Get parsed entries for DESTinY IDs and optionally check the sets overlap."""
        return [ParsedRecord.from_record(record) for record in await self.get_entries(destiny_ids=destiny_ids, ensure_overlap=ensure_overlap)]

    async def get_parsed_extra_entries(self, destiny_ids: set[UUID], ensure_overlap: bool = False) -> Sequence[ParsedRecordExtra]:
        """Get parsed entries with raw payload for DESTinY IDs and optionally check the sets overlap."""
        return [ParsedRecordExtra.from_record(record) for record in await self.get_entries(destiny_ids=destiny_ids, ensure_overlap=ensure_overlap)]

    async def log_submission(self, cache_entries: Sequence[ParsedRecord]) -> None:
        """Log submission to repository."""
        async with self.db.session() as session:
            await session.exec(
                update(Record)
                .where(col(Record.record_id).in_([entry.record_id for entry in cache_entries if entry.record_id is not None]))
                .values(enhancement_submitted=True),
            )
            await session.commit()

    async def persist_match_results(
        self,
        cache_entries: Sequence[ParsedRecord],
        matched_cache_entries: Sequence[ParsedRecord],
        filtered_references: Sequence[tuple[ParsedRecord, ODSRecord]],
        requested_cache_entries: Sequence[ParsedRecord],
    ) -> None:
        """
        Write all provenance information to the database in a single transaction.

        This prevents information loss if the process is interrupted
        and allows us to resume from where we left off without reprocessing the same entries.

        Updates the following database columns:

            - evaluated: Marks records that have been evaluated against the destiny repository in this batch.
            - found_destiny_reference: Marks entries that were matched to a DESTinY ID in the repository.
            - abstract_enhancement_required: Marks entries that require an abstract enhancement to be submitted.
            - destiny_id: Updates the DESTinY ID for matched entries.
            - enhancement_requested: Marks entries for which enhancement requests were successfully sent to the DESTINY repository.

        Args:
            cache_entries (list[Record]): All cache items considered as part of this batch.
            matched_cache_entries (list[Record]): Cache items that were matched to a DESTinY ID in the repository.
            filtered_references (list[tuple[Record, Record]]): Cache items that passed the enhancement criteria.
            requested_cache_entries (list[Record]): Cache items for which enhancement requests were
                sent to the DESTINY repository.

        """
        evaluated_ids = sorted({entry.record_id for entry in cache_entries if entry.record_id is not None})
        found_destiny_reference_ids = sorted({entry.record_id for entry in matched_cache_entries if entry.record_id is not None})
        requested_ids = sorted({entry.record_id for entry in requested_cache_entries if entry.record_id is not None})
        abstract_enhancement_required_ids = sorted({cache_entry.record_id for cache_entry, _ in filtered_references if cache_entry.record_id is not None})
        matched_values = [
            {
                "record_id": cache_entry.record_id,
                "destiny_id": reference.destiny_id,
            }
            for cache_entry, reference in filtered_references
            if reference.destiny_id is not None
        ]
        matched_values.sort(key=lambda row: row["record_id"])

        all_touched_ids = sorted(
            set(evaluated_ids)
            | set(found_destiny_reference_ids)
            | set(requested_ids)
            | set(abstract_enhancement_required_ids)
            | {row["record_id"] for row in matched_values},
        )

        async with self.db.session() as session:
            if all_touched_ids:
                self.logger.debug("Acquiring row locks in deterministic order to avoid deadlocks between workers")
                await session.exec(
                    select(Record.record_id)
                    .where(col(Record.record_id).in_(all_touched_ids))
                    .order_by(col(Record.record_id))
                    .with_for_update(),  # Get row-level lock https://www.postgresql.org/docs/18/explicit-locking.html#LOCKING-ROWS
                )

            if evaluated_ids:
                await session.exec(
                    update(Record).where(col(Record.record_id).in_(evaluated_ids)).values(evaluated=True),
                )

            if found_destiny_reference_ids:
                await session.exec(
                    update(Record).where(col(Record.record_id).in_(found_destiny_reference_ids)).values(found_destiny_reference=True),
                )

            if abstract_enhancement_required_ids:
                await session.exec(
                    update(Record).where(col(Record.record_id).in_(abstract_enhancement_required_ids)).values(abstract_enhancement_required=True),
                )

            if matched_values:
                await session.execute(update(Record), matched_values)

            if requested_ids:
                await session.exec(
                    update(Record).where(col(Record.record_id).in_(requested_ids)).values(enhancement_requested=True),
                )
            await session.commit()
