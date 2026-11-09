"""Every optional setting of the bot, in one place: its .env variable, its default and its limits.

The environment gives each setting its default; the admin dashboard can override it at runtime.
An override is stored in SQLite (store.settings), and get() reads it on every call, so a change
takes effect on the next message without a restart. Resetting an override deletes the row, so the
setting falls back to the .env value again. API keys, tokens and passwords, the database path and
the dashboard's own address stay in .env only: they are secret, or needed before the database opens.
"""

import json
import logging
import os
from dataclasses import dataclass
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from . import store

log = logging.getLogger("debeloglav.settings")

REASONING_EFFORTS = ("none", "low", "medium", "high")


@dataclass(frozen=True)
class Setting:
    key: str
    group: str
    label: str
    help: str
    default: str
    env: str | None = (
        None  # the .env variable that gives the default; None: dashboard only
    )
    kind: str = "text"  # "text", "int", "choice", "textarea" or "timezone"
    choices: tuple[str, ...] = ()
    minimum: int | None = None
    maximum: int | None = None
    empty: str | None = None  # the value of an env variable that is set but empty


SETTINGS = [
    # Models
    Setting(
        "model",
        "Models",
        "Main model",
        "Answers messages, runs research and learns the style.",
        "mistral-large-4",
        env="MISTRAL_MODEL",
    ),
    Setting(
        "reasoning_effort",
        "Models",
        "Reasoning effort",
        "How long the main model thinks before it answers. Higher is slower. Research always uses "
        "the model's default.",
        "low",
        env="REASONING_EFFORT",
        kind="choice",
        choices=REASONING_EFFORTS,
        empty="none",
    ),
    Setting(
        "tool_model",
        "Models",
        "Tool model",
        "A faster model for the hosted web search and image generation.",
        "mistral-medium-latest",
        env="TOOL_MODEL",
    ),
    Setting(
        "transcribe_model",
        "Models",
        "Transcription model",
        "Transcribes voice messages and the sound of videos.",
        "voxtral-mini-transcribe-3",
        env="TRANSCRIBE_MODEL",
    ),
    Setting(
        "ocr_model",
        "Models",
        "OCR model",
        "Reads PDF, Word and PowerPoint files.",
        "mistral-ocr-latest",
        env="OCR_MODEL",
    ),
    # Behaviour
    Setting(
        "extra_instructions",
        "Behaviour",
        "Extra instructions",
        "Added to the end of the system prompt.",
        "",
        kind="textarea",
    ),
    Setting(
        "context_messages",
        "Behaviour",
        "Context messages",
        "How many recent channel messages the bot reads with a new question.",
        "10",
        env="CONTEXT_MESSAGES",
        kind="int",
        minimum=0,
        maximum=50,
    ),
    Setting(
        "video_frames",
        "Behaviour",
        "Video frames",
        "How many frames of a video the model sees.",
        "6",
        env="VIDEO_FRAMES",
        kind="int",
        minimum=1,
        maximum=20,
    ),
    Setting(
        "thinking_emote",
        "Behaviour",
        "Thinking emote",
        "The server emote, by name, the bot reacts with while it thinks (🤔 when the server has none).",
        "hmmmm",
        env="THINKING_EMOTE",
    ),
    Setting(
        "timezone",
        "Behaviour",
        "Time zone",
        "Local time for reminders, schedules and the dashboard, e.g. Europe/Ljubljana.",
        "Europe/Ljubljana",
        env="TIMEZONE",
        kind="timezone",
    ),
    Setting(
        "session_idle_hours",
        "Behaviour",
        "Conversation timeout (hours)",
        "A reply after this long starts a new conversation instead of continuing the old one.",
        "6",
        env="SESSION_IDLE_HOURS",
        kind="int",
        minimum=1,
        maximum=168,
    ),
    # Limits
    Setting(
        "max_research_jobs",
        "Limits",
        "Research jobs at once",
        "How many background research jobs can run at the same time.",
        "3",
        env="MAX_RESEARCH_JOBS",
        kind="int",
        minimum=1,
        maximum=10,
    ),
    Setting(
        "research_timeout_minutes",
        "Limits",
        "Research timeout (minutes)",
        "A research job that runs longer is stopped.",
        "20",
        env="RESEARCH_TIMEOUT_MINUTES",
        kind="int",
        minimum=1,
        maximum=120,
    ),
    Setting(
        "max_memories",
        "Limits",
        "Memories per server",
        "How many facts the bot remembers per server.",
        "100",
        env="MAX_MEMORIES",
        kind="int",
        minimum=1,
        maximum=1000,
    ),
    Setting(
        "max_tasks",
        "Limits",
        "Tasks per server",
        "How many scheduled tasks a server can have.",
        "25",
        env="MAX_TASKS",
        kind="int",
        minimum=1,
        maximum=200,
    ),
    Setting(
        "max_tasks_per_person",
        "Limits",
        "Tasks per person",
        "How many scheduled tasks one person can have.",
        "10",
        env="MAX_TASKS_PER_PERSON",
        kind="int",
        minimum=1,
        maximum=200,
    ),
    # Style learning
    Setting(
        "learn_every_hours",
        "Style learning",
        "Learn every (hours)",
        "How often the style guide is refreshed.",
        "24",
        env="LEARN_EVERY_HOURS",
        kind="int",
        minimum=1,
        maximum=720,
    ),
    Setting(
        "style_window_days",
        "Style learning",
        "First run reads (days)",
        "How far back a run reads at most.",
        "7",
        env="STYLE_WINDOW_DAYS",
        kind="int",
        minimum=1,
        maximum=60,
    ),
    Setting(
        "style_min_messages",
        "Style learning",
        "Minimum messages",
        "A run with fewer new messages is skipped.",
        "20",
        env="STYLE_MIN_MESSAGES",
        kind="int",
        minimum=1,
        maximum=1000,
    ),
]

