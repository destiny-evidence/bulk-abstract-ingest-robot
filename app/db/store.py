"""Utility class to interact with metadata cache database."""

import logging
from collections.abc import Sequence
from uuid import UUID

from pydantic import ValidationError
from sqlalchemy import update
from sqlmodel import col, select

from ..util.config import Settings
from .engine import DatabaseEngine
from .schemas import ODSRecord, ParsedRecord, ParsedRecordExtra, Record, RecordMatch


def _matched_pairs(references: Sequence[tuple[ParsedRecord, ODSRecord]]) -> set[tuple[UUID, UUID]]:
    """The (record, DESTinY reference) pairs of a match result, complete with both IDs only."""
    return {(entry.record_id, reference.destiny_id) for entry, reference in references if entry.record_id is not None and reference.destiny_id is not None}


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
        """
        Get entries that we might want to check next in the repository.

        Records that are already evaluated or ruled out via `has_eligible_abstract` are skipped,
        and so are records parked with `encountered_parsing_error` - otherwise an unparseable record
        would be picked up again on every iteration (it never gets `evaluated`) and, once enough of
        them fill a batch, starve the runner of work it could actually process.
        Resetting that flag is what makes a record eligible again after a parser fix.
        """
        self.logger.debug(f"Querying cache DB for batch of at most {self.settings.request_batch_size:,} entries to check in the repository")
        parsed_records = []
        async with self.db.session() as session:
            batch = (
                await session.exec(
                    select(Record)
                    .where(
                        col(Record.evaluated).isnot(other=True),
                        col(Record.has_eligible_abstract).isnot(other=False),
                        col(Record.encountered_parsing_error).isnot(other=True),
                    )
                    .limit(self.settings.request_batch_size),
                )
            ).all()

            for record in batch:
                try:
                    parsed_record = ParsedRecordExtra.from_record(record)
                    if not (self.settings.min_abstract_length <= len(parsed_record.abstract or "") < self.settings.max_abstract_length):
                        record.has_eligible_abstract = False
                    else:
                        parsed_records.append(parsed_record)
                except ValidationError as exc:
                    record.encountered_parsing_error = True
                    self.logger.warning(f"Failed record validation: {exc.json(include_url=False)}")
                except (AssertionError, NotImplementedError, ValueError) as exc:
                    record.encountered_parsing_error = True
                    self.logger.warning(f"Failed record parsing: {exc}")

            await session.commit()
        return parsed_records

    async def get_entries(self, destiny_ids: set[UUID], ensure_overlap: bool = False) -> Sequence[tuple[Record, UUID]]:
        """
        Get one cached record per DESTinY ID (the reference it was matched to) and optionally check the sets overlap.

        Several records can have matched the same reference, so the read-back picks one of them; which one is not
        arbitrary but simply the first match of that reference, ordered by `record_id`.
        """
        self.logger.debug(f"Querying cache DB for {len(destiny_ids):,} DESTinY repository IDs...")
        async with self.db.session() as session:
            matches = (
                await session.exec(
                    select(Record, RecordMatch.destiny_id)
                    .join(RecordMatch, col(RecordMatch.record_id) == col(Record.record_id))
                    .where(col(RecordMatch.destiny_id).in_(destiny_ids))
                    .order_by(col(RecordMatch.destiny_id), col(Record.record_id))
                )
            ).all()

            entries: dict[UUID, tuple[Record, UUID]] = {}  # one record per DESTinY ID, the first match wins
            for record, destiny_id in matches:
                entries.setdefault(destiny_id, (record, destiny_id))

            if ensure_overlap and set(entries) != destiny_ids:
                raise RuntimeError("Did not find submittable record for all requested IDs!")
            session.expunge_all()  # detach ORM
            return list(entries.values())

    async def get_parsed_entries(self, destiny_ids: set[UUID], ensure_overlap: bool = False) -> Sequence[ParsedRecord]:
        """Get parsed entries for DESTinY IDs and optionally check the sets overlap."""
        return [
            ParsedRecord.from_record(record=record, destiny_id=destiny_id)
            for record, destiny_id in await self.get_entries(destiny_ids=destiny_ids, ensure_overlap=ensure_overlap)
        ]

    async def get_parsed_extra_entries(self, destiny_ids: set[UUID], ensure_overlap: bool = False) -> Sequence[ParsedRecordExtra]:
        """Get parsed entries with raw payload for DESTinY IDs and optionally check the sets overlap."""
        return [
            ParsedRecordExtra.from_record(record=record, destiny_id=destiny_id)
            for record, destiny_id in await self.get_entries(destiny_ids=destiny_ids, ensure_overlap=ensure_overlap)
        ]

    async def log_submission(self, cache_entries: Sequence[ParsedRecord]) -> None:
        """Log submission to repository by marking the (record, DESTinY reference) pairs that were actually sent."""
        submitted = [
            {"record_id": entry.record_id, "destiny_id": entry.destiny_id, "enhancement_submitted": True}
            for entry in cache_entries
            if entry.record_id is not None and entry.destiny_id is not None
        ]
        async with self.db.session() as session:
            if submitted:
                await session.exec(update(RecordMatch), params=submitted)
            await session.commit()

    async def persist_match_results(
        self,
        cache_entries: Sequence[ParsedRecord],
        matched_references: Sequence[tuple[ParsedRecord, ODSRecord]],
        filtered_references: Sequence[tuple[ParsedRecord, ODSRecord]],
        requested_references: Sequence[tuple[ParsedRecord, ODSRecord]],
    ) -> None:
        """
        Write all provenance information to the database in a single transaction.

        This prevents information loss if the process is interrupted
        and allows us to resume from where we left off without reprocessing the same entries.

        Updates the following database columns:

            - records.evaluated: Marks records that have been evaluated against the destiny repository in this batch.
            - m2m_record_ods: Adds one row per (record, reference) pair the repository returned; the row itself
                marks that a reference was found for that record, and it carries the per-reference flags below.
            - m2m_record_ods.abstract_enhancement_required: Marks matches that require an abstract enhancement
                to be submitted.
            - m2m_record_ods.enhancement_requested: Marks matches for which enhancement requests were successfully
                sent to the DESTINy repository.

        :param cache_entries: All cache items considered as part of this batch.
        :param matched_references: Cache items and the repository references they matched;
                one cache item can feature several times, and so can one reference.
        :param filtered_references: Cache item/reference pairs that passed the enhancement criteria.
        :param requested_references: Cache item/reference pairs for which enhancement requests
                were sent to the DESTINY repository.

        """
        evaluated_ids = sorted({entry.record_id for entry in cache_entries if entry.record_id is not None})
        matched_pairs = sorted(_matched_pairs(matched_references))
        required_pairs = sorted(_matched_pairs(filtered_references))
        requested_pairs = sorted(_matched_pairs(requested_references))

        all_touched_ids = sorted(
            set(evaluated_ids) | {record_id for pairs in (matched_pairs, required_pairs, requested_pairs) for record_id, _ in pairs},
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

            if matched_pairs:
                # Re-evaluating a record (e.g. after a parser fix) must not clash with the matches it already has
                known_pairs = {
                    (record_id, destiny_id)
                    for record_id, destiny_id in (
                        await session.exec(
                            select(RecordMatch.record_id, RecordMatch.destiny_id).where(col(RecordMatch.record_id).in_(all_touched_ids)),
                        )
                    ).all()
                }
                new_pairs = [(record_id, destiny_id) for record_id, destiny_id in matched_pairs if (record_id, destiny_id) not in known_pairs]
                session.add_all([RecordMatch(record_id=record_id, destiny_id=destiny_id) for record_id, destiny_id in new_pairs])
                await session.flush()  # the flags below address these rows by key, so they have to be in the database first

            if required_pairs:
                await session.exec(
                    update(RecordMatch),
                    params=[
                        {"record_id": record_id, "destiny_id": destiny_id, "abstract_enhancement_required": True} for record_id, destiny_id in required_pairs
                    ],
                )

            if requested_pairs:
                await session.exec(
                    update(RecordMatch),
                    params=[{"record_id": record_id, "destiny_id": destiny_id, "enhancement_requested": True} for record_id, destiny_id in requested_pairs],
                )

            await session.commit()
