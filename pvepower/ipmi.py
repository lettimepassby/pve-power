"""Thin wrapper around ipmitool.

Every BMC interaction in this project goes through `IpmiTool.run`, so the
transport (local KCS vs. remote lanplus) is decided in exactly one place.
Parsers here only cope with output shapes verified against the target BMC
(Inspur SA5112M4, ipmitool 1.8.19); anything unexpected is surfaced as raw
text rather than guessed at.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from dataclasses import dataclass, field
from typing import Optional


class IpmiError(RuntimeError):
    """An ipmitool invocation failed."""

    def __init__(self, argv: list[str], returncode: int, stderr: str):
        self.argv = argv
        self.returncode = returncode
        self.stderr = stderr.strip()
        super().__init__(
            f"ipmitool {' '.join(argv)} exited {returncode}: {self.stderr or '(no stderr)'}"
        )


@dataclass
class Sensor:
    name: str
    value: Optional[float]
    unit: str
    status: str
    lower_nr: Optional[float] = None
    lower_crit: Optional[float] = None
    lower_nc: Optional[float] = None
    upper_nc: Optional[float] = None
    upper_crit: Optional[float] = None
    upper_nr: Optional[float] = None

    @property
    def ok(self) -> bool:
        return self.status == "ok"

    @property
    def readable(self) -> bool:
        return self.value is not None

    @property
    def kind(self) -> str:
        """Coarse sensor family, used by the TUI for grouping and filtering."""
        u = self.unit.lower()
        if "degree" in u:
            return "temperature"
        if "volt" in u:
            return "voltage"
        if "rpm" in u:
            return "fan"
        if "watt" in u:
            return "power"
        if "amp" in u:
            return "current"
        return "discrete"

    def headroom(self) -> Optional[float]:
        """Degrees/volts/etc. left before the upper critical threshold."""
        if self.value is None or self.upper_crit is None:
            return None
        return self.upper_crit - self.value


@dataclass
class PowerReading:
    instantaneous: Optional[int] = None
    minimum: Optional[int] = None
    maximum: Optional[int] = None
    average: Optional[int] = None
    timestamp: str = ""
    sampling_period: str = ""
    state: str = ""

    @property
    def valid(self) -> bool:
        return self.instantaneous is not None


@dataclass
class LanConfig:
    channel: int
    fields: dict[str, str] = field(default_factory=dict)

    def get(self, key: str, default: str = "") -> str:
        return self.fields.get(key, default)

    @property
    def ip(self) -> str:
        return self.get("IP Address")

    @property
    def netmask(self) -> str:
        return self.get("Subnet Mask")

    @property
    def gateway(self) -> str:
        return self.get("Default Gateway IP")

    @property
    def mac(self) -> str:
        return self.get("MAC Address")

    @property
    def source(self) -> str:
        return self.get("IP Address Source")

    @property
    def vlan(self) -> str:
        return self.get("802.1q VLAN ID")


@dataclass
class IpmiUser:
    uid: int
    name: str
    callin: bool
    link_auth: bool
    ipmi_msg: bool
    privilege: str

    @property
    def empty(self) -> bool:
        return not self.name.strip()


@dataclass
class SelEntry:
    record_id: str
    date: str
    time: str
    sensor: str
    event: str
    direction: str

    @property
    def when(self) -> str:
        return f"{self.date} {self.time}".strip()

    @property
    def severity(self) -> str:
        """Best-effort triage so the TUI can colour the log."""
        blob = f"{self.sensor} {self.event}".lower()
        if any(k in blob for k in ("non-recoverable", "failure", "fault", "uncorrectable")):
            return "critical"
        if any(k in blob for k in ("critical", "ac lost", "power off", "overload", "error")):
            return "warning"
        return "info"


def _to_float(token: str) -> Optional[float]:
    token = token.strip()
    if not token or token.lower() in {"na", "n/a", "no reading", "disabled", "unspecified"}:
        return None
    try:
        return float(token)
    except ValueError:
        return None


class IpmiTool:
    """Runs ipmitool locally, or against a remote BMC over lanplus."""

    def __init__(
        self,
        binary: str = "ipmitool",
        host: str = "",
        user: str = "",
        password: str = "",
        interface: str = "lanplus",
        timeout: int = 20,
    ):
        self.binary = binary
        self.host = host
        self.user = user
        self.password = password
        self.interface = interface
        self.timeout = timeout

    @property
    def remote(self) -> bool:
        return bool(self.host)

    def available(self) -> bool:
        return shutil.which(self.binary) is not None

    def _base_argv(self) -> list[str]:
        argv = [self.binary]
        if self.remote:
            argv += ["-I", self.interface, "-H", self.host]
            if self.user:
                argv += ["-U", self.user]
            if self.password:
                argv += ["-P", self.password]
        return argv

    def run(self, *args: str, check: bool = True) -> str:
        argv = self._base_argv() + list(args)
        try:
            proc = subprocess.run(
                argv,
                capture_output=True,
                text=True,
                timeout=self.timeout,
            )
        except FileNotFoundError as exc:
            raise IpmiError(list(args), 127, f"{self.binary} not found") from exc
        except subprocess.TimeoutExpired as exc:
            raise IpmiError(list(args), 124, f"timed out after {self.timeout}s") from exc
        if check and proc.returncode != 0:
            raise IpmiError(list(args), proc.returncode, proc.stderr)
        return proc.stdout

    def try_run(self, *args: str) -> tuple[bool, str]:
        """Run without raising; returns (ok, output-or-error)."""
        try:
            return True, self.run(*args)
        except IpmiError as exc:
            return False, str(exc)

    # ---------------- power ----------------

    def power_reading(self) -> PowerReading:
        """DCMI instantaneous/min/max/avg power, in watts."""
        out = self.run("dcmi", "power", "reading")
        reading = PowerReading()
        patterns = {
            "instantaneous": r"Instantaneous power reading:\s+(\d+)",
            "minimum": r"Minimum during sampling period:\s+(\d+)",
            "maximum": r"Maximum during sampling period:\s+(\d+)",
            "average": r"Average power reading over sample period:\s+(\d+)",
        }
        for attr, pat in patterns.items():
            m = re.search(pat, out)
            if m:
                setattr(reading, attr, int(m.group(1)))
        m = re.search(r"IPMI timestamp:\s+(.+?)\s{2,}", out)
        if m:
            reading.timestamp = m.group(1).strip()
        m = re.search(r"Sampling period:\s+(\S+\s*\w*)", out)
        if m:
            reading.sampling_period = m.group(1).strip()
        m = re.search(r"Power reading state is:\s+(\w+)", out)
        if m:
            reading.state = m.group(1).strip()
        return reading

    def instantaneous_watts(self) -> Optional[int]:
        try:
            return self.power_reading().instantaneous
        except IpmiError:
            return None

    # ---------------- sensors ----------------

    def sensors(self) -> list[Sensor]:
        """Full threshold table from `ipmitool sensor`."""
        out = self.run("sensor")
        sensors: list[Sensor] = []
        for line in out.splitlines():
            if "|" not in line:
                continue
            cols = [c.strip() for c in line.split("|")]
            if len(cols) < 4:
                continue
            sensors.append(
                Sensor(
                    name=cols[0],
                    value=_to_float(cols[1]),
                    unit=cols[2],
                    status=cols[3].lower(),
                    lower_nr=_to_float(cols[4]) if len(cols) > 4 else None,
                    lower_crit=_to_float(cols[5]) if len(cols) > 5 else None,
                    lower_nc=_to_float(cols[6]) if len(cols) > 6 else None,
                    upper_nc=_to_float(cols[7]) if len(cols) > 7 else None,
                    upper_crit=_to_float(cols[8]) if len(cols) > 8 else None,
                    upper_nr=_to_float(cols[9]) if len(cols) > 9 else None,
                )
            )
        return sensors

    # ---------------- inventory ----------------

    def mc_info(self) -> dict[str, str]:
        return self._parse_colon_block(self.run("mc", "info"))

    def fru(self) -> dict[str, str]:
        return self._parse_colon_block(self.run("fru", "print"))

    def chassis_status(self) -> dict[str, str]:
        return self._parse_colon_block(self.run("chassis", "status"))

    def bmc_guid(self) -> dict[str, str]:
        ok, out = self.try_run("mc", "guid")
        return self._parse_colon_block(out) if ok else {}

    @staticmethod
    def _parse_colon_block(text: str) -> dict[str, str]:
        """Parse ipmitool's `Key : Value` blocks, keeping first-wins order."""
        data: dict[str, str] = {}
        for line in text.splitlines():
            if ":" not in line:
                continue
            key, _, value = line.partition(":")
            key = key.strip()
            value = value.strip()
            if not key or key in data:
                continue
            data[key] = value
        return data

    # ---------------- network ----------------

    def lan_config(self, channel: int = 1) -> LanConfig:
        out = self.run("lan", "print", str(channel))
        cfg = LanConfig(channel=channel)
        current_key = ""
        for line in out.splitlines():
            if ":" not in line:
                continue
            key, _, value = line.partition(":")
            key = key.strip()
            value = value.strip()
            if key:
                current_key = key
                if key not in cfg.fields:
                    cfg.fields[key] = value
            elif current_key and value:
                # Continuation line (e.g. multi-line Auth Type Enable).
                cfg.fields[current_key] += f"; {value}"
        return cfg

    def discover_lan_channels(self) -> list[int]:
        """Channels that actually answer `lan print`."""
        found = []
        for ch in range(1, 9):
            ok, out = self.try_run("lan", "print", str(ch))
            if ok and "IP Address" in out:
                found.append(ch)
        return found

    def set_lan(self, channel: int, key: str, *values: str) -> str:
        return self.run("lan", "set", str(channel), key, *values)

    # ---------------- users ----------------

    def users(self, channel: int = 1) -> list[IpmiUser]:
        out = self.run("user", "list", str(channel))
        users: list[IpmiUser] = []
        for line in out.splitlines():
            parts = line.split()
            if not parts or not parts[0].isdigit():
                continue
            uid = int(parts[0])
            # An empty-name row collapses to: id, callin, link, msg, priv...
            bools = [p for p in parts[1:] if p in ("true", "false")]
            if len(parts) >= 5 and parts[1] not in ("true", "false"):
                name = parts[1]
            else:
                name = ""
            priv = " ".join(parts[len(parts) - 1 :]) if parts else ""
            for token in ("ADMINISTRATOR", "OPERATOR", "USER", "CALLBACK", "NO ACCESS"):
                if token in line:
                    priv = token
                    break
            users.append(
                IpmiUser(
                    uid=uid,
                    name=name,
                    callin=bools[0] == "true" if len(bools) > 0 else False,
                    link_auth=bools[1] == "true" if len(bools) > 1 else False,
                    ipmi_msg=bools[2] == "true" if len(bools) > 2 else False,
                    privilege=priv,
                )
            )
        return users

    def set_user_password(self, uid: int, password: str) -> str:
        return self.run("user", "set", "password", str(uid), password)

    def set_user_name(self, uid: int, name: str) -> str:
        return self.run("user", "set", "name", str(uid), name)

    def enable_user(self, uid: int) -> str:
        return self.run("user", "enable", str(uid))

    def disable_user(self, uid: int) -> str:
        return self.run("user", "disable", str(uid))

    def set_user_privilege(self, uid: int, channel: int, level: str) -> str:
        codes = {
            "CALLBACK": "1",
            "USER": "2",
            "OPERATOR": "3",
            "ADMINISTRATOR": "4",
            "NO ACCESS": "15",
        }
        code = codes.get(level.upper())
        if code is None:
            raise ValueError(f"unknown privilege level: {level}")
        return self.run("channel", "setaccess", str(channel), str(uid), f"privilege={code}")

    # ---------------- SEL ----------------

    def sel_info(self) -> dict[str, str]:
        return self._parse_colon_block(self.run("sel", "info"))

    def sel_entries(self, limit: int = 200) -> list[SelEntry]:
        """Most recent SEL records, newest first."""
        ok, out = self.try_run("sel", "list", "last", str(limit))
        if not ok:
            ok, out = self.try_run("sel", "list")
            if not ok:
                return []
        entries: list[SelEntry] = []
        for line in out.splitlines():
            cols = [c.strip() for c in line.split("|")]
            if len(cols) < 5:
                continue
            entries.append(
                SelEntry(
                    record_id=cols[0],
                    date=cols[1],
                    time=cols[2],
                    sensor=cols[3],
                    event=cols[4],
                    direction=cols[5] if len(cols) > 5 else "",
                )
            )
        entries.reverse()
        return entries

    def sel_clear(self) -> str:
        return self.run("sel", "clear")

    # ---------------- chassis control ----------------

    def chassis_power(self, action: str) -> str:
        allowed = {"on", "off", "cycle", "reset", "soft", "diag", "status"}
        if action not in allowed:
            raise ValueError(f"unsupported power action: {action}")
        return self.run("chassis", "power", action)

    def chassis_identify(self, seconds: int = 15) -> str:
        return self.run("chassis", "identify", str(seconds))

    def power_policy(self, policy: str) -> str:
        allowed = {"always-on", "always-off", "previous", "list"}
        if policy not in allowed:
            raise ValueError(f"unsupported power policy: {policy}")
        return self.run("chassis", "policy", policy)

    def supported_power_policies(self) -> list[str]:
        ok, out = self.try_run("chassis", "policy", "list")
        if not ok:
            return []
        m = re.search(r"Supported chassis power policy:\s*(.+)", out)
        return m.group(1).split() if m else []
