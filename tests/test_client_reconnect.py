"""The Telethon reconnect budget is configurable, and unset keeps the defaults."""

import pytest

from telegram_mcp import client_factory


class RecordingClient:
    def __init__(self, session, api_id, api_hash, **kwargs):
        self.kwargs = kwargs


def _build():
    return client_factory.build_client(
        "session", "default", client_cls=RecordingClient, credentials=(1, "hash")
    ).kwargs


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for name in ("TELEGRAM_CONNECTION_RETRIES", "TELEGRAM_RETRY_DELAY"):
        monkeypatch.delenv(name, raising=False)


def test_unset_keeps_telethon_defaults():
    kwargs = _build()
    assert "connection_retries" not in kwargs
    assert "retry_delay" not in kwargs


def test_negative_retries_means_forever_and_delay_is_passed(monkeypatch):
    monkeypatch.setenv("TELEGRAM_CONNECTION_RETRIES", "-1")
    monkeypatch.setenv("TELEGRAM_RETRY_DELAY", "5")
    kwargs = _build()
    assert kwargs["connection_retries"] == -1
    assert kwargs["retry_delay"] == 5


@pytest.mark.parametrize("value", ["forever", "1.5"])
def test_garbage_is_ignored(monkeypatch, value):
    monkeypatch.setenv("TELEGRAM_CONNECTION_RETRIES", value)
    monkeypatch.setenv("TELEGRAM_RETRY_DELAY", value)
    kwargs = _build()
    assert "connection_retries" not in kwargs
    assert "retry_delay" not in kwargs


def test_negative_delay_is_ignored(monkeypatch):
    monkeypatch.setenv("TELEGRAM_RETRY_DELAY", "-3")
    assert "retry_delay" not in _build()
