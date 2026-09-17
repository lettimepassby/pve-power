"""日报视图：配置 SMTP、收件人和发送时机，并当场试发一封。

日报本身是 systemd timer 跑的后台任务（pve-power mail-report），这一页
只是它的配置界面加一个状态面板。之所以要有这一页：SMTP 那几个字段最容易
配错——端口和加密方式不匹配、把登录密码当成授权码、发件地址和账号不一致
——而这些错误如果只能靠「等明天早上八点半看有没有收到信」来发现，调一次
要一天。所以这里有 `t` 试发：改完立刻发一封，几秒钟就知道对不对。

密码在界面上永远不显示原文，只显示设没设。它本来就已经在 0600 的配置
文件里了，没必要再让它出现在屏幕上——身后站个人就看见了。
"""

from __future__ import annotations

import curses
import datetime as dt

from ..widgets import (
    CP_ACCENT,
    CP_CRIT,
    CP_DIM,
    CP_HIGHLIGHT,
    CP_NORMAL,
    CP_OK,
    CP_WARN,
    Prompt,
    choose,
    color,
    confirm,
    highlight_row,
    pad,
    panel,
    safe_addstr,
    show_message,
    truncate,
)
from .base import View

# 加密方式的显示名。存储值仍是 starttls / ssl / none。
SECURITY_LABELS = {
    "starttls": "STARTTLS（587，多数服务商）",
    "ssl": "SSL/TLS（465，QQ、163）",
    "none": "不加密（25，仅限内网 relay）",
}

COVERS_LABELS = {
    "yesterday": "前一天（完整的一天）",
    "today": "当天（到发信时刻为止）",
}

# 常见服务商的现成参数，省得去翻文档。填进去之后账号密码还是要自己输。
PRESETS = [
    ("QQ 邮箱", "smtp.qq.com", 465, "ssl"),
    ("163 邮箱", "smtp.163.com", 465, "ssl"),
    ("126 邮箱", "smtp.126.com", 465, "ssl"),
    ("Gmail", "smtp.gmail.com", 587, "starttls"),
    ("Outlook / Microsoft 365", "smtp.office365.com", 587, "starttls"),
    ("阿里云企业邮", "smtp.qiye.aliyun.com", 465, "ssl"),
    ("本机 relay（不加密）", "127.0.0.1", 25, "none"),
]


def _validate_port(value: str) -> str:
    v = value.strip()
    if v in ("", "0", "自动"):
        return ""
    if not v.isdigit() or not 1 <= int(v) <= 65535:
        return "端口必须是 1-65535 之间的整数，或留空表示按加密方式取默认"
    return ""


def _validate_recipients(value: str) -> str:
    addresses = [a.strip() for a in value.replace("；", ";")
                 .replace("，", ",").replace(";", ",").split(",") if a.strip()]
    if not addresses:
        return "请至少填一个收件地址"
    for address in addresses:
        if "@" not in address:
            return f"「{address}」看起来不是邮箱地址"
    return ""


def parse_recipients(value: str) -> list[str]:
    """把一行地址拆成列表。

    中文输入法下逗号分号常常打成全角，这里一并认了——让用户为了一个
    「，」重输一遍地址是没道理的。
    """
    normalised = (value.replace("；", ";").replace("，", ",")
                  .replace(";", ",").replace(" ", ","))
    return [a.strip() for a in normalised.split(",") if a.strip()]


