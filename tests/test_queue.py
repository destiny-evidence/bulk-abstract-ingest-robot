"""
Tests for the ingestion queue runner (`app/robots/queue.py`: transfer a raw JSONL export into the `records` queue).

Azure is mocked at the SDK boundary (`app.robots.queue.BlobClient` → `tests/util/fake_blob.py`): the blob serves its
bytes in blocks of arbitrary size, hands out empty blocks between them, and can have its connection die mid-stream.
The `queue_runner` fixture (tests/util/fixtures.py) wires that blob, the tracking double from `tests/util/fake_database.py`
and the settings into the runner — tests hand `queue_blob` its bytes, tweak `queue_ingest`/`db`/`settings` as they
like, and then run one `_loop_task()`; the whole `search_ingest` state machine (queueing → checkpoints → queued) is read
back from `queue_tracker.trace` and committed rows from `db.records`. Payloads are either fixed-width stub rows
(for exact byte/row checkpoint arithmetic) or a slice of a genuine Scopus API export (tests/util/data/scopus_api_sample.jsonl).
"""

import asyncio
import json
from itertools import accumulate

import pytest
from azure.core.exceptions import ServiceResponseError
from pydantic import ValidationError
from sqlalchemy.exc import OperationalError

from app.db.schemas import Format
from app.robots import queue as queue_module
from app.robots.queue import IngestionQueueRunner

from .util.fake_blob import FakeBlobStorage, fake_blob_client
from .util.fake_data import (
    MULTILINE_ABSTRACT,
    SAS_URL,
    STUB_LINE_BYTES,
    scopus_record,
    scopus_sample_jsonl,
    scopus_sample_lines,
    stub,
    stub_jsonl,
    stub_line,
)
from .util.fake_database import MockStore


@pytest.fixture
def read_blob(monkeypatch):
    """Serve bytes from a fake blob and read them through the real `_read_from_blob_storage` line reader."""

    def read(data: bytes, *, offset: int = 0, **options) -> tuple[list[tuple[str, int]], FakeBlobStorage]:
        storage = FakeBlobStorage(data, **options)
        monkeypatch.setattr(queue_module, "BlobClient", fake_blob_client(storage))

        async def collect() -> list[tuple[str, int]]:
            return [entry async for entry in queue_module._read_from_blob_storage(sas_url=SAS_URL, byte_offset=offset)]

        return asyncio.run(collect()), storage

    return read


class TestBlobReader:
    def test_yields_each_line_with_its_ending_byte_offset(self, read_blob):
        lines, storage = read_blob(stub_jsonl(count=3), block_size=4096)

        assert [json.loads(text) for text, _ in lines] == [stub(1), stub(2), stub(3)]
        assert [offset for _, offset in lines] == [STUB_LINE_BYTES * i for i in (1, 2, 3)]  # newlines counted, trailing newline not a fourth line
        assert storage.requests == [0]  # one download, from the head of the blob
        assert storage.urls == [SAS_URL]

    @pytest.mark.parametrize("block_size", [1, 7, 997], ids=["byte by byte", "tiny blocks", "odd sized blocks"])
    def test_reassembles_lines_no_matter_where_the_blocks_split_them(self, read_blob, block_size):
        # a real record (with multi-byte characters) plus stubs: blocks cut through strings, escapes, codepoints and newlines alike
        data = (json.dumps(scopus_record(1), ensure_ascii=False) + "\n").encode() + stub_jsonl(count=2)
        lines, _ = read_blob(data, block_size=block_size)

        assert [text for text, _ in lines] == [line.decode("utf-8") for line in data.splitlines()]
        assert lines[-1][1] == len(data)  # offsets are exact, so no line is ever double-counted or skipped on restart

    def test_finds_line_boundaries_in_a_full_sample_stream(self, read_blob):
        data = scopus_sample_jsonl(20)  # 20 real Scopus records of 1.5-14 KB each, served in 1 KB blocks

        lines, _ = read_blob(data, block_size=1024)

        assert [json.loads(text) for text, _ in lines] == [json.loads(line) for line in scopus_sample_lines()]
        assert [offset for _, offset in lines][-1] == len(data)

    def test_strips_crlf_line_endings_but_counts_them_in_bytes(self, read_blob):
        lines, _ = read_blob(b'{"a": 1}\r\n{"b": 2}\r\n', block_size=1)  # forces block boundaries between \r and \n

        assert lines == [('{"a": 1}', 10), ('{"b": 2}', 20)]  # the \r\n pair is stripped from the text but counted in the offset

    def test_ignores_empty_blocks_between_data(self, read_blob):
        data = stub_jsonl(count=2)

        lines, storage = read_blob(data, block_size=len(data), empty_blocks=True)

        assert [json.loads(text) for text, _ in lines] == [stub(1), stub(2)]
        assert [offset for _, offset in lines] == [STUB_LINE_BYTES, len(data)]
        assert storage.requests == [0]

    def test_empty_blob_produces_no_lines(self, read_blob):
        lines, storage = read_blob(b"", empty_blocks=True)  # an empty result file still arrives as an empty block

        assert lines == []
        assert storage.requests == [0]

    def test_resumes_from_a_byte_offset_without_touching_earlier_bytes(self, read_blob):
        data = scopus_sample_jsonl(20)
        full, _ = read_blob(data, block_size=2048)

        resume_at = full[3][1]  # the first four lines were transferred by a previous process
        tail, storage = read_blob(data, offset=resume_at, block_size=2048)

        assert tail == full[4:]
        assert storage.requests == [resume_at]

    def test_line_breaks_inside_json_strings_are_not_line_breaks(self, read_blob):
        mangled = scopus_record(0)
        mangled["dc:description"] = MULTILINE_ABSTRACT  # the abstract itself spans several lines, so its JSON escapes them as \n and \r\n
        untouched = scopus_record(1)
        data = ("\n".join(json.dumps(obj, ensure_ascii=False) for obj in (mangled, untouched)) + "\n").encode()

        lines, _ = read_blob(data, block_size=3)  # blocks split inside escape sequences (between \ and n) and inside multi-byte characters

        assert len(lines) == 2  # two physical lines, no matter how many line breaks live inside the strings
        assert [json.loads(text) for text, _ in lines] == [mangled, untouched]
        assert all("\n" not in text and "\r" not in text for text, _ in lines)
        assert lines[-1][1] == len(data)


