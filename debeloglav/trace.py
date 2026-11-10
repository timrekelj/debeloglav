"""What happened during one request, and when: so the dashboard can show where a reply's time went.

A Trace times the bot's own stages (reading the context, the agent's turn, sending the reply) and
records every step of the turn from the Agents SDK's events: each model message, tool call and
tool result, with the milliseconds since the request started. It is stored with the activity row
as JSON, and the dashboard shows it as a timeline and exports it. It also carries the tokens the
request's model calls used (debeloglav.usage).

The steps of a subagent (a research job's researchers) carry an "agent" label, e.g. "researcher 2".
"""

import asyncio
import json
import logging
import time
from contextlib import asynccontextmanager, contextmanager
from typing import Any

from mistralai import agents

from . import usage

log = logging.getLogger("debeloglav.trace")

MAX_STEPS = 400
MAX_DETAIL = 600
SUBAGENT_POLL_SECONDS = 2.0


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
        self.usage: dict[str, usage.Counts] = {}  # model -> the tokens its calls used

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
        agent: str | None = None,
    ) -> None:
        if len(self.steps) >= MAX_STEPS:
            return
        step: dict[str, Any] = {"at_ms": self.ms(), "kind": kind}
        if agent:
            step["agent"] = agent
        if name:
            step["name"] = name
        if detail not in (None, ""):
            step["detail"] = clip(detail)
        if error:
            step["error"] = True
        self.steps.append(step)

    def collect_usage(self, *session_ids: str) -> None:
        """Add the tokens the sessions' model calls used since they were last collected."""
        for model, counts in usage.take(*session_ids).items():
            usage.add(self.usage, model, counts)

    def on_event(self, event: object, agent: str | None = None) -> None:
        """The Agents SDK's event handler for session.run(on_event=...). Never raises."""
        try:
            self._event(event, agent)
        except Exception:
            log.exception("Could not trace the event %r", event)

    def _event(self, event: object, agent: str | None) -> None:
        if isinstance(event, agents.TurnStarted):
            self.step("turn_started", agent=agent)
        elif isinstance(event, agents.EntryAdded):
            entry = event.entry
            if isinstance(entry, agents.MessageEntry) and entry.role == "assistant":
                self.step(
                    "model_writing", agent=agent
                )  # the model's first output token
        elif isinstance(event, agents.EntryCompleted):
            self._entry(event.entry, agent)
        elif isinstance(event, agents.Notice):
            self.step(
                "notice", name=str(event.level), detail=event.message, agent=agent
            )
        elif isinstance(event, agents.TurnEnded):
            state = event.state
            error = getattr(state, "error", None)
            self.step(
                "turn_ended",
                name=type(state).__name__,
                detail=getattr(error, "message", None) if error is not None else None,
                error=error is not None,
                agent=agent,
            )

    def _entry(self, entry: object, agent: str | None) -> None:
        if isinstance(entry, agents.ToolCallEntry):
            self.step("tool_call", name=entry.name, detail=entry.arguments, agent=agent)
        elif isinstance(entry, agents.ToolResultEntry):
            text = "".join(getattr(block, "text", "") for block in entry.content)
            self.step(
                "tool_result",
                name=entry.name,
                detail=text or entry.structured_content,
                error=entry.is_error,
                agent=agent,
            )
        elif isinstance(entry, agents.MessageEntry):
            if entry.role == "assistant":
                self.step(
                    "model_message",
                    detail=f"{len(entry.text)} chars: {entry.text}",
                    agent=agent,
                )
            else:
                self.step(
                    f"{entry.role}_message",
                    detail=f"{len(entry.text)} chars",
                    agent=agent,
                )

    @asynccontextmanager
    async def following_subagents(self, session: object, label: str = "researcher"):
        """While the block runs, record the steps of every subagent the session starts.

        The lead's own events come through session.run(on_event=...); a subagent's do not, so
        this polls session.subagents() and follows each new one's events, labelled "<label> N".
        """
        followers: list[asyncio.Task[None]] = []
        seen: set[str] = set()
        running: dict[str, int] = {}  # subagent label -> turns started but not ended

        async def follow(child: Any, name: str) -> None:
            try:
                async for event in child.iter_events():
                    if isinstance(event, agents.TurnStarted):
                        running[name] = running.get(name, 0) + 1
                    elif isinstance(event, agents.TurnEnded):
                        running[name] = running.get(name, 0) - 1
                    self.on_event(event, agent=name)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.warning("Lost the events of %s", name, exc_info=True)
                self.step(
                    "notice", name="error", detail=f"Lost the events: {e}", agent=name
                )

        async def watch() -> None:
            while True:
                try:
                    children = await session.subagents()
                except asyncio.CancelledError:
                    raise
                except Exception:
                    log.debug("Could not list the subagents", exc_info=True)
                    children = []
                for child in reversed(children):  # subagents() lists the newest first
                    if child.id in seen:
                        continue
                    seen.add(child.id)
                    name = f"{label} {len(seen)}"
                    self.step("subagent_started", name=name, detail=child.id)
                    followers.append(asyncio.create_task(follow(child, name)))
                await asyncio.sleep(SUBAGENT_POLL_SECONDS)

        watcher = asyncio.create_task(watch())
        try:
            yield
        finally:
            for task in (watcher, *followers):
                task.cancel()
            await asyncio.gather(watcher, *followers, return_exceptions=True)
            try:  # a subagent that came and went between two polls still used tokens
                seen.update(child.id for child in await session.subagents())
            except Exception:
                log.debug("Could not list the subagents", exc_info=True)
            self.collect_usage(*seen)
            # Who was still working when the lead stopped, e.g. at a timeout.
            for name, turns in running.items():
                if turns > 0:
                    self.step("subagent_unfinished", name=name, error=True, agent=name)

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
        data = {**self.info, "stages": self.stages, "steps": self.steps}
        if self.usage:
            data["usage"] = self.usage
        return data
