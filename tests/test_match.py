"""
Tests for the matching/enhancement-eligibility runner.

The repository API (see https://api.evidence-repository.org/openapi.json) is mocked at the
`Repository` boundary: `query_repository` (references lookup) and `request_to_enhance`
(POST /v1/enhancement-requests/), so no HTTP traffic is generated here.
"""

import asyncio
import itertools
import logging
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID

import pytest

from app.db.schemas import ODSRecord, ParsedRecord
from app.robots.match import MatchRunner
from app.util.config import Settings

DESTINY_ID = UUID("11111111-1111-1111-1111-111111111111")
OTHER_ID = UUID("22222222-2222-2222-2222-222222222222")

TITLE = "Climate-change adaptation in urban areas: a review of 42 cities (2005–2023) — what works?!"
UNRELATED_TITLES = [
    "Medieval pottery typology in the Levantine coast: 118 kiln sites (400–900 CE) — a critique!",
    "Glacial isostatic adjustment beneath the Laurentide ice sheet; marine isotope stage 2 revisited?",
]

NEW_ABSTRACT = (
    "This study reviews heat + drought adaptation in 42 coastal cities (2005–2023); we ask: what works? Governance, finance, and health outcomes were scored — median risk "
    "reduction 23% (IQR: 11–37%). Early-warning systems save lives (+2.1 life-years per 1,000); funding gaps remain: not one city had a plan covering all districts."
)  # raw length 325, comfortably above min_abstract_length (200)
SAME_ABSTRACT = NEW_ABSTRACT  # the repository copy, identical to the cached one
DIFFERENT_ABSTRACT = (
    "Palaeolithic flint knapping in the Alpine foreland: 118 core fragments were analysed; typology suggests seasonal camps (c. 14,500–12,800 BP). Isotope data — oxygen + "
    "carbon — indicate short summer stays; no evidence for year-round occupation was found."
)
OTHER_ABSTRACT = (
    "We modelled wheat + barley yields under 6 GCM scenarios (RCP 2.6/6.0; 2010–2099). Median decline: 17%; irrigation cuts losses by 9% — can farmers adapt? Survey data "
    "from 3 provinces (n = 1,412) suggest limited uptake of drought-tolerant seed."
)
UNRELATED_ABSTRACTS = [DIFFERENT_ABSTRACT, OTHER_ABSTRACT]


def words_with_letters(count: int) -> str:
    """Real words plus digits/punctuation holding exactly `count` letters (digits and symbols are stripped before comparison and add no distance)."""
    pool = ["governance", "adaptation", "heat", "waves", "in", "2019", "coastal", "risk", "and", "a"]
    words, remaining = [], count
    for word in itertools.cycle(pool):
        letters = sum(c.isalpha() for c in word)
        if letters <= remaining:
            words.append(word)
            remaining -= letters
            if remaining == 0:
                break
    return " " + " ".join(words) + " (2024)?"


def cache_entry(**overrides) -> ParsedRecord:
    return ParsedRecord(**{"title": TITLE, "abstract": NEW_ABSTRACT, "publication_year": 2020, **overrides})


def ods_reference(**overrides) -> ODSRecord:
    return ODSRecord(
        **{
            "destiny_id": DESTINY_ID,
            "titles": [TITLE],
            "abstracts": [DIFFERENT_ABSTRACT],
            "publication_years": [2020],
            **overrides,
        },
    )


@pytest.fixture
def settings() -> Settings:
    return Settings()


@pytest.fixture
def runner(settings: Settings) -> MatchRunner:
    """MatchRunner with bypassed __init__ (no settings/logging/DB/auth clients) and mocked store/repository."""
    runner = MatchRunner.__new__(MatchRunner)
    runner.settings = settings
    runner.logger = logging.getLogger("test")
    runner.loop_logger = logging.getLogger("test.loop")
    runner.total_entries_processed = 0
    runner.shutdown_event = asyncio.Event()
    runner.store = AsyncMock()
    runner.repository = MagicMock()
    runner.repository.query_repository = AsyncMock()
    runner.repository.request_to_enhance = MagicMock()
    return runner


class TestShouldEnhance:
    @pytest.mark.parametrize("overrides", [{"abstract": None}, {"abstract": "too short"}], ids=["missing abstract", "short abstract"])
    def test_rejects_empty_or_short_abstract(self, runner, overrides):
        assert runner.should_enhance(cache_entry(**overrides), ods_reference()) is False

    def test_rejects_reference_without_destiny_id(self, runner):
        assert runner.should_enhance(cache_entry(), ods_reference(destiny_id=None)) is False

    def test_rejects_year_outside_tolerance(self, runner, settings):
        assert (
            runner.should_enhance(cache_entry(publication_year=2020), ods_reference(publication_years=[2020 + settings.publication_year_tolerance + 1]))
            is False
        )

    def test_accepts_year_within_tolerance(self, runner, settings):
        assert runner.should_enhance(cache_entry(publication_year=2020), ods_reference(publication_years=[2020 + settings.publication_year_tolerance])) is True

    @pytest.mark.parametrize(
        ("year", "ref_years"),
        [(None, [2020]), (2020, None)],
        ids=["cached year unknown", "reference years unknown"],
    )
    def test_skips_year_check_when_unavailable(self, runner, year, ref_years):
        assert runner.should_enhance(cache_entry(publication_year=year), ods_reference(publication_years=ref_years)) is True


