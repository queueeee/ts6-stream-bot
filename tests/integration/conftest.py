"""Integration tests need a real Xvfb + Chromium. They are opt-in via RUN_INTEGRATION=1."""

from __future__ import annotations

import os

import pytest

if not os.environ.get("RUN_INTEGRATION"):
    pytest.skip(
        "integration tests skipped (set RUN_INTEGRATION=1 to enable)",
        allow_module_level=True,
    )
