"""The admin dashboard: the login form and session cookie, the CSRF guard, every page and every
POST action, with a fake bot and the temp SQLite database from conftest — no Discord, no network."""

import asyncio
import logging
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest
from aiohttp import ClientResponse
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


async def login(
    client: TestClient,
    password: str | None = PASSWORD,
    next: str = "/",
    headers: dict[str, str] | None = None,
) -> ClientResponse:
    """Submit the login form (POST /login). On success the session cookie lands in the client's
    cookie jar, so every later request from this client is authenticated."""
    data = {"next": next}
    if password is not None:
        data["password"] = password
    return await client.post(
        "/login", data=data, headers=headers or {}, allow_redirects=False
    )


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


@pytest.fixture
async def logged_in(client) -> TestClient:
    """A client that has logged in through POST /login (the session cookie is in its jar)."""
    response = await login(client)
    assert response.status == 303
    assert response.headers["Location"] == "/"
    return client


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
        store.Style(True, "## Ton\nPrijazni.", datetime.now(UTC)),
    )
    store.log_style_run("guild:1", 42, "## Ton\nPrijazni.", None)
    store.log_style_run("guild:1", 3, None, None, ["#memes: blocked"])
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
        for path in ("/", "/login"):
            response = await client.get(path)
            assert response.status == 503, path


async def test_serve_runs_until_cancelled(monkeypatch):
    monkeypatch.setenv("DASHBOARD_PASSWORD", PASSWORD)
    monkeypatch.setenv("DASHBOARD_PORT", "0")  # any free port
    task = asyncio.create_task(dashboard.serve(FakeBot()))
    await asyncio.sleep(0.2)
    assert not task.done()  # it is serving
    task.cancel()
    with suppress(asyncio.CancelledError):
        await task


# --- Login form and session cookie -------------------------------------------------------------------


async def test_login_page_renders_without_a_session(client):
    response = await client.get("/login")
    assert response.status == 200
    text = await response.text()
    assert "<style>" in text  # the same layout and CSS as the other pages
    assert "<nav>" not in text  # but no nav (and no logout button) on the login page
    assert "action='/login'" in text
    assert "type='password'" in text
    assert "name='password'" in text
    assert "<button type='submit'>" in text


async def test_unauthenticated_requests_redirect_to_login(client):
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
        response = await client.get(path, allow_redirects=False)
        assert response.status == 303, path
        location = urlsplit(response.headers["Location"])
        assert location.path == "/login", path
        assert parse_qs(location.query)["next"] == [path], path
    # a query string travels along in next
    response = await client.get("/requests?kind=reply", allow_redirects=False)
    location = urlsplit(response.headers["Location"])
    assert parse_qs(location.query)["next"] == ["/requests?kind=reply"]
    # POSTs redirect to the login page the same way, and do nothing
    response = await client.post(
        "/settings", data={"key": "model", "value": "x"}, allow_redirects=False
    )
    assert response.status == 303
    assert urlsplit(response.headers["Location"]).path == "/login"
    assert store.get_setting("model") is None


async def test_wrong_password_is_401_with_an_error_and_counts_as_a_failure(client):
    limiter = client.server.app[dashboard.LIMITER]
    response = await login(client, password="wrong", next="/requests?kind=reply")
    assert response.status == 401
    text = await response.text()
    assert "Wrong password" in text
    assert "value='/requests?kind=reply'" in text  # next is kept in the form
    assert len(limiter._failures["127.0.0.1"]) == 1
    assert len(limiter._global) == 1
    # no session was created, and the login page itself is still reachable
    assert client.server.app[dashboard.SESSIONS] == {}
    assert (await client.get("/login")).status == 200
    assert (await client.get("/", allow_redirects=False)).status == 303


async def test_correct_password_sets_a_session_cookie_and_redirects_to_next(client):
    app = client.server.app
    response = await login(client, next="/requests?kind=reply")
    assert response.status == 303
    assert response.headers["Location"] == "/requests?kind=reply"
    set_cookie = response.headers["Set-Cookie"]
    assert dashboard.SESSION_COOKIE in set_cookie
    assert "HttpOnly" in set_cookie
    assert "SameSite=Strict" in set_cookie
    assert "Path=/" in set_cookie
    (token,) = list(app[dashboard.SESSIONS])
    assert token in set_cookie
    # the cookie jar of the client now authenticates it
    assert (await client.get("/")).status == 200
    assert (await client.get("/requests?kind=reply")).status == 200


