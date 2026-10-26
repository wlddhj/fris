"""产业信号自动判定 —— 唯一事实来源：signal id -> 判定函数

设计原则
--------
1. 只自动「行情数据能客观验证」的条件。现货报价、开工率、库存、压榨利润、
   收储公告、售粮进度这类产业数据没有公开接口，继续走人工确认，不编造数值。
2. 阈值写死、可复现，detail 里带出真实数值，方便盘后回头复核。
3. 任何一环数据缺失就返回 state="unknown"，不猜、不用代理数据冒充原条件
   （唯一例外 pp3：布伦特无行情源，改用上海原油 SC，文案已同步改写注明）。

state 语义
----------
hit      条件已成立（页面亮灯）
miss     条件未成立（灰灯，detail 说明差在哪）
unknown  数据不足/未接通（黄灯，不参与计数）
ref      仅参考值，不参与自动判定（m3 美豆用豆二代理）
"""
from __future__ import annotations

import re

import pandas as pd

VOL_LOOKBACK = 20      # 均量基准窗口（日）
VOL_SURGE = 1.5        # 放量标准：达到 20 日均量的 1.5 倍
VOL_SHRINK = 0.60      # 缩量标准：降至放量日的 60% 以内
OI_DROP_PCT = -0.5     # 持仓至少下降 0.5%
RECENT_BARS = 10       # 只在最近 10 根日线里找"放量下跌日"


def _daily(kdf, n=80):
    """取最近 n 根有效日线。"""
    if kdf is None or len(kdf) == 0:
        return None
    df = kdf[kdf["datetime"] > 0]
    df = df.dropna(subset=["open", "high", "low", "close", "volume"])
    if len(df) < 5:
        return None
    return df.tail(n).reset_index(drop=True)


def _day_label(ns):
    try:
        return pd.Timestamp(int(ns)).tz_localize("UTC").tz_convert("Asia/Shanghai").strftime("%m-%d")
    except Exception:
        return "?"


def _ext(ext, key):
    """取外参，缺数据/0/nan 一律当没有。

    行情断线或休市重连时 TqSdk 的 last_price 会变成 0 甚至 nan；nan 过不了
    `in (None, 0)` 的判断，会一路算成 "+nan 元/吨" 还判出 miss —— 假的确定结论
    比没有结论更糟，所以在这里卡死。
    """
    e = (ext or {}).get(key)
    if not e:
        return None
    last = e.get("last")
    if last is None or last != last or last == 0:
        return None
    return e


def _q_sym(quotes, prod):
    """从主合约行情里按品种代码挑 symbol，如 prod="M" -> "DCE.m2701"。

    用 fullmatch 卡死「字母 + 纯数字」，否则 prod="C" 会误吃 "CS2701"；
    比较时统一转大写，因为 TqSdk 的 symbol 是 DCE.m2701（小写品种）而配置里写 M2701。
    """
    for s in (quotes or {}):
        if re.fullmatch(rf"{prod.upper()}\d+", s.split(".")[-1].upper()):
            return s
    return None


def _q_last(quotes, prod, klines=None):
    """主合约最新价。休市/断线时 last_price 会变 0 或 nan，这时退回日线最后收盘。

    日盘 15:00 收盘到夜盘 21:00 开盘这 6 小时里 TqSdk 不给实时价，若不做兜底，
    m2/c3 每天下午都会掉成"数据不足"。收盘价就是那时的最新价，用它算并不失真，
    只是要在 detail 里标成收盘口径。
    """
    s = _q_sym(quotes, prod)
    if not s:
        return None
    try:
        v = float(getattr(quotes[s], "last_price", 0) or 0)
    except Exception:
        v = 0
    if v > 0 and v == v:
        return v
    df = _daily((klines or {}).get((s, "86400")), 5)
    if df is None or len(df) == 0:
        return None
    c = df["close"].iloc[-1]
    try:
        c = float(c)
    except Exception:
        return None
    return c if c > 0 and c == c else None


def _q_live(quotes, prod):
    """只看实时价（不回退），用来判断该不该标"收盘口径"。"""
    s = _q_sym(quotes, prod)
    if not s:
        return None
    try:
        v = float(getattr(quotes[s], "last_price", 0) or 0)
    except Exception:
        return None
    return v if (v > 0 and v == v) else None


def _date_close(df, n=140):
    """日线 → {日期: 收盘价}，日期按北京时间。用于多腿对齐算历史。"""
    d = _daily(df, n)
    if d is None:
        return {}
    out = {}
    for _, r in d.iterrows():
        c = r.get("close")
        if c is None or c != c or c <= 0:
            continue
        try:
            day = (pd.Timestamp(int(r["datetime"])).tz_localize("UTC")
                   .tz_convert("Asia/Shanghai").strftime("%Y-%m-%d"))
        except Exception:
            continue
        out[day] = float(c)
    return out


def _pct_rank(vals, x):
    """x 在 vals 里所处分位（0~100）。"""
    if not vals:
        return None
    return sum(1 for v in vals if v <= x) / len(vals) * 100.0


# ---------------- 规则 ----------------

