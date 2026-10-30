from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Annotated, Any
from uuid import UUID, uuid6

from pydantic import StringConstraints
from sqlalchemy import Column, DateTime, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlmodel import Field, SQLModel
from sqlmodel.main import SQLModelConfig  # type: ignore[attr-defined]


class Format(str, Enum):
    SCOPUS_API = "scopus_api"
    WOS_API = "wos_api"
    PUBMED_API = "pubmed_api"
    DIMENSIONS_API = "dimensions_api"
    OPENALEX_API = "openalex_api"
    PIK_SOLR = "pik_solr"


class Source(str, Enum):
    OTHER = "OTHER"
    SCOPUS = "SCOPUS"
    WOS = "WOS"
    PUBMED = "PUBMED"
    DIMENSIONS = "DIMENSIONS"
    OPENALEX = "OPENALEX"


OpenAlexID = Annotated[str, StringConstraints(pattern=r"^W\d+$", to_upper=True)]
DOI = Annotated[str, StringConstraints(pattern=r"^10\.\d{4,9}/[-._;()/#:A-Za-z0-9]+$", to_lower=True)]


class RecordIdentifiers(SQLModel):
    record_id: UUID = Field(default_factory=uuid6, primary_key=True, description="Internal identifier")
    destiny_id: UUID | None = Field(default=None, index=True, description="ID in ODS if available and matched successfully")

    openalex_id: OpenAlexID | None = Field(default=None, index=True)  # Must be W123456789 format
    doi: DOI | None = Field(default=None, index=True)  # Expecting no URL prefixes, starting with 10.*

    wos_id: str | None = Field(default=None, index=True)
    scopus_id: str | None = Field(default=None, index=True)
    s2_id: str | None = Field(default=None, index=True)
    pubmed_id: str | None = Field(default=None, index=True)
    dimensions_id: str | None = Field(default=None, index=True)


class _TrackingMixin(SQLModel):
    created_at: datetime = Field(
        default_factory=datetime.now,
        sa_column=Column(
            DateTime,
            server_default=text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
    )

    has_eligible_abstract: bool | None = Field(default=None, index=True, description="Set to false if record failed eligibility rule (i.e. abstract too short)")
    evaluated: bool = Field(default=False, index=True, description="Set to true if record was evaluated against ODS")
    found_destiny_reference: bool = Field(default=False, index=True, description="Set to true if we found a matching reference in ODS for this record")
    abstract_enhancement_required: bool = Field(default=False, index=True, description="Set to true if ODS requires an abstract enhancement from this record")
    enhancement_requested: bool = Field(default=False, index=True, description="Set to true if enhancement request was sent to ODS")
    enhancement_submitted: bool = Field(default=False, index=True, description="Set to true if enhancement was submitted to ODS")
    encountered_parsing_error: bool = Field(default=False, index=True, description="Set to true if any of the processing of the raw data failed")


class _RawMixin(SQLModel):
    # Source of this record (to inform the shape of the `raw` field)
    format: Format | None = None
    raw: dict[str, Any] = Field(
        default_factory=dict,
        sa_column=Column("raw", JSONB, nullable=False),
    )


class _BiblioMixin(SQLModel):
    title: str | None = None
    abstract: str | None = None
    publication_year: int | None = None
    source: Source = Source.OTHER


class _MultiBiblioMixin(SQLModel):
    titles: list[str] | None = None
    abstracts: list[str] | None = None
    publication_years: list[int] | None = None


class Record(RecordIdentifiers, _RawMixin, _TrackingMixin, table=True):
    __tablename__ = "records"
    model_config = SQLModelConfig(validate_default=True, extra="ignore")


class ParsedRecord(RecordIdentifiers, _BiblioMixin, _TrackingMixin):
    model_config = SQLModelConfig(validate_default=True, extra="ignore")

    @classmethod
    def _parse_record_kwargs(cls, record: Record) -> dict[str, Any]:
        if record.format == Format.SCOPUS_API:
            py: int | None = None
            py_: str | None = record.raw.get("prism:coverDate") or record.raw.get("prism:coverDisplayDate")
            if py_ and len(py_) >= 4:  # noqa: PLR2004
                py = int(py_[:4])

            scopus_id = record.raw.get("eid") or record.raw.get("dc:identifier")
            if scopus_id and record.scopus_id and scopus_id != record.scopus_id:
                raise AssertionError(f"Scopus IDs do not align: {scopus_id} != {record.scopus_id}!")

            doi = record.raw.get("prism:doi")
            if doi and record.doi and doi != record.doi:
                raise AssertionError(f"DOIs do not align: {doi} != {record.doi}!")

            return {
                "title": record.raw.get("dc:title"),
                "abstract": record.raw.get("dc:description"),
                "publication_year": py,
                "scopus_id": record.scopus_id or scopus_id,
                "doi": record.doi or doi,
                "source": Source.SCOPUS,
                **record.model_dump(
                    exclude_defaults=True, exclude={"scopus_id", "doi", "format", "raw"}
                ),  # `format` and `raw` are set by `ParsedRecordExtra.from_record` separately
            }

        raise NotImplementedError(f"No parser for format {record.format}")

    @classmethod
    def from_record(cls, record: Record) -> ParsedRecord:
        kwargs = cls._parse_record_kwargs(record)
        return cls(**kwargs)


class ParsedRecordExtra(ParsedRecord, _RawMixin):
    """Model containing raw source data and parsed values in a single entity."""

    model_config = SQLModelConfig(validate_default=True, extra="ignore")

    @classmethod
    def from_record(cls, record: Record) -> ParsedRecordExtra:
        kwargs = cls._parse_record_kwargs(record)
        return cls(
            **kwargs,
            format=record.format,
            raw=record.raw,
        )


class ODSRecord(RecordIdentifiers, _MultiBiblioMixin, _TrackingMixin):
    model_config = SQLModelConfig(validate_default=True, extra="ignore")


class SearchIngest(SQLModel, table=True):
    __tablename__ = "search_ingest"
    ingest_id: UUID = Field(default_factory=uuid6, primary_key=True)
    azure_link: str = Field(
        nullable=False,
        description="Link with auth key to result file in azure blob storage",
    )
    is_queueing: bool = Field(
        default=False,
        nullable=False,
        description="Status on transfer to ingestion queue (started transfer file to queue)",
    )
    is_queued: bool = Field(
        default=False,
        nullable=False,
        description="Status on transfer to ingestion queue (completed transfer file to queue)",
    )
    checkpoint_bytes: int | None = Field(
        default=None,
        nullable=True,
        description="Offset in bytes from the latest commit to the queue (restart from here on failure)",
    )
    checkpoint_rows: int | None = Field(
        default=None,
        nullable=True,
        description="Number of lines read from the latest commit to the queue",
    )


__all__ = [
    "DOI",
    "Format",
    "ODSRecord",
    "OpenAlexID",
    "ParsedRecord",
    "ParsedRecordExtra",
    "Record",
    "RecordIdentifiers",
    "SearchIngest",
    "Source",
]
