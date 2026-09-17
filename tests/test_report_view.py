"""日报设置页。

这一页的风险集中在两处，都不是画面问题：

  * 密码。它已经在 0600 的配置文件里了，再让它出现在屏幕上就白费了 ——
    身后站个人就看见。所以有断言盯着「任何显示路径都不得出现原文」。
  * 试发。它走的是和定时器完全相同的组装与投递路径（否则「试发成功但
    定时发送失败」就成了可能），但绝不能写 last_report_sent —— 那个标记
    是给「今天已经发过」用的，试发把它置上会让当天真正的那封被跳过。

发信同样不打桩，用 test_report.py 里那个真的本地 SMTP 服务器。
"""

from __future__ import annotations

import datetime as dt
import importlib.util
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pvepower.config import Config, TariffConfig
from pvepower.storage import Storage
from pvepower.tui.views import report as report_view
from pvepower.tui.views.report import PRESETS, ReportView, parse_recipients

# 复用 test_report.py 里的本地 SMTP 服务器，不再抄一份。
_spec = importlib.util.spec_from_file_location(
    "_tr", os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "test_report.py"))
_tr = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_tr)
FakeSMTPServer = _tr.FakeSMTPServer

SECRET = "hunter2-authcode"


class FakeIpmi:
    def fru(self):
        return {"Product Name": "SA5112M4"}

    def sensors(self):
        return []

    def sel_entries(self, limit=200):
        return []


class FakeData:
    def __init__(self, storage, status=None):
        self.storage = storage
        self._status = status or {}

    @property
    def report_status(self):
        return self._status

    def invalidate(self, kind):
        pass


class FakeApp:
    def __init__(self, config, storage, status=None):
        self.config = config
        self.storage = storage
        self.ipmi = FakeIpmi()
        self.data = FakeData(storage, status)
        self.stdscr = None
        self.flashes: list[str] = []
        self.saved = False

    def content_size(self):
        return 34, 118

    def flash(self, message, ok=True, seconds=4.0):
        self.flashes.append(message)

    def save_config(self):
        self.saved = True


class ViewFixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.config = Config()
        self.config.db_path = os.path.join(self.tmp.name, "power.db")
        self.config.tariff = TariffConfig(mode="flat", currency="CNY",
                                          flat_price=0.98)
        self.storage = Storage(self.config.db_path)
        self.addCleanup(self.storage.close)

        # 一天多的采样，报告才有内容。
        t = dt.datetime.combine(dt.date.today() - dt.timedelta(days=1),
                                dt.time.min)
        now = dt.datetime.now()
        while t < now:
            self.storage.record(190.0, self.config.tariff, when=t,
                                max_gap_seconds=900)
            t += dt.timedelta(minutes=10)
        self.storage.commit()

        self.app = FakeApp(self.config, self.storage)
        self.view = ReportView(self.app)

    def configure_mail(self, port: int):
        m, r = self.config.smtp, self.config.report
        m.host, m.port, m.security = "127.0.0.1", port, "none"
        m.sender, m.sender_name = "pve@example.com", "pve-power"
        r.recipients = ["ops@example.com"]
        r.enabled = True


class TestPasswordIsNeverShown(ViewFixture):
    def test_stored_password_is_masked_in_the_list(self):
        self.config.smtp.password = SECRET
        shown = " ".join(str(v) for _, _, v in self.view._rows())
        self.assertNotIn(SECRET, shown)
        self.assertIn("已设置", shown)

    def test_absent_password_says_so(self):
        shown = dict((f, v) for f, _, v in self.view._rows())
        self.assertIn("未设置", shown["password"])

    def test_env_var_password_is_labelled_as_such(self):
        """来自环境变量时要说清楚，否则用户看到配置文件里是空的会以为没设。"""
        self.config.smtp.password = ""
        os.environ["PVE_POWER_SMTP_PASSWORD"] = SECRET
        self.addCleanup(os.environ.pop, "PVE_POWER_SMTP_PASSWORD", None)
        shown = dict((f, v) for f, _, v in self.view._rows())
        self.assertNotIn(SECRET, shown["password"])
        self.assertIn("环境变量", shown["password"])


class TestRecipients(unittest.TestCase):
    def test_full_width_punctuation_is_accepted(self):
        """中文输入法下逗号分号常打成全角。为一个「，」让人重输是没道理的。"""
        self.assertEqual(
            parse_recipients("a@example.com，b@example.com；c@example.com"),
            ["a@example.com", "b@example.com", "c@example.com"])

    def test_plain_comma_and_spaces(self):
        self.assertEqual(parse_recipients("a@x.com, b@x.com"),
                         ["a@x.com", "b@x.com"])

    def test_empty_input_yields_nothing(self):
        self.assertEqual(parse_recipients("  ,  ; "), [])

    def test_validator_rejects_a_non_address(self):
        self.assertIn("不是邮箱", report_view._validate_recipients("nobody"))
        self.assertEqual(report_view._validate_recipients("a@b.com"), "")


class TestProblems(ViewFixture):
    def test_a_blank_configuration_lists_what_is_missing(self):
        problems = self.view._problems()
        self.assertTrue(any("发信服务器" in p for p in problems))
        self.assertTrue(any("收件人" in p for p in problems))

    def test_problems_are_deduplicated(self):
        """validate_mail 和这一页的检查会说到同一件事，不该重复两遍。"""
        self.config.report.enabled = True
        problems = self.view._problems()
        self.assertEqual(len(problems), len(set(problems)))

    def test_a_complete_configuration_has_no_problems(self):
        self.configure_mail(port=2525)
        self.assertEqual(self.view._problems(), [])


