"""Keeps a runaway model answer (stuck repeating itself) from flooding the channel."""

import re
import zlib

WINDOW = 300  # characters checked at a time
STEP = 100
# Normal text, even emoji-heavy, compresses about 1.5-2.5x per window; a loop that repeats
# the same few tokens compresses far more.
LOOP_RATIO = 3.2


def _ratio(text: str) -> float:
    data = text.encode()
    return len(data) / max(1, len(zlib.compress(data, 9)))


def trim_runaway(text: str) -> str:
    """Cut the answer where it starts looping, at the last sentence end before the loop."""
    if len(text) < 2 * WINDOW:
        return text
    for start in range(0, len(text) - WINDOW + 1, STEP):
        if _ratio(text[start : start + WINDOW]) > LOOP_RATIO:
            head = text[:start]
            ends = [m.end() for m in re.finditer(r"[.!?…)](\s|$)|\n", head)]
            cut = ends[-1] if ends and ends[-1] > len(head) // 2 else len(head)
            return head[:cut].rstrip(" —-–") + " …"
    return text


def cap(text: str, max_chars: int) -> str:
    """Shorten to max_chars at a paragraph or sentence break."""
    if len(text) <= max_chars:
        return text
    head = text[:max_chars]
    for pattern in (r"\n\s*\n", r"\n", r"[.!?](\s|$)"):
        ends = [m.end() for m in re.finditer(pattern, head)]
        if ends and ends[-1] > max_chars // 2:
            return head[: ends[-1]].rstrip() + "\n…"
    return head.rsplit(" ", 1)[0] + " …"
