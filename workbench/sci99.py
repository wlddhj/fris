#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""卓创资讯（sci99.com）产业数据抓取 —— 鸡蛋的淘鸡（老母鸡）出栏量。

为什么做这个
------------
产业信号 JD-2「淘鸡放量：周度淘鸡出栏量环比增加」一直靠人工盯。卓创每周四在
家禽频道发《全国代表市场老母鸡出栏量统计表》，正文是 22 个代表市场的出栏量
表格 + 样本总和 + 环比/同比。匿名访问正文容器（news_content）整个不渲染，
必须带登录 Cookie；账号 config.json → sci99（畜牧业主订阅）。

数据源（产品页 → 最新一期文章）
------------------------------
老母鸡出栏量周数据   ahd.sci99.com/product/culling_chicken   数据统计栏，周四发
文章 URL 形如 /info/1_41220_45788225_1.html（41220 是栏目 id，数字 id 单调递增，
直接用它排序取最新，比解析标题里的「2026年10月第2周」可靠）。

解析说明
--------
正文自带结论句：「本周全国代表市场老母鸡出栏量为 68.88 万只，环比增幅 1.03%，
同比减幅 5.84%」。方向词在数字前面（增幅=正、减幅=负），当期即可判「环比增加」，
不需要像隆众那样再翻上一期原文。

用法
----
    python workbench/sci99.py --once     # 抓一轮打印，落 sci99.json
    python workbench/sci99.py --probe    # 存最新一期原文，便于校验解析
    python workbench/sci99.py            # 常驻，30 分钟一轮（周数据足够新鲜）
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
STATE = BASE / "sci99.json"
PROBE = BASE / "zc_probe"

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")

# 栏目定义：key -> (产品页, 标题正则, 刷新间隔秒)
COLS = {
    "taoji": ("https://ahd.sci99.com/product/culling_chicken",
              r"老母鸡出栏量统计表", 4 * 3600),
    # 玉米基层售粮进度。卓创不在固定栏目发，而是「分析文章 + 数据统计」两种形态：
    #   数据统计型《主要产区玉米基层售粮进度统计（YYYYMMDD）》—— 分地区表格
    #   分析型《2025/26年度玉米售粮进度整体快于去年同期 本周提速放缓》—— 结论句带数字
    # 两者标题都含「玉米」+「售粮进度」。必须带玉米：聚合页里还有绿豆/谷子/红小豆的
    # 同款售粮进度统计（标题长得一模一样），只用「售粮进度」会把杂粮数据错当玉米。
    "corn_sell": ("https://primagri.sci99.com/product/corn?navpath=62524_62878_63101_12540",
                  r"玉米[^，。]{0,20}售粮进度|售粮进度[^，。]{0,20}玉米", 6 * 3600),
    # 鸡蛋现货：全国主产区均价。日报《全国蛋鸡日度简报（YYYYMMDD）》每天 15:30 前后发，
    # 正文第一句就是「主产区鸡蛋均价为 3.69 元/斤，较昨日价格下跌」——正是 jd1 要的口径，
    # 也是原信号写的「主产区均价」。产品页按日期同时挂着最近 4~5 期，一次抓完就有连续序列，
    # 不用等攒天数（生意社那边经常断，所以主源换成这里，生意社降级兜底）。
    "egg_price": ("https://ahd.sci99.com/product/egg", r"全国蛋鸡日度简报", 2 * 3600),
}

# 一个栏目可能要看多个列表页（产品页只保留最近 1 个月，聚合页跨度更大）
SCAN_PAGES = {
    "corn_sell": ["https://primagri.sci99.com/product/corn?navpath=62524_62878_63101_12540",
                  "https://primagri.sci99.com/chain/grain"],
}

# 需要多期才能判定的栏目：一次抓最近 N 篇，拼成序列（判「连续 3 日」至少要 4 期）
MULTI_COLS = {"egg_price": 5}

# 列表页一条都没匹配上时的说明（售粮进度是季节性指标，10 月新季刚开始时正常为空）
_MISS = {
    "corn_sell": "卓创本季尚未发布玉米售粮进度（新季首期一般在 11 月，随收购上量开更）",
    "egg_price": "卓创鸡蛋日报暂未找到（每个交易日 15:30 前后发布）",
}

# 无权限/登录的页面特征
NOPERM_HINT = ("暂无该栏目的查看权限", "没有开通", "请联系客户经理", "请订阅", "试用到期")
LOGIN_HINT = ("注册", "登录")


