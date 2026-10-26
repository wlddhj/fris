#!/usr/bin/env python3
"""SQLite 落档：把盯盘工作台每次推送的关键状态存进本地单文件库。

为什么只用 SQLite（分析结论见 2026-10-20 日志）
  参考项目用 MySQL 是因为行情脚本/网站/定时任务三处异地部署，库是唯一交接面；
  本项目单进程同机，唯一缺的能力是「历史留档」——信号触发后行情怎么走、当天盘面
  提示了什么，事后无从查证。单文件库零依赖零运维，够用。

存什么
  pushes  精简推送快照（行情行 + 产业信号判定 + 各产业源摘要；不含 K 线，太肥）
          kind = tick        盘中每 5 分钟一条（交易时段内）
               close         15:00 日盘收盘定格
               night_close   23:00 夜盘收盘定格
  signals pushes 里的信号判定拍平成行：symbol × sig_id × state × detail，
          加索引，以后算「信号触发后 N 日涨跌」直接 SQL 就能跑

表结构刻意宽松：payload 存 JSON 文本，列只挑查询要用的，避免每次加字段改表。
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

BASE = Path(__file__).resolve().parent
DB_PATH = BASE / "archive.db"

TICK_INTERVAL = 300          # 盘中采样间隔 5 分钟
TICK_KEEP_DAYS = 30          # tick 快照保留天数（close/night_close 永久保留）
# 北京时间交易时段（分），与前端 tradingSession 同口径
SESSIONS = [(540, 615), (630, 690), (810, 900), (1260, 1380)]
BOUNDARIES = ((900, "close"), (1380, "night_close"))   # (分钟, 收盘定格 kind)

# ---- 定时备份 ----
# 备份目录默认 workbench/backups/；可用环境变量 FRIS_BACKUP_DIR 指到 git 仓库内，
# 备份完成后自动 add/commit/push（见 _git_commit_push）
BACKUP_DIR = Path(os.environ.get("FRIS_BACKUP_DIR") or (BASE / "backups"))
BACKUP_KEEP_DAYS = 60                          # 本地备份保留天数
BACKUP_AT = (15 * 60 + 5, 23 * 60 + 5)         # 日盘/夜盘收盘定格后 5 分钟各备一次

_lock = threading.Lock()
_conn: sqlite3.Connection | None = None
_last_tick = 0.0


def _db() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        _conn = sqlite3.connect(DB_PATH, check_same_thread=False)
        _conn.execute("PRAGMA journal_mode=WAL")
        _conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS pushes (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              ts TEXT NOT NULL,             -- 北京时间 YYYY-MM-DD HH:MM:SS
              trade_date TEXT NOT NULL,     -- 交易日（夜盘归次日自然日，与盘面一致）
              kind TEXT NOT NULL,           -- tick / close / night_close
              payload TEXT NOT NULL         -- 精简快照 JSON
            );
            CREATE INDEX IF NOT EXISTS idx_pushes_date ON pushes(trade_date, kind);
            CREATE TABLE IF NOT EXISTS signals (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              push_id INTEGER NOT NULL,
              ts TEXT NOT NULL,
              trade_date TEXT NOT NULL,
              symbol TEXT NOT NULL,         -- 剧本 id（JD/L/PP/M/C）
              sig_id TEXT NOT NULL,         -- jd1/jd2/l1/m2/c3 ...
              state TEXT NOT NULL,          -- hit / miss / unknown
              detail TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_signals_lookup ON signals(trade_date, symbol, sig_id);
            """
        )
        _conn.commit()
    return _conn


def _now() -> tuple[datetime, int, str]:
    """返回 (北京时间, 当日分钟数, 交易日)。夜盘 21:00-23:00 归当日自然日——
    剧本口径里夜盘属于当天交易日，跨 0 点的品种（有的到 01:00）这里不覆盖，
    23:00 定格之后的夜盘不落 tick，够用。"""
    d = datetime.now(timezone(timedelta(hours=8)))   # 北京时间（时区感知，不用已弃用的 utcnow）
    return d, d.hour * 60 + d.minute, d.strftime("%Y-%m-%d")


def in_session(minutes: int) -> bool:
    return any(a <= minutes < b for a, b in SESSIONS)


