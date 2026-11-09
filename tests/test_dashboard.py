"""The admin dashboard: auth, the CSRF guard, every page and every POST action, with a fake bot
and the temp SQLite database from conftest — no Discord, no network."""

import asyncio
import base64
import logging
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from aiohttp.test_utils import TestClient, TestServer

from debeloglav import blocked, dashboard, store
from debeloglav.agent import update_setting

PASSWORD = "geslo-123"


# --- Fakes -----------------------------------------------------------------------------------------


class FakeChannel:
    def __init__(self, id: int, name: str) -> None:
        self.id = id
        self.name = name


class FakeGuild:
    def __init__(self, id: int, name: str, channels=(), categories=()) -> None:
        self.id = id
        self.name = name
        self.text_channels = list(channels)
        self.categories = list(categories)


class FakeBot:
    def __init__(self) -> None:
        self.user = SimpleNamespace(id=999, name="Debeloglav")
        self.latency = 0.05
        self.guilds = [
            FakeGuild(
                1,
                "Testni",
                channels=[FakeChannel(10, "general"), FakeChannel(11, "memes")],
                categories=[FakeChannel(20, "Kategorija")],
            ),
            FakeGuild(2, "Drugi"),
        ]
        self.applied = 0

    def is_ready(self) -> bool:
        return True

    def get_guild(self, guild_id: int):
        return next((g for g in self.guilds if g.id == guild_id), None)

    def get_channel(self, channel_id: int):
        for guild in self.guilds:
            for place in [*guild.text_channels, *guild.categories]:
                if place.id == channel_id:
                    return place
        return None

    def apply_settings(self) -> bool:
        self.applied += 1
        return True


def auth(user: str = "admin", password: str = PASSWORD) -> dict[str, str]:
    encoded = base64.b64encode(f"{user}:{password}".encode()).decode()
    return {"Authorization": f"Basic {encoded}"}


@pytest.fixture(autouse=True)
def clean_tables(monkeypatch):
    """Each test starts with empty tables and an empty blocked-channel cache."""
    with store._connect() as db:
        for table in (
            "activity",
            "style_runs",
            "memories",
            "tasks",
            "styles",
            "blocked_channels",
            "settings",
            "conversations",
        ):
            db.execute(f"DELETE FROM {table}")
    monkeypatch.setattr(blocked, "_db_blocked", set())
    monkeypatch.setattr(blocked, "_db_loaded", True)
    monkeypatch.setattr(blocked, "BLOCKED_CHANNELS", frozenset({777}))


@pytest.fixture
def bot() -> FakeBot:
    return FakeBot()


class FakeClock:
    """A stand-in for time.monotonic, so lockout tests can travel in time instead of sleeping."""

    def __init__(self, start: float = 1_000.0) -> None:
        self.t = start

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
async def client(bot, clock, monkeypatch):
    monkeypatch.delenv(
        "DASHBOARD_TRUSTED_PROXIES", raising=False
    )  # trust no proxy by default
    app = dashboard.make_app(bot, PASSWORD, clock=clock)
    async with TestClient(TestServer(app)) as test_client:
        yield test_client


def origin_of(client: TestClient, host: str = "") -> str:
    server = client.server
    return f"http://{host or server.host}:{server.port}"


