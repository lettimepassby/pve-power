"""BMC 视图：硬件身份信息、LAN 配置与机箱控制。

这里的编辑会写进 BMC，因此全程有两条护栏：

  * 修改「你用来连上 BMC 的那个通道」的 LAN 参数，可能把你自己关在门外。
    当工具连的是远端 BMC 时，这类修改会先警告再继续。
  * 机箱电源操作要求完整输入 `yes` 确认，因为对错误的机器执行
    `power off` 是无法从 TUI 里挽回的。
"""

from __future__ import annotations

import curses
import ipaddress
import re

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
    status_attr,
    truncate,
)
from .base import View

# LAN 字段在界面上显示的名称。左侧的键是 ipmitool 自己的字段名——读取
# 回来的值就是以它们为键存放的——所以键必须原样保留，只翻译显示。
LAN_LABELS = {
    "IP Address Source": "IP 地址来源",
    "IP Address": "IP 地址",
    "Subnet Mask": "子网掩码",
    "Default Gateway IP": "默认网关 IP",
    "SNMP Community String": "SNMP 团体名",
}

# 上电恢复策略的中文对照，仅用于显示：发给 ipmitool 的仍是原值。
POLICY_LABELS = {
    "always-on": "始终上电",
    "always-off": "保持断电",
    "previous": "恢复断电前的状态",
}

# 危险电源操作的后果，接在确认问题后面，和英文版一样把话说透。
POWER_CONSEQUENCE = {
    "off": "这会立即切断电源，操作系统来不及正常关机。",
    "cycle": "机器会立即断电再上电，未保存的数据会丢失。",
    "reset": "机器会立即硬重启，未保存的数据会丢失。",
}


def _lan_label(key: str) -> str:
    """界面显示用的 LAN 字段名；键本身仍是 ipmitool 的字段名。"""
    return LAN_LABELS.get(key, key)


def _validate_ip(value: str) -> str:
    try:
        ipaddress.IPv4Address(value.strip())
    except ValueError:
        return "不是有效的 IPv4 地址"
    return ""


def _validate_netmask(value: str) -> str:
    try:
        addr = ipaddress.IPv4Address(value.strip())
    except ValueError:
        return "不是有效的 IPv4 子网掩码"
    # A netmask must be a run of ones followed by a run of zeros.
    bits = int(addr)
    inverted = bits ^ 0xFFFFFFFF
    if bits and ((inverted + 1) & inverted) != 0:
        return "子网掩码必须是连续的一段 1 加一段 0（例如 255.255.255.0）"
    return ""


def _validate_vlan(value: str) -> str:
    v = value.strip().lower()
    if v in ("off", "disable", "none", ""):
        return ""
    if not v.isdigit() or not 1 <= int(v) <= 4094:
        return "VLAN ID 必须是 1-4094，或填 'off'"
    return ""


