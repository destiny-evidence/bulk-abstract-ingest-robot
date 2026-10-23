"""API config parsing and model."""

import logging
import tomllib
from enum import StrEnum
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Any
from uuid import UUID

from pydantic import AfterValidator, Field, HttpUrl, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Environment(StrEnum):
    """
    Environment that the toy robot is running in.

    As this robot is for demo purposes only, we do not accept `production` as a value

    **Allowed values**:
    - `local`: The robot is running locally
    - `development`: The robot is running in development
    - `staging`: The robot is running in staging
    - `test`: The robot is running as a test fixture for the repository
    """

    LOCAL = "local"
    DEVELOPMENT = "development"
    STAGING = "staging"
    TEST = "test"


def read_toml_value(path_to_toml: str | Path, *path: str) -> str:
    """Read the information from the pyproject.toml."""
    with open(path_to_toml, "rb") as toml_file:  # noqa: PTH123
        current_node: Any | None = tomllib.load(toml_file)
        steps = ""
        for step in path:
            if type(current_node) is not dict:
                raise ValueError(f"Cannot follow `step` after `{steps}` in {path_to_toml}")

            steps += f".{step}"

            if not (current_node := current_node.get(step, None)):
                raise ValueError(f"`{steps}` not present in {path_to_toml}")

        if type(current_node) is not str:
            raise ValueError(
                f"{steps} did not lead to singular string value in {path_to_toml}",
            )

        return current_node


class Settings(BaseSettings):
    """Settings model for polling robot."""

    model_config = SettingsConfigDict(
        env_file=(".env.secret", ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    log_config: Path = Path(".config/logging.json")
    loglevel: int = logging.DEBUG

    robot_secret: SecretStr = Field(
        description="Secret needed for communicating with destiny repo.",
    )
    robot_id: UUID = Field(
        description="Client id needed for communicating with destiny repository.",
    )

    robot_version: str = Field(
        default=read_toml_value("pyproject.toml", "project", "version"),
        pattern="[0-9]+.[0-9]+.[0-9]+",
        description="Semantic version of the robot",
    )

    robot_name: str = Field(
        default=read_toml_value("pyproject.toml", "project", "name"),
        pattern="([a-z]+-)+",
        description="Name of the robot",
    )

    base_url: HttpUrl = Field(
        default=HttpUrl("https://api.staging.evidence-repository.org"),
        description="DESTinY repository API endpoint",
    )

    env: Environment = Field(
        default=Environment.STAGING,
        description="The environment the toy robot is deployed in.",
    )

    match_interval_seconds: int = Field(
        default=30,
        description="How long to sleep between match requests (seconds)",
    )
    enhance_interval_seconds: int = Field(
        default=30,
        description="How long to sleep between enhancement batches (seconds)",
    )
    queue_interval_seconds: int = Field(
        default=3600,  # defaults to checking once per hour
        description="How long to sleep between checking for new files to transfer to ingest queue (seconds)",
    )
    request_batch_size: int = Field(
        default=10,
        description="The number of references to include per enhancement batch",
    )
    fulfil_batch_size: int = Field(
        default=10,
        description="The number of references to include per enhancement batch",
    )
    repository_lookup_batch_size: int = Field(
        default=100,
        ge=1,
        le=100,
        description="Maximum number of identifiers to include in a single repository lookup request",
    )
    repository_lookup_concurrency: int = Field(
        default=1,
        ge=1,
        description="Maximum number of concurrent repository lookup requests.",
    )

    keycloak_id: str | None = Field(default=None, description="keycloak client id")
    keycloak_secret: SecretStr | None = Field(
        default=None,
        description="keycloak client secret",
    )
    keycloak_url: str = Field(
        default="https://auth.evidence-repository.org/",
        description="keycloak authentication url",
    )
    keycloak_realm: str = Field(default="destiny", description="keycloak realm")

    db_user: str = Field(
        default="pguser",
        description="references database user",
    )
    db_password: str = Field(
        default="password",
        description="references database password",
    )
    db_host: str = Field(
        default="localhost",
        description="references database host",
    )
    db_port: int = Field(
        default=5432,
        description="references database port",
    )
    db_name: str = Field(
        default="references",
        description="references database name",
    )

    publication_year_tolerance: int = Field(
        default=2,
        description=(
            "When we have publication year information in the repository and the cached data, "
            "the years are allowed to be this many years apart (2025 vs 2026 counting as 1 year apart)"
        ),
    )
    min_abstract_length: int = Field(
        default=200,
        description="Minimum length of abstract that we might consider for submitting to the repository",
    )
    max_abstract_slop: int = Field(
        default=100,
        description="Threshold for edit distance between abstracts to consider them identical",
    )
    max_title_slop: int = Field(
        default=10,
        description="Threshold for edit distance between titles to consider them identical",
    )
    repository_provenance: str = Field(
        default="PIK Metadata Cache",
        description="How do we want to call this abstract enhancement source",
    )

    api_wrapper_endpoint: Annotated[str, AfterValidator(lambda v: v.rstrip("/"))] = Field(
        default="http://localhost:8000/api",
        description="Public REST API of the academic-api tool without trailing slash",
    )
    api_wrapper_api_key: str = Field(
        default="very-secret",
        description="Authentication key for the academic-api tool API",
    )
    api_wrapper_checkpoint_interval: int = Field(
        default=1000,
        description="Number of rows to process before submitting checkpoint to academic-api tool API.",
    )
    transfer_locks_path: Path = Field(
        default=Path(".locks"),
        description="Directory where to host locks so multiple transfer processes don't work on the same file.",
    )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Get a cached settings object."""
    return Settings()