def seed() -> int:
    """One activity row (with a prompt and reply), one memory, one task, one style and run."""
    store.log_activity(
        kind="reply",
        scope="guild:1",
        channel_id=10,
        user_name="Tim (@tim)",
        question="Kaj je 2+2?",
        prompt="You are Debeloglav…\nKaj je 2+2?",
        reply="Štiri.",
        ok=True,
        duration_ms=1234,
    )
    store.log_activity(
        kind="research",
        scope="guild:2",
        channel_id=None,
        user_name="Luka (@luka)",
        question="Raziskava",
        prompt=None,
        reply=None,
        ok=False,
        error="RuntimeError: boom",
        duration_ms=99,
    )
    store.add_memory("guild:1", "Tim je admin", "Tim (@tim)", 1)
    store.add_task(
        scope="guild:1",
        channel_id=10,
        user_id=1,
        user_name="Tim (@tim)",
        prompt="Spomni me jutri",
        cron=None,
        next_run=datetime.now(UTC) + timedelta(hours=1),
        watch=False,
        mention=True,
    )
    store.save_style(
        "guild:1",
        store.Style(True, ["general"], "## Ton\nPrijazni.", datetime.now(UTC)),
    )
    store.log_style_run("guild:1", 42, "## Ton\nPrijazni.", None)
    store.log_style_run("guild:1", 3, None, None)
    store.block_channel(1, 11)
    store.link_messages("ses-1", 101)
    update_setting("extra_instructions", "Bodi zabaven.")
    (activity,) = [a for a in store.recent_activity(10) if a.kind == "reply"]
    return activity.id


# --- Store helpers used by the dashboard ------------------------------------------------------------


def test_activity_by_id():
    store.log_activity(kind="task", scope="guild:9", question="q", ok=False, error="e")
    (row,) = store.recent_activity(10)
    assert store.activity(row.id) == row
    assert store.activity(424242) is None


def test_all_memories_by_scope():
    store.add_memory("guild:a", "m1", "Tim (@tim)", 1)
    store.add_memory("guild:b", "m2", "Luka (@luka)", 2)
    assert store.all_memories_by_scope() == [
        ("guild:a", store.Memory(1, "m1", "Tim (@tim)", 1)),
        ("guild:b", store.Memory(2, "m2", "Luka (@luka)", 2)),
    ]


# --- Enabled only with DASHBOARD_PASSWORD -----------------------------------------------------------


async def test_serve_is_disabled_without_a_password(monkeypatch, caplog):
    monkeypatch.delenv("DASHBOARD_PASSWORD", raising=False)
    with caplog.at_level(logging.INFO, logger="debeloglav.dashboard"):
        assert (
            await dashboard.serve(FakeBot()) is None
        )  # returns at once, binds nothing
    assert "disabled" in caplog.text


async def test_app_without_a_password_answers_503(monkeypatch):
    monkeypatch.delenv("DASHBOARD_PASSWORD", raising=False)
    app = dashboard.make_app(FakeBot())  # no password anywhere
    async with TestClient(TestServer(app)) as client:
        response = await client.get("/", headers=auth())
    assert response.status == 503


async def test_serve_runs_until_cancelled(monkeypatch):
    monkeypatch.setenv("DASHBOARD_PASSWORD", PASSWORD)
    monkeypatch.setenv("DASHBOARD_PORT", "0")  # any free port
    task = asyncio.create_task(dashboard.serve(FakeBot()))
    await asyncio.sleep(0.2)
    assert not task.done()  # it is serving
    task.cancel()
    with suppress(asyncio.CancelledError):
        await task


# --- Auth -------------------------------------------------------------------------------------------


async def test_pages_require_auth(client):
    for path in (
        "/",
        "/requests",
        "/prompts",
        "/learning",
        "/memories",
        "/channels",
        "/tasks",
        "/settings",
    ):
        response = await client.get(path)
        assert response.status == 401, path
        assert response.headers["WWW-Authenticate"].startswith("Basic")
    assert (await client.get("/", headers=auth(password="wrong"))).status == 401
    assert (
        await client.get("/", headers=auth(user="k morski"))
    ).status == 200  # any username


# --- Brute-force protection (failed-login rate limiting) ----------------------------------------


