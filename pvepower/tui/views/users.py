"""用户视图：BMC 账户管理。

BMC 是机器上第二套独立的认证入口：不管宿主机操作系统是否在运行，它
都在网络上应答，因此遗留在上面的厂商默认账户是实实在在的风险敞口。
所以这个视图既会把常见的默认用户名高亮出来，也允许把它们改掉。
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
    cwidth,
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

# 权限级别的中文对照，仅用于显示：发给 ipmitool 的仍是上面的原值。
PRIVILEGE_LABELS = {
    "ADMINISTRATOR": "管理员",
    "OPERATOR": "操作员",
    "USER": "普通用户",
    "CALLBACK": "回拨",
    "NO ACCESS": "无访问权限",
}

# 表格列宽，按终端列数计（中文字符占两列）。
COL_UID = 4
COL_NAME = 18
COL_PRIV = 16
COL_MSG = 10
COL_LINK = 11
COL_CALLIN = 8


class UsersView(View):
    title = "用户"
    hotkeys = [("p", "密码"), ("n", "重命名"), ("e/d", "启用/禁用")]

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
        draw_box(win, 0, 0, height - 4, width, f"BMC 用户 — 通道 {channel}",
                 color(CP_TITLE), color(CP_TITLE, bold=True))
        # pad() counts terminal columns; an f-string's :<n> counts characters
        # and would misalign every column after a Chinese one.
        safe_addstr(
            win, 1, 2,
            pad(pad("ID", COL_UID) + pad("用户名", COL_NAME)
                + pad("权限", COL_PRIV) + pad("IPMI 消息", COL_MSG)
                + pad("链路认证", COL_LINK) + pad("回拨", COL_CALLIN)
                + "备注", width - 4),
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
            name = u.name if u.name else "（空槽位）"
            line = (
                pad(str(u.uid), COL_UID)
                + pad(truncate(name, COL_NAME - 1), COL_NAME)
                + pad(truncate(u.privilege, COL_PRIV - 1), COL_PRIV)
                + pad("是" if u.ipmi_msg else "否", COL_MSG)
                + pad("是" if u.link_auth else "否", COL_LINK)
                + pad("是" if u.callin else "否", COL_CALLIN)
            )
            safe_addstr(win, y, 2, line, attr)
            note, note_attr = self._note(u)
            if note:
                safe_addstr(win, y, 2 + cwidth(line),
                            truncate(note, width - cwidth(line) - 4),
                            attr if selected else note_attr)

        self._draw_footer(win, height, width, users)

    def _note(self, user):
        if user.empty:
            return "空槽位", color(CP_DIM)
        if user.name.lower() in DEFAULT_NAMES and user.privilege == "ADMINISTRATOR":
            return "厂商默认用户名，且拥有管理员权限", color(CP_WARN, bold=True)
        if user.privilege == "ADMINISTRATOR" and user.ipmi_msg:
            return "可通过网络进行管理员访问", color(CP_ACCENT)
        if user.privilege == "NO ACCESS":
            return "已禁用", color(CP_DIM)
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
        summary = f"共 {len(active)} 个用户，其中 {len(admins)} 个拥有管理员权限"
        safe_addstr(win, y + 1, 2, summary, color(CP_NORMAL))
        if risky:
            safe_addstr(
                win, y + 1, 2 + cwidth(summary) + 3,
                f"⚠ 有 {len(risky)} 个厂商默认名的管理员账户："
                + "、".join(u.name for u in risky),
                color(CP_WARN, bold=True),
            )
        safe_addstr(
            win, y + 2, 2,
            "p：设置密码   n：重命名   v：权限   e：启用   d：禁用",
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
        label = user.name or f"槽位 {user.uid}"

        def validate(value: str) -> str:
            # IPMI 2.0 stores a 20-byte password; ipmitool's 16-byte mode is
            # the compatible default and is what most BMCs accept.
            if len(value) < 8:
                return "密码至少需要 8 个字符"
            if len(value) > 20:
                return "IPMI 密码最长 20 个字符"
            return ""

        first = prompt.ask(f"为 {label} 设置新密码：", secret=True, validator=validate)
        if first is None:
            return
        second = prompt.ask("再次输入密码确认：", secret=True)
        if second is None:
            return
        if first != second:
            show_message(stdscr, "密码未修改", "两次输入的密码不一致。",
                         is_error=True)
            return
        try:
            self.app.ipmi.set_user_password(user.uid, first)
            self.app.flash(f"{label} 的密码已更新", ok=True)
            self.app.storage.log_event("user_password", f"uid={user.uid}")
        except IpmiError as exc:
            show_message(stdscr, "设置密码失败", str(exc), is_error=True)
        self.data.invalidate("users")

    def _rename(self, user):
        stdscr = self.app.stdscr
        prompt = Prompt(stdscr)

        def validate(value: str) -> str:
            v = value.strip()
            if not v:
                return "用户名不能为空"
            if len(v) > 16:
                return "IPMI 用户名最长 16 个字符"
            return ""

        name = prompt.ask(f"用户 {user.uid} 的用户名：", user.name, validator=validate)
        if name is None:
            return
        if not confirm(stdscr, f"确定要把用户 {user.uid} 重命名为 '{name.strip()}' 吗？"):
            return
        try:
            self.app.ipmi.set_user_name(user.uid, name.strip())
            self.app.flash(f"用户 {user.uid} 已重命名", ok=True)
            self.app.storage.log_event("user_rename", f"uid={user.uid} -> {name.strip()}")
        except IpmiError as exc:
            show_message(stdscr, "重命名用户失败", str(exc), is_error=True)
        self.data.invalidate("users")

    def _set_privilege(self, user):
        stdscr = self.app.stdscr
        # The level sent to ipmitool is the bare token; the menu shows a
        # Chinese gloss beside it.
        labels = [f"{p}（{PRIVILEGE_LABELS[p]}）" for p in PRIVILEGES]
        idx = choose(stdscr, f"{user.name or user.uid} 的权限", labels)
        if idx is None:
            return
        level = PRIVILEGES[idx]
        if not confirm(stdscr, f"确定要把用户 {user.uid} 的权限设为 {level} 吗？"):
            return
        try:
            self.app.ipmi.set_user_privilege(
                user.uid, self.app.config.ipmi.lan_channel, level
            )
            self.app.flash(f"权限已设为 {level}", ok=True)
            self.app.storage.log_event("user_privilege", f"uid={user.uid} {level}")
        except (IpmiError, ValueError) as exc:
            show_message(stdscr, "设置权限失败", str(exc), is_error=True)
        self.data.invalidate("users")

    def _toggle(self, user, enable: bool):
        stdscr = self.app.stdscr
        verb = "启用" if enable else "禁用"
        label = user.name or f"槽位 {user.uid}"
        if not confirm(stdscr, f"确定要{verb} BMC 用户 {user.uid}（{label}）吗？"):
            return
        try:
            if enable:
                self.app.ipmi.enable_user(user.uid)
            else:
                self.app.ipmi.disable_user(user.uid)
            self.app.flash(f"用户 {user.uid} 已{verb}", ok=True)
            self.app.storage.log_event("user_toggle", f"uid={user.uid} enable={enable}")
        except IpmiError as exc:
            show_message(stdscr, f"{verb}用户失败", str(exc),
                         is_error=True)
        self.data.invalidate("users")
