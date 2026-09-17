#!/bin/bash
# 在 Proxmox VE 主机上安装 pve-power。
#
# 刻意做得很朴素：把程序复制到 /opt，装一个启动器和一个 systemd 单元，
# 不动已有的配置和数据库。重复执行就是升级，是安全的。

set -euo pipefail

PREFIX="${PREFIX:-/opt/pve-power}"
CONFIG_DIR="${CONFIG_DIR:-/etc/pve-power}"
CONFIG="$CONFIG_DIR/config.json"
STATE_DIR="${STATE_DIR:-/var/lib/pve-power}"
BIN="/usr/local/bin/pve-power"
UNIT="/etc/systemd/system/pve-power-collector.service"
REPORT_UNIT="/etc/systemd/system/pve-power-report.service"
REPORT_TIMER="/etc/systemd/system/pve-power-report.timer"
LEGACY_CRON_SCRIPT="/usr/local/bin/pve-power-log.sh"
SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

die() { echo "错误：$*" >&2; exit 1; }
note() { echo "  $*"; }

[ "$(id -u)" -eq 0 ] || die "请以 root 身份运行"
command -v ipmitool >/dev/null || die "未安装 ipmitool（执行 apt install ipmitool）"
python3 -c 'import curses, sqlite3' 2>/dev/null || die "python3 缺少 curses 或 sqlite3 模块"

echo "正在安装 pve-power"

# --- BMC reachability -------------------------------------------------
if ! ipmitool dcmi power reading >/dev/null 2>&1; then
    echo "警告：'ipmitool dcmi power reading' 执行失败。" >&2
    echo "      采集器需要它来测量功率。" >&2
    echo "      请检查 ipmi_devintf 和 ipmi_si 模块是否已加载。" >&2
fi

# --- files ------------------------------------------------------------
note "程序       -> $PREFIX"
install -d -m 0755 "$PREFIX"
rm -rf "$PREFIX/pvepower"
cp -r "$SRC/pvepower" "$PREFIX/pvepower"
find "$PREFIX" -name '__pycache__' -type d -exec rm -rf {} + 2>/dev/null || true
# The unit's Documentation= points here.
install -m 0644 "$SRC/README.md" "$PREFIX/README.md"

install -d -m 0755 "$CONFIG_DIR"
install -d -m 0750 "$STATE_DIR"

note "启动器     -> $BIN"
cat > "$BIN" <<EOF
#!/bin/bash
# pve-power launcher
exec env PYTHONPATH="$PREFIX" python3 -m pvepower --config "$CONFIG" "\$@"
EOF
chmod 0755 "$BIN"

# --- configuration ----------------------------------------------------
if [ -f "$CONFIG" ]; then
    note "配置文件   -> $CONFIG （已存在，保留）"
else
    note "配置文件   -> $CONFIG （新建）"
    PYTHONPATH="$PREFIX" python3 -m pvepower --config "$CONFIG" config --init >/dev/null
    chmod 0600 "$CONFIG"
fi

# --- import the old cron data ----------------------------------------
if [ -d /var/log/pve-power ] && compgen -G "/var/log/pve-power/*.csv" >/dev/null; then
    note "正在导入已有的 CSV 历史数据"
    PYTHONPATH="$PREFIX" python3 -m pvepower --config "$CONFIG" --force import \
        | sed 's/^/    /'
fi

# --- retire the cron job ---------------------------------------------
if crontab -l 2>/dev/null | grep -q 'pve-power-log.sh'; then
    echo
    echo "旧的 5 分钟 cron 任务仍然存在："
    crontab -l 2>/dev/null | grep 'pve-power-log.sh' | sed 's/^/    /'
    echo "它只记录瞬时功率，无法统计电量。"
    read -r -p "是否删除它，改由采集服务接手？[y/N] " reply
    if [[ "$reply" =~ ^[Yy]$ ]]; then
        crontab -l 2>/dev/null | grep -v 'pve-power-log.sh' | crontab -
        note "已删除 cron 任务"
        if [ -f "$LEGACY_CRON_SCRIPT" ]; then
            mv "$LEGACY_CRON_SCRIPT" "$LEGACY_CRON_SCRIPT.replaced-by-pve-power"
            note "旧脚本已重命名为 $(basename "$LEGACY_CRON_SCRIPT").replaced-by-pve-power"
        fi
    else
        echo "    已保留。两者都会写入，但只有服务能统计电费。"
    fi
fi

# --- service ----------------------------------------------------------
note "systemd 单元 -> $UNIT"
install -m 0644 "$SRC/etc/pve-power-collector.service" "$UNIT"
systemctl daemon-reload
systemctl enable --now pve-power-collector.service >/dev/null 2>&1 || \
    systemctl enable pve-power-collector.service

sleep 2
if systemctl is-active --quiet pve-power-collector.service; then
    note "采集器正在运行"
else
    echo "警告：采集器未能启动。请检查：" >&2
    echo "      journalctl -u pve-power-collector -n 50" >&2
fi

# --- daily report -----------------------------------------------------
# 单元文件总是装上，但定时器只在配置里真的开了日报时才启用 ——
# 没配 SMTP 的人不该每天收到一封发送失败的告警。
note "日报单元   -> $REPORT_TIMER"
install -m 0644 "$SRC/etc/pve-power-report.service" "$REPORT_UNIT"
install -m 0644 "$SRC/etc/pve-power-report.timer" "$REPORT_TIMER"
systemctl daemon-reload

REPORT_ENABLED="$(PYTHONPATH="$PREFIX" python3 - "$CONFIG" <<'PYEOF'
import json, sys
try:
    with open(sys.argv[1], encoding="utf-8") as fh:
        print("1" if (json.load(fh).get("report") or {}).get("enabled") else "0")
except Exception:
    print("0")
PYEOF
)"

if [ "$REPORT_ENABLED" = "1" ]; then
    systemctl enable --now pve-power-report.timer >/dev/null 2>&1 ||         systemctl enable pve-power-report.timer
    note "日报定时器已启用（$(systemctl show -p TriggersNext --value \
        pve-power-report.timer 2>/dev/null || echo '见 systemctl list-timers')）"
else
    systemctl disable --now pve-power-report.timer >/dev/null 2>&1 || true
    note "日报未启用（配置里 report.enabled 为 false）"
fi

cat <<EOF

安装完成。

  pve-power              启动界面
  pve-power status       一次性健康检查
  pve-power report       用电汇总
  pve-power mail-report --dry-run   预览日报（不发信）

在看电费数字之前，请先把电价设对：启动 'pve-power'，进入
"电价"标签页，把费率改成你账单上的数字。
默认值 0.60 元/kWh 只是占位。

想每天收一封用电日报，先把 SMTP 填进 $CONFIG 的
"smtp" 和 "report" 两节（README 里有 QQ / 163 的示例），
再跑一次本脚本，或者直接：

  systemctl enable --now pve-power-report.timer

配置文件：$CONFIG
数据库：  $STATE_DIR/power.db
服务：    systemctl status pve-power-collector
日报：    systemctl list-timers pve-power-report.timer
EOF
