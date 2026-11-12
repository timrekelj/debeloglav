"""Every optional setting of the bot, in one place: its default and its limits.

The settings are changed only in the admin dashboard, never read from the environment. A changed
value is stored in SQLite (store.settings), and get() reads it on every call, so a change takes
effect on the next message without a restart. Resetting it deletes the row, so the setting falls
back to its built-in default. Only the API keys, the token, the dashboard password and what is
needed before the database opens (its path, the dashboard's address) come from .env.
"""

import json
from dataclasses import dataclass
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from . import store

REASONING_EFFORTS = ("none", "low", "medium", "high")
THREAD_ARCHIVE_HOURS = ("1", "24", "72", "168")  # the only durations Discord allows


@dataclass(frozen=True)
class Setting:
    key: str
    group: str
    label: str
    help: str
    default: str
    kind: str = "text"  # "text", "int", "choice", "textarea", "timezone" or "prices"
    choices: tuple[str, ...] = ()
    minimum: int | None = None
    maximum: int | None = None


SETTINGS = [
    # Models
    Setting(
        "model",
        "Models",
        "Main model",
        "Answers messages, runs research and learns the style.",
        "mistral-large-4",
    ),
    Setting(
        "reasoning_effort",
        "Models",
        "Reasoning effort",
        "How long the main model thinks before it answers. Higher is slower. Research always uses "
        "the model's default.",
        "low",
        kind="choice",
        choices=REASONING_EFFORTS,
    ),
    Setting(
        "tool_model",
        "Models",
        "Tool model",
        "A faster model for the hosted web search and image generation.",
        "mistral-medium-latest",
    ),
    Setting(
        "transcribe_model",
        "Models",
        "Transcription model",
        "Transcribes voice messages and the sound of videos.",
        "voxtral-mini-transcribe-3",
    ),
    Setting(
        "ocr_model",
        "Models",
        "OCR model",
        "Reads PDF, Word and PowerPoint files.",
        "mistral-ocr-latest",
    ),
    Setting(
        "model_prices",
        "Models",
        "Model prices",
        "For the approximate cost on the Usage page. One model per line: its name, then the US "
        "dollars per million input and output tokens, and optionally per million cached input "
        "tokens (a tenth of the input price when left out). A model gets the price of the longest "
        "name it starts with, so mistral-large also prices mistral-large-latest.",
        "mistral-large-4 0.70 2.10\n"
        "mistral-large 0.50 1.50\n"
        "mistral-medium 1.50 7.50\n"
        "mistral-small 0.15 0.60",
        kind="prices",
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
    ),
    Setting(
        "timezone",
        "Behaviour",
        "Time zone",
        "Local time for reminders, schedules and the dashboard, e.g. Europe/Ljubljana.",
        "Europe/Ljubljana",
        kind="timezone",
    ),
    Setting(
        "session_idle_hours",
        "Behaviour",
        "Conversation timeout (hours)",
        "A reply after this long starts a new conversation instead of continuing the old one.",
        "6",
        kind="int",
        minimum=1,
        maximum=168,
    ),
    # Threads
    Setting(
        "reply_in_threads",
        "Threads",
        "Reply in threads",
        "When tagged in a channel, open a thread on the message and answer there. The bot "
        "reads every message in its own threads and answers the ones meant for it; in other "
        "threads only tags and replies.",
        "on",
        kind="choice",
        choices=("on", "off"),
    ),
    Setting(
        "thread_archive_hours",
        "Threads",
        "Archive threads after (hours)",
        "A thread the bot opened is hidden after this long without messages.",
        "24",
        kind="choice",
        choices=THREAD_ARCHIVE_HOURS,
    ),
    # Limits
    Setting(
        "max_research_jobs",
        "Limits",
        "Research jobs at once",
        "How many background research jobs can run at the same time.",
        "3",
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
        kind="int",
        minimum=1,
        maximum=1000,
    ),
]

BY_KEY = {setting.key: setting for setting in SETTINGS}
GROUPS = list(dict.fromkeys(setting.group for setting in SETTINGS))


@dataclass(frozen=True)
class Price:
    """What a model costs, in US dollars per million tokens."""

    input: float
    output: float
    cached: float  # input tokens read from the cache


def parse_prices(text: str) -> dict[str, Price]:
    """The model_prices setting as a price per model name; raises ValueError on a bad line."""
    prices: dict[str, Price] = {}
    for line in text.splitlines():
        if not line.strip():
            continue
        model, *numbers = line.split()
        try:
            values = [float(number.lstrip("$").replace(",", ".")) for number in numbers]
        except ValueError:
            values = []
        if len(values) not in (2, 3) or not all(0 <= v < 1_000_000 for v in values):
            raise ValueError(
                f"model_prices: {line.strip()!r} must be a model name, then its input and output "
                "price per million tokens (and optionally the cached input price), "
                "e.g. mistral-large 0.50 1.50."
            )
        prices[model] = Price(
            values[0], values[1], values[2] if len(values) == 3 else values[0] / 10
        )
    return prices


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
    if setting.kind == "prices":
        parse_prices(value)
        return "\n".join(" ".join(line.split()) for line in value.splitlines() if line.strip())
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


def defaults() -> dict[str, str]:
    return {setting.key: setting.default for setting in SETTINGS}


def overrides() -> dict[str, str]:
    """The values changed in the dashboard, only for known keys."""
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
    return setting.default


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
