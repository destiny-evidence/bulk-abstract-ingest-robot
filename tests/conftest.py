"""Shared test setup: minimal environment so `app` modules can be imported without a real deployment."""

import os

os.environ.setdefault("ROBOT_SECRET", "test-secret")
os.environ.setdefault("ROBOT_ID", "00000000-0000-0000-0000-000000000001")
