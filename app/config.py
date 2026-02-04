"""API config parsing and model."""

import logging
import tomllib
from enum import StrEnum
from functools import lru_cache
from pathlib import Path

from destiny_sdk import UUID
from pydantic import Field, HttpUrl
from pydantic_settings import BaseSettings, SettingsConfigDict


def configure_logging() -> None:
    """Configure logging for the application."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    )


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


def read_version_from_toml(path_to_toml: str) -> str:
    """Read the robot version from the pyproject.toml."""
    with Path.open(Path(path_to_toml), "rb") as toml_file:
        toml = tomllib.load(toml_file)
        if not (project := toml.get("project", None)):
            msg = f"Project section not present in {path_to_toml}"
            raise ValueError(msg)
        if not (version := project.get("version", None)):
            msg = f"Robot version not present in {path_to_toml}"
            raise ValueError(msg)
        return version


class Settings(BaseSettings):
    """Settings model for polling robot."""

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")

    robot_secret: str = Field(
        description="Secret needed for communicating with destiny repo.",
    )
    robot_id: UUID = Field(
        description="Client id needed for communicating with destiny repository.",
    )

    robot_version: str = Field(
        default=read_version_from_toml("pyproject.toml"),
        pattern="[0-9]+.[0-9]+.[0-9]+",
        description="Semantic version of the robot",
    )

    destiny_repository_url: HttpUrl

    env: Environment = Field(
        default=Environment.STAGING,
        description="The environment the toy robot is deployed in.",
    )

    poll_interval_seconds: int = Field(
        default=30,
        description=("How often to poll for new robot enhancement batches (seconds)"),
    )

    batch_size: int = Field(
        default=2,
        description=("The number of references to include per enhancement batch"),
    )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Get a cached settings object."""
    return Settings()  # type: ignore[call-arg]
