"""SQLite storage for what has to survive a restart: memories, tasks, style guides, conversations and settings."""

import json
import os
import sqlite3
from dataclasses import asdict, dataclass, field
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
    guide TEXT,
    learned_at TEXT  -- NULL means learn at the next check
);
CREATE TABLE IF NOT EXISTS blocked_channels (
    guild_id INTEGER NOT NULL,
    channel_id INTEGER NOT NULL,  -- a channel or category id, blocked in that server by an admin
    PRIMARY KEY (guild_id, channel_id)
);
CREATE TABLE IF NOT EXISTS conversations (
    message_id INTEGER PRIMARY KEY,
    session_id TEXT NOT NULL,
    last_used TEXT NOT NULL  -- wall-clock UTC ISO time of the session's last use
);
CREATE INDEX IF NOT EXISTS conversations_session ON conversations (session_id);
CREATE TABLE IF NOT EXISTS activity (
    id INTEGER PRIMARY KEY,
    at TEXT NOT NULL,
    kind TEXT NOT NULL,  -- 'reply' | 'research' | 'task'
    scope TEXT NOT NULL,
    channel_id INTEGER,
    user_name TEXT,
    question TEXT,
    prompt TEXT,  -- the text the model received for the turn; NULL when it never started
    reply TEXT,
    ok INTEGER NOT NULL,
    error TEXT,
    duration_ms INTEGER,
    trace TEXT  -- JSON: the request's stages, steps and settings, see debeloglav.trace
);
CREATE INDEX IF NOT EXISTS activity_at ON activity (at);
CREATE TABLE IF NOT EXISTS style_runs (
    id INTEGER PRIMARY KEY,
    scope TEXT NOT NULL,
    at TEXT NOT NULL,
    messages INTEGER NOT NULL,
    guide TEXT,  -- NULL when the run was skipped or failed
    error TEXT,
    skipped TEXT  -- JSON list of "#channel: why" for the channels left out
);
CREATE TABLE IF NOT EXISTS usage (
    day TEXT NOT NULL,  -- local date, YYYY-MM-DD
    model TEXT NOT NULL,
    calls INTEGER NOT NULL,
    input_tokens INTEGER NOT NULL,
    cached_tokens INTEGER NOT NULL,  -- the part of input_tokens that was read from the cache
    output_tokens INTEGER NOT NULL,
    PRIMARY KEY (day, model)
);
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""


@dataclass
class Style:
    enabled: bool = True
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


@dataclass
class Activity:
    id: int
    at: datetime
    kind: str  # 'reply' | 'research' | 'task'
    scope: str
    channel_id: int | None
    user_name: str | None
    question: str | None
    prompt: str | None
    reply: str | None
    ok: bool
    error: str | None
    duration_ms: int | None
    trace: dict = field(default_factory=dict)


@dataclass
class Usage:
    day: str  # local date, YYYY-MM-DD
    model: str
    calls: int
    input_tokens: int
    cached_tokens: int
    output_tokens: int


@dataclass
class StyleRun:
    id: int
    scope: str
    at: datetime
    messages: int
    guide: str | None
    error: str | None
    skipped: list[str]


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
    if "author_id" not in {
        row["name"] for row in _db.execute("PRAGMA table_info(memories)")
    }:
        _db.execute("ALTER TABLE memories ADD COLUMN author_id INTEGER")
    # Databases created before skipped channels were recorded.
    if "skipped" not in {
        row["name"] for row in _db.execute("PRAGMA table_info(style_runs)")
    }:
        _db.execute("ALTER TABLE style_runs ADD COLUMN skipped TEXT")
    # Databases created before requests were traced.
    if "trace" not in {
        row["name"] for row in _db.execute("PRAGMA table_info(activity)")
    }:
        _db.execute("ALTER TABLE activity ADD COLUMN trace TEXT")
    # Databases from when style learning could be limited to a list of channels.
    if "channels" in {row["name"] for row in _db.execute("PRAGMA table_info(styles)")}:
        _db.execute("ALTER TABLE styles DROP COLUMN channels")


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
            "SELECT * FROM tasks WHERE scope = ? AND id = ? AND active = 1",
            (scope, task_id),
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


