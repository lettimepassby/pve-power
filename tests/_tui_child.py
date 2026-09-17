"""Child process for the TUI smoke test.

Run as a standalone program against a pty, so the terminal is real and
nothing is inherited across a fork. Arguments:

    _tui_child.py <rows> <cols> <db-path> <failing> <populated>

Exits 0 if every view drew and handled keys without raising; on failure,
prints a traceback and exits non-zero.
"""

from __future__ import annotations

import curses
import os
import sys
import traceback

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pvepower.config import Config, TariffConfig, TouPeriod  # noqa: E402

# Keys pressed against every view: navigation, filters, and the keys that
# open modal dialogs. The parent feeds ESC into the pty, so each modal
# opens and is then cancelled, which exercises the dialog code paths
# without the test needing to script every prompt.
KEYS = [
    curses.KEY_DOWN, curses.KEY_DOWN, curses.KEY_NPAGE, curses.KEY_END,
    curses.KEY_HOME, curses.KEY_UP, curses.KEY_PPAGE,
    ord("g"), ord("G"), ord("j"), ord("k"),
    curses.KEY_LEFT, curses.KEY_RIGHT,
    ord("f"), ord("F"), ord("o"), ord("m"), ord("["), ord("]"),
    ord("d"), ord("h"),
    10,                      # Enter: drill down, or open an editor
    ord("c"), ord("p"), ord("i"),       # BMC actions
    ord("n"), ord("v"), ord("e"),       # user actions / export
    ord("a"), ord("x"), ord("s"), ord("P"), ord("R"), ord("X"),
]


def build_tariff() -> TariffConfig:
    return TariffConfig(
        mode="tou",
        currency="CNY",
        flat_price=0.6,
        tou_periods=[
            TouPeriod("峰", 1.0, list(range(8, 12)) + list(range(18, 22))),
            TouPeriod("平", 0.65, [7, 12, 13, 14, 15, 16, 17, 22, 23]),
            TouPeriod("谷", 0.35, list(range(0, 7))),
        ],
    )


def main() -> int:
    rows, cols = int(sys.argv[1]), int(sys.argv[2])
    db_path = sys.argv[3]
    failing = sys.argv[4] == "1"

    os.environ.setdefault("TERM", "xterm-256color")
    os.environ["LINES"] = str(rows)
    os.environ["COLUMNS"] = str(cols)

    from fake_ipmi import FakeIpmi  # noqa: E402
    from pvepower.tui.app import App  # noqa: E402

    config = Config()
    config.db_path = db_path
    config.tariff = build_tariff()

    app = App(config, db_path + ".config.json")
    app.ipmi = FakeIpmi(failing=failing)
    app.data.ipmi = app.ipmi

    # Fill the cache synchronously: a draw must never depend on a
    # background refresh having landed.
    for kind in ("power", "sensors", "chassis", "identity", "lan", "users",
                 "sel", "fan_control"):
        value = app.data._fetch(kind)
        if value is not None:
            app.data._values[kind] = value
    app.data.refresh_energy()

    failures: list[str] = []

    def body(stdscr):
        from pvepower.tui.views.bmc import BmcView
        from pvepower.tui.views.energy import EnergyView
        from pvepower.tui.views.fans import FansView
        from pvepower.tui.views.overview import OverviewView
        from pvepower.tui.views.sel import SelView
        from pvepower.tui.views.sensors import SensorsView
        from pvepower.tui.views.tariff import TariffView
        from pvepower.tui.views.users import UsersView
        from pvepower.tui.widgets import init_colors

        curses.curs_set(0)
        init_colors()
        app.stdscr = stdscr
        app.views = [
            OverviewView(app), EnergyView(app), SensorsView(app), FansView(app),
            BmcView(app), UsersView(app), SelView(app), TariffView(app),
        ]
        for idx, view in enumerate(app.views):
            app.active = idx
            try:
                app._draw()
            except Exception:
                failures.append(f"{view.title}: initial draw\n{traceback.format_exc()}")
                continue
            for key in KEYS:
                try:
                    view.handle_key(key)
                    app._draw()
                except Exception:
                    failures.append(
                        f"{view.title}: key {key}\n{traceback.format_exc()}"
                    )
                    break

    curses.wrapper(body)

    if failures:
        sys.stderr.write("\n\n".join(failures))
        return 1
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SystemExit:
        raise
    except BaseException:
        traceback.print_exc()
        raise SystemExit(2)
