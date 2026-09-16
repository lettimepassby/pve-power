"""End-to-end TUI smoke test.

Every view is rendered in a real pty across a range of terminal sizes,
with a stubbed BMC, and driven through its keys. curses failures are
nearly always geometry errors that surface only at a particular size, so
the sizes include the narrowest the app claims to support and one below
its minimum.

The child runs via `exec` rather than a bare fork: forking a process
that has already initialised curses and sqlite is not safe, and doing so
produced crashes that had nothing to do with the code under test.
"""

from __future__ import annotations

import datetime as dt
import os
import sys
import tempfile
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

from _tui_child import build_tariff  # noqa: E402
from pvepower.storage import Storage  # noqa: E402

CHILD = os.path.join(HERE, "_tui_child.py")

SIZES = [
    (24, 80),    # the classic default
    (30, 120),   # a comfortable console
    (60, 200),   # wide
    (24, 63),    # one column above the declared minimum
    (13, 62),    # one row above the declared minimum, at exactly the minimum width
    (10, 40),    # below the minimum: must show the size warning, not crash
]


def populate(db_path: str, days: int = 3) -> None:
    """Write a few days of realistic samples, including a coverage gap."""
    tariff = build_tariff()
    storage = Storage(db_path)
    now = dt.datetime.now().replace(minute=0, second=0, microsecond=0)
    when = now - dt.timedelta(days=days)
    step = dt.timedelta(minutes=5)
    i = 0
    while when <= now:
        # Skip a two-hour stretch on the first day so gap handling is
        # exercised in the rendered output too.
        in_outage = (
            when < now - dt.timedelta(days=days - 1)
            and 3 <= when.hour < 5
        )
        if not in_outage:
            watts = 150.0 + (60.0 if 8 <= when.hour <= 22 else 0.0) + (i % 7) * 3
            storage.record(watts, tariff, when=when, max_gap_seconds=900)
        when += step
        i += 1
    storage.close()


def run_child(
    rows: int, cols: int, db_path: str, failing: bool, timeout: float = 90.0
) -> tuple[int, str]:
    """Run the TUI child against a pty of the given size.

    ESC is fed in continuously: several hotkeys open a modal that blocks
    on getch, so without input the child would sit there forever. Feeding
    ESC means each dialog opens and is then cancelled, and a genuine hang
    still trips the timeout rather than stalling the suite.
    """
    import errno
    import fcntl
    import select
    import signal
    import struct
    import termios

    pid, fd = os.forkpty()
    if pid == 0:
        try:
            fcntl.ioctl(
                sys.stdout.fileno(), termios.TIOCSWINSZ,
                struct.pack("HHHH", rows, cols, 0, 0),
            )
            env = dict(os.environ)
            env["TERM"] = "xterm-256color"
            env["LINES"] = str(rows)
            env["COLUMNS"] = str(cols)
            os.execve(
                sys.executable,
                [sys.executable, CHILD, str(rows), str(cols), db_path,
                 "1" if failing else "0"],
                env,
            )
        except BaseException:
            os._exit(3)
        os._exit(3)

    chunks: list[bytes] = []
    deadline = time.time() + timeout
    timed_out = False
    last_write = 0.0
    try:
        while True:
            if time.time() > deadline:
                timed_out = True
                os.kill(pid, signal.SIGKILL)
                break
            readable, writable, _ = select.select([fd], [fd], [], 0.2)
            if readable:
                try:
                    data = os.read(fd, 65536)
                except OSError as exc:
                    if exc.errno in (errno.EIO, errno.EBADF):
                        break
                    raise
                if not data:
                    break
                chunks.append(data)
            if writable and time.time() - last_write > 0.05:
                try:
                    os.write(fd, b"\x1b")
                except OSError:
                    break
                last_write = time.time()
    finally:
        try:
            os.close(fd)
        except OSError:
            pass
    _, status = os.waitpid(pid, 0)
    code = os.waitstatus_to_exitcode(status)
    output = b"".join(chunks).decode("utf-8", "replace")
    if timed_out:
        note = (
            f"\n\n*** child did not exit within {timeout}s "
            "— a modal is probably blocking on input ***"
        )
        return 124, output + note
    return code, output


class TestTuiRendering(unittest.TestCase):
    def _check(self, rows: int, cols: int, failing: bool, populated: bool) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = os.path.join(tmp, "power.db")
            if populated:
                populate(db)
            else:
                Storage(db).close()
            code, output = run_child(rows, cols, db, failing)
            self.assertEqual(
                code, 0,
                f"TUI child exited {code} at {cols}x{rows} "
                f"(failing={failing}, populated={populated}):\n"
                + _tail(output),
            )

    def test_renders_with_data(self):
        for rows, cols in SIZES:
            with self.subTest(size=f"{cols}x{rows}"):
                self._check(rows, cols, failing=False, populated=True)

    def test_renders_with_empty_database(self):
        for rows, cols in SIZES[:4]:
            with self.subTest(size=f"{cols}x{rows}"):
                self._check(rows, cols, failing=False, populated=False)

    def test_renders_when_bmc_unreachable(self):
        # Every BMC call raises. The interface must stay up and show the
        # problem rather than dropping the operator back to a shell.
        for rows, cols in SIZES[:4]:
            with self.subTest(size=f"{cols}x{rows}"):
                self._check(rows, cols, failing=True, populated=True)


def _tail(text: str, limit: int = 3000) -> str:
    # Strip escape sequences so a failure is readable in test output.
    import re

    clean = re.sub(r"\x1b\[[0-9;?]*[A-Za-z]", "", text)
    clean = re.sub(r"\x1b[()][A-Z0-9]", "", clean)
    return clean[-limit:]


if __name__ == "__main__":
    unittest.main(verbosity=2)