def vol_stall(ctx):
    """jd3 缩量企稳：放量下跌日之后，量能萎缩 + 持仓回落。纯技术面，五品种通用。"""
    df = _daily(ctx.get("kdf"))
    min_bars = VOL_LOOKBACK + RECENT_BARS
    if df is None or len(df) <= min_bars:
        return {"state": "unknown", "detail": f"日线不足 {min_bars + 1} 根，样本太短不判"}
    df = df.iloc[:-1]      # 剔除当日进行中的日K：盘中量天然偏小，否则"缩量"会永远成立
    vol = df["volume"].to_numpy(dtype=float)
    op = df["open"].to_numpy(dtype=float)
    cl = df["close"].to_numpy(dtype=float)
    oi = df["close_oi"].to_numpy(dtype=float) if "close_oi" in df.columns else None
    n = len(df)

    surge = None
    surge_base = 0.0
    for i in range(n - RECENT_BARS, n):        # 取最近一次放量阴线
        if i < VOL_LOOKBACK:
            continue
        base = vol[i - VOL_LOOKBACK:i].mean()
        if base > 0 and cl[i] < op[i] and vol[i] >= base * VOL_SURGE:
            surge, surge_base = i, base
    if surge is None:
        return {"state": "miss",
                "detail": f"近 {RECENT_BARS} 日无放量阴线（量≥{VOL_LOOKBACK} 日均量 {VOL_SURGE} 倍）"}
    if surge == n - 1:
        # 放量阴线就是最新一根：还没有"之后"，缩量无从谈起，不能拿它跟自己比
        return {"state": "miss",
                "detail": f"{_day_label(df['datetime'].iloc[surge])} 刚放量下跌"
                          f"（量 {vol[surge]/1e4:.1f} 万手，{vol[surge]/surge_base:.1f}×均量），"
                          f"尚无后续交易日可判缩量"}

    shrink = (vol[n - 1] / vol[surge]) if vol[surge] else 1.0
    oi_pct = None
    if oi is not None and oi[surge]:
        oi_pct = (oi[n - 1] - oi[surge]) / oi[surge] * 100.0

    hit_vol = shrink <= VOL_SHRINK
    hit_oi = oi_pct is not None and oi_pct <= OI_DROP_PCT
    detail = (f"放量阴线 {_day_label(df['datetime'].iloc[surge])}：量 {vol[surge]/1e4:.1f} 万手 → "
              f"最新 {vol[-1]/1e4:.1f} 万手（{shrink*100:.0f}%）；"
              f"持仓 {f'{oi_pct:+.1f}%' if oi_pct is not None else '缺失'}")
    if hit_vol and hit_oi:
        return {"state": "hit", "detail": "量能萎缩且持仓回落｜" + detail}
    reason = "量已缩至 60% 内" if hit_vol else f"量仍有 {shrink*100:.0f}%、未缩至 60%"
    reason += "、持仓已降" if hit_oi else "、持仓未降"
    return {"state": "miss", "detail": f"{reason}｜{detail}"}


def sc_break_high(ctx):
    """l3 成本抬升：SC 原油突破近 20 日高点。"""
    e = _ext(ctx.get("ext"), "SC")
    if not e:
        return {"state": "unknown", "detail": "上海原油 SC 行情未接通"}
    last, hi = e["last"], e.get("high20")
    if not hi:
        return {"state": "unknown", "detail": "SC 日线不足 20 根"}
    gap = (last / hi - 1) * 100
    if last > hi:
        return {"state": "hit", "detail": f"SC 主连 {last} 上破前 20 日高 {hi}（+{gap:.1f}%）"}
    return {"state": "miss", "detail": f"SC 主连 {last}，距前 20 日高 {hi} 还差 {abs(gap):.1f}%"}


def brent_cost(ctx):
    """pp3 成本支撑：布伦特站上 100 美元（原条件）且站上 20 日均线（时效过滤）。

    只用绝对关口会钝化——布伦特长期在 100 上方时这条会永远亮灯，所以加一条
    均线过滤；两条都成立才算"成本支撑"。
    """
    e = _ext(ctx.get("ext"), "BRENT")
    if not e:
        return {"state": "unknown", "detail": "新浪外盘布伦特未接通"}
    last, ma = e.get("last"), e.get("ma20")
    if not last:
        return {"state": "unknown", "detail": "布伦特实时价缺失"}
    if not ma:
        return {"state": "unknown", "detail": "布伦特日线不足 20 根，无法判均线"}
    over = last - 100
    gap = (last / ma - 1) * 100
    head = (f"布伦特 {last}（关口 100 {'上方' if over >= 0 else '下方'} {over:+.1f}；"
            f"MA20 {ma}，{gap:+.1f}%）")
    if over > 0 and last >= ma:
        return {"state": "hit", "detail": head + "，成本端支撑成立"}
    fail = "已跌破 100 关口" if over <= 0 else "低于 20 日均线"
    return {"state": "miss", "detail": head + f"，{fail}，成本支撑不成立"}


def cbot_resurgence(ctx):
    """m3 美豆反弹：上破前 20 日高。

    原条件"突破 1000 美分"是绝对阈值，美豆价格中枢上移后早已钝化（现价 1250 上下），
    永远成立等于没有信号，所以主判定改用"上破 20 日高"，1000 关口降为备注列出。
    """
    e = _ext(ctx.get("ext"), "CBOT_S")
    if not e:
        return {"state": "unknown", "detail": "新浪外盘 CBOT 大豆未接通"}
    last, hi = e.get("last"), e.get("high20")
    if not last:
        return {"state": "unknown", "detail": "美豆实时价缺失"}
    if not hi:
        return {"state": "unknown", "detail": "美豆日线不足 20 根，无法判前高"}
    gap = (last / hi - 1) * 100
    note = f"1000 关口：{last:.1f} 美分（{(last / 1000 - 1) * 100:+.1f}%，已{'在上方' if last >= 1000 else '跌破'}，阈值钝化）"
    if last > hi:
        return {"state": "hit", "detail": f"美豆 {last} 美分上破前 20 日高 {hi}（+{gap:.1f}%）｜{note}"}
    return {"state": "miss", "detail": f"美豆 {last} 美分，距前 20 日高 {hi} 还差 {abs(gap):.1f}%｜{note}"}


