#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""生意社（100ppi.com）现货基准价自动抓取。

为什么做这个
------------
产业信号里的「现货价格 / 基差」一直是人工填。参考站靠服务器 cron 上一份没进备份
的脚本补这块，所以源码里查不到。生意社的现期表是免登录静态 HTML，一张表给出
60 个品种的现货基准价，正好补上。

数据源
------
https://www.100ppi.com/sf/    现期表：60 个品种的现货基准价（另有最近/主力合约价格，
                              但我们只用现货价，期货价用本项目自己的行情算，口径才一致）
https://egg.100ppi.com/       鸡蛋频道：近几日的基准价序列，用来判「连续 3 日不创新低」

反爬处理
--------
首次 GET 会返回约 660 字节的安全检查页，页面里明文写着 HW_CHECK 这个 cookie 的值，
带该 cookie 再 GET 一次就能拿到完整页面。两次请求，不需要浏览器、不需要账号。

口径
----
1. 现货价 = 生意社每日发布的「基准价」。鸡蛋是元/公斤，期货报价是元/500kg，
   所以鸡蛋要乘 500 才能跟盘面比；其余四个品种现货与期货同为元/吨，乘 1。
2. 基差 = 现货（换算后）− 本项目主连最新价，由 server 端实时算，不采信页面的
   「现期差」列（那一列格式残缺，且用的是上期结算价）。
3. 生意社基准价每天 16:38 前后更新一次，所以默认 30 分钟抓一回，失败留旧值并标 stale。

用法
----
    python workbench/spot.py --once     # 抓一次打印，不常驻
    python workbench/spot.py            # 常驻，30 分钟一轮