def _style(row: sqlite3.Row) -> Style:
    return Style(
        enabled=bool(row["enabled"]),
        guide=row["guide"],
        learned_at=datetime.fromisoformat(row["learned_at"])
        if row["learned_at"]
        else None,
    )


def style(scope: str) -> Style:
    with _connect() as db:
        row = db.execute("SELECT * FROM styles WHERE scope = ?", (scope,)).fetchone()
    return _style(row) if row is not None else Style()


def save_style(scope: str, style: Style) -> None:
    with _connect() as db:
        db.execute(
            """INSERT INTO styles (scope, enabled, guide, learned_at) VALUES (?, ?, ?, ?)
               ON CONFLICT (scope) DO UPDATE SET
                 enabled = excluded.enabled, guide = excluded.guide,
                 learned_at = excluded.learned_at""",
            (
                scope,
                style.enabled,
                style.guide,
                style.learned_at.isoformat() if style.learned_at else None,
            ),
        )


def configure_style(
    scope: str,
    *,
    enabled: bool | None = None,
    relearn: bool = False,
) -> Style:
    """Change and save a server's style-learning settings (from the dashboard), and return them."""
    current = style(scope)
    if enabled is not None:
        current.enabled = enabled
        if not enabled:
            current.guide = None  # turning learning off also deletes the learned guide
        current.learned_at = None
    if relearn:
        current.guide, current.learned_at = None, None
    save_style(scope, current)
    return current


def block_channel(guild_id: int, channel_id: int) -> None:
    with _connect() as db:
        db.execute(
            "INSERT OR IGNORE INTO blocked_channels (guild_id, channel_id) VALUES (?, ?)",
            (guild_id, channel_id),
        )


def unblock_channel(guild_id: int, channel_id: int) -> bool:
    with _connect() as db:
        return (
            db.execute(
                "DELETE FROM blocked_channels WHERE guild_id = ? AND channel_id = ?",
                (guild_id, channel_id),
            ).rowcount
            > 0
        )


def blocked_channels(guild_id: int) -> list[int]:
    with _connect() as db:
        rows = db.execute(
            "SELECT channel_id FROM blocked_channels WHERE guild_id = ? ORDER BY channel_id",
            (guild_id,),
        ).fetchall()
    return [row["channel_id"] for row in rows]


def all_blocked_channel_ids() -> list[int]:
    """Every id blocked in any server. Channel ids are unique across Discord, so one set serves all servers."""
    with _connect() as db:
        rows = db.execute("SELECT channel_id FROM blocked_channels").fetchall()
    return [row["channel_id"] for row in rows]


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


def session_ids() -> set[str]:
    """Every session a stored message points at."""
    with _connect() as db:
        rows = db.execute("SELECT DISTINCT session_id FROM conversations").fetchall()
    return {row["session_id"] for row in rows}


# --- Activity ------------------------------------------------------------------------------------
# What the bot did: every answered message, research job and scheduled task run, for the admin
# dashboard. Only the newest ACTIVITY_LIMIT rows are kept, so the table stays small enough to
# read whole.


ACTIVITY_LIMIT = 2000
MAX_ACTIVITY_TEXT = 20_000  # stored prompts and replies are capped, so one huge answer cannot bloat the table


