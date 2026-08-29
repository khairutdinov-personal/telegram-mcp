"""Background export jobs, so the server can export without an MCP tool call
having to last for an hour.

The point of a job here is not concurrency, it is size. A chat export is
hundreds of megabytes and minutes to hours of work; a tool that returned it
would push all of it through the model's context. So the tool starts a job and
gets back an id, the bytes go to disk next to the server, and only counters and
a path ever travel back.
"""

import asyncio
import datetime as dt
import json
import os
import uuid
from pathlib import Path
from typing import Any, Optional

from .run import ExportOptions, export_one
from .util import log

# Live tasks, keyed by job id. A finished job keeps its record but drops the
# task; a record with no task and state "running" means the server restarted
# under it, which is reported rather than pretended away.
_TASKS: dict = {}
_RECORDS: dict = {}


def export_root() -> Path:
    """Where exports are written. Never inside the install directory: in a
    container that is a fresh layer on every rebuild, and the whole point is
    that the files outlive the run."""
    raw = os.getenv("TELEGRAM_EXPORT_DIR")
    if raw:
        base = Path(raw).expanduser()
    else:
        state = os.getenv("XDG_STATE_HOME") or Path.home() / ".local" / "state"
        base = Path(state) / "telegram-mcp" / "exports"
    base.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(base, 0o700)
    except OSError:
        pass
    return base


def _jobs_dir() -> Path:
    d = export_root() / ".jobs"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def _persist(record: dict) -> None:
    """A job record survives a restart, otherwise a crashed export leaves no
    trace of what it had already written."""
    try:
        path = _jobs_dir() / f"{record['job_id']}.json"
        path.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError as error:  # a job must not die because its bookkeeping failed
        log(f"warning: could not persist job record: {error}")


def _load(job_id: str) -> Optional[dict]:
    path = _jobs_dir() / f"{job_id}.json"
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


async def _run(record: dict, client, targets: list, options: ExportOptions) -> None:
    def on_progress(event: dict) -> None:
        record["progress"] = event
        if event.get("stage") in ("started", "finished"):
            _persist(record)

    try:
        for index, target in enumerate(targets, start=1):
            record["current"] = {"index": index, "of": len(targets), "target": target}
            _persist(record)
            try:
                result = await export_one(client, target, options, on_progress=on_progress)
                record["done"].append(
                    {
                        "target": target,
                        "chat": result["meta"]["chat"].get("title"),
                        "messages": result["meta"]["message_count"],
                        "dir": str(result["dir"]),
                    }
                )
            except asyncio.CancelledError:
                raise
            except Exception as error:
                # One unreachable chat out of twenty must not lose the other
                # nineteen; the failure is recorded and the run continues.
                record["failed"].append(
                    {"target": target, "error": f"{type(error).__name__}: {error}"}
                )
            _persist(record)
        record["state"] = "finished" if record["done"] else "failed"
    except asyncio.CancelledError:
        record["state"] = "cancelled"
        raise
    except Exception as error:
        record["state"] = "failed"
        record["error"] = f"{type(error).__name__}: {error}"
    finally:
        record["current"] = None
        record["finished_at"] = _now()
        _persist(record)
        _TASKS.pop(record["job_id"], None)


def start(client, targets: list, options: ExportOptions) -> dict:
    """Start an export in the background and return its record immediately."""
    job_id = uuid.uuid4().hex[:12]
    record: dict = {
        "job_id": job_id,
        "state": "running",
        "started_at": _now(),
        "finished_at": None,
        "targets": list(targets),
        "out": str(options.out),
        "options": {
            "formats": list(options.formats),
            "since": options.since.isoformat() if options.since else None,
            "until": options.until.isoformat() if options.until else None,
            "media": options.media,
            "media_max_mb": options.media_max_mb,
            "transcribe": options.transcribe,
            "raw_included": options.include_raw,
            "resume": options.resume,
        },
        "done": [],
        "failed": [],
        "current": None,
        "progress": None,
        "error": None,
    }
    _RECORDS[job_id] = record
    _persist(record)
    task = asyncio.create_task(_run(record, client, targets, options))
    _TASKS[job_id] = task
    return record


def status(job_id: str) -> Optional[dict]:
    record = _RECORDS.get(job_id) or _load(job_id)
    if record is None:
        return None
    if record.get("state") == "running" and job_id not in _TASKS:
        # The record outlived the process that was writing it.
        record = dict(record, state="interrupted")
    return record


def recent(limit: int = 10) -> list:
    """Newest jobs first, including ones from before a restart."""
    records: dict = dict(_RECORDS)
    for path in _jobs_dir().glob("*.json"):
        job_id = path.stem
        if job_id not in records:
            loaded = _load(job_id)
            if loaded:
                records[job_id] = loaded
    ordered = sorted(records.values(), key=lambda r: r.get("started_at") or "", reverse=True)
    out = []
    for record in ordered[:limit]:
        if record.get("state") == "running" and record["job_id"] not in _TASKS:
            record = dict(record, state="interrupted")
        out.append(record)
    return out


def cancel(job_id: str) -> Any:
    task = _TASKS.get(job_id)
    if task is None:
        return None
    task.cancel()
    return task
