"""Main robot entry point."""

import asyncio
import logging
from enum import Enum
from typing import TYPE_CHECKING, Annotated

import typer

from .db.engine import get_engine
from .robots import EnhancementRunner, MatchRunner, IngestionQueueRunner
from .util import get_settings, init_logging

if TYPE_CHECKING:
    from .util import Runner


class RunnerTask(str, Enum):
    """Enum for types of runners."""

    enhance = "enhance"
    match = "match"
    queue = "queue"


def main(
    task: RunnerTask,
    loglevel: Annotated[str, typer.Option(help="Basic loglevel for base-logger")] = "INFO",
) -> None:
    """Start runner for selected `task`."""
    settings = get_settings()
    init_logging()
    logger = logging.getLogger("main")

    SelectedRunner: type[Runner]  # noqa: N806
    loop_interval: int
    if task == RunnerTask.enhance:
        SelectedRunner = EnhancementRunner  # noqa: N806
        loop_interval = settings.enhance_interval_seconds
    elif task == RunnerTask.match:
        SelectedRunner = MatchRunner  # noqa: N806
        loop_interval = settings.match_interval_seconds
    elif task == RunnerTask.queue:
        SelectedRunner = IngestionQueueRunner  # noqa: N806
        loop_interval = settings.queue_interval_seconds
    else:
        raise ValueError(f"Unknown runner type: {task}")

    logger.info(f"Starting process as `{SelectedRunner.__name__}` runner...")

    async def _main() -> None:
        logger.info("Establish database connection and initialise schema if needed...")
        db_engine = get_engine()
        await db_engine.initialise()

        logger.info("Initialising runner...")
        runner = SelectedRunner(name=task.value, loglevel=loglevel, loop_interval_seconds=loop_interval)
        logger.info("Starting runner loop...")
        await runner.start()

    asyncio.run(_main())