def spot_stop_fall(ctx):
    """jd1 现货止跌：全国主产区鸡蛋均价连续 3 日不再创新低。

    口径
    ----
    主源用卓创《全国蛋鸡日度简报》的「主产区鸡蛋均价」（元/斤）——这正是原信号写的
    口径，日报每个交易日 15:30 前后发布，卓创产品页同时挂着最近 4~5 期，一次抓完
    就有连续序列。生意社基准价降为兜底（它常常抓不到，而且口径是「基准价」，
    不全等于主产区均价）。

    判定
    ----
    「连续 3 日不再创新低」按定义逐日判：最近 3 个观测日，每一天的价格都不低于
    它在序列中的前低。只要中间有一天破了前低，就不算止跌（比如 3.89→3.89→3.70→3.69
    里 3.70 已经破过 3.89，即使最后一天只是小跌 0.01 也不能算止跌）。
    这个口径比「最新 vs 前 3 日最低」严，但更贴合「连续」二字的原意。
    """
    it = _zc_item(ctx, "egg_price")
    series = [s for s in (it.get("series") or []) if s.get("price") is not None]
    if len(series) >= 4:
        seq = series[-4:]
        px = [float(s["price"]) for s in seq]
        days = [str(s.get("date") or "")[5:] for s in seq]
        path = "→".join(f"{d} {p:.2f}" for d, p in zip(days, px))
        # 最近 3 个观测日：各自对比它在序列里的前低
        breaks = []
        for i in range(len(px) - 3, len(px)):
            low_before = min(px[:i])
            if px[i] < low_before:
                breaks.append((days[i], px[i], low_before))
        head = f"卓创主产区鸡蛋均价 {path} 元/斤"
        tail = f"｜前低 {min(px[:-1]):.2f}，折盘面 {px[-1] * 1000:.0f} 元/500kg"
        if not breaks:
            return {"state": "hit", "detail": head + "，连续 3 日未创新低，现货止跌" + tail}
        d, v, low = breaks[-1]
        return {"state": "miss",
                "detail": head + f"，{d} 的 {v:.2f} 低于此前低点 {low:.2f}，仍在创新低" + tail}

    # 兜底：卓创序列不足（首轮还没抓全 / Cookie 过期）时用生意社
    hist = (ctx.get("spot") or {}).get("jdHist")
    if hist and len(hist) >= 4:
        seq = sorted(hist, key=lambda r: tuple(r["mdKey"]))[-4:]
        px = [float(r["price"]) for r in seq]
        last, low_before = px[-1], min(px[:-1])
        path = "→".join(f"{p:.2f}" for p in px)
        note = "｜卓创序列不足，暂用生意社基准价兜底"
        if last >= low_before:
            return {"state": "hit",
                    "detail": f"生意社鸡蛋基准价 {path} 元/公斤，最新未创新低（前低 {low_before:.2f}）" + note}
        return {"state": "miss",
                "detail": f"生意社鸡蛋基准价 {path} 元/公斤，最新 {last:.2f} 破前低 {low_before:.2f}" + note}

    if not it.get("ok"):
        return {"state": "unknown", "detail": _zc_missing(ctx, "egg_price")}
    return {"state": "unknown",
            "detail": f"卓创鸡蛋均价仅 {len(series)} 期，不足以判连续 3 日（日报每交易日 15:30 发布）"}


STORE_BIG_T = 50000     # 单场采购计划量达 5 万吨 = 集团/储运级别集中收购，不是分公司常规轮换
STORE_PLAN_T = 150000   # 今日~未来两日采购计划量合计 15 万吨 = 收储实际放量


def sino_storage(ctx):
    """c1 收储启动：中储粮玉米竞价采购是否实质放量。

    为什么不能"发布公告即成立"
    ------------------------
    中储粮分公司几乎每个交易日都有玉米竞价采购专场，那是**常规轮换**（卖旧买新），
    一年到头都在发。原条件「中储粮发布收储公告」照字面判会天天亮灯，等于没信号。
    所以主判定换成量级：
      · 单场计划量 ≥ 5 万吨（储运/集团公司级别的集中收购），或
      · 今日 ~ 未来两个交易日采购计划量合计 ≥ 15 万吨
    两条任一成立算"收储启动"。阈值写在 detail 里，盘后可以按实际节奏调。

    另外两个必须一起看的量：
      · 净轮入 = 采购成交量 − 销售成交量。销售里常混着「进口玉米定向拍卖」，
        那是抛储、是供给，跟收储方向相反，单看采购量会误判。
      · 成交率覆盖率：结果公告当天中午前后才陆续出，覆盖不足时成交率不能当定论。
    """
    s = ctx.get("sino") or {}
    if not s.get("ok"):
        return {"state": "unknown", "detail": "中储粮公告未接通（首轮抓取约 40 秒）"}
    t = s.get("today") or {}
    rows = s.get("rows") or []
    today = t.get("day") or ""
    if not today:
        return {"state": "unknown", "detail": "中储粮数据为空"}

    # 今日及未来两个已公告交易日的采购计划
    days = sorted({r["day"] for r in rows if r["kind"] == "buy" and r["day"] >= today})
    win = days[:3]
    win_plan = sum(r["qty"] or 0 for r in rows
                   if r["kind"] == "buy" and r["day"] in win)
    big = max([r["qty"] or 0 for r in rows if r["kind"] == "buy" and r["day"] in win] or [0])

    if not win_plan:
        return {"state": "miss", "detail": "今日起三个交易日内无玉米竞价采购公告"}

    head = (f"{win[0][5:]}~{win[-1][5:]} 玉米竞价采购 {len([r for r in rows if r['kind']=='buy' and r['day'] in win])} 场、"
            f"计划 {win_plan/1e4:.1f} 万吨（单场最大 {big/1e4:.1f} 万吨）")
    extra = []
    if t.get("rateBuyPct") is not None:
        cov = t.get("coverageBuyPct")
        extra.append(f"今日成交率 {t['rateBuyPct']:.0f}%"
                     + (f"（仅覆盖 {cov}% 计划量）" if cov is not None and cov < 100 else ""))
    if t.get("netInT") is not None:
        extra.append(f"净轮入 {t['netInT']/1e4:+.2f} 万吨")
    if t.get("importSellT"):
        extra.append(f"进口玉米定向拍卖 {t['importSellT']/1e4:.1f} 万吨（抛储，反向）")
    tail = ("｜" + "，".join(extra)) if extra else ""

    by_big = big >= STORE_BIG_T
    by_plan = win_plan >= STORE_PLAN_T
    if by_big or by_plan:
        why = []
        if by_big:
            why.append(f"出现单场 ≥{STORE_BIG_T/1e4:.0f} 万吨集中收购")
        if by_plan:
            why.append(f"三日计划量 ≥{STORE_PLAN_T/1e4:.0f} 万吨")
        return {"state": "hit", "detail": "；".join(why) + "｜" + head + tail}
    return {"state": "miss",
            "detail": f"未达阈值（单场 ≥{STORE_BIG_T/1e4:.0f} 万吨 或 三日 ≥{STORE_PLAN_T/1e4:.0f} 万吨）｜{head}{tail}"}


