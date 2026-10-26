"""外盘行情源（新浪财经）—— 给产业信号补上 CBOT 大豆、布伦特原油。

为什么要单独一个源
------------------
TqSdk 只有国内交易所，美豆/布伦特拿不到；而产业信号里 m3（美豆突破 1000 美分）
和 pp3（布伦特成本支撑）的原始条件就写在这两个品种上。新浪 hq.sinajs.cn 的外盘
接口（hf_ 前缀）能免费拿到实时价，日线用 GlobalFuturesDailyKLine 拿。

做法
----
后台线程：报价 5 秒轮询、日线 10 分钟刷新一次（日线只用来算 20 日高/均线/昨收，
不需要高频）。取不到就把对应字段留空，上层判定成 unknown，不猜。

字段口径（实测核对，别照抄第三方文档）
------------------------------------
hf_ 行情串：0=最新 2=买一 3=卖一 4=最高 5=最低 6=时间 8=今开
日线：date/open/high/low/close，最后一根是当日进行中 → 统计时剔除
"""
from __future__ import annotations

import json
import re
import threading
import time
import urllib.request

QUOTE_URL = "https://hq.sinajs.cn/list={codes}"
KLINE_URL = ("https://stock2.finance.sina.com.cn/futures/api/jsonp.php/var%20_=/"
             "GlobalFuturesService.getGlobalFuturesDailyKLine?symbol={sym}")
HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"),
    "Referer": "https://finance.sina.com.cn",
}

# 内部 key -> (新浪代码, 中文名)
SYMBOLS = {
    "CBOT_S": ("S", "CBOT大豆"),
    "BRENT": ("OIL", "布伦特原油"),
}

QUOTE_EVERY = 5.0
KLINE_EVERY = 600.0


def _get(url: str, timeout: float = 8.0) -> str:
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = r.read()
    for enc in ("utf-8", "gbk", "latin-1"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", "ignore")


def _parse_quotes(text: str) -> dict:
    out = {}
    for m in re.finditer(r'hq_str_hf_(\w+)="([^"]*)"', text):
        code, body = m.group(1), m.group(2)
        f = body.split(",")
        if len(f) < 9:
            continue
        try:
            out[code] = {
                "last": float(f[0]) if f[0] else None,
                "high": float(f[4]) if f[4] else None,
                "low": float(f[5]) if f[5] else None,
                "time": f[6].strip(),
            }
        except (ValueError, IndexError):
            continue
    return out


def _parse_kline(text: str) -> list:
    i = text.find("[")
    if i < 0:
        return []
    try:
        arr = json.loads(text[i:text.rfind("]") + 1])
    except Exception:
        return []
    rows = []
    for r in arr:
        try:
            rows.append({"date": r["date"], "high": float(r["high"]),
                         "low": float(r["low"]), "close": float(r["close"])})
        except Exception:
            continue
    return rows


def _stats(kline: list) -> dict:
    """前 20 日高 / 20 日均线 / 昨收：都基于已完成的日线（剔除当日进行中那根）。"""
    if not kline:
        return {}
    done = kline[:-1] if len(kline) > 1 else kline
    if len(done) < 20:
        return {"prev_close": (done[-1]["close"] if done else None)}
    tail = done[-20:]
    return {
        "high20": round(max(r["high"] for r in tail), 3),
        "ma20": round(sum(r["close"] for r in tail) / 20, 3),
        "prev_close": round(done[-1]["close"], 3),
        "bar_date": done[-1]["date"],
    }


class SinaExtFeed(threading.Thread):
    """后台轮询外盘行情；snapshot() 返回 {CBOT_S: {...}, BRENT: {...}}，线程安全。"""

    def __init__(self):
        super().__init__(daemon=True, name="sina-ext-feed")
        self.lock = threading.Lock()
        self.data = {}
        self.kline = {}
        self.err = ""

    def _one_quote_round(self):
        codes = ",".join("hf_" + c for c, _ in SYMBOLS.values())
        quotes = _parse_quotes(_get(QUOTE_URL.format(codes=codes)))
        now = time.strftime("%H:%M:%S")
        with self.lock:
            for key, (code, name) in SYMBOLS.items():
                q = quotes.get(code)
                merged = dict(self.data.get(key) or {})
                merged.update({"name": name, "code": code, "quote_at": now})
                if q:
                    merged.update({"last": q["last"], "day_high": q["high"],
                                   "day_low": q["low"], "quote_time": q["time"]})
                self.data[key] = merged
            self.err = ""

    def _one_kline_round(self):
        for key, (code, _name) in SYMBOLS.items():
            try:
                rows = _parse_kline(_get(KLINE_URL.format(sym=code), timeout=15))
            except Exception:
                continue
            if not rows:
                continue
            with self.lock:
                self.kline[key] = rows
                merged = dict(self.data.get(key) or {})
                merged.update(_stats(rows))
                self.data[key] = merged

    def run(self):
        last_kline = 0.0
        while True:
            try:
                self._one_quote_round()
            except Exception as exc:
                with self.lock:
                    self.err = f"{type(exc).__name__}: {str(exc)[:80]}"
            if time.time() - last_kline > KLINE_EVERY:
                try:
                    self._one_kline_round()
                    last_kline = time.time()
                except Exception:
                    pass
            time.sleep(QUOTE_EVERY)

    def snapshot(self) -> dict:
        with self.lock:
            return {k: dict(v) for k, v in self.data.items()}
