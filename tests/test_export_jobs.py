"""Tests for the background export jobs and the MCP tools that drive them.

The point of these tools is that an export runs inside the already-logged-in
server, so what has to hold is: the depth is never guessed, one bad chat does
not lose the good ones, a crashed server does not report a job as still
running, and nothing bulky travels back through the tool result.
"""

import asyncio
import json

import pytest

from telegram_mcp.export import jobs
from telegram_mcp.export.run import ExportOptions
from telegram_mcp.tools import export as export_tools


@pytest.fixture
def export_root(tmp_path, monkeypatch):
    root = tmp_path / "exports"
    monkeypatch.setenv("TELEGRAM_EXPORT_DIR", str(root))
    jobs._RECORDS.clear()
    jobs._TASKS.clear()
    yield root
    jobs._RECORDS.clear()
    jobs._TASKS.clear()


def _options(root):
    return ExportOptions(out=root, formats=["jsonl"])


async def _drain(job_id, timeout=2.0):
    task = jobs._TASKS.get(job_id)
    if task is not None:
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            pass
    return jobs.status(job_id)


# ---------------------------------------------------------------------------
# The job itself
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_job_returns_immediately_and_finishes_in_the_background(export_root, monkeypatch):
    started = asyncio.Event()

    async def _fake_export(client, target, options, on_progress=None):
        started.set()
        await asyncio.sleep(0)
        return {
            "dir": export_root / target,
            "meta": {"chat": {"title": target}, "message_count": 7},
        }

    monkeypatch.setattr(jobs, "export_one", _fake_export)

    record = jobs.start(None, ["@one"], _options(export_root))

    # The call that starts the export must not wait for it.
    assert record["state"] == "running"
    assert not started.is_set()

    final = await _drain(record["job_id"])
    assert final["state"] == "finished"
    assert final["done"][0]["messages"] == 7


@pytest.mark.asyncio
async def test_one_failing_chat_does_not_lose_the_others(export_root, monkeypatch):
    async def _fake_export(client, target, options, on_progress=None):
        if target == "@bad":
            raise RuntimeError("no such chat")
        return {
            "dir": export_root / target,
            "meta": {"chat": {"title": target}, "message_count": 3},
        }

    monkeypatch.setattr(jobs, "export_one", _fake_export)

    record = jobs.start(None, ["@good", "@bad", "@also-good"], _options(export_root))
    final = await _drain(record["job_id"])

    assert final["state"] == "finished"
    assert [item["target"] for item in final["done"]] == ["@good", "@also-good"]
    assert final["failed"] == [{"target": "@bad", "error": "RuntimeError: no such chat"}]


@pytest.mark.asyncio
async def test_a_job_that_saved_nothing_is_failed_not_finished(export_root, monkeypatch):
    async def _always_fails(client, target, options, on_progress=None):
        raise RuntimeError("nope")

    monkeypatch.setattr(jobs, "export_one", _always_fails)

    record = jobs.start(None, ["@one"], _options(export_root))
    final = await _drain(record["job_id"])

    assert final["state"] == "failed"


@pytest.mark.asyncio
async def test_cancelled_job_keeps_what_it_wrote(export_root, monkeypatch):
    async def _slow(client, target, options, on_progress=None):
        await asyncio.sleep(30)

    monkeypatch.setattr(jobs, "export_one", _slow)

    record = jobs.start(None, ["@one"], _options(export_root))
    await asyncio.sleep(0)
    task = jobs.cancel(record["job_id"])
    assert task is not None
    with pytest.raises(asyncio.CancelledError):
        await task

    assert jobs.status(record["job_id"])["state"] == "cancelled"


@pytest.mark.asyncio
async def test_a_job_record_outliving_its_process_is_not_reported_as_running(
    export_root, monkeypatch
):
    async def _slow(client, target, options, on_progress=None):
        await asyncio.sleep(30)

    monkeypatch.setattr(jobs, "export_one", _slow)
    record = jobs.start(None, ["@one"], _options(export_root))
    job_id = record["job_id"]

    # A restart: the file on disk still says "running", nothing is running.
    task = jobs._TASKS.pop(job_id)
    task.cancel()
    jobs._RECORDS.clear()

    assert jobs.status(job_id)["state"] == "interrupted"
    assert jobs.recent()[0]["state"] == "interrupted"


def test_status_of_an_unknown_job_is_none(export_root):
    assert jobs.status("deadbeef") is None


