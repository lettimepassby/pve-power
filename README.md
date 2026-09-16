# pve-power

Electricity metering and BMC management for a Proxmox VE host, driven
entirely through IPMI. It samples the power draw the server reports over
DCMI, turns those samples into kWh and money, and gives you a terminal
interface for reading and changing the BMC's configuration.

Pure Python standard library — no pip, no packages to install. It needs
`python3` (with `curses` and `sqlite3`, both of which Debian's build
includes) and `ipmitool`.

## Why it exists

The cron job this replaces looked like:

```bash
w=$(ipmitool dcmi power reading | awk '/Instantaneous/{print $4}')
echo "$(date '+%F %T'),$w" >> /var/log/pve-power/$(date +%F).csv
```

That records what the machine was drawing at each moment, which is a
useful log but not a bill. An instantaneous wattage says nothing about
energy on its own; you have to integrate it over time, decide what to do
about the minutes when nothing was recorded, and price each bit of energy
at whatever rate applied *when it was consumed*. This project does those
three things, and the old CSVs can be imported so the history is not lost.

## Install

From a checkout on the PVE host, as root:

```bash
./tools/install.sh
```

It copies the package to `/opt/pve-power`, installs `/usr/local/bin/pve-power`,
writes `/etc/pve-power/config.json` if there isn't one already, imports any
existing `/var/log/pve-power/*.csv`, offers to retire the old cron entry,
and starts the collector service. Re-running it upgrades the code and
leaves your configuration and database alone.

**Set your electricity price before trusting any cost figure.** The
default is a placeholder of 0.60 CNY/kWh. Launch `pve-power`, go to the
Tariff tab, and edit the rates to match your own bill.

## Use

```
pve-power                  launch the interface (default)
pve-power status           one-shot health check; --json for scripts
pve-power report           consumption summary; --days N or --month YYYY-MM
pve-power sample           take a single reading and store it
pve-power collect          run the sampling loop (this is what systemd runs)
pve-power import           import the legacy cron CSVs; --dir to override
pve-power config --init    write a default config; --preset flat|china-tou
```

`-c/--config` points at a different configuration file. `--force` runs
despite configuration problems that would otherwise stop the command.

### The interface

Tabs are `1`–`7` or Tab/Shift-Tab; `r` refreshes, `?` lists keys, `q` quits.
Arrows, PgUp/PgDn, Home/End and `j`/`k`/`g`/`G` move within a view.

| Tab | Shows | Keys |
| --- | --- | --- |
| Overview | live watts, today and this month, projected month cost, BMC and chassis health | `r` refresh |
| Energy | daily or hourly consumption with cost, as bars | `d`/`h` daily or hourly, `[` `]` range, Enter drill into a day, `e` export CSV |
| Sensors | every IPMI sensor with its headroom to the critical threshold | `f` filter by kind, `o` faults only |
| BMC | firmware and FRU identity, LAN configuration, chassis status | Enter edit a field, `c` chassis power, `p` power-restore policy, `i` identify LED |
| Users | BMC accounts, privilege, and which are vendor defaults | `p` password, `n` rename, `e`/`d` enable or disable, `v` privilege |
| SEL | the event log, triaged by severity | `o` problems only, `X` clear the log |
| Tariff | prices, billing mode, sampling settings | Enter edit, `m` switch flat/TOU, `a`/`x` add or remove a tier, `P` load a preset, `s` save, `R` reprice history |

Anything that changes the BMC asks first, and the genuinely dangerous
actions — a hard power-off, clearing the SEL — make you type `yes` in
full. Changing the BMC's IP address over a remote connection warns you
that you are about to cut the branch you are sitting on.

## How the numbers are produced

**Energy.** Each sample is integrated against the one before it with the
trapezoidal rule: `kWh = (W₁ + W₂)/2 × Δt / 3,600,000`. Multiplying a
single reading by the interval instead would bias the total by whichever
way the load happened to be moving — high while ramping up, low while
ramping down. Averaging the two readings that bracket the interval is
exact for a linear ramp and much closer for everything else.

**Gaps.** If more than `max_gap_seconds` passed since the previous sample
(default 900s), the interval is recorded with zero energy and flagged as a
gap. The collector was not running, so there is no honest basis for
claiming the machine drew anything; inventing consumption across an outage
would be worse than admitting the hole. The Energy tab shows coverage so
you can see how much of a period was actually measured.

**Money.** Cost is computed per sample, at the price in force at that
sample's timestamp, and stored alongside it. Editing your tariff next
month therefore cannot rewrite what last month cost. When you genuinely
want history recalculated — you entered the wrong price and want to fix
it — `R` on the Tariff tab reprices all samples, replaying month-to-date
totals so tiered pricing steps exactly as it did live. It never changes
recorded kWh, only what that energy is said to have cost.

**Averages.** Average watts over a period is `kWh × 3,600,000 / covered
seconds` — weighted by time, not by sample count, so a burst of closely
spaced samples doesn't drag the average toward itself.

### Tariffs

Three modes, and the tiers compose with the others:

- **flat** — one price per kWh.
- **tou** (分时电价) — a price per hour-of-day, optionally restricted to
  certain weekdays. Hours you don't cover fall back to the flat price
  rather than billing at zero; the Tariff tab shows a 24-hour strip
  marking anything uncovered.
- **tiered** (阶梯电价) — a surcharge added on top, stepping as the
  month's cumulative kWh crosses each threshold. The last tier must be
  unbounded.

`pve-power config --init --preset china-tou` writes a sample Chinese TOU
schedule (尖峰/高峰/平段/低谷). The prices in it are examples, not your
utility's.

## Remote BMCs

Leaving `ipmi.host` empty uses the local KCS interface via `/dev/ipmi0`,
which is what you want on the PVE host itself. Setting it makes every call
go out over the network with `-I lanplus`, so you can run the interface
from a workstation against a server's BMC. The password lives in the
config file, which is why it is written `0600`.

## Layout

```
pvepower/ipmi.py        every ipmitool invocation and its parsing
pvepower/storage.py     SQLite schema, integration, aggregation, repricing
pvepower/config.py      config model, tariff maths, validation
pvepower/collector.py   the sampling daemon and the legacy CSV importer
pvepower/cli.py         subcommands
pvepower/tui/           curses interface: app, data cache, widgets, views
etc/                    systemd unit
tools/install.sh        installer
tests/                  energy/tariff maths, and a pty-driven TUI smoke test
```

`/etc/pve-power/config.json` holds configuration, `/var/lib/pve-power/power.db`
the samples. The database runs in WAL mode so the interface can read while
the collector writes.

## Tests

```bash
python3 -m unittest tests.test_energy tests.test_tui
```

`test_energy` checks the integration and pricing against hand-computed
values — 100W for an hour is 0.1 kWh, a 100→200W ramp over an hour is
0.15 kWh, a valley kWh billed at 0.20 stays at 0.20 after peak samples
arrive. `test_tui` renders every view in a real pty at six terminal sizes,
including one below the declared minimum, and presses every key including
the ones that open dialogs.

## Things this does not do

DCMI power capping is not implemented: this BMC (Inspur SA5112M4,
firmware 4.12) answers `dcmi power get_limit` with error 80, so there was
nothing to build against. The code paths for it were left out rather than
shipped untested.
