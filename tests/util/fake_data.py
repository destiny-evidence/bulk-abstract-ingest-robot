"""
Realistic sample data and generators shared across the runner tests.

Texts are sized and shaped to exercise the matching thresholds in `app/robots/match.py`:
`title_with_distance` and `abstract_with_distance` control the exact edit distance from
`TITLE`/`NEW_ABSTRACT`, and the `*_for` helpers build reference value lists around those thresholds.
"""

import itertools
from uuid import UUID

from app.db.schemas import ODSRecord, ParsedRecord

DESTINY_ID = UUID("11111111-1111-1111-1111-111111111111")
OTHER_ID = UUID("22222222-2222-2222-2222-222222222222")

DEFAULT_TITLE = "Governance pathways for coastal flood adaptation in 12 port cities"
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

DEFAULT_ABSTRACT = (
    "We compare flood adaptation governance across 12 port cities (2010–2024) using 96 policy documents and 41 interviews. Financing mode, not hazard exposure, best explains "
    "implementation speed; centrally funded plans deliver 2.3x faster. We recommend pooling municipal risk bonds — median coverage rises from 34% to 71% in our simulations."
)


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
