"""Runner that keeps matching cache entries to DESTinY IDs and checks plausibility of candidates."""

import difflib
import re

from app.db.schemas import ODSRecord, ParsedRecord
from app.util import Runner

ALPH = re.compile(r"[^a-z]+")


class MatchRunner(Runner):
    """Runner that keeps matching cache entries to DESTinY IDs and checks plausibility of candidates."""

    def should_enhance(self, cache_entry: ParsedRecord, reference: ODSRecord) -> bool:
        """Submittable if following rules hold true: (abstract is missing in repository or not matching known abstract) and publication year +/-N."""
        if cache_entry.abstract is None or len(cache_entry.abstract) < self.settings.min_abstract_length:
            # Empty and short abstracts are not good //  We should not end up here, this check is just in case...
            return False

        if reference.destiny_id is None:
            # Missing DESTinY repository ID //  We should not end up here, this check is just in case...
            return False

        if (
            cache_entry.publication_year is not None
            and reference.publication_years is not None
            and not any(abs(cache_entry.publication_year - py) <= self.settings.publication_year_tolerance for py in reference.publication_years)
        ):
            # None of the known publication years is within the tolerance
            return False

        if cache_entry.title is not None and reference.titles is not None:
            cached_title = ALPH.sub("", cache_entry.title.lower())
            if not any(  # using inverted logic with `not any (<=)` so we don't always have to iterate the entire list as with `all (>)`
                (
                    title is not None
                    and len(list(difflib.ndiff(cached_title, ALPH.sub("", title.lower())))) - len(cached_title) <= self.settings.max_title_slop
                    for title in reference.titles
                ),
            ):
                # None of the known titles are below the similarity threshold
                return False

        if cache_entry.abstract is not None and reference.abstracts is not None and len(cache_entry.abstract) > self.settings.min_abstract_length:
            cached_abstract = ALPH.sub("", cache_entry.abstract.lower())
            if not any(
                (
                    abstract is not None
                    and len(abstract) > self.settings.min_abstract_length
                    and len(list(difflib.ndiff(cached_abstract, ALPH.sub("", abstract.lower())))) - len(cached_abstract) <= self.settings.max_abstract_slop
                    for abstract in reference.abstracts
                ),
            ):
                # No need to submit this abstract as an enhancement, we have a similar abstract already in the repository
                return False

        # All checks passed, we can submit this abstract enhancement to the repository
        return True

    async def _loop_task(self) -> None:
        """Process single batch of enhancements."""
        # Get eligible entries from the references database
        cache_entries = await self.store.get_next_request_batch()

        if len(cache_entries) == 0:
            self.loop_logger.info("No unprocessed cache entries found.")
            await self.stop()
            return

        # Query repository for reference entries
        matched_destiny_references = await self.repository.query_repository(cache_entries=cache_entries)

        matched_cache_entries = [cache_entry for cache_entry, _reference in matched_destiny_references]

        # Filter results
        filtered_references = [
            (cache_entry, reference)
            for cache_entry, reference in matched_destiny_references
            if self.should_enhance(cache_entry=cache_entry, reference=reference)
        ]

        # Request to enhance references
        if destiny_ids := [reference.destiny_id for _cache_entry, reference in filtered_references if reference.destiny_id is not None]:
            self.repository.request_to_enhance(
                destiny_ids=destiny_ids,
            )

        requested_cache_entries = [submittable_cache_entry for submittable_cache_entry, reference in filtered_references if reference.destiny_id is not None]

        await self.store.persist_match_results(
            cache_entries=cache_entries,
            matched_cache_entries=matched_cache_entries,
            filtered_references=filtered_references,
            requested_cache_entries=requested_cache_entries,
        )

        self.total_entries_processed += len(cache_entries)
        self.loop_logger.info(
            f"[Total: {self.total_entries_processed} entries] "
            f"Tested {len(cache_entries):,} cache entries that "
            f"matched to {len(matched_destiny_references)} references of which "
            f"{len(filtered_references)} were eligible.",
        )
