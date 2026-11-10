"""How many tokens the models used, and roughly what they cost.

Every model call is counted when it ends: the agents' calls by a hook (Meter), the bot's own calls
to the API (web search, images, style learning) from the usage in their response. Each call is
added to the day's total in SQLite (store.usage), and kept under its session until the request's
trace takes it, so the dashboard can also show what one request used.

The cost is approximate: the tokens times the model_prices setting, at today's prices. Calls that
are not billed by the token (transcription per minute, OCR per page, web search and image
generation per call) are not in it.
"""

import logging
from datetime import datetime

from mistralai import agents

from . import settings, store
from .context import timezone

log = logging.getLogger("debeloglav.usage")

MAX_PENDING_SESSIONS = 500  # sessions whose usage nobody took are dropped, oldest first

Counts = dict[str, int]  # calls, input_tokens, cached_tokens, output_tokens

# session id -> model -> what its calls used since the session's usage was last taken
_pending: dict[str, dict[str, Counts]] = {}


def add(total: dict[str, Counts], model: str, counts: Counts) -> None:
    into = total.setdefault(model, {})
    for key, value in counts.items():
        into[key] = into.get(key, 0) + value


def record(
    model: str,
    input_tokens: int,
    output_tokens: int,
    cached_tokens: int = 0,
    session_id: str | None = None,
) -> None:
    """Count one model call. Never raises: counting must not break answering."""
    try:
        store.add_usage(
            f"{datetime.now(timezone()):%Y-%m-%d}",
            model,
            input_tokens,
            output_tokens,
            cached_tokens,
        )
    except Exception:
        log.exception("Could not record the usage of %s", model)
    if session_id is None:
        return
    if session_id not in _pending and len(_pending) >= MAX_PENDING_SESSIONS:
        del _pending[next(iter(_pending))]
    add(
        _pending.setdefault(session_id, {}),
        model,
        {
            "calls": 1,
            "input_tokens": input_tokens,
            "cached_tokens": cached_tokens,
            "output_tokens": output_tokens,
        },
    )


def record_response(
    model: str, response: object, session_id: str | None = None
) -> None:
    """Count a chat or conversations API call from the usage in its response."""
    used = getattr(response, "usage", None)
    if used is None:
        return
    record(
        model,
        getattr(used, "prompt_tokens", 0) or 0,
        getattr(used, "completion_tokens", 0) or 0,
        session_id=session_id,
    )


def take(*session_ids: str) -> dict[str, Counts]:
    """What the sessions used since it was last taken, per model."""
    total: dict[str, Counts] = {}
    for session_id in session_ids:
        for model, counts in _pending.pop(session_id, {}).items():
            add(total, model, counts)
    return total


class Meter(agents.Hook):
    """Counts the tokens of every model call of the agent it is a hook of."""

    model: str

    async def post_llm_call(self, ctx, invocation):
        used = invocation.usage
        if used is not None:
            record(
                self.model,
                used.input_tokens,
                used.output_tokens,
                used.cached_input_tokens,
                session_id=ctx.session.id,
            )


def price_of(model: str, prices: dict[str, settings.Price]) -> settings.Price | None:
    """The price of the longest listed name the model starts with."""
    names = [name for name in prices if model.startswith(name)]
    return prices[max(names, key=len)] if names else None


def cost(model: str, counts: Counts, prices: dict[str, settings.Price]) -> float | None:
    """What the tokens cost in US dollars, or None when the model has no price."""
    price = price_of(model, prices)
    if price is None:
        return None
    cached = counts.get("cached_tokens", 0)
    return (
        (counts.get("input_tokens", 0) - cached) * price.input
        + cached * price.cached
        + counts.get("output_tokens", 0) * price.output
    ) / 1_000_000


def prices() -> dict[str, settings.Price]:
    return settings.parse_prices(settings.get("model_prices"))