async def test_open_redirect_next_is_rejected(client):
    for bad in ("https://evil.example/", "//evil.example", "http://evil.example/x", ""):
        response = await login(client, next=bad)
        assert response.status == 303, bad
        assert response.headers["Location"] == "/", bad
    # the login form sanitizes next in the hidden input too
    response = await client.get("/login?next=https://evil.example/")
    text = await response.text()
    assert "value='https://evil.example/'" not in text
    assert "value='/'" in text
    # a same-site next survives
    text = await (await client.get("/login?next=/settings")).text()
    assert "value='/settings'" in text


async def test_logout_invalidates_the_session(client, logged_in):
    app = client.server.app
    assert (await client.get("/")).status == 200
    (token,) = list(app[dashboard.SESSIONS])
    text = await (await client.get("/")).text()
    assert "action='/logout'" in text  # the nav has a logout button
    response = await client.post("/logout", allow_redirects=False)
    assert response.status == 303
    assert response.headers["Location"] == "/login"
    assert token not in app[dashboard.SESSIONS]
    set_cookie = response.headers["Set-Cookie"]
    assert dashboard.SESSION_COOKIE in set_cookie
    assert "Max-Age=0" in set_cookie  # the cookie is cleared
    # the jar dropped it too: back to the login redirect
    response = await client.get("/", allow_redirects=False)
    assert response.status == 303
    assert urlsplit(response.headers["Location"]).path == "/login"


async def test_expired_session_is_redirected_to_login(logged_in):
    app = logged_in.server.app
    (token,) = list(app[dashboard.SESSIONS])
    app[dashboard.SESSIONS][token] = 0.0  # expired long ago
    response = await logged_in.get("/", allow_redirects=False)
    assert response.status == 303
    assert urlsplit(response.headers["Location"]).path == "/login"
    assert token not in app[dashboard.SESSIONS]  # pruned


# --- Brute-force protection (failed-login rate limiting) ---------------------------------------------


async def test_per_ip_lockout_after_five_failed_logins(client):
    for _ in range(dashboard.PER_IP_FAILURE_LIMIT):
        response = await login(client, password="wrong")
        assert response.status == 401
    response = await login(client, password="wrong")
    assert response.status == 429
    assert response.headers["Retry-After"] == str(int(dashboard.LOCKOUT_SECONDS))
    # even the correct password is refused while the IP is locked
    response = await login(client, password=PASSWORD)
    assert response.status == 429
    assert response.headers["Retry-After"] == str(int(dashboard.LOCKOUT_SECONDS))
    # the login form itself stays reachable, and pages redirect to it as usual
    assert (await client.get("/login")).status == 200
    assert (await client.get("/", allow_redirects=False)).status == 303


async def test_per_ip_lockout_expires(client, clock):
    for _ in range(dashboard.PER_IP_FAILURE_LIMIT):
        assert (await login(client, password="wrong")).status == 401
    assert (await login(client)).status == 429
    clock.advance(dashboard.LOCKOUT_SECONDS + 1)
    assert (await login(client)).status == 303
    # the lockout is over, so failures count from zero again
    for _ in range(dashboard.PER_IP_FAILURE_LIMIT):
        assert (await login(client, password="wrong")).status == 401
    assert (await login(client)).status == 429


async def test_successful_login_clears_the_ip_failure_count(client):
    for _ in range(dashboard.PER_IP_FAILURE_LIMIT - 1):
        assert (await login(client, password="wrong")).status == 401
    assert (await login(client)).status == 303
    # without the reset, these would have locked the IP at the 5th failure already
    for _ in range(dashboard.PER_IP_FAILURE_LIMIT - 1):
        assert (await login(client, password="wrong")).status == 401
    assert (await login(client)).status == 303
    for _ in range(dashboard.PER_IP_FAILURE_LIMIT):
        assert (await login(client, password="wrong")).status == 401
    assert (await login(client)).status == 429


