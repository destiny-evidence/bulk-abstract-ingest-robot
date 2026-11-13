"""
Tests for the store (`app/db/store.py`): which queued records the match runner is offered next, the verdicts it
writes back onto them (`has_eligible_abstract`, `encountered_parsing_error`), and what it leaves behind after
matching - one `m2m_record_ods` row per matched DESTinY reference, with the per-reference flags on it.

These rules are production SQL and production parsing, so the `MockStore`-based tests cannot cover them — a mock that
mirrored the query would mirror its bugs too. The `store` fixture therefore runs the real `AbstractStore` against a
throwaway SQLite file (tests/util/sqlite_database.py). The payloads are tampered variants of a genuine Scopus API
export (tests/util/data/scopus_api_sample.jsonl), the shape of the rows a queued batch actually holds: samples 0-4 and 9
carry 600-2,000-character abstracts (and real DOIs where noted), while sample 18 came through the export without an abstract.
"""

import asyncio
from collections.abc import Sequence
from uuid import UUID

import pytest

from app.db.schemas import Format, ODSRecord, ParsedRecordExtra, Record, RecordMatch, Source
from tests.util.fake_data import DESTINY_ID, OTHER_ID, ods_reference, scopus_record, scopus_row
from tests.util.sqlite_database import fetch_matches, fetch_rows, seed


def run_batches(store, *rows: Record, passes: int = 1):
    """Seed the rows, take `passes` request batches from the store, then read the `records` table back."""

    async def flow() -> tuple[list, list]:
        await seed(store, *rows)
        batches = [list(await store.get_next_request_batch()) for _ in range(passes)]
        return batches, await fetch_rows(store)

    return asyncio.run(flow())


def run_match_stage(
    store,
    rows: Sequence[Record],
    *,
    matched: Sequence[tuple[Record, UUID]] = (),
    filtered: Sequence[tuple[Record, UUID]] = (),
    requested: Sequence[tuple[Record, UUID]] = (),
    submitted: Sequence[tuple[Record, UUID]] = (),
    twice: bool = False,
) -> tuple[list[Record], list[RecordMatch]]:
    """
    Seed the rows, hand the store the outcome of one match batch, and read the `records` and `m2m_record_ods` tables back.

    The pair arguments are lists of `(row, DESTinY ID)`: `matched` what the repository returned, `filtered` the pairs that
    passed the enhancement criteria, `requested` those we actually asked about, and `submitted` the pairs the enhance
    runner later reports back. `twice` replays the same result, as a re-evaluation of the batch would.
    """

    async def flow() -> tuple[list[Record], list[RecordMatch]]:
        await seed(store, *rows)
        entries = {row.record_id: ParsedRecordExtra.from_record(record=row) for row in rows}

        def as_cache_pairs(pairs: Sequence[tuple[Record, UUID]]) -> list[tuple[ParsedRecordExtra, ODSRecord]]:
            return [(entries[row.record_id], ods_reference(destiny_id=destiny_id)) for row, destiny_id in pairs]

        results = {
            "matched_references": as_cache_pairs(matched),
            "filtered_references": as_cache_pairs(filtered),
            "requested_references": as_cache_pairs(requested),
        }
        for _ in range(2 if twice else 1):
            await store.persist_match_results(cache_entries=list(entries.values()), **results)

        if submitted:
            await store.log_submission([entries[row.record_id].model_copy(update={"destiny_id": destiny_id}) for row, destiny_id in submitted])

        return await fetch_rows(store), await fetch_matches(store)

    return asyncio.run(flow())


def match_flags(matches: Sequence[RecordMatch]) -> dict[tuple[UUID, UUID], tuple[bool, bool, bool]]:
    """The per-reference flags of every stored match, keyed by (record, DESTinY reference)."""
    return {
        (match.record_id, match.destiny_id): (match.abstract_enhancement_required, match.enhancement_requested, match.enhancement_submitted)
        for match in matches
    }


