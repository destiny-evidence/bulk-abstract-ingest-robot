"""Runner that keeps matching cache entries to DESTinY IDs and checks plausibility of candidates."""

from .util import Record, Runner


class MatchRunner(Runner):
    """Runner that keeps matching cache entries to DESTinY IDs and checks plausibility of candidates."""

    def should_enhance(self, cache_entry: Record, reference: Record) -> bool:
        """Submittable iff following rules hold true: (abstract is missing in repository or not matching known abstract) and publication year +/-N."""
        if cache_entry.abstract is None or len(cache_entry.abstract) < self.settings.min_abstract_length:
            # Empty and short abstracts are not good //  We should not end up here, this check is just in case...
            return False

        if reference.destiny_id is None:
            # Missing DESTinY repository ID //  We should not end up here, this check is just in case...
            return False

        if (
            cache_entry.publication_year is not None
            and reference.publication_years is not None
            and all(abs(cache_entry.publication_year - py) > self.settings.publication_year_tolerance for py in reference.publication_years)
        ):
            # None of the known publication years is within the tolerance
            return False

        if cache_entry.abstract is not None and reference.abstracts is not None:
            for abstract in reference.abstracts:
                if cache_entry.abstract == abstract:
                    # No need to submit this abstract as an enhancement, we have it already in the repository
                    return False

        # All checks passed, we can submit this abstract enhancement to the repository
        return True

    async def _loop_task(self) -> None:
        """Process single batch of enhancements."""
        # Get eligible entries from meta-cache
        cache_entries = await self.store.get_next_request_batch()

        if len(cache_entries) == 0:
            self.loop_logger.info("No unprocessed cache entries found.")
            await self.stop()
            return

        # Query repository for meta-cache entries
        matched_references = await self.repository.query_repository(cache_entries=cache_entries)

        # Filter results
        filtered_references = [
            (cache_entry, reference) for cache_entry, reference in matched_references if self.should_enhance(cache_entry=cache_entry, reference=reference)
        ]

        # Remember DESTinY IDs
        await self.store.write_matches(filtered_references)

        # Request to enhance references
        if destiny_ids := [reference.destiny_id for _cache_entry, reference in filtered_references if reference.destiny_id is not None]:
            self.repository.request_to_enhance(
                destiny_ids=destiny_ids,
            )

        # Remember we queried those
        await self.store.log_request(cache_entries=cache_entries)

        self.loop_logger.info(
            f"Tested {len(cache_entries):,} cache entries that "
            f"matched to {len(matched_references)} references of which"
            f"for {len(filtered_references)} were eligible."
        )