"""
from __future__ import annotations

import json
import re
import threading
import time
import urllib.request
from pathlib import Path

BASE = Path(__file__).resolve().parent
CACHE = BASE / "spot.json"

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")

# 生意社现期表里的商品名 -> 本项目品种
WANTED = {
    "鸡蛋":   {"id": "JD", "unit": "元/公斤", "mult": 500},   # 期货 元/500kg
    "聚乙烯": {"id": "L",  "unit": "元/吨",   "mult": 1},
    "聚丙烯": {"id": "PP", "unit": "元/吨",   "mult": 1},
    "豆粕":   {"id": "M",  "unit": "元/吨",   "mult": 1},
    "玉米":   {"id": "C",  "unit": "元/吨",   "mult": 1},
}

SF_URL = "https://www.100ppi.com/sf/"
EGG_URL = "https://egg.100ppi.com/"

INTERVAL = 1800          # 30 分钟
TIMEOUT = 20
CACHE_TTL = 6 * 3600     # 缓存超过 6 小时算 stale


def _get(url: str, cookie: str | None = None) -> str:
    """取页面。生意社的 JS 安全检查：先拿到 HW_CHECK 的值，再带着它请求一次。"""
    hdr = {"User-Agent": UA, "Accept-Language": "zh-CN,zh;q=0.9"}
    if cookie:
        hdr["Cookie"] = cookie
        hdr["Referer"] = "https://www.100ppi.com/"
    req = urllib.request.Request(url, headers=hdr)
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        raw = r.read()
    txt = raw.decode("utf-8", "ignore")

    if not cookie:
        m = re.search(r'var\s+_0x2\s*=\s*"([0-9a-f]{32})"', txt)
        if m:                                   # 撞上安全检查页，带 cookie 重来
            return _get(url, f"HW_CHECK={m.group(1)}")
    return txt


def _clean_cell(html: str) -> str:
    return re.sub(r"<[^>]+>", "", html).replace("&nbsp;", " ").strip()


def parse_sf(html: str) -> dict:
    """解析现期表。

    版面是「品种链接 + 紧随的现货价单元格」，一行一个品种，共 50 多行。整表用
    `</tr>` 切行不可靠——现期差那两格是嵌套 table，非贪婪匹配会先撞上内层 `</tr>`
    把行截断。直接锚定「品种链接 + 下一个 td 的价格」最稳，也顺手跳过了交易所分组行。
    主力合约价不取：期货价一律用本项目自己的行情算基差，两边合约月份和更新时间
    才一致（生意社的期货价是上一交易日结算口径）。
    """
    pat = re.compile(
        r'<a href="https://www\.100ppi\.com/sf/\d+\.html"[^>]*>([^<]+)</a></td>\s*'
        r"<td>([\d.]+)&nbsp;</td>",
        re.S,
    )
    out = {}
    for name, price in pat.findall(html):
        meta = WANTED.get(name.strip())
        if not meta:
            continue
        spot = float(price)
        out[meta["id"]] = {
            "name": name.strip(),
            "spot": spot,
            "unit": meta["unit"],
            "mult": meta["mult"],
            "spotFut": round(spot * meta["mult"], 2),   # 换算到期货报价单位
        }
    return out


def _num(s):
    try:
        return float(s)
    except (TypeError, ValueError):
        return None


def parse_egg_history(html: str) -> list:
    """鸡蛋频道里「X月X日生意社鸡蛋基准价为Y元/公斤」的序列，按日期倒序去重。"""
    rows = re.findall(r"(\d{1,2})月(\d{1,2})日生意社鸡蛋基准价为([\d.]+)元/公斤", html)
    seen, out = set(), []
    for mo, d, v in rows:
        key = (int(mo), int(d))
        if key in seen:
            continue
        seen.add(key)
        out.append({"md": f"{int(mo):02d}-{int(d):02d}", "mdKey": key, "price": float(v)})
    return out


def judge_jd1(hist: list) -> dict:
    """历史实现，判定逻辑已迁到 autosig.spot_stop_fall，这里只保留给手工调试用。

    autosig.py 是信号的唯一事实来源，判定不能两处各写一份——口径一改就漂。
    """
    if not hist or len(hist) < 4:
        return {"state": "unknown", "detail": f"生意社鸡蛋基准价仅 {len(hist or [])} 日"}
    seq = sorted(hist, key=lambda r: r["mdKey"])[-4:]
    px = [r["price"] for r in seq]
    last, low_before = px[-1], min(px[:-1])
    state = "hit" if last >= low_before else "miss"
    return {"state": state,
            "detail": f"{'→'.join(f'{p:.2f}' for p in px)} 元/公斤（前低 {low_before:.2f}）"}


def fetch_once() -> dict:
    """抓一轮。任何一步失败都不抛异常，返回带 error 的结果，调用方决定用不用旧值。"""
    res = {"fetchedAt": time.strftime("%Y-%m-%d %H:%M:%S"), "ok": False, "items": {}, "jdHist": None}
    try:
        items = parse_sf(_get(SF_URL))
        if items:
            res["items"] = items
            res["ok"] = True
        else:
            res["error"] = "现期表解析为空（页面结构可能变了）"
    except Exception as exc:
        res["error"] = f"现期表抓取失败 {type(exc).__name__}: {exc}"

    try:
        hist = parse_egg_history(_get(EGG_URL))
        if hist:
            res["jdHist"] = sorted(hist, key=lambda r: r["mdKey"], reverse=True)[:8]
    except Exception as exc:
        res.setdefault("warn", []).append(f"鸡蛋频道抓取失败 {type(exc).__name__}")
    return res


def load_cache() -> dict:
    try:
        return json.loads(CACHE.read_text("utf-8"))
    except Exception:
        return {}


class SpotFeed:
    """常驻线程：每 30 分钟抓一次，成功落盘；失败保留上一份并标记 stale。"""

    def __init__(self, interval: int = INTERVAL):
        self.interval = interval
        self._lock = threading.Lock()
        self._data = load_cache() or {"ok": False, "items": {}}
        self._thread: threading.Thread | None = None

    def start(self):
        self._thread = threading.Thread(target=self._loop, name="spot-feed", daemon=True)
        self._thread.start()

    def _loop(self):
        while True:
            try:
                fresh = fetch_once()
                with self._lock:
                    if fresh.get("ok"):
                        fresh["stale"] = False
                        self._data = fresh
                        try:
                            CACHE.write_text(json.dumps(fresh, ensure_ascii=False, indent=1), "utf-8")
                        except OSError:
                            pass
                    elif self._data.get("items"):
                        self._data["stale"] = True
                        self._data["lastError"] = fresh.get("error")
            except Exception:
                pass
            time.sleep(self.interval)

    def snapshot(self) -> dict:
        with self._lock:
            d = dict(self._data)
        if d.get("fetchedAt"):
            try:
                ts = time.mktime(time.strptime(d["fetchedAt"], "%Y-%m-%d %H:%M:%S"))
                d["ageSec"] = int(time.time() - ts)
                d["stale"] = d.get("stale") or d["ageSec"] > CACHE_TTL
            except ValueError:
                pass
        return d


if __name__ == "__main__":
    if "--once" in __import__("sys").argv:
        r = fetch_once()
        r["jd1"] = judge_jd1(r.get("jdHist") or [])
        print(json.dumps(r, ensure_ascii=False, indent=1))
    else:
        feed = SpotFeed()
        feed.start()
        print("生意社现货抓取已启动，30 分钟一轮，Ctrl-C 退出")
        while True:
            time.sleep(3600)