async def test_unauthenticated_page_requests_are_not_counted_as_failures(client):
    for _ in range(10):  # redirects to the login form, no password attempt
        response = await client.get("/", allow_redirects=False)
        assert response.status == 303
    assert (await login(client)).status == 303  # never locked


async def test_a_missing_password_field_counts_as_a_failure(client):
    for _ in range(dashboard.PER_IP_FAILURE_LIMIT):
        response = await login(client, password=None)
        assert response.status == 401
    response = await login(client, password=None)
    assert response.status == 429


async def test_global_lockout_only_lets_the_correct_password_through(
    bot, clock, monkeypatch
):
    monkeypatch.setenv("DASHBOARD_TRUSTED_PROXIES", "127.0.0.1")
    app = dashboard.make_app(bot, PASSWORD, clock=clock)
    async with TestClient(TestServer(app)) as client:
        # lock one client IP individually first
        for _ in range(dashboard.PER_IP_FAILURE_LIMIT):
            headers = {"X-Forwarded-For": "10.9.9.9"}
            assert (
                await login(client, password="wrong", headers=headers)
            ).status == 401
        # more than GLOBAL_FAILURE_LIMIT failures in total, spread over distinct client IPs
        extra = dashboard.GLOBAL_FAILURE_LIMIT - dashboard.PER_IP_FAILURE_LIMIT + 1
        for i in range(extra):
            headers = {"X-Forwarded-For": f"10.1.0.{i}"}
            assert (
                await login(client, password="wrong", headers=headers)
            ).status == 401
        # the global ceiling is now exceeded: a fresh IP with a wrong password is rejected
        fresh = {"X-Forwarded-For": "10.2.2.2"}
        response = await login(client, password="wrong", headers=fresh)
        assert response.status == 429
        assert response.headers["Retry-After"] == str(int(dashboard.LOCKOUT_SECONDS))
        # the correct password still gets through from an IP that is not individually locked
        response = await login(client, headers=fresh)
        assert response.status == 303
        # ...but not from the individually locked IP
        response = await login(client, headers={"X-Forwarded-For": "10.9.9.9"})
        assert response.status == 429
        # failures during the global lockout are not counted: after it expires, wrong is 401 again
        clock.advance(dashboard.LOCKOUT_SECONDS + 1)
        response = await login(client, password="wrong", headers=fresh)
        assert response.status == 401


async def test_a_valid_session_is_not_blocked_by_a_lockout(client):
    assert (await login(client)).status == 303  # the session cookie is in the jar now
    assert (await client.get("/")).status == 200
    # fail the login form enough times to lock this IP out
    for _ in range(dashboard.PER_IP_FAILURE_LIMIT):
        assert (await login(client, password="wrong")).status == 401
    assert (await login(client, password="wrong")).status == 429
    # the valid session still works everywhere, lockouts only guard the login form
    assert (await client.get("/")).status == 200
    response = await client.post(
        "/settings",
        data={"key": "model", "value": "m"},
        allow_redirects=False,
    )
    assert response.status == 303
    assert store.get_setting("model") == "m"


async def test_x_forwarded_for_is_ignored_without_a_trusted_proxy(client):
    # the peer 127.0.0.1 is not in DASHBOARD_TRUSTED_PROXIES, so the header is spoofable noise
    for i in range(dashboard.PER_IP_FAILURE_LIMIT):
        headers = {"X-Forwarded-For": f"1.2.3.{i}"}
        assert (await login(client, password="wrong", headers=headers)).status == 401
    # all failures landed on the peer itself, so even a "fresh" spoofed IP is locked out
    response = await login(client, headers={"X-Forwarded-For": "9.9.9.9"})
    assert response.status == 429


async def test_x_forwarded_for_is_honored_from_a_trusted_proxy(bot, clock, monkeypatch):
    monkeypatch.setenv("DASHBOARD_TRUSTED_PROXIES", "127.0.0.1")
    app = dashboard.make_app(bot, PASSWORD, clock=clock)
    async with TestClient(TestServer(app)) as client:
        for _ in range(dashboard.PER_IP_FAILURE_LIMIT):
            headers = {"X-Forwarded-For": "8.8.8.8"}
            assert (
                await login(client, password="wrong", headers=headers)
            ).status == 401
        # 8.8.8.8 is locked, another client behind the same proxy is not
        response = await login(client, headers={"X-Forwarded-For": "8.8.4.4"})
        assert response.status == 303
        response = await login(client, headers={"X-Forwarded-For": "8.8.8.8"})
        assert response.status == 429
        # no X-Forwarded-For from the proxy: the client is the proxy itself
        assert (await login(client)).status == 303


