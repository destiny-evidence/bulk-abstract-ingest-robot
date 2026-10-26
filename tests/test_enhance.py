"""
Tests for the enhancement (submission) enhance_runner.

The repository API (see https://api.evidence-repository.org/openapi.json) is mocked at the
`Repository` boundary: `get_next_batch` (POST /v1/robot-enhancement-batches/ + GET of the JSONL
at `reference_storage_url`) and `submit_enhancements` (PUT of the enhancement JSONL to
`result_storage_url` + POST /v1/robot-enhancement-batches/{batch_id}/results/), so no HTTP
traffic is generated here. The store runs against the in-memory mock database from
tests/fakes.py, so every test follows the submission flowchart (docs/docs/data-model.md:44)
through the tracking flags on the `records` table: requested -> submitted.
The sample texts come from tests/samples.py.
"""

import asyncio
from uuid import uuid4

import httpx
import pytest
from destiny_sdk.references import Reference
from destiny_sdk.robots import RobotEnhancementBatch
from sqlalchemy.exc import OperationalError

from app.db.schemas import ParsedRecordExtra
from tests.util.fake_data import DIFFERENT_ABSTRACT, NEW_ABSTRACT
from tests.util.fake_database import connection_died, requested_record

BATCH_ID = uuid4()
DESTINY_A = uuid4()
DESTINY_B = uuid4()
ABSTRACT_A = NEW_ABSTRACT
ABSTRACT_B = DIFFERENT_ABSTRACT


def http_error(code: int, method: str, url: str) -> httpx.HTTPStatusError:
    """Error raised by `raise_for_status()`, as the mocked API calls would produce it."""
    request = httpx.Request(method, url)
    return httpx.HTTPStatusError(f"HTTP {code} from {method} {url}", request=request, response=httpx.Response(code, request=request))


def offer_batch(enhance_runner, *references: Reference) -> RobotEnhancementBatch:
    """Tell the mocked repository API to hand out a batch for these references on the next poll."""
    batch_info = RobotEnhancementBatch(
        id=BATCH_ID,
        reference_storage_url=f"https://destiny-blobs.example.com/refs/{BATCH_ID}.jsonl?sv=lease",  # type: ignore[arg-type]
        result_storage_url=f"https://destiny-blobs.example.com/results/{BATCH_ID}.jsonl?sv=lease",  # type: ignore[arg-type]
    )
    enhance_runner.repository.get_next_batch.return_value = (batch_info, list(references))
    return batch_info


class TestSubmissionFlow:
    def test_submits_requested_records_and_marks_submitted(self, enhance_runner, db):
        record_a = requested_record(db, destiny_id=DESTINY_A, abstract=ABSTRACT_A)
        record_b = requested_record(db, destiny_id=DESTINY_B, abstract=ABSTRACT_B)
        batch_info = offer_batch(enhance_runner, Reference(id=DESTINY_A), Reference(id=DESTINY_B))

        asyncio.run(enhance_runner._loop_task())

        call = enhance_runner.repository.submit_enhancements.await_args
        assert call.kwargs["batch_info"] is batch_info  # the batch we just leased, not a stale one
        entries = call.kwargs["cache_entries"]
        assert {entry.destiny_id for entry in entries} == {DESTINY_A, DESTINY_B}
        assert {entry.abstract for entry in entries} == {ABSTRACT_A, ABSTRACT_B}
        # the raw source payload travels along for the RawEnhancement
        assert all(isinstance(entry, ParsedRecordExtra) and entry.raw["dc:description"] == entry.abstract for entry in entries)
        # ... and the flow moves one step on: requested -> submitted (for exactly these two rows)
        assert (record_a.enhancement_submitted, record_b.enhancement_submitted) == (True, True)
        assert enhance_runner.total_entries_processed == 2

    def test_deduplicates_records_matching_the_same_reference(self, enhance_runner, db):
        first = requested_record(db, destiny_id=DESTINY_A, abstract=ABSTRACT_A)
        duplicate = requested_record(db, destiny_id=DESTINY_A, abstract=ABSTRACT_B)
        offer_batch(enhance_runner, Reference(id=DESTINY_A))

        asyncio.run(enhance_runner._loop_task())

        entries = enhance_runner.repository.submit_enhancements.await_args.kwargs["cache_entries"]
        assert [entry.record_id for entry in entries] == [first.record_id]  # one row per DESTinY ID
        assert (first.enhancement_submitted, duplicate.enhancement_submitted) == (True, False)  # only what was sent is marked
        assert enhance_runner.total_entries_processed == 1

    @pytest.mark.parametrize(
        "batch",
        [
            (None, None),
            (
                RobotEnhancementBatch(
                    id=BATCH_ID,
                    reference_storage_url="https://destiny-blobs.example.com/refs.jsonl",  # type: ignore[arg-type]
                    result_storage_url="https://destiny-blobs.example.com/results.jsonl",  # type: ignore[arg-type]
                ),
                None,
            ),
        ],
        ids=["empty batch (HTTP 204)", "empty reference file"],
    )
    def test_returns_without_touching_the_database_when_nothing_is_offered(self, enhance_runner, db, batch):
        requested_record(db, destiny_id=DESTINY_A)
        enhance_runner.repository.get_next_batch.return_value = batch

        asyncio.run(enhance_runner._loop_task())

        enhance_runner.repository.submit_enhancements.assert_not_awaited()
        assert enhance_runner.store.calls == []  # nothing read, nothing marked; the requested record waits for a later loop

    def test_records_at_earlier_flow_stages_are_never_submitted(self, enhance_runner, db):
        requested = requested_record(db, destiny_id=DESTINY_A)
        no_match = db.add_record(evaluated=True)  # evaluated, but no DESTinY ID found
        rules_rejected = db.add_record(destiny_id=uuid4(), evaluated=True, found_destiny_reference=True)  # matched, but no enhancement wanted
        not_eligible = db.add_record(has_eligible_abstract=False)
        offer_batch(enhance_runner, Reference(id=DESTINY_A))  # the repository only asks for the requested one

        asyncio.run(enhance_runner._loop_task())

        entries = enhance_runner.repository.submit_enhancements.await_args.kwargs["cache_entries"]
        assert [entry.destiny_id for entry in entries] == [DESTINY_A]
        assert (requested.enhancement_submitted, no_match.enhancement_submitted, rules_rejected.enhancement_submitted, not_eligible.enhancement_submitted) == (
            True,
            False,
            False,
            False,
        )

    def test_fails_without_submitting_when_a_reference_has_no_matching_record(self, enhance_runner, db):
        pending = requested_record(db, destiny_id=DESTINY_A)
        # the batch also asks for an ID the store cannot cover (never requested, or purged by housekeeping)
        offer_batch(enhance_runner, Reference(id=DESTINY_A), Reference(id=uuid4()))

        with pytest.raises(RuntimeError, match="Did not find submittable record for all requested IDs!"):
            asyncio.run(enhance_runner._loop_task())

        enhance_runner.repository.submit_enhancements.assert_not_awaited()
        assert pending.enhancement_submitted is False  # the whole batch is left untouched for a retry, rather than submitted half-covered


