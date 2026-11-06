"""SQLite storage for what has to survive a restart: memories, scheduled tasks, style guides and conversations."""

import json
import os
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

DB_PATH = Path(os.environ.get("DB_PATH", "data/debeloglav.db"))

SCHEMA = """
CREATE TABLE IF NOT EXISTS memories (
    id INTEGER PRIMARY KEY,
    scope TEXT NOT NULL,
    text TEXT NOT NULL,
    author TEXT NOT NULL,
    author_id INTEGER,  -- NULL for memories saved before this column existed
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS tasks (
    id INTEGER PRIMARY KEY,
    scope TEXT NOT NULL,
    channel_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    user_name TEXT NOT NULL,
    prompt TEXT NOT NULL,
    cron TEXT,
    next_run TEXT NOT NULL,
    watch INTEGER NOT NULL,
    mention INTEGER NOT NULL,
    last_report TEXT,
    active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS tasks_due ON tasks (active, next_run);
CREATE TABLE IF NOT EXISTS styles (
    scope TEXT PRIMARY KEY,
    enabled INTEGER NOT NULL DEFAULT 1,
    channels TEXT,  -- JSON list of channel names to learn from; NULL means every channel
    guide TEXT,
    learned_at TEXT  -- NULL means learn at the next check
);
CREATE TABLE IF NOT EXISTS conversations (
    message_id INTEGER PRIMARY KEY,
    session_id TEXT NOT NULL,
    last_used TEXT NOT NULL  -- wall-clock UTC ISO time of the session's last use
);
CREATE INDEX IF NOT EXISTS conversations_session ON conversations (session_id);
"""


@dataclass
class Style:
    enabled: bool = True
    channels: list[str] | None = None
    guide: str | None = None
    learned_at: datetime | None = None


@dataclass
class Memory:
    id: int
    text: str
    author: str
    author_id: int | None = None


@dataclass
class Task:
    id: int
    scope: str
    channel_id: int
    user_id: int
    user_name: str
    prompt: str
    cron: str | None
    next_run: datetime
    watch: bool
    mention: bool
    last_report: str | None


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row
    return db


with _connect() as _db:
    _db.executescript(SCHEMA)
    # Databases created before author_id existed.
    if "author_id" not in {row["name"] for row in _db.execute("PRAGMA table_info(memories)")}:
        _db.execute("ALTER TABLE memories ADD COLUMN author_id INTEGER")


def add_memory(scope: str, text: str, author: str, author_id: int) -> int:
    with _connect() as db:
        return (
            db.execute(
                "INSERT INTO memories (scope, text, author, author_id, created_at) VALUES (?, ?, ?, ?, ?)",
                (scope, text, author, author_id, _now()),
            ).lastrowid
            or 0
        )


def memory(scope: str, memory_id: int) -> Memory | None:
    with _connect() as db:
        row = db.execute(
            "SELECT id, text, author, author_id FROM memories WHERE scope = ? AND id = ?",
            (scope, memory_id),
        ).fetchone()
    return Memory(**row) if row else None


def task(scope: str, task_id: int) -> Task | None:
    with _connect() as db:
        row = db.execute(
            "SELECT * FROM tasks WHERE scope = ? AND id = ? AND active = 1", (scope, task_id)
        ).fetchone()
    return _task(row) if row else None


def delete_memory(scope: str, memory_id: int) -> bool:
    with _connect() as db:
        return (
            db.execute(
                "DELETE FROM memories WHERE scope = ? AND id = ?", (scope, memory_id)
            ).rowcount
            > 0
        )


def memories(scope: str) -> list[Memory]:
    with _connect() as db:
        rows = db.execute(
            "SELECT id, text, author, author_id FROM memories WHERE scope = ? ORDER BY id",
            (scope,),
        ).fetchall()
    return [Memory(**row) for row in rows]


