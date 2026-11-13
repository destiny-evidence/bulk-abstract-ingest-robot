"""
Mock database for the store — the `records` and `m2m_record_ods` tables from docs/docs/data-model.md.

`MockDatabase` holds plain `Record` rows (seedable at any stage of the submission flowchart) plus the
`RecordMatch` rows linking them to DESTinY references, and lets tests schedule a crash on the next read or
write to simulate the process or the database connection dying mid-flow. `MockStore` mirrors the parts of
`app.db.store.AbstractStore` that the runners use — including the "one record per DESTinY ID" pick and the
`ensure_overlap` guard — so tests can track the flow's tracking flags without a live Postgres. Because the
per-reference flags live on the match row, `db.add_record(destiny_id=...)` also creates the match, and the
`match_of` helper reads the flags back.

`MockIngestTracker` mirrors the `IngestTracker` state machine from `app/db/ingest.py` without
Postgres or file locks: handing out a request marks it `is_queueing`, checkpoints are only
accepted while queueing and not yet queued, `mark_queued` refuses to run before queueing —
and every transition appends a `(event, is_queueing, is_queued, checkpoint_bytes, checkpoint_rows)`
snapshot to `tracker.trace`, so tests can read the whole transfer back. Like the real tracker,
`log_checkpoint` accepts the runner's `session` and a `Persist` mode: the checkpoint update then
joins that session's transaction and only materialises (and shows up in the trace) when the
session commits — so a commit crash rolls the checkpoint back together with the rows of its batch.

`MockSession`/`MockDatabase.session()` stand in for `ExtendedAsyncSession`/`DatabaseEngine.session`:
added rows and deferred tracker writes only become visible once the session commits (and die with
the session if the process goes down before that), `flush_or_commit` mirrors the engine's
`Persist`-switch, and `crash_on_commit` kills the n-th commit to simulate the database dying
mid-checkpoint.
"""

from collections.abc import AsyncIterator, Callable, Sequence
from contextlib import asynccontextmanager
from typing import Any
from uuid import UUID, uuid6

from sqlalchemy.exc import OperationalError

from app.db.engine import Persist
from app.db.schemas import Format, ParsedRecord, ParsedRecordExtra, Record, RecordMatch, SearchIngest

from .fake_data import DEFAULT_ABSTRACT, DEFAULT_TITLE, NEW_ABSTRACT


class MockDatabase:
    """Stand-in for the Postgres `records` and `m2m_record_ods` tables."""

    def __init__(self, *, crash_on_commit: int | None = None, commit_error: Exception | None = None) -> None:
        self.records: list[Record] = []
        self.matches: list[RecordMatch] = []
        self.commits = 0
        # Exceptions to raise (once) from the next store read/write, simulating a mid-flow crash
        self.crash_on_next_read: Exception | None = None
        self.crash_on_next_write: Exception | None = None
        self.crash_on_commit = crash_on_commit  # fail the n-th commit of this database (1-based)
        self.commit_error = commit_error or connection_died()
        self.sessions: list[MockSession] = []

    def add_record(
        self,
        *,
        abstract: str | None = DEFAULT_ABSTRACT,
        title: str = DEFAULT_TITLE,
        publication_year: int = 2020,
        destiny_id: UUID | None = None,
        **flags: object,
    ) -> Record:
        """
        Insert a `scopus_api` record; keyword arguments set the identifier and tracking columns.

        A `destiny_id` also writes the `m2m_record_ods` row that links the record to that reference, which is
        where the per-reference flags (`abstract_enhancement_required` and friends) are kept.
        """
        raw: dict[str, Any] = {"dc:title": title, "prism:coverDate": f"{publication_year}-06-01"}
        if abstract is not None:
            raw["dc:description"] = abstract
        record_flags = {flag: value for flag, value in flags.items() if flag in Record.model_fields}
        match_flags = {flag: value for flag, value in flags.items() if flag in RecordMatch.model_fields}
        record = Record(format=Format.SCOPUS_API, raw=raw, **record_flags)
        self.records.append(record)
        if destiny_id is not None:
            self.add_match(record, destiny_id=destiny_id, **match_flags)
        return record

    def add_match(self, record: Record, *, destiny_id: UUID, **flags: object) -> RecordMatch:
        """Record that this cached record matched one DESTinY reference (many records to many references)."""
        match = RecordMatch(record_id=record.record_id, destiny_id=destiny_id, **flags)
        self.matches.append(match)
        return match

    def match_of(self, record: Record) -> RecordMatch | None:
        """The match row of a record (asserting there is exactly one), for reading back the per-reference flags."""
        matches = [match for match in self.matches if match.record_id == record.record_id]
        assert len(matches) <= 1, f"Record {record.record_id} has {len(matches)} matches, use `db.matches` to inspect them"
        return matches[0] if matches else None

    def consume_crash(self, kind: str) -> Exception | None:
        """Pop a scheduled crash for a "read" or "write" (one-shot, like a real transient failure)."""
        exc = getattr(self, f"crash_on_next_{kind}")
        setattr(self, f"crash_on_next_{kind}", None)
        return exc

    @asynccontextmanager
    async def session(self, existing_session: MockSession | None = None) -> AsyncIterator[MockSession]:
        if existing_session is not None:
            # Re-use the outer session; the parent owns the lifecycle (no close, no rollback here)
            yield existing_session
            return
        session = MockSession(self)
        self.sessions.append(session)
        try:
            yield session
        finally:
            session.closed = True  # uncommitted rows and deferred tracker writes die with the session, like a rollback


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

        by_id = {record.record_id: record for record in self.db.records}
        found: dict[UUID, tuple[Record, UUID]] = {}  # one record per DESTinY ID, the first match wins
        for match in self.db.matches:
            if match.destiny_id in destiny_ids and match.record_id in by_id:
                found.setdefault(match.destiny_id, (by_id[match.record_id], match.destiny_id))

        if ensure_overlap and set(found) != set(destiny_ids):
            raise RuntimeError("Did not find submittable record for all requested IDs!")
        return [ParsedRecordExtra.from_record(record=record, destiny_id=destiny_id) for record, destiny_id in found.values()]

    async def log_submission(self, cache_entries: Sequence[ParsedRecord]) -> None:
        """Log submission to repository by setting `enhancement_submitted` on the matching record/reference pairs."""
        self.calls.append("log_submission")
        if (crash := self.db.consume_crash("write")) is not None:
            raise crash

        submitted = {(entry.record_id, entry.destiny_id) for entry in cache_entries if entry.record_id is not None and entry.destiny_id is not None}
        for match in self.db.matches:
            if (match.record_id, match.destiny_id) in submitted:
                match.enhancement_submitted = True