# ---------------------------------------------------------------------------
# The tool boundary
# ---------------------------------------------------------------------------


def _capturing_start(captured):
    """Stand in for jobs.start and record what the tool decided."""

    def _start(client, targets, options):
        captured["targets"] = targets
        captured["options"] = options
        return {"job_id": "0123456789ab", "out": str(options.out)}

    return _start


async def _start(**kwargs):
    params = dict(chats="@chat", everything=True)
    params.update(kwargs)
    return json.loads(await export_tools.start_chat_export(**params))


@pytest.mark.asyncio
async def test_depth_must_be_stated_exactly_once(export_root, monkeypatch):
    monkeypatch.setattr(export_tools, "get_client", lambda account: None, raising=False)

    no_depth = json.loads(await export_tools.start_chat_export(chats="@chat"))
    both = await _start(months=3)

    assert no_depth["reason"] == "ambiguous_depth"
    assert both["reason"] == "ambiguous_depth"


@pytest.mark.asyncio
async def test_unknown_format_is_refused_before_any_work(export_root, monkeypatch):
    monkeypatch.setattr(export_tools, "get_client", lambda account: None, raising=False)
    result = await _start(formats="jsonl,pdf")
    assert result["reason"] == "unknown_format"
    assert "pdf" in result["detail"]


@pytest.mark.asyncio
async def test_jsonl_is_always_written_even_when_only_html_was_asked_for(export_root, monkeypatch):
    captured = {}
    monkeypatch.setattr(export_tools, "get_client", lambda account: None, raising=False)
    monkeypatch.setattr(jobs, "start", _capturing_start(captured))

    await _start(formats="html")

    assert captured["options"].formats[0] == "jsonl"
    assert "html" in captured["options"].formats


@pytest.mark.asyncio
async def test_groq_transcription_without_a_key_is_refused_up_front(export_root, monkeypatch):
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    monkeypatch.setattr(export_tools, "get_client", lambda account: None, raising=False)

    result = await _start(transcribe="groq")

    assert result["reason"] == "missing_groq_key"


@pytest.mark.asyncio
async def test_unresolvable_date_is_a_refusal_not_a_crash(export_root, monkeypatch):
    monkeypatch.setattr(export_tools, "get_client", lambda account: None, raising=False)

    result = json.loads(await export_tools.start_chat_export(chats="@chat", since="last tuesday"))

    # parse_date raises ExportError; SystemExit here would have killed the server.
    assert result["reason"] == "invalid_request"
    assert "last tuesday" in result["detail"]


@pytest.mark.asyncio
async def test_start_returns_a_job_id_and_no_message_content(export_root, monkeypatch):
    monkeypatch.setattr(export_tools, "get_client", lambda account: None, raising=False)

    async def _fake_export(client, target, options, on_progress=None):
        return {
            "dir": export_root / "chat",
            "meta": {"chat": {"title": "Chat"}, "message_count": 4},
        }

    monkeypatch.setattr(jobs, "export_one", _fake_export)
    result = await _start(chats=["@a", "@b"])

    assert result["started"] is True
    assert result["chats"] == ["@a", "@b"]
    assert len(result["job_id"]) == 12
    # The whole point: the payload is a handle, not the export.
    assert len(json.dumps(result)) < 600
    await _drain(result["job_id"])


@pytest.mark.asyncio
async def test_comma_separated_chats_are_accepted_like_a_list(export_root, monkeypatch):
    captured = {}
    monkeypatch.setattr(export_tools, "get_client", lambda account: None, raising=False)
    monkeypatch.setattr(jobs, "start", _capturing_start(captured))

    await _start(chats="@a, @b ,@c")

    assert captured["targets"] == ["@a", "@b", "@c"]


@pytest.mark.asyncio
async def test_status_lists_jobs_and_names_the_export_root(export_root, monkeypatch):
    async def _fake_export(client, target, options, on_progress=None):
        return {"dir": export_root / "c", "meta": {"chat": {"title": "C"}, "message_count": 1}}

    monkeypatch.setattr(jobs, "export_one", _fake_export)
    record = jobs.start(None, ["@one"], _options(export_root))
    await _drain(record["job_id"])

    listing = json.loads(await export_tools.export_status())
    single = json.loads(await export_tools.export_status(job_id=record["job_id"]))
    missing = json.loads(await export_tools.export_status(job_id="nope"))

    assert listing["export_root"] == str(export_root)
    assert single["state"] == "finished"
    assert missing["found"] is False
