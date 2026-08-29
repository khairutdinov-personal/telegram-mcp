"""Bulk chat export as background jobs.

The server is already logged in, so exporting through it costs no second
session and no second login. The export itself cannot be a normal tool call:
it runs for minutes to hours and produces hundreds of megabytes. So these
tools start a job, and the caller polls it - the files land on the server's
disk and only counters and paths come back.
"""

import datetime as dt
import json
import os
from pathlib import Path

from telegram_mcp.export import jobs
from telegram_mcp.export.run import ExportOptions
from telegram_mcp.export.util import ExportError, months_ago, parse_date
from telegram_mcp.runtime import *  # noqa: F403

KNOWN_FORMATS = ("jsonl", "html", "md", "txt")


def _fail(reason: str, detail: str, **extra) -> str:
    return json.dumps(
        {"started": False, "reason": reason, "detail": detail, **extra}, ensure_ascii=False
    )


def _parse_targets(chats) -> list:
    if isinstance(chats, str):
        items = [part.strip() for part in chats.split(",")]
    else:
        items = [str(part).strip() for part in (chats or [])]
    return [item for item in items if item]


@mcp.tool(  # noqa: F405
    annotations=ToolAnnotations(  # noqa: F405
        title="Start Chat Export",
        openWorldHint=True,
        # Reads Telegram, but writes files to the server's disk, so it is not
        # read-only in the sense the annotation means. A read-only deployment
        # that wants it has to name it: TELEGRAM_EXPOSED_TOOLS=read-only+start_chat_export,export_status
        readOnlyHint=False,
    )
)
@with_account(readonly=False)  # noqa: F405
async def start_chat_export(
    chats,
    everything: bool = False,
    months: int = None,
    since: str = None,
    until: str = None,
    formats: str = "jsonl",
    media: bool = False,
    media_max_mb: float = None,
    transcribe: str = None,
    include_raw: bool = True,
    resume: bool = False,
    out: str = None,
    account: str = None,
) -> str:
    """
    Export whole chats to files on the server, as a background job.

    Returns a job_id immediately; poll it with export_status. Nothing is sent
    through this tool's result but counters and paths - fetch the files from
    the server's export directory yourself.

    Args:
        chats: One target or a list of them. Accepts @username, a t.me link,
            a numeric chat id, or a chat title.
        everything: Export the whole history. Mutually exclusive with
            months/since, and one of the three is required - an export with no
            depth would silently mean something different for every chat.
        months: Only the last N months.
        since: Only messages from this date (YYYY-MM-DD or an ISO timestamp).
        until: Stop at this date.
        formats: Comma-separated: jsonl, html, md, txt. JSONL is always written
            (the other formats are rendered from it).
        media: Download photos, video, voice and documents as well.
        media_max_mb: Skip media larger than this.
        transcribe: 'groq' or 'telegram' to transcribe voice and video notes.
        include_raw: Keep the raw Telegram payload in the JSONL. Turning it off
            makes the file smaller and lossy.
        resume: Append only messages newer than a previous export in the same
            folder, instead of starting over.
        out: Output directory. Defaults to TELEGRAM_EXPORT_DIR.
    """
    try:
        targets = _parse_targets(chats)
        if not targets:
            return _fail("no_targets", "Pass at least one chat.")

        depth = [bool(everything), months is not None, since is not None]
        if sum(depth) != 1:
            return _fail(
                "ambiguous_depth",
                "Pass exactly one of everything=True, months=N, or since='YYYY-MM-DD'.",
            )

        window_since: dt.datetime = None
        if months is not None:
            if months <= 0:
                return _fail("bad_window", "months must be a positive number.")
            window_since = months_ago(months)
        elif since is not None:
            window_since = parse_date(since)
        window_until = parse_date(until) if until else None
        if window_since and window_until and window_until <= window_since:
            return _fail("bad_window", "until must be later than since.")

        wanted = [part.strip().lower() for part in formats.split(",") if part.strip()]
        unknown = [name for name in wanted if name not in KNOWN_FORMATS]
        if unknown:
            return _fail(
                "unknown_format",
                f"Unknown format(s): {', '.join(unknown)}. Known: {', '.join(KNOWN_FORMATS)}.",
            )
        if "jsonl" not in wanted:
            wanted.insert(0, "jsonl")

        if transcribe is not None:
            from telegram_mcp.transcription import ENGINES

            if transcribe not in ENGINES:
                return _fail(
                    "unknown_engine",
                    f"Unknown transcription engine '{transcribe}'. "
                    f"Known: {', '.join(sorted(ENGINES))}.",
                )
            if transcribe == "groq" and not os.getenv("GROQ_API_KEY"):
                return _fail(
                    "missing_groq_key",
                    "transcribe='groq' needs GROQ_API_KEY on the server. "
                    "Use transcribe='telegram' or set the key.",
                )

        out_dir = Path(out).expanduser() if out else jobs.export_root()
        out_dir.mkdir(parents=True, exist_ok=True)

        options = ExportOptions(
            out=out_dir,
            formats=wanted,
            since=window_since,
            until=window_until,
            media=bool(media),
            media_max_mb=media_max_mb or None,
            transcribe=transcribe,
            include_raw=bool(include_raw),
            resume=bool(resume),
        )

        cl = get_client(account)  # noqa: F405
        record = jobs.start(cl, targets, options)
        return json.dumps(
            {
                "started": True,
                "job_id": record["job_id"],
                "chats": targets,
                "out": record["out"],
                "note": "Poll export_status(job_id). Files stay on the server; "
                "copy them from the output directory yourself.",
            },
            ensure_ascii=False,
        )
    except ExportError as error:
        return _fail("invalid_request", str(error))
    except Exception as error:  # noqa: BLE001 - tool boundary
        return log_and_format_error("start_chat_export", error, chats=chats)  # noqa: F405


