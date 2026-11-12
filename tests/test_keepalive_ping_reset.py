"""A reconnect clears the keep-alive ping, so one lost pong does not loop forever."""

import asyncio

from telethon.network.mtprotosender import MTProtoSender

from telegram_mcp import client_factory


def test_reconnect_clears_stale_ping(monkeypatch):
    seen = {}

    async def fake_original(self, last_error):
        seen["ping"] = self._ping
        seen["error"] = last_error

    monkeypatch.setattr(client_factory, "_original_reconnect", fake_original)
    sender = object.__new__(MTProtoSender)
    sender._ping = 12345

    asyncio.run(MTProtoSender._reconnect(sender, None))

    assert seen == {"ping": None, "error": None}
    assert sender._ping is None


def test_stale_ping_then_reconnect_sends_a_new_ping(monkeypatch):
    """The loop seen in production: pong lost -> reconnect -> next tick must ping again."""
    sent, reconnects = [], []

    async def fake_original(self, last_error):
        reconnects.append(last_error)

    monkeypatch.setattr(client_factory, "_original_reconnect", fake_original)
    sender = object.__new__(MTProtoSender)
    sender._ping = None
    sender.send = lambda request: sent.append(request.ping_id)
    sender._start_reconnect = lambda error: asyncio.run(MTProtoSender._reconnect(sender, error))

    sender._keepalive_ping(1)  # sent, pong never comes
    sender._keepalive_ping(2)  # previous pong missing -> reconnect
    sender._keepalive_ping(3)  # after the reconnect a fresh ping goes out

    assert sent == [1, 3]
    assert len(reconnects) == 1


def test_patch_is_installed_once():
    assert MTProtoSender._reconnect is client_factory._reconnect_with_fresh_ping
