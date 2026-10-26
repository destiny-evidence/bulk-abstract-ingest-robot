"""
Mock database for the store — the `records` table from docs/docs/data-model.md.

`MockDatabase` holds plain `Record` rows (seedable at any stage of the submission flowchart)
and lets tests schedule a crash on the next read or write to simulate the process or the
database connection dying mid-flow. `MockStore` mirrors the parts of `app.db.store.AbstractStore`
that the runners use — including the `DISTINCT ON (destiny_id)` deduplication and the
`ensure_overlap` guard — so tests can track the flow's tracking flags without a live Postgres.
"""

from collections.abc import Sequence
from typing import Any
from uuid import UUID, uuid6

from sqlalchemy.exc import OperationalError

from app.db.schemas import Format, ParsedRecord, ParsedRecordExtra, Record

from .fake_data import DEFAULT_ABSTRACT, DEFAULT_TITLE, NEW_ABSTRACT


class MockDatabase:
    """Stand-in for the Postgres `records` table."""

    def __init__(self) -> None:
        self.records: list[Record] = []
        # Exceptions to raise (once) from the next store read/write, simulating a mid-flow crash
        self.crash_on_next_read: Exception | None = None
        self.crash_on_next_write: Exception | None = None

    def add_record(self, *, abstract: str | None = DEFAULT_ABSTRACT, title: str = DEFAULT_TITLE, publication_year: int = 2020, **flags: object) -> Record:
        """Insert a `scopus_api` record; keyword arguments set the identifier and tracking columns."""
        raw: dict[str, Any] = {"dc:title": title, "prism:coverDate": f"{publication_year}-06-01"}
        if abstract is not None:
            raw["dc:description"] = abstract
        record = Record(format=Format.SCOPUS_API, raw=raw, **flags)
        self.records.append(record)
        return record

    def consume_crash(self, kind: str) -> Exception | None:
        """Pop a scheduled crash for a "read" or "write" (one-shot, like a real transient failure)."""
        exc = getattr(self, f"crash_on_next_{kind}")
        setattr(self, f"crash_on_next_{kind}", None)
        return exc


class MockStore:
    """Duck-typed replacement for `AbstractStore`, backed by `MockDatabase` instead of Postgres."""

    def __init__(self, db: MockDatabase) -> None:
        self.db = db
        self.calls: list[str] = []  # store methods invoked, for "did the runner touch the database at all?" assertions

    async def get_parsed_extra_entries(self, destiny_ids: set[UUID], ensure_overlap: bool = False) -> Sequence[ParsedRecordExtra]:
        """Get parsed entries with raw payload for DESTinY IDs and optionally check the sets overlap."""
        self.calls.append("get_parsed_extra_entries")
        if (crash := self.db.consume_crash("read")) is not None:
            raise crash

        found: dict[UUID, Record] = {}  # DISTINCT ON (destiny_id): one row per DESTinY ID, first one wins
        for record in self.db.records:
            if record.destiny_id is not None and record.destiny_id in destiny_ids:
                found.setdefault(record.destiny_id, record)

        if ensure_overlap and {record.destiny_id for record in found.values()} != set(destiny_ids):
            raise RuntimeError("Did not find submittable record for all requested IDs!")
        return [ParsedRecordExtra.from_record(record) for record in found.values()]

    async def log_submission(self, cache_entries: Sequence[ParsedRecord]) -> None:
        """Log submission to repository by setting the `enhancement_submitted` flag on the matching rows."""
        self.calls.append("log_submission")
        if (crash := self.db.consume_crash("write")) is not None:
            raise crash

        submitted = {entry.record_id for entry in cache_entries if entry.record_id is not None}
        for record in self.db.records:
            if record.record_id in submitted:
                record.enhancement_submitted = True


def requested_record(db: MockDatabase, **overrides) -> Record:
    """Seed a record at the `enhancement_requested` stage — the flowchart state the enhance runner expects."""
    return db.add_record(
        **{
            "destiny_id": uuid6(),
            "abstract": NEW_ABSTRACT,
            "evaluated": True,
            "found_destiny_reference": True,
            "abstract_enhancement_required": True,
            "enhancement_requested": True,
            **overrides,
        },
    )


def connection_died() -> OperationalError:
    """Lost database connection, as SQLAlchemy surfaces it."""
    return OperationalError("SELECT records.destiny_id FROM records", {}, ConnectionResetError(104, "Connection reset by peer"))