# ---------------- 隆众口径（PE/PP 库存与开工率） ----------------
# 数据来自 longzhong.py：抓隆众资讯的周/日度数据文章，正文按登录态渲染，
# 需要 config.json 里配一个已开通数据资讯的账号 Cookie。
PE_OPRATE_MAX = 75.0     # PE 产能利用率低于 75% = 供应收缩
PP_DOWN_MIN = 35.0       # PP 下游开工率高于 35% = 需求回升

_LZ_COL_NAME = {"pe_cap": "PE产能利用率", "twooil": "两油（聚烯烃）库存",
                "pp_stock": "PP生产企业库存", "pp_down": "PP下游开工"}


def _lz_item(ctx, col):
    return ((ctx.get("lz") or {}).get("items") or {}).get(col) or {}


def _lz_missing(ctx, col):
    """数据不可用时的统一说明：区分「没配 Cookie」「Cookie 过期」「已登录但没订阅」。"""
    it = _lz_item(ctx, col)
    lz = ctx.get("lz") or {}
    if not lz.get("configured"):
        return f"{_LZ_COL_NAME[col]}：隆众账号未配置（config.json → longzhong.cookie）"
    reason = it.get("reason")
    if reason == "login":
        return f"{_LZ_COL_NAME[col]}：隆众 Cookie 已过期，重跑 lz_login.js 刷新"
    if reason == "noperm":
        return f"{_LZ_COL_NAME[col]}：账号未开通该数据资讯（续费后自动生效）"
    return f"{_LZ_COL_NAME[col]}：{it.get('err') or '未抓取到数据'}"


def _lz_head(it, col):
    """统一的数据出处后缀：数据日期 + 标题，方便盘后回查。"""
    d = it.get("date") or ""
    if len(d) == 6:
        d = f"20{d[:2]}-{d[2:4]}-{d[4:]}"
    return f"（隆众 {d}）"


def pe_oprate(ctx):
    """l1 供应收缩：PE 产能利用率 < 75%。周更，取最新一期。"""
    it = _lz_item(ctx, "pe_cap")
    if not it.get("ok"):
        return {"state": "unknown", "detail": _lz_missing(ctx, "pe_cap")}
    p = it.get("parsed") or {}
    v = p.get("value")
    if v is None:
        return {"state": "unknown", "detail": "PE 产能利用率解析失败，原文：" + (it.get("snippet") or "")[:60]}
    d = p.get("delta")
    tag = f"PE 产能利用率 {v:.2f}%" + (f"（较上期 {d:+.2f}pp）" if d is not None else "") + _lz_head(it, "pe_cap")
    if v < PE_OPRATE_MAX:
        return {"state": "hit", "detail": f"{tag}，低于 {PE_OPRATE_MAX:.0f}% 阈值，供应端收缩"}
    return {"state": "miss", "detail": f"{tag}，未低于 {PE_OPRATE_MAX:.0f}% 阈值（还差 {v - PE_OPRATE_MAX:+.2f}pp）"}


def _lz_two_declines(ctx, col):
    """「连续两期环比下降」判定，返回 (hit, 说明)。

    两期变化值的来源有两条，优先级如下：
      ① parsed.delta（当期「较上期下降 X」）+ parsed.prevDelta（上一期原文里的环比）
         —— longzhong 抓当期时顺手把上一篇也取了，当天就能判定；
      ② 本地 history（每期一条 {d, v}）—— ①缺失时用它凑够 3 期再判。
    两条都没有就说清还差什么，不硬判。
    """
    it = _lz_item(ctx, col)
    if not it.get("ok"):
        return None, _lz_missing(ctx, col)
    p = it.get("parsed") or {}
    name = _LZ_COL_NAME[col]
    cur = p.get("value")
    if cur is None:
        return None, f"{name}解析失败"
    d_now = p.get("delta")
    d_prev = p.get("prevDelta")
    hist = ((ctx.get("lz") or {}).get("history") or {}).get(col) or []
    # 序列展示：优先当期+上一期（有具体数值），否则退回本地 history
    prev_v = p.get("prev")
    if prev_v is None and d_now is not None and cur is not None:
        prev_v = round(cur - d_now, 2)
    seq = ""
    if prev_v is not None:
        seq = f"{prev_v:.2f}→{cur:.2f}"
    elif len(hist) >= 2:
        seq = "→".join(f"{h['v']:.2f}" for h in hist[-4:])
    head = f"{name} {cur:.2f} 万吨" + (f"（{seq}）" if seq else "") + _lz_head(it, col)
    if d_now is not None and d_prev is not None:
        pair = f"近两期环比 {d_prev:+.2f} / {d_now:+.2f} 万吨"
        if d_now < 0 and d_prev < 0:
            return True, head + f"，{pair}，连续两期下降"
        return False, head + f"，{pair}，未构成连续两期下降"
    if len(hist) >= 3:
        v = [h["v"] for h in hist[-3:]]
        downs = sum(1 for i in range(1, len(v)) if v[i] < v[i - 1])
        if downs == 2:
            return True, head + "，历史序列连续两期环比下降"
        return False, head + f"，历史序列近两期仅 {downs} 次下降"
    # 两条路都不足：明确说差什么
    tail = f"，本期 {'下降' if (d_now or 0) < 0 else '上升'} {abs(d_now):.2f} 万吨" if d_now is not None else ""
    return None, head + tail + "；上一期原文未取到（环比缺失），历史仅 %d 期，暂无法判「连续两期」" % len(hist)


def twooil_trend(ctx):
    """l2 库存去化：两油（重点生产企业聚烯烃）库存连续两期下降。日更。"""
    ok, msg = _lz_two_declines(ctx, "twooil")
    if ok is None:
        return {"state": "unknown", "detail": msg}
    return {"state": "hit" if ok else "miss", "detail": msg}


