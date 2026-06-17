"""Utility class to interact with metadata cache database."""

from typing import TYPE_CHECKING

import sqlalchemy as sa
from uuid import UUID
from .db_engine import get_engine
from .types import Record

if TYPE_CHECKING:
    import logging

    from .config import Settings

publication_year = r"""
coalesce(
    (raw -> 'meta' -> 'openalex' ->> 'publication_year')::INT,
    (raw -> 'meta' -> 'openalex-api' ->> 'publication_year')::INT,
    (raw ->> 'publication_year')::INT,
    (raw ->> 'year')::INT,
    (raw -> 'static_data' -> 'summary' -> 'pub_info' ->> 'pubyear')::INT,
    SUBSTRING(raw ->> 'prism:coverDate' FROM '\d{4}')::INT,
    SUBSTRING(raw ->> 'prism:coverDisplayDate' FROM '\d{4}')::INT,
    (raw -> 'PubmedData' -> 0 -> 'History' -> 0 -> 'PubMedPubDate' -> 0 -> 'Year' -> 0 ->> '_text')::INT,
    (raw -> 'MedlineCitation' -> 0 -> 'Article' -> 0 -> 'Journal' -> 0 -> 'JournalIssue' -> 0 -> 'PubDate' -> 0 -> 'Year' -> 0 ->> '_text')::INT
) AS publication_year
"""

class AbstractStore:
    """Utility class to interact with metadata cache database."""

    def __init__(
        self,
        settings: Settings,
        logger: logging.Logger,
        debug_db: bool = False,
    ) -> None:
        """Initialise the store."""
        self.settings = settings
        self.logger = logger
        self.db = get_engine(debug=debug_db, settings=settings)

    async def get_next_request_batch(self) -> list[Record]:
        """Get entries that we might want to check next in the repository."""
        self.logger.debug(f"Querying cache DB for batch of at most {self.settings.request_batch_size:,} entries to check in the repository")
        async with self.db.session() as session:
            stmt = sa.text(
                "SELECT record_id, "
                "       openalex_id,"
                "       doi,"
                "       pubmed_id,"
                "       abstract,"
                f"      {publication_year} "
                "FROM request "
                "WHERE length(coalesce(abstract, '')) > :min_length AND "
                "      processed IS NOT TRUE "
                "LIMIT :batch_size;",
            )
            batch = await session.execute(
                stmt,
                {
                    "min_length": self.settings.min_abstract_length,
                    "batch_size": self.settings.request_batch_size,
                },
            )

            return [Record.from_cache_tuple(row) for row in batch]

    async def get_entries(self, destiny_ids: set[UUID], ensure_overlap: bool = False) -> list[Record]:
        """Get entries for DESTinY IDs and optionally check the sets overlap."""
        self.logger.debug(f"Querying cache DB for {len(destiny_ids):,} DESTinY repository IDs...")
        async with self.db.session() as session:
            stmt = sa.text(
                "SELECT record_id,"
                "       doi,"
                "       openalex_id,"
                "       destiny_id,"
                "       pubmed_id,"
                "       abstract,"
                f"      {publication_year} "
                "FROM request "
                "WHERE destiny_id = ANY(:destiny_ids) AND" \
                "      submitted IS NOT TRUE" \
            )
            batch = await session.execute(stmt, {"destiny_ids": list(destiny_ids)})
            records = [Record.from_cache_destiny_tuple(row) for row in batch]
            if ensure_overlap and {record.destiny_id for record in records} != destiny_ids:
                raise RuntimeError("Did not find submittable record for all requested IDs!")
            return records
        
    async def log_submission(self, cache_entries: list[Record]) -> None:
        """Log submission to repository."""
        async with self.db.session() as session:
            stmt = sa.text("UPDATE request SET submitted = TRUE WHERE record_id = ANY(:record_ids);")
            await session.execute(stmt, {"record_ids": [entry.record_id for entry in cache_entries if entry.record_id is not None]})
            await session.commit()


    async def persist_match_results(
        self,
        cache_entries: list[Record],
        matched_cache_entries: list[Record],
        filtered_references: list[tuple[Record, Record]],
        requested_cache_entries: list[Record],
    ) -> None:
        """
        Write all provenance information to the database in a single transaction.

        This prevents information loss if the process is interrupted
        and allows us to resume from where we left off without reprocessing the same entries.


        Updates the following database columns:

            - processed: Marks entries that have been processed in this batch.
            - exists_in_destiny: Marks entries that were matched to a DESTinY ID in the repository.
            - abstract_enhancement_required: Marks entries that require an abstract enhancement to be submitted.
            - destiny_id: Updates the DESTinY ID for matched entries.
            - requested: Marks entries for which enhancement requests were successfully submitted to the DESTINY repository.

        Args:
            cache_entries (list[Record]): All cache items considered as part of this batch.
            matched_cache_entries (list[Record]): Cache items that were matched to a DESTinY ID in the repository.
            filtered_references (list[tuple[Record, Record]]): Cache items that passed the enhancement criteria.
            requested_cache_entries (list[Record]): Cache items for which enhancement requests were
                submitted to the DESTINY repository.
        """

        processed_ids = sorted({entry.record_id for entry in cache_entries if entry.record_id is not None})
        exists_ids = sorted({entry.record_id for entry in matched_cache_entries if entry.record_id is not None})
        requested_ids = sorted({entry.record_id for entry in requested_cache_entries if entry.record_id is not None})
        abstract_enhancement_required_ids = sorted({cache_entry.record_id for cache_entry, _ in filtered_references if cache_entry.record_id is not None})
        matched_values = [
            {
                "record_id": cache_entry.record_id,
                "destiny_id": reference.destiny_id,
            }
            for cache_entry, reference in filtered_references
            if cache_entry.record_id is not None and reference.destiny_id is not None
        ]
        matched_values.sort(key=lambda row: row["record_id"])

        all_touched_ids = sorted(
            set(processed_ids)
            | set(exists_ids)
            | set(requested_ids)
            | set(abstract_enhancement_required_ids)
            | {row["record_id"] for row in matched_values}
        )

        async with self.db.session() as session:
            if all_touched_ids:
                self.logger.debug("Acquiring row locks in deterministic order to avoid deadlocks between workers")
                await session.execute(
                    sa.text(
                        "SELECT record_id "
                        "FROM request "
                        "WHERE record_id = ANY(:record_ids) "
                        "ORDER BY record_id "
                        "FOR UPDATE;"
                    ),
                    {"record_ids": all_touched_ids},
                )

            if processed_ids:
                await session.execute(
                    sa.text("UPDATE request SET processed = TRUE WHERE record_id = ANY(:record_ids);"),
                    {"record_ids": processed_ids},
                )

            if exists_ids:
                await session.execute(
                    sa.text("UPDATE request SET exists_in_destiny = TRUE WHERE record_id = ANY(:record_ids);"),
                    {"record_ids": exists_ids},
                )

            if abstract_enhancement_required_ids:
                await session.execute(
                    sa.text("UPDATE request SET abstract_enhancement_required = TRUE WHERE record_id = ANY(:record_ids);"),
                    {"record_ids": abstract_enhancement_required_ids},
                )

            if matched_values:
                await session.execute(
                    sa.text("UPDATE request SET destiny_id = :destiny_id WHERE record_id = :record_id;"),
                    matched_values,
                )

            if requested_ids:
                await session.execute(
                    sa.text("UPDATE request SET requested = TRUE WHERE record_id = ANY(:record_ids);"),
                    {"record_ids": requested_ids},
                )
    
            await session.commit()
