"""A stand-in for IpmiTool, returning representative Inspur SA5112M4 output.

The values here are copied from a real BMC so the views are exercised
against the shapes they will actually meet: a sensor with no reading, a
sensor out of spec, empty user slots, and a default-named admin account.
"""

from __future__ import annotations

from pvepower.ipmi import (
    IpmiError,
    IpmiUser,
    LanConfig,
    PowerReading,
    SelEntry,
    Sensor,
)


class FakeIpmi:
    remote = False

    def __init__(self, failing: bool = False):
        self.failing = failing

    def _fail(self):
        raise IpmiError(["stub"], 1, "stubbed BMC failure")

    def power_reading(self):
        if self.failing:
            self._fail()
        return PowerReading(
            instantaneous=190, minimum=130, maximum=254, average=188,
            timestamp="09/16/2026 02:29:42 PM CST",
            sampling_period="00000005", state="activated",
        )

    def sensors(self):
        if self.failing:
            self._fail()
        return [
            Sensor("CPU0_Temp", 58.0, "degrees C", "ok",
                   upper_nc=101.0, upper_crit=103.0),
            Sensor("CPU1_Temp", 99.0, "degrees C", "nc",
                   upper_nc=101.0, upper_crit=103.0),
            Sensor("82599_Temp", None, "discrete", "ns"),
            Sensor("SYS_12V", 12.03, "Volts", "ok", upper_crit=13.0),
            Sensor("SYS_3.3V", 3.27, "Volts", "ok", upper_crit=3.6),
            Sensor("FAN_0_Front", 8400.0, "RPM", "ok", upper_crit=20000.0),
            Sensor("FAN_0_Rear", 8600.0, "RPM", "ok", upper_crit=20000.0),
            # An unpopulated bay: reads as absent, not as a fault.
            Sensor("FAN_3_Front", None, "RPM", "ns"),
            Sensor("PSU0_PIN", 190.0, "Watts", "ok"),
            Sensor("CPU0_Status", None, "discrete", "ok"),
        ]

    def probe_fan_control(self):
        from pvepower.ipmi import FanControl
        if self.failing:
            self._fail()
        return FanControl(
            supported=False,
            probed=[("Inspur 风扇模式 (0x3a 0x07)", "不支持")],
        )

    def fans(self):
        from pvepower.ipmi import IpmiTool
        return IpmiTool.fans_from_sensors(self.sensors())

    def try_run(self, *args):
        if self.failing:
            return False, "stubbed BMC failure"
        return True, ""

    def run(self, *args, check=True):
        if self.failing:
            self._fail()
        return ""

    def chassis_status(self):
        if self.failing:
            self._fail()
        return {
            "System Power": "on",
            "Power Overload": "false",
            "Power Interlock": "inactive",
            "Main Power Fault": "false",
            "Power Control Fault": "false",
            "Power Restore Policy": "always-off",
            "Last Power Event": "ac-failed",
            "Chassis Intrusion": "inactive",
            "Front-Panel Lockout": "inactive",
            "Drive Fault": "false",
            "Cooling/Fan Fault": "false",
        }

    def supported_power_policies(self):
        return [] if self.failing else ["always-off", "always-on", "previous"]

    def mc_info(self):
        if self.failing:
            self._fail()
        return {
            "Device ID": "32",
            "Firmware Revision": "4.12",
            "IPMI Version": "2.0",
            "Manufacturer Name": "Inspur(BeiJing) Electronic Information Industry Co.,Ltd",
            "Product Name": "Unknown (0xAABB)",
            "Device Available": "yes",
        }

    def fru(self):
        if self.failing:
            self._fail()
        return {
            "Chassis Type": "Rack Mount Chassis",
            "Board Mfg": "Inspur",
            "Board Product": "Shuyu",
            "Board Serial": "MBH606S40933A70",
            "Product Manufacturer": "INSPUR",
            "Product Name": "SA5112M4",
            "Product Serial": "817307946",
        }

    def lan_config(self, channel=1):
        if self.failing:
            self._fail()
        return LanConfig(
            channel=channel,
            fields={
                "IP Address Source": "Static Address",
                "IP Address": "192.168.100.100",
                "Subnet Mask": "255.255.255.0",
                "MAC Address": "6c:92:bf:59:39:03",
                "Default Gateway IP": "0.0.0.0",
                "802.1q VLAN ID": "401",
                "SNMP Community String": "Inspur",
            },
        )

    def users(self, channel=1):
        if self.failing:
            self._fail()
        return [
            IpmiUser(1, "albert", False, False, True, "ADMINISTRATOR"),
            IpmiUser(2, "ADMIN", True, True, True, "ADMINISTRATOR"),
            IpmiUser(3, "root", True, True, True, "ADMINISTRATOR"),
            IpmiUser(4, "qwer", True, False, False, "NO ACCESS"),
            IpmiUser(5, "", True, False, False, "NO ACCESS"),
            IpmiUser(6, "", True, False, False, "NO ACCESS"),
            IpmiUser(7, "", True, False, False, "NO ACCESS"),
        ]

    def sel_entries(self, limit=200):
        if self.failing:
            return []
        return [
            SelEntry("236", "09/15/2026", "08:43:10 PM CST", "OS Boot #0x8d",
                     "boot completed - device not specified", "Asserted"),
            SelEntry("235", "09/15/2026", "08:42:43 PM CST", "Power Unit #0x61",
                     "AC lost", "Asserted"),
            SelEntry("234", "09/15/2026", "08:41:50 PM CST",
                     "Drive Slot / Bay #0xa5", "Drive Present", "Asserted"),
            SelEntry("233", "09/14/2026", "11:02:11 AM CST", "CPU0_Temp",
                     "Upper Critical going high", "Asserted"),
        ]

    def sel_info(self):
        if self.failing:
            self._fail()
        return {
            "Version": "1.5 (v1.5, v2 compliant)",
            "Entries": "566",
            "Percent Used": "14%",
            "Last Add Time": "09/15/2026 08:43:10 PM CST",
        }
