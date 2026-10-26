#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""我的农产品（mysteel.com）产业数据抓取 —— 豆粕库存。

为什么做这个
------------
产业信号 M-1「库存去化：豆粕周度库存<100万吨」一直靠人工盯。Mysteel 农产品
每周一在蛋白饲料频道发《全国主要区域大豆及豆粕库存统计》，正文直接给出
「豆粕库存 109.6 万吨，较上周增加 0.52 万吨」等结论句（在 AI 智能摘要与正文
首段各出现一次），纯文本可解析，无需 OCR。
账号 config.json → mysteel（与隆众/卓创同套路：Cookie 登录态）。

数据源（栏目页 → 最新一期文章）
------------------------------
豆粕库存周数据   siliao.mysteel.com/article/p-4118----0703---------1.html  周一发
文章 URL 形如 /a/26101916/hash.html（前缀 YYMMDDHH），用它排序取最新；
标题括号里的（20261019）作数据日期展示。

解析说明
--------
锚点句（智能摘要 + 正文首段各一次）：
「据Mysteel调查，2026年第42周全国主要油厂大豆库存下降，豆粕库存基本持平，
未执行合同下降。其中大豆库存843.01万吨，较上周减少19.17万吨；豆粕库存
109.6万吨，较上周增加0.52万吨；未执行合同447.90万吨，较上周减少70.33万吨；
豆粕表观消费量为175.13万吨，较上周增加58.57万吨。」
方向词在数字前（增加=正、减少=负），当期即可判环比，无需翻上一期。

用法
----
    python workbench/mysteel.py --once     # 抓一轮打印，落 mysteel.json
    python workbench/mysteel.py --probe    # 存最新一期原文，便于校验解析
    python workbench/mysteel.py            # 常驻，30 分钟一轮（周数据足够新鲜）
