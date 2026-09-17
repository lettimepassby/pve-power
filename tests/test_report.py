"""日报的内容组装、渲染和发送。

发信这部分不打桩 smtplib，而是在本地起一个真的 SMTP 服务器（下面那个
几十行的 FakeSMTPServer）让 smtplib 真连上去。打桩只能证明「我调用了
sendmail」，证明不了信封地址对不对、多部分结构是否合法、中文主题有没有
正确编码——而这些恰恰是发信真正会出错的地方。
"""

from __future__ import annotations

import datetime as dt
import email
import os
import smtplib
import socket
import sys
import tempfile
import threading
import unittest
from email import policy

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pvepower import mailer, report as R
from pvepower.config import Config, SmtpConfig, TariffConfig, TouPeriod
from pvepower.ipmi import SelEntry, Sensor
from pvepower.storage import Storage


# ---------------------------------------------------------------- 夹具


def build_config(db_path: str) -> Config:
    cfg = Config()
    cfg.db_path = db_path
    cfg.tariff = TariffConfig(
        mode="tou", currency="CNY", flat_price=0.98,
        tou_periods=[
            TouPeriod("峰", 1.30, list(range(8, 12)) + list(range(18, 22))),
            TouPeriod("平", 0.98, [7, 12, 13, 14, 15, 16, 17, 22, 23]),
            TouPeriod("谷", 0.45, list(range(0, 7))),
        ],
    )
    cfg.report.recipients = ["ops@example.com"]
    return cfg


class ReportFixture(unittest.TestCase):
    """两整天的采样，夜里低载白天高载，好让逐小时表有形状。"""

    WATTS = {"night": 150.0, "day": 220.0}

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.config = build_config(os.path.join(self.tmp.name, "power.db"))
        self.storage = Storage(self.config.db_path)
        self.today = dt.date(2026, 9, 17)
        self.day = dt.date(2026, 9, 16)
        start = dt.datetime.combine(self.day - dt.timedelta(days=1), dt.time.min)
        end = dt.datetime.combine(self.today, dt.time.min)
        t = start
        while t < end:
            watts = self.WATTS["night"] if t.hour < 7 else self.WATTS["day"]
            self.storage.record(watts, self.config.tariff, when=t,
                                max_gap_seconds=900)
            t += dt.timedelta(minutes=10)
        self.storage.commit()
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(self.storage.close)

    def build(self, **kwargs):
        kwargs.setdefault("day", self.day)
        kwargs.setdefault("now", dt.datetime.combine(self.today, dt.time(8, 30)))
        return R.build(self.config, self.storage, **kwargs)


# ---------------------------------------------------------------- 组装


class TestBuild(ReportFixture):
    def test_full_day_is_summed(self):
        rep = self.build()
        self.assertEqual(rep.window_label, "全天")
        self.assertFalse(rep.partial)
        # 7 小时 @150W + 17 小时 @220W = 4.79 kWh。允许一个采样间隔的误差。
        expected = (7 * 150 + 17 * 220) / 1000.0
        self.assertAlmostEqual(rep.day_agg.kwh, expected, delta=0.05)

    def test_hourly_has_24_slots_and_a_peak(self):
        rep = self.build()
        self.assertEqual(len(rep.hourly), 24)
        self.assertTrue(rep.has_hourly)
        # 高峰一定落在白天段，不会是夜里那几个低载小时。
        self.assertGreaterEqual(int(rep.peak_hour.label[:2]), 7)

    def test_tou_periods_split_the_day(self):
        rep = self.build()
        names = {name for name, _, _ in rep.periods}
        self.assertEqual(names, {"峰", "平", "谷"})
        # 分时段的电量加起来应该等于全天。
        self.assertAlmostEqual(sum(k for _, k, _ in rep.periods),
                               rep.day_agg.kwh, places=6)

    def test_partial_day_stops_at_now(self):
        """报「今天」时窗口截到此刻，不能把还没发生的小时算成缺口。"""
        now = dt.datetime.combine(self.today, dt.time(8, 30))
        rep = self.build(day=self.today, now=now)
        self.assertTrue(rep.partial)
        self.assertIn("08:30", rep.window_label)

    def test_empty_day_does_not_crash(self):
        """很久以前的一天没有任何数据。avg_watts 在这条路径上是 None，
        渲染时不能变成 "None W"。"""
        rep = self.build(day=dt.date(2020, 1, 1))
        self.assertEqual(rep.day_agg.kwh, 0.0)
        self.assertFalse(rep.has_hourly)
        self.assertIsNone(rep.peak_hour)
        text = R.render_text(rep)
        self.assertNotIn("None", text)
        self.assertNotIn("None", R.render_html(rep))

    def test_no_bmc_means_no_hardware_section(self):
        rep = self.build()
        self.assertFalse(rep.bmc_checked)
        self.assertNotIn("硬件", R.render_text(rep))


