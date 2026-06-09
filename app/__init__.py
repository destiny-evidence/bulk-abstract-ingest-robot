"""Main robot entry point."""

import asyncio
from enum import Enum
from typing import TYPE_CHECKING, Annotated

import typer

from .enhance import EnhancementRunner
from .match import MatchRunner
from .util import get_settings

if TYPE_CHECKING:
    from .util import Runner


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
    settings = get_settings()

    SelectedRunner: type[Runner]  # noqa: N806
    loop_interval: int
    if task == RunnerTask.enhance:
        SelectedRunner = EnhancementRunner  # noqa: N806
        loop_interval = settings.enhance_interval_seconds
    elif task == RunnerTask.match:
        SelectedRunner = MatchRunner  # noqa: N806
        loop_interval = settings.match_interval_seconds
    else:
        raise ValueError(f"Unknown runner type: {task}")

    async def _main() -> None:
        runner = SelectedRunner(name=task.value, loglevel=loglevel, db_debug=debug_database, loop_interval_seconds=loop_interval)
        await runner.start()

    asyncio.run(_main())
