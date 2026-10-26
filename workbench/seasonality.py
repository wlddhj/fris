#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""季节性日历计算：五品种 1-12 月涨跌强弱。

为什么不用主力连续
------------------
`KQ.m@DCE.xxx` 是未复权的主力连续，换月当天会有合约价差跳空，
直接算月度收益会把"换合约"变成"涨跌"（第一版就踩了这个坑：
JD 4 月 +11.7% 明显失真）。

本脚本的做法（同合约内比较，天然无跳空）
------------------------------------
1. 取该品种全部 1/5/9 交割月合约（DCE 主力月）日线；
2. 每个合约各自算月末收盘价与当月成交量；
3. 每个自然月，选当月成交量最大、且上月末也有数据的合约，
   用「本合约本月末收盘 / 本合约上月末收盘 - 1」作为这个月的收益；
4. 按 1-12 月分组统计均值、中位数、胜率、样本年数、极值。

输出 workbench/seasonality.json
"""
import json
import os
import time
from pathlib import Path

import numpy as np
import pandas as pd
from tqsdk import TqApi, TqAuth

BASE = Path(__file__).resolve().parent
OUT = BASE / "seasonality.json"

PRODUCTS = [
    ("JD", "鸡蛋", "jd"),
    ("L", "塑料", "l"),
    ("PP", "聚丙烯", "pp"),
    ("M", "豆粕", "m"),
    ("C", "玉米", "c"),
]

MAIN_MONTHS = ("01", "05", "09")  # DCE 主力交割月
DATA_LEN = 420
START_YEAR = 20  # 合约年份 >= 2020


def month_end_table(frames: dict) -> pd.DataFrame:
    """把各合约日线压成 (合约, 年, 月) 的月末收盘 + 当月成交量。"""
    rows = []
    for c, df in frames.items():
        g = df.groupby([df.index.year, df.index.month])
        for (y, m), sub in g:
            if len(sub) == 0:
                continue
            rows.append({
                "c": c, "y": int(y), "m": int(m),
                "close": float(sub["close"].iloc[-1]),
                "vol": float(sub["volume"].sum()),
                "last_day": sub.index[-1].strftime("%Y-%m-%d"),
            })
    return pd.DataFrame(rows)


def monthly_returns(tab: pd.DataFrame) -> pd.DataFrame:
    """每月取量最大的合约，同合约算环比。"""
    out = []
    for (y, m), g in tab.groupby(["y", "m"]):
        g = g.sort_values("vol", ascending=False)
        py, pm = (y, m - 1) if m > 1 else (y - 1, 12)
        for _, r in g.iterrows():
            prev = tab[(tab.c == r["c"]) & (tab.y == py) & (tab.m == pm)]
            if len(prev) and prev.iloc[0]["close"] > 0:
                out.append({
                    "y": y, "m": m, "c": r["c"],
                    "r": (r["close"] / prev.iloc[0]["close"] - 1) * 100,
                    "vol": r["vol"],
                })
                break
    return pd.DataFrame(out).sort_values(["y", "m"]).reset_index(drop=True)


def stats_by_month(mr: pd.DataFrame) -> dict:
    months = []
    for m in range(1, 13):
        sub = mr[mr["m"] == m]
        vals = sub["r"].values
        if len(vals) == 0:
            months.append({"m": m, "n": 0, "avg": 0.0, "med": 0.0, "win": 0, "best": 0.0, "worst": 0.0})
            continue
        months.append({
            "m": m,
            "n": int(len(vals)),
            "avg": round(float(np.mean(vals)), 2),
            "med": round(float(np.median(vals)), 2),
            "win": int(round(float((vals > 0).mean()) * 100)),
            "best": round(float(np.max(vals)), 1),
            "worst": round(float(np.min(vals)), 1),
            "stdev": round(float(np.std(vals)), 1),
            "years": [
                {"y": int(y), "r": round(float(r), 1), "c": c}
                for y, r, c in zip(sub["y"].values, sub["r"].values, sub["c"].values)
            ],
        })
    # 该品种内 12 个月的相对强弱分位（0=最弱, 100=最强）
    avgs = [x["avg"] for x in months if x["n"]]
    for x in months:
        if not x["n"] or len(avgs) < 2:
            x["score"] = 50
        else:
            x["score"] = int(round(sum(1 for a in avgs if a < x["avg"]) / (len(avgs) - 1) * 100))
    return {"months": months}


def main():
    user, pwd = os.environ.get("TQ_AUTH_USER"), os.environ.get("TQ_AUTH_PASS")
    if not user or not pwd:
        raise SystemExit("未找到快期账户凭据（TQ_AUTH_USER / TQ_AUTH_PASS）")

    api = TqApi(auth=TqAuth(user, pwd))
    try:
        deadline = time.time() + 20
        while time.time() < deadline:
            api.wait_update(deadline=deadline)
            break

        now = pd.Timestamp.now(tz="Asia/Shanghai")
        result = {
            "generated_at": now.strftime("%Y-%m-%d %H:%M"),
            "method": "分合约月末收盘环比（每月取成交量最大且上月末有数据的合约），不跨合约拼接，无换月跳空",
            "products": [],
        }

        for pid, name, prod in PRODUCTS:
            t0 = time.time()
            lst = api.query_quotes(ins_class="FUTURE", exchange_id="DCE", product_id=prod, expired=True)
            conts = [
                c for c in lst
                if c.split(".")[1][-2:] in MAIN_MONTHS and int(c.split(".")[1][2:4]) >= START_YEAR
            ]
            conts.sort()
            ks = {c: api.get_kline_serial(c, 86400, data_length=DATA_LEN) for c in conts}
            dl = time.time() + 150
            while time.time() < dl:
                api.wait_update(deadline=dl)
                if all(k.iloc[-1]["datetime"] > 0 for k in ks.values()):
                    break

            frames = {}
            for c, k in ks.items():
                df = k[(k["datetime"] > 0) & k["close"].notna()][["datetime", "close", "volume"]].copy()
                if len(df) < 20:
                    continue
                df["dt"] = pd.to_datetime(df["datetime"].astype("int64"), unit="ns", utc=True)\
                    .dt.tz_convert("Asia/Shanghai").dt.normalize()
                frames[c] = df.set_index("dt").sort_index()[["close", "volume"]]

            tab = month_end_table(frames)
            mr = monthly_returns(tab)
            # 剔除本月未走完的月收益
            mr = mr[~((mr["y"] == now.year) & (mr["m"] == now.month))]

            st = stats_by_month(mr)
            st.update({
                "id": pid, "name": name, "product": prod,
                "contracts": len(conts),
                "samples": int(len(mr)),
                "range": f"{int(mr['y'].min())}-{int(mr['y'].max())}" if len(mr) else "-",
            })
            result["products"].append(st)
            print(f"{pid} {name}: 合约 {len(conts)} 个, 有效月样本 {len(mr)} ({st['range']}), 耗时 {time.time()-t0:.0f}s")

        OUT.write_text(json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")
        print("\n已写入", OUT)

        print("\n月均涨跌 %（括号内胜率/样本数）")
        print("品种  " + "".join(f"{m:>12}" for m in range(1, 13)))
        for p in result["products"]:
            cells = "".join(
                f"{x.get('avg', 0):>7.1f}({x.get('win', 0):>2}%/{x.get('n', 0)})"[:12].rjust(12)
                for x in p["months"]
            )
            print(f"{p['id']:<5}{cells}")
    finally:
        api.close()


if __name__ == "__main__":
    main()