BY_KEY = {setting.key: setting for setting in SETTINGS}
GROUPS = list(dict.fromkeys(setting.group for setting in SETTINGS))


def validate(key: str, value: str) -> str:
    """The value, normalized; raises ValueError when it is not valid for the setting."""
    setting = BY_KEY.get(key)
    if setting is None:
        raise ValueError(
            f"Unknown setting {key!r}; the editable settings are: {', '.join(sorted(BY_KEY))}."
        )
    value = value.strip()
    if setting.kind == "textarea":
        return value
    if not value:
        raise ValueError(f"{key} must not be empty.")
    if setting.kind == "choice" and value not in setting.choices:
        raise ValueError(
            f"{key} must be one of: {', '.join(setting.choices)}; got {value!r}."
        )
    if setting.kind == "int":
        try:
            number = int(value)
        except ValueError:
            raise ValueError(f"{key} must be a whole number; got {value!r}.") from None
        if setting.minimum is not None and number < setting.minimum:
            raise ValueError(f"{key} must be at least {setting.minimum}; got {number}.")
        if setting.maximum is not None and number > setting.maximum:
            raise ValueError(f"{key} must be at most {setting.maximum}; got {number}.")
        return str(number)
    if setting.kind == "timezone":
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError):
            raise ValueError(f"{key}: {value!r} is not a known time zone.") from None
    return value


def env_default(setting: Setting) -> str:
    """The setting's default: its .env value when that is set and valid, else the built-in one."""
    raw = os.environ.get(setting.env) if setting.env else None
    if raw is None:
        return setting.default
    if not raw.strip() and setting.empty is not None:
        return setting.empty
    try:
        return validate(setting.key, raw)
    except ValueError as error:
        log.warning("Ignoring %s from the environment: %s", setting.env, error)
        return setting.default


def defaults() -> dict[str, str]:
    return {setting.key: env_default(setting) for setting in SETTINGS}


def overrides() -> dict[str, str]:
    """The values saved from the dashboard, only for known keys."""
    return {key: value for key, value in store.all_settings().items() if key in BY_KEY}


def current() -> dict[str, str]:
    """The effective settings: the dashboard's value when one is saved and valid, else the default."""
    values = defaults()
    for key, value in overrides().items():
        try:
            values[key] = validate(key, value)
        except ValueError:
            pass  # saved under older limits: the default wins
    return values


def get(key: str) -> str:
    setting = BY_KEY[key]
    saved = store.get_setting(key)
    if saved is not None:
        try:
            return validate(key, saved)
        except ValueError:
            pass
    return env_default(setting)


def get_int(key: str) -> int:
    return int(get(key))


def update(key: str, value: str | None) -> dict[str, str]:
    """Validate and save one setting (None resets it to the default); return the effective settings.

    Raises ValueError on an unknown key or an invalid value, and saves nothing then.
    """
    if key not in BY_KEY:
        validate(key, "")  # raises the unknown-key error
    if value is not None:
        value = validate(key, value)
    store.set_setting(key, value)
    return current()


def version() -> str:
    """A fingerprint of the effective settings, so the bot can tell a change without rebuilding."""
    return json.dumps(current(), sort_keys=True)