def _cfg() -> dict:
    try:
        return json.loads(CONFIG.read_text(encoding="utf-8"))
    except Exception:
        return {}


def cookie() -> str:
    zc = (_cfg().get("sci99") or {})
    return (zc.get("cookie") or "").strip()


def fetch(url: str, timeout: int = 25, tries: int = 3) -> str:
    """GET 并解码（gzip/deflate，同隆众套路）。429/5xx 指数退避。"""
    headers = {
        "User-Agent": UA,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9",
        "Accept-Encoding": "gzip, deflate",
        "Referer": "https://ahd.sci99.com/",
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


# 文章链接：/info/<栏目>_<栏目id>_<文章id>_1.html，文章 id 全站单调递增
_ART_RX = re.compile(r'href="((?:https?://[\w.]+)?/info/\d+_\d+_(\d+)_1\.html[^"]*)"[^>]*>(.*?)</a>', re.S)


def _abs(href: str, page: str) -> str:
    if href.startswith("http"):
        return href
    m = re.match(r"(https?://[\w.]+)", page)
    return (m.group(1) if m else "https://primagri.sci99.com") + href


def latest_list(col: str, n: int = 3) -> list[dict]:
    """取最新 n 期。文章数字 id 单调递增，直接按 id 排序（比标题里的日期可靠）。

    产品页只保留最近一个月，所以支持 SCAN_PAGES 多页合并去重。
    """
    url, pat, _ = COLS[col]
    pages = SCAN_PAGES.get(col) or [url]
    best = {}
    for page in pages:
        try:
            html = fetch(page)
        except Exception:
            continue
        for m in _ART_RX.finditer(html):
            href, aid, raw_title = _abs(m.group(1), page), int(m.group(2)), html_text(m.group(3))
            if not raw_title or not re.search(pat, raw_title):
                continue
            if aid not in best:
                best[aid] = {"key": str(aid), "url": href, "title": raw_title}
        time.sleep(0.8)
    return [best[k] for k in sorted(best, reverse=True)[:n]]


def latest(col: str) -> dict | None:
    rows = latest_list(col, 1)
    return rows[0] if rows else None


def article(url: str) -> dict:
    """取正文并三态归类（同隆众）：

    ok       正文容器 news_content 存在
    login    匿名/Cookie 失效 —— 正文容器整个不渲染（实测匿名页没有 news_content）
    noperm   已登录但订阅过期/无权限
    """
    html = fetch(url)
    i = html.find('class="news_content"')
    if i < 0:
        if any(k in html for k in NOPERM_HINT):
            return {"ok": False, "reason": "noperm", "needLogin": False, "text": "", "url": url,
                    "note": "账号无该栏目权限（畜牧业主订阅已到期或未开通）"}
        return {"ok": False, "reason": "login", "needLogin": True, "text": "", "url": url,
                "note": "需要登录：Cookie 为空或已过期（重跑 node zc_login.js）"}
    seg = html[i:i + 80000]
    text = html_text(seg)
    if any(k in text for k in NOPERM_HINT):
        return {"ok": False, "reason": "noperm", "needLogin": False, "text": "", "url": url,
                "note": "账号无该栏目权限（畜牧业主订阅已到期或未开通）"}
    return {"ok": bool(text), "reason": "ok" if text else "empty",
            "needLogin": False, "text": text, "url": url}


# ---------- 指标抽取 ----------
def _num(s):
    try:
        return float(s)
    except Exception:
        return None


def parse_taoji(text: str) -> dict:
    """老母鸡出栏量：样本总和 + 环比 + 同比（万只）。

    结论句：「本周全国代表市场老母鸡出栏量为68.88万只，环比增幅1.03%，同比减幅5.84%」。
    表格兜底行：「样本总和 \t 68.88 \t 1.03% \t -5.84%」。
    """
    out = {}
    m = (re.search(r"老母鸡出栏量为?\s*(\d+(?:\.\d+)?)\s*万只", text)
         or re.search(r"样本总和\s*[\t|]\s*(\d+(?:\.\d+)?)", text))
    if m:
        out["value"] = _num(m.group(1))
        out["unit"] = "万只"
    dm = re.search(r"环比(增幅|涨幅|减幅|跌幅|增加|下降|上升|下滑)?\s*(\d+(?:\.\d+)?)\s*%", text)
    if dm:
        v = _num(dm.group(2))
        if v is not None and dm.group(1) in ("减幅", "跌幅", "下降", "下滑"):
            v = -abs(v)
        out["deltaPct"] = v
    ym = re.search(r"同比(增幅|涨幅|减幅|跌幅|增加|下降|上升|下滑)?\s*(\d+(?:\.\d+)?)\s*%", text)
    if ym:
        v = _num(ym.group(2))
        if v is not None and ym.group(1) in ("减幅", "跌幅", "下降", "下滑"):
            v = -abs(v)
        out["yoy"] = v
    # 兜底：表格行（正文结论句缺失时）
    if "deltaPct" not in out:
        tm = re.search(r"样本总和\s*[\t|]\s*(\d+(?:\.\d+)?)\s*[\t|]\s*(-?[\d.]+)%", text)
        if tm:
            if "value" not in out:
                out["value"] = _num(tm.group(1))
            out["deltaPct"] = _num(tm.group(2))
    if "value" in out and "deltaPct" in out and out["value"] is not None:
        out["prev"] = round(out["value"] / (1 + out["deltaPct"] / 100), 2)
    return out


def parse_sell_progress(text: str) -> dict:
    """玉米基层售粮进度（%）：东北 / 华北 / 两区平均 + 同比 + 截止日 + 年度。

    原文（2025/11/20 那期，实测）：
      「就东北、华北两大地区玉米平均销售进度来看，2025/26年度玉米平均销售进度23.5%，
        去年同期为19%」
      「截止到11月20日，东北产区平均售粮进度23%，较去年同期偏快4个百分点」
      「截止到11月20日，华北地区平均售粮进度24%，较去年同期快5个百分点」

    注意地区名和「进度」之间会夹「产区/地区/平均」等词，窗口给 20 字；
    跨句（。；）就截断，避免把玉米淀粉、深加工开工率之类的其他百分比吃进来。
    """
    out = {}
    m = re.search(r"玉米平均(?:销售|售粮)进度(?:为)?\s*(\d+(?:\.\d+)?)\s*%", text)
    if m:
        out["avg"] = _num(m.group(1))
    for key, kw in (("ne", "东北"), ("hua", "华北")):
        mm = re.search(kw + r"[^。；]{0,20}?平均(?:销售|售粮)进度(?:为)?\s*(\d+(?:\.\d+)?)\s*%", text)
        if mm:
            out[key] = _num(mm.group(1))
            # 同句里的同比：偏快/快 = 正，偏慢/慢 = 负（单位：个百分点）
            dy = re.search(kw + r"[^。；]{0,40}?较去年同期\s*(偏快|快|偏慢|慢|高|低|增加|下降)?\s*"
                           r"(\d+(?:\.\d+)?)\s*个百分点", text)
            if dy:
                v = _num(dy.group(2))
                if v is not None and dy.group(1) in ("偏慢", "慢", "低", "下降"):
                    v = -v
                out[key + "Yoy"] = v
    ym = re.search(r"去年同期为\s*(\d+(?:\.\d+)?)\s*%", text)
    if ym:
        out["lastYear"] = _num(ym.group(1))
    sm = re.search(r"(20\d{2}/2\d)\s*年度", text)
    if sm:
        out["season"] = sm.group(1)
    dm = re.search(r"截止到?\s*(\d{1,2})月(\d{1,2})日", text)
    if dm:
        out["asOf"] = f"{int(dm.group(1))}月{int(dm.group(2))}日"
    # 主值取两区平均；只有分地区值时就取两者较高者（卖压最早释放的那一头）
    if "avg" in out:
        out["value"] = out["avg"]
    elif out.get("ne") is not None or out.get("hua") is not None:
        out["value"] = max(v for v in (out.get("ne"), out.get("hua")) if v is not None)
    if "value" in out:
        out["unit"] = "%"
    return out


def parse_egg_daily(text: str) -> dict:
    """全国蛋鸡日度简报 → 主产区鸡蛋均价（元/斤）及当日配套数据。

    原文（2026-10-20 实测）：
      「今日全国鸡蛋价格多数稳定，主产区鸡蛋均价为3.69元/斤，较昨日价格下跌；
        主销区均价3.92元/斤，较昨日价格走低；主产区老母鸡价格多数稳，少数地区
        窄幅调整，老母鸡日均价4.69元/斤，较昨日价格持平。」
    涨跌是定性表述（上涨/下跌/稳定），只有个别日子才带「0.01元/斤」的幅度，
    所以 delta 只记方向（+1/-1/0），判「连续不创新低」用价格序列本身，不靠幅度。

    单位换算：卓创报元/斤，大商所鸡蛋期货报元/500kg，1 元/斤 = 1000 元/500kg。
    """
    out = {}
    m = re.search(r"主产区鸡蛋均价\s*(?:为)?\s*(\d+(?:\.\d+)?)\s*元/斤", text)
    if not m:
        # 极少数期写成「全国主产区鸡蛋均价 X 元/斤」
        m = re.search(r"鸡蛋均价\s*(?:为)?\s*(\d+(?:\.\d+)?)\s*元/斤", text)
    if m:
        out["value"] = _num(m.group(1))
        out["unit"] = "元/斤"
        out["spotFut"] = round(out["value"] * 1000, 0)      # 元/500kg，供算基差
    dm = re.search(r"(?:主产区鸡蛋均价|鸡蛋均价)[^。；]{0,12}?较(?:昨日|上一?工作日|前一日)"
                   r"(?:价格)?\s*(上涨|下跌|走高|走低|上涨|下滑|持平|稳定)", text)
    if dm:
        w = dm.group(1)
        out["delta"] = 1 if w in ("上涨", "走高") else (-1 if w in ("下跌", "走低", "下滑") else 0)
        out["deltaWord"] = w
        am = re.search(r"较(?:昨日|上一?工作日|前一日)[^。；]{0,10}?[涨跌]\s*(\d+(?:\.\d+)?)\s*元/斤", text)
        if am:
            v = _num(am.group(1))
            out["deltaAbs"] = (v if out["delta"] >= 0 else -v) if v is not None else None
    sm = re.search(r"主销区均价\s*(?:为)?\s*(\d+(?:\.\d+)?)\s*元/斤", text)
    if sm:
        out["xiaoqu"] = _num(sm.group(1))
    om = re.search(r"老母鸡日均价\s*(?:为)?\s*(\d+(?:\.\d+)?)\s*元/斤", text)
    if om:
        out["cullPrice"] = _num(om.group(1))
    km = re.search(r"生产环节平均库存\s*(\d+(?:\.\d+)?)\s*天", text)
    if km:
        out["stockProd"] = _num(km.group(1))
    km2 = re.search(r"流通环节(?:平均)?库存\s*(\d+(?:\.\d+)?)\s*天", text)
    if km2:
        out["stockFlow"] = _num(km2.group(1))
    return out


PARSERS = {"taoji": parse_taoji, "corn_sell": parse_sell_progress,
           "egg_price": parse_egg_daily}


def title_date(title: str) -> str | None:
    """标题里的日期：优先「（20261020）」，其次「2026年10月第2周」。"""
    t = title or ""
    m = re.search(r"[（(](20\d{2})(\d{2})(\d{2})[）)]", t)
    if m:
        return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
    wm = re.search(r"(\d{4})年(\d{1,2})月第(\d)周", t)
    if wm:
        return f"{wm.group(1)}-{int(wm.group(2)):02d}-W{wm.group(3)}"
    return None


def _multi_item(col: str, n: int, old: dict) -> dict:
    """多期栏目：抓最近 n 篇拼成序列。

    已抓过的文章按 URL 复用，所以稳定状态下每轮只多抓 1 篇（新的那天）。
    这也意味着首轮就有完整序列——不用像纯增量方案那样等攒够 4 天才能判定。
    """
    item = {"col": col, "ok": False, "series": []}
    try:
        rows = latest_list(col, n)
    except Exception as e:
        item["err"] = f"{type(e).__name__}: {e}"
        return item
    if not rows:
        item["err"] = _MISS.get(col, "产品页未找到匹配文章")
        item["pending"] = True
        return item

    cached = {s.get("url"): s for s in (old.get("series") or []) if s.get("url")}
    series, errs = [], []
    for r in rows:
        c = cached.get(r["url"])
        if c and c.get("price") is not None:
            series.append(c)
            continue
        art = article(r["url"])
        if not art["ok"]:
            errs.append(art.get("note") or art["reason"])
            continue
        p = PARSERS[col](art["text"])
        if p.get("value") is None:
            errs.append(f"正文未解析出价格：{r['title'][:26]}")
            continue
        series.append({"date": title_date(r["title"]), "url": r["url"], "title": r["title"],
                       "price": p.get("value"), "unit": p.get("unit"),
                       "spotFut": p.get("spotFut"), "delta": p.get("delta"),
                       "xiaoqu": p.get("xiaoqu"), "cullPrice": p.get("cullPrice"),
                       "stockProd": p.get("stockProd"), "stockFlow": p.get("stockFlow")})
        time.sleep(1.0)

    series = [s for s in series if s.get("date")]
    series.sort(key=lambda s: s["date"])
    item["series"] = series[-8:]
    if not series:
        item["err"] = errs[0] if errs else _MISS.get(col, "未取到数据")
        return item
    last = item["series"][-1]
    item.update({
        "ok": True, "title": last.get("title"), "url": last.get("url"),
        "date": last.get("date"),
        "parsed": {"value": last.get("price"), "unit": last.get("unit"),
                   "spotFut": last.get("spotFut"), "delta": last.get("delta"),
                   "xiaoqu": last.get("xiaoqu"), "cullPrice": last.get("cullPrice"),
                   "stockProd": last.get("stockProd"), "stockFlow": last.get("stockFlow")},
        "snippet": f"序列 {len(item['series'])} 期：" + " → ".join(
            f"{s['date'][5:]} {s['price']:.2f}" for s in item["series"]),
        "chars": 0,
    })
    if errs:
        item["warn"] = errs[:2]
    return item


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
            if col in MULTI_COLS:
                item = _multi_item(col, MULTI_COLS[col], old_items.get(col) or {})
                res["items"][col] = item
                time.sleep(1.2)
                continue
            rows = latest_list(col, 2)
            la = rows[0] if rows else None
            if not la:
                item["err"] = _MISS.get(col, "产品页未找到匹配文章")
                # 季节性栏目本期尚未发布：不算故障，别把整块状态拉成「部分栏目缺失」
                item["pending"] = True
            else:
                old = old_items.get(col) or {}
                if old.get("ok") and old.get("url") == la["url"]:
                    item = dict(old)
                    item.pop("err", None)
                    item.pop("reason", None)
                else:
                    item.update({"title": la["title"], "url": la["url"]})
                    art = article(la["url"])
                    if not art["ok"]:
                        item["err"] = art.get("note") or art["reason"]
                        item["reason"] = art["reason"]
                        if art["reason"] == "login":
                            item["needLogin"] = True
                    else:
                        item["ok"] = True
                        item["parsed"] = PARSERS[col](art["text"])
                        # 期数标记：标题「2026年10月第2周」→ date=2026-10-W2
                        wm = re.search(r"(\d{4})年(\d{1,2})月第(\d)周", la["title"])
                        if wm:
                            item["date"] = f"{wm.group(1)}-{int(wm.group(2)):02d}-W{wm.group(3)}"
                        else:
                            # 数据统计型标题带「（20261015）」→ 2026-10-15
                            dm = re.search(r"[（(](20\d{2})(\d{2})(\d{2})[）)]", la["title"])
                            if dm:
                                item["date"] = f"{dm.group(1)}-{dm.group(2)}-{dm.group(3)}"
                        item["snippet"] = art["text"][:220].replace("\n", " ")
                        item["chars"] = len(art["text"])
        except Exception as e:
            item["err"] = f"{type(e).__name__}: {e}"
        res["items"][col] = item
        time.sleep(1.2)
    reasons = {it.get("reason") for it in res["items"].values()
               if not it.get("ok") and not it.get("pending")}
    live = [it for it in res["items"].values() if not it.get("pending")]
    if reasons == {"noperm"}:
        res["status"] = "noPermission"
        res["note"] = "账号可登录，但该栏目需畜牧业主订阅（当前试用 2026-10-27 到期）"
    elif "login" in reasons:
        res["status"] = "needLogin"
        res["note"] = "Cookie 为空或已过期，重跑 node zc_login.js"
    elif all(it.get("ok") for it in live):
        res["status"] = "ok"
    else:
        res["status"] = "partial"
    return res


def merge_history(new: dict) -> dict:
    """与旧文件合并，保留最近 12 期（样本总和）。"""
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
        stamp = it.get("date") or new["at"][:10]
        if not arr or arr[-1].get("d") != stamp:
            arr.append({"d": stamp, "v": v,
                        "extra": {k: x for k, x in p.items()
                                  if k not in ("value", "unit", "prev")}})
        else:
            arr[-1]["v"] = v
        hist[col] = arr[-12:]
    new["history"] = hist
    return new


class Sci99Feed(threading.Thread):
    daemon = True

    def __init__(self, interval: int = 1800):
        super().__init__(name="sci99")
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
                print("[sci99] 抓取失败:", e, flush=True)
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
    Sci99Feed().run()


if __name__ == "__main__":
    main()
