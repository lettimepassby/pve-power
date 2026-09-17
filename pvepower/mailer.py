"""SMTP 发信。

只做一件事：把一封已经渲染好的邮件送到服务器。报告内容在 report.py。

几个刻意的决定：

  * **证书默认验证**。`ssl.create_default_context()` 会校验证书链和主机名。
    没有开关可以关掉它 —— 一个每天自动发信的任务如果对中间人毫无察觉，
    那 SMTP 密码就等于公开了。自签证书的内网 MTA 请把 CA 装进系统信任库。
  * **失败会重试**。日报一天只有一次机会，而 SMTP 超时、连接被重置这类
    抖动非常常见。三次尝试，间隔递增；认证失败和被服务器拒收这类不会
    因为重试而变好的错误则立刻放弃。
  * **不打印密码**。异常信息会原样进日志和 systemd journal，所以这里
    自己组装错误文本，只带主机、端口和错误类型。
"""

from __future__ import annotations

import smtplib
import ssl
import time
from email import policy
from email.message import EmailMessage
from email.utils import formataddr, formatdate, make_msgid
from typing import Callable, Optional

from .config import SmtpConfig

# 重试之间的等待秒数。第一次失败后等 2 秒，第二次等 8 秒 —— 覆盖常见的
# 瞬时抖动，又不会让 systemd 的 timer 任务挂太久。
RETRY_DELAYS = (2, 8)


class MailError(Exception):
    """发信失败。消息里不含密码，可以直接进日志。"""


# EmailMessage 默认用 policy.default，而它的 cte_type 是 "8bit"：中文正文
# 会以裸 UTF-8 字节加 "Content-Transfer-Encoding: 8bit" 发出去。只有服务器
# 宣告了 8BITMIME 扩展才允许这样，否则是协议违规 —— 宽容的服务器照收，
# 严格的会拒收，中间的会把正文弄乱。
#
# 改成 7bit 后 set_content 会自动挑 base64 或 quoted-printable，任何 SMTP
# 服务器都能正确转发。这个差别在本地 msg.as_string() 上看不出来（那条路径
# 本来就按 7bit 走），只有真的连一次 socket 才暴露，所以
# tests/test_report.py 里那个断言是对着线路上的字节做的。
SMTP_POLICY = policy.default.clone(cte_type="7bit")


def build_message(
    smtp: SmtpConfig,
    recipients: list[str],
    subject: str,
    text: str,
    html: str = "",
) -> EmailMessage:
    """组装 multipart/alternative 邮件。

    纯文本在前、HTML 在后，这是 RFC 2046 要求的顺序（客户端取它能显示的
    最后一个），写反了会让纯文本客户端显示一堆标签。

    中文主题和发件人显示名的编码由 EmailMessage 自己处理（RFC 2047），
    不需要手动 Header(...)。
    """
    msg = EmailMessage(policy=SMTP_POLICY)
    msg["Subject"] = subject
    sender = smtp.effective_sender()
    msg["From"] = (formataddr((smtp.sender_name, sender))
                   if smtp.sender_name else sender)
    msg["To"] = ", ".join(recipients)
    msg["Date"] = formatdate(localtime=True)
    # 有些服务商（QQ、163）对没有 Message-ID 的信更容易判成垃圾邮件。
    msg["Message-ID"] = make_msgid(domain=sender.split("@")[-1] or None)
    msg["Auto-Submitted"] = "auto-generated"   # RFC 3834：别对它自动回复

    msg.set_content(text, subtype="plain", charset="utf-8")
    if html:
        msg.add_alternative(html, subtype="html", charset="utf-8")
    return msg


def _connect(smtp: SmtpConfig) -> smtplib.SMTP:
    """按 security 建立连接。调用方负责 quit()。"""
    host, port = smtp.host, smtp.effective_port()
    if smtp.security == "ssl":
        context = ssl.create_default_context()
        return smtplib.SMTP_SSL(host, port, timeout=smtp.timeout,
                                context=context)
    conn = smtplib.SMTP(host, port, timeout=smtp.timeout)
    if smtp.security == "starttls":
        conn.ehlo()
        conn.starttls(context=ssl.create_default_context())
        conn.ehlo()
    return conn


def _describe(exc: BaseException, smtp: SmtpConfig) -> str:
    """错误文本。带上足够定位的信息，但不带密码。"""
    where = f"{smtp.host}:{smtp.effective_port()}（{smtp.security}）"
    if isinstance(exc, smtplib.SMTPAuthenticationError):
        return (f"{where} 认证被拒：{exc.smtp_code} "
                f"{_decode(exc.smtp_error)}。"
                "QQ / 163 这类邮箱要用「授权码」而不是登录密码。")
    if isinstance(exc, smtplib.SMTPRecipientsRefused):
        addrs = "、".join(exc.recipients)
        return f"{where} 拒收了全部收件人：{addrs}"
    if isinstance(exc, smtplib.SMTPSenderRefused):
        return (f"{where} 拒绝了发件地址 {exc.sender}："
                f"{exc.smtp_code} {_decode(exc.smtp_error)}。"
                "多数服务商要求发件地址和登录账号一致。")
    if isinstance(exc, ssl.SSLError):
        return (f"{where} TLS 握手失败：{exc}。"
                "端口和 smtp.security 可能对不上（465 用 ssl，587 用 starttls）。")
    if isinstance(exc, (TimeoutError, OSError)):
        return f"{where} 连接失败：{type(exc).__name__}: {exc}"
    return f"{where} 发送失败：{type(exc).__name__}: {exc}"


def _decode(value) -> str:
    return value.decode("utf-8", "replace") if isinstance(value, bytes) else str(value)


# 这些错误重试也不会变好：账号密码不对、地址被拒。立刻放弃，
# 免得对着服务器连试三次认证 —— 有些服务商会因此临时封禁 IP。
_FATAL = (
    smtplib.SMTPAuthenticationError,
    smtplib.SMTPRecipientsRefused,
    smtplib.SMTPSenderRefused,
)


def send(
    smtp: SmtpConfig,
    message: EmailMessage,
    recipients: list[str],
    connect: Optional[Callable[[SmtpConfig], smtplib.SMTP]] = None,
    sleep: Callable[[float], None] = time.sleep,
) -> None:
    """发送。失败抛 MailError。

    `connect` 和 `sleep` 可以注入，测试就不用真的起服务器或者真的等待。
    """
    if not smtp.host:
        raise MailError("没有配置 smtp.host")
    if not recipients:
        raise MailError("没有收件人")
    connect = connect or _connect

    last = ""
    attempts = len(RETRY_DELAYS) + 1
    for attempt in range(attempts):
        conn = None
        try:
            conn = connect(smtp)
            password = smtp.effective_password()
            if smtp.user and password:
                conn.login(smtp.user, password)
            conn.send_message(message, from_addr=smtp.effective_sender(),
                              to_addrs=recipients)
            return
        except _FATAL as exc:
            raise MailError(_describe(exc, smtp)) from exc
        except Exception as exc:
            last = _describe(exc, smtp)
            if attempt < attempts - 1:
                sleep(RETRY_DELAYS[attempt])
        finally:
            if conn is not None:
                try:
                    conn.quit()
                except Exception:
                    # 连接已经断了的话 quit() 也会炸，但这时候信要么已经
                    # 发出去要么已经记下失败原因，再抛一次只会盖掉真正的错。
                    pass
    raise MailError(f"重试 {attempts} 次后仍然失败 —— {last}")