def pp_stock_trend(ctx):
    """pp1 库存去化：PP 生产企业库存连续两期下降。周更。"""
    ok, msg = _lz_two_declines(ctx, "pp_stock")
    if ok is None:
        return {"state": "unknown", "detail": msg}
    return {"state": "hit" if ok else "miss", "detail": msg}


def pp_down_rate(ctx):
    """pp2 开工回升：PP 下游塑编 / 注塑 / BOPP 开工率均 > 35%。周更。"""
    it = _lz_item(ctx, "pp_down")
    if not it.get("ok"):
        return {"state": "unknown", "detail": _lz_missing(ctx, "pp_down")}
    p = it.get("parsed") or {}
    keys = [k for k in ("塑编", "注塑", "BOPP") if p.get(k) is not None]
    if not keys:
        return {"state": "unknown", "detail": "PP 下游分项开工率解析失败"}
    seg = " / ".join(f"{k} {p[k]:.2f}%" for k in keys)
    low = [k for k in keys if p[k] < PP_DOWN_MIN]
    avg = p.get("value")
    avgseg = f"，行业平均 {avg:.2f}%" if avg is not None else ""
    tag = f"{seg}{avgseg}" + _lz_head(it, "pp_down")
    if not low:
        return {"state": "hit", "detail": f"{tag}，{len(keys)} 项全部高于 {PP_DOWN_MIN:.0f}%，需求端回升"}
    return {"state": "miss",
            "detail": f"{tag}，{'/'.join(low)} 仍低于 {PP_DOWN_MIN:.0f}% 阈值"}


# ---------------- 卓创（sci99）· 淘鸡出栏量 ----------------

_ZC_COL_NAME = {"taoji": "淘鸡（老母鸡）出栏量", "corn_sell": "玉米基层售粮进度",
                "egg_price": "鸡蛋主产区均价"}


def _zc_item(ctx, col):
    return ((ctx.get("zc") or {}).get("items") or {}).get(col) or {}


def _zc_missing(ctx, col):
    it = _zc_item(ctx, col)
    zc = ctx.get("zc") or {}
    if not zc.get("configured"):
        return f"{_ZC_COL_NAME[col]}：卓创账号未配置（config.json → sci99.cookie）"
    reason = it.get("reason")
    if reason == "login":
        return f"{_ZC_COL_NAME[col]}：卓创 Cookie 已过期，重跑 zc_login.js 刷新"
    if reason == "noperm":
        return f"{_ZC_COL_NAME[col]}：卓创畜牧业主订阅到期（试用 2026-10-27 止，续订后自动生效）"
    return f"{_ZC_COL_NAME[col]}：{it.get('err') or '未抓取到数据'}"


def _zc_head(it):
    d = it.get("date") or ""          # 形如 2026-10-W2
    return f"（卓创 {d}）" if d else "（卓创）"


def taoji_rise(ctx):
    """jd2 淘鸡放量：周度淘鸡（老母鸡）出栏量环比增加。

    卓创每期正文自带环比（增幅/减幅），当期即可判，无需历史积累。
    deltaPct > 0 → hit；< 0 → miss；解析不出 → unknown。
    """
    it = _zc_item(ctx, "taoji")
    if not it.get("ok"):
        return {"state": "unknown", "detail": _zc_missing(ctx, "taoji")}
    p = it.get("parsed") or {}
    v, d = p.get("value"), p.get("deltaPct")
    if v is None or d is None:
        return {"state": "unknown", "detail": f"{_ZC_COL_NAME['taoji']}解析失败（{_zc_head(it)}）"}
    head = f"{_ZC_COL_NAME['taoji']} {v:.2f} 万只，环比 {d:+.2f}%"
    if p.get("yoy") is not None:
        head += f"，同比 {p['yoy']:+.2f}%"
    head += _zc_head(it)
    if d > 0:
        tail = "，养殖端淘汰节奏加快，供应边际增加（对蛋价利空）"
        return {"state": "hit", "detail": head + tail}
    tail = f"，出栏量环比收缩（上期约 {p['prev']:.2f} 万只）" if p.get("prev") is not None else ""
    return {"state": "miss", "detail": head + tail}


def _ms_item(ctx, col):
    return (((ctx.get("ms") or {}).get("items") or {}).get(col)) or {}

_MS_COL_NAME = {"meal_stock": "豆粕库存"}


def _ms_missing(ctx, col):
    ms = ctx.get("ms") or {}
    it = _ms_item(ctx, col)
    if not ms.get("configured"):
        return f"{_MS_COL_NAME[col]}：Mysteel 账号未配置（config.json → mysteel.cookie）"
    if ms.get("needLogin") or it.get("needLogin"):
        return f"{_MS_COL_NAME[col]}：Mysteel Cookie 已过期，重跑 ms_login.js 刷新"
    return f"{_MS_COL_NAME[col]}：{it.get('err') or '未抓取到数据'}"


def _ms_head(it):
    wk = (it.get("parsed") or {}).get("week")
    d = it.get("date") or ""
    tag = f"Mysteel {wk or d}" if (wk or d) else "Mysteel"
    return f"（{tag}）"


MEAL_STOCK_MAX = 100.0   # 豆粕库存低于 100 万吨 = 库存去化


def meal_stock_low(ctx):
    """m1 库存去化：豆粕周度库存 < 100 万吨。

    Mysteel 每期正文自带环比（较上周增加/减少），当期即可判方向；
    触发与否只看绝对值，不需要历史积累。
    """
    it = _ms_item(ctx, "meal_stock")
    if not it.get("ok"):
        return {"state": "unknown", "detail": _ms_missing(ctx, "meal_stock")}
    p = it.get("parsed") or {}
    v, d = p.get("value"), p.get("delta")
    if v is None:
        return {"state": "unknown", "detail": f"{_MS_COL_NAME['meal_stock']}解析失败{_ms_head(it)}"}
    head = f"{_MS_COL_NAME['meal_stock']} {v:.1f} 万吨"
    if d is not None:
        head += f"，环比 {d:+.2f}"
    head += _ms_head(it)
    if v < MEAL_STOCK_MAX:
        return {"state": "hit", "detail": head + "，低于 100 万吨，库存去化成立（对豆粕利多）"}
    gap = v - MEAL_STOCK_MAX
    tail = f"，距 100 万吨还差 {gap:.1f}"
    if d is not None:
        tail += "；环比仍在累库" if d > 0 else "；环比已转降，关注去化节奏"
    return {"state": "miss", "detail": head + tail}


