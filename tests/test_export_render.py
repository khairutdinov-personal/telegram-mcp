"""Tests for telegram_mcp/export: entity formatting and the JSONL renderers.

The load-bearing case is UTF-16. Telegram counts entity offsets in UTF-16 code
units, so one emoji earlier in a line shifts every later offset by one; slicing
the Python string instead is the classic exporter bug and it corrupts formatting
silently, in exactly the messages people quote. ``test_utf16_mutant_is_caught``
reintroduces that bug on purpose and asserts the check goes red, so the check
above it cannot quietly become decorative.
"""

import json
import re
from types import SimpleNamespace

import pytest

from telegram_mcp.export import entities
from telegram_mcp.export.render import render

EMOJI_TEXT = "🔥 это важное слово"
# The emoji is two UTF-16 code units but one Python character: "важное" starts
# at code unit 7 and at Python index 6.
BOLD_ON_VAZHNOE = [{"type": "bold", "class": "MessageEntityBold", "offset": 7, "length": 6}]


def _bold_spans(html: str) -> list:
    return re.findall(r"<strong>([^<]*)</strong>", html)


# --- entity formatting -------------------------------------------------------


def test_bold_survives_a_leading_emoji():
    assert _bold_spans(entities.to_html(EMOJI_TEXT, BOLD_ON_VAZHNOE)) == ["важное"]


def test_markdown_bold_survives_a_leading_emoji():
    assert "**важное**" in entities.to_markdown(EMOJI_TEXT, BOLD_ON_VAZHNOE)


def test_utf16_mutant_is_caught(monkeypatch):
    """Slice Python characters instead of UTF-16 units: the check must fail."""
    monkeypatch.setattr(entities, "_slice", lambda buf, a, b: buf.decode("utf-16-le")[a:b])
    assert _bold_spans(entities.to_html(EMOJI_TEXT, BOLD_ON_VAZHNOE)) != ["важное"]


def test_message_text_is_escaped_not_emitted():
    html = entities.to_html("<b>raw</b> & co", [])
    assert "&lt;b&gt;raw&lt;/b&gt;" in html
    assert "<b>" not in html


def test_text_url_becomes_an_anchor():
    html = entities.to_html(
        "смотри ссылку",
        [
            {
                "type": "text_url",
                "class": "MessageEntityTextUrl",
                "offset": 7,
                "length": 6,
                "url": "https://example.com",
            }
        ],
    )
    assert '<a href="https://example.com">ссылку</a>' in html


def test_nested_entities_do_not_cross_tags():
    html = entities.to_html(
        "bold link",
        [
            {
                "type": "text_url",
                "class": "MessageEntityTextUrl",
                "offset": 0,
                "length": 9,
                "url": "https://example.com",
            },
            {"type": "bold", "class": "MessageEntityBold", "offset": 0, "length": 4},
        ],
    )
    assert html.count("<a ") == html.count("</a>")
    assert "<strong>bold</strong>" in html


def test_entities_to_json_keeps_urls_and_survives_unknown_types():
    class MessageEntityBold:
        offset, length = 0, 4

    class SomeFutureEntity:
        offset, length = 4, 2

    payload = entities.entities_to_json([MessageEntityBold(), SomeFutureEntity()])
    assert payload[0]["type"] == "bold"
    assert payload[1]["type"] == "plain"  # degrades instead of raising


# --- rendering ---------------------------------------------------------------


