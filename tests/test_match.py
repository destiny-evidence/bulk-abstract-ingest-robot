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
    DIFFERENT_ABSTRACT,
    MINIMAL_ABSTRACT,
    NEW_ABSTRACT,
    OTHER_ID,
    SAME_ABSTRACT,
    TITLE,
    abstract_with_raw_length,
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
    # The rows carry the outcome for both settings of `use_slop_for_enhancement_check`: compared (True) or not (False, the default).
    @pytest.mark.parametrize(
        ("kind", "expected_compared", "expected_presence_only"),
        [
            ("none", True, True),  # the repository has no abstract yet, submitting is the whole point
            ("exact", False, False),  # an identical abstract is already in the repository
            ("near", False, False),  # distance == max_abstract_slop, still considered a duplicate
            ("far", True, False),  # distance == max_abstract_slop + 1: new text when compared, but any repository abstract will do when not
            ("unrelated", True, False),  # whether the repository abstract says something else only matters when the texts are compared
            ("too_short", True, True),  # repository abstracts at or below min_abstract_length are ignored
            ("just_below_max_length", False, False),  # long, but still short enough to be trusted
            ("at_max_length", True, True),  # max_abstract_length is where a repository abstract stops being trusted, in either mode
            ("overlong", True, True),  # ... so even the very same text we are about to submit stops blocking once it is over the threshold
            ("multi_all_unrelated", True, False),
            ("multi_exact_among_unrelated", False, False),
            ("multi_near_among_unrelated", False, False),
            ("multi_all_beyond_threshold", True, False),
            ("multi_all_three_thresholds", False, False),  # a single similar repository abstract blocks submission
        ],
    )
    @pytest.mark.parametrize("use_slop", [True, False], ids=["compared", "presence only"])
    def test_abstract_filtering(self, match_runner, settings, kind, expected_compared, expected_presence_only, use_slop):
        settings.use_slop_for_enhancement_check = use_slop
        expected = expected_compared if use_slop else expected_presence_only
        abstracts = abstracts_for(kind, settings.max_abstract_slop, settings.max_abstract_length)

        assert match_runner.should_enhance(cache_entry(), ods_reference(abstracts=abstracts)) is expected

    @pytest.mark.parametrize("offset", [-1, 0], ids=["just below min", "at min"])
    def test_cached_abstract_shortness(self, match_runner, settings, offset):
        # Below min_abstract_length there is nothing worth submitting; at exactly the threshold the abstract counts, and the comparison is skipped (it asks for more than the minimum)
        expected = offset >= 0
        abstract = abstract_with_raw_length(settings.min_abstract_length + offset, MINIMAL_ABSTRACT)

        assert match_runner.should_enhance(cache_entry(abstract=abstract), ods_reference()) is expected

    @pytest.mark.parametrize("offset", [-1, 0, 1], ids=["just below max", "at max", "above max"])
    def test_cached_abstract_length_window(self, match_runner, settings, offset):
        # Our own abstract is only trustworthy while it stays below max_abstract_length; the reference carries none, so this is purely the cached side's bound
        expected = offset < 0
        abstract = abstract_with_raw_length(settings.max_abstract_length + offset)

        assert match_runner.should_enhance(cache_entry(abstract=abstract), ods_reference()) is expected

    @pytest.mark.parametrize("use_slop", [True, False], ids=["compared", "presence only"])
    def test_overlong_cached_abstract_is_rejected_before_comparing(self, match_runner, settings, use_slop):
        # Too long to be trusted, so it never reaches the abstract check — not even against a repository copy of itself
        settings.use_slop_for_enhancement_check = use_slop
        overlong = abstract_with_raw_length(settings.max_abstract_length)

        assert match_runner.should_enhance(cache_entry(abstract=overlong), ods_reference(abstracts=[SAME_ABSTRACT])) is False

    def test_abstract_comparison_is_normalised(self, match_runner, settings):
        # case, punctuation, digits and spacing differences are stripped before comparison, so the repository still has a matching abstract
        # (only the comparison can show that: presence-only mode would block this entry for length reasons alone)
        settings.use_slop_for_enhancement_check = True

        assert (
            match_runner.should_enhance(cache_entry(abstract=NEW_ABSTRACT.upper().replace(" ", ",  ") + "?? + 2026"), ods_reference(abstracts=[SAME_ABSTRACT]))
            is False
        )

    def test_the_check_mode_decides_a_different_repository_abstract(self, match_runner, settings):
        """
        One and the same record is eligible or not, depending only on `use_slop_for_enhancement_check`:
        the repository has an abstract of trustworthy length that clearly talks about something else.
        """
        reference = ods_reference(abstracts=[DIFFERENT_ABSTRACT])

        settings.use_slop_for_enhancement_check = True
        assert match_runner.should_enhance(cache_entry(), reference) is True

        settings.use_slop_for_enhancement_check = False
        assert match_runner.should_enhance(cache_entry(), reference) is False


class TestLoopTask:
    def test_keeps_polling_when_no_entries(self, match_runner):
        match_runner.store.get_next_request_batch.return_value = []

        asyncio.run(match_runner._loop_task())

        assert not match_runner.shutdown_event.is_set()  # an empty task list no longer stops the robot
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

    def test_enhancement_check_mode_changes_what_is_requested(self, match_runner, settings):
        """The same batch yields a request in comparison mode and none in presence-only mode (the default)."""
        entry = cache_entry()
        match_runner.store.get_next_request_batch.return_value = [entry]
        match_runner.repository.query_repository.return_value = [(entry, ods_reference(abstracts=[DIFFERENT_ABSTRACT]))]

        settings.use_slop_for_enhancement_check = True
        asyncio.run(match_runner._loop_task())
        assert match_runner.repository.request_to_enhance.call_count == 1

        match_runner.repository.request_to_enhance.reset_mock()
        settings.use_slop_for_enhancement_check = False
        asyncio.run(match_runner._loop_task())
        match_runner.repository.request_to_enhance.assert_not_called()
        match_runner.store.persist_match_results.assert_awaited()  # the entry is still matched and recorded, just not submitted
