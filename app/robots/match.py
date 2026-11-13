"""Runner that keeps matching cache entries to DESTinY IDs and checks plausibility of candidates."""

import asyncio
import re

from rapidfuzz.distance import Levenshtein

from app.db.schemas import ODSRecord, ParsedRecord
from app.util import Runner

ALPH = re.compile(r"[^a-z]+")


def str_diff(a: str, b: str, max_diff: int) -> int:
    """
    Edit distance between two normalised texts.

    Anything further away than `max_diff` comes back as `max_diff + 1` (the distance computation
    stops early there), so callers just compare against the threshold.
    """
    return Levenshtein.distance(
        ALPH.sub("", a.lower()),
        ALPH.sub("", b.lower()),
        score_cutoff=max_diff,
    )


class MatchRunner(Runner):
    """Runner that keeps matching cache entries to DESTinY IDs and checks plausibility of candidates."""

    def should_enhance(self, cache_entry: ParsedRecord, reference: ODSRecord) -> bool:
        """Submittable if following rules hold true: (abstract is missing in repository or not matching known abstract) and publication year +/-N."""
        if cache_entry.abstract is None or not self.settings.min_abstract_length <= len(cache_entry.abstract) < self.settings.max_abstract_length:
            # Current record has no abstract, or it is too short/too long to be worth submitting
            return False

        if reference.destiny_id is None:
            # Missing ODS ID //  We should not end up here, this check is just in case...
            return False

        if (
            cache_entry.publication_year is not None
            and reference.publication_years is not None
            and not any(abs(cache_entry.publication_year - py) <= self.settings.publication_year_tolerance for py in reference.publication_years)
        ):
            # None of the known publication years is within the tolerance
            return False

        if (
            cache_entry.title is not None
            and reference.titles is not None
            and not any(  # using inverted logic with `not any (<=)` so we don't always have to iterate the entire list as with `all (>)`
                title is not None and str_diff(cache_entry.title, title, self.settings.max_title_slop) <= self.settings.max_title_slop
                for title in reference.titles
            )
        ):
            # None of the known titles are below the similarity threshold
            return False

        # We have abstracts to compare to
        has_good_enough_abstract = reference.abstracts is not None and any(
            # Existing abstract is valid (exists and within length bounds)
            abstract is not None
            and self.settings.min_abstract_length <= len(abstract) < self.settings.max_abstract_length
            and (
                # Mode 1) Existing abstract is "good enough" -> don't enhance
                not self.settings.replace_abstracts_on_mismatch
                # Mode 2) Abstracts are similar (with slop) -> don't enhance
                or str_diff(cache_entry.abstract, abstract, self.settings.max_abstract_slop) <= self.settings.max_abstract_slop
            )
            for abstract in reference.abstracts
        )
        # If so, there is no need to submit this abstract as an enhancement, we have a good enough or similar abstract already in the repository;
        # if all checks passed, we can submit this abstract enhancement to the repository
        return not has_good_enough_abstract

    async def _loop_task(self) -> None:
        """Process single batch of enhancements."""
        # Get eligible entries from the references database
        cache_entries = await self.store.get_next_request_batch()

        if len(cache_entries) == 0:
            self.loop_logger.info("No unprocessed cache entries found.")
            return

        # Query repository for reference entries (one cache entry can match several references, and vice versa)
        matched_destiny_references = await self.repository.query_repository(cache_entries=cache_entries)

        # Filter results
        filtered_references = [
            (cache_entry, reference)
            for cache_entry, reference in matched_destiny_references
            if self.should_enhance(cache_entry=cache_entry, reference=reference)
        ]

        # Request to enhance references (several cache entries can point at the same reference, so ask about each one only once)
        requested_references = [(cache_entry, reference) for cache_entry, reference in filtered_references if reference.destiny_id is not None]
        if destiny_ids := list(dict.fromkeys(reference.destiny_id for _cache_entry, reference in requested_references if reference.destiny_id is not None)):
            try:
                # The repository client is synchronous: keep its HTTP call off the event loop
                await asyncio.to_thread(self.repository.request_to_enhance, destiny_ids=destiny_ids)
            except Exception as e:  # noqa: BLE001 - the match results below must be persisted even if the request did not go out
                # The pairs keep their `abstract_enhancement_required` flag without `enhancement_requested`, marking them for a re-request later
                self.loop_logger.warning(
                    f"Enhancement request for {len(destiny_ids):,} reference(s) failed ({e}); "
                    f"persisting the batch without it, the references stay required-but-unrequested."
                )
                requested_references = []

        await self.store.persist_match_results(
            cache_entries=cache_entries,
            matched_references=matched_destiny_references,
            filtered_references=filtered_references,
            requested_references=requested_references,
        )

        self.total_entries_processed += len(cache_entries)
        self.loop_logger.info(
            f"[Total: {self.total_entries_processed} entries] "
            f"Tested {len(cache_entries):,} cache entries that "
            f"matched to {len(matched_destiny_references)} references of which "
            f"{len(filtered_references)} were eligible.",
        )
