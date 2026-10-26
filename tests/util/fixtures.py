"""
Shared fixtures: settings, the mock database, and generic runner construction.

`runner_factory` builds any `Runner` subclass with a bypassed `__init__` (no settings/logging/DB/auth
clients), the store of your choice, and a spec-ed mock repository — `MagicMock(spec=Repository)` turns
the async API methods into awaitable `AsyncMock`s automatically, so no per-method mock wiring is needed.
The per-runner fixtures below just say which runner class and which store flavour a test wants.
"""

import asyncio
import logging
from typing import TypeVar
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.robots.enhance import EnhancementRunner
from app.robots.match import MatchRunner
from app.util.config import Settings
from app.util.repository import Repository
from app.util.runner import Runner

from .fake_database import MockDatabase, MockStore

R = TypeVar("R", bound=Runner)


@pytest.fixture
def settings() -> Settings:
    return Settings()


@pytest.fixture
def db() -> MockDatabase:
    return MockDatabase()


@pytest.fixture
def runner_factory(settings: Settings):
    """Build a runner with bypassed __init__ (no settings/logging/DB/auth clients) and mocked store/repository."""

    def make(runner_class: type[R], *, store: object | None = None, repository: MagicMock | None = None) -> R:
        runner: R = runner_class.__new__(runner_class)
        runner.settings = settings
        runner.logger = logging.getLogger("test")
        runner.loop_logger = runner.logger.getChild("loop")
        runner.total_entries_processed = 0
        runner.shutdown_event = asyncio.Event()
        runner.store = store if store is not None else AsyncMock()  # auto-async-mocked store, unless the tests want a real one
        runner.repository = repository if repository is not None else MagicMock(spec=Repository)  # async API methods become AsyncMock instances
        return runner

    return make


@pytest.fixture
def match_runner(runner_factory) -> MatchRunner:
    """MatchRunner on a fully auto-mocked store and repository API."""
    return runner_factory(MatchRunner)


@pytest.fixture
def enhance_runner(runner_factory, db: MockDatabase) -> EnhancementRunner:
    """EnhancementRunner on a repository API that offers no batch and a store backed by the mock database."""
    runner: EnhancementRunner = runner_factory(EnhancementRunner, store=MockStore(db))
    runner.repository.get_next_batch.return_value = (None, None)  # the repository offers no batch until a test says otherwise
    return runner
