#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""中储粮网(esinograin.com) 竞价数据抓取 —— 玉米「收储启动」信号原料。

免登录，三层数据：
  1) 首页 HTML 内嵌公告列表：opengg('<32位ggid>', '<ubd>') + title
     ubd=0212 交易公告(含计划量)、0215 交易结果(含成交率)
  2) POST /api/others/getTransactionAnnouncementDetail {jyggId} -> JSON 正文
  3) POST /api/bid/getBdxxListNewxxzt -> 一口价/挂牌标的（在架量与价格）

口径（重要）：
  · 公告标题里的日期是「专场交易日」，常常提前 1-3 天发布 → 按交易日聚合，不按发布日
  · 成交率只在「结果」公告里，且当天上午场 12:00 左右才出 → 覆盖率必须一并暴露
  · 结果按（交易日, 分公司, 采购/销售）对回公告的计划量，成交率按量加权；
    对不上的场次退化为算术平均，并计入 unmatched
  · 净轮入 = 采购成交量 − 销售成交量（正=中储粮净收，负=净抛）
"""
from __future__ import annotations

import json
import re
import threading
import time
import urllib.parse
import urllib.request
import http.cookiejar
from datetime import date, datetime, timedelta
from pathlib import Path

BASE = "https://www.esinograin.com"
HOME = BASE + "/"
DETAIL = BASE + "/api/others/getTransactionAnnouncementDetail"
BIDLIST = BASE + "/api/bid/getBdxxListNewxxzt"
STATE = Path(__file__).with_name("sinograin.json")
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0 Safari/537.36")

GRAIN = "玉米"
VAR_RE = r"(?:玉米|小麦|稻谷|稻谷|菜籽油|大豆|油脂|进口玉米)"
AGENT_RE = re.compile(r"\d{1,2}月\d{1,2}日(.+?)" + VAR_RE)


class Client:
    def __init__(self, delay: float = 1.2):
        cj = http.cookiejar.CookieJar()
        self.op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))
        self.op.addheaders = [("User-Agent", UA), ("Referer", HOME),
                              ("Accept-Language", "zh-CN,zh;q=0.9")]
        self.delay = delay
        self._last = 0.0
        self.req = 0

    def _pace(self):
        wait = self.delay - (time.time() - self._last)
        if wait > 0:
            time.sleep(wait)
        self._last = time.time()
        self.req += 1

    def get(self, url: str, tries: int = 3) -> str:
        for i in range(tries):
            self._pace()
            try:
                with self.op.open(urllib.request.Request(url), timeout=30) as r:
                    return r.read().decode("utf-8", errors="ignore")
            except Exception:
                if i == tries - 1:
                    raise
                time.sleep(2 + i * 2)
        return ""

    def post_form(self, url: str, payload: dict, tries: int = 4) -> dict:
        data = urllib.parse.urlencode(payload).encode()
        for i in range(tries):
            self._pace()
            try:
                req = urllib.request.Request(
                    url, data=data,
                    headers={"Content-Type": "application/x-www-form-urlencoded"})
                with self.op.open(req, timeout=30) as r:
                    js = json.loads(r.read().decode("utf-8", errors="ignore"))
                body = (js.get("data") or {}).get("jyggNr") or ""
                # 站点限流时接口仍 200，但正文是提示语 → 视为失败重试
                if "当前访问人数较多" in body:
                    raise ValueError("throttled")
                return js
            except Exception:
                if i == tries - 1:
                    return {}
                time.sleep(4 + i * 4)
        return {}


# ---------- 解析 ----------

def parse_list(html: str) -> list:
    out, seen = [], set()
    for gid, ubd, title in re.findall(
            r"opengg\('([0-9a-f]{32})',\s*'(\d+)'\)[^>]*title=\"([^\"]+)\"", html):
        if gid in seen:
            continue
        seen.add(gid)
        out.append({"ggid": gid, "ubd": ubd, "title": title.strip()})
    return out


def html_text(html: str) -> str:
    t = re.sub(r"<script.*?</script>", "", html, flags=re.S)
    t = re.sub(r"</t[dh]>", "\t", t, flags=re.S)
    t = re.sub(r"</tr>", "\n", t, flags=re.S)
    t = re.sub(r"<[^>]+>", "", t).replace("&nbsp;", " ").replace("\u3000", " ")
    t = "\n".join(l.strip() for l in t.splitlines() if l.strip())
    # 官方公告常把两字标题点开写：「数 量：75722吨」「品 种：进口玉米」
    # 中文之间的空白一律压掉，否则正则取不到数量
    t = re.sub(r"(?<=[\u4e00-\u9fa5])[ \t]+(?=[\u4e00-\u9fa5])", "", t)
    return t


def judge_kind(title: str) -> str:
    if "取消" in title:
        return "cancel"
    if "结果" in title:
        return "result"
    if "双向" in title:
        return "swap"
    if "采购" in title:
        return "buy"
    if "销售" in title:
        return "sell"
    return "other"


def parse_qty(text: str):
    m = re.search(r"数量[：:]\s*([\d,，\.]+)\s*吨", text) or \
        re.search(r"数量[：:]\s*([\d,，\.]+)", text)
    if not m:
        return None
    try:
        return float(m.group(1).replace(",", "").replace("，", ""))
    except ValueError:
        return None


def parse_rate(text: str):
    m = re.search(r"合计\s*(\d+(?:\.\d+)?)\s*%", text)
    return float(m.group(1)) if m else None


def parse_day(title: str, fallback: str) -> str:
    m = re.search(r"(\d{1,2})月(\d{1,2})日", title)
    if not m:
        return fallback
    return f"{fallback[:4]}-{int(m.group(1)):02d}-{int(m.group(2)):02d}"


def parse_agent(title: str) -> str:
    m = AGENT_RE.search(title)
    if m:
        a = m.group(1).strip("·、 ")
        return "进口玉米" if a.startswith("进口") else a
    return ""


def is_import(title: str) -> bool:
    return "进口" in title


def collect(client: Client, grain: str = GRAIN, cache: dict | None = None) -> list:
    """首页列表 -> 逐条取正文。
    cache: {ggid: row}，公告正文按 ggid 不变，命中就跳过请求（每轮通常只剩 1 个首页请求）。
    """
    cache = cache or {}
    html = client.get(HOME)
    today = date.today().isoformat()
    rows = []
    for it in parse_list(html):
        t = it["title"]
        if grain not in t:
            continue
        kind = judge_kind(t)
        if kind == "other":
            continue
        hit = cache.get(it["ggid"])
        if hit and (hit.get("ok") or 0):
            rows.append(hit)
            continue
        d = (client.post_form(DETAIL, {"jyggId": it["ggid"]}).get("data") or {})
        body = html_text(d.get("jyggNr") or "")
        rows.append({
            "ggid": it["ggid"],
            "kind": kind,
            "title": t,
            "day": parse_day(t, (d.get("jyggSj") or "")[:10] or today),
            "agent": parse_agent(t),
            "import": is_import(t),
            "qty": parse_qty(body),
            "rate": parse_rate(body),
            "publishedAt": d.get("jyggSj", ""),
            "ok": bool(body),
        })
    return rows


# ---------- 聚合 ----------

def day_stats(rows: list, day: str) -> dict:
    sess = [r for r in rows if r["day"] == day and r["kind"] in ("buy", "sell", "swap")]
    res = [r for r in rows if r["day"] == day and r["kind"] == "result"]
    # 结果 -> 场次：按 (agent, 采/销) 对回计划量
    idx = {}
    for r in sess:
        idx[(r["agent"], r["kind"])] = r
    out = {}
    for side in ("buy", "sell"):
        ss = [r for r in sess if r["kind"] == side]
        plan = sum(r["qty"] or 0 for r in ss)
        matched, batch, unmatched = [], [], []
        for r in res:
            if side == "buy" and "采购" not in r["title"]:
                continue
            if side == "sell" and "销售" not in r["title"]:
                continue
            s = idx.get((r["agent"], side))
            q = (s or {}).get("qty") or 0
            if r["rate"] is None:
                continue
            if q:
                matched.append((q, r["rate"]))
            else:
                unmatched.append(r["rate"])
        done = sum(q * rt / 100 for q, rt in matched)
        allr = [rt for _, rt in matched] + unmatched
        n_res = len([r for r in res
                     if ("采购" if side == "buy" else "销售") in r["title"] and r["rate"] is not None])
        rate = round(sum(rt for _, rt in matched) / len(matched), 1) if matched else \
               (round(sum(unmatched) / len(unmatched), 1) if unmatched else None)
        covered_plan = sum(q for q, _ in matched)
        out[side] = {
            "sessions": len(ss),
            "planT": round(plan) if plan else None,
            "resultSessions": n_res,
            "ratePct": rate,
            "doneT": round(done) if matched else None,
            "coveredPlanT": round(covered_plan) if covered_plan else None,
            "coveragePct": round(covered_plan / plan * 100) if plan else None,
            "unmatchedResults": len(unmatched),
            "rates": allr,
        }
    buy, sell = out["buy"], out["sell"]
    net = None
    if buy["doneT"] is not None or sell["doneT"] is not None:
        net = round((buy["doneT"] or 0) - (sell["doneT"] or 0))
    return {
        "day": day,
        "buySessions": buy["sessions"], "sellSessions": sell["sessions"],
        "swapSessions": len([r for r in sess if r["kind"] == "swap"]),
        "cancelSessions": len([r for r in rows if r["day"] == day and r["kind"] == "cancel"]),
        "planBuyT": buy["planT"], "planSellT": sell["planT"],
        "rateBuyPct": buy["ratePct"], "rateSellPct": sell["ratePct"],
        "doneBuyT": buy["doneT"], "doneSellT": sell["doneT"],
        "coverageBuyPct": buy["coveragePct"], "coverageSellPct": sell["coveragePct"],
        "uncoveredBuyT": round((buy["planT"] or 0) - (buy["coveredPlanT"] or 0)) or None,
        "unmatchedResults": buy["unmatchedResults"] + sell["unmatchedResults"],
        "netInT": net,
        "importSellT": next((r["qty"] for r in sess if r["kind"] == "sell" and r["import"] and r["qty"]), None),
    }


def _load_state() -> dict:
    if not STATE.exists():
        return {}
    try:
        return json.loads(STATE.read_text("utf-8"))
    except Exception:
        return {}


def snapshot(rows: list, prev_state: dict | None = None) -> dict:
    today = date.today()
    days = sorted({r["day"] for r in rows})
    cur = day_stats(rows, today.isoformat())
    # 前瞻：已公告但交易日未到（明天/后天的采购排期 = 供给与收储节奏的先行指标）
    fwd = []
    for d in [x for x in days if x > today.isoformat()][:5]:
        st = day_stats(rows, d)
        fwd.append({"day": d, "planBuyT": st["planBuyT"], "planSellT": st["planSellT"],
                    "buySessions": st["buySessions"], "sellSessions": st["sellSessions"],
                    "swapSessions": st["swapSessions"]})
    # 历史：自己落盘的逐日快照（首页只留最近几十条公告，回溯不了，只能靠日积月累）
    st_old = prev_state if prev_state is not None else _load_state()
    hist = st_old.get("history") or []
    keep = {h["day"]: h for h in hist if h.get("day")}
    keep[today.isoformat()] = {
        "day": today.isoformat(),
        "planBuyT": cur["planBuyT"], "planSellT": cur["planSellT"],
        "doneBuyT": cur["doneBuyT"], "doneSellT": cur["doneSellT"],
        "netInT": cur["netInT"], "buySessions": cur["buySessions"],
        "importSellT": cur["importSellT"],
    }
    hist = sorted(keep.values(), key=lambda x: x["day"])[-15:]

    def agg(items):
        return {
            "buyPlanT": sum(h.get("planBuyT") or 0 for h in items) or None,
            "sellPlanT": sum(h.get("planSellT") or 0 for h in items) or None,
            "buySessions": sum(h.get("buySessions") or 0 for h in items) or None,
            "netInT": sum(h.get("netInT") or 0 for h in items) if any(
                h.get("netInT") is not None for h in items) else None,
            "days": len(items),
            "positiveDays": len([h for h in items if (h.get("netInT") or 0) > 0]),
        }

    r3 = [h for h in hist if h["day"] >= (today - timedelta(days=2)).isoformat()]
    r5 = [h for h in hist if h["day"] >= (today - timedelta(days=4)).isoformat()]
    return {
        "ok": True,
        "grain": GRAIN,
        "fetchedAt": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "source": "中储粮网 esinograin.com 竞价公告/结果",
        "today": cur,
        "forward": fwd,
        "history": hist,
        "recent3d": agg(r3),
        "recent5d": agg(r5),
        "rows": rows,
    }


def fetch_once(prev: dict | None = None) -> dict:
    c = Client()
    rows = collect(c, cache=(prev or {}).get("_rowCache") or {})
    snap = snapshot(rows, prev)
    snap["reqCount"] = c.req
    snap["_rowCache"] = {r["ggid"]: r for r in rows if r.get("ok")}
    try:
        STATE.write_text(json.dumps({"history": snap["history"], "fetchedAt": snap["fetchedAt"],
                                     "today": snap["today"], "forward": snap["forward"]},
                                    ensure_ascii=False, indent=1), "utf-8")
    except Exception:
        pass
    return snap


INTERVAL = 1200          # 20 分钟：公告一天发几批，结果中午前后出，20 分钟足够且请求量小
CACHE_TTL = 6 * 3600


class SinograinFeed:
    """常驻线程。首轮全量取正文（约 20~30 个请求 + 1.2s 间隔 ≈ 40s），
    之后每轮只有首页 1 个请求（正文按 ggid 缓存），抓到新公告才补取。"""

    def __init__(self, interval: int = INTERVAL):
        self.interval = interval
        self._lock = threading.Lock()
        st = _load_state()
        self._data = st or {"ok": False}
        self._data.setdefault("_rowCache", {})
        self._thread = None

    def start(self):
        self._thread = threading.Thread(target=self._loop, name="sinograin-feed", daemon=True)
        self._thread.start()

    def _loop(self):
        while True:
            try:
                with self._lock:
                    prev = dict(self._data)
                fresh = fetch_once(prev)
                with self._lock:
                    self._data = fresh
            except Exception:
                with self._lock:
                    self._data = dict(self._data, stale=True,
                                      lastError="fetch failed")
            time.sleep(self.interval)

    def snapshot(self) -> dict:
        with self._lock:
            d = dict(self._data)
        d.pop("_rowCache", None)
        if d.get("fetchedAt"):
            try:
                ts = time.mktime(time.strptime(d["fetchedAt"], "%Y-%m-%d %H:%M:%S"))
                d["ageSec"] = int(time.time() - ts)
                d["stale"] = bool(d.get("stale")) or d["ageSec"] > CACHE_TTL
            except ValueError:
                pass
        return d


if __name__ == "__main__":
    import sys
    if "--daemon" in sys.argv:
        SinograinFeed().start()
        print("中储粮抓取已启动，20 分钟一轮，Ctrl-C 退出")
        while True:
            time.sleep(3600)
    else:
        s = fetch_once()
        brief = {k: s[k] for k in ("fetchedAt", "today", "forward",
                                   "recent3d", "recent5d", "reqCount")}
        print(json.dumps(brief, ensure_ascii=False, indent=1))
        print("\n--- 明细 ---")
        for r in s["rows"]:
            print(f"{r['day']} {r['kind']:7s} {r['agent'][:10]:10s} "
                  f"{str(r['qty'] or ''):>8s} {str(r['rate'] or ''):>5s} "
                  f"{'进口' if r['import'] else '    '} {r['title'][:44]}")
