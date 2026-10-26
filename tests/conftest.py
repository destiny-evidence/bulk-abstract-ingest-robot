"""Shared test setup: minimal environment so `app` modules can be imported without a real deployment."""

import os

os.environ.setdefault("ROBOT_SECRET", "test-secret")
os.environ.setdefault("ROBOT_ID", "00000000-0000-0000-0000-000000000001")

# Registers the shared fixtures below for all test modules (must come after the environment setup, as importing `app` reads the settings at import time).
from tests.util.fixtures import (  # noqa: E402, F401, RUF100
    db,
    enhance_runner,
    match_runner,
    runner_factory,
    settings,
)