class TestDelta(unittest.TestCase):
    def test_rounding_noise_is_not_reported_as_change(self):
        # 这是真跑出来的一个措辞 bug：差值 0.001 被写成 "−0.00 CNY（−0.1%）"。
        self.assertEqual(R._delta_text(4.450, 4.451, "CNY"), "基本持平")

    def test_increase_and_decrease(self):
        self.assertIn("+1.00", R._delta_text(5.0, 4.0, "CNY"))
        self.assertIn("−1.00", R._delta_text(3.0, 4.0, "CNY"))

    def test_no_baseline_is_stated_not_guessed(self):
        self.assertIn("无数据", R._delta_text(5.0, 0.0, "CNY"))
        self.assertEqual(R._delta_text(0.0, 0.0, "CNY"), "")


class TestResolveDay(unittest.TestCase):
    def test_default_is_the_previous_complete_day(self):
        now = dt.datetime(2026, 9, 17, 8, 30)
        self.assertEqual(R.resolve_day("yesterday", now), dt.date(2026, 9, 16))
        self.assertEqual(R.resolve_day("today", now), dt.date(2026, 9, 17))

    def test_month_boundary(self):
        now = dt.datetime(2026, 9, 1, 8, 30)
        self.assertEqual(R.resolve_day("yesterday", now), dt.date(2026, 8, 31))


# ---------------------------------------------------------------- 硬件


class FakeIpmi:
    """只实现 report._fill_bmc 用到的三个方法。"""

    def __init__(self, sensors=None, entries=None, fail=""):
        self._sensors = sensors or []
        self._entries = entries or []
        self._fail = fail

    def _maybe_fail(self):
        if self._fail:
            from pvepower.ipmi import IpmiError
            raise IpmiError(["sensor"], 1, self._fail)

    def fru(self):
        self._maybe_fail()
        return {"Product Name": "SA5112M4"}

    def sensors(self):
        self._maybe_fail()
        return self._sensors

    def sel_entries(self, limit=200):
        self._maybe_fail()
        return self._entries


def temp_sensor(name, value, status="ok", crit=103.0):
    return Sensor(name=name, value=value, unit="degrees C", status=status,
                  upper_nc=crit - 3, upper_crit=crit)


def volt_sensor(name, value):
    """标称 12V 的电压轨，两侧都有阈值。"""
    return Sensor(name=name, value=value, unit="Volts", status="ok",
                  lower_crit=9.024, lower_nc=10.528,
                  upper_nc=13.536, upper_crit=14.288)