def _prune(conn: sqlite3.Connection) -> None:
    cut = (datetime.now(timezone(timedelta(hours=8))) - timedelta(days=TICK_KEEP_DAYS)).strftime("%Y-%m-%d")
    conn.execute("DELETE FROM pushes WHERE kind='tick' AND trade_date < ?", (cut,))
    conn.execute(
        "DELETE FROM signals WHERE push_id IN (SELECT id FROM pushes WHERE kind='tick' AND trade_date < ?)",
        (cut,))
    conn.commit()


def maybe_record(lean: dict) -> str | None:
    """节流入口：_rebuild_update 每拍调一次，内部决定存不存。

    - 交易时段内每 5 分钟一条 tick
    - 15:00 / 23:00 收盘边界 2 分钟窗口内强制一条定格（即使不在交易时段判断边缘）
    返回落库的 kind（没存返回 None），日志可见。
    """
    global _last_tick
    now, minutes, tdate = _now()
    ts = now.strftime("%Y-%m-%d %H:%M:%S")

    kind = None
    if in_session(minutes):
        if time.time() - _last_tick >= TICK_INTERVAL:
            kind = "tick"
    else:
        for bmin, bkind in BOUNDARIES:
            if bmin <= minutes < bmin + 2:
                kind = bkind
                break
    if kind is None:
        return None

    # 收盘定格 2 分钟窗口内会反复触发，去重：同日同 kind 已有就跳过
    conn = _db()
    with _lock:
        if kind != "tick":
            row = conn.execute(
                "SELECT 1 FROM pushes WHERE trade_date=? AND kind=? LIMIT 1", (tdate, kind)).fetchone()
            if row:
                return None
        cur = conn.execute(
            "INSERT INTO pushes (ts, trade_date, kind, payload) VALUES (?,?,?,?)",
            (ts, tdate, kind, json.dumps(lean, ensure_ascii=False)))
        pid = cur.lastrowid
        for pid_book, sigs in (lean.get("autoSignals") or {}).items():
            for sid, sv in (sigs or {}).items():
                conn.execute(
                    "INSERT INTO signals (push_id, ts, trade_date, symbol, sig_id, state, detail) "
                    "VALUES (?,?,?,?,?,?,?)",
                    (pid, ts, tdate, pid_book, sid,
                     str((sv or {}).get("state") or ""), str((sv or {}).get("detail") or "")))
        conn.commit()
        if kind == "tick":
            _last_tick = time.time()
        if conn.execute("SELECT (SELECT COUNT(*) FROM pushes) % 200").fetchone()[0] == 0:
            _prune(conn)
    return kind


# ---------------- 基差历史 ----------------

# 合约代码前缀 → 剧本 id（兼容没有 spot.id 的老记录）
_ID_BY_CODE = {"jd": "JD", "l": "L", "pp": "PP", "m": "M", "c": "C"}


def _code_of(qsym) -> str:
    """DCE.jd2612 -> jd（取交易所后的字母部分，去掉月份数字）。"""
    return re.sub(r"\d+$", "", str(qsym or "").split(".")[-1].lower())


def basis_series(days: int = 45) -> dict[str, list[dict]]:
    """按交易日取每个品种最后一条带现货的记录，供前端算基差分位与日间变化。

    为什么回头从 pushes 里捞而不是单建一张基差表：现货行本来就挂在推送 payload 的
    quotes[sym].spot 上，再建表等于同一份数据存两遍。每个交易日只有几十条，Python 过一遍
    很快，调用方（server）另做了 10 分钟缓存，不会每拍都扫库。

    取「当天最后一条」而不是收盘那条：现货报价一天更新一次（卓创 15:30、生意社 9:49），
    日内不变，但基差随盘面抖动——要的是收盘附近的那个值。

    返回 {品种id: [{"d","basis","pct","spot","fut"}, ...]}，按日期升序。
    没有 id 的老记录（加字段之前的）跳过，不影响新数据积累。
    """
    if not DB_PATH.exists():
        return {}
    cut = (datetime.now(timezone(timedelta(hours=8))) - timedelta(days=days)).strftime("%Y-%m-%d")
    try:
        conn = _db()
        with _lock:
            rows = conn.execute(
                "SELECT trade_date, payload FROM pushes WHERE trade_date >= ? ORDER BY trade_date, id",
                (cut,)).fetchall()
    except Exception:
        return {}
    latest: dict[tuple[str, str], dict] = {}
    for tdate, payload in rows:
        try:
            quotes = (json.loads(payload) or {}).get("quotes") or {}
        except Exception:
            continue
        for qsym, q in quotes.items():
            sp = (q or {}).get("spot") or {}
            b = sp.get("basis")
            # 品种 id：新记录里带（spot.id）；加字段之前的老记录用合约代码前缀推
            # （DCE.jd2612 -> jd -> JD），兼容期过了就可以只认 spot.id。
            bid = sp.get("id") or _ID_BY_CODE.get(_code_of(qsym))
            if not bid or b is None:
                continue
            latest[(tdate, bid)] = {          # 同日多条后写覆盖，即当天最后一条
                "d": tdate,
                "basis": round(float(b), 2),
                "pct": sp.get("basisPct"),
                "spot": sp.get("spot"),
                "fut": sp.get("fut") if sp.get("fut") is not None else None,
            }
    out: dict[str, list[dict]] = {}
    for (_, bid), v in sorted(latest.items()):
        out.setdefault(bid, []).append(v)
    return out


