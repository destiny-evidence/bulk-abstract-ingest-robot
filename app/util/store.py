"""Utility class to interact with metadata cache database."""

from typing import TYPE_CHECKING

import sqlalchemy as sa

from .db_engine import get_engine
from .types import Record

if TYPE_CHECKING:
    import logging
    import uuid

    from .config import Settings


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
                "       publication_year "
                "FROM request "
                "WHERE length(coalesce(abstract, '')) > :min_length AND "
                "      requested = FALSE AND "
                "      submitted = FALSE AND "
                "      destiny_id IS NULL "
                "LIMIT :batch_size;",
            )
            batch = await session.execute(
                stmt,
                {
                    "min_length": self.settings.min_abstract_length,
                    "batch_size": self.settings.request_batch_size,
                },
            )
            return [Record.model_validate(row) for row in batch]

    async def get_entries(self, destiny_ids: set[uuid.UUID], ensure_overlap: bool = False) -> list[Record]:
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
                "       publication_year "
                "FROM request "
                "WHERE destiny_id = ANY(:destiny_ids);",
            )
            batch = await session.execute(stmt, {"destiny_ids": list(destiny_ids)})
            records = [Record.model_validate(row) for row in batch]
            if ensure_overlap and {record.destiny_id for record in records} != destiny_ids:
                raise RuntimeError("Did not find submittable record for all requested IDs!")
            return records

    async def log_request(self, cache_entries: list[Record]) -> None:
        """Log lookup in repository."""
        async with self.db.session() as session:
            stmt = sa.text("UPDATE request SET requested = TRUE WHERE record_id = ANY(:record_ids);")
            await session.execute(
                stmt,
                {"record_ids": [entry.record_id for entry in cache_entries if entry.record_id is not None]},
            )
            await session.commit()

    async def log_submission(self, destiny_ids: set[uuid.UUID]) -> None:
        """Log submission to repository."""
        async with self.db.session() as session:
            stmt = sa.text("UPDATE request SET submitted = TRUE WHERE destiny_id = ANY(:destiny_ids);")
            await session.execute(stmt, {"destiny_ids": destiny_ids})
            await session.commit()

    async def write_matches(self, matched_references: list[tuple[Record, Record]]) -> None:
        """Write matched references to cache database."""
        async with self.db.session() as session:
            stmt = sa.text("UPDATE request SET destiny_id = :destiny_id WHERE record_id = :record_id;")
            for cache_entry, reference in matched_references:
                await session.execute(
                    stmt,
                    {
                        "destiny_id": reference.destiny_id,
                        "record_id": cache_entry.record_id,
                    },
                )
            await session.commit()