class TestHardwareSection(ReportFixture):
    def test_nominal_voltage_is_not_listed_as_a_fault(self):
        """和 test_sensors.py 里那个误报同源：11.844V 是最健康的读数，
        不该因为它占上限的 83% 就进故障清单。"""
        ipmi = FakeIpmi(sensors=[volt_sensor("SYS_12V", 11.844),
                                 temp_sensor("CPU0_Temp", 57.0)])
        rep = self.build(ipmi=ipmi)
        self.assertEqual(rep.faults, [])
        self.assertFalse(rep.has_problems)
        self.assertNotIn("⚠", rep.subject())

    def test_overheating_sensor_reaches_the_subject_line(self):
        ipmi = FakeIpmi(sensors=[temp_sensor("CPU1_Temp", 104.0, status="cr")])
        rep = self.build(ipmi=ipmi)
        self.assertEqual([s.name for s in rep.faults], ["CPU1_Temp"])
        self.assertTrue(rep.has_problems)
        self.assertIn("⚠", rep.subject())
        self.assertIn("CPU1_Temp", R.render_text(rep))
        self.assertIn("CPU1_Temp", R.render_html(rep))

    def test_sel_is_filtered_to_the_reported_day(self):
        entries = [
            SelEntry("1", "09/16/26", "10:00:00", "Power Unit", "AC lost", "Asserted"),
            SelEntry("2", "09/15/26", "10:00:00", "Drive Slot", "Drive Present", ""),
            SelEntry("3", "Pre-Init", "0000028848", "Microcontroller", "x", ""),
        ]
        rep = self.build(ipmi=FakeIpmi(entries=entries))
        self.assertEqual([e.record_id for e in rep.sel], ["1"])

    def test_bmc_failure_does_not_lose_the_energy_report(self):
        """ipmitool 抽风时报告照发，只在硬件那一节写明没读到。"""
        rep = self.build(ipmi=FakeIpmi(fail="ipmitool: 设备忙"))
        self.assertTrue(rep.bmc_checked)
        self.assertIn("设备忙", rep.bmc_error)
        text = R.render_text(rep)
        self.assertIn("读取 BMC 失败", text)
        # 电量数字还在。
        self.assertIn(f"{rep.day_agg.kwh:.3f} kWh", text)

    def test_product_name_replaces_hostname_in_the_subject(self):
        rep = self.build(ipmi=FakeIpmi(sensors=[temp_sensor("CPU0_Temp", 50.0)]))
        self.assertIn("SA5112M4", rep.subject())


class TestCoverage(ReportFixture):
    def test_a_gappy_day_says_so(self):
        """覆盖率低意味着那天的电费本身就是少算的。不说清楚，
        读的人会以为那天真的省电了。"""
        rep = self.build()
        rep.coverage = 0.42
        self.assertTrue(rep.has_problems)
        self.assertIn("少算", R.render_text(rep))
        self.assertIn("42%", R.render_html(rep))


# ---------------------------------------------------------------- 渲染


class TestRender(ReportFixture):
    def test_text_report_is_complete_on_its_own(self):
        text = R.render_text(self.build())
        for needle in ("用电日报", "电量", "电费", "本月累计", "覆盖率", "逐小时"):
            self.assertIn(needle, text)

    def test_text_bar_is_ascii_only(self):
        """日报会被转发进只认 ASCII 的地方，Unicode 方块在那里是一排问号。"""
        text = R.render_text(self.build())
        start = text.index("逐小时")
        block = text[start:text.index("----", start)]
        self.assertIn("#", block)
        self.assertNotIn("█", block)
        self.assertNotIn("▂", block)

    def test_html_escapes_values_that_come_from_the_bmc(self):
        """传感器名和事件文本来自 BMC，不是我们写的常量。"""
        ipmi = FakeIpmi(
            sensors=[temp_sensor("<script>alert(1)</script>", 104.0, status="cr")],
            entries=[SelEntry("1", "09/16/26", "10:00", "S&D",
                              "a<b>c", "Asserted")],
        )
        html = R.render_html(self.build(ipmi=ipmi))
        self.assertNotIn("<script>", html)
        self.assertIn("&lt;script&gt;", html)
        self.assertIn("S&amp;D", html)

    def test_html_is_self_contained(self):
        """邮件客户端不会去取外部资源，取了也常被拦。"""
        html = R.render_html(self.build())
        for scheme in ("http://", "https://", "src="):
            self.assertNotIn(scheme, html)

    def test_html_stays_well_under_the_gmail_clip_limit(self):
        # Gmail 超过 102KB 会把邮件截断并显示「查看全部内容」。
        self.assertLess(len(R.render_html(self.build()).encode()), 60_000)

    def test_subject_carries_the_numbers(self):
        rep = self.build()
        subject = rep.subject("[pve-power]")
        self.assertTrue(subject.startswith("[pve-power]"))
        self.assertIn("kWh", subject)
        self.assertIn("CNY", subject)


