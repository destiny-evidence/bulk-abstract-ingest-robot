"""
Tests for the store's request-batch step (`app/db/store.py`: which queued records the match runner is offered next,
and the verdicts the store writes back onto the ones it looks at: `has_eligible_abstract` and `encountered_parsing_error`).

These rules are production SQL and production parsing, so the `MockStore`-based tests cannot cover them — a mock that
mirrored the query would mirror its bugs too. The `store` fixture therefore runs the real `AbstractStore` against a
throwaway SQLite file (tests/util/sqlite_database.py). The payloads are tampered variants of a genuine Scopus API
export (tests/util/data/scopus_api_sample.jsonl), the shape of the rows a queued batch actually holds: samples 0-4 and 9
carry 600-2,000-character abstracts (and real DOIs where noted), while sample 18 came through the export without an abstract.
"""

import asyncio

import pytest

from app.db.schemas import Format, Record, Source
from tests.util.fake_data import scopus_record, scopus_row
from tests.util.sqlite_database import fetch_rows, seed


def run_batches(store, *rows: Record, passes: int = 1):
    """Seed the rows, take `passes` request batches from the store, then read the `records` table back."""

    async def flow() -> tuple[list, list]:
        await seed(store, *rows)
        batches = [list(await store.get_next_request_batch()) for _ in range(passes)]
        return batches, await fetch_rows(store)

    return asyncio.run(flow())


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

    @pytest.mark.xfail(
        reason="`get_next_request_batch` parks the parse failures it expects (AssertionError, ValidationError); a row in a `Format` without a parser raises NotImplementedError, which still aborts the entire batch instead of parking the record"
    )
    def test_record_in_unimplemented_format_is_parked(self, store):
        unimplemented = Record(format=Format.WOS_API, raw={"uid": "WOS:000300182800012", "title": ["Adaptation pathways in coastal cities"]})

        batches, rows = run_batches(store, scopus_row(0), unimplemented)

        assert [entry.scopus_id for entry in batches[0]] == [scopus_record(0)["eid"]]
        assert rows[-1].encountered_parsing_error is True
