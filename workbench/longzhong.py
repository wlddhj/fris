#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""隆众资讯（oilchem.net）产业数据抓取 —— PE/PP 的库存与开工率。

为什么做这个
------------
产业信号里 L-1（PE产能利用率<75%）、L-2（两油库存两连降）、PP-1（PP商业库存两连降）、
PP-2（下游塑编/BOPP/注塑开工率>35%）一直靠人工盯。这四项隆众每天都发，但正文在
登录墙后面：服务端直接把 <div id="content"> 换成「免费开通/会员登录」表单，
匿名只能拿到标题和发布日期。所以本模块需要 config.json 里配一个登录 Cookie。

数据源（栏目 → 最新一期文章）
------------------------------
PE 产能利用率周数据     list.oilchem.net/328/27721/   周四发
PE 库存早报（两油库存）  list.oilchem.net/328/550/     每交易日 9-10 点
PP 库存（生产/贸易/港口） list.oilchem.net/329/38328/   周三/周四
PP 下游开工率           list.oilchem.net/329/569/     周三

反爬说明
--------
1. 响应是 brotli 压缩（Content-Encoding: br），Python 标准库没有 brotli 解码器，
   所以这里显式只声明 gzip/deflate，服务端会退回 gzip，标准库能解。
2. 正文按登录态渲染 —— 没有 Cookie 时返回登录墙，本模块会识别并标 needLogin，
   不会把登录表单当正文解析。
3. 栏目页与文章页间隔 1.5s，失败退避重试 2 次。

用法
----
    python workbench/longzhong.py --once     # 抓一轮打印，落 longzhong.json
    python workbench/longzhong.py --probe    # 只抓最新一期原文，存 lz_probe/ 便于校验解析
    python workbench/longzhong.py            # 常驻（库存类 30 分钟，周更类 4 小时）
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
STATE = BASE / "longzhong.json"
PROBE = BASE / "lz_probe"

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")

# 栏目定义：key -> (栏目列表页, 标题正则, 刷新间隔秒)
COLS = {
    "pe_cap":   ("https://list.oilchem.net/328/27721/",
                 r"聚乙烯生产企业产能利用率", 4 * 3600),
    "twooil":   ("https://list.oilchem.net/328/550/",
                 r"重点生产企业聚烯烃库存", 30 * 60),
    "pp_stock": ("https://list.oilchem.net/329/38328/",
                 r"聚丙烯生产企业库存", 4 * 3600),
    "pp_down":  ("https://list.oilchem.net/329/569/",
                 r"聚丙烯下游开工", 4 * 3600),
}

LOGIN_HINT = ("免费开通", "会员登录", "chrg")


def _cfg() -> dict:
    try:
        return json.loads(CONFIG.read_text(encoding="utf-8"))
    except Exception:
        return {}


def cookie() -> str:
    lz = (_cfg().get("longzhong") or {})
    return (lz.get("cookie") or "").strip()