class TestPresets(ViewFixture):
    def test_every_preset_is_internally_consistent(self):
        """端口和加密方式必须配套：465 走 ssl，587 走 starttls。
        写反了发出去就是 TLS 握手失败，而错误信息只会说「握手失败」。"""
        for name, host, port, security in PRESETS:
            with self.subTest(name=name):
                self.assertTrue(host)
                if security == "ssl":
                    self.assertEqual(port, 465)
                elif security == "starttls":
                    self.assertEqual(port, 587)
                else:
                    self.assertEqual((port, security), (25, "none"))

    def test_port_row_shows_the_derived_default_when_unset(self):
        self.config.smtp.port = 0
        self.config.smtp.security = "ssl"
        shown = dict((f, v) for f, _, v in self.view._rows())
        self.assertIn("465", shown["port"])


class TestTimerStatusText(unittest.TestCase):
    def test_missing_systemctl_is_not_an_error(self):
        self.assertEqual(report_view._usec_to_text(None), "")
        self.assertEqual(report_view._usec_to_text("0"), "")

    def test_future_time_is_rendered_with_a_rough_distance(self):
        soon = (dt.datetime.now() + dt.timedelta(hours=3)).timestamp() * 1e6
        text = report_view._usec_to_text(str(int(soon)))
        self.assertIn("小时后", text)


class TestPreview(ViewFixture):
    def test_preview_renders_the_real_report(self):
        """预览走的是真正的 report.build，不是另写的摘要 ——
        预览的意义就在于看到将要发出去的那份东西。"""
        captured = {}

        def fake_show_message(stdscr, title, body, is_error=False):
            captured["title"] = title
            captured["body"] = body
            captured["is_error"] = is_error

        original = report_view.show_message
        report_view.show_message = fake_show_message
        self.addCleanup(lambda: setattr(report_view, "show_message", original))

        self.view._preview()
        self.assertFalse(captured.get("is_error"), captured.get("body"))
        self.assertIn("用电日报", captured["body"])
        self.assertIn("kWh", captured["body"])
        # include_bmc 默认开，产品名应该来自 FakeIpmi。
        self.assertIn("SA5112M4", captured["title"])


class TestSendTest(ViewFixture):
    """试发走真实的 SMTP 路径。"""

    def setUp(self):
        super().setUp()
        self.shown = {}
        self.confirmed = True

        def fake_confirm(stdscr, question, danger=False):
            self.shown["question"] = question
            return self.confirmed

        def fake_show_message(stdscr, title, body, is_error=False):
            self.shown["title"] = title
            self.shown["body"] = body
            self.shown["is_error"] = is_error

        for name, fake in (("confirm", fake_confirm),
                           ("show_message", fake_show_message)):
            original = getattr(report_view, name)
            setattr(report_view, name, fake)
            self.addCleanup(
                lambda n=name, o=original: setattr(report_view, n, o))

    def test_it_actually_delivers(self):
        server = FakeSMTPServer()
        server.start()
        self.configure_mail(server.port)
        self.view._send_test()
        server.join(10)

        self.assertEqual(server.error, "")
        self.assertEqual(server.rcpt_to, ["ops@example.com"])
        self.assertFalse(self.shown.get("is_error"), self.shown.get("body"))
        self.assertIn("用电日报", server.message["Subject"])

    def test_it_does_not_mark_the_day_as_sent(self):
        """最要紧的一条：试发若写了 last_report_sent，当天真正的定时发送
        会被「今天已经发过了」跳过，用户按了一下试发反而收不到当天的日报。"""
        server = FakeSMTPServer()
        server.start()
        self.configure_mail(server.port)
        self.view._send_test()
        server.join(10)
        self.assertEqual(self.storage.get_meta("last_report_sent"), "")

    def test_declining_the_confirmation_sends_nothing(self):
        server = FakeSMTPServer()
        server.start()
        self.configure_mail(server.port)
        self.confirmed = False
        self.view._send_test()
        self.assertEqual(server.mail_from, "")
        server.sock.close()

    def test_an_incomplete_configuration_stops_before_connecting(self):
        # 没有 host，也没有收件人。
        self.view._send_test()
        self.assertTrue(self.shown["is_error"])
        self.assertIn("配置还不完整", self.shown["title"])
        self.assertNotIn("question", self.shown)

    def test_a_failure_is_reported_and_logged(self):
        self.configure_mail(port=1)          # 没人监听
        self.config.smtp.timeout = 2
        self.view._send_test()
        self.assertTrue(self.shown["is_error"])
        kinds = [row["kind"] for row in self.storage.recent_events(10)]
        self.assertIn("report_failed", kinds)

    def test_the_failure_message_never_leaks_the_password(self):
        self.configure_mail(port=1)
        self.config.smtp.user = "pve@example.com"
        self.config.smtp.password = SECRET
        self.config.smtp.timeout = 2
        self.view._send_test()
        self.assertNotIn(SECRET, self.shown["body"])
        # 事件日志也会被别人看到。
        details = " ".join(row["detail"] or ""
                           for row in self.storage.recent_events(10))
        self.assertNotIn(SECRET, details)


if __name__ == "__main__":
    unittest.main()
