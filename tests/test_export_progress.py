"""What an export reports about itself while it is still running.

Found by watching a real export: with media on, the caller saw the same
"200 messages" line for minutes on end, because progress was tied to the
message counter alone and the time was going into downloads.
"""

import datetime as dt

import pytest

from telegram_mcp.export import run as run_mod
from telegram_mcp.export.run import ExportOptions, export_one


@pytest.fixture
def engine(monkeypatch, tmp_path):
    """export_one wired to a chat of made-up messages."""

    async def _resolve(client, target):
        return object()

    async def _meta(client, entity):
        return {"id": 42, "title": "Chat", "type": "user"}

    monkeypatch.setattr(run_mod, "resolve_target", _resolve)
    monkeypatch.setattr(run_mod, "chat_meta", _meta)
    monkeypatch.setattr(run_mod, "render", lambda *a, **k: None)
    return tmp_path


def _records(count, media_root=None):
    async def _iter(client, entity, **kwargs):
        for index in range(1, count + 1):
            record = {
                "id": index,
                "date": dt.datetime(2026, 8, 1, tzinfo=dt.timezone.utc),
                "text": "hi",
                "media": None,
            }
            if media_root is not None:
                name = f"media/photos/{index:08d}.jpg"
                path = media_root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b"x" * 100)
                record["media"] = {"kind": "photo", "file": name}
            yield record

    return _iter


@pytest.mark.asyncio
async def test_a_slow_export_reports_before_it_reaches_two_hundred(engine, monkeypatch):
    monkeypatch.setattr(run_mod, "iter_records", _records(5))
    monkeypatch.setattr(run_mod, "PROGRESS_EVERY_SECONDS", 0.0)
    events = []

    await export_one(
        None, "@chat", ExportOptions(out=engine), on_progress=lambda e: events.append(e)
    )

    running = [e for e in events if e["stage"] == "running"]
    # Without a clock the only running event would be at message 200, and this
    # chat has five.
    assert running, "an export that is working must say so before message 200"
    assert running[-1]["messages"] <= 5


@pytest.mark.asyncio
async def test_progress_carries_the_media_on_disk(engine, monkeypatch):
    export_dir = engine / "Chat_42"
    export_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(run_mod, "iter_records", _records(3, media_root=export_dir))
    monkeypatch.setattr(run_mod, "PROGRESS_EVERY_SECONDS", 0.0)
    events = []

    await export_one(
        None,
        "@chat",
        ExportOptions(out=engine, media=True),
        on_progress=lambda e: events.append(e),
    )

    finished = [e for e in events if e["stage"] == "finished"][-1]
    # Counting bytes is what separates "downloading something big" from "hung".
    assert finished["media_files"] == 3
    assert finished["media_bytes"] == 300


@pytest.mark.asyncio
async def test_a_quiet_export_does_not_report_on_every_message(engine, monkeypatch):
    monkeypatch.setattr(run_mod, "iter_records", _records(20))
    monkeypatch.setattr(run_mod, "PROGRESS_EVERY_SECONDS", 3600.0)
    events = []

    await export_one(
        None, "@chat", ExportOptions(out=engine), on_progress=lambda e: events.append(e)
    )

    assert [e for e in events if e["stage"] == "running"] == []
