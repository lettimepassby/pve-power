"""弹窗的按键处理。

这些测试存在的原因是一个真实的 bug：菜单里按上下方向键会把菜单关掉。

keypad 模式在 ncurses 里是**按窗口**的属性，而不是全局的。app.py 给
stdscr 开了 keypad(True)，但 choose() 和 show_message() 用 curses.newwin()
另建了窗口，新窗口默认是关的。于是方向键不会被翻译成 KEY_UP/KEY_DOWN，
而是以原始转义序列 ESC [ A 逐字节到达 —— 第一个字节 27 正好被 choose()
当成「取消」，菜单当场关闭。

关键点是这件事**只有在真终端上才会发生**：打桩 getch() 直接喂
curses.KEY_UP，永远复现不了。所以这里在真 pty 里跑，写进去的是真正的
转义序列字节。
"""

from __future__ import annotations

import os
import pty
import select
import signal
import struct
import sys
import termios
import fcntl
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

CHILD = os.path.join(HERE, "_modal_child.py")

# 方向键在终端上真正发出的字节，有两套，取决于终端认不认 smkx：
#
#   应用模式 ESC O A/B —— curses 开 keypad 后会给终端发 smkx，绝大多数
#       终端据此切到这一种。ncurses 的 terminfo 里有对应项，能翻译。
#   普通模式 ESC [ A/B —— 有的终端不理会 smkx，照发这一种。terminfo 里
#       没有，ncurses 原样吐出三个字节，头一个 27 就是那个把菜单关掉的
#       罪魁祸首。
#
# 两套都要过，所以每个测试跑两遍。
APP_MODE = {"up": b"\x1bOA", "down": b"\x1bOB"}
NORMAL_MODE = {"up": b"\x1b[A", "down": b"\x1b[B"}
ARROW_MODES = [("应用模式 ESC O", APP_MODE), ("普通模式 ESC [", NORMAL_MODE)]

ENTER = b"\r"
ESC = b"\x1b"


def run_modal(kind: str, keys: list[bytes], rows: int = 24,
              cols: int = 80, timeout: float = 15.0) -> str:
    """在 pty 里跑一个弹窗，按顺序写入按键，返回子进程报告的 RESULT。"""
    err_r, err_w = os.pipe()
    pid, fd = pty.fork()
    if pid == 0:
        os.close(err_r)
        os.dup2(err_w, 2)
        os.close(err_w)
        os.environ["TERM"] = "xterm-256color"
        os.environ["LANG"] = "en_US.UTF-8"
        fcntl.ioctl(0, termios.TIOCSWINSZ,
                    struct.pack("HHHH", rows, cols, 0, 0))
        os.execvp(sys.executable, [sys.executable, CHILD, kind])
    os.close(err_w)
    fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))

    def drain(seconds: float) -> None:
        deadline = time.time() + seconds
        while time.time() < deadline:
            ready, _, _ = select.select([fd], [], [], 0.05)
            if ready:
                try:
                    os.read(fd, 65536)
                except OSError:
                    return

    drain(1.5)                      # 等 curses 初始化并画出弹窗
    for key in keys:
        try:
            os.write(fd, key)
        except OSError:
            # 子进程已经退出（弹窗关掉了），后面的键没人收。这本身就是
            # 一种结果，交给断言去解释，别在这里炸成 OSError 把真正的
            # 失败原因盖掉。
            break
        # 转义序列必须整串写入后留出处理时间。ncurses 用一个很短的超时
        # 来区分「单独按了 ESC」和「方向键的前缀」，写得太碎会被判成前者。
        drain(0.4)

    captured = b""
    deadline = time.time() + timeout
    while time.time() < deadline:
        ready, _, _ = select.select([err_r, fd], [], [], 0.2)
        if err_r in ready:
            chunk = os.read(err_r, 65536)
            if not chunk:
                break
            captured += chunk
            if b"RESULT=" in captured:
                break
        if fd in ready:
            try:
                os.read(fd, 65536)
            except OSError:
                break
    os.close(err_r)
    try:
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    os.waitpid(pid, 0)
    os.close(fd)

    text = captured.decode("utf-8", "replace")
    for line in text.splitlines():
        if line.startswith("RESULT="):
            return line.split("=", 1)[1].strip()
    return f"（子进程没有报告结果）{text[-400:]}"


class TestChooseArrowKeys(unittest.TestCase):
    """就是用户碰到的那个：加密方式菜单按上下键没反应。"""

    def test_down_arrow_moves_the_selection(self):
        for name, arrow in ARROW_MODES:
            with self.subTest(name):
                # 下、回车 → 选中第二项（SSL/TLS），而不是取消。
                self.assertEqual(
                    run_modal("choose", [arrow["down"], ENTER]), "1")

    def test_down_twice_reaches_the_third_option(self):
        for name, arrow in ARROW_MODES:
            with self.subTest(name):
                self.assertEqual(
                    run_modal("choose",
                              [arrow["down"], arrow["down"], ENTER]), "2")

    def test_up_arrow_wraps_to_the_last_option(self):
        for name, arrow in ARROW_MODES:
            with self.subTest(name):
                self.assertEqual(
                    run_modal("choose", [arrow["up"], ENTER]), "2")

    def test_arrows_do_not_cancel_the_menu(self):
        """bug 的核心症状：方向键的首字节是 ESC，被当成了取消。"""
        for name, arrow in ARROW_MODES:
            with self.subTest(name):
                self.assertNotEqual(
                    run_modal("choose", [arrow["down"], ENTER]), "None")

    def test_jk_still_work(self):
        """vim 键位不经过 keypad 翻译，之前就是好的，别改坏。"""
        self.assertEqual(run_modal("choose", [b"j", ENTER]), "1")
        self.assertEqual(run_modal("choose", [b"k", ENTER]), "2")

    def test_enter_on_the_first_option(self):
        self.assertEqual(run_modal("choose", [ENTER]), "0")

    def test_a_burst_of_escapes_does_not_wedge_the_menu(self):
        """连按 ESC 必须能关掉菜单。

        这条是我自己引入的 bug 的回归：修方向键时在 ESC 之后加了 50ms 的
        探测，而连着的第二个 ESC 会被探到 —— 它既不是 [ 也不是 O，当时
        返回 -1，于是菜单不关也不动，下一轮又读到它，永远出不来。
        冒烟测试每 50ms 灌一个 ESC，当场挂死。
        """
        self.assertEqual(run_modal("choose", [ESC, ESC, ESC]), "None")

    def test_escape_still_cancels(self):
        """ESC 本身仍然要能取消。

        这条和上面那条是一对：修方向键的办法是「看到 ESC 再探一下后面
        有没有字节」，探过头就会把取消键也一起弄没。
        """
        self.assertEqual(run_modal("choose", [ESC]), "None")


class TestShowMessageKeys(unittest.TestCase):
    def test_a_long_message_scrolls_and_closes(self):
        """show_message 用的是同一种新建窗口，滚动键受同一个问题影响。"""
        for name, arrow in ARROW_MODES:
            with self.subTest(name):
                self.assertEqual(
                    run_modal("message",
                              [arrow["down"], arrow["down"], b"q"]), "closed")


if __name__ == "__main__":
    unittest.main()