# ---------------- 盘面自算（不依赖付费资讯权限） ----------------
# Mysteel 的《进口大豆到港成本及压榨利润》《淀粉企业加工利润估算表》都是表格型
# 文章，正文被频道权限裁掉（实测返回「您暂无 农产品频道…访问权限」），只留一句
# 摘要。所以这两条改用盘面数据自己算，口径写在 detail 里，可复核。

CRUSH_MEAL_RATE = 0.785     # 出粕率：1 吨大豆 -> 0.785 吨豆粕
CRUSH_OIL_RATE = 0.185      # 出油率：1 吨大豆 -> 0.185 吨豆油
CRUSH_FEE = 150.0           # 加工费 元/吨（Mysteel 口径：未扣加工费的是"毛利"）
CRUSH_MIN = 0.0             # 净利 >0 视为油厂压榨有利润

STARCH_LOOKBACK = 120       # 淀粉价差的历史窗口（交易日）


def _crush_legs(ctx):
    """取榨利三腿：豆粕主力（主行情）、豆油 Y 与豆二 B（外参）。"""
    m_sym = _q_sym(ctx.get("quotes"), "M")
    m_last = _q_last(ctx.get("quotes"), "M", ctx.get("klines"))
    eY, eB = _ext(ctx.get("ext"), "Y"), _ext(ctx.get("ext"), "B")
    return m_sym, m_last, eY, eB


def _crush_history(ctx, m_sym):
    """按日对齐三腿，算近 120 日盘面净利序列。缺任一条腿就返回空。"""
    kl = ctx.get("klines") or {}
    raw = ctx.get("extraw") or {}
    mk = kl.get((m_sym, "86400"))
    yk = (raw.get("Y") or {}).get("kline")
    bk = (raw.get("B") or {}).get("kline")
    if mk is None or yk is None or bk is None:
        return []
    md, yd, bd = _date_close(mk), _date_close(yk), _date_close(bk)
    days = sorted(set(md) & set(yd) & set(bd))[-STARCH_LOOKBACK:]
    return [CRUSH_MEAL_RATE * md[d] + CRUSH_OIL_RATE * yd[d] - bd[d] - CRUSH_FEE for d in days]


def crush_margin(ctx):
    """m2 压榨利润：盘面净利（已扣 150 元/吨加工费）> 0。

    为什么不用 Mysteel 现成榨利
    --------------------------
    那篇《进口大豆到港成本及压榨利润》是表格型文章，正文被频道权限裁掉，抓下来
    只剩一句摘要、一个数字都没有（实测）。改用盘面自己算：

        净利 = 豆粕×0.785 + 豆油×0.185 − 豆二 − 150
                （收入端）              （原料）  （加工费）

    成本腿用大商所豆二（进口大豆）而不是 CBOT 折算，省掉汇率、升贴水、关税三个
    假设，是市场上更常用的"盘面榨利"口径。

    注意钝化：近半年净利长期在 0 上方运行时，" >0 "这条会永远亮灯。detail 里同时
    报区间与分位，真到那一步看分位比看阈值有用（同 m3 美豆 1000 关口的处理）。
    """
    m_sym, m_last, eY, eB = _crush_legs(ctx)
    if not m_last:
        return {"state": "unknown",
                "detail": "豆粕主连行情为空（休市或行情断线，夜盘 21:00 后自动恢复）"}
    if not eY:
        return {"state": "unknown", "detail": "豆油 Y 行情为空（休市或行情断线，夜盘 21:00 后自动恢复）"}
    if not eB:
        return {"state": "unknown", "detail": "豆二 B 行情为空（休市或行情断线，夜盘 21:00 后自动恢复）"}

    y_last, b_last = eY["last"], eB["last"]
    gross = CRUSH_MEAL_RATE * m_last + CRUSH_OIL_RATE * y_last - b_last
    net = gross - CRUSH_FEE
    live = (_q_live(ctx.get("quotes"), "M") is not None
            and not eY.get("closed") and not eB.get("closed"))
    tag = "" if live else "（收盘口径）"
    head = (f"盘面净利{tag} {net:+.1f} 元/吨（毛利 {gross:+.1f}，已扣 {CRUSH_FEE:.0f} 加工费）｜"
            f"豆粕 {m_last:.0f}×0.785 + 豆油 {y_last:.0f}×0.185 − 豆二 {b_last:.0f}")

    hist = _crush_history(ctx, m_sym)
    tail = ""
    if len(hist) >= 20:
        lo, hi = min(hist), max(hist)
        pct = _pct_rank(hist, net)
        tail = f"｜近 {len(hist)} 日 {lo:+.0f}~{hi:+.0f}，当前分位 {pct:.0f}%"
        if lo > CRUSH_MIN:
            tail += "，区间全为正、阈值已钝化，看分位更有效"
    elif hist:
        tail = f"｜历史仅 {len(hist)} 日，样本不足，未给分位"

    if net > CRUSH_MIN:
        return {"state": "hit", "detail": head + tail + "，油厂压榨有利润"}
    return {"state": "miss",
            "detail": head + tail + f"，跌破盈亏线 {abs(net):.1f} 元，油厂有停机压力"}


def _starch_history(ctx, cs_kline, ckline):
    if cs_kline is None or ckline is None:
        return []
    sd, cd = _date_close(cs_kline), _date_close(ckline)
    days = sorted(set(sd) & set(cd))[-STARCH_LOOKBACK:]
    return [sd[d] - cd[d] for d in days]