class TestTransfer:
    def test_transfers_all_rows_and_walks_the_states_from_queueing_to_queued(self, queue_runner, queue_blob, db, queue_ingest, queue_tracker, settings):
        queue_blob.data = stub_jsonl(count=9)
        settings.api_wrapper_checkpoint_interval = 3

        asyncio.run(queue_runner._loop_task())

        assert [record.raw for record in db.records] == [stub(i) for i in range(1, 10)]
        assert all(record.format is Format.SCOPUS_API for record in db.records)
        assert all(
            getattr(record, column) is None
            for record in db.records
            for column in ("destiny_id", "openalex_id", "doi", "wos_id", "scopus_id", "s2_id", "pubmed_id", "dimensions_id")
        )
        assert queue_runner.total_entries_processed == 9
        assert queue_blob.requests == [0]
        assert queue_blob.urls == [SAS_URL]
        assert queue_tracker.trace == [
            ("queueing", True, False, None, None),  # lock acquired and request marked queueing before any transfer
            ("checkpoint", True, False, 3 * STUB_LINE_BYTES, 3),  # progress while still queueing (never queued before the end)
            ("checkpoint", True, False, 6 * STUB_LINE_BYTES, 6),
            ("checkpoint", True, False, 9 * STUB_LINE_BYTES, 9),
            ("queued", True, True, 9 * STUB_LINE_BYTES, 9),  # only now marked queued
        ]
        assert (queue_ingest.is_queueing, queue_ingest.is_queued) == (True, True)

    def test_transfers_a_real_scopus_export_and_logs_checkpoints_by_bytes(self, queue_runner, queue_blob, db, queue_ingest, queue_tracker, settings):
        queue_blob.data = scopus_sample_jsonl(20)
        ends = list(accumulate(len(line) + 1 for line in scopus_sample_lines()))
        settings.api_wrapper_checkpoint_interval = 10

        asyncio.run(queue_runner._loop_task())

        assert [record.raw for record in db.records] == [json.loads(line) for line in scopus_sample_lines()]
        assert [event for event, *_ in queue_tracker.trace] == ["queueing", "checkpoint", "checkpoint", "queued"]
        assert [cp[3] for cp in queue_tracker.trace if cp[0] == "checkpoint"] == [ends[9], ends[19]]  # byte-exact across varying line lengths
        assert (queue_ingest.checkpoint_bytes, queue_ingest.checkpoint_rows) == (len(queue_blob.data), 20)

    def test_logs_checkpoints_at_the_interval_with_cumulative_row_counts(self, queue_runner, queue_blob, db, queue_ingest, queue_tracker, settings):
        # an earlier process transferred lines 1-3 and logged a checkpoint at 100 rows in total
        queue_ingest.is_queueing = True
        queue_ingest.checkpoint_bytes = 3 * STUB_LINE_BYTES
        queue_ingest.checkpoint_rows = 100
        queue_blob.data = stub_jsonl(count=9)
        settings.api_wrapper_checkpoint_interval = 3

        asyncio.run(queue_runner._loop_task())

        assert queue_blob.requests == [3 * STUB_LINE_BYTES]  # resumed exactly where the checkpoint stopped, not at the head
        assert [record.raw for record in db.records] == [stub(i) for i in range(4, 10)]
        assert queue_tracker.trace == [
            ("queueing", True, False, 3 * STUB_LINE_BYTES, 100),
            ("checkpoint", True, False, 6 * STUB_LINE_BYTES, 103),  # this run's row count added to the checkpointed total
            ("checkpoint", True, False, 9 * STUB_LINE_BYTES, 106),
            ("queued", True, True, 9 * STUB_LINE_BYTES, 106),
        ]

    def test_empty_blob_is_queued_without_checkpoints(self, queue_runner, queue_blob, db, queue_tracker, settings):
        queue_blob.empty_blocks = True
        settings.api_wrapper_checkpoint_interval = 3

        asyncio.run(queue_runner._loop_task())

        assert db.records == []
        assert db.commits == 0
        assert queue_tracker.trace == [("queueing", True, False, None, None), ("queued", True, True, None, None)]