async def test_x_forwarded_for_takes_the_rightmost_untrusted_entry(
    bot, clock, monkeypatch
):
    monkeypatch.setenv("DASHBOARD_TRUSTED_PROXIES", "127.0.0.1, 10.0.0.1")
    app = dashboard.make_app(bot, PASSWORD, clock=clock)
    async with TestClient(TestServer(app)) as client:
        chain = "5.6.7.8, 10.0.0.1, 127.0.0.1"
        for _ in range(dashboard.PER_IP_FAILURE_LIMIT):
            headers = {"X-Forwarded-For": chain}
            assert (
                await login(client, password="wrong", headers=headers)
            ).status == 401
        # the client IP is 5.6.7.8 (right-most entry that is not a trusted proxy): locked
        response = await login(client, headers={"X-Forwarded-For": chain})
        assert response.status == 429
        # a different real client behind the same proxies is not locked
        headers = {"X-Forwarded-For": "5.6.7.9, 10.0.0.1"}
        assert (await login(client, headers=headers)).status == 303
        # a chain of only trusted proxies falls back to the peer itself
        headers = {"X-Forwarded-For": "10.0.0.1, 127.0.0.1"}
        assert (await login(client, headers=headers)).status == 303


# --- Security headers -----------------------------------------------------------------------------------


async def test_security_headers_on_200_303_401_404_and_429(client):
    def assert_headers(response) -> None:
        assert response.headers["X-Frame-Options"] == "DENY"
        csp = response.headers["Content-Security-Policy"]
        assert "frame-ancestors 'none'" in csp
        assert "style-src 'unsafe-inline'" in csp  # the inline <style> keeps working
        assert response.headers["Cache-Control"] == "no-store"
        assert response.headers["Referrer-Policy"] == "same-origin"
        assert response.headers["X-Content-Type-Options"] == "nosniff"

    response = await client.get(
        "/", allow_redirects=False
    )  # 303 to /login, no session yet
    assert response.status == 303
    assert_headers(response)

    response = await client.get("/login")  # the login page itself
    assert response.status == 200
    assert_headers(response)
    assert "<style>" in await response.text()

    response = await login(client, password="wrong")  # 401
    assert response.status == 401
    assert_headers(response)

    assert (await login(client)).status == 303  # now logged in
    response = await client.get("/")
    assert response.status == 200
    assert_headers(response)
    assert (
        "<style>" in await response.text()
    )  # the page still renders with its inline CSS

    response = await client.get("/requests/987654")  # 404
    assert response.status == 404
    assert_headers(response)

    response = await client.post(  # 303
        "/memories/add",
        data={"scope": "guild:1", "text": "x"},
        allow_redirects=False,
    )
    assert response.status == 303
    assert_headers(response)

    for _ in range(dashboard.PER_IP_FAILURE_LIMIT):
        response = await login(client, password="wrong")
        assert response.status == 401
        assert_headers(response)
    response = await login(client, password="wrong")  # 429
    assert response.status == 429
    assert response.headers["Retry-After"] == str(int(dashboard.LOCKOUT_SECONDS))
    assert_headers(response)


async def test_security_headers_on_503_when_the_dashboard_is_disabled(monkeypatch):
    monkeypatch.delenv("DASHBOARD_PASSWORD", raising=False)
    app = dashboard.make_app(FakeBot())  # no password anywhere
    async with TestClient(TestServer(app)) as client:
        response = await client.get("/")
    assert response.status == 503
    assert response.headers["X-Frame-Options"] == "DENY"
    assert response.headers["Content-Security-Policy"].endswith("form-action 'self'")
    assert response.headers["Cache-Control"] == "no-store"
    assert response.headers["Referrer-Policy"] == "same-origin"
    assert response.headers["X-Content-Type-Options"] == "nosniff"