class ReportView(View):
    title = "日报"
    hotkeys = [("Enter", "编辑"), ("s", "保存"), ("t", "试发"), ("v", "预览")]

    def _rows(self):
        """(字段名, 显示名, 显示值)。密码只显示设没设。"""
        r = self.app.config.report
        m = self.app.config.smtp
        recipients = "、".join(r.recipients) if r.recipients else "— 未设置 —"
        return [
            ("enabled", "启用日报", "开" if r.enabled else "关"),
            ("recipients", "收件人", recipients),
            ("covers", "报告范围", COVERS_LABELS.get(r.covers, r.covers)),
            ("prefix", "主题前缀", r.subject_prefix or "—"),
            ("hourly", "含逐小时表", "是" if r.include_hourly else "否"),
            ("bmc", "含硬件信息", "是" if r.include_bmc else "否"),
            ("host", "发信服务器", m.host or "— 未设置 —"),
            ("port", "端口", f"{m.port}" if m.port else f"自动（{m.effective_port()}）"),
            ("security", "加密方式", SECURITY_LABELS.get(m.security, m.security)),
            ("user", "登录账号", m.user or "— 未设置 —"),
            # 屏幕上不出现密码原文。环境变量来的要说清楚，否则用户会
            # 以为配置文件里那个空白是没设。
            ("password", "密码 / 授权码", self._password_display()),
            ("sender", "发件地址", m.sender or (m.user and f"（同账号）{m.user}") or "— 未设置 —"),
            ("sender_name", "发件人名称", m.sender_name or "—"),
        ]

    def _password_display(self) -> str:
        import os

        if os.environ.get("PVE_POWER_SMTP_PASSWORD"):
            return "已设置（来自环境变量 PVE_POWER_SMTP_PASSWORD）"
        if self.app.config.smtp.password:
            return "已设置（••••••）"
        return "— 未设置 —"

    # ---------------- 绘制 ----------------

    def draw(self, win, height: int, width: int) -> None:
        rows = self._rows()
        list_h = min(len(rows) + 2, max(6, height - 9))
        half = max(46, width // 2)

        panel(win, 0, 0, list_h, half, "日报设置")
        visible = list_h - 2
        self.scroll = max(0, min(self.scroll, max(0, len(rows) - visible)))
        for i in range(min(visible, len(rows))):
            idx = self.scroll + i
            _, label, value = rows[idx]
            y = 1 + i
            selected = idx == self.cursor
            if selected:
                highlight_row(win, y, 1, half - 2)
            attr = color(CP_HIGHLIGHT) if selected else color(CP_NORMAL)
            safe_addstr(win, y, 2, pad(label, 16),
                        attr if selected else color(CP_DIM))
            safe_addstr(win, y, 19,
                        pad(truncate(str(value), half - 21), half - 21), attr)

        self._draw_status(win, 0, half, list_h, width - half)
        self._draw_footer(win, list_h, 0, height - list_h, width)

    def _draw_status(self, win, y, x, h, w) -> None:
        panel(win, y, x, h, w, "状态")
        cfg = self.app.config
        status = self.data.report_status
        line = y + 1

        def put(label, value, attr=None):
            nonlocal line
            if line >= y + h - 1:
                return
            safe_addstr(win, line, x + 2, pad(label, 14), color(CP_DIM))
            safe_addstr(win, line, x + 17, truncate(str(value), w - 19),
                        attr or color(CP_NORMAL))
            line += 1

        # 定时器。这是「明天早上到底会不会发」的唯一可靠答案：配置里
        # enabled=true 但没 enable 单元的话，什么也不会发生。
        if not status:
            put("定时器", "读取中…", color(CP_DIM))
        elif not status.get("available"):
            put("定时器", "本机没有 systemctl", color(CP_DIM))
        elif not status.get("installed"):
            put("定时器", "单元未安装（跑一次 tools/install.sh）",
                color(CP_WARN, bold=True))
        elif status.get("enabled"):
            put("定时器", "已启用", color(CP_OK, bold=True))
            nxt = _usec_to_text(status.get("NextElapseUSecRealtime"))
            if nxt:
                put("下次发送", nxt, color(CP_ACCENT))
        else:
            put("定时器", "未启用", color(CP_WARN, bold=True))

        last = status.get("last_sent") if status else ""
        put("上次发送", last or "还没发过", color(CP_NORMAL) if last
            else color(CP_DIM))
        put("报告日期", _target_day(cfg).isoformat(), color(CP_ACCENT))

        # 配置问题。validate_mail 只在 enabled 时才较真，所以这里
        # 单独把「想发但缺东西」也算上。
        problems = self._problems()
        if line < y + h - 1:
            line += 1
            if problems:
                safe_addstr(win, line, x + 2,
                            truncate(f"⚠ {problems[0]}", w - 4),
                            color(CP_CRIT, bold=True))
                if len(problems) > 1 and line + 1 < y + h - 1:
                    safe_addstr(win, line + 1, x + 4,
                                truncate(f"另有 {len(problems) - 1} 项待补",
                                         w - 6),
                                color(CP_WARN))
            elif cfg.smtp.host and cfg.report.recipients:
                safe_addstr(win, line, x + 2, "配置完整，可以按 t 试发一封",
                            color(CP_OK))

    def _draw_footer(self, win, y, x, h, w) -> None:
        if h < 3:
            return
        panel(win, y, x, h, w, "说明")
        cfg = self.app.config
        lines = [
            "日报由 systemd 定时器在后台发送，默认每天 8:30 发前一天的完整报告。",
            "内容：电量、电费、与前一天对比、逐小时曲线、分时段拆分、本月累计，"
            "以及当天的 BMC 事件和异常传感器。",
            "",
            "Enter 编辑选中项   s 保存到配置文件   P 选择服务商预设",
            "t 立刻试发一封（会真的发出去）   v 预览报告正文（不发信）",
            "",
            "QQ / 163 邮箱要用「授权码」，在邮箱设置里单独生成，不是登录密码。",
            "密码也可以不写进配置文件，改放 /etc/pve-power/smtp.env"
            "（PVE_POWER_SMTP_PASSWORD=...，权限 0600）。",
        ]
        if cfg.report.enabled:
            status = self.data.report_status
            if status.get("available") and not status.get("enabled"):
                lines.insert(
                    0,
                    "⚠ 配置里已启用日报，但 systemd 定时器还没打开 —— "
                    "执行：systemctl enable --now pve-power-report.timer",
                )
        for i, text in enumerate(lines):
            row = y + 1 + i
            if row >= y + h - 1:
                break
            attr = color(CP_DIM)
            if text.startswith("⚠"):
                attr = color(CP_WARN, bold=True)
            safe_addstr(win, row, x + 2, truncate(text, w - 4), attr)

    def _problems(self) -> list[str]:
        """这份配置还缺什么才能发信。"""
        cfg = self.app.config
        problems = [p for p in cfg.validate_mail()]
        if not cfg.smtp.host:
            problems.append("没有发信服务器（smtp.host）")
        if not cfg.report.recipients:
            problems.append("没有收件人")
        if not cfg.smtp.effective_sender():
            problems.append("没有发件地址（登录账号或发件地址填一个）")
        # 去重但保持顺序：validate_mail 和上面几条会说到同一件事。
        return list(dict.fromkeys(problems))

    # ---------------- 按键 ----------------

    def handle_key(self, key: int) -> bool:
        rows = self._rows()
        height, _ = self.app.content_size()
        visible = max(1, min(len(rows), height - 9))
        if self.handle_list_key(key, len(rows), visible):
            return True
        if key in (curses.KEY_ENTER, 10, 13):
            self._edit(rows[self.cursor][0])
            return True
        if key == ord("s"):
            self.app.save_config()
            return True
        if key == ord("P"):
            self._load_preset()
            return True
        if key == ord("t"):
            self._send_test()
            return True
        if key == ord("v"):
            self._preview()
            return True
        return False

    def _edit(self, field: str) -> None:
        stdscr = self.app.stdscr
        prompt = Prompt(stdscr)
        cfg = self.app.config
        r, m = cfg.report, cfg.smtp

        if field == "enabled":
            r.enabled = not r.enabled
        elif field == "recipients":
            value = prompt.ask(
                "收件人（多个用逗号分隔）：", "、".join(r.recipients).replace("、", ","),
                validator=_validate_recipients,
            )
            if value is not None:
                r.recipients = parse_recipients(value)
        elif field == "covers":
            idx = choose(stdscr, "报告覆盖哪一天",
                         [COVERS_LABELS["yesterday"], COVERS_LABELS["today"]])
            if idx is not None:
                r.covers = ["yesterday", "today"][idx]
                if r.covers == "today":
                    self.app.flash(
                        "记得把定时器时间也改到当天晚些时候，"
                        "否则早上发出去的是只过了几小时的半天数据"
                    )
        elif field == "prefix":
            value = prompt.ask("主题前缀（留空则不加）：", r.subject_prefix)
            if value is not None:
                r.subject_prefix = value.strip()
        elif field == "hourly":
            r.include_hourly = not r.include_hourly
        elif field == "bmc":
            r.include_bmc = not r.include_bmc
        elif field == "host":
            value = prompt.ask("发信服务器（SMTP 主机名）：", m.host)
            if value is not None:
                m.host = value.strip()
        elif field == "port":
            value = prompt.ask(
                f"端口（留空＝按加密方式自动取 {m.effective_port()}）：",
                str(m.port) if m.port else "", validator=_validate_port,
            )
            if value is not None:
                v = value.strip()
                m.port = int(v) if v and v != "0" else 0
        elif field == "security":
            order = ["starttls", "ssl", "none"]
            idx = choose(stdscr, "加密方式",
                         [SECURITY_LABELS[k] for k in order])
            if idx is not None:
                m.security = order[idx]
                # 端口是自动的话，换加密方式就等于换了端口，说一声。
                if not m.port:
                    self.app.flash(f"端口将使用 {m.effective_port()}")
        elif field == "user":
            value = prompt.ask("登录账号（通常就是完整邮箱地址）：", m.user)
            if value is not None:
                m.user = value.strip()
        elif field == "password":
            import os

            if os.environ.get("PVE_POWER_SMTP_PASSWORD"):
                show_message(
                    stdscr, "密码来自环境变量",
                    "当前密码取自环境变量 PVE_POWER_SMTP_PASSWORD，"
                    "它的优先级高于配置文件。\n\n"
                    "要改的话请编辑 /etc/pve-power/smtp.env，"
                    "或者先取消该环境变量再回到这里设置。",
                )
                return
            value = prompt.ask("密码 / 授权码：", "", secret=True)
            if value is not None:
                m.password = value
                self.app.flash(
                    "密码已填入 —— 按 s 保存（配置文件权限是 0600）"
                )
        elif field == "sender":
            value = prompt.ask(
                "发件地址（留空＝与登录账号相同）：", m.sender)
            if value is not None:
                m.sender = value.strip()
        elif field == "sender_name":
            value = prompt.ask("发件人显示名称：", m.sender_name)
            if value is not None:
                m.sender_name = value.strip()

        problems = cfg.validate()
        if problems:
            self.app.flash(problems[0], ok=False)
        elif field != "password":
            self.app.flash("已修改 —— 按 s 保存到磁盘")

    def _load_preset(self) -> None:
        stdscr = self.app.stdscr
        labels = [f"{name}  —  {host}:{port}" for name, host, port, _ in PRESETS]
        idx = choose(stdscr, "选择服务商预设（之后还要填账号和密码）", labels)
        if idx is None:
            return
        name, host, port, security = PRESETS[idx]
        m = self.app.config.smtp
        m.host, m.port, m.security = host, port, security
        self.app.flash(f"已套用 {name} 的参数 —— 还需要填登录账号和密码")

    # ---------------- 动作 ----------------

    def _preview(self) -> None:
        """把报告正文显示出来，不发信。

        BMC 那部分要花几秒，但预览的意义就在于看到真实的东西，
        所以这里不跳过它。
        """
        from ... import report as report_mod

        cfg = self.app.config
        self.app.flash("正在生成预览…")
        try:
            rep = report_mod.build(
                cfg, self.app.storage,
                day=_target_day(cfg),
                ipmi=self.app.ipmi if cfg.report.include_bmc else None,
            )
            text = report_mod.render_text(rep)
        except Exception as exc:
            show_message(self.app.stdscr, "生成预览失败", str(exc), is_error=True)
            return
        show_message(self.app.stdscr, f"日报预览 — {rep.subject()}", text)

    def _send_test(self) -> None:
        """立刻发一封。

        这封是真的会送到收件箱的，所以先确认。走的就是定时器那条路径
        （同样的组装、同样的投递），不是另写一套——否则「试发成功但定时
        发送失败」就成了可能。
        """
        from ... import mailer, report as report_mod

        cfg = self.app.config
        stdscr = self.app.stdscr
        problems = self._problems()
        if problems:
            show_message(
                stdscr, "配置还不完整",
                "请先补上：\n\n" + "\n".join(f"  • {p}" for p in problems),
                is_error=True,
            )
            return
        recipients = cfg.report.recipients
        if not confirm(
            stdscr,
            f"现在给 {'、'.join(recipients)} 发一封日报吗？"
            f"（服务器 {cfg.smtp.host}:{cfg.smtp.effective_port()}，"
            f"{cfg.smtp.security}）这封信会真的送达。",
        ):
            return

        self.app.flash("正在发送…")
        try:
            rep = report_mod.build(
                cfg, self.app.storage,
                day=_target_day(cfg),
                ipmi=self.app.ipmi if cfg.report.include_bmc else None,
            )
            message = mailer.build_message(
                cfg.smtp, recipients,
                subject=rep.subject(cfg.report.subject_prefix),
                text=report_mod.render_text(rep),
                html=report_mod.render_html(rep),
            )
            mailer.send(cfg.smtp, message, recipients)
        except mailer.MailError as exc:
            self.app.storage.log_event("report_failed", str(exc))
            show_message(stdscr, "发送失败", str(exc), is_error=True)
            return
        except Exception as exc:
            show_message(stdscr, "发送失败", f"{type(exc).__name__}: {exc}",
                         is_error=True)
            return

        # 试发不写 last_report_sent：那个标记是给「今天已经发过了」用的，
        # 试发把它置上会让当天的定时发送被跳过。
        self.app.storage.log_event("report_test", f"-> {', '.join(recipients)}")
        show_message(
            stdscr, "已发送",
            f"已发给 {'、'.join(recipients)}。\n\n"
            "这封是试发，不会影响定时任务 —— 到点了还会照常发当天那封。",
        )
        self.data.invalidate("report_status")


def _target_day(config) -> dt.date:
    from ...report import resolve_day

    return resolve_day(config.report.covers)


def _usec_to_text(value) -> str:
    """systemd 的 NextElapseUSecRealtime 是微秒时间戳；0 表示没有排期。"""
    try:
        usec = int(value)
    except (TypeError, ValueError):
        return ""
    if usec <= 0:
        return ""
    when = dt.datetime.fromtimestamp(usec / 1_000_000)
    delta = when - dt.datetime.now()
    hours = int(delta.total_seconds() // 3600)
    if hours >= 24:
        rough = f"{hours // 24} 天后"
    elif hours >= 1:
        rough = f"{hours} 小时后"
    else:
        rough = f"{max(0, int(delta.total_seconds() // 60))} 分钟后"
    return f"{when:%m-%d %H:%M}（{rough}）"
