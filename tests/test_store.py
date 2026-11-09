from datetime import UTC, datetime, timedelta

import pytest

from debeloglav import bot, store


@pytest.fixture(autouse=True)
def clean_tables():
    """Each test starts with empty tables: conftest points all tests at one temp database."""
    with store._connect() as db:
        for table in (
            "memories",
            "tasks",
            "styles",
            "blocked_channels",
            "conversations",
            "activity",
            "style_runs",
            "settings",
        ):
            db.execute(f"DELETE FROM {table}")


def test_memories_are_kept_per_server():
    first = store.add_memory("guild:mem1", "Tim je admin", "Tim (@tim)", 1)
    store.add_memory("guild:mem2", "drug server", "Luka (@luka)", 2)
    assert [m.text for m in store.memories("guild:mem1")] == ["Tim je admin"]
    saved = store.memory("guild:mem1", first)
    assert saved is not None and saved.author_id == 1
    assert store.memory("guild:mem2", first) is None  # not across servers


def test_deleting_a_memory():
    memory_id = store.add_memory("guild:mem3", "pozabi me", "Tim (@tim)", 1)
    assert not store.delete_memory("guild:other", memory_id)
    assert store.delete_memory("guild:mem3", memory_id)
    assert store.memories("guild:mem3") == []


def add_task(scope, next_run, cron=None):
    return store.add_task(
        scope=scope,
        channel_id=10,
        user_id=1,
        user_name="Tim (@tim)",
        prompt="Spomni Tima",
        cron=cron,
        next_run=next_run,
        watch=False,
        mention=True,
    )


def test_due_tasks_and_rescheduling():
    now = datetime.now(UTC)
    due = add_task("guild:tasks1", now - timedelta(minutes=1), cron="0 8 * * *")
    later = add_task("guild:tasks1", now + timedelta(hours=1))
    assert due in {t.id for t in store.due_tasks(now)}
    assert later not in {t.id for t in store.due_tasks(now)}

    store.reschedule_task(due, now + timedelta(days=1))
    assert due not in {t.id for t in store.due_tasks(now)}
    store.reschedule_task(later, None)  # a one-off that ran
    assert [t.id for t in store.tasks("guild:tasks1")] == [due]


def test_cancelling_a_task():
    task_id = add_task("guild:tasks2", datetime.now(UTC) + timedelta(hours=1))
    assert not store.cancel_task("guild:other", task_id)
    assert store.cancel_task("guild:tasks2", task_id)
    assert store.task("guild:tasks2", task_id) is None
    assert not store.cancel_task("guild:tasks2", task_id)  # already cancelled


def test_last_report():
    task_id = add_task(
        "guild:tasks3", datetime.now(UTC) + timedelta(hours=1), cron="0 * * * *"
    )
    store.set_last_report(task_id, "GTA 6 še ni izšel")
    task = store.task("guild:tasks3", task_id)
    assert task is not None and task.last_report == "GTA 6 še ni izšel"


def test_style_defaults_and_saving():
    assert store.style("guild:style1") == store.Style()
    learned = datetime(2026, 11, 1, tzinfo=UTC)
    store.save_style(
        "guild:style1", store.Style(False, "## Ton", learned)
    )
    assert store.style("guild:style1") == store.Style(False, "## Ton", learned)
    store.save_style("guild:style1", store.Style())
    assert store.style("guild:style1") == store.Style()


def test_configure_style():
    scope = "guild:style2"
    learned = datetime(2026, 11, 1, tzinfo=UTC)
    store.save_style(scope, store.Style(True, "## Ton", learned))
    # turning learning off also deletes the guide and learned_at
    saved = store.configure_style(scope, enabled=False)
    assert saved == store.Style(False, None, None)
    assert store.style(scope) == saved
    # relearn clears the guide and learned_at, but keeps the settings
    store.save_style(scope, store.Style(True, "## Ton", learned))
    saved = store.configure_style(scope, relearn=True)
    assert saved == store.Style(True, None, None)


def test_blocked_channels_per_server():
    store.block_channel(11, 500)
    store.block_channel(11, 501)
    store.block_channel(12, 502)
    assert store.blocked_channels(11) == [500, 501]
    assert store.blocked_channels(12) == [502]
    assert store.blocked_channels(13) == []
    store.block_channel(11, 500)  # blocking again changes nothing
    assert store.blocked_channels(11) == [500, 501]
    assert 502 in store.all_blocked_channel_ids()
    assert not store.unblock_channel(11, 502)  # blocked in another server
    assert store.unblock_channel(11, 500)
    assert store.blocked_channels(11) == [501]
    assert not store.unblock_channel(11, 500)  # already unblocked


