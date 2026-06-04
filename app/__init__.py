"""Main robot entry point."""

import asyncio
from enum import Enum
from typing import Annotated

import typer

from .enhance import EnhancementRunner
from .match import MatchRunner


class RunnerTask(str, Enum):  # noqa: UP042
    """Enum for types of runners."""

    enhance = "enhance"
    match = "match"


def main(
    task: RunnerTask,
    loglevel: Annotated[str, typer.Option(help="Basic loglevel for base-logger")] = "INFO",
    debug_database: Annotated[bool, typer.Option(help="Verbose database logs from sqlalchemy")] = False,
) -> None:
    """Start runner for selected `task`."""
    if task == RunnerTask.enhance:
        Runner = EnhancementRunner  # noqa: N806
    elif task == RunnerTask.match:
        Runner = MatchRunner  # noqa: N806
    else:
        raise ValueError(f"Unknown runner type: {task}")

    async def _main() -> None:
        runner = Runner(name=task.value, loglevel=loglevel, db_debug=debug_database)
        await runner.start()

    asyncio.run(_main())
