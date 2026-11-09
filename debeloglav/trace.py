"""What happened during one request, and when: so the dashboard can show where a reply's time went.

A Trace times the bot's own stages (reading the context, the agent's turn, sending the reply) and
records every step of the turn from the Agents SDK's events: each model message, tool call and
tool result, with the milliseconds since the request started. It is stored with the activity row
as JSON, and the dashboard shows it as a timeline and exports it.
"""

import json
import logging
import time
from contextlib import contextmanager
from typing import Any

from mistralai import agents

log = logging.getLogger("debeloglav.trace")

MAX_STEPS = 200
MAX_DETAIL = 600


def clip(text: object, limit: int = MAX_DETAIL) -> str:
    if not isinstance(text, str):
        try:
            text = json.dumps(text, ensure_ascii=False, default=str)
        except (TypeError, ValueError):
            text = str(text)
    return text if len(text) <= limit else text[: limit - 1] + "…"


class Trace:
    def __init__(self) -> None:
        self.started = time.monotonic()
        self.stages: dict[str, int] = {}  # stage name -> milliseconds spent in it
        self.steps: list[dict[str, Any]] = []
        self.info: dict[str, Any] = {}

    def ms(self) -> int:
        return int((time.monotonic() - self.started) * 1000)

    @contextmanager
    def stage(self, name: str):
        """Time a stage of the request; a stage that runs twice adds up."""
        start = time.monotonic()
        try:
            yield
        finally:
            spent = int((time.monotonic() - start) * 1000)
            self.stages[name] = self.stages.get(name, 0) + spent

    def step(
        self,
        kind: str,
        name: str | None = None,
        detail: object = None,
        error: bool = False,
    ) -> None:
        if len(self.steps) >= MAX_STEPS:
            return
        step: dict[str, Any] = {"at_ms": self.ms(), "kind": kind}
        if name:
            step["name"] = name
        if detail not in (None, ""):
            step["detail"] = clip(detail)
        if error:
            step["error"] = True
        self.steps.append(step)

    def on_event(self, event: object) -> None:
        """The Agents SDK's event handler for session.run(on_event=...). Never raises."""
        try:
            self._event(event)
        except Exception:
            log.exception("Could not trace the event %r", event)

    def _event(self, event: object) -> None:
        if isinstance(event, agents.TurnStarted):
            self.step("turn_started")
        elif isinstance(event, agents.EntryAdded):
            entry = event.entry
            if isinstance(entry, agents.MessageEntry) and entry.role == "assistant":
                self.step("model_writing")  # the model's first output token
        elif isinstance(event, agents.EntryCompleted):
            self._entry(event.entry)
        elif isinstance(event, agents.Notice):
            self.step("notice", name=str(event.level), detail=event.message)
        elif isinstance(event, agents.TurnEnded):
            self.step("turn_ended", name=type(event.state).__name__)

    def _entry(self, entry: object) -> None:
        if isinstance(entry, agents.ToolCallEntry):
            self.step("tool_call", name=entry.name, detail=entry.arguments)
        elif isinstance(entry, agents.ToolResultEntry):
            text = "".join(getattr(block, "text", "") for block in entry.content)
            self.step(
                "tool_result",
                name=entry.name,
                detail=text or entry.structured_content,
                error=entry.is_error,
            )
        elif isinstance(entry, agents.MessageEntry):
            if entry.role == "assistant":
                self.step(
                    "model_message", detail=f"{len(entry.text)} chars: {entry.text}"
                )
            else:
                self.step(f"{entry.role}_message", detail=f"{len(entry.text)} chars")

    def finish(self, state: object) -> None:
        """Note how the turn ended: its state, stop reason and tool calls."""
        self.info["state"] = type(state).__name__
        stop_reason = getattr(state, "stop_reason", None)
        if stop_reason:
            self.info["stop_reason"] = stop_reason
        calls = getattr(state, "tool_calls", None)
        if calls:
            self.info["tool_calls"] = [call.name for call in calls]
        error = getattr(state, "error", None)
        if error is not None:
            self.info["turn_error"] = {
                "message": getattr(error, "message", str(error)),
                "code": getattr(error, "code", None),
            }

    def as_dict(self) -> dict[str, Any]:
        return {**self.info, "stages": self.stages, "steps": self.steps}