def test_activity_is_logged_and_read_newest_first():
    store.log_activity(
        "reply",
        "guild:act1",
        channel_id=10,
        user_name="Tim (@tim)",
        question="Kaj je novega?",
        prompt="Message that tagged you:\nTim: kaj je novega?",
        reply="Nič novega.",
        ok=True,
        duration_ms=1234,
    )
    store.log_activity(
        "research",
        "guild:act1",
        channel_id=10,
        user_name="Tim (@tim)",
        question="Kako daleč je luna?",
        prompt="Research question from Tim (@tim):\nKako daleč je luna?",
        reply="384.400 km.",
        ok=False,
        error="TimeoutError: too slow",
        duration_ms=20_000,
    )
    recent = store.recent_activity()
    assert [a.kind for a in recent] == ["research", "reply"]  # newest first
    newest, oldest = recent
    assert newest.scope == "guild:act1" and newest.channel_id == 10
    assert newest.user_name == "Tim (@tim)" and newest.at is not None
    assert newest.question == "Kako daleč je luna?"
    assert newest.prompt == "Research question from Tim (@tim):\nKako daleč je luna?"
    assert newest.reply == "384.400 km."
    assert newest.ok is False and newest.error == "TimeoutError: too slow"
    assert newest.duration_ms == 20_000
    assert oldest.ok is True and oldest.error is None and oldest.reply == "Nič novega."
    assert len(store.recent_activity(limit=1)) == 1


def test_activity_keeps_only_the_newest_2000_rows():
    with store._connect() as db:
        base = datetime.now(UTC) - timedelta(minutes=60)
        db.executemany(
            "INSERT INTO activity (at, kind, scope, question, ok)"
            " VALUES (?, 'reply', 'guild:prune', ?, 1)",
            [
                ((base + timedelta(seconds=i)).isoformat(), f"old-{i}")
                for i in range(store.ACTIVITY_LIMIT)
            ],
        )
    store.log_activity("reply", "guild:prune", question="newest", ok=True)
    rows = store.recent_activity(limit=store.ACTIVITY_LIMIT + 10)
    assert len(rows) == store.ACTIVITY_LIMIT
    assert rows[0].question == "newest"  # newest first
    questions = {row.question for row in rows}
    assert "old-0" not in questions  # the oldest row was pruned on insert
    assert "old-1" in questions


def test_activity_prompt_and_reply_are_truncated():
    store.log_activity("reply", "guild:act2", prompt="p" * 30_000, reply="r" * 30_000)
    (row,) = store.recent_activity()
    assert row.prompt == "p" * store.MAX_ACTIVITY_TEXT
    assert row.reply == "r" * store.MAX_ACTIVITY_TEXT


def test_record_activity_never_breaks_answering(monkeypatch):
    def broken(**kwargs):
        raise RuntimeError("database is gone")

    monkeypatch.setattr(store, "log_activity", broken)
    bot.record_activity(
        kind="reply", scope="guild:act3", question="q", reply="r", ok=True
    )


def test_style_runs_are_logged_newest_first():
    store.log_style_run("guild:sr1", 42, "## Ton\nPrijazen", None)
    store.log_style_run("guild:sr1", 7, None, None, ["#tajno: blocked"])  # skipped
    store.log_style_run("guild:sr2", 100, "## Ton", None)
    store.log_style_run("guild:sr1", 0, None, "RuntimeError: boom")  # failed
    runs = store.style_runs()
    assert [r.scope for r in runs] == [
        "guild:sr1",
        "guild:sr2",
        "guild:sr1",
        "guild:sr1",
    ]
    failed, other, skipped, summarized = runs
    assert failed.messages == 0 and failed.guide is None
    assert failed.error == "RuntimeError: boom"
    assert other.scope == "guild:sr2" and other.messages == 100
    assert skipped.messages == 7 and skipped.guide is None and skipped.error is None
    assert skipped.skipped == ["#tajno: blocked"] and summarized.skipped == []
    assert summarized.messages == 42 and summarized.guide == "## Ton\nPrijazen"
    assert [r.messages for r in store.style_runs("guild:sr1")] == [0, 7, 42]
    assert len(store.style_runs(limit=2)) == 2


def test_dashboard_reads_everything_across_scopes():
    store.add_memory("guild:a", "m1", "Tim (@tim)", 1)
    store.add_memory("guild:b", "m2", "Luka (@luka)", 2)
    assert [(m.text, m.author) for m in store.all_memories()] == [
        ("m1", "Tim (@tim)"),
        ("m2", "Luka (@luka)"),
    ]

    now = datetime.now(UTC)
    first = add_task("guild:a", now + timedelta(hours=1))
    second = add_task("guild:b", now + timedelta(hours=2), cron="0 8 * * *")
    store.cancel_task("guild:b", second)
    assert [t.id for t in store.all_tasks()] == [first]  # only active ones by default
    assert [t.id for t in store.all_tasks(active_only=False)] == [first, second]

    store.save_style("guild:a", store.Style(True, "## Ton", now))
    store.save_style("guild:b", store.Style())
    assert store.all_styles() == [
        ("guild:a", store.Style(True, "## Ton", now)),
        ("guild:b", store.Style()),
    ]

    store.block_channel(11, 500)
    store.block_channel(12, 501)
    store.block_channel(11, 502)
    assert store.all_blocked_channels() == [(11, 500), (11, 502), (12, 501)]

    assert store.conversation_count() == 0
    store.link_messages("s1", 101, 102)
    store.link_messages("s2", 103)
    assert store.conversation_count() == 2