async def test_failed_logins_are_logged_with_the_ip_never_the_password(client, caplog):
    with caplog.at_level(logging.WARNING, logger="debeloglav.dashboard"):
        for _ in range(dashboard.PER_IP_FAILURE_LIMIT):
            await login(client, password="h4x0r-attempt")
        await login(client, password="h4x0r-attempt")  # locked: not counted
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
        await login(client, password="wrong")
    assert len(limiter._failures["127.0.0.1"]) == 3
    assert len(limiter._global) == 3
    clock.advance(dashboard.FAILURE_WINDOW + 1)
    await login(client, password="wrong")
    assert len(limiter._failures["127.0.0.1"]) == 1  # the old entries are gone
    assert len(limiter._global) == 1
    other = dashboard.make_app(bot, PASSWORD, clock=clock)
    assert other[dashboard.LIMITER] is not limiter
    assert other[dashboard.SESSIONS] == {}


# --- Pages ----------------------------------------------------------------------------------------------


async def test_every_page_renders_with_seeded_data(logged_in):
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
            "blocked ones",
            "Learned at",
            "History",
            "learned",
            "skipped",
            "#memes: blocked",
        ],
        "/memories": ["guild:1", "Tim je admin", "Tim (@tim)", "Add a memory"],
        "/channels": ["memes", "Unblock", "Block a channel or category", "Kategorija"],
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
        response = await logged_in.get(path)
        assert response.status == 200, path
        text = await response.text()
        for needle in expected:
            assert needle in text, f"{needle!r} missing from {path}"


async def test_request_detail_404_for_unknown_id(logged_in):
    response = await logged_in.get("/requests/987654")
    assert response.status == 404


async def test_request_filters(logged_in):
    seed()
    text = await (await logged_in.get("/requests?kind=research")).text()
    assert "Raziskava" in text and "Kaj je 2+2?" not in text
    text = await (await logged_in.get("/requests?errors=1")).text()
    assert "Raziskava" in text and "Kaj je 2+2?" not in text
    text = await (await logged_in.get("/requests?kind=reply")).text()
    assert "Kaj je 2+2?" in text and "Raziskava" not in text


async def test_database_content_is_escaped(logged_in):
    store.add_memory("guild:1", "<script>alert('x')</script>", "dashboard", 0)
    store.log_activity(
        kind="reply", scope="guild:1", question="<b>q</b>", reply="<i>r</i>", ok=True
    )
    text = await (await logged_in.get("/memories")).text()
    assert "&lt;script&gt;alert(&#x27;x&#x27;)&lt;/script&gt;" in text
    assert "<script>alert" not in text
    activity_id = store.recent_activity(1)[0].id
    text = await (await logged_in.get(f"/requests/{activity_id}")).text()
    assert "<b>q</b>" not in text and "&lt;b&gt;q&lt;/b&gt;" in text


# --- CSRF guard -------------------------------------------------------------------------------------


async def test_cross_origin_post_is_rejected(client):
    # the login form itself
    response = await client.post(
        "/login",
        data={"password": PASSWORD, "next": "/"},
        headers={"Origin": "http://evil.example"},
        allow_redirects=False,
    )
    assert response.status == 403
    assert client.server.app[dashboard.SESSIONS] == {}  # no session was created
    assert (await login(client)).status == 303  # now logged in
    response = await client.post(
        "/settings",
        data={"key": "model", "value": "x"},
        headers={"Origin": "http://evil.example"},
        allow_redirects=False,
    )
    assert response.status == 403
    assert store.get_setting("model") is None  # nothing changed
    response = await client.post(
        "/settings",
        data={"key": "model", "value": "x"},
        headers={"Referer": "http://evil.example/page"},
        allow_redirects=False,
    )
    assert response.status == 403
    assert store.get_setting("model") is None


async def test_same_origin_post_is_allowed(logged_in):
    response = await logged_in.post(
        "/settings",
        data={"key": "model", "value": "mistral-medium-latest"},
        headers={"Origin": origin_of(logged_in)},
        allow_redirects=False,
    )
    assert response.status == 303
    assert store.get_setting("model") == "mistral-medium-latest"


