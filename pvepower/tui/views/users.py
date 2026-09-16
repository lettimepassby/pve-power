"""Users view: BMC account management.

The BMC is a second, independent authentication surface on the machine:
it answers on the network whether or not the host OS is running, and a
forgotten default account there is a real exposure. This view therefore
highlights well-known default names as well as letting them be changed.
"""

from __future__ import annotations

import curses

from ...ipmi import IpmiError
from ..widgets import (
    CP_ACCENT,
    CP_CRIT,
    CP_DIM,
    CP_HIGHLIGHT,
    CP_NORMAL,
    CP_OK,
    CP_TITLE,
    CP_WARN,
    Prompt,
    choose,
    color,
    confirm,
    draw_box,
    pad,
    safe_addstr,
    show_message,
    truncate,
)
from .base import View

# Names shipped by vendors that should not survive into production.
DEFAULT_NAMES = {"admin", "administrator", "root", "operator", "user", "guest",
                 "usuario", "ADMIN"}

PRIVILEGES = ["ADMINISTRATOR", "OPERATOR", "USER", "CALLBACK", "NO ACCESS"]


class UsersView(View):
    title = "Users"
    hotkeys = [("p", "password"), ("n", "rename"), ("e/d", "enable/disable")]

    def visible_users(self):
        users = self.data.users or []
        # Empty slots at the end are noise; keep the first free one so a
        # new account can be created, drop the rest.
        out = []
        seen_empty = False
        for u in users:
            if u.empty:
                if seen_empty:
                    continue
                seen_empty = True
            out.append(u)
        return out

    def draw(self, win, height: int, width: int) -> None:
        users = self.visible_users()
        channel = self.app.config.ipmi.lan_channel
        draw_box(win, 0, 0, height - 4, width, f"BMC Users — channel {channel}",
                 color(CP_TITLE), color(CP_TITLE, bold=True))
        safe_addstr(
            win, 1, 2,
            pad(f"{'ID':<4}{'Name':<18}{'Privilege':<16}"
                f"{'IPMI msg':<10}{'Link auth':<11}{'Callin':<8}Notes", width - 4),
            color(CP_DIM, bold=True),
        )

        visible = height - 7
        total = len(users)
        self.scroll = max(0, min(self.scroll, max(0, total - visible)))
        self.cursor = max(0, min(self.cursor, max(0, total - 1)))

        for i in range(min(visible, total)):
            idx = self.scroll + i
            if idx >= total:
                break
            u = users[idx]
            y = 2 + i
            selected = idx == self.cursor
            attr = color(CP_HIGHLIGHT) if selected else color(CP_NORMAL)
            name = u.name if u.name else "(empty)"
            line = (
                f"{u.uid:<4}"
                f"{truncate(name, 17):<18}"
                f"{truncate(u.privilege, 15):<16}"
                f"{'yes' if u.ipmi_msg else 'no':<10}"
                f"{'yes' if u.link_auth else 'no':<11}"
                f"{'yes' if u.callin else 'no':<8}"
            )
            safe_addstr(win, y, 2, line, attr)
            note, note_attr = self._note(u)
            if note:
                safe_addstr(win, y, 2 + len(line),
                            truncate(note, width - len(line) - 4),
                            attr if selected else note_attr)

        self._draw_footer(win, height, width, users)

    def _note(self, user):
        if user.empty:
            return "free slot", color(CP_DIM)
        if user.name.lower() in DEFAULT_NAMES and user.privilege == "ADMINISTRATOR":
            return "default vendor name with admin rights", color(CP_WARN, bold=True)
        if user.privilege == "ADMINISTRATOR" and user.ipmi_msg:
            return "network admin access", color(CP_ACCENT)
        if user.privilege == "NO ACCESS":
            return "disabled", color(CP_DIM)
        return "", color(CP_NORMAL)

    def _draw_footer(self, win, height, width, users):
        y = height - 4
        draw_box(win, y, 0, 4, width, "", color(CP_DIM))
        active = [u for u in users if not u.empty]
        admins = [u for u in active if u.privilege == "ADMINISTRATOR"]
        risky = [
            u for u in active
            if u.name.lower() in DEFAULT_NAMES and u.privilege == "ADMINISTRATOR"
        ]
        summary = f"{len(active)} accounts, {len(admins)} with administrator rights"
        safe_addstr(win, y + 1, 2, summary, color(CP_NORMAL))
        if risky:
            safe_addstr(
                win, y + 1, 2 + len(summary) + 3,
                f"⚠ {len(risky)} default-named admin account(s): "
                + ", ".join(u.name for u in risky),
                color(CP_WARN, bold=True),
            )
        safe_addstr(
            win, y + 2, 2,
            "p: set password   n: rename   v: privilege   e: enable   d: disable",
            color(CP_DIM),
        )

    # ---------------- actions ----------------

    def handle_key(self, key: int) -> bool:
        height, _ = self.app.content_size()
        users = self.visible_users()
        if self.handle_list_key(key, len(users), max(1, height - 7)):
            return True
        if not users or not (0 <= self.cursor < len(users)):
            return False
        user = users[self.cursor]
        if key == ord("p"):
            self._set_password(user)
            return True
        if key == ord("n"):
            self._rename(user)
            return True
        if key == ord("v"):
            self._set_privilege(user)
            return True
        if key == ord("e"):
            self._toggle(user, enable=True)
            return True
        if key == ord("d"):
            self._toggle(user, enable=False)
            return True
        return False

    def _set_password(self, user):
        stdscr = self.app.stdscr
        prompt = Prompt(stdscr)
        label = user.name or f"slot {user.uid}"

        def validate(value: str) -> str:
            # IPMI 2.0 stores a 20-byte password; ipmitool's 16-byte mode is
            # the compatible default and is what most BMCs accept.
            if len(value) < 8:
                return "Use at least 8 characters"
            if len(value) > 20:
                return "IPMI passwords are limited to 20 characters"
            return ""

        first = prompt.ask(f"New password for {label}:", secret=True, validator=validate)
        if first is None:
            return
        second = prompt.ask("Confirm password:", secret=True)
        if second is None:
            return
        if first != second:
            show_message(stdscr, "Password not changed", "The two entries differ.",
                         is_error=True)
            return
        try:
            self.app.ipmi.set_user_password(user.uid, first)
            self.app.flash(f"Password updated for {label}", ok=True)
            self.app.storage.log_event("user_password", f"uid={user.uid}")
        except IpmiError as exc:
            show_message(stdscr, "Failed to set password", str(exc), is_error=True)
        self.data.invalidate("users")

    def _rename(self, user):
        stdscr = self.app.stdscr
        prompt = Prompt(stdscr)

        def validate(value: str) -> str:
            v = value.strip()
            if not v:
                return "Name cannot be empty"
            if len(v) > 16:
                return "IPMI usernames are limited to 16 characters"
            return ""

        name = prompt.ask(f"Name for user {user.uid}:", user.name, validator=validate)
        if name is None:
            return
        if not confirm(stdscr, f"Rename user {user.uid} to '{name.strip()}'?"):
            return
        try:
            self.app.ipmi.set_user_name(user.uid, name.strip())
            self.app.flash(f"User {user.uid} renamed", ok=True)
            self.app.storage.log_event("user_rename", f"uid={user.uid} -> {name.strip()}")
        except IpmiError as exc:
            show_message(stdscr, "Failed to rename user", str(exc), is_error=True)
        self.data.invalidate("users")

    def _set_privilege(self, user):
        stdscr = self.app.stdscr
        idx = choose(stdscr, f"Privilege for {user.name or user.uid}", PRIVILEGES)
        if idx is None:
            return
        level = PRIVILEGES[idx]
        if not confirm(stdscr, f"Set user {user.uid} privilege to {level}?"):
            return
        try:
            self.app.ipmi.set_user_privilege(
                user.uid, self.app.config.ipmi.lan_channel, level
            )
            self.app.flash(f"Privilege set to {level}", ok=True)
            self.app.storage.log_event("user_privilege", f"uid={user.uid} {level}")
        except (IpmiError, ValueError) as exc:
            show_message(stdscr, "Failed to set privilege", str(exc), is_error=True)
        self.data.invalidate("users")

    def _toggle(self, user, enable: bool):
        stdscr = self.app.stdscr
        verb = "Enable" if enable else "Disable"
        label = user.name or f"slot {user.uid}"
        if not confirm(stdscr, f"{verb} BMC user {user.uid} ({label})?"):
            return
        try:
            if enable:
                self.app.ipmi.enable_user(user.uid)
            else:
                self.app.ipmi.disable_user(user.uid)
            self.app.flash(f"User {user.uid} {verb.lower()}d", ok=True)
            self.app.storage.log_event("user_toggle", f"uid={user.uid} enable={enable}")
        except IpmiError as exc:
            show_message(stdscr, f"Failed to {verb.lower()} user", str(exc),
                         is_error=True)
        self.data.invalidate("users")
