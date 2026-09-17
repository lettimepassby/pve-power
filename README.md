# pve-power

给 Proxmox VE 主机做电量统计和 BMC 管理，全部通过 IPMI 完成。它采样服务器
经 DCMI 上报的功率，把这些采样变成 kWh 和电费，并提供一个终端界面来查看和
修改 BMC 的配置。

纯 Python 标准库实现 —— 不需要 pip，不需要安装任何依赖。运行需要 `python3`
（带 `curses` 和 `sqlite3`，Debian 自带的编译版本都有）和 `ipmitool`。

## 为什么要有这个项目

它替代的 cron 脚本长这样：

```bash
w=$(ipmitool dcmi power reading | awk '/Instantaneous/{print $4}')
echo "$(date '+%F %T'),$w" >> /var/log/pve-power/$(date +%F).csv
```

它记录的是每个时刻机器的瞬时功率。这是一份有用的日志，但不是账单。
单看一个瞬时瓦数说明不了耗电量 —— 你必须对它按时间积分、决定没采到样的
那些分钟怎么处理、并且按**用电当时**生效的电价给每一度电计价。这个项目做的
就是这三件事，并且可以导入旧 CSV，历史数据不会丢。

## 安装

在 PVE 主机上以 root 身份、在代码目录里执行：

```bash
./tools/install.sh
```

它会把程序装到 `/opt/pve-power`，生成 `/usr/local/bin/pve-power` 启动器，
如果还没有配置文件就写入 `/etc/pve-power/config.json`，导入已有的
`/var/log/pve-power/*.csv`，询问你是否停用旧的 cron 任务，然后启动采集服务。
重复执行它是安全的：只升级代码，不动你的配置和数据库。

**在看电费数字之前，先把电价设对。** 默认值 0.60 元/kWh 只是占位。
启动 `pve-power`，进"电价"标签页，把费率改成你账单上的数字。

## 使用

```
pve-power                  启动界面（默认）
pve-power status           一次性健康检查；--json 便于脚本调用
pve-power report           用电汇总；--days N 或 --month YYYY-MM
pve-power sample           取一次读数并存储
pve-power collect          运行采样循环（systemd 跑的就是这个）
pve-power import           导入旧的 cron CSV；--dir 指定目录
pve-power mail-report      生成日报并通过 SMTP 发送
pve-power config --init    写入默认配置；--preset flat|china-tou
```

`-c/--config` 指定其他配置文件。`--force` 在配置有问题时仍然继续运行。

### 界面

用 `1`–`9` 或 Tab/Shift-Tab 切换标签页；`r` 刷新，`?` 查看按键说明，`q` 退出。
方向键、PgUp/PgDn、Home/End 以及 `j`/`k`/`g`/`G` 在视图内移动。

| 标签页 | 内容 | 按键 |
| --- | --- | --- |
| 总览 | 实时功率、今日与本月用电量和电费、预计月电费、BMC 与机箱健康状态 | `r` 刷新 |
| 电量 | 按天或按小时的用电量与电费，柱状展示 | `d`/`h` 按天/按小时，`[` `]` 调整范围，Enter 钻取某天，`e` 导出 CSV |
| 传感器 | 每个 IPMI 传感器及其到临界阈值的余量 | `f` 按类别筛选，`o` 只看故障 |
| BMC | 固件与 FRU 信息、网络配置、机箱状态 | Enter 编辑字段，`c` 机箱电源，`p` 上电恢复策略，`i` 定位指示灯 |
| 用户 | BMC 账户、权限，以及哪些是厂商默认账户 | `p` 改密码，`n` 改名，`e`/`d` 启用/禁用，`v` 权限 |
| 事件日志 | BMC 事件日志，按严重程度分级 | `o` 只看问题事件，`X` 清空日志 |
| 电价 | 价格、计费模式、采样设置 | Enter 编辑，`m` 切换单一/分时电价，`a`/`x` 增删阶梯，`P` 载入预设，`s` 保存，`R` 重算历史 |

任何会改动 BMC 的操作都会先确认；真正危险的操作 —— 强制断电、清空事件日志
—— 需要你完整输入 `yes` 才会执行。在远程连接下修改 BMC 的 IP 地址时，会先
警告你这一步会切断你当前的连接。

## 数字是怎么算出来的