def title_with_distance(distance: int) -> str:
    """TITLE extended with `distance` extra letters via trailing words (each letter adds exactly 1 to the title edit distance)."""
    return TITLE + " —" + words_with_letters(distance)


def titles_for(kind: str, slop: int) -> list[str] | None:
    """Reference title lists around the similarity threshold (accepted up to and including distance `slop`)."""
    near, far, far_away = title_with_distance(slop), title_with_distance(slop + 1), title_with_distance(slop + 5)
    return {
        "none": None,
        "exact": [TITLE],
        "near": [near],
        "far": [far],
        "unrelated": [UNRELATED_TITLES[0]],
        "multi_all_unrelated": UNRELATED_TITLES,
        "multi_exact_among_unrelated": [UNRELATED_TITLES[0], TITLE, UNRELATED_TITLES[1]],
        "multi_near_among_unrelated": [UNRELATED_TITLES[0], near],
        "multi_all_beyond_threshold": [UNRELATED_TITLES[0], far, far_away],
        "multi_all_three_thresholds": [far, far_away, near],
    }[kind]


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
    def test_title_filtering(self, runner, settings, kind, expected):
        assert runner.should_enhance(cache_entry(), ods_reference(titles=titles_for(kind, settings.max_title_slop))) is expected

    def test_title_comparison_is_normalised(self, runner):
        # case, punctuation, digits and spacing differences are stripped before comparison
        assert (
            runner.should_enhance(
                cache_entry(title="CLIMATE CHANGE ADAPTATION, IN URBAN AREAS — A REVIEW OF 14 CITIES: 2005–2023?! WHAT WORKS!!"), ods_reference(titles=[TITLE]),
            )
            is True
        )


def abstract_with_distance(distance: int) -> str:
    """NEW_ABSTRACT extended with `distance` extra letters via trailing words (each letter adds exactly 1 to the abstract edit distance)."""
    return NEW_ABSTRACT + ";" + words_with_letters(distance)


def abstracts_for(kind: str, slop: int) -> list[str] | None:
    """Reference abstract lists around the similarity threshold (considered duplicates up to and including distance `slop`)."""
    near, far, far_away = abstract_with_distance(slop), abstract_with_distance(slop + 1), abstract_with_distance(slop + 5)
    return {
        "none": None,
        "exact": [SAME_ABSTRACT],
        "near": [near],
        "far": [far],
        "unrelated": [UNRELATED_ABSTRACTS[0]],
        "too_short": ["Abstract not available. (2019)?"],
        "multi_all_unrelated": UNRELATED_ABSTRACTS,
        "multi_exact_among_unrelated": [UNRELATED_ABSTRACTS[0], SAME_ABSTRACT, UNRELATED_ABSTRACTS[1]],
        "multi_near_among_unrelated": [UNRELATED_ABSTRACTS[0], near],
        "multi_all_beyond_threshold": [*UNRELATED_ABSTRACTS, far, far_away],
        "multi_all_three_thresholds": [far, UNRELATED_ABSTRACTS[1], near],
    }[kind]


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
    def test_abstract_filtering(self, runner, settings, kind, expected):
        assert runner.should_enhance(cache_entry(), ods_reference(abstracts=abstracts_for(kind, settings.max_abstract_slop))) is expected

    def test_abstract_comparison_is_normalised(self, runner):
        # case, punctuation, digits and spacing differences are stripped before comparison, so the repository still has a matching abstract
        assert (
            runner.should_enhance(cache_entry(abstract=NEW_ABSTRACT.upper().replace(" ", ",  ") + "?? + 2026"), ods_reference(abstracts=[SAME_ABSTRACT]))
            is False
        )


class TestLoopTask:
    def test_stops_when_no_entries(self, runner):
        runner.store.get_next_request_batch.return_value = []

        asyncio.run(runner._loop_task())

        assert runner.shutdown_event.is_set()
        runner.repository.query_repository.assert_not_called()
        runner.store.persist_match_results.assert_not_awaited()

    def test_filters_and_requests_enhancements(self, runner):
        matched, stale, unmatched = cache_entry(), cache_entry(abstract=SAME_ABSTRACT), cache_entry()
        matched_ref, stale_ref = ods_reference(), ods_reference(destiny_id=OTHER_ID, abstracts=[SAME_ABSTRACT])
        runner.store.get_next_request_batch.return_value = [matched, stale, unmatched]
        runner.repository.query_repository.return_value = [(matched, matched_ref), (stale, stale_ref)]

        asyncio.run(runner._loop_task())

        runner.repository.request_to_enhance.assert_called_once_with(destiny_ids=[DESTINY_ID])  # only the eligible entry
        runner.store.persist_match_results.assert_awaited_once_with(
            cache_entries=[matched, stale, unmatched],
            matched_cache_entries=[matched, stale],
            filtered_references=[(matched, matched_ref)],
            requested_cache_entries=[matched],
        )
        assert runner.total_entries_processed == 3

    def test_persists_without_requesting_when_nothing_is_eligible(self, runner):
        entry = cache_entry()
        runner.store.get_next_request_batch.return_value = [entry]
        runner.repository.query_repository.return_value = [(entry, ods_reference(abstracts=[SAME_ABSTRACT]))]

        asyncio.run(runner._loop_task())

        runner.repository.request_to_enhance.assert_not_called()
        runner.store.persist_match_results.assert_awaited_once()
