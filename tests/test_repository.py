"""
Tests for the identifier lookup that hands cached records to the references the repository holds for them.

Only `Repository.__init__` talks to the outside world, so the tests build the helper with a bypassed `__init__`
(no secrets, no auth clients) and replace `repo_client` with a double whose synchronous `.lookup` plays the API:
it hands back whichever offered references carry one of the identifiers it was asked about.
Multiplicity is the point of these tests - the repository can hold several unmerged references for one identifier,
and our own cache can hold several records for it, so the result is one pair per (record, reference).
"""

import asyncio
import logging
from collections.abc import Sequence
from unittest.mock import MagicMock
from uuid import UUID, uuid4

import pytest
from destiny_sdk.identifiers import DOIIdentifier, ExternalIdentifierType, IdentifierLookup, OpenAlexIdentifier
from destiny_sdk.references import Reference

from app.db.schemas import ODSRecord, ParsedRecord
from app.util.config import Settings
from app.util.repository import HTTP_TIMEOUT_SECONDS, Repository

from .util.fake_data import cache_entry

DOI_ONE = "10.1000/one"
DOI_TWO = "10.1000/two"
DESTINY_ONE = uuid4()
DESTINY_TWO = uuid4()


def reference_with_doi(destiny_id: UUID, doi: str) -> Reference:
    """A reference as the lookup API returns it: the identifier it is filed under and nothing else."""
    return Reference(id=destiny_id, identifiers=[DOIIdentifier(identifier=doi)])


def cached(doi: str | None, label: str) -> ParsedRecord:
    """
    A cached record carrying `doi`, with `label` as its title so a test can tell two records with the same DOI apart
    (the title plays no part in the lookup itself).
    """
    return cache_entry(doi=doi, title=label)


def as_pairs(pairs: Sequence[tuple[ParsedRecord, ODSRecord]]) -> list[tuple[str | None, UUID | None]]:
    """The result reduced to (record label, matched reference ID) - the only two things a test needs to tell apart."""
    return [(record.title, reference.destiny_id) for record, reference in pairs]


def references_asked_about(client: MagicMock) -> list[str]:
    """Every identifier the helper put into a lookup request, across its batches."""
    return [lookup.identifier for call in client.lookup.call_args_list for lookup in call.args[0]]


@pytest.fixture
def repository_factory(settings: Settings):
    """Build the lookup helper on the shared settings, with a double that plays the repository API and remembers what it was asked."""

    def make(*offered: Reference, always_returned: Sequence[Reference] = ()) -> tuple[Repository, MagicMock]:
        repo: Repository = Repository.__new__(Repository)  # no secrets, so no auth clients and no HTTP
        repo.logger = logging.getLogger("test")
        repo.settings = settings  # the fixture's own object, so a test that changes a setting before building is heard

        def play_the_api(batch: list[IdentifierLookup], timeout: int | None = None) -> list[Reference]:  # noqa: ARG001
            """The API returns the references filed under the asked identifiers, plus whatever else it likes (the timeout is taken and ignored)."""
            asked = {lookup.identifier for lookup in batch}
            hits = [reference for reference in offered if asked & {identifier.identifier for identifier in reference.identifiers or []}]
            return hits + list(always_returned)

        client = MagicMock()
        client.lookup.side_effect = play_the_api
        repo.repo_client = client
        return repo, client

    return make