**电量。** 每个采样点与前一个采样点之间用梯形法积分：
`kWh = (W₁ + W₂)/2 × Δt / 3,600,000`。如果改用单次读数乘以间隔，总量会被
负载当时的升降方向带偏 —— 上升时偏高，下降时偏低。取区间两端读数的平均值，
对线性变化是精确的，对其他情况也远比矩形法接近。

**缺口。** 如果距上次采样超过了 `max_gap_seconds`（默认 900 秒），这个区间
记为 0 电量并标记为缺口。采集器当时没在运行，就没有任何诚实的依据声称机器
耗了电；编造停机期间的用电量，比承认这个数据洞更糟。"电量"标签页会显示
覆盖率，你可以看到某段时间里实际被测量到的比例。

**电费。** 每个采样点按**其时间戳当时**生效的电价算好，和采样一起存下来。
所以下个月修改电价，不会篡改上个月的费用。当你确实需要重算历史时 —— 比如
电价填错了要修正 —— 在"电价"标签页按 `R`，它会重算所有采样，并按自然月
重置月累计电量，让阶梯电价一级一级地复现当时的跳档过程。它只改钱，
永远不改已记录的电量。

**平均值。** 某段时间的平均功率是 `kWh × 3,600,000 / 有效时长` —— 按时间
加权，而不是按采样条数加权，所以密集采样的一段不会把平均值拉向自己。

### 电价

三种模式，阶梯是在其他模式之上叠加的：

- **单一电价（flat）** —— 每度电一个价。
- **分时电价（tou）** —— 按一天中的小时定价，可选限定星期几。没有覆盖到的
  小时会回退到基础电价，而不是按 0 元计费；"电价"标签页有一条 24 格覆盖条，
  标出哪些小时没被覆盖。
- **阶梯电价（tiered）** —— 在基础电价之上加价，按当月累计电量跨过各档
  阈值逐级跳档。最后一档必须不设上限。

`pve-power config --init --preset china-tou` 会写入一份示例分时电价方案
（尖峰/高峰/平段/低谷）。里面的价格只是示例，不是你当地电网的价格。

## 用电日报

每天早上发一封邮件，内容是前一天的用电量、电费、逐小时曲线、分时段拆分、
本月累计和推算，外加当天的 BMC 事件和异常传感器。同时给出纯文本和 HTML
两个版本 —— 纯文本不是降级品，它自己就是完整的，方便转发进只显示纯文本
的地方。

### 在界面里设置

启动 `pve-power`，切到「9：日报」标签页。所有字段都在那里：收件人、发送
范围、SMTP 服务器和账号密码。

* `P` 挑服务商预设（QQ、163、Gmail、Outlook 等），主机名、端口、加密方式
  一次填好，只剩账号和密码要自己输。
* `v` 预览报告正文，不发信。
* `t` 立刻试发一封 —— 改完设置马上就知道对不对。SMTP 最容易配错的是端口
  和加密方式不匹配、把登录密码当成授权码、发件地址和账号不一致，而这些
  如果只能靠「等明天早上八点半看有没有收到」来发现，调一次要一天。
* `s` 保存到配置文件。

密码在界面上永远只显示设没设，不显示原文。试发不会影响定时任务：它不写
「今天已发送」标记，到点了照常发当天那封。

右边的状态面板会告诉你定时器是不是真的开着 —— 配置里 `enabled` 为 true
但没 `systemctl enable` 的话，什么也不会发生，这是最容易漏的一步。

### 或者直接改配置文件

先预览，确认内容对了再配发信：

```bash
pve-power mail-report --dry-run            # 打到终端，不连服务器
pve-power mail-report --dry-run --html     # 连 HTML 一起打出来
pve-power mail-report --dry-run --date 2026-09-16
```

然后在 `/etc/pve-power/config.json` 里填两节：

```json
{
  "smtp": {
    "host": "smtp.qq.com",
    "security": "ssl",
    "user": "you@qq.com",
    "password": "授权码，不是登录密码",
    "sender": "you@qq.com",
    "sender_name": "pve-power"
  },
  "report": {
    "enabled": true,
    "recipients": ["ops@example.com"],
    "covers": "yesterday"
  }
}
```

`security` 有三个值，端口不填就按它取默认：

| security   | 默认端口 | 用在哪 |
|------------|---------|--------|
| `starttls` | 587     | 绝大多数服务商（Gmail、企业邮箱、自建 Postfix） |
| `ssl`      | 465     | QQ 邮箱、163 这些只开 465 的 |
| `none`     | 25      | 内网 relay；配了它就不能再设密码，否则密码明文过网 |

