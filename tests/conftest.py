"""Test settings: set before the bot's modules are imported, as they read them at import."""

import os
import tempfile
from pathlib import Path

os.environ["MISTRAL_API_KEY"] = "test"
os.environ["DB_PATH"] = str(Path(tempfile.mkdtemp()) / "test.db")
os.environ["TIMEZONE"] = "Europe/Ljubljana"
os.environ.pop(
    "AGENT", None
)  # the Mistral workflows config fails to load with someone else's AGENT