class TestMatchingRecordsToReferences:
    def test_pairs_each_record_with_the_reference_for_its_identifier(self, repository_factory):
        repo, _ = repository_factory(reference_with_doi(DESTINY_ONE, DOI_ONE))
        matched, unmatched = cached(DOI_ONE, "known to the repository"), cached(DOI_TWO, "unknown to the repository")

        result = asyncio.run(repo.query_repository([matched, unmatched]))

        # only the record whose identifier the repository knows about goes on to the next stage
        assert as_pairs(result) == [("known to the repository", DESTINY_ONE)]
        assert (result[0][1].destiny_id, result[0][1].doi) == (DESTINY_ONE, DOI_ONE)  # the reference arrives flattened

    def test_gives_the_reference_to_every_record_sharing_its_identifier(self, repository_factory):
        """
        Two exports can carry the same work under one DOI, and both records are queued separately.

        Handing the reference to only one of them would leave the other unmatched while still being marked evaluated,
        so it would never be enhanced and would end up purged - which is why all of them have to come back.
        """
        repo, _ = repository_factory(reference_with_doi(DESTINY_ONE, DOI_ONE))

        result = asyncio.run(repo.query_repository([cached(DOI_ONE, "first copy"), cached(DOI_ONE, "second copy")]))

        assert as_pairs(result) == [("first copy", DESTINY_ONE), ("second copy", DESTINY_ONE)]

    def test_gives_a_record_a_pair_for_each_of_its_references(self, repository_factory):
        """An unmerged work sits in two references under the same DOI: both matches belong in the match table."""
        repo, _ = repository_factory(reference_with_doi(DESTINY_ONE, DOI_ONE), reference_with_doi(DESTINY_TWO, DOI_ONE))

        result = asyncio.run(repo.query_repository([cached(DOI_ONE, "one record")]))

        assert as_pairs(result) == [("one record", DESTINY_ONE), ("one record", DESTINY_TWO)]

    def test_leaves_behind_records_without_the_matched_identifier(self, repository_factory):
        repo, client = repository_factory(reference_with_doi(DESTINY_ONE, DOI_ONE))

        result = asyncio.run(repo.query_repository([cached(None, "no doi"), cached(DOI_ONE, "has a doi")]))

        assert as_pairs(result) == [("has a doi", DESTINY_ONE)]
        assert references_asked_about(client) == [DOI_ONE]  # the record without a DOI is not even asked about

    def test_ignores_references_it_was_not_asked_about(self, repository_factory):
        """An API response is not obliged to hold only what we asked for: a reference nobody queried must not match any record."""
        unasked = reference_with_doi(DESTINY_ONE, "10.1000/unasked")
        identifierless = Reference(id=DESTINY_TWO)
        repo, _ = repository_factory(always_returned=[unasked, identifierless])

        assert asyncio.run(repo.query_repository([cached(DOI_ONE, "one record")])) == []


class TestIdentifierType:
    def test_matches_on_openalex_ids_when_configured(self, repository_factory, settings):
        settings.match_identifier = ExternalIdentifierType.OPEN_ALEX
        repo, client = repository_factory(Reference(id=DESTINY_ONE, identifiers=[OpenAlexIdentifier(identifier="W2741809807")]))
        entry = cache_entry(openalex_id="W2741809807", title="one record")

        result = asyncio.run(repo.query_repository([entry]))

        assert as_pairs(result) == [("one record", DESTINY_ONE)]
        assert references_asked_about(client) == ["W2741809807"]

    def test_reports_an_unimplemented_identifier_type(self, repository_factory, settings):
        settings.match_identifier = ExternalIdentifierType.ERIC  # nothing in the cache schema or the SDK pairs with this one
        repo, _ = repository_factory()

        with pytest.raises(NotImplementedError, match="not implemented"):
            asyncio.run(repo.query_repository([cached(DOI_ONE, "one record")]))


class TestLookupRequests:
    def test_asks_the_repository_for_each_identifier_only_once(self, repository_factory):
        """
        The identifiers go into the request URL, so a duplicated identifier costs a longer request for nothing.

        Asking once still has to reach both records: the deduplication is on the question, not on the answer.
        """
        repo, client = repository_factory(reference_with_doi(DESTINY_ONE, DOI_ONE), reference_with_doi(DESTINY_TWO, DOI_TWO))

        asyncio.run(repo.query_repository([cached(DOI_ONE, "a"), cached(DOI_ONE, "b"), cached(DOI_TWO, "c")]))

        assert sorted(references_asked_about(client)) == sorted([DOI_ONE, DOI_TWO])

    def test_splits_the_identifiers_into_batches(self, repository_factory, settings):
        settings.repository_lookup_batch_size = 2
        repo, client = repository_factory()
        entries = [cached(f"10.1000/{n}", f"record {n}") for n in range(5)]

        asyncio.run(repo.query_repository(entries))

        # batches may go out concurrently, so only the sizes are a stable assertion
        assert sorted((len(call.args[0]) for call in client.lookup.call_args_list), reverse=True) == [2, 2, 1]

    def test_stays_off_the_phone_without_any_identifier(self, repository_factory):
        """A batch of records that carry no matchable identifier should not reach the API at all."""
        repo, client = repository_factory(reference_with_doi(DESTINY_ONE, DOI_ONE))

        assert asyncio.run(repo.query_repository([cached(None, "no identifier")])) == []
        assert client.lookup.call_count == 0

    def test_gives_the_lookup_the_http_timeout(self, repository_factory):
        """Reference payloads come back big, so the lookup runs on the helper's own generous timeout."""
        repo, client = repository_factory()

        asyncio.run(repo.query_repository([cached(DOI_ONE, "one record")]))

        assert client.lookup.call_args.kwargs == {"timeout": HTTP_TIMEOUT_SECONDS}