@mcp.tool(  # noqa: F405
    annotations=ToolAnnotations(title="Export Status", readOnlyHint=True)  # noqa: F405
)
async def export_status(job_id: str = None, limit: int = 10) -> str:
    """
    Progress of a background chat export.

    Args:
        job_id: The id returned by start_chat_export. Omit to list recent jobs.
        limit: How many jobs to list when job_id is omitted.
    """
    try:
        if job_id:
            record = jobs.status(job_id)
            if record is None:
                return json.dumps(
                    {"found": False, "job_id": job_id, "detail": "No such export job."},
                    ensure_ascii=False,
                )
            return json.dumps(record, ensure_ascii=False, default=str)
        return json.dumps(
            {"jobs": jobs.recent(limit), "export_root": str(jobs.export_root())},
            ensure_ascii=False,
            default=str,
        )
    except Exception as error:  # noqa: BLE001 - tool boundary
        return log_and_format_error("export_status", error, job_id=job_id)  # noqa: F405


@mcp.tool(  # noqa: F405
    annotations=ToolAnnotations(title="Cancel Export", readOnlyHint=False)  # noqa: F405
)
async def cancel_export(job_id: str) -> str:
    """
    Stop a running export. Whatever it already wrote stays on disk, and
    start_chat_export(resume=True) continues from there.

    Args:
        job_id: The id returned by start_chat_export.
    """
    try:
        task = jobs.cancel(job_id)
        if task is None:
            record = jobs.status(job_id)
            detail = "No such export job." if record is None else f"Job is {record['state']}."
            return json.dumps(
                {"cancelled": False, "job_id": job_id, "detail": detail}, ensure_ascii=False
            )
        return json.dumps(
            {"cancelled": True, "job_id": job_id, "note": "Partial output stays on disk."},
            ensure_ascii=False,
        )
    except Exception as error:  # noqa: BLE001 - tool boundary
        return log_and_format_error("cancel_export", error, job_id=job_id)  # noqa: F405