# ---------------- 定时备份 ----------------

def backup(dest: str | Path | None = None) -> Path:
    """一致性备份：VACUUM INTO 生成紧凑单文件副本（WAL 内容并入），只读打开不动在线库。
    库不存在时抛 FileNotFoundError，调用方自行兜底。"""
    dest = Path(dest) if dest else BACKUP_DIR / f"archive-{datetime.now(timezone(timedelta(hours=8))).strftime('%Y%m%d')}.db"
    if not DB_PATH.exists():
        raise FileNotFoundError(f"数据库不存在: {DB_PATH}")
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.unlink(missing_ok=True)   # VACUUM INTO 要求目标不存在，同日重备直接覆盖
    src = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    try:
        src.execute("VACUUM INTO ?", (str(dest),))   # 只读连接即可，写入的是目标文件
    finally:
        src.close()
    return dest


def _git_commit_push(p: Path) -> str | None:
    """备份文件落在 git 仓库内时自动 add/commit/push。
    返回说明文案；不在仓库返回 None；失败返回错误摘要（不影响备份本身）。"""
    try:
        r = subprocess.run(["git", "-C", str(p.parent), "rev-parse", "--show-toplevel"],
                           capture_output=True, text=True, timeout=10)
    except Exception:
        return None
    if r.returncode != 0:
        return None
    repo = r.stdout.strip()
    rel = str(p.relative_to(repo))
    for cmd in (["git", "-C", repo, "add", rel],
                ["git", "-C", repo, "commit", "-m", f"db backup {p.stem}"],
                ["git", "-C", repo, "push"]):
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        except Exception as exc:
            return f"git 执行异常: {exc}"
        out = (r.stderr or r.stdout).strip()
        if r.returncode != 0 and "nothing to commit" not in out:
            return out[:200] or f"git 返回 {r.returncode}"
    return f"git 已提交推送: {rel}"


def _prune_backups() -> None:
    """按文件名里的日期轮换，超过 BACKUP_KEEP_DAYS 的删掉。"""
    cut = (datetime.now(timezone(timedelta(hours=8))) - timedelta(days=BACKUP_KEEP_DAYS)).date()
    for p in BACKUP_DIR.glob("archive-*.db"):
        try:
            d = datetime.strptime(p.stem, "archive-%Y%m%d").date()
        except ValueError:
            continue   # 命名不符合的不碰
        if d < cut:
            p.unlink(missing_ok=True)


def auto_backup_loop() -> None:
    """随 server 常驻：睡到下一个备份点（15:05 / 23:05）执行一次，失败不中断循环。"""
    while True:
        now, minutes, _ = _now()
        nxt = min((m for m in BACKUP_AT if m > minutes), default=BACKUP_AT[0] + 1440)
        wait = max((nxt - minutes) * 60 - now.second, 1.0)
        time.sleep(wait)
        try:
            p = backup()
            _prune_backups()
            ts = datetime.now(timezone(timedelta(hours=8))).strftime("%m-%d %H:%M:%S")
            msg = f"[{ts}] db 备份完成 -> {p}"
            git = _git_commit_push(p)
            if git:
                msg += f"，{git}"
            print(msg, flush=True)
        except Exception as exc:
            print(f"[{datetime.now(timezone(timedelta(hours=8))).strftime('%H:%M:%S')}] db 备份失败: {exc}", flush=True)


