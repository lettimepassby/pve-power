"""在真 pty 里跑一个弹窗，把结果打到 stderr 供父进程断言。

    _modal_child.py choose        —— 开一个三项菜单，输出选中的下标
    _modal_child.py message       —— 开一个长消息框，输出滚动到的行号

之所以要在真 pty 里跑：方向键在终端上是转义序列（ESC [ A），要靠 curses
的 keypad 模式翻译成 KEY_UP/KEY_DOWN。而 keypad 在 ncurses 里是**按窗口**
的属性，curses.newwin() 建出来的新窗口默认是关的。打桩 getch() 永远测不到
这件事——打桩直接喂 KEY_UP，而真实终端喂的是 ESC。
"""

from __future__ import annotations

import curses
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pvepower.tui.widgets import choose, init_colors, show_message  # noqa: E402


def main() -> int:
    what = sys.argv[1]

    def body(stdscr):
        curses.curs_set(0)
        init_colors()
        stdscr.keypad(True)
        if what == "choose":
            idx = choose(stdscr, "加密方式",
                         ["STARTTLS（587）", "SSL/TLS（465）", "不加密（25）"])
            sys.stderr.write(f"RESULT={idx}\n")
        else:
            body_text = "\n".join(f"第 {i} 行" for i in range(1, 61))
            show_message(stdscr, "长消息", body_text)
            sys.stderr.write("RESULT=closed\n")
        sys.stderr.flush()

    curses.wrapper(body)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