# --- POST actions -----------------------------------------------------------------------------------


async def test_settings_post_saves_and_applies(logged_in, bot):
    response = await logged_in.post(
        "/settings",
        data={"key": "reasoning_effort", "value": "high"},
        allow_redirects=False,
    )
    assert response.status == 303 and response.headers["Location"] == "/settings"
    assert store.get_setting("reasoning_effort") == "high"
    assert bot.applied == 1  # the bot rebuilt its agent

    # an invalid value redirects back with the error, and saves nothing
    response = await logged_in.post(
        "/settings",
        data={"key": "reasoning_effort", "value": "ultra"},
        allow_redirects=False,
    )
    assert response.status == 303
    assert "error=" in response.headers["Location"]
    assert store.get_setting("reasoning_effort") == "high"
    page = await (await logged_in.get(response.headers["Location"])).text()
    assert "must be one of" in page

    # reset to default deletes the row
    response = await logged_in.post(
        "/settings",
        data={"key": "reasoning_effort", "reset": "1"},
        allow_redirects=False,
    )
    assert response.status == 303
    assert store.get_setting("reasoning_effort") is None


async def test_memory_add_and_delete(logged_in):
    response = await logged_in.post(
        "/memories/add",
        data={"scope": "guild:1", "text": " iz dashboarda"},
        allow_redirects=False,
    )
    assert response.status == 303
    (memory,) = store.memories("guild:1")
    assert (
        memory.text == "iz dashboarda"
        and memory.author == "dashboard"
        and memory.author_id == 0
    )

    response = await logged_in.post(
        "/memories/delete",
        data={"scope": "guild:1", "id": str(memory.id)},
        allow_redirects=False,
    )
    assert response.status == 303
    assert store.memories("guild:1") == []


async def test_block_and_unblock_channel(logged_in):
    response = await logged_in.post(
        "/channels/block",
        data={"guild_id": "1", "channel_id": "11"},
        allow_redirects=False,
    )
    assert response.status == 303
    assert store.blocked_channels(1) == [11]
    assert blocked.channel(SimpleNamespace(id=11, category_id=None))  # cache in sync

    response = await logged_in.post(
        "/channels/unblock",
        data={"guild_id": "1", "channel_id": "11"},
        allow_redirects=False,
    )
    assert response.status == 303
    assert store.blocked_channels(1) == []
    assert not blocked.channel(SimpleNamespace(id=11, category_id=None))


async def test_learning_actions(logged_in):
    store.save_style("guild:1", store.Style(True, "## Ton", datetime.now(UTC)))

    async def post(action: str, **extra):
        return await logged_in.post(
            "/learning/action",
            data={"scope": "guild:1", "action": action, **extra},
            allow_redirects=False,
        )

    assert (await post("relearn")).status == 303
    st = store.style("guild:1")
    assert st.guide is None and st.learned_at is None  # relearn: forget and learn again

    assert (await post("disable")).status == 303
    st = store.style("guild:1")
    assert not st.enabled and st.guide is None  # off also deletes the guide

    assert (await post("enable")).status == 303
    assert store.style("guild:1").enabled


async def test_task_cancel(logged_in):
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
    response = await logged_in.post(
        "/tasks/cancel",
        data={"scope": "guild:2", "id": str(task.id)},
        allow_redirects=False,
    )
    assert response.status == 303
    assert store.all_tasks() == []


async def test_post_redirect_stays_on_the_dashboard(logged_in):
    response = await logged_in.post(
        "/memories/add",
        data={"scope": "guild:1", "text": "x", "next": "https://evil.example/"},
        allow_redirects=False,
    )
    assert response.status == 303
    assert response.headers["Location"] == "/"  # not to another site


# --- Fonts, traces and exports ----------------------------------------------------------------------


async def test_fonts_are_served_without_a_session_and_cached(client):
    response = await client.get("/static/Geist-Variable.woff2")
    assert response.status == 200
    assert response.content_type == "font/woff2"
    assert response.headers["Cache-Control"] == "public, max-age=604800"
    assert "font-src 'self'" in response.headers["Content-Security-Policy"]
    assert (await response.read())[:4] == b"wOF2"
    for bad in ("/static/..%2Fstore.py", "/static/missing.woff2", "/static/settings.py"):
        assert (await client.get(bad)).status == 404, bad


