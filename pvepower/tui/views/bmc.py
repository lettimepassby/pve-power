"""BMC view: identity, LAN configuration, and chassis control.

Editing here writes to the BMC. Two guardrails apply throughout:

  * Changing the LAN parameters of the channel you are *reaching the BMC
    through* can cut you off. When the tool is running against a remote
    BMC, those edits warn before proceeding.
  * Chassis power actions are confirmed in full (typing `yes`), because
    `power off` on the wrong machine is not recoverable from a TUI.
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
    draw_box,
    pad,
    safe_addstr,
    show_message,
    status_attr,
    truncate,
)
from .base import View


def _validate_ip(value: str) -> str:
    try:
        ipaddress.IPv4Address(value.strip())
    except ValueError:
        return "Not a valid IPv4 address"
    return ""


def _validate_netmask(value: str) -> str:
    try:
        addr = ipaddress.IPv4Address(value.strip())
    except ValueError:
        return "Not a valid IPv4 netmask"
    # A netmask must be a run of ones followed by a run of zeros.
    bits = int(addr)
    inverted = bits ^ 0xFFFFFFFF
    if bits and ((inverted + 1) & inverted) != 0:
        return "Not a contiguous netmask (e.g. 255.255.255.0)"
    return ""


def _validate_vlan(value: str) -> str:
    v = value.strip().lower()
    if v in ("off", "disable", "none", ""):
        return ""
    if not v.isdigit() or not 1 <= int(v) <= 4094:
        return "VLAN id must be 1-4094, or 'off'"
    return ""


class BmcView(View):
    title = "BMC"
    hotkeys = [("Enter", "edit"), ("c", "chassis"), ("i", "identify")]

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
                rows.append(("lan", label, lan.get(label, "—"), kind, setter))
        return rows

    def draw(self, win, height: int, width: int) -> None:
        half = width // 2
        self._draw_identity(win, 0, 0, 11, half)
        self._draw_lan(win, 0, half, 11, width - half)
        self._draw_chassis(win, 11, 0, height - 11, width)

    def _draw_identity(self, win, y, x, h, w):
        draw_box(win, y, x, h, w, "BMC Identity",
                 color(CP_TITLE), color(CP_TITLE, bold=True))
        mc = self.data.mc_info or {}
        fru = self.data.fru or {}
        items = [
            ("Manufacturer", mc.get("Manufacturer Name", "?")),
            ("Product", fru.get("Product Name", "?")),
            ("Board", fru.get("Board Product", "?")),
            ("Serial", fru.get("Product Serial", "?")),
            ("IPMI version", mc.get("IPMI Version", "?")),
            ("Firmware", mc.get("Firmware Revision", "?")),
            ("Device ID", mc.get("Device ID", "?")),
            ("Available", mc.get("Device Available", "?")),
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
        draw_box(win, y, x, h, w, f"LAN — channel {channel}",
                 color(CP_TITLE), color(CP_TITLE, bold=True))
        if not lan:
            safe_addstr(win, y + 2, x + 2, "LAN configuration unavailable",
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
            safe_addstr(win, mac_row, x + 2, pad("MAC Address", 15), color(CP_DIM))
            safe_addstr(win, mac_row, x + 18, lan.mac, color(CP_ACCENT))

    def _draw_chassis(self, win, y, x, h, w):
        draw_box(win, y, x, h, w, "Chassis & Power Control",
                 color(CP_TITLE), color(CP_TITLE, bold=True))
        chassis = self.data.chassis_status or {}
        line = y + 1
        left = [
            ("System Power", chassis.get("System Power", "?")),
            ("Power Restore Policy", chassis.get("Power Restore Policy", "?")),
            ("Last Power Event", chassis.get("Last Power Event", "-") or "-"),
            ("Power Overload", chassis.get("Power Overload", "?")),
            ("Main Power Fault", chassis.get("Main Power Fault", "?")),
            ("Cooling/Fan Fault", chassis.get("Cooling/Fan Fault", "?")),
            ("Drive Fault", chassis.get("Drive Fault", "?")),
            ("Chassis Intrusion", chassis.get("Chassis Intrusion", "?")),
        ]
        for label, value in left:
            if line >= y + h - 4:
                break
            safe_addstr(win, line, x + 2, pad(label, 22), color(CP_DIM))
            attr = status_attr(value)
            if label.endswith("Fault") or label == "Power Overload":
                attr = color(CP_CRIT, bold=True) if value.lower() == "true" else color(CP_OK)
            safe_addstr(win, line, x + 25, value, attr)
            line += 1

        policies = self.data.power_policies
        if policies and line < y + h - 2:
            safe_addstr(win, line, x + 2, pad("Supported policies", 22), color(CP_DIM))
            safe_addstr(win, line, x + 25, " ".join(policies), color(CP_ACCENT))

        footer = y + h - 2
        safe_addstr(
            win, footer, x + 2,
            "c: power action    p: restore policy    i: identify LED    "
            "Enter: edit LAN field",
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
            idx = choose(stdscr, "IP address source", options)
            if idx is None:
                return
            value = options[idx]
            args = ("ipsrc", value)
        elif kind == "ip":
            if self.app.ipmi.remote:
                if not confirm(
                    stdscr,
                    "You are managing this BMC over the network. "
                    "Changing its IP will drop your connection. Continue?",
                    danger=True,
                ):
                    return
            value = prompt.ask(f"{label}:", current, validator=_validate_ip)
            if value is None:
                return
            args = ("ipaddr", value.strip())
        elif kind == "netmask":
            value = prompt.ask(f"{label}:", current, validator=_validate_netmask)
            if value is None:
                return
            args = ("netmask", value.strip())
        elif kind == "gateway":
            value = prompt.ask(f"{label}:", current, validator=_validate_ip)
            if value is None:
                return
            args = ("defgw", "ipaddr", value.strip())
        elif kind == "vlan":
            value = prompt.ask(
                f"{label} (1-4094, or 'off'):", current, validator=_validate_vlan
            )
            if value is None:
                return
            v = value.strip().lower()
            args = ("vlan", "id", "off" if v in ("off", "disable", "none", "") else v)
        else:
            value = prompt.ask(f"{label}:", current)
            if value is None:
                return
            args = (setter, value.strip())

        if not confirm(stdscr, f"Set {label} = {value} on channel {channel}?"):
            return
        try:
            out = self.app.ipmi.run("lan", "set", str(channel), *args)
            self.app.flash(f"{label} updated", ok=True)
            if out.strip():
                show_message(stdscr, "ipmitool output", out.strip())
        except IpmiError as exc:
            show_message(stdscr, "Failed to apply LAN setting", str(exc), is_error=True)
        self.data.invalidate("lan")

    def _power_action(self):
        stdscr = self.app.stdscr
        labels = [
            "status  — query power state",
            "on      — power on",
            "soft    — graceful shutdown (ACPI)",
            "off     — hard power off",
            "cycle   — power cycle",
            "reset   — hard reset",
        ]
        actions = ["status", "on", "soft", "off", "cycle", "reset"]
        idx = choose(stdscr, "Chassis power action", labels)
        if idx is None:
            return
        action = actions[idx]
        if action != "status":
            destructive = action in ("off", "cycle", "reset")
            question = f"Really run 'chassis power {action}' on this machine?"
            if not confirm(stdscr, question, danger=destructive):
                return
        try:
            out = self.app.ipmi.chassis_power(action)
            show_message(stdscr, f"chassis power {action}", out.strip() or "(no output)")
            self.app.storage.log_event("chassis_power", action)
        except IpmiError as exc:
            show_message(stdscr, "Power action failed", str(exc), is_error=True)
        self.data.invalidate("chassis")

    def _set_policy(self):
        stdscr = self.app.stdscr
        policies = self.data.power_policies or ["always-off", "always-on", "previous"]
        idx = choose(stdscr, "Power restore policy (after AC loss)", policies)
        if idx is None:
            return
        policy = policies[idx]
        if not confirm(stdscr, f"Set power restore policy to '{policy}'?"):
            return
        try:
            self.app.ipmi.power_policy(policy)
            self.app.flash(f"Restore policy set to {policy}", ok=True)
            self.app.storage.log_event("power_policy", policy)
        except IpmiError as exc:
            show_message(stdscr, "Failed to set policy", str(exc), is_error=True)
        self.data.invalidate("chassis")

    def _identify(self):
        stdscr = self.app.stdscr
        prompt = Prompt(stdscr)
        value = prompt.ask(
            "Blink identify LED for how many seconds? (0 = off):", "15",
            validator=lambda v: "" if v.strip().isdigit() else "Enter a number",
        )
        if value is None:
            return
        try:
            self.app.ipmi.chassis_identify(int(value.strip()))
            self.app.flash(f"Identify LED: {value.strip()}s", ok=True)
        except IpmiError as exc:
            show_message(stdscr, "Identify failed", str(exc), is_error=True)
