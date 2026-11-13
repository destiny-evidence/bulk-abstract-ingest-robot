"""
Central logging setup for the FastAPI app and the TaskIQ workers.

Both entry points (``uv run fastapi dev`` and ``uv run taskiq worker``) call
:func:`init_logging` on startup. It applies a standard logging configuration
loaded from the JSON dictConfig file ``AAI_LOG_CONFIG`` (default:
``.config/logging.json``). The root level is additionally taken from
``AAI_LOGLEVEL`` (``settings.loglevel``), so it can be raised or lowered per
run without editing the file. If the file is missing, a sensible fallback is
used instead.
"""

import json
import logging
import logging.config
from typing import TYPE_CHECKING, Any

from .config import get_settings

if TYPE_CHECKING:
    from pathlib import Path

settings = get_settings()

DEFAULT_LOG_FORMAT = "%(asctime)s [%(levelname)s] %(name)s (%(process)d): %(message)s"
DEFAULT_TASK_LOGGER_NAME = "abstract-ingest-robot"

# Third-party loggers that are too chatty at the default level; used when the
# config file cannot be found (the file itself configures them as well).
NOISY_LOGGERS = (
    "azure",
    "httpcore",
    "httpx",
    "matplotlib",
    "sqlalchemy",
    "urllib3",
)


def _load_config_file(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as fp:
        config: dict[str, Any] = json.load(fp)
    return config


def init_logging(default_level: int | str | None = None) -> None:
    """
    Configure root logging from the JSON config file (with fallback).

    Call once per process, as early as possible. Safe to call again: the
    config file fully replaces the previous configuration.
    """
    level = default_level or settings.loglevel
    config_file = settings.log_config
    if config_file.is_file():
        logging.config.dictConfig(_load_config_file(config_file))
        logging.getLogger().setLevel(level)
    else:
        logging.basicConfig(
            format=DEFAULT_LOG_FORMAT,
            force=True,  # Overrides any existing handlers set earlier other libraries
            level=level,
        )
        for name in NOISY_LOGGERS:
            logging.getLogger(name).setLevel(logging.WARNING)