def traced_request() -> int:
    store.log_activity(
        kind="reply",
        scope="guild:1",
        channel_id=10,
        user_name="Tim (@tim)",
        question="Kako si?",
        reply="Dobro.",
        duration_ms=22_268,
        trace={
            "model": "mistral-large-4",
            "reasoning_effort": "low",
            "stages": {"context": 300, "turn": 21_500, "send": 400},
            "steps": [
                {"at_ms": 400, "kind": "turn_started"},
                {
                    "at_ms": 6_000,
                    "kind": "tool_call",
                    "name": "python.show_style_guide",
                },
                {
                    "at_ms": 6_010,
                    "kind": "tool_result",
                    "name": "python.show_style_guide",
                },
                {"at_ms": 21_800, "kind": "model_message", "detail": "6 chars: Dobro."},
            ],
            "tool_calls": ["python.show_style_guide"],
        },
    )
    return store.recent_activity(1)[0].id


async def test_request_detail_shows_the_timeline(logged_in):
    activity_id = traced_request()
    text = await (await logged_in.get(f"/requests/{activity_id}")).text()
    for needle in (
        "Agent turn",
        "show_style_guide",
        "model thinking",
        "mistral-large-4",
        "22.3 s",
    ):
        assert needle in text, needle
    text = await (await logged_in.get("/requests")).text()
    assert "show_style_guide" in text  # the tools column
    text = await (await logged_in.get("/")).text()
    assert "Where reply time goes" in text and "show_style_guide" in text


async def test_exports(logged_in):
    seed()
    activity_id = traced_request()
    response = await logged_in.get(f"/requests/{activity_id}/json")
    assert response.status == 200
    assert "attachment" in response.headers["Content-Disposition"]
    data = await response.json(content_type=None)
    assert data["id"] == activity_id and data["trace"]["stages"]["turn"] == 21_500
    assert data["guild"] == "Testni" and data["channel"] == "#general"

    data = await (await logged_in.get("/export/requests.json?errors=1")).json(
        content_type=None
    )
    assert [a["question"] for a in data] == ["Raziskava"]

    text = await (await logged_in.get("/export/requests.csv")).text()
    header, *rows = text.strip().splitlines()
    assert header.startswith("id,at,kind") and "turn_ms" in header
    assert len(rows) == 3 and any("show_style_guide" in row for row in rows)

    data = await (await logged_in.get("/export/learning.json")).json(content_type=None)
    assert data["styles"][0]["guide"] == "## Ton\nPrijazni." and len(data["runs"]) == 2

    data = await (await logged_in.get("/export/all.json")).json(content_type=None)
    assert {
        "settings",
        "requests",
        "runs",
        "memories",
        "tasks",
        "blocked_channels",
    } <= set(data)
    assert data["settings"]["overrides"] == {"extra_instructions": "Bodi zabaven."}
    assert (
        "MISTRAL_API_KEY" not in str(data)
        and "test" not in data["settings"]["current"].values()
    )


async def test_exports_need_a_session(client):
    for path in ("/export/all.json", "/export/requests.csv", "/requests/1/json"):
        response = await client.get(path, allow_redirects=False)
        assert response.status == 303, path


async def test_every_setting_is_on_the_settings_page_and_validated(logged_in):
    text = await (await logged_in.get("/settings")).text()
    for key in (
        "tool_model",
        "context_messages",
        "timezone",
        "learn_every_hours",
        "max_tasks",
    ):
        assert f"<code>{key}</code>" in text, key
    response = await logged_in.post(
        "/settings",
        data={"key": "context_messages", "value": "500"},
        allow_redirects=False,
    )
    assert "error=" in response.headers["Location"]
    assert store.get_setting("context_messages") is None
    response = await logged_in.post(
        "/settings",
        data={
            "key": "context_messages",
            "value": "4",
            "next": "/settings#context_messages",
        },
        allow_redirects=False,
    )
    assert response.headers["Location"] == "/settings#context_messages"
    assert store.get_setting("context_messages") == "4"
