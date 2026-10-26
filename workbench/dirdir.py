"""方向倾向打分（后端权威实现）。

四块合成：日线 MA5/MA20 排列 ±1.5 · 20 日区间位置 ±0.5 · 四象限 ±2/±1 · 资金失衡 ≥5% ±1。

**唯一真相源在这里。** 前端 `static/index.html` 里的 `dirScore()` 只是同口径的兜底副本
（离线打开旧页面、或后端没下发 dir 字段时才用），两边公式与文案必须逐字一致；
改这里就必须同步改那边，`scripts/dir_parity.py` 会对拍两边输出。

术语说明见 docs/期货盯盘工作台-标签判定依据.md §2。
"""

from __future__ import annotations

import math

QUAD_SCORE = {
    "long_attack": 2,
    "short_cover": 1,
    "flat": 0,
    "long_exit": -1,
    "short_attack": -2,
}
QUAD_LABEL = {
    "long_attack": "多头进攻",
    "short_cover": "空头回补",
    "flat": "无量整理",
    "long_exit": "多头离场",
    "short_attack": "空头进攻",
}

MINUS = "−"          # U+2212，和前端模板里的减号一致（不是 ASCII '-'）


def _fixed(x: float, nd: int = 0) -> str:
    """模拟 JS 的 toFixed：四舍五入按「远离零」进，负数保留符号。"""
    p = 10 ** nd
    v = x * p
    r = math.floor(v + 0.5) if v >= 0 else -math.floor(-v + 0.5)
    return f"{r / p:.{nd}f}"


def _ma(vals, n):
    """N 日均值。

    刻意用显式累加而不是内置 sum()：Python 3.12 起 sum() 对浮点开了补偿求和，
    结果会和前端 JS 的 reduce（朴素左到右累加）在末位上差一点，
    导致 MA 取整后一个显示 2228、一个显示 2227。这里对齐 JS 的累加顺序。
    """
    if len(vals) < n:
        return None
    tot = 0.0
    for v in vals[-n:]:
        tot += v
    return tot / n


def score(rows, last, quad="flat", net_ratio=0.0, flow_bars=0):
    """算方向倾向总分。

    rows: 日线 K 线，形如 [[时间, 开, 高, 低, 收, 量], ...]（前复权无需处理，主连原始价）
    last: 最新价
    quad: 四象限（long_attack / short_cover / flat / long_exit / short_attack）
    net_ratio: 当日净流入失衡（%，正=多侧净流入）
    flow_bars: 资金流累计用了几根分钟线（0 表示数据还没起来，不参与打分）

    返回 {s, level, label, terms}：
      s     总分（0.5 的整数倍）
      level long / short / flat（动手判定用，±2 为界）
      label 强多 / 偏多 / 中性 / 偏空 / 强空（±2 / ±4 分档）
      terms 每块打分的明细文案（悬停说明里原样展示，和前端逐字一致）
    """
    closes = [r[4] for r in rows if len(r) > 4 and r[4] is not None]
    terms = []
    s = 0.0

    m5, m20 = _ma(closes, 5), _ma(closes, 20)
    if m5 is not None and m20 is not None:
        d = 1 if m5 > m20 else -1
        s += d * 1.5
        terms.append(
            f"MA5{'>' if d > 0 else '<'}MA20（{_fixed(m5)}/{_fixed(m20)}）"
            f"{'+' if d > 0 else MINUS}1.5"
        )
    else:
        terms.append("日线不足 20 根，MA 不计")

    if len(rows) >= 20:
        win = rows[-20:]
        hi = max(r[2] for r in win)
        lo = min(r[3] for r in win)
        if hi > lo:
            pos = (last - lo) / (hi - lo)      # 0 = 20 日区间底，1 = 区间顶
            d = 1 if pos >= 0.6 else (-1 if pos <= 0.4 else 0)
            if d:
                s += d * 0.5
                terms.append(
                    f"20日区间{'上' if d > 0 else '下'}沿 {_fixed(pos * 100)}% "
                    f"{'+' if d > 0 else MINUS}0.5"
                )
            else:
                terms.append(f"20日区间中部 {_fixed(pos * 100)}% 0")

    qs = QUAD_SCORE.get(quad or "flat", 0)
    if qs:
        s += qs
        terms.append(f"{QUAD_LABEL.get(quad, '无量整理')} {'+' if qs > 0 else ''}{qs}")

    nr = net_ratio or 0.0
    if flow_bars and abs(nr) >= 5:
        d = 1 if nr > 0 else -1
        s += d
        terms.append(f"资金失衡 {'+' if nr > 0 else ''}{_fixed(nr, 1)}% {'+1' if d > 0 else MINUS + '1'}")
    elif flow_bars:
        terms.append(f"资金失衡 {('+' if nr > 0 else '') + _fixed(nr, 1)}%（|·|<5% 不计）")

    s = float(_fixed(s, 1))
    level = "long" if s >= 2 else ("short" if s <= -2 else "flat")
    label = ("强多" if s >= 4 else "偏多" if s >= 2
             else "强空" if s <= -4 else "偏空" if s <= -2 else "中性")
    return {"s": s, "level": level, "label": label, "terms": terms}
