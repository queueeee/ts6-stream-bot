"""Pytest fixtures.

`BOT_API_KEY` is set at module-import time so that `ts6_stream_bot.config.Settings`
can be instantiated when test modules are first imported (the validator rejects
empty/placeholder keys).
"""

from __future__ import annotations

import os

# Set BEFORE any test module imports ts6_stream_bot.config
os.environ.setdefault("BOT_API_KEY", "test-key")
os.environ.setdefault("LOG_LEVEL", "WARNING")

import pytest


@pytest.fixture(autouse=True)
def _ensure_test_env(monkeypatch):
    """Per-test guarantee of a sane env, in case a test mutates it."""
    monkeypatch.setenv("BOT_API_KEY", "test-key")
    monkeypatch.setenv("LOG_LEVEL", "WARNING")