class TestAbstractEligibility:
    def test_offers_fully_parsed_records_with_long_abstracts(self, store):
        batches, rows = run_batches(store, scopus_row(0), scopus_row(9))

        assert [entry.scopus_id for entry in batches[0]] == [scopus_record(0)["eid"], scopus_record(9)["eid"]]
        entry, raw = batches[0][0], scopus_record(0)
        assert (entry.title, entry.abstract, entry.publication_year, entry.doi) == (raw["dc:title"], raw["dc:description"], 2011, raw["prism:doi"])
        assert entry.source is Source.SCOPUS
        assert entry.raw == raw  # the raw payload travels along with the parsed values for the enhancement step
        assert all(row.has_eligible_abstract is None and row.encountered_parsing_error is False for row in rows)  # offered, but nothing ruled out

    def test_rules_out_abstracts_below_the_minimum_and_skips_them_thereafter(self, store, settings):
        too_short = scopus_record(1)
        too_short["dc:description"] = too_short["dc:description"][: settings.min_abstract_length - 1]
        at_minimum = scopus_record(2)
        at_minimum["dc:description"] = at_minimum["dc:description"][: settings.min_abstract_length]

        batches, rows = run_batches(store, scopus_row(0), scopus_row(raw=too_short), scopus_row(raw=at_minimum), scopus_row(18), passes=2)

        assert [entry.scopus_id for entry in batches[0]] == [
            scopus_record(0)["eid"],
            at_minimum["eid"],
        ]  # the exact minimum still counts; sample 18 lost its abstract in the export itself
        ruled_out = {row.raw.get("eid"): row for row in rows}
        assert ruled_out[too_short["eid"]].has_eligible_abstract is False  # the verdict lands on the row ...
        assert ruled_out[scopus_record(18)["eid"]].has_eligible_abstract is False  # ... and on the one that never had an abstract ...
        assert ruled_out[at_minimum["eid"]].has_eligible_abstract is None  # ... but not on the eligible ones, which await matching
        assert [entry.scopus_id for entry in batches[1]] == [
            scopus_record(0)["eid"],
            at_minimum["eid"],
        ]  # the eligible rows reappear (only matching marks `evaluated`) - the ruled-out ones never do


class TestParsingErrors:
    @pytest.mark.parametrize(
        "broken",
        [
            pytest.param(lambda: scopus_row(raw={}), id="no raw payload"),  # AssertionError: the queue stored an empty row
            pytest.param(lambda: scopus_row(0, scopus_id="2-s2.0-11111111111"), id="Scopus ID disagrees with payload"),  # AssertionError
            pytest.param(lambda: scopus_row(0, doi="10.1007/s00382-011-0116-3"), id="DOI disagrees with payload"),  # AssertionError
            pytest.param(  # ValidationError: a DOI only ever seen as a URL, outside the expected shape
                lambda: scopus_row(0, raw={**scopus_record(0), "prism:doi": "https://doi.org/10.1371/journal.pone.0029657"}),
                id="DOI outside its shape",
            ),
        ],
    )
    def test_parks_unparseable_record_without_losing_the_rest_of_the_batch(self, store, broken):
        before, poisoned, after = scopus_row(1), broken(), scopus_row(9)

        batches, rows = run_batches(store, before, poisoned, after)

        assert [entry.scopus_id for entry in batches[0]] == [scopus_record(1)["eid"], scopus_record(9)["eid"]]  # the poison record does not take the batch down
        by_id = {row.record_id: row for row in rows}
        assert by_id[poisoned.record_id].encountered_parsing_error is True  # parked ...
        assert by_id[poisoned.record_id].has_eligible_abstract is None  # ... as broken input, not as an ineligible abstract
        assert by_id[before.record_id].encountered_parsing_error is False
        assert by_id[after.record_id].encountered_parsing_error is False

    def test_parked_records_do_not_occupy_later_batches(self, store, settings):
        settings.request_batch_size = 1  # one row per batch: a bad record that kept coming back would starve everything behind it
        too_short = scopus_record(3)
        too_short["dc:description"] = too_short["dc:description"][: settings.min_abstract_length - 1]

        batches, rows = run_batches(store, scopus_row(raw={}), scopus_row(raw=too_short), scopus_row(4), passes=3)

        assert [[entry.scopus_id for entry in batch] for batch in batches] == [
            [],
            [],
            [scopus_record(4)["eid"]],
        ]  # park once, skip forever: the good row surfaces in its own batch
        assert rows[0].encountered_parsing_error is True
        assert rows[1].has_eligible_abstract is False

    def test_record_in_unimplemented_format_is_parked(self, store):
        """A row in a `Format` without a parser parks on its own — it must not abort the batch the good records share with it."""
        unimplemented = Record(format=Format.WOS_API, raw={"uid": "WOS:000300182800012", "title": ["Adaptation pathways in coastal cities"]})

        batches, rows = run_batches(store, scopus_row(0), unimplemented)

        assert [entry.scopus_id for entry in batches[0]] == [scopus_record(0)["eid"]]
        assert rows[-1].encountered_parsing_error is True

    def test_unparseable_cover_date_lands_with_unknown_year(self, store):
        """'In Press' is not a parse failure: the year simply stays unknown (the year check tolerates that), the record stays in play."""
        in_press = scopus_record(0)
        in_press["prism:coverDate"] = "In Press"
        in_press.pop("prism:coverDisplayDate", None)

        batches, rows = run_batches(store, scopus_row(raw=in_press))

        assert [entry.scopus_id for entry in batches[0]] == [in_press["eid"]]  # offered normally, not parked
        assert batches[0][0].publication_year is None
        assert rows[0].encountered_parsing_error is False


