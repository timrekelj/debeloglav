"""Token usage: counting model calls, the per-day totals, prices and cost."""

from datetime import datetime
from types import SimpleNamespace

import pytest
from mistralai import agents
from mistralai.client import models as api_models

from debeloglav import context, settings, store, usage
from debeloglav.trace import Trace


@pytest.fixture(autouse=True)
def clean():
    with store._connect() as db:
        for table in ("usage", "settings", "activity"):
            db.execute(f"DELETE FROM {table}")
    usage._pending.clear()


def today() -> str:
    return f"{datetime.now(context.timezone()):%Y-%m-%d}"


def test_record_adds_up_per_day_and_model():
    usage.record("mistral-large-4", 1000, 200, cached_tokens=400)
    usage.record("mistral-large-4", 500, 100)
    usage.record("mistral-medium-latest", 10, 5)
    assert store.usage() == [
        store.Usage(today(), "mistral-large-4", 2, 1500, 400, 300),
        store.Usage(today(), "mistral-medium-latest", 1, 10, 0, 5),
    ]


def test_take_returns_a_sessions_usage_once():
    usage.record("mistral-large-4", 1000, 200, session_id="lead")
    usage.record("mistral-large-4", 300, 50, session_id="child")
    usage.record("mistral-medium-latest", 10, 5, session_id="child")
    usage.record("mistral-large-4", 7, 7, session_id="other")
    assert usage.take("lead", "child", "unknown") == {
        "mistral-large-4": {
            "calls": 2,
            "input_tokens": 1300,
            "cached_tokens": 0,
            "output_tokens": 250,
        },
        "mistral-medium-latest": {
            "calls": 1,
            "input_tokens": 10,
            "cached_tokens": 0,
            "output_tokens": 5,
        },
    }
    assert usage.take("lead", "child") == {}
    assert list(usage._pending) == ["other"]


def test_untaken_sessions_are_dropped_oldest_first(monkeypatch):
    monkeypatch.setattr(usage, "MAX_PENDING_SESSIONS", 2)
    for session_id in ("a", "b", "c"):
        usage.record("m", 1, 1, session_id=session_id)
    assert list(usage._pending) == ["b", "c"]


def test_a_database_error_does_not_break_the_call(monkeypatch):
    def fail(*args, **kwargs):
        raise RuntimeError("disk full")

    monkeypatch.setattr(store, "add_usage", fail)
    usage.record("m", 1, 2, session_id="s")
    assert usage.take("s")["m"]["output_tokens"] == 2


async def test_meter_counts_the_agents_model_calls():
    meter = usage.Meter(model="mistral-large-4")
    ctx = SimpleNamespace(session=SimpleNamespace(id="s1"))
    used = agents.TokenUsage(
        input_tokens=900, output_tokens=100, total_tokens=1000, cached_input_tokens=600
    )
    assert await meter.post_llm_call(ctx, SimpleNamespace(usage=used)) is None
    await meter.post_llm_call(ctx, SimpleNamespace(usage=None))  # not reported
    assert usage.take("s1") == {
        "mistral-large-4": {
            "calls": 1,
            "input_tokens": 900,
            "cached_tokens": 600,
            "output_tokens": 100,
        }
    }


def test_record_response_reads_the_api_usage():
    response = SimpleNamespace(
        usage=api_models.ConversationUsageInfo(
            prompt_tokens=120, completion_tokens=30, total_tokens=150
        )
    )
    usage.record_response("mistral-medium-latest", response, "s1")
    usage.record_response("mistral-medium-latest", SimpleNamespace(), "s1")  # no usage
    assert usage.take("s1")["mistral-medium-latest"] == {
        "calls": 1,
        "input_tokens": 120,
        "cached_tokens": 0,
        "output_tokens": 30,
    }


def test_trace_carries_the_usage_it_collected():
    trace = Trace()
    assert "usage" not in trace.as_dict()
    usage.record("mistral-large-4", 100, 10, session_id="s1")
    trace.collect_usage("s1")
    usage.record("mistral-large-4", 50, 5, session_id="s1")
    trace.collect_usage("s1")
    assert trace.as_dict()["usage"]["mistral-large-4"] == {
        "calls": 2,
        "input_tokens": 150,
        "cached_tokens": 0,
        "output_tokens": 15,
    }


# --- Prices ----------------------------------------------------------------------------------------


def test_prices_parse_and_match_the_longest_name():
    prices = settings.parse_prices("mistral-large 0.5 1.5\nmistral-large-4 $0,70 2.10 0.2\n\n")
    assert prices["mistral-large"] == settings.Price(0.5, 1.5, 0.05)
    assert usage.price_of("mistral-large-4", prices) == settings.Price(0.7, 2.1, 0.2)
    assert usage.price_of("mistral-large-latest", prices).output == 1.5
    assert usage.price_of("voxtral-mini", prices) is None


@pytest.mark.parametrize(
    "bad", ["mistral-large", "mistral-large 1", "mistral-large a b", "m 1 2 3 4", "m -1 2"]
)
def test_bad_prices_are_rejected(bad):
    with pytest.raises(ValueError, match="model_prices"):
        settings.update("model_prices", bad)
    assert store.get_setting("model_prices") is None


def test_cost_prices_cached_input_apart():
    prices = settings.parse_prices("m 2 10 0.2")
    counts = {"input_tokens": 1_000_000, "cached_tokens": 500_000, "output_tokens": 100_000}
    assert usage.cost("m", counts, prices) == pytest.approx(1 + 0.1 + 1)
    assert usage.cost("unknown", counts, prices) is None


def test_the_default_prices_cover_the_default_models():
    prices = usage.prices()
    for key in ("model", "tool_model"):
        assert usage.price_of(settings.get(key), prices) is not None, key
