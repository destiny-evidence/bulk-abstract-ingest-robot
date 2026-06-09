"""Type definitions and converters."""

from typing import TYPE_CHECKING

from destiny_sdk.enhancements import EnhancementType
from destiny_sdk.identifiers import ExternalIdentifierType
from pydantic import BaseModel

if TYPE_CHECKING:
    import uuid

    from destiny_sdk.references import Reference


class Record(BaseModel):
    """Simplified representation of a reference."""

    record_id: uuid.UUID | None = None
    destiny_id: uuid.UUID | None = None

    openalex_id: str | None = None  # Must be W123456789 format
    doi: str | None = None  # Expecting no URL prefixes, starting with 10.*

    wos_id: str | None = None
    scopus_id: str | None = None
    s2_id: str | None = None
    pubmed_id: str | None = None
    dimensions_id: str | None = None

    source: str | None = None

    abstract: str | None = None
    abstracts: list[str] | None = None

    publication_year: int | None = None
    publication_years: list[int] | None = None


def flatten_reference(reference: Reference) -> Record:
    """Take nested DESTinY `Reference` and transform into (mostly) flat `Record`."""
    record = Record(destiny_id=reference.id)

    for identifier in reference.identifiers or []:
        if identifier.identifier is None or type(identifier.identifier) is not str:
            continue

        if identifier.identifier_type == ExternalIdentifierType.OPEN_ALEX and record.openalex_id is None:
            record.openalex_id = identifier.identifier
        elif identifier.identifier_type == ExternalIdentifierType.DOI and record.doi is None:
            record.doi = identifier.identifier
        elif identifier.identifier_type == ExternalIdentifierType.PM_ID and record.pubmed_id is None:  # type: ignore[comparison-overlap]
            record.pubmed_id = identifier.identifier

    for enhancement in reference.enhancements or []:
        if (
            enhancement.content.enhancement_type == EnhancementType.BIBLIOGRAPHIC
            and record.publication_year is None
            and enhancement.content.publication_year is not None
        ):
            if record.publication_years is None:
                record.publication_years = []
            record.publication_years.append(enhancement.content.publication_year)

        if enhancement.content.enhancement_type == EnhancementType.ABSTRACT:
            if record.abstracts is None:
                record.abstracts = []
            record.abstracts.append(enhancement.content.abstract)

    return record