class TestMatchPersistence:
    def test_one_match_row_per_reference_a_record_matched(self, store):
        """The repository answered one batch with several references per record - every pair becomes its own row."""
        first, second = scopus_row(0), scopus_row(9)

        rows, matches = run_match_stage(
            store,
            [first, second],
            matched=[(first, DESTINY_ID), (first, OTHER_ID), (second, DESTINY_ID)],
        )

        assert all(row.evaluated is True for row in rows)  # evaluation stays a property of the record ...
        assert sorted((match.record_id, match.destiny_id) for match in matches) == sorted(
            [(first.record_id, DESTINY_ID), (first.record_id, OTHER_ID), (second.record_id, DESTINY_ID)]
        )  # ... while the references land next to it, one row each
        assert match_flags(matches) == {
            (first.record_id, DESTINY_ID): (False, False, False),  # nothing passed the enhancement criteria yet
            (first.record_id, OTHER_ID): (False, False, False),
            (second.record_id, DESTINY_ID): (False, False, False),
        }

    def test_enhancement_flags_are_set_per_reference(self, store):
        """Only the pairs that passed the criteria (and were asked about) are marked - the other reference of the same record is not."""
        first, second = scopus_row(0), scopus_row(9)

        rows, matches = run_match_stage(
            store,
            [first, second],
            matched=[(first, DESTINY_ID), (first, OTHER_ID), (second, DESTINY_ID)],
            filtered=[(first, DESTINY_ID), (second, DESTINY_ID)],
            requested=[(second, DESTINY_ID)],
        )

        assert all(row.evaluated is True for row in rows)  # marking the record stays independent of its matches
        assert match_flags(matches) == {
            (first.record_id, DESTINY_ID): (True, False, False),  # enhancement wanted, not yet asked for
            (first.record_id, OTHER_ID): (False, False, False),  # the record's other reference is untouched
            (second.record_id, DESTINY_ID): (True, True, False),
        }

    def test_replaying_a_match_result_adds_nothing_and_resets_nothing(self, store):
        """Re-evaluating a record (after a parser fix, or after a crash that lost the response) must not clash with its own matches."""
        first = scopus_row(0)

        rows, matches = run_match_stage(
            store,
            [first],
            matched=[(first, DESTINY_ID), (first, OTHER_ID)],
            filtered=[(first, DESTINY_ID)],
            requested=[(first, DESTINY_ID)],
            submitted=[(first, DESTINY_ID)],
            twice=True,
        )

        assert [row.evaluated for row in rows] == [True]
        assert len(matches) == 2  # replayed, but nothing duplicated
        assert match_flags(matches) == {(first.record_id, DESTINY_ID): (True, True, True), (first.record_id, OTHER_ID): (False, False, False)}

    def test_only_the_submitted_pair_is_marked(self, store):
        """The enhance runner reports (record, reference) pairs, so its sibling matches stay unsubmitted."""
        first, second = scopus_row(0), scopus_row(9)

        _, matches = run_match_stage(
            store,
            [first, second],
            matched=[(first, DESTINY_ID), (first, OTHER_ID), (second, DESTINY_ID)],
            filtered=[(first, DESTINY_ID), (first, OTHER_ID), (second, DESTINY_ID)],
            requested=[(first, DESTINY_ID), (first, OTHER_ID), (second, DESTINY_ID)],
            submitted=[(first, OTHER_ID)],
        )

        assert match_flags(matches) == {
            (first.record_id, DESTINY_ID): (True, True, False),
            (first.record_id, OTHER_ID): (True, True, True),
            (second.record_id, DESTINY_ID): (True, True, False),
        }

    def test_a_record_without_any_match_keeps_no_row(self, store):
        """Evaluated against the repository, but nothing found: the record carries the verdict, the match table stays empty."""
        row = scopus_row(0)

        rows, matches = run_match_stage(store, [row], matched=[])

        assert [row.evaluated for row in rows] == [True]
        assert matches == []