def fetch(url: str, timeout: int = 25, tries: int = 3) -> str:
    """GET 并解码。只接受 gzip/deflate —— 标准库没有 brotli。

    限流是真实存在的坑：短时间连续请求 5 篇以上就会 429，所以对 429/5xx 做指数
    退避重试（3s、6s），而不是直接抛错让整轮抓取失败。
    """
    headers = {
        "User-Agent": UA,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9",
        "Accept-Encoding": "gzip, deflate",
        "Referer": "https://www.oilchem.net/",
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
    else:                      # 三次都失败
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
    """去标签取文字；中文空白（含全角空格、nbsp）压成单个空格。"""
    frag = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", frag, flags=re.S | re.I)
    frag = re.sub(r"<br\s*/?>|</p>|</tr>|</div>|</h\d>", "\n", frag, flags=re.I)
    frag = re.sub(r"</td>|</th>", "\t", frag, flags=re.I)
    frag = re.sub(r"<[^>]+>", "", frag)
    frag = frag.replace("\u3000", " ").replace("\xa0", " ").replace("&nbsp;", " ")
    frag = re.sub(r"[ \t]+", " ", frag)
    frag = re.sub(r"\n\s*\n+", "\n", frag)
    return frag.strip()


def latest_list(col: str, n: int = 3) -> list[dict]:
    """栏目页按「日期新→旧」取前 n 期，同日分项/合计择优（取合计那篇）。

    为什么需要不止一期：l2（两油库存）与 pp1（PP 生产企业库存）判的是「连续两期
    环比下降」。只看当期只能拿到一个变化值，要么等本地 history 攒够 3 期（日更的
    要等 3 天、周更的要等 3 周），要么直接去翻上一期原文——每篇原文都写着「较上期
    下降 X 万吨」，取回上一篇就有第二个变化值，当天即可判定。
    """
    url, pat, _ = COLS[col]
    html = fetch(url)
    rx = re.compile(r'<a[^>]+href="(https://www\.oilchem\.net/\d{2}-\d{4}-[^"]+\.html)"[^>]*>(.*?)</a>', re.S)
    best = {}          # date -> 该日期最优的一篇（合计优先）
    for m in rx.finditer(html):
        href, raw_title = m.group(1), html_text(m.group(2))
        if not re.search(pat, raw_title):
            continue
        # URL 形如 /26-1020-10-hash.html → 年月日
        d = re.match(r"https://www\.oilchem\.net/(\d{2})-(\d{2})(\d{2})-", href)
        key = f"{d.group(1)}{d.group(2)}{d.group(3)}" if d else "000000"
        # 同一天常常有「分项」和「合计」两篇（如 PP 库存：生产企业/贸易商/港口 各一篇 +
        # 一篇综合分析）。信号要的是合计口径，所以同日优先取综合分析。
        pref = 1 if re.search(r"库存周数据分析|库存数据分析|库存周数据统计\(|库存周数据统计（", raw_title) else 0
        cand = {"key": key, "pref": pref, "url": href, "title": raw_title}
        if key not in best or cand["pref"] > best[key]["pref"]:
            best[key] = cand
    return [best[k] for k in sorted(best, reverse=True)[:n]]


def latest(col: str) -> dict | None:
    """栏目页取最新一期。"""
    rows = latest_list(col, 1)
    return rows[0] if rows else None


def article(url: str) -> dict:
    """取正文，并三态归类：

    ok       正常，text 是正文
    login    匿名（或 Cookie 失效）—— 正文位置是「免费开通/会员登录」表单
    noperm   登录了但没订阅 —— 正文位置是「您没有开通该新闻的页面权限，延期请联系客服」

    noperm 是本项目实测遇到的真实状态：免费注册账号能登录，但数据类文章（库存、
    产能利用率、开工率）属于付费资讯，页面会提示「延期」——即订阅到期需续费。
    """
    html = fetch(url)
    m = re.search(r'<div[^>]+id="content"[^>]*>(.*?)<div[^>]+class="xq-', html, re.S)
    body = m.group(1) if m else ""
    if not body:
        m = re.search(r'<div[^>]+class="xq-content"[^>]*>(.*?)</div>\s*</div>', html, re.S)
        body = m.group(1) if m else html
    text = html_text(body)
    if "没有开通该新闻的页面权限" in body or "没有开通该新闻的页面权限" in text:
        return {"ok": False, "reason": "noperm", "needLogin": False,
                "text": "", "url": url,
                "note": "该账号未开通此篇资讯（页面提示「延期请联系客服」＝订阅到期或未订阅数据资讯）"}
    if any(k in body for k in LOGIN_HINT) and len(text) < 400:
        return {"ok": False, "reason": "login", "needLogin": True, "text": "", "url": url,
                "note": "需要登录：Cookie 为空或已过期"}
    return {"ok": bool(text), "reason": "ok" if text else "empty",
            "needLogin": False, "text": text, "url": url}


# ---------- 各项指标抽取 ----------
def _num(s):
    try:
        return float(s)
    except Exception:
        return None


# 涨跌方向词。隆众原文把方向写在数字前面好几个字之外，中间还夹着「库存」「了」
# 这类插入语（「与上期库存下降 1 万吨」「较上周期减少了 2.97 个百分点」），
# 把方向词写进正则会频繁匹配不到，所以统一改成「取数字位置往前扫 16 个字」判断符号。
_DOWN_W = ("下降", "减少", "回落", "走低", "下跌", "降低", "下滑", "收窄")
_UP_W = ("上涨", "增加", "回升", "走高", "上升", "提升")


def _signed(text: str, m) -> float | None:
    """数字组（第 1 组）按前文方向词补正负号。"""
    if not m:
        return None
    v = _num(m.group(1))
    if v is None:
        return None
    pre = text[max(0, m.start(1) - 16):m.start(1)]
    if any(w in pre for w in _DOWN_W):
        return -abs(v)
    if any(w in pre for w in _UP_W):
        return abs(v)
    return v


def parse_twooil(text: str) -> dict:
    """两油库存：本日值 + 较上期变化 + 去年同期。

    原文形如：「中国重点生产企业聚烯烃库存为 79 万吨，与上期库存下降 1 万吨。
    较去年同期 77 万吨上涨 2 万吨，同比上涨 2.60%。」
    """
    out = {}
    m = re.search(r"(?:两油库存|聚烯烃库存)[^0-9%]{0,14}(\d+(?:\.\d+)?)\s*万吨", text)
    if not m:
        m = re.search(r"库存[^0-9%]{0,10}(\d+(?:\.\d+)?)\s*万吨", text)
    if m:
        out["value"] = _num(m.group(1))
        out["unit"] = "万吨"
    dm = re.search(r"(?:与|较)(?:上期|上周期|前一工作日|昨日|上周同期|上周)"
                   r"[^0-9]{0,12}(\d+(?:\.\d+)?)\s*万吨", text)
    delta = _signed(text, dm)
    if delta is not None:
        out["delta"] = delta
        if out.get("value") is not None:
            out["prev"] = round(out["value"] - delta, 2)
    ym = re.search(r"去年同期[^0-9]{0,10}(\d+(?:\.\d+)?)\s*万吨", text)
    if ym:
        out["lastYear"] = _num(ym.group(1))
    return out


def parse_rate(text: str, label: str) -> dict:
    """通用百分比：label（如「产能利用率」「开工率」）后 20 字内的百分数 + 环比。"""
    out = {}
    m = re.search(label + r"[^0-9%]{0,18}(\d+(?:\.\d+)?)\s*%", text)
    if m:
        out["value"] = _num(m.group(1))
        out["unit"] = "%"
    dm = re.search(r"较(?:上周期|上期|上周同期|上周|上一周期)[^0-9]{0,12}"
                   r"(\d+(?:\.\d+)?)\s*(?:个?百分点|%)", text)
    delta = _signed(text, dm)
    if delta is not None:
        out["delta"] = delta
    return out


def parse_pp_down(text: str) -> dict:
    """PP 下游分项开工率：塑编 / 注塑 / BOPP / 管材 / 无纺布 / CPP / 透明 / 改性 / 共聚 + 行业平均。

    原文：「本周国内聚丙烯下游行业平均开工上涨 6.55 个百分点至 42.88%。」
    下面还有一张分项表（行业 · 本周 · 上周 · 涨跌），分项值取表格里各行业的第一列。
    """
    out = {}
    # 平均：正文说「…至 42.88%」，表格最后一行是「平均 / 42.88%」，两条路都留着
    m = (re.search(r"平均开工[^。]{0,40}?至\s*(\d+(?:\.\d+)?)\s*%", text)
         or re.search(r"\n\s*平均\s*(\d+(?:\.\d+)?)\s*%", text))
    if m:
        out["value"] = _num(m.group(1))
        out["unit"] = "%"
    dm = re.search(r"平均开工[^0-9]{0,10}(\d+(?:\.\d+)?)\s*个百分点", text)
    delta = _signed(text, dm)
    if delta is not None:
        out["delta"] = delta
    for name in ("塑编", "注塑", "BOPP", "管材", "无纺布", "CPP", "透明", "改性", "共聚"):
        mm = re.search(name + r"[^0-9%]{0,12}(\d+(?:\.\d+)?)\s*%", text)
        if mm:
            out[name] = _num(mm.group(1))
    return out


def parse_pp_stock(text: str) -> dict:
    """PP 库存：生产企业 / 贸易商 / 港口 三档（万吨）+ 环比。"""
    out = {}
    for name in ("生产企业", "贸易商", "港口", "商业"):
        m = re.search(name + r"[^0-9]{0,14}(\d+(?:\.\d+)?)\s*万吨", text)
        if m:
            out[name] = _num(m.group(1))
    m = re.search(r"库存[^0-9]{0,14}(\d+(?:\.\d+)?)\s*万吨", text)
    if "value" not in out and m:
        out["value"] = _num(m.group(1))
        out["unit"] = "万吨"
    dm = re.search(r"较上期[^0-9]{0,10}(\d+(?:\.\d+)?)\s*万吨", text)
    delta = _signed(text, dm)
    if delta is not None:
        out["delta"] = delta
        if out.get("value") is not None:
            out["prev"] = round(out["value"] - delta, 2)
    return out


PARSERS = {"pe_cap": lambda t: parse_rate(t, "产能利用率"),
           "twooil": parse_twooil,
           "pp_stock": parse_pp_stock,
           "pp_down": parse_pp_down}

# 这几个栏目判「连续两期环比下降」, 需要上一期的环比值 —— 多取一篇原文即可,
# 不必等本地 history 攒满 3 期（周更栏目那要等三周）。
NEED_PREV = ("twooil", "pp_stock")


def snapshot(prev: dict | None = None) -> dict:
    """抓一轮。返回 {配置状态, 各项数值, 更新时间}。

    prev 传上一轮的快照（longzhong.json）：栏目里最新一期文章 URL 与上轮相同就直接
    复用上轮的解析结果，不再去请求文章页。周更的三个栏目（产能利用率/库存/开工率）
    一周才变一次，这个复用能把每轮请求从 8 次压到 4 次左右，也躲开隆众的 429 限流。
    """
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
            rows = latest_list(col, 3)
            la = rows[0] if rows else None
            if not la:
                item["err"] = "栏目页未找到匹配文章"
            else:
                old = old_items.get(col) or {}
                if old.get("ok") and old.get("url") == la["url"]:
                    item = dict(old)   # 同一篇文章，直接复用（不打文章页）
                    item["parsed"] = dict(old.get("parsed") or {})   # 深一层，下面可能要改
                    item.pop("err", None)
                    item.pop("reason", None)
                else:
                    item.update({"title": la["title"], "url": la["url"], "date": la["key"]})
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
                # 上一期的环比：判「连续两期下降」用，取不到不影响当期数值。
                # 同样按 URL 复用，避免每轮都多打一次文章页。
                if item.get("ok") and col in NEED_PREV and len(rows) > 1:
                    nxt = rows[1]
                    keep = old.get("prev") or {}
                    if (keep.get("url") == nxt["url"]
                            and item.get("parsed", {}).get("prevDelta") is not None):
                        item["prev"] = keep
                    else:
                        time.sleep(1.5)
                        pv = article(nxt["url"])
                        if pv["ok"]:
                            pd = PARSERS[col](pv["text"])
                            if pd.get("delta") is not None:
                                item.setdefault("parsed", {})["prevDelta"] = pd["delta"]
                                item["prev"] = {"date": nxt["key"], "title": nxt["title"],
                                                "url": nxt["url"],
                                                "value": pd.get("value"), "delta": pd["delta"]}
                        elif pv.get("reason"):
                            item["prevErr"] = pv["reason"]
        except Exception as e:  # 网络/解析异常都不能拖垮其它栏目
            item["err"] = f"{type(e).__name__}: {e}"
        res["items"][col] = item
        time.sleep(1.2)
    reasons = {it.get("reason") for it in res["items"].values() if not it.get("ok")}
    if reasons == {"noperm"}:
        res["status"] = "noPermission"
        res["note"] = "账号可登录，但数据类资讯需订阅（页面提示「延期请联系客服」）"
    elif "login" in reasons:
        res["status"] = "needLogin"
        res["note"] = "Cookie 为空或已过期，需要重新登录"
    elif all(it.get("ok") for it in res["items"].values()):
        res["status"] = "ok"
    else:
        res["status"] = "partial"
    return res


def merge_history(new: dict) -> dict:
    """与旧文件合并，按月/日保留最近 12 个观测值（供「连续两期下降」判定）。"""
    try:
        old = json.loads(STATE.read_text(encoding="utf-8"))
    except Exception:
        old = {}
    hist = old.get("history") or {}
    for col, it in (new.get("items") or {}).items():
        p = it.get("parsed") or {}
        v = p.get("value", p.get("塑编"))
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


class LongzhongFeed(threading.Thread):
    daemon = True

    def __init__(self, interval: int = 1800):
        super().__init__(name="longzhong")
        self.interval = interval
        self.lock = threading.Lock()
        # 先把上次落盘的快照读进来：重启后到第一轮抓取完成（约 20s）之间，
        # 前端和信号判定用的是上一次的数值，而不是空白 + 一片 unknown。
        self.data = {}
        try:
            self.data = json.loads(STATE.read_text(encoding="utf-8"))
        except Exception:
            pass

    def run(self):
        while True:
            try:
                snap = merge_history(snapshot(self.get()))   # 传上轮结果，URL 没变就不重复抓
                with self.lock:
                    self.data = snap
                STATE.write_text(json.dumps(snap, ensure_ascii=False, indent=1), encoding="utf-8")
            except Exception as e:
                print("[longzhong] 抓取失败:", e, flush=True)
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
            print(f"{col}: {la['title']}  需登录={art['needLogin']}  字数={len(art['text'])}")
            time.sleep(1.5)
        return
    if "--once" in sys.argv:
        try:
            prev = json.loads(STATE.read_text(encoding="utf-8"))
        except Exception:
            prev = None
        snap = merge_history(snapshot(prev))
        STATE.write_text(json.dumps(snap, ensure_ascii=False, indent=1), encoding="utf-8")
        print(json.dumps(snap, ensure_ascii=False, indent=1)[:3000])
        return
    LongzhongFeed().run()


if __name__ == "__main__":
    main()