async def test_per_ip_lockout_after_five_failed_logins(client):
    for _ in range(dashboard.PER_IP_FAILURE_LIMIT):
        response = await client.get("/", headers=auth(password="wrong"))
        assert response.status == 401
    response = await client.get("/", headers=auth(password="wrong"))
    assert response.status == 429
    assert response.headers["Retry-After"] == str(int(dashboard.LOCKOUT_SECONDS))
    # even the correct password is refused while the IP is locked
    response = await client.get("/", headers=auth())
    assert response.status == 429
    assert response.headers["Retry-After"] == str(int(dashboard.LOCKOUT_SECONDS))


async def test_per_ip_lockout_expires(client, clock):
    for _ in range(dashboard.PER_IP_FAILURE_LIMIT):
        assert (await client.get("/", headers=auth(password="wrong"))).status == 401
    assert (await client.get("/", headers=auth())).status == 429
    clock.advance(dashboard.LOCKOUT_SECONDS + 1)
    assert (await client.get("/", headers=auth())).status == 200
    # the lockout is over, so failures count from zero again
    for _ in range(dashboard.PER_IP_FAILURE_LIMIT):
        assert (await client.get("/", headers=auth(password="wrong"))).status == 401
    assert (await client.get("/", headers=auth())).status == 429


async def test_successful_login_clears_the_ip_failure_count(client):
    for _ in range(dashboard.PER_IP_FAILURE_LIMIT - 1):
        assert (await client.get("/", headers=auth(password="wrong"))).status == 401
    assert (await client.get("/", headers=auth())).status == 200
    # without the reset, these would have locked the IP at the 5th failure already
    for _ in range(dashboard.PER_IP_FAILURE_LIMIT - 1):
        assert (await client.get("/", headers=auth(password="wrong"))).status == 401
    assert (await client.get("/", headers=auth())).status == 200
    for _ in range(dashboard.PER_IP_FAILURE_LIMIT):
        assert (await client.get("/", headers=auth(password="wrong"))).status == 401
    assert (await client.get("/", headers=auth())).status == 429


async def test_requests_without_authorization_header_are_not_counted(client):
    for _ in range(10):  # the browser's first request, before it shows the prompt
        response = await client.get("/")
        assert response.status == 401
        assert response.headers["WWW-Authenticate"].startswith("Basic")
    assert (await client.get("/", headers=auth())).status == 200  # never locked


async def test_malformed_credentials_count_as_failures(client):
    for _ in range(dashboard.PER_IP_FAILURE_LIMIT):
        response = await client.get(
            "/", headers={"Authorization": "Basic !!!not-base64!!!"}
        )
        assert response.status == 401
    response = await client.get("/", headers={"Authorization": "Bearer nope"})
    assert response.status == 429


async def test_global_lockout_only_lets_the_correct_password_through(
    bot, clock, monkeypatch
):
    monkeypatch.setenv("DASHBOARD_TRUSTED_PROXIES", "127.0.0.1")
    app = dashboard.make_app(bot, PASSWORD, clock=clock)
    async with TestClient(TestServer(app)) as client:
        # lock one client IP individually first
        for _ in range(dashboard.PER_IP_FAILURE_LIMIT):
            headers = {**auth(password="wrong"), "X-Forwarded-For": "10.9.9.9"}
            assert (await client.get("/", headers=headers)).status == 401
        # more than GLOBAL_FAILURE_LIMIT failures in total, spread over distinct client IPs
        extra = dashboard.GLOBAL_FAILURE_LIMIT - dashboard.PER_IP_FAILURE_LIMIT + 1
        for i in range(extra):
            headers = {**auth(password="wrong"), "X-Forwarded-For": f"10.1.0.{i}"}
            assert (await client.get("/", headers=headers)).status == 401
        # the global ceiling is now exceeded: a fresh IP with a wrong password is rejected
        fresh = {**auth(password="wrong"), "X-Forwarded-For": "10.2.2.2"}
        response = await client.get("/", headers=fresh)
        assert response.status == 429
        assert response.headers["Retry-After"] == str(int(dashboard.LOCKOUT_SECONDS))
        # no Authorization header at all counts as unauthenticated too
        response = await client.get("/", headers={"X-Forwarded-For": "10.2.2.2"})
        assert response.status == 429
        # the correct password still gets through from an IP that is not individually locked
        response = await client.get(
            "/", headers={**auth(), "X-Forwarded-For": "10.2.2.2"}
        )
        assert response.status == 200
        # ...but not from the individually locked IP
        response = await client.get(
            "/", headers={**auth(), "X-Forwarded-For": "10.9.9.9"}
        )
        assert response.status == 429
        # failures during the global lockout are not counted: after it expires, wrong is 401 again
        clock.advance(dashboard.LOCKOUT_SECONDS + 1)
        response = await client.get("/", headers=fresh)
        assert response.status == 401