class TestCrashAndApiFailure:
    @pytest.mark.parametrize(
        "exc",
        [
            http_error(500, "POST", "https://api.staging.evidence-repository.org/v1/robot-enhancement-batches/"),
            httpx.ConnectError("connection refused"),
            httpx.ReadTimeout("poll timed out"),
        ],
        ids=["server error", "connection refused", "timeout"],
    )
    def test_api_error_while_polling_leaves_everything_untouched(self, enhance_runner, db, exc):
        pending = requested_record(db, destiny_id=DESTINY_A)
        enhance_runner.repository.get_next_batch.side_effect = exc

        with pytest.raises(type(exc)):
            asyncio.run(enhance_runner._loop_task())

        assert enhance_runner.store.calls == []
        enhance_runner.repository.submit_enhancements.assert_not_awaited()
        assert pending.enhancement_submitted is False

    def test_database_crash_while_fetching_aborts_the_batch(self, enhance_runner, db):
        pending = requested_record(db, destiny_id=DESTINY_A)
        offer_batch(enhance_runner, Reference(id=DESTINY_A))
        db.crash_on_next_read = connection_died()

        with pytest.raises(OperationalError):
            asyncio.run(enhance_runner._loop_task())

        enhance_runner.repository.submit_enhancements.assert_not_awaited()
        assert pending.enhancement_submitted is False

    @pytest.mark.parametrize(
        "exc",
        [
            http_error(503, "PUT", "https://destiny-blobs.example.com/results/outcome.jsonl"),
            http_error(502, "POST", "https://api.staging.evidence-repository.org/v1/robot-enhancement-batches/outcome/results/"),
            httpx.ReadTimeout("upload timed out"),
        ],
        ids=["blob upload rejected", "finalise rejected", "timeout"],
    )
    def test_api_error_during_submission_leaves_records_unsubmitted(self, enhance_runner, db, exc):
        pending = requested_record(db, destiny_id=DESTINY_A)
        offer_batch(enhance_runner, Reference(id=DESTINY_A))
        enhance_runner.repository.submit_enhancements.side_effect = exc

        with pytest.raises(type(exc)):
            asyncio.run(enhance_runner._loop_task())

        assert enhance_runner.store.calls == ["get_parsed_extra_entries"]  # we never reach the logging step
        assert pending.enhancement_submitted is False  # the lease expires and the repository re-offers the batch

    def test_crash_after_submission_is_resumed_by_resubmitting(self, enhance_runner, db):
        pending = requested_record(db, destiny_id=DESTINY_A)
        offer_batch(enhance_runner, Reference(id=DESTINY_A))
        db.crash_on_next_write = connection_died()  # the process dies after the repository accepted the enhancements, but before we logged them

        with pytest.raises(OperationalError):
            asyncio.run(enhance_runner._loop_task())

        assert enhance_runner.repository.submit_enhancements.await_count == 1
        assert pending.enhancement_submitted is False  # nothing marked, nothing counted
        assert enhance_runner.total_entries_processed == 0

        offer_batch(enhance_runner, Reference(id=DESTINY_A))  # the lease expired and the batch is re-offered
        asyncio.run(enhance_runner._loop_task())

        assert enhance_runner.repository.submit_enhancements.await_count == 2  # at-least-once: the repository may see the enhancement twice
        assert pending.enhancement_submitted is True  # ... but the flow finally completes to `submitted`
        assert enhance_runner.total_entries_processed == 1
