"""Utility class for interacting with the repository."""

from typing import TYPE_CHECKING

import httpx
from destiny_sdk.client import KeycloakOAuthMiddleware, OAuthClient, RobotClient
from destiny_sdk.enhancements import (
    AbstractContentEnhancement,
    AbstractProcessType,
    Enhancement,
)
from destiny_sdk.identifiers import (
    ExternalIdentifierType,
    IdentifierLookup,
    OpenAlexIdentifier,
)
from destiny_sdk.references import Reference
from destiny_sdk.robots import (
    EnhancementRequestIn,
    RobotEnhancementBatch,
    RobotEnhancementBatchResult,
)
from destiny_sdk.visibility import Visibility

from .types import Record, flatten_reference

if TYPE_CHECKING:
    import logging
    import uuid

    from .config import Settings


class Repository:
    """Utility class for interacting with the repository."""

    def __init__(self, settings: Settings, logger: logging.Logger) -> None:
        """Initialise the repository utils."""
        if settings.robot_secret is None or settings.keycloak_id is None or settings.keycloak_secret is None:
            raise ValueError

        self.logger = logger
        self.settings = settings

        self.robot_client = RobotClient(
            settings.base_url,
            settings.robot_secret.get_secret_value(),
            settings.robot_id,
        )
        self.blob_client = httpx.AsyncClient()

        self.repo_client = OAuthClient(
            settings.base_url,
            KeycloakOAuthMiddleware(
                settings.keycloak_url,
                settings.keycloak_realm,
                settings.keycloak_id,
                settings.keycloak_secret,
            ),
        )

    async def query_repository(self, cache_entries: list[Record]) -> list[tuple[Record, Record]]:
        """
        Get DESTinY references for the given PIK works, matching by OpenAlex ID.

        The API call passes IDs via URL which is the limiting factor on batch size.
        """
        entries = {entry.openalex_id: entry for entry in cache_entries if entry.openalex_id is not None}

        query: list[str | IdentifierLookup] = [
            IdentifierLookup.from_identifier(
                OpenAlexIdentifier(
                    identifier=openalex_id,
                    identifier_type=ExternalIdentifierType.OPEN_ALEX,
                ),
            )
            for openalex_id in entries
        ]
        references = self.repo_client.lookup(query, timeout=60)

        matched_references = []
        for reference in references:
            flat_reference = flatten_reference(reference)
            if flat_reference.openalex_id is not None and flat_reference.openalex_id in entries:
                matched_references.append((entries[flat_reference.openalex_id], flat_reference))

        self.logger.debug(f"Queried for {len(entries):,} OpenAlex IDs, received {len(references):,} references of which {len(matched_references):,} matched")

        return matched_references

    def request_to_enhance(self, destiny_ids: list[uuid.UUID]) -> None:
        """Ask repository if we can provide enhancements for these IDs."""
        response = self.repo_client.get_client().post(
            "/enhancement-requests/",
            json=EnhancementRequestIn(
                robot_id=self.settings.robot_id,
                reference_ids=destiny_ids,
                source=self.settings.repository_provenance,
            ).model_dump(mode="json"),
        )
        response.raise_for_status()

    async def get_next_batch(
        self,
    ) -> tuple[RobotEnhancementBatch | None, list[Reference] | None]:
        """Ask repository which references it wants enhancements for."""
        batch_info = self.robot_client.poll_robot_enhancement_batch(
            robot_id=self.settings.robot_id,
            limit=self.settings.fulfil_batch_size,
        )
        if batch_info is None:
            return None, None

        response = await self.blob_client.get(str(batch_info.reference_storage_url))
        response.raise_for_status()
        references = [Reference.from_jsonl(line) for line in response.text.splitlines() if line.strip()]

        if len(references) == 0:
            return None, None

        return batch_info, references

    async def submit_enhancements(self, batch_info: RobotEnhancementBatch, cache_entries: list[Record]) -> None:
        """Submit enhancements to repository."""
        enhancements = self._records_to_enhancements(cache_entries)
        await self._upload_enhancements(
            target_url=str(batch_info.result_storage_url),
            jsonl_enhancements=enhancements,
        )
        self._finalise_enhancement_batch(batch_info.id)

    def _records_to_enhancements(self, records: list[Record]) -> bytes:
        file_content = b""
        for record in records:
            if record.destiny_id is None or record.abstract is None:
                raise ValueError("Cache entry is missing destiny ID or abstract!")

            abstract_enhancement = Enhancement(
                reference_id=record.destiny_id,
                source=f"{self.settings.repository_provenance} ({record.source or 'OTHER'})",
                visibility=Visibility.RESTRICTED,
                robot_version=self.settings.robot_version,
                content=AbstractContentEnhancement(
                    abstract=record.abstract,
                    process=AbstractProcessType.OTHER,
                ),
            )
            file_content += (abstract_enhancement.to_jsonl() + "\n").encode("utf-8")

        return file_content

    async def _upload_enhancements(self, target_url: str, jsonl_enhancements: bytes) -> None:
        response = await self.blob_client.put(
            target_url,
            content=jsonl_enhancements,
            headers={
                "Content-Type": "application/jsonl",
                "x-ms-blob-type": "BlockBlob",
                "Content-Length": str(len(jsonl_enhancements)),
            },
        )
        response.raise_for_status()

    def _finalise_enhancement_batch(self, batch_id: uuid.UUID) -> None:
        self.robot_client.send_robot_enhancement_batch_result(
            RobotEnhancementBatchResult(
                request_id=batch_id,
                error=None,
            ),
        )
