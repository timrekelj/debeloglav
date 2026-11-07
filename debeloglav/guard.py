"""Keeps a runaway model answer (stuck repeating itself) from flooding the channel."""

import re
import zlib

WINDOW = 300  # characters checked at a time
STEP = 100
# Normal text, even emoji-heavy, compresses about 1.5-2.5x per window; a loop that repeats
# the same few tokens compresses far more.
LOOP_RATIO = 3.2
# A window only counts as a loop when the same text also repeats verbatim several times
# in a row (a run of 20+ characters, or the same line). Templated lists, where only the
# numbers differ from line to line, compress well but are not loops.
VERBATIM_REPEAT = re.compile(r"([\s\S]{20,}?)\1{2,}")


def _ratio(text: str) -> float:
    data = text.encode()
    return len(data) / max(1, len(zlib.compress(data, 9)))


def _repeats_verbatim(window: str) -> bool:
    """True when the window repeats the same chunk of text verbatim, in a row."""
    if VERBATIM_REPEAT.search(window):
        return True
    lines = window.split("\n")
    return any(
        line and line == lines[i + 1] == lines[i + 2]
        for i, line in enumerate(lines[:-2])
    )


def trim_runaway(text: str) -> str:
    """Cut the answer where it starts looping, at the last sentence end before the loop."""
    if len(text) < 2 * WINDOW:
        return text
    for start in range(0, len(text) - WINDOW + 1, STEP):
        window = text[start : start + WINDOW]
        if _ratio(window) > LOOP_RATIO and _repeats_verbatim(window):
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