class TestFormatProblems:
    def test_unknown_format_stops_the_transfer_at_the_poison_line(self, queue_runner, queue_blob, db, queue_ingest, queue_tracker, settings):
        # lines 1-2 are fine, line 3 is an HTML error page someone uploaded instead of the export, lines 4-6 are fine
        queue_blob.data = stub_jsonl(count=2) + b"<html><body>403 Forbidden</body></html>\n" + stub_jsonl(first=4, count=3)
        settings.api_wrapper_checkpoint_interval = 2

        with pytest.raises(json.JSONDecodeError):
            asyncio.run(queue_runner._loop_task())

        assert [record.raw for record in db.records] == [stub(1), stub(2)]  # only the checkpointed prefix survived
        assert queue_tracker.trace == [("queueing", True, False, None, None), ("checkpoint", True, False, 2 * STUB_LINE_BYTES, 2)]
        assert (queue_ingest.is_queueing, queue_ingest.is_queued) == (True, False)  # left queueing for a retry ...

        with pytest.raises(json.JSONDecodeError):  # ... and the retry hits the same poison line at the same place
            asyncio.run(queue_runner._loop_task())
        assert queue_blob.requests == [0, 2 * STUB_LINE_BYTES]
        assert db.commits == 1
        assert len(db.records) == 2

    def test_valid_json_objects_are_queued_verbatim_whatever_their_shape(self, queue_runner, queue_blob, db, queue_ingest, settings):
        # queue.py does not interpret the payload: a Crossref-shaped object among the Scopus lines is stored raw and
        # stamped with the hard-coded `Format.SCOPUS_API` (the TO-DO in _loop_task) — recognising formats is the parser's job.
        crossref = {"DOI": "10.1234/abcd.5678", "type": "posted-content", "message": {"abstract": "A Crossref-shaped payload, not Scopus."}}
        queue_blob.data = stub_line(1) + (json.dumps(crossref) + "\n").encode() + stub_line(2)
        settings.api_wrapper_checkpoint_interval = 3

        asyncio.run(queue_runner._loop_task())

        assert [record.raw for record in db.records] == [stub(1), crossref, stub(2)]
        assert all(record.format is Format.SCOPUS_API for record in db.records)
        assert queue_ingest.is_queued is True

    @pytest.mark.xfail(
        reason="queue.py feeds json.loads() output straight into Record.raw (typed dict[str, Any]) without a shape check, so a whole-file JSON array enters the queue as one poisoned raw 'record'",
    )
    def test_json_that_is_not_a_line_of_objects_is_rejected(self, queue_runner, queue_blob, settings):
        # the classic upload mistake: a raw API response (one JSON array) instead of JSON lines
        queue_blob.data = (json.dumps([stub(1), stub(2)]) + "\n").encode()
        settings.api_wrapper_checkpoint_interval = 1

        with pytest.raises(ValidationError):
            asyncio.run(queue_runner._loop_task())