def requested_record(db: MockDatabase, *, destiny_id: UUID | None = None, **overrides) -> Record:
    """Seed a record at the `enhancement_requested` stage — the flowchart state the enhance runner expects."""
    return db.add_record(
        **{
            "destiny_id": destiny_id if destiny_id is not None else uuid6(),
            "abstract": NEW_ABSTRACT,
            "evaluated": True,
            "abstract_enhancement_required": True,
            "enhancement_requested": True,
            **overrides,
        },
    )


def connection_died() -> OperationalError:
    """Lost database connection, as SQLAlchemy surfaces it."""
    return OperationalError("SELECT m2m_record_ods.destiny_id FROM m2m_record_ods", {}, ConnectionResetError(104, "Connection reset by peer"))


class MockIngestTracker:
    """Duck-typed `IngestTracker` for a single transfer request."""

    def __init__(self, ingest: SearchIngest) -> None:
        self.ingest = ingest
        self.trace: list[tuple[str, bool, bool, int | None, int | None]] = []

    def _snapshot(self, event: str) -> None:
        self.trace.append((event, self.ingest.is_queueing, self.ingest.is_queued, self.ingest.checkpoint_bytes, self.ingest.checkpoint_rows))

    def _check_ingest(self, ingest_id: str | UUID) -> None:
        if str(ingest_id) != str(self.ingest.ingest_id):
            raise AssertionError(f"Tracker holds ingest {self.ingest.ingest_id}, not {ingest_id}")

    @asynccontextmanager
    async def exclusive_transfer_request(self) -> AsyncIterator[SearchIngest]:
        # the real tracker acquires the file lock and marks the request queueing before handing out
        # a detached row — later checkpoint/queued writes go to the database through a separate session
        # and never update the runner's copy, so neither does this one
        self.ingest.is_queueing = True
        self.ingest.is_queued = False
        self._snapshot("queueing")
        yield self.ingest.model_copy()

    async def log_checkpoint(
        self,
        ingest_id: str | UUID,
        checkpoint_bytes: int,
        checkpoint_rows: int,
        session: MockSession | None = None,
        commit: Persist = Persist.COMMIT,
    ) -> SearchIngest:
        self._check_ingest(ingest_id)
        if not self.ingest.is_queueing or self.ingest.is_queued:
            raise AssertionError(f"Trying to write checkpoint for unqueued for fully queued ingest_id={ingest_id}")

        def write() -> None:
            # the checkpointed state appears exactly when the row update becomes durable — commit or no trace entry
            self.ingest.checkpoint_bytes = checkpoint_bytes
            self.ingest.checkpoint_rows = checkpoint_rows
            self._snapshot("checkpoint")

        if session is None:
            # the real tracker opens a session of its own; a FLUSH on that would roll back at close, only a COMMIT lands
            if commit is Persist.COMMIT:
                write()
            return self.ingest

        session.deferred_writes.append(write)  # joins the runner's transaction (flushed along with the batch's records)
        await session.flush_or_commit(commit)
        return self.ingest

    async def mark_queued(self, ingest_id: str | UUID) -> SearchIngest:
        self._check_ingest(ingest_id)
        if not self.ingest.is_queueing:
            raise AssertionError(f"Trying to mark queued before queueing ingest_id={ingest_id}")

        self.ingest.is_queued = True
        self._snapshot("queued")
        return self.ingest


class MockSession:
    """Stand-in for `ExtendedAsyncSession` — one transaction collecting the queue runner's `Record` rows and tracker updates."""

    def __init__(self, db: MockDatabase) -> None:
        self.db = db
        self.pending: list[Record] = []
        self.deferred_writes: list[Callable[[], None]] = []  # other statements in this transaction (e.g. the checkpointed tracker row)
        self.closed = False

    def add(self, record: Record) -> None:
        self.pending.append(record)

    async def flush(self) -> None:
        pass  # flushed rows are only visible inside the transaction — this double materialises everything at commit

    async def flush_or_commit(self, persist: Persist) -> None:
        if persist is Persist.COMMIT:
            await self.commit()
        else:
            await self.flush()

    async def commit(self) -> None:
        self.db.commits += 1
        if self.db.crash_on_commit == self.db.commits:
            self.db.crash_on_commit = None  # one-shot, like a real transient failure
            raise self.db.commit_error  # rows and deferred tracker writes stay unapplied, as they would after a rollback
        for write in self.deferred_writes:
            write()
        self.db.records.extend(self.pending)
        self.pending.clear()
        self.deferred_writes.clear()
