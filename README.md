# fris —— 期货盯盘工作台

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python 3.9+](https://img.shields.io/badge/python-3.9%2B-blue.svg)](https://www.python.org/)

```bash
git clone https://github.com/wlddhj/fris.git && cd fris
pip install -r requirements.txt
cp workbench/config.example.json workbench/config.json
printf 'TQ_AUTH_USER=你的快期账号\nTQ_AUTH_PASS=你的密码\n' > workbench/.tqenv
python3 workbench/server.py        # 打开 http://localhost:8000
```

期货产业链实时盯盘与信号系统：快期行情（WebSocket 推送）+ 多源现货/产业数据（卓创、隆众、Mysteel、生意社、布瑞克）+ 15 条产业信号自动判定 + SQLite 历史落档。

> **定位**：这是一个「自己用的盯盘台」，不是交易信号服务。它把散落在各处的行情、持仓、现货、库存拼到一块屏上，把判定规则写成人能读的标签，让你少算几步。所有结论都只是数据整理，**不构成投资建议**。
>
> **开箱能跑的部分**：快期行情、技术位与持仓建议、方向打分、15 条产业信号框架、历史落档、前端工作台。
> **需要你自备的**：快期账号（免费版够用）、以及卓创/隆众/Mysteel/布瑞克的订阅（没有订阅时，对应产业信号会显示「账号未配置」，不影响其余功能）。详见 [数据源与合规](#数据源与合规)。

## 名词速查（看不懂页面上的词先看这里）

页面和文档里的术语都是行话，完整解释见 [`docs/期货盯盘工作台-标签判定依据.md`](docs/期货盯盘工作台-标签判定依据.md) 的 **§10 名词对照表**。最常被问到的几个：

| 术语 | 代码字段 | 一句话解释 |
|---|---|---|
| 多突破位 | `longTriggerBreak` | 价格站上它并站稳，才算多头起来 → 可顺势做多 |
| 多回踩位 | `longPullback` | 涨上去后回落到的支撑价，回踩不破可试多（比追高划算） |
| 空破位 | `shortBreakdown` | 价格跌破它并站不回来，才算空头成立 → 可顺势做空 |
| 滞涨区 | `shortFadeZone` | 反弹到这一带涨不动的区间，空头性价比最高的试空位置 |
| 机位 | `plan.entry` | 还没进场时的开仓计划（≠ 已有仓位的持仓建议） |
| 基差 | 现货 − 期货 | 正=现货升水/盘面贴水（下方有托底），负=盘面升水（上方有压力）；已统一换算到期货报价单位 |
| 四象限 | 持仓 × 价格 | 涨价增仓=多头进攻、跌价增仓=空头进攻、涨价减仓=空头回补、跌价减仓=多头离场 |
| ATR14 | 日均波幅 | 全站的波动尺子：止损距离、突破滞后阈值、基差深浅都按它折算 |
| 方向倾向 | `dirdir.py` → `dir` 字段 | MA 排列 + 区间位置 + 四象限 + 资金失衡四块打分，≥4 强多 / ≤−4 强空，全部实时算，无人工设定 |

## 功能

- **方向打分后端统一下发**（dirdir.py）：方向倾向是唯一真相源，页面优先用后端结果、离线才本地兜底；`scripts/dir_parity.py` 对拍两份实现
- **实时盘面**：主连合约 K 线、技术位（levels.py）、季节性（seasonality.py），WebSocket 毫秒级推送到浏览器工作台
- **15 条产业信号全自动**（autosig.py）：
  - 鸡蛋 jd1-jd3：现货止跌（卓创蛋价序列）、淘汰鸡、基差
  - 塑料/PP l1-l3 / pp1-pp3：石化出厂价、基差、隆众开工/库存
  - 豆粕 m1-m3：压榨利润（自算：豆粕×0.785 + 豆油×0.185 − 豆二 − 150）、库存/开机（Mysteel）、基差
  - 玉米 c1-c3：北港库存（布瑞克）、基层售粮进度（卓创，>50% 触发）、淀粉-玉米价差分位（自算）
- **多源数据抓取**：sci99.py（卓创）、longzhong.py（隆众）、mysteel.py（我的农产品网）、sinograin.py（布瑞克）、spot.py（生意社）。付费源需你自己登录后把 cookie 填进 `config.json`（本仓库不含自动登录脚本，原因见 [数据源与合规](#数据源与合规)）
- **历史落档**（archivedb.py）：盘中 tick 每 5 分钟、收盘/夜盘收盘自动归档 SQLite，`python3 workbench/archivedb.py --today` 查看；每天 15:05 / 23:05 收盘定格后自动一致性备份，备份落在 git 仓库内时自动 commit + push（设 `FRIS_BACKUP_DIR=/path/to/fris/workbench/backups` 启用，默认落 `workbench/backups/` 仅保留 60 天），`--backup` 可手动触发
- **多空力量面板**：时报简报上方常驻展示

## 启动

```bash
# 1. 安装依赖
pip install -r requirements.txt

# 2. 配置凭证
export TQ_AUTH_USER=xxx TQ_AUTH_PASS=yyy     # 快期凭据（或写入 workbench/.tqenv）
cp workbench/config.example.json workbench/config.json   # 填入第三方数据账号

# 3. 前台启动
python3 workbench/server.py
# 打开 http://localhost:8000

# 3b. 后台常驻启动（备份目录指向 git 仓库 → 每次备份自动 commit+push）
FRIS_BACKUP_DIR=/path/to/fris/workbench/backups \
  nohup python3 workbench/server.py > workbench/nohup.out 2>&1 &

# 查看运行日志 / 停止
tail -f workbench/nohup.out
lsof -ti:8000 -ti:8765 | xargs kill        # 8000=页面 8765=WebSocket
```

服务启动后无需再动：行情断线自动重连，`playbook.json` 改动热重载，数据库落档与定时备份（15:05 / 23:05）自动进行。

## 维护命令

```bash
cd workbench

# ---- 历史落档（archivedb.py）----
python3 archivedb.py --today               # 当日落档记录 + 信号判定分布
python3 archivedb.py --hits 30             # 近 N 天各信号 hit/miss/unknown 统计
python3 archivedb.py --close               # 今日收盘定格快照（可指定日期 --close 2026-10-20）
python3 archivedb.py --export out.json     # 全量导出 JSON
python3 archivedb.py --backup              # 手动备份（落在 git 仓库内会自动 commit+push）
git log --oneline -- workbench/backups     # 查看备份入库历史

# ---- 数据生成 ----
# levels.py 同时产出：关键位阶梯 + 持仓建议（短线止损/波段止损/波段目标/趋势目标）
TQ_AUTH_USER=xxx TQ_AUTH_PASS=yyy python3 levels.py        # 重算关键位与持仓建议（写 playbook.json，服务自动热重载）
TQ_AUTH_USER=xxx TQ_AUTH_PASS=yyy python3 levels.py --dry-run   # 只打印核对表，不写盘
TQ_AUTH_USER=xxx TQ_AUTH_PASS=yyy python3 seasonality.py   # 重新生成季节性 seasonality.json

# ---- 第三方数据站 cookie（在你的浏览器里登录后手动获取，填进 config.json）----
# 1. 浏览器登录对应站点（卓创 / 隆众 / Mysteel），进入任意一篇数据类文章
# 2. DevTools → Network → 任一请求 → 复制 Request Headers 里 Cookie 的完整值
# 3. 粘到 workbench/config.json 对应站点的 cookie 字段，重启服务
# cookie 是 JWT，会过期；过期后信号会提示「Cookie 已过期」，重复上面三步即可
```

## 部署到服务器（可选）

本机自己盯盘不需要这一步；要 24 小时不断线（比如手机在外看），再往服务器上放。

以 `root@<your-server>`、目录 `/opt/fris` 为例：

```bash
# 1. 打包（自动排除凭证与数据）
tar czf /tmp/fris.tgz -C /path/to/fris \
  --exclude='.git' --exclude='__pycache__' --exclude='backups' \
  --exclude='archive*.db*' --exclude='workbench/config.json' --exclude='workbench/.tqenv' .
# 2. 上传解压
ssh root@<your-server> 'mkdir -p /opt/fris'
scp /tmp/fris.tgz root@<your-server>:/opt/fris/
ssh root@<your-server> 'cd /opt/fris && tar xzf fris.tgz && rm fris.tgz'
# 3. 装依赖
ssh root@<your-server> 'cd /opt/fris && python3 -m venv .venv && \
  .venv/bin/pip install -r requirements.txt'
# 4. 单独上传凭证（不入 git，也不进发布包）
scp workbench/config.json workbench/.tqenv root@<your-server>:/opt/fris/workbench/
# 5. 装 systemd 服务（先按实际路径改 deploy/fris.service）
scp deploy/fris.service root@<your-server>:/etc/systemd/system/fris.service
ssh root@<your-server> 'systemctl daemon-reload && systemctl enable --now fris'
```

安全组需放行 **8000（页面）和 8765（WebSocket）**——两个都要，只开 8000 页面会一直显示「连接中」。

`deploy/fris.service` 默认按 `/opt/fris` 写，换成你的路径后 `systemctl daemon-reload` 生效。要点：`WorkingDirectory` 指向 `workbench/`、`Environment=TZ=Asia/Shanghai`、`Restart=always`、日志追加到 `/var/log/fris.log`。

### 日常更新

`scripts/deploy.sh` 是配套的发布脚本：打包 → 上传 → 服务器备份旧版（保留最近 10 份）→ 解压 → 重启 → 健康检查。

```bash
FRIS_SERVER=root@<your-server> FRIS_DIR=/opt/fris bash scripts/deploy.sh
```

不带环境变量时它不会猜地址，会要求你显式指定。

### 运维命令

```bash
systemctl status fris          # 状态
systemctl restart fris         # 重启（改 playbook.json 不用重启，服务热重载）
tail -f /var/log/fris.log      # 实时日志
ls -1t /opt/fris/releases | head    # 历史版本
```

### 定时重算关键位

日盘收盘后重算一次，落在服务器 crontab：

```bash
10 15 * * 1-5 cd /opt/fris/workbench && ../.venv/bin/python levels.py >> /var/log/fris-levels.log 2>&1
```

### 注意

- 服务器上的 `workbench/backups/` 是数据目录，不是 git 仓库，不会自动 push。
- 同一快期账户不要同时在本机和服务器跑两个实例，会互相抢占行情连接。
- 付费源 cookie 过期后，在服务器上改 `workbench/config.json` 再 `systemctl restart fris`。本仓库没有自动登录脚本，你需要在本机浏览器登录后复制 cookie（见上文维护命令）。

## 数据源与合规

| 数据 | 来源 | 是否需要订阅 | 没订阅时 |
|---|---|---|---|
| 期货行情/K线/持仓 | 快期（天勤 TqSdk） | 免费账号可用 | 跑不起来，先注册 |
| 现货基准价 | 生意社公开页面 | 否 | 正常 |
| 蛋价/售粮进度 | 卓创资讯 | 是 | 显示「账号未配置」 |
| 开工率/库存 | 隆众资讯 | 是 | 显示「账号未配置」 |
| 豆粕库存/开机 | Mysteel | 是 | 显示「账号未配置」 |
| 北港玉米库存 | 布瑞克 | 是 | 显示「账号未配置」 |

几点说明，用之前请先看：

1. **付费数据要自己有订阅。** 各站的账号、cookie 是你自己的，本项目只做「用你的凭证读你有权读的页面」，不提供也不分发任何数据。
2. **本仓库不含自动登录脚本。** 之前自己用的时候写过 playwright 自动登录，但那类脚本要绕过站点的验证码与风控，公开分发不合适，也会给用的人带来麻烦。所以这里只留手动填 cookie 的路径。
3. **抓取频率要克制。** 默认按信号所需的最低频率取数，别改成高频轮询，容易被封号，也不礼貌。
4. **不要拿去卖信号。** 这个项目的价值在于「把公开信息和你有权看的数据整理清楚」，不是给你一个能对外兜售的信号源。

## 许可

MIT，见 [LICENSE](LICENSE)。自用、改造、二次分发都行，出问题自己担。

## 结构

```
workbench/
  server.py        # HTTP + WebSocket 服务主程序
  autosig.py       # 15 条产业信号自动判定引擎
  archivedb.py     # SQLite 历史落档（pushes/signals 表）
  levels.py        # 技术位计算（KDJ/MACD/布林等）
  seasonality.py   # 季节性统计
  extfeed.py       # 外参合约订阅（豆二/玉米/淀粉月对齐）
  sci99.py         # 卓创资讯抓取（售粮进度/蛋价）
  longzhong.py     # 隆众资讯抓取（开工率/库存）
  mysteel.py       # Mysteel 抓取（豆粕库存/开机率）
  sinograin.py     # 布瑞克抓取（北港玉米库存）
  spot.py          # 生意社现货基准价
  static/index.html# 前端工作台
docs/              # 需求文档 / 判定依据 / 接入勘察记录
```

## 安全

- `config.json`（第三方账号密码/cookie）与 `.tqenv`（快期凭据）已在 `.gitignore` 中排除，入库的是脱敏模板 `config.example.json`
- 仓库内不含任何真实凭证、服务器地址与个人路径；部署脚本的服务器地址一律走环境变量