# ---------------- 查询 CLI ----------------

def _rows(sql: str, args=()) -> list[sqlite3.Row]:
    conn = _db()
    conn.row_factory = sqlite3.Row
    return conn.execute(sql, args).fetchall()


def cmd_today() -> None:
    today = datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d")
    rows = _rows("SELECT id, ts, kind FROM pushes WHERE trade_date=? ORDER BY id", (today,))
    print(f"{today} 共 {len(rows)} 条：")
    for r in rows:
        print(f"  #{r['id']:<6d} {r['ts'][11:]}  {r['kind']}")
    sig = _rows(
        "SELECT symbol, sig_id, state, COUNT(*) n FROM signals WHERE trade_date=? "
        "GROUP BY symbol, sig_id, state ORDER BY symbol, sig_id", (today,))
    if sig:
        print("\n信号判定分布：")
        for r in sig:
            print(f"  {r['symbol']:>3s} {r['sig_id']:<4s} {r['state']:<8s} x{r['n']}")


def cmd_hits(days: int) -> None:
    """近 N 天各信号 hit 次数——以后接「触发后 N 日收益」就是从这里出发。"""
    cut = (datetime.now(timezone(timedelta(hours=8))) - timedelta(days=days)).strftime("%Y-%m-%d")
    rows = _rows(
        "SELECT symbol, sig_id, SUM(state='hit') hits, SUM(state='miss') misses, "
        "SUM(state='unknown') unknowns FROM signals WHERE trade_date>=? "
        "GROUP BY symbol, sig_id ORDER BY symbol, sig_id", (cut,))
    print(f"近 {days} 天信号判定统计：")
    print(f"  {'品种':<5s}{'信号':<6s}{'hit':>5s}{'miss':>6s}{'unknown':>9s}")
    for r in rows:
        print(f"  {r['symbol']:<5s}{r['sig_id']:<6s}{r['hits']:>5d}{r['misses']:>6d}{r['unknowns']:>9d}")


def cmd_export(path: str) -> None:
    rows = _rows("SELECT id, ts, trade_date, kind, payload FROM pushes ORDER BY id")
    out = [{"id": r["id"], "ts": r["ts"], "trade_date": r["trade_date"], "kind": r["kind"],
            "payload": json.loads(r["payload"])} for r in rows]
    Path(path).write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"导出 {len(out)} 条 -> {path}")


def cmd_close(date: str | None) -> None:
    d = date or datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d")
    rows = _rows("SELECT ts, kind, payload FROM pushes WHERE trade_date=? AND kind LIKE '%close%' "
                 "ORDER BY id", (d,))
    if not rows:
        print(f"{d} 无收盘定格记录")
        return
    for r in rows:
        p = json.loads(r["payload"])
        print(f"\n== {r['kind']} @ {r['ts'][11:]} ==")
        for sym, q in (p.get("quotes") or {}).items():
            sp = q.get("spot") or {}
            print(f"  {sym:<14s} last={q.get('last')} 现货={sp.get('spot')} 基差={sp.get('basis')}")


def main(argv: list[str]) -> None:
    if "--today" in argv:
        cmd_today()
    elif "--hits" in argv:
        i = argv.index("--hits")
        cmd_hits(int(argv[i + 1]) if i + 1 < len(argv) else 30)
    elif "--export" in argv:
        i = argv.index("--export")
        cmd_export(argv[i + 1] if i + 1 < len(argv) else str(BASE / "archive_export.json"))
    elif "--backup" in argv:
        i = argv.index("--backup")
        p = backup(argv[i + 1] if i + 1 < len(argv) else None)
        _prune_backups()
        print(f"备份完成 -> {p}")
        git = _git_commit_push(p)
        if git:
            print(git)
    elif "--close" in argv:
        i = argv.index("--close")
        cmd_close(argv[i + 1] if i + 1 < len(argv) else None)
    else:
        print(__doc__)
        print("用法: python archivedb.py --today | --hits [N] | --close [YYYY-MM-DD] | --export [path] | --backup [path]")


if __name__ == "__main__":
    main(sys.argv[1:])