# ---------------------------------------------------------------- 发信


class FakeSMTPServer(threading.Thread):
    """够 smtplib 走完一次投递的最小 SMTP 服务器。

    不用 smtpd 模块：它在 3.12 里已经被移除了，而这个项目要在主机的
    Python 3.13 上跑。也不引入 aiosmtpd —— 目标机器没有 pip。
    """

    daemon = True

    def __init__(self, require_auth: bool = False, reject_rcpt: bool = False):
        super().__init__()
        self.require_auth = require_auth
        self.reject_rcpt = reject_rcpt
        self.sock = socket.socket()
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(1)
        self.port = self.sock.getsockname()[1]
        self.mail_from = ""
        self.rcpt_to: list[str] = []
        self.data = ""
        self.authenticated = False
        self.error: str = ""

    def run(self):
        try:
            self._serve()
        except Exception as exc:          # 让测试看见服务端的问题
            self.error = f"{type(exc).__name__}: {exc}"
        finally:
            self.sock.close()

    def _serve(self):
        conn, _ = self.sock.accept()
        conn.settimeout(10)
        fh = conn.makefile("rwb")

        def reply(line: str):
            fh.write(line.encode() + b"\r\n")
            fh.flush()

        reply("220 localhost ESMTP fake")
        while True:
            raw = fh.readline()
            if not raw:
                break
            line = raw.decode("utf-8", "replace").strip()
            upper = line.upper()
            if upper.startswith("EHLO"):
                reply("250-localhost")
                reply("250-AUTH PLAIN LOGIN")
                reply("250 HELP")
            elif upper.startswith("HELO"):
                reply("250 localhost")
            elif upper.startswith("AUTH"):
                # 这个假服务器不校验凭据，只记录客户端确实认证过了。
                if len(line.split()) > 2:
                    self.authenticated = True
                    reply("235 ok")
                else:
                    reply("334 ")
                    fh.readline()
                    self.authenticated = True
                    reply("235 ok")
            elif upper.startswith("MAIL FROM:"):
                if self.require_auth and not self.authenticated:
                    reply("530 authentication required")
                    continue
                self.mail_from = line[10:].strip().strip("<>")
                reply("250 ok")
            elif upper.startswith("RCPT TO:"):
                if self.reject_rcpt:
                    reply("550 no such user")
                    continue
                self.rcpt_to.append(line[8:].strip().strip("<>"))
                reply("250 ok")
            elif upper == "DATA":
                reply("354 end with .")
                chunks = []
                while True:
                    part = fh.readline()
                    if not part or part.strip() == b".":
                        break
                    chunks.append(part)
                self.data = b"".join(chunks).decode("utf-8", "replace")
                reply("250 queued")
            elif upper == "QUIT":
                reply("221 bye")
                break
            elif upper == "RSET":
                reply("250 ok")
            else:
                reply("250 ok")
        fh.close()
        conn.close()

    @property
    def message(self):
        """把收到的 DATA 解析回邮件对象。"""
        return email.message_from_string(self.data, policy=policy.default)


def local_smtp(port: int) -> SmtpConfig:
    return SmtpConfig(host="127.0.0.1", port=port, security="none",
                      sender="pve@example.com", sender_name="pve-power",
                      timeout=10)


