"""风扇视图：转速、相对占空比，以及本机是否支持手动调速。"""

from __future__ import annotations

import curses

from ..widgets import (
    CP_ACCENT,
    CP_CRIT,
    CP_DIM,
    CP_HIGHLIGHT,
    CP_NORMAL,
    CP_OK,
    CP_TITLE,
    CP_WARN,
    CP_CRIT_BAR,
    CP_WARN_BAR,
    CP_OK_BAR,
    color,
    draw_bar,
    cwidth,
    hbar,
    highlight_row,
    pad,
    panel,
    rpad,
    safe_addstr,
    table_header,
    show_message,
    truncate,
)
from .base import View

POSITION_LABELS = {"Front": "前", "Rear": "后"}

# 机箱风扇的额定上限。这台 SA5112M4 实测满速约 7900 RPM，取 8000 作为
# 百分比基准；低于实测峰值会让常态转速显示成 100%，反而看不出余量。
NOMINAL_MAX_RPM = 8000.0

HELP_MANUAL = """\
这台服务器的 BMC 没有开放手动调速接口。

已按顺序探测过下列厂商私有命令，全部返回 0xc1（命令不存在）：

  Inspur 风扇模式        0x3a 0x07
  Inspur/曙光 散热策略   0x3a 0x0b / 0x0d
  AMI 转速档 / 占空比    0x3a 0xd7 / 0xda
  通用 手动占空比        0x3c 0x2d / 0x2e / 0x2f
  Dell/超微 风扇模式     0x30 0x30 / 0x30 0xce

也就是说，固件 4.12 把转速完全交给 BMC 的温控算法，IPMI 侧没有留出
写入口。网上流传的那些 raw 命令是别的机型/固件的，发到这台机器上只会
被拒绝，不会生效。

还能怎么降噪：

  1. BMC Web 界面「电源和风扇 → 风扇转速控制」有档位可调（例如 20%），
     但 BMC 专用网口 192.168.100.100 目前从本机 ping 不通、80/443 也
     不通，需要先把管理网连通，或者用共享网口。
  2. 进 BIOS 的 Advanced → Thermal / Fan Profile，把策略从 Performance
     改成 Acoustic / Power Saving，这是不依赖 IPMI 的办法。
  3. 找浪潮售后要对应固件的《BMC IPMI 命令手册》，或升级 BMC 固件后
     再回到本页重新探测（按 p）。

当前机箱温度很宽裕（CPU 53/47℃，进风 26℃），确实存在降噪空间 ——
只是这一版固件不让 IPMI 来调。
"""