def log_activity(
    kind: str,
    scope: str,
    channel_id: int | None = None,
    user_name: str | None = None,
    question: str | None = None,
    prompt: str | None = None,
    reply: str | None = None,
    ok: bool = True,
    error: str | None = None,
    duration_ms: int | None = None,
    trace: dict | None = None,
) -> None:
    """Record one thing the bot did, and drop the rows older than the newest ACTIVITY_LIMIT."""
    with _connect() as db:
        db.execute(
            """INSERT INTO activity (at, kind, scope, channel_id, user_name, question, prompt, reply, ok, error, duration_ms, trace)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                _now(),
                kind,
                scope,
                channel_id,
                user_name,
                question,
                prompt[:MAX_ACTIVITY_TEXT] if prompt is not None else None,
                reply[:MAX_ACTIVITY_TEXT] if reply is not None else None,
                ok,
                error,
                duration_ms,
                json.dumps(trace, ensure_ascii=False, default=str) if trace else None,
            ),
        )
        db.execute(
            "DELETE FROM activity WHERE id NOT IN"
            " (SELECT id FROM activity ORDER BY at DESC, id DESC LIMIT ?)",
            (ACTIVITY_LIMIT,),
        )


def _activity(row: sqlite3.Row) -> Activity:
    return Activity(
        id=row["id"],
        at=datetime.fromisoformat(row["at"]),
        kind=row["kind"],
        scope=row["scope"],
        channel_id=row["channel_id"],
        user_name=row["user_name"],
        question=row["question"],
        prompt=row["prompt"],
        reply=row["reply"],
        ok=bool(row["ok"]),
        error=row["error"],
        duration_ms=row["duration_ms"],
        trace=json.loads(row["trace"]) if row["trace"] else {},
    )


def recent_activity(limit: int = 100) -> list[Activity]:
    """The newest activity rows, newest first."""
    with _connect() as db:
        rows = db.execute(
            "SELECT * FROM activity ORDER BY at DESC, id DESC LIMIT ?", (limit,)
        ).fetchall()
    return [_activity(row) for row in rows]


def as_json(activity: "Activity | StyleRun") -> dict:
    """A row as plain JSON values, for the dashboard's exports."""
    data = asdict(activity)
    data["at"] = activity.at.isoformat()
    return data


def activity(activity_id: int) -> Activity | None:
    """One activity row by its id, for the dashboard's request detail page."""
    with _connect() as db:
        row = db.execute(
            "SELECT * FROM activity WHERE id = ?", (activity_id,)
        ).fetchone()
    return _activity(row) if row else None


# --- Style runs ----------------------------------------------------------------------------------
# Every style-learning run, including the skipped and the failed ones, so the guide's history
# survives even though the guide itself is overwritten each time.


def log_style_run(
    scope: str,
    messages: int,
    guide: str | None = None,
    error: str | None = None,
    skipped: list[str] | None = None,
) -> None:
    with _connect() as db:
        db.execute(
            "INSERT INTO style_runs (scope, at, messages, guide, error, skipped) VALUES (?, ?, ?, ?, ?, ?)",
            (
                scope,
                _now(),
                messages,
                guide,
                error,
                json.dumps(skipped) if skipped else None,
            ),
        )


def _style_run(row: sqlite3.Row) -> StyleRun:
    return StyleRun(
        id=row["id"],
        scope=row["scope"],
        at=datetime.fromisoformat(row["at"]),
        messages=row["messages"],
        guide=row["guide"],
        error=row["error"],
        skipped=json.loads(row["skipped"]) if row["skipped"] else [],
    )


def style_runs(scope: str | None = None, limit: int = 50) -> list[StyleRun]:
    """The newest style-learning runs, newest first, of one server or of all of them."""
    query = "SELECT * FROM style_runs"
    params: list[object] = []
    if scope is not None:
        query += " WHERE scope = ?"
        params.append(scope)
    query += " ORDER BY at DESC, id DESC LIMIT ?"
    params.append(limit)
    with _connect() as db:
        rows = db.execute(query, params).fetchall()
    return [_style_run(row) for row in rows]


# --- Usage ---------------------------------------------------------------------------------------
# The tokens every model call used, added up per day and model. Kept apart from the activity table,
# which holds only the newest rows, so the totals stay complete.


def add_usage(
    day: str,
    model: str,
    input_tokens: int,
    output_tokens: int,
    cached_tokens: int = 0,
    calls: int = 1,
) -> None:
    with _connect() as db:
        db.execute(
            """INSERT INTO usage (day, model, calls, input_tokens, cached_tokens, output_tokens)
               VALUES (?, ?, ?, ?, ?, ?)
               ON CONFLICT (day, model) DO UPDATE SET
                 calls = calls + excluded.calls,
                 input_tokens = input_tokens + excluded.input_tokens,
                 cached_tokens = cached_tokens + excluded.cached_tokens,
                 output_tokens = output_tokens + excluded.output_tokens""",
            (day, model, calls, input_tokens, cached_tokens, output_tokens),
        )


def usage() -> list[Usage]:
    """The usage of every day and model, newest day first."""
    with _connect() as db:
        rows = db.execute("SELECT * FROM usage ORDER BY day DESC, model").fetchall()
    return [Usage(**row) for row in rows]


# --- Settings ------------------------------------------------------------------------------------
# Admin-editable settings (model, reasoning effort, extra instructions), stored so a dashboard can
# change them at runtime; a missing key falls back to the default read from the environment.


def get_setting(key: str) -> str | None:
    with _connect() as db:
        row = db.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else None


def set_setting(key: str, value: str | None) -> None:
    """Save a setting; None deletes the row, so the key falls back to its default."""
    with _connect() as db:
        if value is None:
            db.execute("DELETE FROM settings WHERE key = ?", (key,))
        else:
            db.execute(
                """INSERT INTO settings (key, value, updated_at) VALUES (?, ?, ?)
                   ON CONFLICT (key) DO UPDATE SET
                     value = excluded.value, updated_at = excluded.updated_at""",
                (key, value, _now()),
            )


def all_settings() -> dict[str, str]:
    with _connect() as db:
        rows = db.execute("SELECT key, value FROM settings ORDER BY key").fetchall()
    return {row["key"]: row["value"] for row in rows}


# --- Everything, for the admin dashboard ---------------------------------------------------------


def all_memories() -> list[Memory]:
    with _connect() as db:
        rows = db.execute(
            "SELECT id, text, author, author_id FROM memories ORDER BY id"
        ).fetchall()
    return [Memory(**row) for row in rows]


def all_memories_by_scope() -> list[tuple[str, Memory]]:
    """Every memory with its scope, as (scope, memory) pairs, oldest first — for the dashboard."""
    with _connect() as db:
        rows = db.execute(
            "SELECT scope, id, text, author, author_id FROM memories ORDER BY id"
        ).fetchall()
    return [
        (row["scope"], Memory(row["id"], row["text"], row["author"], row["author_id"]))
        for row in rows
    ]


def all_tasks(active_only: bool = True) -> list[Task]:
    query = "SELECT * FROM tasks"
    params: list[object] = []
    if active_only:
        query += " WHERE active = 1"
    query += " ORDER BY next_run"
    with _connect() as db:
        rows = db.execute(query, params).fetchall()
    return [_task(row) for row in rows]


def all_styles() -> list[tuple[str, Style]]:
    """Every server's style settings and learned guide, as (scope, style) pairs."""
    with _connect() as db:
        rows = db.execute("SELECT * FROM styles ORDER BY scope").fetchall()
    return [(row["scope"], _style(row)) for row in rows]


def all_blocked_channels() -> list[tuple[int, int]]:
    """Every (guild_id, channel_id) blocked anywhere."""
    with _connect() as db:
        rows = db.execute(
            "SELECT guild_id, channel_id FROM blocked_channels ORDER BY guild_id, channel_id"
        ).fetchall()
    return [(row["guild_id"], row["channel_id"]) for row in rows]


def conversation_count() -> int:
    """How many distinct conversations (sessions) the stored messages point at."""
    with _connect() as db:
        row = db.execute(
            "SELECT COUNT(DISTINCT session_id) AS n FROM conversations"
        ).fetchone()
    return int(row["n"])