def add_task(
    *,
    scope: str,
    channel_id: int,
    user_id: int,
    user_name: str,
    prompt: str,
    cron: str | None,
    next_run: datetime,
    watch: bool,
    mention: bool,
) -> int:
    with _connect() as db:
        return (
            db.execute(
                """INSERT INTO tasks (scope, channel_id, user_id, user_name, prompt, cron, next_run, watch, mention, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    scope,
                    channel_id,
                    user_id,
                    user_name,
                    prompt,
                    cron,
                    next_run.isoformat(),
                    watch,
                    mention,
                    _now(),
                ),
            ).lastrowid
            or 0
        )


def _task(row: sqlite3.Row) -> Task:
    return Task(
        id=row["id"],
        scope=row["scope"],
        channel_id=row["channel_id"],
        user_id=row["user_id"],
        user_name=row["user_name"],
        prompt=row["prompt"],
        cron=row["cron"],
        next_run=datetime.fromisoformat(row["next_run"]),
        watch=bool(row["watch"]),
        mention=bool(row["mention"]),
        last_report=row["last_report"],
    )


def tasks(scope: str) -> list[Task]:
    with _connect() as db:
        rows = db.execute(
            "SELECT * FROM tasks WHERE scope = ? AND active = 1 ORDER BY next_run",
            (scope,),
        ).fetchall()
    return [_task(row) for row in rows]


def due_tasks(now: datetime) -> list[Task]:
    with _connect() as db:
        rows = db.execute(
            "SELECT * FROM tasks WHERE active = 1 AND next_run <= ? ORDER BY next_run",
            (now.isoformat(),),
        ).fetchall()
    return [_task(row) for row in rows]


def reschedule_task(task_id: int, next_run: datetime | None) -> None:
    """Set the next run, or deactivate the task when there is none."""
    with _connect() as db:
        if next_run is None:
            db.execute("UPDATE tasks SET active = 0 WHERE id = ?", (task_id,))
        else:
            db.execute(
                "UPDATE tasks SET next_run = ? WHERE id = ?",
                (next_run.isoformat(), task_id),
            )


def set_last_report(task_id: int, report: str) -> None:
    with _connect() as db:
        db.execute("UPDATE tasks SET last_report = ? WHERE id = ?", (report, task_id))


def cancel_task(scope: str, task_id: int) -> bool:
    with _connect() as db:
        return (
            db.execute(
                "UPDATE tasks SET active = 0 WHERE scope = ? AND id = ? AND active = 1",
                (scope, task_id),
            ).rowcount
            > 0
        )


def style(scope: str) -> Style:
    with _connect() as db:
        row = db.execute("SELECT * FROM styles WHERE scope = ?", (scope,)).fetchone()
    if row is None:
        return Style()
    return Style(
        enabled=bool(row["enabled"]),
        channels=json.loads(row["channels"]) if row["channels"] else None,
        guide=row["guide"],
        learned_at=datetime.fromisoformat(row["learned_at"]) if row["learned_at"] else None,
    )


def save_style(scope: str, style: Style) -> None:
    with _connect() as db:
        db.execute(
            """INSERT INTO styles (scope, enabled, channels, guide, learned_at) VALUES (?, ?, ?, ?, ?)
               ON CONFLICT (scope) DO UPDATE SET
                 enabled = excluded.enabled, channels = excluded.channels,
                 guide = excluded.guide, learned_at = excluded.learned_at""",
            (
                scope,
                style.enabled,
                json.dumps(style.channels) if style.channels else None,
                style.guide,
                style.learned_at.isoformat() if style.learned_at else None,
            ),
        )


# --- Conversations -----------------------------------------------------------------------------
# Which Agents SDK session each Discord message belongs to, so a reply continues that conversation.
# Kept in SQLite, so the map survives a restart; the sessions themselves are journaled on disk.


def link_messages(session_id: str, *message_ids: int) -> None:
    """Point the messages at their conversation's session, and mark the session used now."""
    now = _now()
    with _connect() as db:
        db.executemany(
            """INSERT INTO conversations (message_id, session_id, last_used) VALUES (?, ?, ?)
               ON CONFLICT (message_id) DO UPDATE SET
                 session_id = excluded.session_id, last_used = excluded.last_used""",
            [(message_id, session_id, now) for message_id in message_ids],
        )
        db.execute(
            "UPDATE conversations SET last_used = ? WHERE session_id = ?",
            (now, session_id),
        )


def session_of(message_id: int) -> str | None:
    """The session the message belongs to, if any."""
    with _connect() as db:
        row = db.execute(
            "SELECT session_id FROM conversations WHERE message_id = ?", (message_id,)
        ).fetchone()
    return row["session_id"] if row else None


def sessions_idle_since(cutoff: str) -> list[str]:
    """The sessions whose last use is before the cutoff (a UTC ISO time), oldest first."""
    with _connect() as db:
        rows = db.execute(
            """SELECT session_id, MAX(last_used) AS last_used FROM conversations
               GROUP BY session_id HAVING last_used < ? ORDER BY last_used""",
            (cutoff,),
        ).fetchall()
    return [row["session_id"] for row in rows]


def delete_session_rows(session_id: str) -> None:
    """Forget every message of the session."""
    with _connect() as db:
        db.execute("DELETE FROM conversations WHERE session_id = ?", (session_id,))
