"""Pytest fixtures."""
import pytest


@pytest.fixture(autouse=True)
def _set_test_env(monkeypatch):
    """Ensure tests use predictable settings, no real .env."""
    monkeypatch.setenv("BOT_API_KEY", "test-key")
    monkeypatch.setenv("LOG_LEVEL", "WARNING")