class TestBuildMessage(unittest.TestCase):
    def test_plain_part_comes_before_html(self):
        """RFC 2046：客户端取它能显示的最后一个部分。顺序写反，
        纯文本客户端会显示一屏 HTML 标签。"""
        msg = mailer.build_message(
            local_smtp(0), ["a@example.com"], "主题", "纯文本", "<p>html</p>")
        subtypes = [p.get_content_subtype() for p in msg.iter_parts()]
        self.assertEqual(subtypes, ["plain", "html"])

    def test_chinese_subject_is_encoded_and_round_trips(self):
        subject = "[pve-power] 服务器 09-16 用电日报 — 4.56 kWh"
        msg = mailer.build_message(
            local_smtp(0), ["a@example.com"], subject, "正文")
        # 线路上必须是 ASCII（RFC 2047 编码字），收端能还原成原文。
        raw = msg.as_string()
        self.assertTrue(raw.isascii())
        parsed = email.message_from_string(raw, policy=policy.default)
        self.assertEqual(parsed["Subject"], subject)

    def test_wire_bytes_are_7bit_clean(self):
        """线路上必须是纯 ASCII。

        EmailMessage 默认的 policy.default 里 cte_type 是 "8bit"，中文正文
        会以裸 UTF-8 字节加 CTE: 8bit 发出去 —— 只有宣告了 8BITMIME 的
        服务器才允许这样。本地 as_string() 看不出这个差别（那条路径按
        7bit 走），所以这里是对着 BytesGenerator 的输出断言的，
        和 smtplib 真正发出去的字节是同一条路。
        """
        import email.generator
        import io

        msg = mailer.build_message(
            local_smtp(0), ["a@example.com"], "中文主题", "中文正文",
            "<p>中文 HTML</p>")
        buf = io.BytesIO()
        email.generator.BytesGenerator(buf).flatten(msg, linesep="\r\n")
        raw = buf.getvalue()
        self.assertTrue(raw.isascii(), "线路上出现了非 ASCII 字节")
        self.assertNotIn(b"Content-Transfer-Encoding: 8bit", raw)

    def test_headers_that_keep_it_out_of_the_spam_folder(self):
        msg = mailer.build_message(
            local_smtp(0), ["a@example.com"], "s", "t")
        self.assertIn("@", msg["Message-ID"])
        self.assertEqual(msg["Auto-Submitted"], "auto-generated")
        self.assertTrue(msg["Date"])


class TestSend(unittest.TestCase):
    def test_end_to_end_through_a_real_socket(self):
        server = FakeSMTPServer()
        server.start()
        smtp = local_smtp(server.port)
        msg = mailer.build_message(
            smtp, ["ops@example.com", "boss@example.com"],
            "服务器 09-16 用电日报", "纯文本正文", "<p>HTML 正文</p>")
        mailer.send(smtp, msg, ["ops@example.com", "boss@example.com"])
        server.join(10)

        self.assertEqual(server.error, "")
        self.assertEqual(server.mail_from, "pve@example.com")
        self.assertEqual(server.rcpt_to, ["ops@example.com", "boss@example.com"])
        received = server.message
        self.assertEqual(received["Subject"], "服务器 09-16 用电日报")
        body = received.get_body(preferencelist=("plain",)).get_content()
        self.assertIn("纯文本正文", body)
        html = received.get_body(preferencelist=("html",)).get_content()
        self.assertIn("HTML 正文", html)

    def test_login_happens_when_credentials_are_set(self):
        server = FakeSMTPServer(require_auth=True)
        server.start()
        smtp = local_smtp(server.port)
        smtp.user = "pve@example.com"
        smtp.password = "secret"
        msg = mailer.build_message(smtp, ["ops@example.com"], "s", "t")
        mailer.send(smtp, msg, ["ops@example.com"])
        server.join(10)
        self.assertTrue(server.authenticated)

    def test_rejected_recipients_fail_immediately(self):
        """被拒收重试也不会变好，而且反复认证会招来服务商的临时封禁。"""
        server = FakeSMTPServer(reject_rcpt=True)
        server.start()
        smtp = local_smtp(server.port)
        msg = mailer.build_message(smtp, ["nobody@example.com"], "s", "t")
        slept: list[float] = []
        with self.assertRaises(mailer.MailError) as ctx:
            mailer.send(smtp, msg, ["nobody@example.com"],
                        sleep=slept.append)
        self.assertEqual(slept, [])
        self.assertIn("拒收", str(ctx.exception))
        server.join(10)

    def test_transient_failure_is_retried(self):
        attempts = []

        def flaky(_smtp):
            attempts.append(1)
            if len(attempts) < 3:
                raise TimeoutError("连接超时")
            return _Recorder()

        smtp = local_smtp(2525)
        msg = mailer.build_message(smtp, ["ops@example.com"], "s", "t")
        slept: list[float] = []
        mailer.send(smtp, msg, ["ops@example.com"],
                    connect=flaky, sleep=slept.append)
        self.assertEqual(len(attempts), 3)
        self.assertEqual(slept, list(mailer.RETRY_DELAYS))

    def test_gives_up_after_the_last_retry(self):
        def always_fails(_smtp):
            raise TimeoutError("连接超时")

        smtp = local_smtp(2525)
        msg = mailer.build_message(smtp, ["ops@example.com"], "s", "t")
        with self.assertRaises(mailer.MailError) as ctx:
            mailer.send(smtp, msg, ["ops@example.com"],
                        connect=always_fails, sleep=lambda _: None)
        self.assertIn("重试", str(ctx.exception))

    def test_password_never_appears_in_an_error(self):
        """错误文本会原样进 journal。"""
        smtp = local_smtp(2525)
        smtp.user = "pve@example.com"
        smtp.password = "hunter2-very-secret"

        def auth_fails(_smtp):
            raise smtplib.SMTPAuthenticationError(535, b"bad password")

        msg = mailer.build_message(smtp, ["ops@example.com"], "s", "t")
        with self.assertRaises(mailer.MailError) as ctx:
            mailer.send(smtp, msg, ["ops@example.com"],
                        connect=auth_fails, sleep=lambda _: None)
        self.assertNotIn("hunter2", str(ctx.exception))
        # 但要给出可操作的提示。
        self.assertIn("授权码", str(ctx.exception))


