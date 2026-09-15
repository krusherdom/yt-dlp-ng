"""SQLite persistence for jobs (aiosqlite, WAL, single async writer lock)."""

from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional

import aiosqlite

from . import config

_conn: Optional[aiosqlite.Connection] = None
_write_lock = asyncio.Lock()

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id          TEXT PRIMARY KEY,
    url         TEXT NOT NULL,
    title       TEXT,
    preset      TEXT NOT NULL DEFAULT 'best',
    subfolder   TEXT NOT NULL DEFAULT '',
    extra_args  TEXT NOT NULL DEFAULT '',
    status      TEXT NOT NULL DEFAULT 'queued',
    progress    REAL NOT NULL DEFAULT 0,
    speed       TEXT,
    eta         TEXT,
    filename    TEXT,
    error       TEXT,
    parent_id   TEXT,
    type        TEXT NOT NULL DEFAULT 'single',
    proxy       TEXT,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status);
CREATE INDEX IF NOT EXISTS idx_jobs_parent ON jobs(parent_id);
CREATE INDEX IF NOT EXISTS idx_jobs_created ON jobs(created_at);
"""

_UPDATABLE = {
    "url",
    "title",
    "preset",
    "subfolder",
    "extra_args",
    "status",
    "progress",
    "speed",
    "eta",
    "filename",
    "error",
    "parent_id",
    "type",
    "proxy",
}

#: column -> DDL type for columns added after v0.1. Applied by _migrate().
_ADDED_COLUMNS = {
    "proxy": "TEXT",
}


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def new_id() -> str:
    return str(uuid.uuid4())


async def init_db() -> None:
    """Open the connection and create the schema. Idempotent."""
    global _conn
    if _conn is not None:
        return
    config.ensure_dirs()
    conn = await aiosqlite.connect(str(config.DB_PATH))
    conn.row_factory = aiosqlite.Row
    await conn.execute("PRAGMA journal_mode=WAL")
    await conn.execute("PRAGMA synchronous=NORMAL")
    await conn.execute("PRAGMA foreign_keys=ON")
    await conn.executescript(SCHEMA)
    await _migrate(conn)
    await conn.commit()
    _conn = conn


async def _migrate(conn: aiosqlite.Connection) -> int:
    """Add columns missing from a database created by an older version.

    ``CREATE TABLE IF NOT EXISTS`` is a no-op on an existing table, so new
    columns have to be bolted on explicitly. Guarded by ``PRAGMA table_info``
    and therefore safe to run on every startup.
    """
    async with conn.execute("PRAGMA table_info(jobs)") as cur:
        rows = await cur.fetchall()
    existing = {r["name"] for r in rows}
    added = 0
    for column, ddl in _ADDED_COLUMNS.items():
        if column not in existing:
            await conn.execute(f"ALTER TABLE jobs ADD COLUMN {column} {ddl}")
            added += 1
    if added:
        await conn.commit()
    return added


async def close_db() -> None:
    global _conn
    if _conn is not None:
        await _conn.close()
        _conn = None


def _require() -> aiosqlite.Connection:
    if _conn is None:
        raise RuntimeError("Database not initialised; call init_db() first")
    return _conn


def _row_to_dict(row: aiosqlite.Row) -> Dict[str, Any]:
    d = dict(row)
    d["progress"] = float(d.get("progress") or 0.0)
    # Columns added by a migration are absent from rows read through an older
    # connection; keep the job dict shape stable for the API and the UI.
    d.setdefault("proxy", None)
    return d


async def create_job(
    *,
    url: str,
    preset: str = "best",
    subfolder: str = "",
    extra_args: str = "",
    title: Optional[str] = None,
    parent_id: Optional[str] = None,
    job_type: str = "single",
    status: str = "queued",
    job_id: Optional[str] = None,
) -> Dict[str, Any]:
    conn = _require()
    jid = job_id or new_id()
    ts = now_iso()
    async with _write_lock:
        await conn.execute(
            """INSERT INTO jobs
               (id, url, title, preset, subfolder, extra_args, status, progress,
                speed, eta, filename, error, parent_id, type, created_at, updated_at)
               VALUES (?,?,?,?,?,?,?,0,NULL,NULL,NULL,NULL,?,?,?,?)""",
            (
                jid,
                url,
                title,
                preset,
                subfolder or "",
                extra_args or "",
                status,
                parent_id,
                job_type,
                ts,
                ts,
            ),
        )
        await conn.commit()
    job = await get_job(jid)
    assert job is not None
    return job


async def create_jobs_bulk(rows: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Insert several jobs in one transaction. Each row accepts the same
    keyword names as :func:`create_job`."""
    conn = _require()
    payload = []
    ids: List[str] = []
    ts = now_iso()
    for r in rows:
        jid = r.get("job_id") or new_id()
        ids.append(jid)
        payload.append(
            (
                jid,
                r["url"],
                r.get("title"),
                r.get("preset", "best"),
                r.get("subfolder") or "",
                r.get("extra_args") or "",
                r.get("status", "queued"),
                r.get("parent_id"),
                r.get("job_type", "single"),
                ts,
                ts,
            )
        )
    if not payload:
        return []
    async with _write_lock:
        await conn.executemany(
            """INSERT INTO jobs
               (id, url, title, preset, subfolder, extra_args, status, progress,
                speed, eta, filename, error, parent_id, type, created_at, updated_at)
               VALUES (?,?,?,?,?,?,?,0,NULL,NULL,NULL,NULL,?,?,?,?)""",
            payload,
        )
        await conn.commit()
    return [j for j in (await get_jobs_by_ids(ids))]