"""
from __future__ import annotations

import gzip
import json
import re
import sys
import threading
import time
import urllib.error
import urllib.request
import zlib
from datetime import datetime
from pathlib import Path

BASE = Path(__file__).resolve().parent
CONFIG = BASE / "config.json"
STATE = BASE / "mysteel.json"
PROBE = BASE / "ms_probe"

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")

# 栏目定义：key -> (栏目列表页, 标题正则, 刷新间隔秒)
COLS = {
    "meal_stock": ("https://siliao.mysteel.com/article/p-4118----0703---------1.html",
                   r"全国主要区域大豆及豆粕库存统计", 4 * 3600),
}

LOGIN_HINT = ("请登录", "登录后查看", "立即登录", "免费注册")


def _cfg() -> dict:
    try:
        return json.loads(CONFIG.read_text(encoding="utf-8"))
    except Exception:
        return {}


def cookie() -> str:
    ms = (_cfg().get("mysteel") or {})
    return (ms.get("cookie") or "").strip()


def fetch(url: str, timeout: int = 25, tries: int = 3) -> str:
    """GET 并解码（gzip/deflate，同隆众/卓创套路）。429/5xx 指数退避。"""
    headers = {
        "User-Agent": UA,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9",
        "Accept-Encoding": "gzip, deflate",
        "Referer": "https://ncp.mysteel.com/",
    }
    ck = cookie()
    if ck:
        headers["Cookie"] = ck
    last = None
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                raw = r.read()
                enc = (r.headers.get("Content-Encoding") or "").lower()
            break
        except urllib.error.HTTPError as e:
            last = e
            if e.code in (429, 500, 502, 503) and i < tries - 1:
                time.sleep(3 * (i + 1))
                continue
            raise
        except (urllib.error.URLError, TimeoutError) as e:
            last = e
            if i < tries - 1:
                time.sleep(2 * (i + 1))
                continue
            raise
    else:
        raise last or RuntimeError("fetch failed")
    if enc == "gzip":
        raw = gzip.decompress(raw)
    elif enc == "deflate":
        try:
            raw = zlib.decompress(raw)
        except Exception:
            raw = zlib.decompress(raw, -15)
    for cs in ("utf-8", "gb18030"):
        try:
            return raw.decode(cs)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", "replace")


def html_text(frag: str) -> str:
    frag = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", frag, flags=re.S | re.I)
    frag = re.sub(r"</td>|</th>", "\t", frag, flags=re.I)
    frag = re.sub(r"<br\s*/?>|</p>|</tr>|</div>|</h\d>", "\n", frag, flags=re.I)
    frag = re.sub(r"<[^>]+>", "", frag)
    frag = frag.replace("\u3000", " ").replace("\xa0", " ").replace("&nbsp;", " ")
    frag = re.sub(r"[ \t]+", " ", frag)
    frag = re.sub(r"\n\s*\n+", "\n", frag)
    return frag.strip()


def latest_list(col: str, n: int = 3) -> list[dict]:
    """栏目页取最新 n 期。URL 前缀 YYMMDDHH（如 26101916）单调，直接排序。"""
    url, pat, _ = COLS[col]
    html = fetch(url)
    rx = re.compile(
        r'<a[^>]+href="(https://ncp\.mysteel\.com/a/(\d{8})[^"]+\.html)"[^>]*>(.*?)</a>', re.S)
    best = {}
    for m in rx.finditer(html):
        href, ymdhh, raw_title = m.group(1), m.group(2), html_text(m.group(3))
        if not re.search(pat, raw_title):
            continue
        # 排除区域分项（两湖/川渝/福建/广东/河南等），只要全国口径
        if re.search(r"两湖|川渝|福建|广东|河南|华东|华南|华北", raw_title):
            continue
        if ymdhh not in best:
            dm = re.search(r"（(\d{8})）", raw_title)
            best[ymdhh] = {"key": ymdhh,
                           "date": f"{dm.group(1)[:4]}-{dm.group(1)[4:6]}-{dm.group(1)[6:]}" if dm else "",
                           "url": href, "title": raw_title}
    return [best[k] for k in sorted(best, reverse=True)[:n]]


def latest(col: str) -> dict | None:
    rows = latest_list(col, 1)
    return rows[0] if rows else None


def article(url: str) -> dict:
    """取正文并三态归类（同隆众/卓创）：

    ok    正文（智能摘要或首段）存在且含库存句
    login 匿名/Cookie 失效 —— 出现登录墙特征
    """
    html = fetch(url)
    text = html_text(html)
    # 登录墙判定：正文没有结论句且页面出现登录提示
    if "豆粕库存" not in text and any(k in text for k in LOGIN_HINT):
        return {"ok": False, "reason": "login", "needLogin": True, "text": "", "url": url,
                "note": "需要登录：Cookie 为空或已过期（重跑 node ms_login.js）"}
    return {"ok": "豆粕库存" in text, "reason": "ok" if "豆粕库存" in text else "empty",
            "needLogin": False, "text": text, "url": url}


# ---------- 指标抽取 ----------
def _num(s):
    try:
        return float(s)
    except Exception:
        return None


def _delta(m) -> float | None:
    """「较上周增加/减少 X」→ 带方向数值。"""
    if not m:
        return None
    v = _num(m.group(2))
    if v is not None and m.group(1) in ("减少", "下降", "回落", "下滑", "降低"):
        v = -abs(v)
    return v


def parse_meal_stock(text: str) -> dict:
    """豆粕库存（万吨）+ 环比 + 顺带大豆库存/未执行合同/表观消费量。

    锚点：「豆粕库存109.6万吨，较上周增加0.52万吨」。
    """
    out = {}
    m = re.search(r"豆粕库存[^0-9]{0,12}(\d+(?:\.\d+)?)\s*万吨", text)
    if m:
        out["value"] = _num(m.group(1))
        out["unit"] = "万吨"
    dm = re.search(r"豆粕库存[^。；]{0,40}?较上周(增加|减少|下降|上升|回落)\s*(\d+(?:\.\d+)?)\s*万吨", text)
    d = _delta(dm)
    if d is not None:
        out["delta"] = round(d, 2)
    # 周数：「2026年第42周」
    wm = re.search(r"(\d{4})年第(\d{1,2})周", text)
    if wm:
        out["week"] = f"{wm.group(1)}-W{int(wm.group(2)):02d}"
    # 顺带指标（前端展示次要行）。窗口收紧到 4 字：原文「未执行合同下降」后面
    # 紧跟的是大豆库存数值，窗口太宽会把 843.01 错认成未执行合同（踩过）。
    for name, key in (("大豆库存", "soy"), ("未执行合同", "contract"), ("表观消费量", "consumption")):
        mm = re.search(name + r"为?[^0-9]{0,4}(\d+(?:\.\d+)?)\s*万吨", text)
        if mm:
            out[key] = _num(mm.group(1))
    if "value" in out and out.get("delta") is not None and out["value"]:
        out["prev"] = round(out["value"] - out["delta"], 2)
    return out


PARSERS = {"meal_stock": parse_meal_stock}


def snapshot(prev: dict | None = None) -> dict:
    """抓一轮。文章 URL 与上轮相同则直接复用（周数据，绝大多数轮次 0 次文章请求）。"""
    ck = cookie()
    res = {"configured": bool(ck), "at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
           "items": {}}
    if not ck:
        res["needLogin"] = True
        return res
    old_items = (prev or {}).get("items") or {}
    for col, (url, _pat, _iv) in COLS.items():
        item = {"col": col, "ok": False}
        try:
            rows = latest_list(col, 2)
            la = rows[0] if rows else None
            if not la:
                item["err"] = "栏目页未找到匹配文章"
            else:
                old = old_items.get(col) or {}
                if old.get("ok") and old.get("url") == la["url"]:
                    item = dict(old)
                    item.pop("err", None)
                    item.pop("reason", None)
                else:
                    item.update({"title": la["title"], "url": la["url"], "date": la["date"]})
                    art = article(la["url"])
                    if not art["ok"]:
                        item["err"] = art.get("note") or art["reason"]
                        item["reason"] = art["reason"]
                        if art["reason"] == "login":
                            item["needLogin"] = True
                    else:
                        item["ok"] = True
                        item["parsed"] = PARSERS[col](art["text"])
                        item["snippet"] = art["text"][:220].replace("\n", " ")
                        item["chars"] = len(art["text"])
        except Exception as e:
            item["err"] = f"{type(e).__name__}: {e}"
        res["items"][col] = item
        time.sleep(1.2)
    reasons = {it.get("reason") for it in res["items"].values() if not it.get("ok")}
    if "login" in reasons:
        res["status"] = "needLogin"
        res["note"] = "Cookie 为空或已过期，重跑 node ms_login.js"
    elif all(it.get("ok") for it in res["items"].values()):
        res["status"] = "ok"
    else:
        res["status"] = "partial"
    return res


def merge_history(new: dict) -> dict:
    """与旧文件合并，保留最近 12 期（豆粕库存）。"""
    try:
        old = json.loads(STATE.read_text(encoding="utf-8"))
    except Exception:
        old = {}
    hist = old.get("history") or {}
    for col, it in (new.get("items") or {}).items():
        p = it.get("parsed") or {}
        v = p.get("value")
        if not it.get("ok") or v is None:
            continue
        arr = hist.get(col) or []
        stamp = it.get("date") or p.get("week") or new["at"][:10]
        if not arr or arr[-1].get("d") != stamp:
            arr.append({"d": stamp, "v": v,
                        "extra": {k: x for k, x in p.items()
                                  if k not in ("value", "unit", "prev")}})
        else:
            arr[-1]["v"] = v
        hist[col] = arr[-12:]
    new["history"] = hist
    return new


class MysteelFeed(threading.Thread):
    daemon = True

    def __init__(self, interval: int = 1800):
        super().__init__(name="mysteel")
        self.interval = interval
        self.lock = threading.Lock()
        self.data = {}
        try:
            self.data = json.loads(STATE.read_text(encoding="utf-8"))
        except Exception:
            pass

    def run(self):
        while True:
            try:
                snap = merge_history(snapshot(self.get()))
                with self.lock:
                    self.data = snap
                STATE.write_text(json.dumps(snap, ensure_ascii=False, indent=1), encoding="utf-8")
            except Exception as e:
                print("[mysteel] 抓取失败:", e, flush=True)
            time.sleep(self.interval)

    def get(self) -> dict:
        with self.lock:
            return dict(self.data or {})


def main():
    if "--probe" in sys.argv:
        if not PROBE.exists():
            PROBE.mkdir(parents=True, exist_ok=True)
        for col in COLS:
            la = latest(col)
            if not la:
                print(col, "未找到文章")
                continue
            art = article(la["url"])
            (PROBE / f"{col}.txt").write_text(
                f"{la['title']}\n{la['url']}\n\n{art['text']}", encoding="utf-8")
            print(f"{col}: {la['title']}  reason={art['reason']}  字数={len(art['text'])}")
        return
    if "--once" in sys.argv:
        try:
            prev = json.loads(STATE.read_text(encoding="utf-8"))
        except Exception:
            prev = None
        snap = merge_history(snapshot(prev))
        STATE.write_text(json.dumps(snap, ensure_ascii=False, indent=1), encoding="utf-8")
        print(json.dumps(snap, ensure_ascii=False, indent=1)[:2500])
        return
    MysteelFeed().run()


if __name__ == "__main__":
    main()