def starch_margin(ctx):
    """c3 加工利润：淀粉-玉米盘面价差，用近 120 日中位作分化线。

    为什么是价差而不是利润绝对值
    --------------------------
    玉米淀粉加工利润要扣玉米成本、副产品（蛋白粉/胚芽/纤维）收益和加工费，副产品
    报价拿不到公开接口。硬套一套系数等于自己编参数，宁可退一步用价差这个纯盘面、
    可复现的代理：价差走扩 = 淀粉端相对玉米更强 = 加工利润改善。

    为什么价差必须同月
    ----------------
    淀粉主力常在 1/5/9，玉米主力在 1 月，两条腿不同月时价差里混着月间结构（近月
    升贴水），量出来的不是加工利润。所以优先用 server 挂的「淀粉同月玉米腿」，没有
    再退回玉米主合约，并在 detail 里点明用的是哪条腿。
    """
    cs = _ext(ctx.get("ext"), "CS")
    if not cs:
        return {"state": "unknown", "detail": "玉米淀粉 CS 行情为空（休市或行情断线，夜盘 21:00 后自动恢复）"}

    raw = ctx.get("extraw") or {}
    mate = _ext(ctx.get("ext"), "C_STARCH")
    if mate:
        c_last = mate["last"]
        ckline = (raw.get("C_STARCH") or {}).get("kline")
        leg = (raw.get("C_STARCH") or {}).get("symbol") or "同月玉米"
        c_closed = bool(mate.get("closed"))
    else:
        c_sym = _q_sym(ctx.get("quotes"), "C")
        c_last = _q_last(ctx.get("quotes"), "C", ctx.get("klines"))
        ckline = (ctx.get("klines") or {}).get((c_sym, "86400")) if c_sym else None
        leg = "玉米主合约（跨月）"
        c_closed = _q_live(ctx.get("quotes"), "C") is None
    if not c_last:
        return {"state": "unknown",
                "detail": "玉米主连行情为空（休市或行情断线，夜盘 21:00 后自动恢复）"}

    cs_last = cs["last"]
    spread = cs_last - c_last
    if spread != spread:      # nan 兜底：任何一腿异常都不出结论
        return {"state": "unknown", "detail": "淀粉/玉米价格异常（nan），暂不判定"}
    tag = "" if (not c_closed and not cs.get("closed")) else "（收盘口径）"
    head = (f"淀粉-玉米价差{tag} {spread:+.0f} 元/吨（淀粉 {cs_last:.0f} − 玉米 {c_last:.0f}，"
            f"对腿 {leg}）")

    hist = _starch_history(ctx, (raw.get("CS") or {}).get("kline"), ckline)
    if len(hist) < 20:
        return {"state": "unknown",
                "detail": head + f"｜历史仅 {len(hist)} 日，不足以定中位线，暂不判定"}
    med = float(pd.Series(hist).median())
    pct = _pct_rank(hist, spread)
    tail = f"｜近 {len(hist)} 日中位 {med:.0f}、区间 {min(hist):.0f}~{max(hist):.0f}，当前分位 {pct:.0f}%"

    if spread > med:
        return {"state": "hit", "detail": head + tail + "，价差高于常态，加工利润改善（利多玉米需求）"}
    return {"state": "miss", "detail": head + tail + "，价差低于常态，加工利润偏弱（玉米需求端拖累）"}


SELL_PROGRESS_MIN = 50.0   # 基层售粮进度过半 = 卖压释放过半


def sell_progress(ctx):
    """c2 售粮进度：基层售粮进度 > 50%（卖压释放过半，供应压力见顶）。

    为什么用卓创而不是 Mysteel
    --------------------------
    实测 Mysteel 玉米频道（p-4118/4275/4264/4266/4267 五个 070401 子栏目）只有
    港口/饲料/加工企业库存和深加工消费统计，**没有售粮进度这个系列**；玉米周报里
    只有一句定性的「农户售粮节奏分化」。卓创则在分析文章和数据统计两种形态里都给
    分地区百分比（东北/华北 + 同比 ±N 个百分点），正文免费可读。

    季节性提醒：玉米售粮进度是随收购季推进的指标，新季（10 月起）首期一般在 11 月
    发布。取不到数据时判 unknown 并在 detail 说明，不做任何估算替身。
    """
    it = _zc_item(ctx, "corn_sell")
    if not it.get("ok"):
        return {"state": "unknown", "detail": _zc_missing(ctx, "corn_sell")}
    p = it.get("parsed") or {}
    v = p.get("value")
    if v is None:
        return {"state": "unknown",
                "detail": f"{_ZC_COL_NAME['corn_sell']}解析失败，正文未取到百分比{_zc_head(it)}"}

    parts = []
    if p.get("ne") is not None:
        parts.append(f"东北 {p['ne']:.0f}%" +
                     (f"（同比 {p['neYoy']:+.0f}pp）" if p.get("neYoy") is not None else ""))
    if p.get("hua") is not None:
        parts.append(f"华北 {p['hua']:.0f}%" +
                     (f"（同比 {p['huaYoy']:+.0f}pp）" if p.get("huaYoy") is not None else ""))
    head = (f"{p['season']} 年度" if p.get("season") else "") + \
           (f"截至 {p['asOf']}，" if p.get("asOf") else "")
    head += (f"两区平均 {v:.1f}%" if p.get("avg") is not None else f"进度 {v:.0f}%")
    if parts:
        head += "｜" + "、".join(parts)
    if p.get("lastYear") is not None and p.get("avg") is not None:
        head += f"｜去年同期 {p['lastYear']:.0f}%"
    head += _zc_head(it)

    if v > SELL_PROGRESS_MIN:
        return {"state": "hit",
                "detail": head + f"，已过 {SELL_PROGRESS_MIN:.0f}%，基层卖压释放过半（供应压力见顶，利多玉米）"}
    return {"state": "miss",
            "detail": head + f"，距 {SELL_PROGRESS_MIN:.0f}% 还差 {SELL_PROGRESS_MIN - v:.1f}pp，卖压仍在释放"}