async def test_x_forwarded_for_is_ignored_without_a_trusted_proxy(client):
    # the peer 127.0.0.1 is not in DASHBOARD_TRUSTED_PROXIES, so the header is spoofable noise
    for i in range(dashboard.PER_IP_FAILURE_LIMIT):
        headers = {**auth(password="wrong"), "X-Forwarded-For": f"1.2.3.{i}"}
        assert (await client.get("/", headers=headers)).status == 401
    # all failures landed on the peer itself, so even a "fresh" spoofed IP is locked out
    response = await client.get("/", headers={**auth(), "X-Forwarded-For": "9.9.9.9"})
    assert response.status == 429


async def test_x_forwarded_for_is_honored_from_a_trusted_proxy(bot, clock, monkeypatch):
    monkeypatch.setenv("DASHBOARD_TRUSTED_PROXIES", "127.0.0.1")
    app = dashboard.make_app(bot, PASSWORD, clock=clock)
    async with TestClient(TestServer(app)) as client:
        for _ in range(dashboard.PER_IP_FAILURE_LIMIT):
            headers = {**auth(password="wrong"), "X-Forwarded-For": "8.8.8.8"}
            assert (await client.get("/", headers=headers)).status == 401
        # 8.8.8.8 is locked, another client behind the same proxy is not
        response = await client.get(
            "/", headers={**auth(), "X-Forwarded-For": "8.8.4.4"}
        )
        assert response.status == 200
        response = await client.get(
            "/", headers={**auth(), "X-Forwarded-For": "8.8.8.8"}
        )
        assert response.status == 429
        # no X-Forwarded-For from the proxy: the client is the proxy itself
        assert (await client.get("/", headers=auth())).status == 200


async def test_x_forwarded_for_takes_the_rightmost_untrusted_entry(
    bot, clock, monkeypatch
):
    monkeypatch.setenv("DASHBOARD_TRUSTED_PROXIES", "127.0.0.1, 10.0.0.1")
    app = dashboard.make_app(bot, PASSWORD, clock=clock)
    async with TestClient(TestServer(app)) as client:
        chain = "5.6.7.8, 10.0.0.1, 127.0.0.1"
        for _ in range(dashboard.PER_IP_FAILURE_LIMIT):
            headers = {**auth(password="wrong"), "X-Forwarded-For": chain}
            assert (await client.get("/", headers=headers)).status == 401
        # the client IP is 5.6.7.8 (right-most entry that is not a trusted proxy): locked
        response = await client.get("/", headers={**auth(), "X-Forwarded-For": chain})
        assert response.status == 429
        # a different real client behind the same proxies is not locked
        headers = {**auth(), "X-Forwarded-For": "5.6.7.9, 10.0.0.1"}
        assert (await client.get("/", headers=headers)).status == 200
        # a chain of only trusted proxies falls back to the peer itself
        headers = {**auth(), "X-Forwarded-For": "10.0.0.1, 127.0.0.1"}
        assert (await client.get("/", headers=headers)).status == 200


