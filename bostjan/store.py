"""SQLite storage for what has to survive a restart: server memories and scheduled tasks."""

import os
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

DB_PATH = Path(os.environ.get("DB_PATH", "data/bostjan.db"))

SCHEMA = """
CREATE TABLE IF NOT EXISTS memories (
    id INTEGER PRIMARY KEY,
    scope TEXT NOT NULL,
    text TEXT NOT NULL,
    author TEXT NOT NULL,
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
"""


@dataclass
class Memory:
    id: int
    text: str
    author: str


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


def add_memory(scope: str, text: str, author: str) -> int:
    with _connect() as db:
        return (
            db.execute(
                "INSERT INTO memories (scope, text, author, created_at) VALUES (?, ?, ?, ?)",
                (scope, text, author, _now()),
            ).lastrowid
            or 0
        )


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
            "SELECT id, text, author FROM memories WHERE scope = ? ORDER BY id",
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
