#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""关键位自动重算：移植参考站 scripts/push_levels.py 的规则化算法。

为什么需要它
------------
playbook.json 里的 levels 是参考站的静态快照（JD 压力 3500 / 破位 3400），
JD 现价 3302 时"破位 3400"根本没破，提示全是错的。参考站自己是每天收盘后
跑 push_levels.py 重算再推到数据库，静态值只是兜底。这里把同一套算法落到
本地：TqSdk 取日线 → 算 ATR/MA/趋势 → 生成 levels/thesis/plan，写回 playbook.json。

算法口径（与参考站一致）
------------------------
趋势 bias = last<MA5<MA20 → bear；last>MA5>MA20 → bull；其余 range
关键位、三档止损、目标位按 bias 三分支取 MA5/MA20/20日高低/ATR 组合；
历史分位用主连日线（1300 根）算现价在历史上的百分位。

用法
----
    TQ_AUTH_USER=xxx TQ_AUTH_PASS=yyy python3 workbench/levels.py
    ... --dry-run   只打印不写盘

建议每天 15:10 收盘后跑一次（日盘已收、当日 K 线完整，算出的位次日有效）。
"""
import json
import os
import sys
import time
from pathlib import Path

BASE = Path(__file__).resolve().parent
PB_PATH = BASE / "playbook.json"

NAMES = {}


def cont_symbol(main_sym):
    """DCE.jd2612 -> KQ.m@DCE.jd（主连是品种级，不带月份）。"""
    ex, code = main_sym.split(".", 1)
    prod = "".join(c for c in code if c.isalpha())
    return f"KQ.m@{ex}.{prod}"


# ---------- 基础指标 ----------

def atr14(bars, n=14):
    """TR 简单平均（与参考站同口径，非 Wilder 平滑）。"""
    trs, prev = [], None
    for _, r in bars.iterrows():
        h, l, c = float(r["high"]), float(r["low"]), float(r["close"])
        if prev is not None:
            trs.append(max(h - l, abs(h - prev), abs(l - prev)))
        prev = c
    return sum(trs[-n:]) / min(n, len(trs)) if trs else 0.0


def judge_bias(last, ma5, ma20):
    if last < ma5 < ma20:
        return "bear"
    if last > ma5 > ma20:
        return "bull"
    return "range"


def fix_ladder(L, atr):
    """分层自检修正：保证 压力 > 一撑 > 强撑 > 失守 严格递减。

    参考站原算法在"现价已贴近 20 日低点"时，firstSupport = max(low20, last-1.5ATR)
    会直接等于 low20，跟 strongSupport 撞成同一个数（JD/M/C 都出现）。这里保留
    "强撑 = 真实前低"的语义，把一撑抬到其上方 0.35 ATR；间距按 ATR 缩放，不是拍数字。
    """
    a = max(atr, 1)
    gap = max(round(0.35 * a), 1)
    if L["firstSupport"] <= L["strongSupport"]:
        L["firstSupport"] = L["strongSupport"] + gap
    if L["stopTarget"] >= L["strongSupport"]:
        L["stopTarget"] = L["strongSupport"] - gap
    if L["firstSupport"] >= L["resistance"]:
        L["resistance"] = L["firstSupport"] + gap
    if L["shortBreakdown"] >= L["firstSupport"]:
        L["shortBreakdown"] = L["firstSupport"]
    return L


def calc_levels(last, high20, low20, ma5, ma20, atr, bias):
    """规则化关键位：全部由均线/20日高低/ATR 推出，可复现。
    与参考站差别：range 分支的 stopTarget 强制低于 strongSupport，
    避免参考站 L 那种"失守目标 8500 > 强撑 8221"的逻辑倒置。"""
    a = max(atr, 1)
    if bias == "bear":
        return {
            "resistance": round(high20),
            "firstSupport": round(max(low20, last - 1.5 * a)),
            "strongSupport": round(low20),
            "stopTarget": round(low20 - 0.5 * a),
            "longTriggerBreak": round(ma5 if ma5 > last else last + 1.2 * a),
            "longPullback": round(max(low20, last - a)),
            "shortBreakdown": round(last - 0.8 * a) if last - 0.8 * a > low20 else round(low20),
            "shortFadeZone": [round(ma5 if ma5 > last else last + a),
                              round(ma5 + a if ma5 > last else last + 2 * a)],
        }
    if bias == "bull":
        return {
            "resistance": round(high20),
            "firstSupport": round(ma5 if ma5 < last else last - 0.8 * a),
            "strongSupport": round(ma20),
            "stopTarget": round(ma20 - 0.5 * a),
            "longTriggerBreak": round(high20),
            "longPullback": round(ma5 if ma5 < last else last - a),
            "shortBreakdown": round(ma20),
            "shortFadeZone": [round(high20), round(high20 + 0.5 * a)],
        }
    return {
        "resistance": round(high20),
        "firstSupport": round(low20),
        "strongSupport": round(low20 - 0.5 * a),
        "stopTarget": round(low20 - a),
        "longTriggerBreak": round(high20),
        "longPullback": round(low20 + 0.3 * a),
        "shortBreakdown": round(low20),
        "shortFadeZone": [round(high20 - 0.3 * a), round(high20)],
    }


def advice_direction(bias, last, ma5):
    """把方向从「趋势描述」升级成「当下该怎么拿」的策略状态。

    参考台线上显示的是「持仓建议 · 止跌观望」这类可执行状态，而不是「空头趋势」——
    趋势词只说明均线怎么排，策略词说明手上的仓位该怎么处理。规则就是「排列 × 短线均线位置」：
      空头排列、已站上 MA5  → 短线动能证伪，止跌观望（这个位置不该再追空）
      空头排列、仍在 MA5 下 → 顺势持空
      多头排列、已跌破 MA5  → 回调观望（这个位置不该再追多）
      多头排列、仍在 MA5 上 → 顺势持多
      均线缠绕              → 区间高抛低吸
    与 shortStop 的取值是一套逻辑：空头里 shortStop 就是 MA5，站上 MA5 同时意味着
    「止跌观望」+ 盘中「短线止损已破」，两个结论自洽而不是打架。
    """
    if bias == "bear":
        return "止跌观望" if last > ma5 else "顺势持空"
    if bias == "bull":
        return "回调观望" if last < ma5 else "顺势持多"
    return "区间高抛低吸"


def position_advice(last, high20, low20, ma5, ma20, atr, bias):
    """三档并行建议：短线止损 / 波段止损 / 目标位，按自己的持仓周期选用。

    counter 是「逆势持仓」那套档位（手上单子与 bias 相反）：
      - bias=bear 时手上拿多单 → 止损往下放（20日低之上先减、跌破前低全走），
        目标往上放（MA5 先减、MA20 才算转势）；
      - bias=bull 时手上拿空单 → 完全镜像。
    参考台只有顺向一套，逆势持仓的人拿不到可执行的线，这里补上。
    """
    a = max(atr, 1)
    if bias == "bear":
        return {
            "direction": advice_direction(bias, last, ma5),
            "bias": bias,
            # 前端用 ma5 与实时价重算方向词（站上/跌破 MA5 当场切换），不再只信收盘口径
            "ma5": round(ma5),
            "shortStop": round(ma5 if ma5 > last else last + 1.5 * a),
            "swingStop": round(ma20),
            "swingTarget": round(low20 - a),
            "trendTarget": round(low20 - 2 * a),
            "holdCondition": f"收盘未站上 MA5({round(ma5)}) 则空头结构完好，可持有看 {round(low20 - a)}；站上 MA20({round(ma20)}) 必须离场",
            "counter": {
                "side": "long", "ma5": round(ma5),
                "shortStop": round(low20 + 0.3 * a),
                "swingStop": round(low20),
                "swingTarget": round(ma5),
                "trendTarget": round(ma20),
                "holdCondition": (
                    f"逆势多单：跌破 {round(low20)}（20日低）全走——趋势仍空，不摊平；"
                    f"反弹到 MA5({round(ma5)}) 不过先减，站上 MA20({round(ma20)}) 才算转势"
                ),
            },
        }
    if bias == "bull":
        return {
            "direction": advice_direction(bias, last, ma5),
            "bias": bias,
            "ma5": round(ma5),
            "shortStop": round(ma5 if ma5 < last else last - 1.5 * a),
            "swingStop": round(ma20),
            "swingTarget": round(high20 + a),
            "trendTarget": round(high20 + 2 * a),
            "holdCondition": f"收盘未跌破 MA5({round(ma5)}) 则多头结构完好，可持有看 {round(high20 + a)}；跌破 MA20({round(ma20)}) 必须离场",
            "counter": {
                "side": "short", "ma5": round(ma5),
                "shortStop": round(high20 - 0.3 * a),
                "swingStop": round(high20),
                "swingTarget": round(ma5),
                "trendTarget": round(ma20),
                "holdCondition": (
                    f"逆势空单：站上 {round(high20)}（20日高）全走——趋势仍多，不扛单；"
                    f"回踩 MA5({round(ma5)}) 不破先减，跌破 MA20({round(ma20)}) 才算转势"
                ),
            },
        }
    return {
        "direction": advice_direction(bias, last, ma5),
        "bias": bias,
        "ma5": round(ma5),
        "shortStop": round(low20 - 0.5 * a),
        "swingStop": round(low20 - a),
        "swingTarget": round(high20),
        "trendTarget": None,
        "holdCondition": f"区间 {round(low20)}-{round(high20)}，高抛低吸为主，突破区间再切换趋势模式",
    }


def gen_thesis(last, ma5, ma20, atr, bias, levels, advice):
    # 注意：thesis 会被前端 innerHTML 渲染，比较符号用全角 ＜＞，
    # 裸的 "<MA5" 会被浏览器解析成标签吞掉整段文字
    hi, lo = round(levels["resistance"]), round(levels["strongSupport"])
    if bias == "bear":
        return (f"空头排列（价{last:.0f}＜MA5 {ma5:.0f}＜MA20 {ma20:.0f}），顺势做空不抄底；"
                f"反弹 {levels['shortFadeZone'][0]}-{levels['shortFadeZone'][1]} 滞涨试空，"
                f"站上 MA20({ma20:.0f}) 趋势证伪必须离场，下方看 {advice['swingTarget']}")
    if bias == "bull":
        return (f"多头排列（价{last:.0f}＞MA5 {ma5:.0f}＞MA20 {ma20:.0f}），回踩做多不追高；"
                f"回踩 {levels['longPullback']} 企稳试多，"
                f"跌破 MA20({ma20:.0f}) 趋势证伪必须离场，上方看 {advice['swingTarget']}")
    return (f"区间震荡 {lo}-{hi}（现价 {last:.0f} 居中），高抛低吸为主；"
            f"放量突破 {hi} 转多、跌破 {lo} 转空，突破前不加仓")


def gen_plan(levels, advice, bias, atr):
    """作战卡：给前端的 plan.entry/stop/waveTarget/trendTarget。震荡不给，避免假方向。

    参考站的 entry 与 stop 常算出同一个数（calc_levels 的滞涨区下沿与
    position_advice 的 shortStop 复用同一公式，JD 都等于 MA5），前端据此判
    "止损已破"会把所有单子误判成止损状态。这里强制拉开至少 0.5 ATR。
    """
    if bias == "range":
        return None
    a = max(atr, 1)
    gap = max(round(0.5 * a), 1)
    if bias == "bear":
        entry = levels["shortFadeZone"][0]
        stop = advice["shortStop"]
        if stop < entry + gap:          # 空单止损必须在入场位上方
            stop = entry + gap
        return {"entry": entry, "stop": stop,
                "waveTarget": advice["swingTarget"], "trendTarget": advice["trendTarget"]}
    entry = levels["longPullback"]
    stop = advice["shortStop"]
    if stop > entry - gap:              # 多单止损必须在入场位下方
        stop = entry - gap
    return {"entry": entry, "stop": stop,
            "waveTarget": advice["swingTarget"], "trendTarget": advice["trendTarget"]}


# ---------- 取数 ----------

def drop_unfinished(df, now_bj):
    """剔除未完成的当日 K 线。
    日盘收盘后（15:00+）当天 K 线已完整，必须保留——否则收盘后跑会用昨天的数据；
    盘中或夜盘（21:00 后属于次日交易日）拿到的是半截 K 线，必须剔除。"""
    if len(df) == 0:
        return df
    last_dt = df.index[-1]
    today = now_bj.date()
    hm = now_bj.hour * 100 + now_bj.minute
    if last_dt.date() > today:
        return df.iloc[:-1]          # 夜盘产生的次日K线
    if last_dt.date() == today and hm < 1500:
        return df.iloc[:-1]          # 当日未收盘
    return df


def resolve_main(api, pb):
    conts = api.query_cont_quotes(exchange_id=pb["exchange"], product_id=pb["product"])
    if not conts:
        return None
    if isinstance(conts, dict):
        return max(conts.items(), key=lambda kv: kv[1])[0]
    return list(conts)[0]


def load_env_file():
    """环境变量缺失时读 workbench/.tqenv（KEY=VALUE 行），免去每次命令行带密码。"""
    f = BASE / ".tqenv"
    if not f.exists():
        return
    for line in f.read_text("utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip())


def main():
    dry = "--dry-run" in sys.argv
    load_env_file()
    user = os.environ.get("TQ_AUTH_USER", "")
    pwd = os.environ.get("TQ_AUTH_PASS", "")
    if not user or not pwd:
        raise SystemExit("未找到快期账户凭据（TQ_AUTH_USER / TQ_AUTH_PASS，或 workbench/.tqenv）")

    import pandas as pd
    from tqsdk import TqApi, TqAuth

    book = json.loads(PB_PATH.read_text("utf-8"))
    api = TqApi(auth=TqAuth(user, pwd))
    try:
        jobs, kls, hists = [], {}, {}
        for pb in book["playbooks"]:
            main_sym = resolve_main(api, pb)
            if not main_sym:
                print(f"WARN {pb['id']} 主力解析失败，跳过")
                continue
            jobs.append((pb, main_sym))
            kls[pb["id"]] = api.get_kline_serial(main_sym, 86400, data_length=30)
            hists[pb["id"]] = api.get_kline_serial(cont_symbol(main_sym), 86400, data_length=1300)

        # 等数据包到位（日线历史要一会儿，最多 90 秒）
        deadline = time.time() + 90
        while time.time() < deadline:
            api.wait_update(deadline=deadline)
            ok = all(len(kls[p["id"]]) and kls[p["id"]].iloc[-1]["datetime"] > 0 for p, _ in jobs)
            if ok:
                break

        now_bj = pd.Timestamp.now(tz="Asia/Shanghai")
        rows = []
        for pb, main_sym in jobs:
            kdf = kls[pb["id"]]
            df = kdf[kdf["datetime"] > 0][["datetime", "open", "high", "low", "close", "volume"]].copy()
            df["dt"] = pd.to_datetime(df["datetime"].astype("int64"), unit="ns", utc=True) \
                .dt.tz_convert("Asia/Shanghai").dt.normalize()
            df = df.set_index("dt").sort_index()
            df = drop_unfinished(df, now_bj)
            if len(df) < 20:
                print(f"WARN {pb['id']} 日线不足 20 根（{len(df)}），跳过")
                continue
            bars = df.tail(20)

            closes = bars["close"].astype(float)
            highs = bars["high"].astype(float)
            lows = bars["low"].astype(float)
            last = float(closes.iloc[-1])
            ma5, ma20 = float(closes.tail(5).mean()), float(closes.tail(20).mean())
            atr = atr14(bars)
            bias = judge_bias(last, ma5, ma20)
            high20, low20 = float(highs.max()), float(lows.min())
            levels = calc_levels(last, high20, low20, ma5, ma20, atr, bias)
            levels = fix_ladder(levels, atr)
            advice = position_advice(last, high20, low20, ma5, ma20, atr, bias)
            thesis = gen_thesis(last, ma5, ma20, atr, bias, levels, advice)
            plan = gen_plan(levels, advice, bias, atr)

            # 历史分位（主连，长历史）
            pctile = None
            hdf = hists[pb["id"]]
            hd = hdf[hdf["datetime"] > 0]["close"].astype(float)
            hd = hd[hd > 0]
            if len(hd) > 200:
                pctile = {
                    "pct": round(float((hd < last).mean() * 100), 1),
                    "lo": round(float(hd.min())),
                    "hi": round(float(hd.max())),
                    "years": round(len(hd) / 244, 1),
                }

            asof = bars.index[-1].strftime("%Y-%m-%d")
            tech = (f"收盘 {last:.0f} · MA5 {ma5:.0f} MA20 {ma20:.0f} · ATR14 {atr:.0f} · "
                    f"趋势 {advice['direction']}"
                    + (f" · 历史分位 {pctile['pct']:.0f}%（近 {pctile['years']} 年）" if pctile else ""))

            pb["levels"] = levels
            pb["atr"] = round(atr)
            pb["thesis"] = thesis
            # 不再写 direction：人工/静态方向字段已废弃，盘面方向由前端 dynDir()
            # 用 MA5/MA20 + 20日区间 + 四象限 + 资金失衡实时合成，避免两套方向打架。
            pb.pop("direction", None)
            pb["plan"] = plan
            pb["advice"] = advice
            pb["pctile"] = pctile
            pb["techNote"] = tech
            pb["levelsAsOf"] = asof
            pb["mainSymbol"] = main_sym
            book.setdefault("_meta", {})[pb["id"]] = {
                "asof": asof, "last": round(last, 1), "ma5": round(ma5, 1),
                "ma20": round(ma20, 1), "atr": round(atr, 1), "bias": bias,
                "high20": round(high20), "low20": round(low20),
            }
            rows.append((pb["id"], pb["name"], bias, round(last), levels, plan))

        book["_meta"]["computedAt"] = now_bj.strftime("%Y-%m-%d %H:%M")

        # 打印核对表
        print(f"\n{'品种':<6}{'方向':<7}{'收盘':>8}{'MA5':>8}{'MA20':>8}{'ATR':>6}"
              f"{'压力':>8}{'一撑':>8}{'强撑':>8}{'失守':>8}{'破位':>8}{'滞涨区':>16}")
        for pid, name, bias, last, L, plan in rows:
            fz = f"{L['shortFadeZone'][0]}-{L['shortFadeZone'][1]}"
            m = book["_meta"][pid]
            print(f"{pid:<6}{bias:<7}{last:>8}{m['ma5']:>8.0f}{m['ma20']:>8.0f}{m['atr']:>6.0f}"
                  f"{L['resistance']:>8}{L['firstSupport']:>8}"
                  f"{L['strongSupport']:>8}{L['stopTarget']:>8}{L['shortBreakdown']:>8}{fz:>16}")

        # 逻辑自检：关键位严格递减 + 作战卡的 止损/入场/目标 顺序正确，否则不写盘
        bad = []
        for pid, name, bias, last, L, plan in rows:
            seq = [L["resistance"], L["firstSupport"], L["strongSupport"], L["stopTarget"]]
            if not all(seq[i] > seq[i + 1] for i in range(len(seq) - 1)):
                bad.append(f"{pid} 关键位顺序异常: {seq}")
            if plan:
                e, s, w, t = plan["entry"], plan["stop"], plan["waveTarget"], plan["trendTarget"]
                if bias == "bear" and not (s > e > w >= t):
                    bad.append(f"{pid} 空单 止损/入场/目标 顺序异常: stop={s} entry={e} wave={w} trend={t}")
                if bias == "bull" and not (s < e < w <= t):
                    bad.append(f"{pid} 多单 止损/入场/目标 顺序异常: stop={s} entry={e} wave={w} trend={t}")
        if bad:
            print("\n❌ 自检未通过：\n  " + "\n  ".join(bad))
            raise SystemExit(2)

        if dry:
            print("\n[dry-run] 未写盘")
        else:
            PB_PATH.with_suffix(".json.bak").write_text(
                json.dumps(json.loads(PB_PATH.read_text("utf-8")), ensure_ascii=False, indent=1), "utf-8")
            PB_PATH.write_text(json.dumps(book, ensure_ascii=False, indent=1), "utf-8")
            print(f"\n已写入 {PB_PATH}（原文件备份为 playbook.json.bak）")
            print("注意：服务端只在启动时读 playbook.json，需重启工作台才生效")
    finally:
        api.close()


if __name__ == "__main__":
    main()