async def get_job(job_id: str) -> Optional[Dict[str, Any]]:
    conn = _require()
    async with conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)) as cur:
        row = await cur.fetchone()
    return _row_to_dict(row) if row else None


async def get_jobs_by_ids(ids: List[str]) -> List[Dict[str, Any]]:
    if not ids:
        return []
    conn = _require()
    marks = ",".join("?" * len(ids))
    async with conn.execute(
        f"SELECT * FROM jobs WHERE id IN ({marks}) ORDER BY created_at ASC, rowid ASC", ids
    ) as cur:
        rows = await cur.fetchall()
    return [_row_to_dict(r) for r in rows]


async def list_jobs(status: Optional[str] = None) -> List[Dict[str, Any]]:
    conn = _require()
    if status:
        sql = "SELECT * FROM jobs WHERE status = ? ORDER BY created_at ASC, rowid ASC"
        args: tuple = (status,)
    else:
        sql = "SELECT * FROM jobs ORDER BY created_at ASC, rowid ASC"
        args = ()
    async with conn.execute(sql, args) as cur:
        rows = await cur.fetchall()
    return [_row_to_dict(r) for r in rows]


async def list_children(parent_id: str) -> List[Dict[str, Any]]:
    conn = _require()
    async with conn.execute(
        "SELECT * FROM jobs WHERE parent_id = ? ORDER BY created_at ASC, rowid ASC", (parent_id,)
    ) as cur:
        rows = await cur.fetchall()
    return [_row_to_dict(r) for r in rows]


async def list_resumable() -> List[Dict[str, Any]]:
    """Jobs that should be requeued at startup: queued/running, excluding
    playlist parents (re-extracting those would duplicate children)."""
    conn = _require()
    async with conn.execute(
        """SELECT * FROM jobs
           WHERE status IN ('queued','running') AND type != 'playlist'
           ORDER BY created_at ASC, rowid ASC"""
    ) as cur:
        rows = await cur.fetchall()
    return [_row_to_dict(r) for r in rows]


async def update_job(job_id: str, **fields: Any) -> Optional[Dict[str, Any]]:
    updates = {k: v for k, v in fields.items() if k in _UPDATABLE}
    if not updates:
        return await get_job(job_id)
    conn = _require()
    updates["updated_at"] = now_iso()
    assignments = ", ".join(f"{k} = ?" for k in updates)
    values = list(updates.values()) + [job_id]
    async with _write_lock:
        await conn.execute(f"UPDATE jobs SET {assignments} WHERE id = ?", values)
        await conn.commit()
    return await get_job(job_id)


async def delete_job(job_id: str) -> int:
    """Delete a job and any children. Returns the number of rows removed."""
    conn = _require()
    async with _write_lock:
        cur = await conn.execute(
            "DELETE FROM jobs WHERE id = ? OR parent_id = ?", (job_id, job_id)
        )
        await conn.commit()
        return cur.rowcount or 0


async def count_children_by_status(parent_id: str) -> Dict[str, int]:
    conn = _require()
    async with conn.execute(
        "SELECT status, COUNT(*) AS n FROM jobs WHERE parent_id = ? GROUP BY status",
        (parent_id,),
    ) as cur:
        rows = await cur.fetchall()
    return {r["status"]: int(r["n"]) for r in rows}


async def mark_interrupted_running() -> int:
    """Flip leftover 'running' rows back to 'queued' at startup."""
    conn = _require()
    async with _write_lock:
        cur = await conn.execute(
            "UPDATE jobs SET status='queued', updated_at=? WHERE status='running'",
            (now_iso(),),
        )
        await conn.commit()
        return cur.rowcount or 0