@pytest.fixture
def export_dir(tmp_path):
    """A minimal export folder: text, media, a transcript and a service event."""
    records = [
        {
            "id": 1,
            "date": "2026-08-20T10:00:00+00:00",
            "from_id": 111,
            "from_name": "Иван Петров",
            "outgoing": False,
            "text": EMOJI_TEXT,
            "entities": BOLD_ON_VAZHNOE,
            "reply": None,
            "forward": None,
            "reactions": [{"reaction": "👍", "count": 2}],
            "service": None,
            "media": None,
            "transcript": None,
            "edit_date": None,
        },
        {
            "id": 2,
            "date": "2026-08-20T10:01:00+00:00",
            "from_id": 222,
            "from_name": "Тимур",
            "outgoing": True,
            "text": "ответ",
            "entities": [],
            "reply": {"reply_to_msg_id": 1, "top_msg_id": None, "forum_topic": False},
            "forward": None,
            "reactions": [],
            "service": None,
            "media": {
                "kind": "voice",
                "duration": 23,
                "size": 41000,
                "file": None,
                "name": None,
                "mime": "audio/ogg",
                "skipped": None,
            },
            "transcript": {
                "text": "тестовая расшифровка",
                "engine": "groq",
                "lang": "ru",
                "duration": 23,
            },
            "edit_date": "2026-08-20T10:02:00+00:00",
        },
        {
            "id": 3,
            "date": "2026-08-21T09:00:00+00:00",
            "from_id": None,
            "from_name": None,
            "outgoing": False,
            "text": "",
            "entities": [],
            "reply": None,
            "forward": None,
            "reactions": [],
            "service": {"action": "MessageActionChatAddUser", "title": None},
            "media": None,
            "transcript": None,
            "edit_date": None,
        },
    ]
    with (tmp_path / "messages.jsonl").open("w", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
    meta = {
        "chat": {
            "id": -100123,
            "title": "Тестовый чат",
            "type": "supergroup",
            "username": "testchat",
        },
        "message_count": len(records),
        "window": {"first": "2026-08-20", "last": "2026-08-21"},
    }
    (tmp_path / "meta.json").write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
    return tmp_path, meta


def test_html_render(export_dir):
    path, meta = export_dir
    render(path, meta, ["jsonl", "html"])
    html = (path / "messages.html").read_text(encoding="utf-8")
    assert _bold_spans(html) == ["важное"]
    assert 'class="service"' in html
    assert 'href="#msg1"' in html  # the reply links to the quoted message
    assert "not downloaded" in html  # undownloaded media is still described
    assert "engine: groq" in html
    assert "not a verbatim quote" in html  # a transcript is never passed off as speech


def test_markdown_and_text_render(export_dir):
    path, meta = export_dir
    render(path, meta, ["jsonl", "md", "txt"])
    markdown = (path / "messages.md").read_text(encoding="utf-8")
    assert "🔥 это **важное** слово" in markdown
    assert "transcript (groq)" in markdown
    text = (path / "messages.txt").read_text(encoding="utf-8")
    assert "Иван Петров, [20.08.2026 10:00]" in text


def test_html_paginates(export_dir):
    path, meta = export_dir
    render_pages = __import__("telegram_mcp.export.render", fromlist=["render_html"])
    render_pages.render_html(path, meta, page_size=1)
    assert (path / "messages.html").exists()
    assert (path / "messages2.html").exists()
    assert 'href="messages2.html"' in (path / "messages.html").read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Rich messages in an export
# ---------------------------------------------------------------------------
#
# Found live on 2026-08-29: the first message of a real export was a rich
# message and went to disk as an empty line, because the content lives in page
# blocks and `message` is empty.


def _rich_message(text):
    from telethon.tl import types

    return types.RichMessage(
        blocks=[types.PageBlockParagraph(text=types.TextPlain(text=text))],
        photos=[],
        documents=[],
    )


def test_rich_message_reaches_the_export_record():
    from telegram_mcp import rich_messages

    record = {"text": ""}
    rich_messages.attach_to_record(
        record, SimpleNamespace(rich_message=_rich_message("Quarterly plan"))
    )

    assert record["text"] == "Quarterly plan"
    assert record["rich_message"] == {}


def test_export_keeps_plain_text_when_a_message_has_both():
    from telegram_mcp import rich_messages

    record = {"text": "caption"}
    rich_messages.attach_to_record(record, SimpleNamespace(rich_message=_rich_message("blocks")))

    assert record["text"] == "caption"
    assert record["rich_message"]["markdown"] == "blocks"


def test_export_record_without_rich_content_is_untouched():
    from telegram_mcp import rich_messages

    record = {"text": "plain"}
    rich_messages.attach_to_record(record, SimpleNamespace(rich_message=None))

    assert record == {"text": "plain"}


def test_export_does_not_sanitize_the_rendering():
    """The MCP tools sanitize user content; a JSONL export is a faithful copy."""
    from telegram_mcp import rich_messages

    verbatim = {"text": ""}
    sanitized = {"text": ""}
    msg = SimpleNamespace(rich_message=_rich_message("a <b>tag</b>"))

    rich_messages.attach_to_record(verbatim, msg)
    rich_messages.attach_to_record(sanitized, msg, transform=lambda value: value.upper())

    assert verbatim["text"] == "a <b>tag</b>"
    assert sanitized["text"] == "A <B>TAG</B>"


@pytest.mark.asyncio
async def test_iter_records_writes_rich_content_not_an_empty_line():
    """The end-to-end guard: the renderer being correct is not enough if the
    export never calls it. This is the shape that reached disk empty."""
    import datetime as dt

    from telethon.tl import types

    from telegram_mcp.export import fetch

    class _Iterator:
        def __init__(self, items):
            self._items = list(items)

        def __aiter__(self):
            return self

        async def __anext__(self):
            if not self._items:
                raise StopAsyncIteration
            return self._items.pop(0)

    class _Client:
        def __init__(self, items):
            self._items = items

        def iter_messages(self, entity, **kwargs):
            return _Iterator(self._items)

    rich_only = SimpleNamespace(
        id=860243,
        date=dt.datetime(2026, 8, 26, tzinfo=dt.timezone.utc),
        message="",
        entities=None,
        out=True,
        sender=None,
        sender_id=42,
        rich_message=_rich_message("# Plan\n\nline"),
        media=None,
    )

    records = [
        record
        async for record in fetch.iter_records(
            _Client([rich_only]),
            types.PeerUser(user_id=42),
            since=None,
            until=None,
            include_raw=False,
        )
    ]

    assert len(records) == 1
    assert "Plan" in records[0]["text"]
    assert records[0]["rich_message"] == {}
