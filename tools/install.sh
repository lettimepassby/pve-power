#!/bin/bash
# Install pve-power on a Proxmox VE host.
#
# Deliberately boring: copies the package to /opt, installs a launcher and
# a systemd unit, and leaves any existing configuration and database
# alone. Safe to re-run to upgrade.

set -euo pipefail

PREFIX="${PREFIX:-/opt/pve-power}"
CONFIG_DIR="${CONFIG_DIR:-/etc/pve-power}"
CONFIG="$CONFIG_DIR/config.json"
STATE_DIR="${STATE_DIR:-/var/lib/pve-power}"
BIN="/usr/local/bin/pve-power"
UNIT="/etc/systemd/system/pve-power-collector.service"
LEGACY_CRON_SCRIPT="/usr/local/bin/pve-power-log.sh"
SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

die() { echo "error: $*" >&2; exit 1; }
note() { echo "  $*"; }

[ "$(id -u)" -eq 0 ] || die "run as root"
command -v ipmitool >/dev/null || die "ipmitool is not installed (apt install ipmitool)"
python3 -c 'import curses, sqlite3' 2>/dev/null || die "python3 is missing curses or sqlite3"

echo "Installing pve-power"

# --- BMC reachability -------------------------------------------------
if ! ipmitool dcmi power reading >/dev/null 2>&1; then
    echo "warning: 'ipmitool dcmi power reading' failed." >&2
    echo "         The collector needs it to measure power draw." >&2
    echo "         Check that the ipmi_devintf and ipmi_si modules are loaded." >&2
fi

# --- files ------------------------------------------------------------
note "package    -> $PREFIX"
install -d -m 0755 "$PREFIX"
rm -rf "$PREFIX/pvepower"
cp -r "$SRC/pvepower" "$PREFIX/pvepower"
find "$PREFIX" -name '__pycache__' -type d -exec rm -rf {} + 2>/dev/null || true
# The unit's Documentation= points here.
install -m 0644 "$SRC/README.md" "$PREFIX/README.md"

install -d -m 0755 "$CONFIG_DIR"
install -d -m 0750 "$STATE_DIR"

note "launcher   -> $BIN"
cat > "$BIN" <<EOF
#!/bin/bash
# pve-power launcher
exec env PYTHONPATH="$PREFIX" python3 -m pvepower --config "$CONFIG" "\$@"
EOF
chmod 0755 "$BIN"

# --- configuration ----------------------------------------------------
if [ -f "$CONFIG" ]; then
    note "config     -> $CONFIG (kept)"
else
    note "config     -> $CONFIG (created)"
    PYTHONPATH="$PREFIX" python3 -m pvepower --config "$CONFIG" config --init >/dev/null
    chmod 0600 "$CONFIG"
fi

# --- import the old cron data ----------------------------------------
if [ -d /var/log/pve-power ] && compgen -G "/var/log/pve-power/*.csv" >/dev/null; then
    note "importing existing CSV history"
    PYTHONPATH="$PREFIX" python3 -m pvepower --config "$CONFIG" --force import \
        | sed 's/^/    /'
fi

# --- retire the cron job ---------------------------------------------
if crontab -l 2>/dev/null | grep -q 'pve-power-log.sh'; then
    echo
    echo "The old 5-minute cron job is still installed:"
    crontab -l 2>/dev/null | grep 'pve-power-log.sh' | sed 's/^/    /'
    echo "It only records instantaneous watts and cannot bill for energy."
    read -r -p "Remove it and let the collector service take over? [y/N] " reply
    if [[ "$reply" =~ ^[Yy]$ ]]; then
        crontab -l 2>/dev/null | grep -v 'pve-power-log.sh' | crontab -
        note "cron entry removed"
        if [ -f "$LEGACY_CRON_SCRIPT" ]; then
            mv "$LEGACY_CRON_SCRIPT" "$LEGACY_CRON_SCRIPT.replaced-by-pve-power"
            note "old script renamed to $(basename "$LEGACY_CRON_SCRIPT").replaced-by-pve-power"
        fi
    else
        echo "    Left in place. Both will write, but only the service bills."
    fi
fi

# --- service ----------------------------------------------------------
note "unit       -> $UNIT"
install -m 0644 "$SRC/etc/pve-power-collector.service" "$UNIT"
systemctl daemon-reload
systemctl enable --now pve-power-collector.service >/dev/null 2>&1 || \
    systemctl enable pve-power-collector.service

sleep 2
if systemctl is-active --quiet pve-power-collector.service; then
    note "collector is running"
else
    echo "warning: the collector did not start. Check:" >&2
    echo "         journalctl -u pve-power-collector -n 50" >&2
fi

cat <<EOF

Installed.

  pve-power              launch the interface
  pve-power status       one-shot health check
  pve-power report       consumption summary

Set your electricity price before trusting the cost figures: launch
'pve-power', go to the Tariff tab, and edit the rates to match your bill.
The default is a placeholder of 0.60 CNY/kWh.

Config:   $CONFIG
Database: $STATE_DIR/power.db
Service:  systemctl status pve-power-collector
EOF