async def test_security_headers_on_200_401_404_303_and_429(client):
    def assert_headers(response) -> None:
        assert response.headers["X-Frame-Options"] == "DENY"
        csp = response.headers["Content-Security-Policy"]
        assert "frame-ancestors 'none'" in csp
        assert "style-src 'unsafe-inline'" in csp  # the inline <style> keeps working
        assert response.headers["Cache-Control"] == "no-store"
        assert response.headers["Referrer-Policy"] == "same-origin"
        assert response.headers["X-Content-Type-Options"] == "nosniff"

    response = await client.get("/", headers=auth())
    assert response.status == 200
    assert_headers(response)
    assert (
        "<style>" in await response.text()
    )  # the page still renders with its inline CSS

    response = await client.get("/")  # 401, no credentials yet
    assert response.status == 401
    assert_headers(response)

    response = await client.get("/requests/987654", headers=auth())  # 404
    assert response.status == 404
    assert_headers(response)

    response = await client.post(  # 303
        "/memories/add",
        data={"scope": "guild:1", "text": "x"},
        headers=auth(),
        allow_redirects=False,
    )
    assert response.status == 303
    assert_headers(response)

    for _ in range(dashboard.PER_IP_FAILURE_LIMIT):
        response = await client.get("/", headers=auth(password="wrong"))
        assert response.status == 401
        assert_headers(response)
    response = await client.get("/", headers=auth(password="wrong"))  # 429
    assert response.status == 429
    assert response.headers["Retry-After"] == str(int(dashboard.LOCKOUT_SECONDS))
    assert_headers(response)


async def test_security_headers_on_503_when_the_dashboard_is_disabled(monkeypatch):
    monkeypatch.delenv("DASHBOARD_PASSWORD", raising=False)
    app = dashboard.make_app(FakeBot())  # no password anywhere
    async with TestClient(TestServer(app)) as client:
        response = await client.get("/", headers=auth())
    assert response.status == 503
    assert response.headers["X-Frame-Options"] == "DENY"
    assert response.headers["Content-Security-Policy"].endswith("form-action 'self'")
    assert response.headers["Cache-Control"] == "no-store"
    assert response.headers["Referrer-Policy"] == "same-origin"
    assert response.headers["X-Content-Type-Options"] == "nosniff"


async def test_failed_logins_are_logged_with_the_ip_never_the_password(client, caplog):
    with caplog.at_level(logging.WARNING, logger="debeloglav.dashboard"):
        for _ in range(dashboard.PER_IP_FAILURE_LIMIT):
            await client.get("/", headers=auth(password="h4x0r-attempt"))
        await client.get(
            "/", headers=auth(password="h4x0r-attempt")
        )  # locked: not counted
    assert (
        caplog.text.count("login failed from 127.0.0.1")
        == dashboard.PER_IP_FAILURE_LIMIT
    )
    assert "h4x0r-attempt" not in caplog.text
    assert caplog.text.count("lockout") == 1  # logged once, when the limit locks
    assert all(record.levelno == logging.WARNING for record in caplog.records)


async def test_limiter_prunes_old_failures_and_each_app_gets_a_fresh_one(
    client, clock, bot
):
    limiter = client.server.app[dashboard.LIMITER]
    for _ in range(3):
        await client.get("/", headers=auth(password="wrong"))
    assert len(limiter._failures["127.0.0.1"]) == 3
    assert len(limiter._global) == 3
    clock.advance(dashboard.FAILURE_WINDOW + 1)
    await client.get("/", headers=auth(password="wrong"))
    assert len(limiter._failures["127.0.0.1"]) == 1  # the old entries are gone
    assert len(limiter._global) == 1
    other = dashboard.make_app(bot, PASSWORD, clock=clock)
    assert other[dashboard.LIMITER] is not limiter


# --- Pages ------------------------------------------------------------------------------------------


