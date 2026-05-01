"""Settings validation."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from ts6_stream_bot.config import Settings


@pytest.mark.parametrize(
    "key",
    [
        "",
        "changeme",
        "CHANGEME",
        "changeme-generate-with-openssl-rand-base64-32",
    ],
)
def test_rejects_insecure_api_keys(monkeypatch, key: str) -> None:
    monkeypatch.setenv("BOT_API_KEY", key)
    with pytest.raises(ValidationError):
        Settings()


def test_accepts_real_api_key(monkeypatch) -> None:
    monkeypatch.setenv("BOT_API_KEY", "k7Q9zP2vR4yL8mN6tX1cV3bH5fJ7gK9w")
    s = Settings()
    assert s.BOT_API_KEY == "k7Q9zP2vR4yL8mN6tX1cV3bH5fJ7gK9w"


def test_audio_loudnorm_default_off(monkeypatch) -> None:
    monkeypatch.setenv("BOT_API_KEY", "k7Q9zP2vR4yL8mN6tX1cV3bH5fJ7gK9w")
    monkeypatch.delenv("AUDIO_LOUDNORM", raising=False)
    assert Settings().AUDIO_LOUDNORM is False


def test_audio_loudnorm_can_be_enabled(monkeypatch) -> None:
    monkeypatch.setenv("BOT_API_KEY", "k7Q9zP2vR4yL8mN6tX1cV3bH5fJ7gK9w")
    monkeypatch.setenv("AUDIO_LOUDNORM", "true")
    assert Settings().AUDIO_LOUDNORM is True