class TestReadingMatchesBack:
    """The enhance runner comes back by DESTinY ID and has to find the cache entry behind the match again."""

    @staticmethod
    def read_back(
        store,
        rows: Sequence[Record],
        matched: Sequence[tuple[Record, UUID]],
        destiny_ids: set[UUID],
        *,
        ensure_overlap: bool = False,
    ) -> Sequence[ParsedRecordExtra]:
        """Match the rows to the given references, then read the given DESTinY IDs back as the enhance runner would."""

        async def flow() -> Sequence[ParsedRecordExtra]:
            await seed(store, *rows)
            entries = {row.record_id: ParsedRecordExtra.from_record(record=row) for row in rows}
            await store.persist_match_results(
                cache_entries=list(entries.values()),
                matched_references=[(entries[row.record_id], ods_reference(destiny_id=destiny_id)) for row, destiny_id in matched],
                filtered_references=[],
                requested_references=[],
            )
            return await store.get_parsed_extra_entries(destiny_ids=destiny_ids, ensure_overlap=ensure_overlap)

        return asyncio.run(flow())

    def test_serves_one_entry_per_reference_carrying_that_reference(self, store):
        first, second = scopus_row(0), scopus_row(9)

        entries = self.read_back(
            store,
            [first, second],
            matched=[(first, DESTINY_ID), (first, OTHER_ID), (second, DESTINY_ID)],
            destiny_ids={DESTINY_ID, OTHER_ID},
        )

        assert sorted((entry.destiny_id, entry.record_id) for entry in entries) == sorted([(DESTINY_ID, first.record_id), (OTHER_ID, first.record_id)])
        assert all(isinstance(entry, ParsedRecordExtra) and entry.raw == first.raw for entry in entries)  # the payload of the matched record travels along

    def test_records_sharing_a_reference_collapse_to_one_entry(self, store):
        first, second = scopus_row(0), scopus_row(9)

        entries = self.read_back(store, [first, second], matched=[(first, DESTINY_ID), (second, DESTINY_ID)], destiny_ids={DESTINY_ID})

        assert [entry.record_id for entry in entries] == [min(first.record_id, second.record_id)]  # deterministic: the first match, by record ID
        assert [entry.destiny_id for entry in entries] == [DESTINY_ID]

    def test_a_reference_nothing_matched_is_not_served(self, store):
        row = scopus_row(0)

        entries = self.read_back(store, [row], matched=[(row, DESTINY_ID)], destiny_ids={OTHER_ID})

        assert entries == []  # without a match row there is no link, so the ID stays unanswered

    def test_ensure_overlap_complains_about_the_uncovered_reference(self, store):
        row = scopus_row(0)

        with pytest.raises(RuntimeError, match="Did not find submittable record for all requested IDs!"):
            self.read_back(store, [row], matched=[(row, DESTINY_ID)], destiny_ids={DESTINY_ID, OTHER_ID}, ensure_overlap=True)

    def test_a_record_matched_to_several_references_can_be_served_for_each_of_them(self, store):
        """Reading one record back twice, once per reference, is what lets both enhancements be submitted."""
        row = scopus_row(0)

        for destiny_id in (DESTINY_ID, OTHER_ID):
            entries = self.read_back(store, [row], matched=[(row, DESTINY_ID), (row, OTHER_ID)], destiny_ids={destiny_id}, ensure_overlap=True)
            assert [(entry.record_id, entry.destiny_id) for entry in entries] == [(row.record_id, destiny_id)]