async def test_every_page_renders_with_seeded_data(client, bot):
    activity_id = seed()
    pages = {
        "/": [
            "Debeloglav",
            "Testni",
            "Requests, last 24 h",
            "Last style run per guild",
            "boom",
        ],
        "/requests": ["Kaj je 2+2?", "Raziskava", "general"],
        f"/requests/{activity_id}": ["Kaj je 2+2?", "You are Debeloglav", "Štiri."],
        "/prompts": [
            "You are Debeloglav",
            "research lead",
            "style guide",
            "Tim je admin",
            "## Ton",
        ],
        "/learning": [
            "Testni",
            "general",
            "Learned at",
            "History",
            "learned",
            "skipped",
        ],
        "/memories": ["guild:1", "Tim je admin", "Tim (@tim)", "Add a memory"],
        "/channels": ["777", "Unblock", "Block a channel or category", "Kategorija"],
        "/tasks": ["Spomni me jutri", "Tim (@tim)", "Cancel"],
        "/settings": [
            "reasoning_effort",
            "model",
            "extra_instructions",
            "Bodi zabaven.",
            "Reset to default",
        ],
    }
    for path, expected in pages.items():
        response = await client.get(path, headers=auth())
        assert response.status == 200, path
        text = await response.text()
        for needle in expected:
            assert needle in text, f"{needle!r} missing from {path}"


async def test_request_detail_404_for_unknown_id(client):
    response = await client.get("/requests/987654", headers=auth())
    assert response.status == 404


async def test_request_filters(client):
    seed()
    text = await (await client.get("/requests?kind=research", headers=auth())).text()
    assert "Raziskava" in text and "Kaj je 2+2?" not in text
    text = await (await client.get("/requests?errors=1", headers=auth())).text()
    assert "Raziskava" in text and "Kaj je 2+2?" not in text
    text = await (await client.get("/requests?kind=reply", headers=auth())).text()
    assert "Kaj je 2+2?" in text and "Raziskava" not in text


async def test_database_content_is_escaped(client):
    store.add_memory("guild:1", "<script>alert('x')</script>", "dashboard", 0)
    store.log_activity(
        kind="reply", scope="guild:1", question="<b>q</b>", reply="<i>r</i>", ok=True
    )
    text = await (await client.get("/memories", headers=auth())).text()
    assert "&lt;script&gt;alert(&#x27;x&#x27;)&lt;/script&gt;" in text
    assert "<script>alert" not in text
    activity_id = store.recent_activity(1)[0].id
    text = await (await client.get(f"/requests/{activity_id}", headers=auth())).text()
    assert "<b>q</b>" not in text and "&lt;b&gt;q&lt;/b&gt;" in text


# --- CSRF guard -------------------------------------------------------------------------------------


async def test_cross_origin_post_is_rejected(client):
    response = await client.post(
        "/settings",
        data={"key": "model", "value": "x"},
        headers={**auth(), "Origin": "http://evil.example"},
        allow_redirects=False,
    )
    assert response.status == 403
    assert store.get_setting("model") is None  # nothing changed
    response = await client.post(
        "/settings",
        data={"key": "model", "value": "x"},
        headers={**auth(), "Referer": "http://evil.example/page"},
        allow_redirects=False,
    )
    assert response.status == 403


async def test_same_origin_post_is_allowed(client):
    response = await client.post(
        "/settings",
        data={"key": "model", "value": "mistral-medium-latest"},
        headers={**auth(), "Origin": origin_of(client)},
        allow_redirects=False,
    )
    assert response.status == 303
    assert store.get_setting("model") == "mistral-medium-latest"


# --- POST actions -----------------------------------------------------------------------------------


