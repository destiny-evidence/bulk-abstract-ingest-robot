import httpx
from pydantic import BaseModel

from app.util.logging import settings


class SearchIngest(BaseModel):
    ingest_id: str
    query_id: int
    azure_link: str  # Link with auth key to result file in azure blob storage
    is_queueing: bool  # Status on transfer to ingestion queue (started transfer file to queue)
    is_queued: bool  # Status on transfer to ingestion queue (completed transfer file to queue)


async def get_transfer_requests(is_open: bool = True, offset: int = 0, limit: int = 5) -> list[SearchIngest]:
    async with httpx.AsyncClient() as client:
        response = await client.get(
            f"{settings.api_wrapper_endpoint}/ingest/",
            params={
                "only_open": is_open,
                "offset": offset,
                "limit": limit,
            },
            headers={
                "api-token": settings.api_wrapper_api_key,
            },
        )
        return [SearchIngest.model_validate(req) for req in response.json()]


async def mark_queueing(ingest_id: str) -> SearchIngest:
    async with httpx.AsyncClient() as client:
        response = await client.put(
            f"{settings.api_wrapper_endpoint}/ingest/queueing",
            params={
                "ingest_id": ingest_id,
            },
            headers={
                "api-token": settings.api_wrapper_api_key,
            },
        )
        return SearchIngest.model_validate(response.json())


async def mark_queued(ingest_id: str) -> SearchIngest:
    async with httpx.AsyncClient() as client:
        response = await client.put(
            f"{settings.api_wrapper_endpoint}/ingest/queued",
            params={
                "ingest_id": ingest_id,
            },
            headers={
                "api-token": settings.api_wrapper_api_key,
            },
        )
        return SearchIngest.model_validate(response.json())