class BmcView(View):
    title = "BMC"
    hotkeys = [("Enter", "编辑"), ("c", "机箱"), ("i", "定位")]

    # (label, kind, ipmitool `lan set` key)
    LAN_FIELDS = [
        ("IP Address Source", "source", "ipsrc"),
        ("IP Address", "ip", "ipaddr"),
        ("Subnet Mask", "netmask", "netmask"),
        ("Default Gateway IP", "gateway", "defgw ipaddr"),
        ("802.1q VLAN ID", "vlan", "vlan id"),
        ("SNMP Community String", "text", "snmp"),
    ]

    def __init__(self, app):
        super().__init__(app)
        self.cursor = 0

    def _rows(self):
        """Flatten the panels into a single selectable list."""
        rows = []
        lan = self.data.lan_config
        if lan:
            for label, kind, setter in self.LAN_FIELDS:
                # `label` is what ipmitool calls the field, so it is what the
                # value is looked up by; only the text shown is translated.
                rows.append(
                    ("lan", _lan_label(label), lan.get(label, "—"), kind, setter)
                )
        return rows

    def draw(self, win, height: int, width: int) -> None:
        half = width // 2
        self._draw_identity(win, 0, 0, 11, half)
        self._draw_lan(win, 0, half, 11, width - half)
        self._draw_chassis(win, 11, 0, height - 11, width)

    def _draw_identity(self, win, y, x, h, w):
        draw_box(win, y, x, h, w, "BMC 信息",
                 color(CP_TITLE), color(CP_TITLE, bold=True))
        mc = self.data.mc_info or {}
        fru = self.data.fru or {}
        items = [
            ("制造商", mc.get("Manufacturer Name", "?")),
            ("产品型号", fru.get("Product Name", "?")),
            ("主板型号", fru.get("Board Product", "?")),
            ("序列号", fru.get("Product Serial", "?")),
            ("IPMI 版本", mc.get("IPMI Version", "?")),
            ("固件版本", mc.get("Firmware Revision", "?")),
            ("设备 ID", mc.get("Device ID", "?")),
            ("可用状态", mc.get("Device Available", "?")),
        ]
        for i, (label, value) in enumerate(items):
            row = y + 1 + i
            if row >= y + h - 1:
                break
            safe_addstr(win, row, x + 2, pad(label, 15), color(CP_DIM))
            safe_addstr(win, row, x + 18, truncate(value, w - 20), color(CP_NORMAL))

    def _draw_lan(self, win, y, x, h, w):
        lan = self.data.lan_config
        channel = lan.channel if lan else self.app.config.ipmi.lan_channel
        draw_box(win, y, x, h, w, f"LAN — 通道 {channel}",
                 color(CP_TITLE), color(CP_TITLE, bold=True))
        if not lan:
            safe_addstr(win, y + 2, x + 2, "LAN 配置不可用",
                        color(CP_CRIT))
            return
        rows = self._rows()
        for i, (_, label, value, _, _) in enumerate(rows):
            row = y + 1 + i
            if row >= y + h - 2:
                break
            selected = i == self.cursor
            attr = color(CP_HIGHLIGHT) if selected else color(CP_NORMAL)
            safe_addstr(win, row, x + 2, pad(label, 15),
                        attr if selected else color(CP_DIM))
            safe_addstr(win, row, x + 18, pad(truncate(value, w - 20), w - 20), attr)
        mac_row = y + 1 + len(rows)
        if mac_row < y + h - 1:
            safe_addstr(win, mac_row, x + 2, pad("MAC 地址", 15), color(CP_DIM))
            safe_addstr(win, mac_row, x + 18, lan.mac, color(CP_ACCENT))

    def _draw_chassis(self, win, y, x, h, w):
        draw_box(win, y, x, h, w, "机箱与电源控制",
                 color(CP_TITLE), color(CP_TITLE, bold=True))
        chassis = self.data.chassis_status or {}
        line = y + 1
        left = [
            ("系统电源", chassis.get("System Power", "?")),
            ("上电恢复策略", chassis.get("Power Restore Policy", "?")),
            ("上次电源事件", chassis.get("Last Power Event", "-") or "-"),
            ("电源过载", chassis.get("Power Overload", "?")),
            ("主电源故障", chassis.get("Main Power Fault", "?")),
            ("散热风扇故障", chassis.get("Cooling/Fan Fault", "?")),
            ("硬盘故障", chassis.get("Drive Fault", "?")),
            ("机箱入侵", chassis.get("Chassis Intrusion", "?")),
        ]
        for label, value in left:
            if line >= y + h - 4:
                break
            safe_addstr(win, line, x + 2, pad(label, 22), color(CP_DIM))
            attr = status_attr(value)
            # The label is what the Chinese text is matched on, so the fault
            # test moved with it: these are the rows shown in alarm colours.
            if label.endswith("故障") or label == "电源过载":
                attr = color(CP_CRIT, bold=True) if value.lower() == "true" else color(CP_OK)
            safe_addstr(win, line, x + 25, value, attr)
            line += 1

        policies = self.data.power_policies
        if policies and line < y + h - 2:
            safe_addstr(win, line, x + 2, pad("支持的策略", 22), color(CP_DIM))
            safe_addstr(win, line, x + 25, " ".join(policies), color(CP_ACCENT))

        footer = y + h - 2
        safe_addstr(
            win, footer, x + 2,
            "c：电源操作    p：恢复策略    i：定位指示灯    "
            "Enter：编辑 LAN 字段",
            color(CP_DIM),
        )

    # ---------------- actions ----------------

    def handle_key(self, key: int) -> bool:
        rows = self._rows()
        if self.handle_list_key(key, len(rows), max(1, len(rows))):
            return True
        if key in (curses.KEY_ENTER, 10, 13) and rows:
            self._edit_lan_field(rows[self.cursor])
            return True
        if key == ord("c"):
            self._power_action()
            return True
        if key == ord("p"):
            self._set_policy()
            return True
        if key == ord("i"):
            self._identify()
            return True
        return False

    def _edit_lan_field(self, row):
        _, label, current, kind, setter = row
        lan = self.data.lan_config
        if not lan:
            return
        channel = lan.channel
        stdscr = self.app.stdscr
        prompt = Prompt(stdscr)

        if kind == "source":
            options = ["static", "dhcp", "bios", "none"]
            # The value sent to ipmitool is the bare token; the menu shows a
            # Chinese gloss beside it.
            menu = ["static（静态）", "dhcp（DHCP 获取）", "bios（按 BIOS 设定）",
                    "none（不设置）"]
            idx = choose(stdscr, "IP 地址来源", menu)
            if idx is None:
                return
            value = options[idx]
            args = ("ipsrc", value)
        elif kind == "ip":
            if self.app.ipmi.remote:
                if not confirm(
                    stdscr,
                    "你正在通过网络管理这台 BMC，改掉它的 IP 会立即断开当前连接。"
                    "确定要继续吗？",
                    danger=True,
                ):
                    return
            value = prompt.ask(f"{label}：", current, validator=_validate_ip)
            if value is None:
                return
            args = ("ipaddr", value.strip())
        elif kind == "netmask":
            value = prompt.ask(f"{label}：", current, validator=_validate_netmask)
            if value is None:
                return
            args = ("netmask", value.strip())
        elif kind == "gateway":
            value = prompt.ask(f"{label}：", current, validator=_validate_ip)
            if value is None:
                return
            args = ("defgw", "ipaddr", value.strip())
        elif kind == "vlan":
            value = prompt.ask(
                f"{label}（1-4094，或 'off'）：", current, validator=_validate_vlan
            )
            if value is None:
                return
            v = value.strip().lower()
            args = ("vlan", "id", "off" if v in ("off", "disable", "none", "") else v)
        else:
            value = prompt.ask(f"{label}：", current)
            if value is None:
                return
            args = (setter, value.strip())

        if not confirm(stdscr, f"确定要在通道 {channel} 上设置 {label} = {value} 吗？"):
            return
        try:
            out = self.app.ipmi.run("lan", "set", str(channel), *args)
            self.app.flash(f"{label} 已更新", ok=True)
            if out.strip():
                show_message(stdscr, "ipmitool 输出", out.strip())
        except IpmiError as exc:
            show_message(stdscr, "应用 LAN 设置失败", str(exc), is_error=True)
        self.data.invalidate("lan")

    def _power_action(self):
        stdscr = self.app.stdscr
        labels = [
            "status  — 查询电源状态",
            "on      — 开机",
            "soft    — 软关机（ACPI）",
            "off     — 强制断电",
            "cycle   — 循环上电",
            "reset   — 硬重启",
        ]
        actions = ["status", "on", "soft", "off", "cycle", "reset"]
        idx = choose(stdscr, "机箱电源操作", labels)
        if idx is None:
            return
        action = actions[idx]
        if action != "status":
            destructive = action in ("off", "cycle", "reset")
            question = f"确定要对本机执行 'chassis power {action}' 吗？"
            if action in POWER_CONSEQUENCE:
                question += POWER_CONSEQUENCE[action]
            if not confirm(stdscr, question, danger=destructive):
                return
        try:
            out = self.app.ipmi.chassis_power(action)
            show_message(stdscr, f"chassis power {action}",
                         out.strip() or "（无输出）")
            self.app.storage.log_event("chassis_power", action)
        except IpmiError as exc:
            show_message(stdscr, "电源操作失败", str(exc), is_error=True)
        self.data.invalidate("chassis")

    def _set_policy(self):
        stdscr = self.app.stdscr
        policies = self.data.power_policies or ["always-off", "always-on", "previous"]
        # The policy tokens are what ipmitool accepts, so they are shown as
        # they are, with the Chinese gloss beside them.
        labels = [
            f"{p}（{POLICY_LABELS[p]}）" if p in POLICY_LABELS else p
            for p in policies
        ]
        idx = choose(stdscr, "上电恢复策略（交流断电恢复后）", labels)
        if idx is None:
            return
        policy = policies[idx]
        if not confirm(stdscr, f"确定要把上电恢复策略设为 '{policy}' 吗？"):
            return
        try:
            self.app.ipmi.power_policy(policy)
            self.app.flash(f"上电恢复策略已设为 {policy}", ok=True)
            self.app.storage.log_event("power_policy", policy)
        except IpmiError as exc:
            show_message(stdscr, "设置上电恢复策略失败", str(exc), is_error=True)
        self.data.invalidate("chassis")

    def _identify(self):
        stdscr = self.app.stdscr
        prompt = Prompt(stdscr)
        value = prompt.ask(
            "定位指示灯闪烁多少秒？（0 = 关闭）：", "15",
            validator=lambda v: "" if v.strip().isdigit() else "请输入一个数字",
        )
        if value is None:
            return
        try:
            self.app.ipmi.chassis_identify(int(value.strip()))
            self.app.flash(f"定位指示灯：{value.strip()} 秒", ok=True)
        except IpmiError as exc:
            show_message(stdscr, "定位指示灯操作失败", str(exc), is_error=True)