class FansView(View):
    title = "风扇"
    hotkeys = [("p", "探测调速"), ("r", "刷新")]

    def _fans(self):
        return self.data.fans or []

    def _reference_rpm(self, fans) -> float:
        """百分比基准：额定上限与实测峰值取大者。

        只用实测峰值的话，所有风扇同速时最快的那个永远是 100%，看不出
        离满速还有多远；只用额定值的话，某台机器超过额定就会溢出。
        """
        live = [f.rpm for f in fans if f.rpm is not None]
        return max(NOMINAL_MAX_RPM, max(live) if live else 0.0)

    def draw(self, win, height: int, width: int) -> None:
        fans = self._fans()
        present = [f for f in fans if f.present]
        missing = [f for f in fans if not f.present]

        list_h = max(6, height - 8)
        panel(win, 0, 0, list_h, width, "风扇转速")

        if not fans:
            safe_addstr(win, 2, 2, "正在读取传感器…（首次进入约需 5 秒）",
                        color(CP_DIM))
        else:
            table_header(
                win, 1, 2,
                pad("风扇", 14) + rpad("转速", 10) + "  "
                + pad("状态", 8) + "占空比（按额定转速折算）",
                width - 4,
            )

            reference = self._reference_rpm(fans)
            visible = list_h - 3
            total = len(fans)
            self.scroll = max(0, min(self.scroll, max(0, total - visible)))
            self.cursor = max(0, min(self.cursor, max(0, total - 1)))

            for i in range(min(visible, total)):
                idx = self.scroll + i
                if idx >= total:
                    break
                fan = fans[idx]
                y = 2 + i
                selected = idx == self.cursor
                if selected:
                    highlight_row(win, y, 1, width - 2)
                base = color(CP_HIGHLIGHT) if selected else color(CP_NORMAL)

                position = POSITION_LABELS.get(fan.position, fan.position)
                label = f"{fan.slot} 号{position}" if fan.slot else fan.name
                rpm_text = f"{fan.rpm:.0f} RPM" if fan.present else "—"
                line = pad(truncate(label, 14), 14) + rpad(rpm_text, 10) + "  "
                safe_addstr(win, y, 2, line, base)
                x = 2 + cwidth(line)

                if not fan.present:
                    # 空槽位：机箱报 Cooling/Fan Fault=false，说明这是没装
                    # 风扇，不是坏了，所以用灰色而不是红色。
                    safe_addstr(win, y, x, pad("未安装", 8),
                                base if selected else color(CP_DIM))
                    safe_addstr(win, y, x + 8, "（该槽位为空）",
                                base if selected else color(CP_DIM))
                    continue

                status_text = "正常" if fan.ok else fan.status
                safe_addstr(win, y, x, pad(status_text, 8),
                            base if selected else
                            (color(CP_OK) if fan.ok else color(CP_CRIT, bold=True)))
                x += 8

                percent = fan.percent_of(reference) or 0.0
                bar_width = max(8, width - x - 10)
                if percent >= 85:
                    bar_attr = color(CP_CRIT_BAR)
                elif percent >= 60:
                    bar_attr = color(CP_WARN_BAR)
                else:
                    bar_attr = color(CP_OK_BAR)
                if selected:
                    safe_addstr(win, y, x,
                                pad(hbar(percent, 100.0, bar_width), bar_width),
                                base)
                else:
                    draw_bar(win, y, x, percent, 100.0, bar_width, bar_attr)
                safe_addstr(win, y, x + bar_width + 1, rpad(f"{percent:.0f}%", 5),
                            base if selected else color(CP_DIM))

        # ---- 调速能力面板 ----
        y = list_h
        panel(win, y, 0, height - list_h, width, "手动调速")

        control = self.data.fan_control
        if control is None:
            safe_addstr(win, y + 1, 2, "正在探测 BMC 是否支持手动调速…",
                        color(CP_DIM))
        elif control.supported:
            safe_addstr(win, y + 1, 2,
                        truncate(f"可用接口：{control.method}", width - 4),
                        color(CP_OK, bold=True))
            safe_addstr(win, y + 2, 2,
                        truncate(control.detail, width - 4), color(CP_DIM))
        else:
            safe_addstr(win, y + 1, 2,
                        "本机 BMC（固件 4.12）不支持通过 IPMI 手动调速",
                        color(CP_WARN, bold=True))
            safe_addstr(win, y + 2, 2,
                        truncate("已探测 10 组厂商私有命令，均返回"
                                 "「命令不存在」。按 p 查看详情和替代方案。",
                                 width - 4),
                        color(CP_DIM))

        # ---- 汇总 ----
        if present:
            hottest = max(present, key=lambda f: f.rpm)
            reference = self._reference_rpm(fans)
            avg = sum(f.rpm for f in present) / len(present)
            summary = (f"{len(present)} 个在转，{len(missing)} 个空槽   "
                       f"平均 {avg:.0f} RPM   最高 {hottest.rpm:.0f} RPM"
                       f"（约 {hottest.percent_of(reference):.0f}%）")
            safe_addstr(win, height - 1, 2, truncate(summary, width - 4),
                        color(CP_NORMAL))

    def handle_key(self, key: int) -> bool:
        height, _ = self.app.content_size()
        visible = max(1, height - 11)
        if self.handle_list_key(key, len(self._fans()), visible):
            return True
        if key == ord("p"):
            control = self.data.fan_control
            if control is None:
                self.app.flash("还在探测，请稍候")
                return True
            if control.supported:
                lines = "\n".join(f"  {label}    {verdict}"
                                  for label, verdict in control.probed)
                show_message(self.app.stdscr, "手动调速探测结果",
                             f"可用接口：{control.method}\n\n{lines}")
            else:
                show_message(self.app.stdscr, "为什么不能调速", HELP_MANUAL)
            return True
        return False
