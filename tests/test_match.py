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
    DEFAULT_ABSTRACT,
    DESTINY_ID,
    DIFFERENT_ABSTRACT,
    MINIMAL_ABSTRACT,
    NEW_ABSTRACT,
    OTHER_ABSTRACT,
    OTHER_ID,
    SAME_ABSTRACT,
    TITLE,
    abstract_with_extra_letters,
    abstract_with_raw_length,
    abstracts_for,
    cache_entry,
    ods_reference,
    title_with_distance,
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

    @pytest.mark.parametrize("cached_is_longer", [True, False], ids=["cached title longer", "reference title longer"])
    def test_title_length_mismatch_is_tolerated_on_either_side(self, match_runner, settings, cached_is_longer):
        # The titles are identical except for `max_title_slop` extra letters — which side carries them must not matter
        longer = title_with_distance(settings.max_title_slop)
        cache, reference = (longer, TITLE) if cached_is_longer else (TITLE, longer)

        assert match_runner.should_enhance(cache_entry(title=cache), ods_reference(titles=[reference])) is True

    def test_titles_compatible_in_length_and_content(self, match_runner):
        # Same length (after normalisation) and a handful of different letters: clearly the same work
        assert match_runner.should_enhance(cache_entry(), ods_reference(titles=[TITLE.replace("cities", "coasts")])) is True

    def test_similar_title_length_does_not_make_titles_compatible(self, match_runner):
        # Identical length (after normalisation), but the middle is about kiln pottery and the ending about a critique: a different work, however long or short it is
        incompatible = TITLE.replace("adaptation in urban areas", "typology at the kiln sites").replace("what works", "a critique")

        assert match_runner.should_enhance(cache_entry(), ods_reference(titles=[incompatible])) is False

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
    # The rows carry the outcome for both settings of `replace_abstracts_on_mismatch`: compared (True) or not (False, the default).
    @pytest.mark.parametrize(
        ("kind", "expected_compared", "expected_presence_only"),
        [
            ("none", True, True),  # the repository has no abstract yet, submitting is the whole point
            ("exact", False, False),  # an identical abstract is already in the repository
            ("holds_more_than_ours", True, False),  # it carries our abstract plus a slop beyond: a length gap counts as a difference either way
            ("unrelated", True, False),  # whether the repository abstract says something else only matters when the texts are compared
            ("too_short", True, True),  # repository abstracts below min_abstract_length are ignored
            ("just_below_max_length", False, False),  # long, but still short enough to be trusted
            ("at_max_length", True, True),  # max_abstract_length is where a repository abstract stops being trusted, in either mode
            ("overlong", True, True),  # ... so even the very same text we are about to submit stops blocking once it is over the threshold
            ("multi_all_unrelated", True, False),
            ("multi_exact_among_unrelated", False, False),
            ("multi_holds_more_among_unrelated", True, False),
            ("multi_duplicate_among_beyond_threshold", False, False),  # a single good enough repository abstract blocks submission
        ],
    )
    @pytest.mark.parametrize("use_slop", [True, False], ids=["compared", "presence only"])
    def test_abstract_filtering(self, match_runner, settings, kind, expected_compared, expected_presence_only, use_slop):
        settings.replace_abstracts_on_mismatch = use_slop
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
        settings.replace_abstracts_on_mismatch = use_slop
        overlong = abstract_with_raw_length(settings.max_abstract_length)

        assert match_runner.should_enhance(cache_entry(abstract=overlong), ods_reference(abstracts=[SAME_ABSTRACT])) is False

    def test_abstract_comparison_is_normalised(self, match_runner, settings):
        # case, punctuation, digits and spacing differences are stripped before comparison, so the repository still has a matching abstract
        # (only the comparison can show that: presence-only mode would block this entry for length reasons alone)
        settings.replace_abstracts_on_mismatch = True

        assert (
            match_runner.should_enhance(cache_entry(abstract=NEW_ABSTRACT.upper().replace(" ", ",  ") + "?? + 2026"), ods_reference(abstracts=[SAME_ABSTRACT]))
            is False
        )

    def test_a_repository_abstract_short_of_our_text_does_not_block_it(self, match_runner, settings):
        """The repository copy stops where our text keeps going: within the slop that is noise, one character further it reads as truncated and we offer ours."""
        settings.replace_abstracts_on_mismatch = True
        repository_copy = ods_reference(abstracts=[SAME_ABSTRACT])

        assert match_runner.should_enhance(cache_entry(abstract=abstract_with_extra_letters(settings.max_abstract_slop)), repository_copy) is False
        assert match_runner.should_enhance(cache_entry(abstract=abstract_with_extra_letters(settings.max_abstract_slop + 1)), repository_copy) is True

    def test_a_length_gap_makes_the_abstracts_differ_whichever_side_it_is_on(self, match_runner, settings):
        """The same `max_abstract_slop` + 1 characters, on either side of the comparison: either copy may be the truncated one, so both count as a different abstract."""
        settings.replace_abstracts_on_mismatch = True
        longer, shorter = abstract_with_extra_letters(settings.max_abstract_slop + 1), NEW_ABSTRACT

        # the repository stops short of our text
        assert match_runner.should_enhance(cache_entry(abstract=longer), ods_reference(abstracts=[shorter])) is True
        # the same gap on our side reads the same way
        assert match_runner.should_enhance(cache_entry(abstract=shorter), ods_reference(abstracts=[longer])) is True

    def test_repository_abstract_holding_far_more_than_ours_counts_as_different(self, match_runner, settings):
        """A repository abstract carrying our text and several slops on top is treated as another abstract, not as a better one."""
        settings.replace_abstracts_on_mismatch = True

        assert match_runner.should_enhance(cache_entry(), ods_reference(abstracts=[abstract_with_extra_letters(settings.max_abstract_slop * 3)])) is True

    @pytest.mark.parametrize("use_slop", [True, False], ids=["compared", "presence only"])
    def test_repository_abstract_at_the_minimum_counts_as_a_real_abstract(self, match_runner, settings, use_slop):
        """Symmetry with the cached side (`test_cached_abstract_shortness`): an abstract of exactly `min_abstract_length` characters is trustworthy on either side of the comparison."""
        settings.replace_abstracts_on_mismatch = use_slop
        at_minimum = abstract_with_raw_length(settings.min_abstract_length, MINIMAL_ABSTRACT)

        # presence-only: it blocks submission on its own; compared: it is unrelated to our text, so it does not block
        assert match_runner.should_enhance(cache_entry(), ods_reference(abstracts=[at_minimum])) is use_slop

    def test_minor_typo_in_a_long_repository_abstract_still_reads_as_the_same_abstract(self, match_runner, settings):
        """
        Regression test: the earlier character-diff metric turned unreliable past 200 characters (difflib's `autojunk`
        heuristic) and scored a few typos in a long abstract as a wholly different text, so the robot would have
        re-submitted an abstract the repository already held.
        """
        settings.replace_abstracts_on_mismatch = True
        long_abstract = " ".join([DEFAULT_ABSTRACT] * 6)  # well past the 200-character mark the old metric broke at, still inside the trust window
        assert settings.min_abstract_length <= len(long_abstract) < settings.max_abstract_length

        typo = long_abstract.replace("hazard exposure", "hazard expresure", 1)
        assert match_runner.should_enhance(cache_entry(abstract=long_abstract), ods_reference(abstracts=[typo])) is False

        # and the other direction still holds: a genuinely different long abstract does not block the submission
        unrelated_long = " ".join([DIFFERENT_ABSTRACT, OTHER_ABSTRACT] * 3)
        assert match_runner.should_enhance(cache_entry(abstract=long_abstract), ods_reference(abstracts=[unrelated_long])) is True

    def test_the_check_mode_decides_a_different_repository_abstract(self, match_runner, settings):
        """
        One and the same record is eligible or not, depending only on `replace_abstracts_on_mismatch`:
        the repository has an abstract of trustworthy length that clearly talks about something else.
        """
        reference = ods_reference(abstracts=[DIFFERENT_ABSTRACT])

        settings.replace_abstracts_on_mismatch = True
        assert match_runner.should_enhance(cache_entry(), reference) is True

        settings.replace_abstracts_on_mismatch = False
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
            matched_references=[(matched, matched_ref), (stale, stale_ref)],
            filtered_references=[(matched, matched_ref)],
            requested_references=[(matched, matched_ref)],
        )
        assert match_runner.total_entries_processed == 3

    def test_one_cache_entry_matched_to_several_references_requests_each_eligible_one(self, match_runner):
        """The repository may answer one query with several (not yet merged) references, and they are judged one by one."""
        entry, other_entry = cache_entry(), cache_entry()
        eligible_ref, duplicate_ref = ods_reference(), ods_reference(destiny_id=OTHER_ID, abstracts=[SAME_ABSTRACT])
        match_runner.store.get_next_request_batch.return_value = [entry, other_entry]
        match_runner.repository.query_repository.return_value = [(entry, eligible_ref), (entry, duplicate_ref), (other_entry, eligible_ref)]

        asyncio.run(match_runner._loop_task())

        match_runner.repository.request_to_enhance.assert_called_once_with(destiny_ids=[DESTINY_ID])  # the duplicate blocks its other reference
        persisted = match_runner.store.persist_match_results.await_args.kwargs
        assert persisted["matched_references"] == [(entry, eligible_ref), (entry, duplicate_ref), (other_entry, eligible_ref)]
        assert persisted["filtered_references"] == [(entry, eligible_ref), (other_entry, eligible_ref)]
        assert persisted["requested_references"] == [(entry, eligible_ref), (other_entry, eligible_ref)]

    def test_a_reference_without_destiny_id_is_persisted_but_never_asked_about(self, match_runner):
        """The enhancement rules already rule out a reference we cannot name: the match is recorded, no request goes out."""
        entry = cache_entry()
        reference = ods_reference(destiny_id=None)
        match_runner.store.get_next_request_batch.return_value = [entry]
        match_runner.repository.query_repository.return_value = [(entry, reference)]

        asyncio.run(match_runner._loop_task())

        match_runner.repository.request_to_enhance.assert_not_called()
        persisted = match_runner.store.persist_match_results.await_args.kwargs
        assert persisted["matched_references"] == [(entry, reference)]  # the match itself is still worth remembering
        assert persisted["filtered_references"] == []
        assert persisted["requested_references"] == []

    def test_persists_without_requesting_when_nothing_is_eligible(self, match_runner):
        entry = cache_entry()
        match_runner.store.get_next_request_batch.return_value = [entry]
        match_runner.repository.query_repository.return_value = [(entry, ods_reference(abstracts=[SAME_ABSTRACT]))]

        asyncio.run(match_runner._loop_task())

        match_runner.repository.request_to_enhance.assert_not_called()
        match_runner.store.persist_match_results.assert_awaited_once()

    def test_failed_enhancement_request_keeps_the_match_results(self, match_runner):
        """
        A briefly unreachable repository must not take the evaluated batch down with it: the match results persist
        (no re-querying, no duplicate requests), and the pairs simply stay required-but-unrequested until re-requested.
        """
        entry = cache_entry()
        reference = ods_reference()
        match_runner.store.get_next_request_batch.return_value = [entry]
        match_runner.repository.query_repository.return_value = [(entry, reference)]
        match_runner.repository.request_to_enhance.side_effect = ConnectionError("repository unreachable")

        asyncio.run(match_runner._loop_task())  # the failure must not escape the loop task

        match_runner.store.persist_match_results.assert_awaited_once_with(
            cache_entries=[entry],
            matched_references=[(entry, reference)],
            filtered_references=[(entry, reference)],
            requested_references=[],  # the request did not go out, so the pair is not remembered as requested
        )

    def test_enhancement_check_mode_changes_what_is_requested(self, match_runner, settings):
        """The same batch yields a request in comparison mode and none in presence-only mode (the default)."""
        entry = cache_entry()
        match_runner.store.get_next_request_batch.return_value = [entry]
        match_runner.repository.query_repository.return_value = [(entry, ods_reference(abstracts=[DIFFERENT_ABSTRACT]))]

        settings.replace_abstracts_on_mismatch = True
        asyncio.run(match_runner._loop_task())
        assert match_runner.repository.request_to_enhance.call_count == 1

        match_runner.repository.request_to_enhance.reset_mock()
        settings.replace_abstracts_on_mismatch = False
        asyncio.run(match_runner._loop_task())
        match_runner.repository.request_to_enhance.assert_not_called()
        match_runner.store.persist_match_results.assert_awaited()  # the entry is still matched and recorded, just not submitted
