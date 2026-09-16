"""View base class and the shared view registry."""

from __future__ import annotations

import curses
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..app import App


class View:
    """One tab in the TUI.

    Subclasses draw into the content window and may declare hotkeys. All
    IPMI access goes through `app.data`, which caches and refreshes on a
    schedule, so a redraw never blocks on the BMC.
    """

    title = "view"
    hotkeys: list[tuple[str, str]] = []

    def __init__(self, app: "App"):
        self.app = app
        self.cursor = 0
        self.scroll = 0

    @property
    def data(self):
        return self.app.data

    def draw(self, win, height: int, width: int) -> None:
        raise NotImplementedError

    def handle_key(self, key: int) -> bool:
        """Return True if the key was consumed."""
        return False

    def move_cursor(self, delta: int, total: int, visible: int) -> None:
        """Shared list navigation with scroll clamping."""
        if total <= 0:
            self.cursor = 0
            self.scroll = 0
            return
        self.cursor = max(0, min(total - 1, self.cursor + delta))
        if self.cursor < self.scroll:
            self.scroll = self.cursor
        elif self.cursor >= self.scroll + visible:
            self.scroll = self.cursor - visible + 1
        self.scroll = max(0, min(self.scroll, max(0, total - visible)))

    def handle_list_key(self, key: int, total: int, visible: int) -> bool:
        if key in (curses.KEY_UP, ord("k")):
            self.move_cursor(-1, total, visible)
            return True
        if key in (curses.KEY_DOWN, ord("j")):
            self.move_cursor(1, total, visible)
            return True
        if key == curses.KEY_NPAGE:
            self.move_cursor(visible, total, visible)
            return True
        if key == curses.KEY_PPAGE:
            self.move_cursor(-visible, total, visible)
            return True
        if key == curses.KEY_HOME or key == ord("g"):
            self.cursor = 0
            self.scroll = 0
            return True
        if key == curses.KEY_END or key == ord("G"):
            self.move_cursor(total, total, visible)
            return True
        return False
