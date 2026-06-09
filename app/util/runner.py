"""Abstract runner class with main loop."""

import asyncio
import contextlib
import signal
import sys
from abc import ABC, abstractmethod
from typing import TYPE_CHECKING

from .config import get_logger, get_settings
from .repository import Repository
from .store import AbstractStore

if TYPE_CHECKING:
    from types import FrameType


class Runner(ABC):
    """Abstract runner class with main loop."""

    def __init__(self, name: str, db_debug: bool = False, loglevel: str | int = "INFO", loop_interval_seconds: int = 0) -> None:
        """Initialise runner."""
        self.settings = get_settings()

        logger = get_logger("abstracts-robot", init_logging=True, base_level=loglevel)
        self.logger = logger.getChild(name)
        self.loop_logger = self.logger.getChild("loop")
        self.loop_interval_seconds = loop_interval_seconds

        self.repository = Repository(settings=self.settings, logger=logger.getChild("repository"))
        self.store = AbstractStore(
            settings=self.settings,
            logger=logger.getChild("store"),
            debug_db=db_debug,
        )
        self.shutdown_event = asyncio.Event()

    @abstractmethod
    async def _loop_task(self) -> None:
        """Perform iteration of runner loop."""
        raise NotImplementedError

    async def _main_loop(self) -> None:
        """Run main loop."""
        loop_logger = self.logger.getChild("loop")
        while True:
            try:
                if self.loop_interval_seconds > 0:
                    # Sleep for graceful API use
                    await asyncio.sleep(self.loop_interval_seconds)

                # Perform unit of work
                await self._loop_task()
            except Exception as e:
                loop_logger.error(f"Encountered an error: {e}")
                loop_logger.exception(e)

    async def stop(self) -> None:
        """Initiate graceful halting procedure."""
        self.logger.info("Was asked to stop, initiating graceful shutdown...")
        self.shutdown_event.set()

    async def start(self) -> None:
        """Robot's core working method."""
        self.logger.info(
            f"Initialising main loop for {self.settings.robot_name} with a {self.loop_interval_seconds}s polling interval "
            f"and batch sizes {self.settings.request_batch_size:,} (poll) and {self.settings.fulfil_batch_size} (fulfil)",
        )

        def shutdown_handler(signum: int, _frame: FrameType | None) -> None:
            self.logger.info(f"Received signal {signum}, initiating graceful shutdown...")
            self.shutdown_event.set()

        # Register signal handlers for graceful shutdown
        signal.signal(signal.SIGINT, shutdown_handler)
        signal.signal(signal.SIGTERM, shutdown_handler)

        try:
            main_loop_task = asyncio.create_task(self._main_loop())
            shutdown_task = asyncio.create_task(self.shutdown_event.wait())

            # Wait for either the polling task to complete or shutdown signal
            _done, pending = await asyncio.wait(
                [main_loop_task, shutdown_task],
                return_when=asyncio.FIRST_COMPLETED,
            )

            # Cancel remaining tasks
            for task in pending:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task

            self.logger.info("Shutdown complete")

        except KeyboardInterrupt:
            self.logger.info("Received keyboard interrupt, shutting down...")
            sys.exit(0)
        except Exception as e:
            self.logger.error(f"Fatal error occurred: {e}")
            self.logger.exception(e)
            sys.exit(1)
