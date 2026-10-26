"""
Tests for the matching/enhancement-eligibility runner.

The repository API (see https://api.evidence-repository.org/openapi.json) is mocked at the
`Repository` boundary: `query_repository` (references lookup) and `request_to_enhance`
(POST /v1/enhancement-requests/), so no HTTP traffic is generated here.
The sample texts and the threshold-bearing text generators come from tests/fake_data.py,
the shared fixtures (including the `match_runner` fixture) from tests/fixtures.py.
"""

import asyncio

import pytest

from tests.util.fake_data import (
    DESTINY_ID,
    NEW_ABSTRACT,
    OTHER_ID,
    SAME_ABSTRACT,
    TITLE,
    abstracts_for,
    cache_entry,
    ods_reference,
    titles_for,
)


class TestShouldEnhance:
    @pytest.mark.parametrize("overrides", [{"abstract": None}, {"abstract": "too short"}], ids=["missing abstract", "short abstract"])
    def test_rejects_empty_or_short_abstract(self, match_runner, overrides):
        assert match_runner.should_enhance(cache_entry(**overrides), ods_reference()) is False

    def test_rejects_reference_without_destiny_id(self, match_runner):
        assert match_runner.should_enhance(cache_entry(), ods_reference(destiny_id=None)) is False

    def test_rejects_year_outside_tolerance(self, match_runner, settings):
        assert (
            match_runner.should_enhance(cache_entry(publication_year=2020), ods_reference(publication_years=[2020 + settings.publication_year_tolerance + 1]))
            is False
        )

    def test_accepts_year_within_tolerance(self, match_runner, settings):
        assert (
            match_runner.should_enhance(cache_entry(publication_year=2020), ods_reference(publication_years=[2020 + settings.publication_year_tolerance]))
            is True
        )

    @pytest.mark.parametrize(
        ("year", "ref_years"),
        [(None, [2020]), (2020, None)],
        ids=["cached year unknown", "reference years unknown"],
    )
    def test_skips_year_check_when_unavailable(self, match_runner, year, ref_years):
        assert match_runner.should_enhance(cache_entry(publication_year=year), ods_reference(publication_years=ref_years)) is True


class TestTitleMatching:
    @pytest.mark.parametrize(
        ("kind", "expected"),
        [
            ("none", True),  # no title information in the reference, check is skipped
            ("exact", True),
            ("near", True),  # distance == max_title_slop, still accepted
            ("far", False),  # distance == max_title_slop + 1, just out of tolerance
            ("unrelated", False),
            ("multi_all_unrelated", False),
            ("multi_exact_among_unrelated", True),
            ("multi_near_among_unrelated", True),
            ("multi_all_beyond_threshold", False),
            ("multi_all_three_thresholds", True),  # accepted as soon as any single title passes
        ],
    )
    def test_title_filtering(self, match_runner, settings, kind, expected):
        assert match_runner.should_enhance(cache_entry(), ods_reference(titles=titles_for(kind, settings.max_title_slop))) is expected

    def test_title_comparison_is_normalised(self, match_runner):
        # case, punctuation, digits and spacing differences are stripped before comparison
        assert (
            match_runner.should_enhance(
                cache_entry(title="CLIMATE CHANGE ADAPTATION, IN URBAN AREAS — A REVIEW OF 14 CITIES: 2005–2023?! WHAT WORKS!!"),
                ods_reference(titles=[TITLE]),
            )
            is True
        )


class TestAbstractMatching:
    @pytest.mark.parametrize(
        ("kind", "expected"),
        [
            ("none", True),  # the repository has no abstract yet, submitting is the whole point
            ("exact", False),  # an identical abstract is already in the repository
            ("near", False),  # distance == max_abstract_slop, still considered a duplicate
            ("far", True),  # distance == max_abstract_slop + 1, just out of tolerance and thus a genuinely new abstract
            ("unrelated", True),
            ("too_short", True),  # repository abstracts at or below min_abstract_length are ignored
            ("multi_all_unrelated", True),
            ("multi_exact_among_unrelated", False),
            ("multi_near_among_unrelated", False),
            ("multi_all_beyond_threshold", True),
            ("multi_all_three_thresholds", False),  # a single similar repository abstract blocks submission
        ],
    )
    def test_abstract_filtering(self, match_runner, settings, kind, expected):
        assert match_runner.should_enhance(cache_entry(), ods_reference(abstracts=abstracts_for(kind, settings.max_abstract_slop))) is expected

    def test_abstract_comparison_is_normalised(self, match_runner):
        # case, punctuation, digits and spacing differences are stripped before comparison, so the repository still has a matching abstract
        assert (
            match_runner.should_enhance(cache_entry(abstract=NEW_ABSTRACT.upper().replace(" ", ",  ") + "?? + 2026"), ods_reference(abstracts=[SAME_ABSTRACT]))
            is False
        )


class TestLoopTask:
    def test_stops_when_no_entries(self, match_runner):
        match_runner.store.get_next_request_batch.return_value = []

        asyncio.run(match_runner._loop_task())

        assert match_runner.shutdown_event.is_set()
        match_runner.repository.query_repository.assert_not_called()
        match_runner.store.persist_match_results.assert_not_awaited()

    def test_filters_and_requests_enhancements(self, match_runner):
        matched, stale, unmatched = cache_entry(), cache_entry(abstract=SAME_ABSTRACT), cache_entry()
        matched_ref, stale_ref = ods_reference(), ods_reference(destiny_id=OTHER_ID, abstracts=[SAME_ABSTRACT])
        match_runner.store.get_next_request_batch.return_value = [matched, stale, unmatched]
        match_runner.repository.query_repository.return_value = [(matched, matched_ref), (stale, stale_ref)]

        asyncio.run(match_runner._loop_task())

        match_runner.repository.request_to_enhance.assert_called_once_with(destiny_ids=[DESTINY_ID])  # only the eligible entry
        match_runner.store.persist_match_results.assert_awaited_once_with(
            cache_entries=[matched, stale, unmatched],
            matched_cache_entries=[matched, stale],
            filtered_references=[(matched, matched_ref)],
            requested_cache_entries=[matched],
        )
        assert match_runner.total_entries_processed == 3

    def test_persists_without_requesting_when_nothing_is_eligible(self, match_runner):
        entry = cache_entry()
        match_runner.store.get_next_request_batch.return_value = [entry]
        match_runner.repository.query_repository.return_value = [(entry, ods_reference(abstracts=[SAME_ABSTRACT]))]

        asyncio.run(match_runner._loop_task())

        match_runner.repository.request_to_enhance.assert_not_called()
        match_runner.store.persist_match_results.assert_awaited_once()
