"""Terminal column measurement for East-Asian text.

A CJK glyph occupies two cells, so `len()` is the wrong measure for
anything that has to line up on a terminal. The TUI and the CLI report
tables both need the same answer, so it lives here rather than in the
curses layer.
"""

from __future__ import annotations

import unicodedata


def cwidth(text: str) -> int:
    """Terminal columns occupied by `text`.

    East-Asian Wide and Fullwidth characters take two cells. Combining
    marks take none. Everything else is one.
    """
    total = 0
    for ch in text:
        if unicodedata.combining(ch):
            continue
        total += 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
    return total


def clip(text: str, columns: int) -> str:
    """Longest prefix of `text` that fits in `columns` cells.

    Never splits a double-width glyph across the boundary: if only one
    cell is left, the wide character is dropped rather than half-drawn.
    """
    if columns <= 0:
        return ""
    used = 0
    out = []
    for ch in text:
        w = 0 if unicodedata.combining(ch) else (
            2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
        )
        if used + w > columns:
            break
        out.append(ch)
        used += w
    return "".join(out)


def lpad(text: str, columns: int) -> str:
    """Left-align to `columns` terminal columns."""
    out = clip(text, columns)
    return out + " " * max(0, columns - cwidth(out))


def rpad(text: str, columns: int) -> str:
    """Right-align to `columns` terminal columns."""
    out = clip(text, columns)
    return " " * max(0, columns - cwidth(out)) + out