class TestProcessDiesMidFlight:
    def test_blob_stream_death_keeps_progress_at_the_last_checkpoint(self, queue_runner, queue_blob, db, queue_ingest, queue_tracker, settings):
        # the connection dies in the middle of block 2 — the first block (lines 1-2) was already committed and checkpointed
        queue_blob.data = stub_jsonl(count=6)
        queue_blob.block_size = 2 * STUB_LINE_BYTES
        queue_blob.fail_after_blocks = 1
        settings.api_wrapper_checkpoint_interval = 2

        with pytest.raises(ServiceResponseError):
            asyncio.run(queue_runner._loop_task())

        assert [record.raw for record in db.records] == [stub(1), stub(2)]
        assert queue_tracker.trace == [("queueing", True, False, None, None), ("checkpoint", True, False, 2 * STUB_LINE_BYTES, 2)]
        assert (queue_ingest.is_queueing, queue_ingest.is_queued) == (True, False)  # never marked queued — the request stays up for grabs again
        assert queue_runner.total_entries_processed == 2

    def test_database_death_mid_commit_rolls_back_to_the_last_checkpoint(self, queue_runner, queue_blob, db, queue_ingest, queue_tracker, settings):
        queue_blob.data = stub_jsonl(count=6)
        settings.api_wrapper_checkpoint_interval = 2
        db.crash_on_commit = 2  # the second batch's commit dies, taking its rows and its flushed checkpoint write with it

        with pytest.raises(OperationalError):
            asyncio.run(queue_runner._loop_task())

        assert [record.raw for record in db.records] == [stub(1), stub(2)]
        assert queue_tracker.trace == [("queueing", True, False, None, None), ("checkpoint", True, False, 2 * STUB_LINE_BYTES, 2)]
        assert queue_ingest.is_queued is False
        assert db.sessions[0].closed is True  # the session carrying lines 3-4 was closed with them uncommitted

    def test_restart_from_checkpoint_finishes_without_duplicates(
        self,
        queue_runner,
        queue_blob,
        db,
        queue_ingest,
        queue_tracker,
        runner_factory,
        settings,
    ):
        queue_blob.data = stub_jsonl(count=6)
        settings.api_wrapper_checkpoint_interval = 3
        db.crash_on_commit = 2

        with pytest.raises(OperationalError):
            asyncio.run(queue_runner._loop_task())

        assert queue_tracker.trace == [
            ("queueing", True, False, None, None),
            # lines 1-3 survived; lines 4-6 and their flushed checkpoint died with the uncommitted session
            ("checkpoint", True, False, 3 * STUB_LINE_BYTES, 3),
        ]

        queue_runner = runner_factory(IngestionQueueRunner, store=MockStore(db))  # the crashed process is replaced by a fresh one
        asyncio.run(queue_runner._loop_task())

        assert queue_blob.requests == [0, 3 * STUB_LINE_BYTES]  # the second attempt downloads from the checkpoint, not from the head
        assert [record.raw["eid"] for record in db.records] == [stub(i)["eid"] for i in range(1, 7)]  # every line exactly once
        assert queue_runner.total_entries_processed == 3  # the new process only saw the remainder of the file
        assert queue_tracker.trace[2:] == [
            ("queueing", True, False, 3 * STUB_LINE_BYTES, 3),
            ("checkpoint", True, False, 6 * STUB_LINE_BYTES, 6),
            ("queued", True, True, 6 * STUB_LINE_BYTES, 6),
        ]
        assert (queue_ingest.is_queueing, queue_ingest.is_queued) == (True, True)

    def test_trailing_partial_batch_is_committed_and_checkpointed(self, queue_runner, queue_blob, db, queue_ingest, queue_tracker, settings):
        queue_blob.data = stub_jsonl(count=5)  # lines 1-4 fall on checkpoint intervals, line 5 is the trailing partial batch
        settings.api_wrapper_checkpoint_interval = 2

        asyncio.run(queue_runner._loop_task())

        assert queue_ingest.is_queued is True
        assert [record.raw for record in db.records] == [stub(i) for i in range(1, 6)]  # the short last batch lands too
        assert db.commits == 3  # one commit per batch, the trailing one included
        assert queue_tracker.trace[-2:] == [
            ("checkpoint", True, False, 5 * STUB_LINE_BYTES, 5),  # checkpointed and committed together ...
            ("queued", True, True, 5 * STUB_LINE_BYTES, 5),  # ... so the queued flag cannot outrun the queue contents
        ]