async def test_settings_post_saves_and_applies(client, bot):
    response = await client.post(
        "/settings",
        data={"key": "reasoning_effort", "value": "high"},
        headers=auth(),
        allow_redirects=False,
    )
    assert response.status == 303 and response.headers["Location"] == "/settings"
    assert store.get_setting("reasoning_effort") == "high"
    assert bot.applied == 1  # the bot rebuilt its agent

    # an invalid value redirects back with the error, and saves nothing
    response = await client.post(
        "/settings",
        data={"key": "reasoning_effort", "value": "ultra"},
        headers=auth(),
        allow_redirects=False,
    )
    assert response.status == 303
    assert "error=" in response.headers["Location"]
    assert store.get_setting("reasoning_effort") == "high"
    page = await (await client.get(response.headers["Location"], headers=auth())).text()
    assert "must be one of" in page

    # reset to default deletes the row
    response = await client.post(
        "/settings",
        data={"key": "reasoning_effort", "reset": "1"},
        headers=auth(),
        allow_redirects=False,
    )
    assert response.status == 303
    assert store.get_setting("reasoning_effort") is None


async def test_memory_add_and_delete(client):
    response = await client.post(
        "/memories/add",
        data={"scope": "guild:1", "text": " iz dashboarda"},
        headers=auth(),
        allow_redirects=False,
    )
    assert response.status == 303
    (memory,) = store.memories("guild:1")
    assert (
        memory.text == "iz dashboarda"
        and memory.author == "dashboard"
        and memory.author_id == 0
    )

    response = await client.post(
        "/memories/delete",
        data={"scope": "guild:1", "id": str(memory.id)},
        headers=auth(),
        allow_redirects=False,
    )
    assert response.status == 303
    assert store.memories("guild:1") == []


async def test_block_and_unblock_channel(client):
    response = await client.post(
        "/channels/block",
        data={"guild_id": "1", "channel_id": "11"},
        headers=auth(),
        allow_redirects=False,
    )
    assert response.status == 303
    assert store.blocked_channels(1) == [11]
    assert blocked.channel(SimpleNamespace(id=11, category_id=None))  # cache in sync

    response = await client.post(
        "/channels/unblock",
        data={"guild_id": "1", "channel_id": "11"},
        headers=auth(),
        allow_redirects=False,
    )
    assert response.status == 303
    assert store.blocked_channels(1) == []
    assert not blocked.channel(SimpleNamespace(id=11, category_id=None))


async def test_learning_actions(client):
    store.save_style("guild:1", store.Style(True, None, "## Ton", datetime.now(UTC)))

    async def post(action: str, **extra):
        return await client.post(
            "/learning/action",
            data={"scope": "guild:1", "action": action, **extra},
            headers=auth(),
            allow_redirects=False,
        )

    assert (await post("relearn")).status == 303
    st = store.style("guild:1")
    assert st.guide is None and st.learned_at is None  # relearn: forget and learn again

    assert (await post("enable", channels="general, memes")).status == 303
    st = store.style("guild:1")
    assert st.enabled and st.channels == ["general", "memes"]

    assert (await post("disable")).status == 303
    st = store.style("guild:1")
    assert not st.enabled and st.guide is None  # off also deletes the guide

    assert (await post("all")).status == 303
    st = store.style("guild:1")
    assert st.enabled and st.channels is None  # every public channel


async def test_task_cancel(client):
    store.add_task(
        scope="guild:2",
        channel_id=10,
        user_id=5,
        user_name="Luka (@luka)",
        prompt="Naloga",
        cron="0 8 * * *",
        next_run=datetime.now(UTC) + timedelta(hours=1),
        watch=True,
        mention=False,
    )
    (task,) = store.all_tasks()
    response = await client.post(
        "/tasks/cancel",
        data={"scope": "guild:2", "id": str(task.id)},
        headers=auth(),
        allow_redirects=False,
    )
    assert response.status == 303
    assert store.all_tasks() == []


async def test_post_redirect_stays_on_the_dashboard(client):
    response = await client.post(
        "/memories/add",
        data={"scope": "guild:1", "text": "x", "next": "https://evil.example/"},
        headers=auth(),
        allow_redirects=False,
    )
    assert response.status == 303
    assert response.headers["Location"] == "/"  # not to another site