class _Recorder:
    """test_transient_failure_is_retried 用的最小连接替身。"""

    def login(self, *_):
        pass

    def send_message(self, *_, **__):
        pass

    def quit(self):
        pass


# ---------------------------------------------------------------- 配置


class TestMailConfig(unittest.TestCase):
    def test_ports_follow_the_security_mode(self):
        self.assertEqual(SmtpConfig(security="starttls").effective_port(), 587)
        self.assertEqual(SmtpConfig(security="ssl").effective_port(), 465)
        self.assertEqual(SmtpConfig(security="none").effective_port(), 25)
        self.assertEqual(SmtpConfig(security="ssl", port=2465).effective_port(),
                         2465)

    def test_env_var_overrides_the_stored_password(self):
        smtp = SmtpConfig(password="from-file")
        os.environ["PVE_POWER_SMTP_PASSWORD"] = "from-env"
        self.addCleanup(os.environ.pop, "PVE_POWER_SMTP_PASSWORD", None)
        self.assertEqual(smtp.effective_password(), "from-env")

    def test_password_over_an_unencrypted_connection_is_rejected(self):
        cfg = Config()
        cfg.smtp = SmtpConfig(host="mail.example.com", security="none",
                              user="a@example.com", password="secret")
        self.assertTrue(any("明文" in p for p in cfg.validate_mail()))

    def test_enabling_the_report_without_configuring_it_is_caught(self):
        cfg = Config()
        cfg.report.enabled = True
        problems = cfg.validate_mail()
        self.assertTrue(any("smtp.host" in p for p in problems))
        self.assertTrue(any("recipients" in p for p in problems))

    def test_a_disabled_report_does_not_block_startup(self):
        """没开日报的人不该被日报的配置拦住。"""
        self.assertEqual(Config().validate(), [])

    def test_recipients_accept_a_comma_separated_string(self):
        cfg = Config.from_dict(
            {"report": {"recipients": "a@example.com, b@example.com"}})
        self.assertEqual(cfg.report.recipients,
                         ["a@example.com", "b@example.com"])

    def test_config_round_trips_through_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "config.json")
            cfg = Config()
            cfg.smtp = SmtpConfig(host="smtp.qq.com", user="a@qq.com",
                                  password="code", security="ssl")
            cfg.report.enabled = True
            cfg.report.recipients = ["ops@example.com"]
            cfg.save(path)
            # 里面有 SMTP 密码。
            self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)
            back = Config.load(path)
            self.assertEqual(back.smtp.host, "smtp.qq.com")
            self.assertEqual(back.smtp.security, "ssl")
            self.assertEqual(back.report.recipients, ["ops@example.com"])
            self.assertTrue(back.report.enabled)


if __name__ == "__main__":
    unittest.main()
