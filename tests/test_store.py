from datetime import UTC, datetime, timedelta

from debeloglav import store


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
        "guild:style1", store.Style(False, ["general", "memes"], "## Ton", learned)
    )
    assert store.style("guild:style1") == store.Style(
        False, ["general", "memes"], "## Ton", learned
    )
    store.save_style("guild:style1", store.Style())
    assert store.style("guild:style1") == store.Style()