证书一律验证，没有关掉的开关：一个每天自动发信的任务如果对中间人毫无
察觉，SMTP 密码就等于公开了。自签证书的内网 MTA 请把 CA 装进系统信任库。

QQ 邮箱和 163 要的是**授权码**，在邮箱设置里单独生成，不是你的登录密码。

不想让密码落在配置文件里的话，改放 `/etc/pve-power/smtp.env`（权限 0600）：

```
PVE_POWER_SMTP_PASSWORD=授权码
```

systemd 单元已经带了 `EnvironmentFile=-`，环境变量优先于配置文件。

配好以后发一封真的试试，再交给定时器：

```bash
pve-power mail-report --force          # --force 绕过「今天已发过」
systemctl enable --now pve-power-report.timer
systemctl list-timers pve-power-report.timer
```

默认每天 8:30 发前一天的完整日报（带最多 5 分钟随机延迟，避开整点的
限流高峰）。改时间就改 `etc/pve-power-report.timer` 里的 `OnCalendar`。
如果改成夜里发「今天」的，记得把 `report.covers` 也改成 `"today"`，
否则发出去的还是昨天那份。

机器在 8:30 时关着也不会漏：定时器带 `Persistent=true`，开机后会补发。
同一天只发一封 —— 发送成功的日期记在数据库里，重复触发会跳过。

退出码：`0` 成功或按规则跳过，`1` 发送失败，`2` 配置不全。定时器单元对
`2` 不重试，配置不会自己长好，重试只会每五分钟往 journal 里灌同样的报错。

## 远程 BMC

`ipmi.host` 留空时使用本地 KCS 接口（`/dev/ipmi0`），在 PVE 主机上跑就应该
这样。填上地址后，所有调用都会改走网络、使用 `-I lanplus`，这样你可以从
工作站连到服务器的 BMC 上操作。密码就存在配置文件里，所以它的权限是 `0600`。

## 目录结构

```
pvepower/ipmi.py        所有 ipmitool 调用及其输出解析
pvepower/storage.py     SQLite 表结构、积分、聚合、重算
pvepower/config.py      配置模型、电价计算、校验
pvepower/collector.py   采样守护进程和旧 CSV 导入
pvepower/cli.py         子命令
pvepower/report.py      日报的内容组装与 text/HTML 渲染
pvepower/mailer.py      SMTP 投递
pvepower/tui/           curses 界面：app、数据缓存、控件、各视图
pvepower/tui/theme.py   配色：终端色号与 HTML 邮件共用一套设计色
pvepower/textwidth.py   中日韩宽字符的终端列宽计算
etc/                    systemd 单元文件
tools/install.sh        安装脚本
tests/                  电量与电价计算测试，以及 pty 驱动的界面冒烟测试
```

配置文件在 `/etc/pve-power/config.json`，采样数据在
`/var/lib/pve-power/power.db`。数据库使用 WAL 模式，所以采集器写入时界面
可以同时读取。

## 测试

```bash
python3 -m unittest tests.test_energy tests.test_sensors tests.test_theme \
                   tests.test_energy_view tests.test_report \
                   tests.test_report_view tests.test_tui
```

`test_energy` 用人工手算的数值校验积分和计价 —— 100W 持续一小时是 0.1 kWh，
一小时内从 100W 线性升到 200W 是 0.15 kWh，谷时按 0.20 计费的 1 度电在峰时
采样加入后仍然是 0.20。`test_tui` 在真实 pty 里以六种终端尺寸渲染每一个视图
（包括一种小于最小尺寸的），并按下所有按键，包括会弹出对话框的那些。

`test_report` 里发信那部分不打桩 `smtplib`，而是在本地起一个真的 SMTP
服务器让它连上去。打桩只能证明「我调用了 sendmail」，证明不了信封地址、
多部分结构和中文编码对不对 —— 而这正是发信真正会出错的地方。这条测试
当场就抓到一个：`EmailMessage` 默认 policy 的 `cte_type` 是 `8bit`，中文
正文会以裸 UTF-8 字节发出去，只有宣告了 `8BITMIME` 的服务器才允许这样。

## 没有做的事

没有实现 DCMI 功率封顶（power capping）：这台 BMC（浪潮 SA5112M4，固件 4.12）
对 `dcmi power get_limit` 返回 error 80，没有可对接的东西。相关代码是直接
不写，而不是交付一份没验证过的实现。
