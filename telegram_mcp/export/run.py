"""The export engine, shared by the CLI and the MCP job tools.

Both entry points must produce byte-identical output, so neither owns the
export: the CLI drives this module with a session of its own, and the MCP
server drives it with the client it is already logged in as. Progress leaves
through a callback rather than a print, because one caller writes to a
terminal and the other writes to a job record.
"""

import datetime as dt
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

from .fetch import chat_meta, iter_records, resolve_target
from .render import read_records, render
from .util import json_default, log, safe_name

ProgressCallback = Callable[[dict], None]


@dataclass
class ExportOptions:
    """One export run, as the engine needs it.

    Deliberately not an argparse Namespace: the MCP tools have no argparse, and
    a shared engine that reads ``args.no_raw`` would tie the server to the CLI's
    flag spelling.
    """

    out: Path
    formats: list = field(default_factory=lambda: ["jsonl"])
    since: Optional[dt.datetime] = None
    until: Optional[dt.datetime] = None
    media: bool = False
    media_max_mb: Optional[float] = None
    transcribe: Optional[str] = None
    include_raw: bool = True
    resume: bool = False

    @property
    def media_max_bytes(self) -> Optional[int]:
        return int(self.media_max_mb * 1024 * 1024) if self.media_max_mb else None


def resume_floor(export_dir: Path) -> int:
    """Highest message id already written, so a resume appends instead of
    re-downloading the history it has."""
    source = export_dir / "messages.jsonl"
    if not source.exists():
        return 0
    last = 0
    for record in read_records(source):
        last = max(last, int(record.get("id") or 0))
    return last


def _tool_version() -> str:
    try:
        from importlib.metadata import version as dist_version

        return dist_version("telegram-mcp")
    except Exception:  # running from a source checkout without an install
        return "dev"


async def export_one(
    client,
    target: str,
    options: ExportOptions,
    on_progress: Optional[ProgressCallback] = None,
) -> dict:
    """Export one chat into its own folder under ``options.out``.

    Returns ``{"dir": Path, "meta": dict}``. Raises whatever resolution or the
    network raises; the caller decides whether one bad target kills the run.
    """

    def report(**event: Any) -> None:
        if on_progress is not None:
            on_progress(dict(event, target=target))

    entity = await resolve_target(client, target)
    meta = await chat_meta(client, entity)
    folder = f"{safe_name(meta['title'] or 'chat')}_{meta['id']}"
    export_dir = Path(options.out).expanduser() / folder
    export_dir.mkdir(parents=True, exist_ok=True)

    min_id = resume_floor(export_dir) if options.resume else 0
    mode = "a" if (options.resume and min_id) else "w"
    jsonl_path = export_dir / "messages.jsonl"
    if mode == "w" and jsonl_path.exists():
        jsonl_path.unlink()

    log(
        f"→ {meta['title']} ({meta['type']}, id {meta['id']})"
        + (f" · resuming after message {min_id}" if min_id else "")
    )
    report(stage="started", chat=meta["title"], chat_id=meta["id"], resumed_from=min_id or None)

    media_root = export_dir / "media" if options.media else None
    if media_root:
        media_root.mkdir(parents=True, exist_ok=True)

    people: dict = {}
    count = 0
    first_date = last_date = None

    with jsonl_path.open(mode, encoding="utf-8") as handle:
        async for record in iter_records(
            client,
            entity,
            since=options.since,
            until=options.until,
            min_id=min_id,
            media_root=media_root,
            media_max_bytes=options.media_max_bytes,
            transcribe_engine=options.transcribe,
            include_raw=options.include_raw,
            people=people,
        ):
            handle.write(json.dumps(record, default=json_default, ensure_ascii=False) + "\n")
            count += 1
            stamp = record.get("date")
            if stamp is not None:
                stamp = stamp.isoformat() if hasattr(stamp, "isoformat") else str(stamp)
                first_date = first_date or stamp
                last_date = stamp
            if count % 200 == 0:
                report(stage="running", chat=meta["title"], messages=count, at=last_date)

    # On a resume the window has to describe the whole file, not just the tail.
    total = count
    if mode == "a":
        rows = list(read_records(jsonl_path))
        total = len(rows)
        first_date = rows[0].get("date") if rows else first_date
        last_date = rows[-1].get("date") if rows else last_date

    meta_payload = {
        "tool": f"telegram-mcp-export {_tool_version()}",
        "exported_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "chat": meta,
        "message_count": total,
        "added_this_run": count,
        "window": {
            "requested_since": options.since.isoformat() if options.since else None,
            "requested_until": options.until.isoformat() if options.until else None,
            "first": first_date,
            "last": last_date,
        },
        "options": {
            "media": bool(options.media),
            "media_max_mb": options.media_max_mb or None,
            "transcribe": options.transcribe,
            "raw_included": options.include_raw,
            "formats": list(options.formats),
        },
        "people": {str(k): v for k, v in sorted(people.items())},
    }
    (export_dir / "meta.json").write_text(
        json.dumps(meta_payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    log(f"  jsonl: {count} new message(s), {total} total → {jsonl_path}")
    render(export_dir, meta_payload, list(options.formats))
    report(stage="finished", chat=meta["title"], messages=total, added=count, dir=str(export_dir))
    return {"dir": export_dir, "meta": meta_payload}