AUTO_RULES = {
    "jd1": spot_stop_fall,     # 现货止跌  —— 生意社鸡蛋基准价连续 3 日不创新低
    "jd2": taoji_rise,         # 淘鸡放量  —— 卓创 老母鸡出栏量周数据，环比增加
    "jd3": vol_stall,          # 缩量企稳  —— 日线量 + 持仓，纯技术面
    "l1": pe_oprate,           # 供应收缩  —— 隆众 PE 产能利用率周数据
    "l2": twooil_trend,        # 库存去化  —— 隆众 两油（聚烯烃）库存日数据，连续两期下降
    "l3": sc_break_high,       # 成本抬升  —— 上海原油 SC 突破前 20 日高
    "pp1": pp_stock_trend,     # 库存去化  —— 隆众 PP 生产企业库存周数据，连续两期下降
    "pp2": pp_down_rate,       # 开工回升  —— 隆众 PP 下游塑编/注塑/BOPP 开工率
    "pp3": brent_cost,         # 成本支撑  —— 布伦特 >100 且站上 20 日均线
    "m1": meal_stock_low,      # 库存去化  —— Mysteel 豆粕库存周数据 < 100 万吨
    "m2": crush_margin,        # 压榨利润  —— 盘面自算：豆粕0.785+豆油0.185-豆二-150
    "m3": cbot_resurgence,     # 美豆反弹  —— 上破前 20 日高（1000 关口已钝化）
    "c1": sino_storage,        # 收储启动  —— 中储粮玉米竞价采购放量（单场/三日）
    "c2": sell_progress,       # 售粮进度  —— 卓创 玉米基层售粮进度 > 50%
    "c3": starch_margin,       # 加工利润  —— 盘面自算：淀粉-玉米价差 vs 近120日中位
}

# 信号「触发」时对价格的倾向：+1 利多 / -1 利空 / 0 方向不明（页面按此上红/绿色）
# 依据各规则 hit 分支的语义，与 detail 文案里的「利多/利空」保持同一口径。
SIGNAL_BIAS = {
    "jd1": +1,   # 现货止跌 → 利多
    "jd2": -1,   # 淘鸡出栏放量 → 供应边际增加，利空蛋价
    "jd3": +1,   # 缩量企稳 → 止跌
    "l1": +1,    # PE 开工低 → 供应收缩
    "l2": +1,    # 两油去库 → 利多
    "l3": +1,    # SC 上破 → 成本抬升
    "pp1": +1,   # PP 去库 → 利多
    "pp2": +1,   # 下游开工回升 → 需求好转
    "pp3": +1,   # 油价强 → 成本支撑
    "m1": +1,    # 豆粕去库 → 利多
    "m2": +1,    # 压榨利润改善 → 需求好
    "m3": +1,    # 美豆上破 → 利多
    "c1": +1,    # 收储启动 → 利多
    "c2": +1,    # 售粮过半、卖压见顶 → 利多
    "c3": +1,    # 淀粉利润改善 → 利多玉米需求
}


# ---------------- 外参汇总 ----------------

def ext_stats(entry):
    """把外参合约压成 last / 前20日高 / MA20。日线最后一根进行中，统计时剔除。

    断线时 last_price 可能是 0 或 nan，统一归一成 None；这时退回日线最后收盘，
    并把 closed 标成 True，让上层能在 detail 里注明"收盘口径"。
    """
    q, kdf = entry.get("quote"), entry.get("kline")
    last = None
    try:
        last = round(float(q.last_price), 2)
    except Exception:
        last = None
    if last is not None and (last != last or last == 0):
        last = None
    closed = False
    df = _daily(kdf, 40)
    if last is None and df is not None and len(df):
        try:
            c = float(df["close"].iloc[-1])
            if c > 0 and c == c:
                last, closed = round(c, 2), True
        except Exception:
            pass
    hi = ma = None
    if df is not None and len(df) >= 20:
        done = df.iloc[:-1] if len(df) > 20 else df
        hi = round(float(done["high"].tail(20).max()), 2)
        ma = round(float(done["close"].tail(20).mean()), 2)
    return {"symbol": entry.get("symbol"), "last": last, "high20": hi, "ma20": ma, "closed": closed}


def build_auto_signals(playbooks, klines, ext, extra=None, spot=None, sino=None, lz=None, zc=None, ms=None,
                       quotes=None):
    """返回 {playbook_id: {signal_id: {state, detail}}}，只包含有自动规则的信号。

    ext    : TqSdk 订阅的外参合约 {key: {"quote","kline"}}，会压成统计值
    extra  : 已算好的外部统计 (外盘源直接给 last/high20/ma20)，原样并入
    spot   : 生意社现货快照（spot.py），供 jd1 这类现货口径的规则用
    sino   : 中储粮竞价快照（sinograin.py），供 c1 收储规则用
    lz     : 隆众快照（longzhong.py），供 l1/l2/pp1/pp2 库存与开工率规则用
    zc     : 卓创快照（sci99.py），供 jd2 淘鸡出栏量规则用
    ms     : 我的农产品快照（mysteel.py），供 m1 豆粕库存规则用
    quotes : 主合约实时行情 {symbol: TqSdk quote}，供 m2/c3 这类要拼多腿的盘面自算规则用
    """
    stats = {k: ext_stats(v) for k, v in (ext or {}).items()}
    stats.update(extra or {})
    out = {}
    for pb in (playbooks or {}).get("playbooks", []):
        sym = pb.get("symbol")
        ctx = {"kdf": (klines or {}).get((sym, "86400")), "ext": stats,
               "spot": spot or {}, "sino": sino or {}, "lz": lz or {}, "zc": zc or {},
               "ms": ms or {},
               # 多腿拼算要用原始日线对齐，压过的 stats 不够用
               "quotes": quotes or {}, "extraw": ext or {}, "klines": klines or {}}
        res = {}
        for sig in pb.get("industrySignals") or []:
            fn = AUTO_RULES.get(sig.get("id"))
            if not fn:
                continue
            try:
                one = fn(ctx) or {}
            except Exception as exc:      # 单条规则异常不影响其它品种
                one = {"state": "unknown", "detail": f"判定异常 {type(exc).__name__}"}
            one["bias"] = SIGNAL_BIAS.get(sig.get("id"), 0)   # 触发时的利多/利空倾向
            res[sig["id"]] = one
        if res:
            out[pb.get("id")] = res
    return out
