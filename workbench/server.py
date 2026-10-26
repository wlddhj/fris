"""期货盯盘工作台后端 (M1)

- TqSdk 行情会话: 多合约 quote + 1m/5m/日K 订阅, tick 驱动 wait_update 主循环
- WebSocket (port 8765): 每 0.5s 推送 update (quotes 全量 + 各K线序列最后2根), 新连接先推 init 全量
- HTTP (port 8000): 静态页面 static/index.html
- 凭据: 环境变量 TQ_AUTH_USER / TQ_AUTH_PASS, 不写代码不落日志
- 断线自动重连 (指数退避); 休市时段降级为周期广播静态快照, 不阻塞
"""

import asyncio
import contextlib
import datetime
import functools
import http.server
import json
import os
import re
import socketserver
import sys
import threading
import time
from pathlib import Path

import pandas as pd
from websockets.asyncio.server import serve, broadcast

from tqsdk import TqApi, TqAuth

import dirdir
from autosig import build_auto_signals
from extfeed import SinaExtFeed
from spot import SpotFeed
from sinograin import SinograinFeed
from longzhong import LongzhongFeed
from sci99 import Sci99Feed
from mysteel import MysteelFeed

BASE = Path(__file__).resolve().parent


def _load_env_file():
    """从 workbench/.tqenv 读快期凭据（KEY=VALUE），已存在的环境变量优先。"""
    f = BASE / ".tqenv"
    if not f.exists():
        return
    for line in f.read_text("utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip())


_load_env_file()

CONFIG = json.loads((BASE / "config.json").read_text("utf-8"))
SYMBOLS = CONFIG["symbols"]
PLAYBOOKS = json.loads((BASE / "playbook.json").read_text("utf-8"))

SEASON_FILE = BASE / "seasonality.json"


def _load_seasonality():
    """季节性日历(由 seasonality.py 离线算出并缓存), 缺失时降级为空, 不影响主流程."""
    try:
        return json.loads(SEASON_FILE.read_text("utf-8"))
    except Exception:
        return {"products": [], "method": "", "generated_at": ""}


SEASONALITY = _load_seasonality()
WS_PORT = CONFIG.get("ws_port", 8765)
HTTP_PORT = CONFIG.get("http_port", 8000)
PUSH_INTERVAL = CONFIG.get("push_interval_sec", 0.5)
PERIODS = {"60": 500, "300": 500, "86400": 250}   # 推给前端的根数（画图够用即可）
# 拉取根数：1 分钟要多取，资金流按「当日累计」口径，必须覆盖从夜盘 21:00 起的完整交易日
# （夜盘 3h + 日盘 4h 约 420 根，900 根留足余量，否则开盘头几分钟会漏掉前半段成交）
KLINE_FETCH = {"60": 900, "300": 500, "86400": 250}

INIT_READY_TIMEOUT = 120
NAMES = {}  # symbol -> instrument_name


def kline_dt_str(ns: float, period: str) -> str:
    ts = pd.to_datetime(int(ns), unit="ns", utc=True).tz_convert("Asia/Shanghai")
    return ts.strftime("%Y-%m-%d") if period == "86400" else ts.strftime("%m-%d %H:%M")


def now_beijing() -> str:
    return pd.Timestamp.now(tz="Asia/Shanghai").strftime("%Y-%m-%d %H:%M:%S")


def log(msg: str) -> None:
    print(f"[{now_beijing()}] {msg}", flush=True)


SEASON_MTIME = {"v": None}


def season_data() -> dict:
    """seasonality.json 按 mtime 热重载：重跑 seasonality.py 后不用重启服务。"""
    global SEASONALITY
    try:
        mtime = SEASON_FILE.stat().st_mtime
    except OSError:
        return SEASONALITY
    if SEASON_MTIME["v"] != mtime:
        SEASONALITY = _load_seasonality()
        SEASON_MTIME["v"] = mtime
        log(f"季节性数据已加载（生成于 {SEASONALITY.get('generated_at') or '未知'}）")
    return SEASONALITY


def season_brief(pid: str, ahead: int = 3):
    """当前月起 N 个月的历史季节性统计，卡片正文直接用它。

    只输出算得出来的数字：playbook.json 里的手写 seasonal 字段已删除
    （经验判断与 7 年样本对不上：鸡蛋写「11月全年压力最大」，实际 11 月 -1.51%
    只排第 4 弱，最弱是 8 月 -4.38%），前端没有样本时整行不显示。
    返回 {months:[{m,avg,win,n,best,worst}], range, nMin, nMax, generatedAt}。
    """
    data = season_data()
    prod = next((p for p in (data.get("products") or []) if p.get("id") == pid), None)
    if not prod:
        return None
    cells = prod.get("months") or []
    cur = pd.Timestamp.now(tz="Asia/Shanghai").month
    months = []
    for i in range(max(1, ahead)):
        m = (cur - 1 + i) % 12 + 1
        c = next((x for x in cells if x.get("m") == m), None)
        if not c or not c.get("n"):
            months.append({"m": m, "avg": None, "win": None, "n": 0})
            continue
        avg = round(float(c.get("avg") or 0.0), 2)
        if abs(avg) < 0.005:      # 避免 -0.0 这种显示
            avg = 0.0
        months.append({"m": m, "avg": avg, "win": c.get("win"), "n": c.get("n"),
                       "best": c.get("best"), "worst": c.get("worst")})
    ns = [x["n"] for x in cells if x.get("n")]
    return {
        "months": months,
        "range": prod.get("range") or "",
        "nMin": min(ns) if ns else 0,
        "nMax": max(ns) if ns else 0,
        "samples": prod.get("samples"),
        "generatedAt": data.get("generated_at") or "",
    }


def with_season(pb: dict) -> dict:
    """给 playbook 挂上自动季节性统计（payload 与热重载两条路径共用）。"""
    if isinstance(pb, dict) and pb.get("id"):
        pb["seasonalAuto"] = season_brief(pb["id"])
    return pb


def fut_unit(pb: dict) -> str:
    """期货报价单位，从 playbook 的 spec 文本里取（「5吨/手 · 报价元/500kg」）。

    现货价一律换算到这个单位再展示，才和盘面价格同量级、能直接比大小。
    取不到就按元/吨——五个品种里四个都是元/吨。
    """
    spec = str((pb or {}).get("spec") or "")
    m = re.search(r"报价\s*(元/[^\s·）)、,，]+)", spec)
    return m.group(1).replace("kg", "千克").replace("KG", "千克") if m else "元/吨"


class MarketHub:
    """TqSdk 会话线程: 维护 quotes/klines, 生成 init/update JSON 消息."""

    def __init__(self, symbols):
        self.symbols = symbols
        self.playbooks = PLAYBOOKS
        self.lock = threading.Lock()
        self.init_msg = json.dumps({"type": "init", "quotes": {}, "klines": {}, "playbooks": self.playbooks, "seasonality": SEASONALITY, "server_time": ""})
        self.update_msg = json.dumps({"type": "update", "quotes": {}, "klines": {}, "server_time": ""})
        self.connected = False
        self._push_fn = None  # 由 WsServer 注入: (msg_str) -> None, 线程安全
        self._live = None      # (quotes, klines) 实时引用, 供按需重建 init
        self.init_built_at = 0.0
        self.pb_by_sym = {}    # symbol -> playbook, 供资金流取合约乘数
        self.next_quotes = {}  # 主力 symbol -> 次主力 quote, 供月间价差
        self.ext = {}          # 外参合约 (SC 原油 / 豆二), 供产业信号自动判定
        self.sina = SinaExtFeed()  # 外盘: CBOT 大豆 / 布伦特 (TqSdk 没有, 走新浪)
        self.spot = SpotFeed()     # 现货: 生意社基准价 (100ppi, 免登录), 30 分钟一轮
        self._spot_snap = None     # 现货快照缓存: 5s 内复用, snapshot() 要算 age 有锁开销
        self._spot_at = 0.0
        self.sino = SinograinFeed()  # 收储: 中储粮网竞价公告/结果, 20 分钟一轮
        self._sino_snap = None
        self._sino_at = 0.0
        self.lz = LongzhongFeed()  # 隆众: PE/PP 库存与开工率, 30 分钟一轮(需账号 Cookie)
        self._lz_snap = None
        self._lz_at = 0.0
        self.zc = Sci99Feed()   # 卓创: 淘鸡(老母鸡)出栏量周数据, 30 分钟一轮(需账号 Cookie)
        self._zc_snap = None
        self._zc_at = 0.0
        self.ms = MysteelFeed()  # 我的农产品: 豆粕库存周数据, 30 分钟一轮(需账号 Cookie)
        self._ms_snap = None
        self._ms_at = 0.0
        self._bh_snap = None      # 基差历史缓存: 10 分钟扫一次库, 不需要每拍都查
        self._bh_at = 0.0
        self._auto_cache = None    # 自动信号缓存: 5s 内复用, 避免每 0.5s 重跑 pandas
        self._auto_at = 0.0

    def attach_push(self, fn):
        self._push_fn = fn

    def push_to_all(self, msg: str):
        if self._push_fn:
            with contextlib.suppress(Exception):
                self._push_fn(msg)

    # ---- 消息构建 (锁内) ----
    @staticmethod
    def _f(v):
        """float 消毒: NaN/None -> 0 (NaN 会生成非法 JSON, JS 端 parse 失败)."""
        try:
            f = float(v)
            return f if f == f else 0.0
        except (TypeError, ValueError):
            return 0.0

    _FLOW_CACHE = {}

    def _flow_amount(self, sym, klines):
        """当日资金流（累计口径），对标参考站 tq-feeder.kline_pack。

        两个关键口径（都影响方向判断，不是细节）：
        1. 时间窗：只统计「当前交易日」的 1 分钟 K 线，夜盘（20:00 后）计入次一交易日。
           原来是固定取近 30 根——收盘后窗口会停在 14:30-15:00 那半小时，且可能跨日/跨夜盘断裂，
           金额是"30 分钟窗口"的，不能当全天看，换个时刻还会整体翻转。
        2. 方向：逐根用「收盘 vs 前一根收盘」，而不是「收盘 vs 开盘」。
           单根低开高走但整体仍在下跌时，收>开 会误记成主动多，下跌趋势里系统性高估多头
           （实测 PP 同一时刻：收vs开 多头 53.8%，收vs前收 47.0%，直接跨过中轴反向）。
        首根没有前收盘不计入；但交易日首根会用前一交易日最后一根的收盘做基准
        （隔夜跳空本身就是资金态度，计入），只有整段序列的第一根才会被跳过。

        本质仍是 K 线级价格变化的近似，不是逐笔主动买卖方向，噪声大，只作强弱参照。

        额外输出「持仓动作归属」(oi_long / oi_short)：同一批分钟 K 线，用 close_oi 的逐根变化
        按价格方向归到动作方——涨+增仓=多头增仓、跌+减仓=多头离场（都算多方动作）；
        跌+增仓=空头增仓、涨+减仓=空头回补（都算空方动作）。四象限命名与 TqSdk ta 模块一致。
        这是「仓往哪边挪」，与「钱往哪边砸」（成交额）是两个口径，不能互相替代：
        减仓日成交额可能很大但持仓动作很小，只看资金流会把「平仓离场」误读成「新资金进场」。
        """
        empty = {"long": 0.0, "short": 0.0, "net": 0.0, "bars": 0,
                 "start": "", "trunc": False, "date": "",
                 "oi_long": 0.0, "oi_short": 0.0, "oi_net": 0.0, "oi_bars": 0}
        kdf = klines.get((sym, "60")) if klines else None
        if kdf is None or len(kdf) < 2:
            return empty

        now = datetime.datetime.now()
        # 20:00 之后的夜盘归属次一交易日（交易所口径：夜盘 21:00 起计入 T+1）
        cur = now.date() + datetime.timedelta(days=1) if now.hour >= 20 else now.date()
        last_dt = int(kdf["datetime"].iloc[-1]); last_v = float(kdf["volume"].iloc[-1])
        key = (cur, last_dt, last_v)
        hit = self._FLOW_CACHE.get(sym)
        if hit and hit[0] == key:
            return hit[1]

        pb = self.pb_by_sym.get(sym) or {}
        # 金额一律用「元/点」(pointValue)。鸡蛋报价是元/500kg，1 点值 10 元、合约单位只有 5 吨，
        # 用 contractUnit 会把 JD 的金额算少一半（其余四品种两者相等，不受影响）。
        mult = float(pb.get("pointValue") or pb.get("contractUnit") or 10)
        try:
            bj = pd.to_datetime(kdf["datetime"].astype("int64") + 8 * 3600 * 10 ** 9, unit="ns")
            td = bj.dt.normalize() + pd.to_timedelta((bj.dt.hour >= 20).astype(int), unit="D")
            mask = td == pd.Timestamp(cur)
            c = kdf["close"].astype("float64")
            prev = c.shift(1)
            amt = kdf["volume"].astype("float64") * c * mult
            valid = mask & prev.notna() & (amt > 0)
            up = float(amt[valid & (c >= prev)].sum())
            dn = float(amt[valid & (c < prev)].sum())
            idx = kdf.index[mask]
            bars = int(valid.sum())
            start = kline_dt_str(kdf.at[idx[0], "datetime"], "60") if len(idx) else ""
            trunc = bool(len(idx) and idx[0] == kdf.index[0])
            # 持仓动作归属：ΔOI 按「价格方向 × 增/减仓」归到多方或空方
            if "close_oi" in kdf.columns:
                oi_s = kdf["close_oi"].astype("float64")
                doi = oi_s - oi_s.shift(1)
                m_up, m_dn = valid & (c >= prev), valid & (c < prev)
                add, cut = doi > 0, doi < 0
                # 多方：涨+增仓（多头增仓）、跌+减仓（多头离场）
                oi_long = float(doi[m_up & add].sum()) + float(-doi[m_dn & cut].sum())
                # 空方：跌+增仓（空头增仓）、涨+减仓（空头回补）
                oi_short = float(doi[m_dn & add].sum()) + float(-doi[m_up & cut].sum())
                oi_bars = int((valid & doi.notna()).sum())
            else:
                oi_long = oi_short = 0.0
                oi_bars = 0
        except Exception as e:
            log(f"资金流计算失败 {sym}: {e}")
            self._FLOW_CACHE[sym] = (key, empty)
            return empty

        # 内部按亿保留 4 位（小合约/清淡时段金额小，留 2 位会被抹成 0.00），前端再格式化成 2 位
        out = {"long": round(up / 1e8, 4), "short": round(dn / 1e8, 4),
               "net": round((up - dn) / 1e8, 4), "bars": bars, "start": start,
               "trunc": trunc, "date": cur.isoformat(),
               "oi_long": round(max(oi_long, 0.0), 0), "oi_short": round(max(oi_short, 0.0), 0),
               "oi_net": round(oi_long - oi_short, 0), "oi_bars": oi_bars}
        self._FLOW_CACHE[sym] = (key, out)
        return out

    def _term_row(self, sym, last, pre):
        """月间价差: 主力 - 次主力. 次主力按 DCE 主力月 01/05/09 顺推, 只订阅行情不订阅K线, 连接开销小."""
        nq = self.next_quotes.get(sym)
        if nq is None:
            return None
        last2 = self._f(nq.last_price)
        # 次主力同样以昨结算为基准，和主力保持同一口径（否则价差"日变"会混两个基准）
        pre2 = self._f(getattr(nq, "pre_settlement", 0)) or self._f(nq.pre_close)
        if not last2:
            return None
        nxt = str(getattr(nq, "instrument_id", "") or "")
        return {
            "sym": nxt,
            "last": round(last2, 1),
            "spread": round(last - last2, 1) if last else None,
            "chg": round((last - pre) - (last2 - pre2), 1) if (last and pre and pre2) else None,
        }

    def _quote_row(self, sym, q, klines=None):
        last = self._f(q.last_price)
        pre = self._f(q.pre_close)
        settle = self._f(getattr(q, "pre_settlement", 0)) or pre
        # 涨跌幅一律以「昨结算」为基准（交易所口径，也是行情软件的默认口径）。
        # 原来用昨收，会出现"页面显示 +0.81% 上涨、四象限却判空头进攻"的自相矛盾——
        # 昨收与昨结算可以差几十上百点，两个基准混用必然打架（实测塑料差 86 点，方向相反）。
        basis = settle or pre
        chg = round(last - basis, 2) if (last and basis) else 0.0
        pct = round(chg / basis * 100, 2) if (last and basis) else 0.0
        oi = int(self._f(q.open_interest))
        pre_oi = int(self._f(getattr(q, "pre_open_interest", 0)))
        oi_chg = oi - pre_oi if pre_oi else 0
        base = pre_oi if pre_oi else max(oi - oi_chg, 1)
        oi_pct = round(oi_chg / base * 100, 2) if base else 0.0

        # 四象限 (参考台 FundFlow.tsx: 价格相对昨结算的涨跌 × 持仓变化率, 1% 为阈值)
        chg_settle = (last - settle) if (last and settle) else 0.0
        if abs(oi_pct) < 1.0:
            quad = "flat"
        elif chg_settle > 0:
            quad = "long_attack" if oi_chg > 0 else "short_cover"
        else:
            quad = "short_attack" if oi_chg > 0 else "long_exit"

        flow = self._flow_amount(sym, klines)
        pb = self.pb_by_sym.get(sym) or {}
        # 现货与基差：现货价 - 本项目主连最新价。
        # 现货价按期货报价单位换算过（鸡蛋元/斤 或 元/公斤 → 元/500kg），才能直接相减。
        sp_snap = self._spot_snapshot()
        sitem = (sp_snap.get("items") or {}).get(pb.get("id"))
        src = "生意社"
        asof = (sp_snap.get("fetchedAt") or "")[:16]
        stale = bool(sp_snap.get("stale"))
        # 鸡蛋现货优先用卓创主产区均价：生意社经常抓不到，而且它报的是「基准价」，
        # 不全等于信号要的「主产区均价」。卓创日报每交易日 15:30 发，稳定得多。
        zc_egg = ((self._zc_snapshot().get("items") or {}).get("egg_price") or {})
        zp = zc_egg.get("parsed") or {}
        if pb.get("id") == "JD" and zc_egg.get("ok") and zp.get("value"):
            sitem = {"name": "鸡蛋·主产区均价", "spot": zp["value"],
                     "unit": zp.get("unit") or "元/斤",
                     "spotFut": zp.get("spotFut") or round(zp["value"] * 1000, 0)}
            src = "卓创"
            asof = f"{zc_egg.get('date') or ''} 15:30"
            stale = False
        spot_row = None
        # 收盘后 TqSdk 把报价清成 0，若直接用 last 算基差，15:00~21:00 整块现货行会消失，
        # 新接的卓创均价也看不见。这里退回日线最后一根收盘价，并标注「收盘口径」。
        basis_last, closed_px = last, False
        if not basis_last:
            try:
                df = (klines or {}).get((sym, "86400"))
                if df is not None and len(df):
                    basis_last = float(df["close"].iloc[-1])
                    closed_px = basis_last > 0
            except Exception:
                basis_last = 0
        if sitem and basis_last:
            b = round(sitem["spotFut"] - basis_last, 2)
            pct_b = round(b / basis_last * 100, 2)
            atr = self._f(pb.get("atr")) or 0.0
            # 基差历史（近 45 天，每天收盘附近一条）：给前端算分位与日间变化。
            # 按 ATR 归一（b/atr）而不是按百分比，是因为五个品种的基差量级差一个数量级
            # （鸡蛋 469 点 = 14.6%，豆粕 60 点 = 1.8%），只有 ATR 倍数能横向比"深不深"。
            hist = (self._basis_hist().get(pb.get("id")) or [])
            hrow = None
            if len(hist) >= 3:
                today = hist[-1]["d"]
                prev = [h for h in hist if h["d"] != today]
                pool = prev or hist
                vals = [h["basis"] for h in pool]
                lo, hi = min(vals), max(vals)
                below = sum(1 for v in vals if v < b)
                hrow = {
                    "n": len(pool),
                    "lo": round(lo, 1), "hi": round(hi, 1),
                    "pct": round(below / len(vals) * 100, 0),
                    "chg": round(b - pool[-1]["basis"], 1),
                    "from": pool[0]["d"], "to": pool[-1]["d"],
                    "series": [[h["d"][5:], h["basis"]] for h in hist[-20:]],
                }
            spot_row = {
                "id": pb.get("id"),
                "name": sitem["name"],
                "spot": sitem["spot"],
                "unit": sitem["unit"],
                "spotFut": sitem["spotFut"],
                "futUnit": fut_unit(pb),
                "fut": round(basis_last, 1),
                "basis": b,
                "basisPct": pct_b,
                "atr": atr,
                "basisAtr": round(b / atr, 2) if atr else None,
                "hist": hrow,
                "asof": asof,
                "src": src,
                "closed": closed_px,
                "stale": stale,
            }
        # 金额一律用「元/点」(pointValue)。鸡蛋报价是元/500kg，1 点值 10 元、合约单位只有 5 吨，
        # 用 contractUnit 会把 JD 的金额算少一半（其余四品种两者相等，不受影响）。
        mult = float(pb.get("pointValue") or pb.get("contractUnit") or 10)
        vol = int(self._f(q.volume))
        # 当日成交额 / 持仓市值（亿）：判断流动性与资金流噪声大小的量纲基准
        flow_amt = round(vol * last * mult / 1e8, 1) if (vol and last) else 0.0
        oi_value = round(oi * last * mult / 1e8, 1) if (oi and last) else 0.0
        net_ratio = round(flow["net"] / flow_amt * 100, 1) if flow_amt else 0.0
        # 持仓动作多空占比（手数口径）：与下面的成交额占比区分，一个看仓、一个看钱
        act_tot = (flow.get("oi_long") or 0) + (flow.get("oi_short") or 0)
        oi_ratio = round((flow.get("oi_long") or 0) / act_tot * 100, 1) if act_tot else 0.0
        # 多空力量（参考台 FundFlow.tsx 口径）：占比不是逐笔统计出来的，而是
        #   ① 四象限先给一个基准占比 D：多头进攻 100 / 空头进攻 0 / 空头回补 35 / 多头离场 65 / 无量 50
        #   ② 再按持仓变化率强度从 50% 向 D 收敛：long = 50 + (D-50)*(0.5+0.5*min(|ΔOI%|/3,1))
        # 所以变化率越小越接近对半，≥3% 时完全取基准值。和参考台逐位对得上。
        quad_base = {"long_attack": 100, "short_attack": 0, "short_cover": 35, "long_exit": 65}
        strength = min(abs(oi_pct) / 3.0, 1.0)
        d_base = quad_base.get(quad, 50)
        oi_ratio_ref = int(round(50 + (d_base - 50) * (0.5 + 0.5 * strength)))
        # 新增头寸（亿）= ΔOI × 价 × 乘数；减仓为负。参考台用合约单位，这里统一用元/点，
        # 只有鸡蛋两者不同（5 吨 vs 10 元/点），所以鸡蛋的金额是本值的一半。
        oi_amt = round(oi_chg * last * mult / 1e8, 2) if (oi_chg and last) else 0.0
        row = {
            "symbol": sym,
            "name": NAMES.get(sym, sym),
            "last": last,
            "chg": chg,
            "pct": pct,
            "settle": settle,
            "chg_settle": round(chg_settle, 2),
            "oi_chg": oi_chg,
            "oi_pct": oi_pct,
            "quad": quad,
            "flow_long": flow["long"],
            "flow_short": flow["short"],
            "flow_net": flow["net"],
            "flow_bars": flow["bars"],
            "flow_start": flow["start"],
            "flow_trunc": flow["trunc"],
            "flow_date": flow["date"],
            "flow_amt": flow_amt,
            "oi_value": oi_value,
            "net_ratio": net_ratio,
            # 持仓动作归属（手）：多方/空方各自动了多少仓，及多方占比
            "oi_long": flow.get("oi_long", 0.0),
            "oi_short": flow.get("oi_short", 0.0),
            "oi_net": flow.get("oi_net", 0.0),
            "oi_ratio": oi_ratio,
            "oi_bars": flow.get("oi_bars", 0),
            # 参考台口径的多空力量：占比（%）+ 新增头寸（亿）
            "oi_ratio_ref": oi_ratio_ref,
            "oi_amt": oi_amt,
            "volume": vol,
            "open_interest": oi,
            # 日内位要用今高/今低/今开（昨结±ATR 是参考站的日内锚，缺这三个字段算不出来）
            "high": self._f(getattr(q, "high", 0)),
            "low": self._f(getattr(q, "low", 0)),
            "open": self._f(getattr(q, "open", 0)),
            "bid1": self._f(q.bid_price1),
            "ask1": self._f(q.ask_price1),
            "datetime": str(q.datetime or ""),
            "spot": spot_row,
            "term": self._term_row(sym, last, basis),
        }
        row["dir"] = self._dir_row(sym, klines, last, quad, net_ratio, flow.get("bars", 0))
        return row

    def _dir_row(self, sym, klines, last, quad, net_ratio, flow_bars):
        """方向倾向：后端算好随行情下发（dirdir.py 是唯一真相源）。

        以前只有前端 index.html 里的一份 JS 实现，聊天里问「现在偏哪边」时
        拿不到同口径的分，只能另写一套，两边必然漂移。现在后端统一下发，
        前端优先用它（缺字段才回落到本地 dirScore 兜底）。
        """
        try:
            if not last or klines is None:
                return None
            kdf = klines.get((sym, "86400"))
            if kdf is None or not len(kdf):
                return None
            rows = self._kline_rows(kdf, "86400", 45)
            return dirdir.score(rows, last, quad, net_ratio, flow_bars)
        except Exception as exc:
            log(f"WARN {sym} 方向打分失败: {type(exc).__name__}: {str(exc)[:100]}")
            return None

    def _kline_rows(self, kdf, period, n):
        tail = kdf.tail(n)
        rows = []
        for _, r in tail.iterrows():
            if r["datetime"] <= 0 or pd.isna(r["close"]) or pd.isna(r["open"]):
                continue
            rows.append([
                kline_dt_str(r["datetime"], period),
                round(self._f(r["open"]), 2), round(self._f(r["high"]), 2),
                round(self._f(r["low"]), 2), round(self._f(r["close"]), 2),
                int(self._f(r["volume"])),
            ])
        return rows

    def _reload_playbooks(self):
        """playbook.json 落盘后自动热重载（保留 symbol 映射），改关键位无需重启服务。"""
        p = BASE / "playbook.json"
        try:
            mtime = p.stat().st_mtime
        except OSError:
            return
        if mtime == getattr(self, "_pb_mtime", None):
            return
        try:
            fresh = json.loads(p.read_text("utf-8"))
            syms = {pb.get("id"): pb.get("symbol")
                    for pb in (self.playbooks.get("playbooks") or [])
                    if isinstance(pb, dict) and pb.get("symbol")}
            for pb in fresh.get("playbooks", []):
                if syms.get(pb.get("id")):
                    pb["symbol"] = syms[pb["id"]]
                with_season(pb)   # 季节性统计随时间走（跨月自动换月份），重载时一并重算
            self.playbooks = fresh
            self._pb_mtime = mtime
            log("playbook.json 已热重载")
        except Exception as exc:
            log(f"WARN playbook 热重载失败: {exc}")
            self._pb_mtime = mtime

    def _spot_snapshot(self):
        """生意社现货快照: 5s 缓存. 抓取线程 30 分钟才更新一次, 没必要每 tick 加锁。"""
        now = time.time()
        if self._spot_snap is None or now - self._spot_at > 5.0:
            self._spot_snap = self.spot.snapshot()
            self._spot_at = now
        return self._spot_snap

    def _basis_hist(self):
        """基差历史（按品种、按交易日）: 10 分钟缓存。
        数据从 archive.db 的推送快照里捞（现货行本来就随行情推送落档），
        扫库比抓网页便宜得多，但也没必要每 0.5s 一次——基差日内变化远慢于盘面。"""
        now = time.time()
        if self._bh_snap is None or now - self._bh_at > 600.0:
            try:
                import archivedb
                self._bh_snap = archivedb.basis_series(45)
            except Exception as exc:
                log(f"WARN 基差历史读取失败: {type(exc).__name__}: {str(exc)[:100]}")
                self._bh_snap = {}
            self._bh_at = now
        return self._bh_snap

    def _sino_snapshot(self):
        """中储粮快照: 10s 缓存. 20 分钟才更新一次, 不必每 tick 加锁。"""
        now = time.time()
        if self._sino_snap is None or now - self._sino_at > 10.0:
            self._sino_snap = self.sino.snapshot()
            self._sino_at = now
        return self._sino_snap

    def _lz_snapshot(self):
        """隆众快照: 30s 缓存. 抓取线程 30 分钟才更新一轮, 不必每 tick 加锁;
        加这层缓存是因为它在 _quote_row 里没有调用点, 只在 payload 与信号里各取一次。"""
        now = time.time()
        if self._lz_snap is None or now - self._lz_at > 30.0:
            self._lz_snap = self.lz.get() or {}
            self._lz_at = now
        return self._lz_snap

    def _zc_snapshot(self):
        """卓创快照: 30s 缓存. 周数据, 抓取线程 30 分钟一轮, URL 未变即复用。"""
        now = time.time()
        if self._zc_snap is None or now - self._zc_at > 30.0:
            self._zc_snap = self.zc.get() or {}
            self._zc_at = now
        return self._zc_snap

    def _ms_snapshot(self):
        """我的农产品快照: 30s 缓存. 周数据, 抓取线程 30 分钟一轮, URL 未变即复用。"""
        now = time.time()
        if self._ms_snap is None or now - self._ms_at > 30.0:
            self._ms_snap = self.ms.get() or {}
            self._ms_at = now
        return self._ms_snap

    def _auto_payload(self, quotes, klines):
        """产业信号自动判定结果: {playbook_id: {signal_id: {state, detail}}}。
        判定基于日线, 5s 缓存足够; 单条规则异常在 autosig 内部兜住, 不影响行情推送。"""
        now = time.time()
        if self._auto_cache is not None and now - self._auto_at < 5.0:
            return self._auto_cache
        try:
            self._auto_cache = build_auto_signals(self.playbooks, klines, self.ext,
                                                  self.sina.snapshot(), self._spot_snapshot(),
                                                  self._sino_snapshot(), self._lz_snapshot(),
                                                  self._zc_snapshot(), self._ms_snapshot(),
                                                  quotes=quotes)
            self._auto_at = now
        except Exception as exc:
            log(f"WARN 自动信号计算失败: {type(exc).__name__}: {str(exc)[:120]}")
            return self._auto_cache or {}
        return self._auto_cache

    def _rebuild_init(self, quotes, klines):
        self._reload_playbooks()
        payload = {
            "type": "init",
            "server_time": now_beijing(),
            "connected": self.connected,
            "playbooks": self.playbooks,
            "seasonality": SEASONALITY,
            "autoSignals": self._auto_payload(quotes, klines),
            "sino": self._sino_snapshot(),
            "lz": self._lz_snapshot(),
            "zc": self._zc_snapshot(),
            "ms": self._ms_snapshot(),
            "quotes": {s: self._quote_row(s, quotes[s], klines) for s in self.symbols},
            "klines": {s: {p: self._kline_rows(klines[(s, p)], p, PERIODS[p]) for p in PERIODS} for s in self.symbols},
        }
        self.init_msg = json.dumps(payload, ensure_ascii=False)
        self.init_built_at = time.time()

    def _rebuild_update(self, quotes, klines):
        payload = {
            "type": "update",
            "server_time": now_beijing(),
            "connected": self.connected,
            "autoSignals": self._auto_payload(quotes, klines),
            "sino": self._sino_snapshot(),
            "lz": self._lz_snapshot(),
            "zc": self._zc_snapshot(),
            "ms": self._ms_snapshot(),
            "quotes": {s: self._quote_row(s, quotes[s], klines) for s in self.symbols},
            "klines": {s: {p: self._kline_rows(klines[(s, p)], p, 2) for p in PERIODS} for s in self.symbols},
        }
        self.update_msg = json.dumps(payload, ensure_ascii=False)
        # SQLite 落档：把精简快照交给归档器（内部 5 分钟节流 + 收盘定格，存不存它自己决定）
        try:
            import archivedb
            _kind = archivedb.maybe_record({
                "server_time": payload["server_time"],
                "autoSignals": payload["autoSignals"],
                "sino": payload["sino"], "lz": payload["lz"],
                "zc": payload["zc"], "ms": payload["ms"],
                "quotes": payload["quotes"],
            })
            if _kind:
                log(f"落档: {_kind}")
        except Exception as exc:
            log(f"WARN 归档失败: {type(exc).__name__}: {str(exc)[:120]}")

    # ---- TqSdk 会话 (含重连) ----
    def run_forever(self):
        backoff = 2
        while True:
            try:
                self._session()
                backoff = 2
            except Exception as exc:
                log(f"ERROR 会话异常: {type(exc).__name__}: {str(exc)[:200]}")
            with self.lock:
                self.connected = False
                self._append_status("disconnected")
            log(f"{backoff}s 后重连...")
            time.sleep(backoff)
            backoff = min(backoff * 2, 60)

    def _append_status(self, status):
        for msg_attr in ("init_msg", "update_msg"):
            try:
                d = json.loads(getattr(self, msg_attr))
            except Exception:
                d = {"type": "init", "quotes": {}, "klines": {}}
            d["connected"] = status == "connected"
            d["status"] = status
            setattr(self, msg_attr, json.dumps(d, ensure_ascii=False))

    @staticmethod
    def _next_main_symbol(sym):
        """由主力合约顺推下一个主力月 (DCE 主力月为 01/05/09)."""
        try:
            ex, code = sym.split(".")
            prod = "".join(c for c in code if c.isalpha())
            digits = "".join(c for c in code if c.isdigit())
            if len(digits) != 4:
                return None
            yy, mm = int(digits[:2]), int(digits[2:])
            for m in (1, 5, 9):
                if m > mm:
                    return f"{ex}.{prod}{yy:02d}{m:02d}"
            return f"{ex}.{prod}{(yy + 1) % 100:02d}01"
        except Exception:
            return None

    def _resolve_symbols(self, api):
        """剧本按品种配置, 订阅时动态解析当前主力合约 (写死具体合约会因换月/未上市失效)."""
        resolved = {}  # symbol -> playbook
        for pb in PLAYBOOKS["playbooks"]:
            try:
                conts = api.query_cont_quotes(exchange_id=pb["exchange"], product_id=pb["product"])
            except Exception as exc:
                log(f"WARN {pb['id']} 主力合约解析失败: {type(exc).__name__}")
                continue
            if not conts:
                log(f"WARN {pb['id']} 无主力合约, 跳过")
                continue
            if isinstance(conts, dict):
                main = max(conts.items(), key=lambda kv: kv[1])[0]
            else:
                main = list(conts)[0]
            resolved[main] = pb
            log(f"解析主力: {pb['id']} {pb['name']} -> {main}")
        return resolved

    def _playbooks_payload(self, resolved):
        return {
            "confirmBars": PLAYBOOKS.get("confirmBars", 3),
            "nearPct": PLAYBOOKS.get("nearPct", 0.01),
            "playbooks": [with_season({**pb, "symbol": sym}) for sym, pb in resolved.items()],
        }

    def _session(self):
        user = os.environ.get("TQ_AUTH_USER", "")
        pwd = os.environ.get("TQ_AUTH_PASS", "")
        if not user or not pwd:
            raise RuntimeError("缺少 TQ_AUTH_USER / TQ_AUTH_PASS 环境变量")
        api = TqApi(auth=TqAuth(user, pwd))
        try:
            resolved = self._resolve_symbols(api)
            if not resolved:
                raise RuntimeError("没有任何品种解析到主力合约")
            symbols = list(resolved.keys())
            with self.lock:
                self.symbols = symbols
                self.playbooks = self._playbooks_payload(resolved)
                self.pb_by_sym = dict(resolved)

            quotes = {}
            klines = {}
            for s in symbols:
                q = api.get_quote(s)
                quotes[s] = q
                NAMES[s] = resolved[s]["name"]
            # 次主力 (仅行情, 用于月间价差)
            next_quotes = {}
            for s in symbols:
                nxt = self._next_main_symbol(s)
                if not nxt or nxt == s:
                    continue
                try:
                    next_quotes[s] = api.get_quote(nxt)
                except Exception as exc:
                    log(f"WARN 次主力 {nxt} 订阅失败: {type(exc).__name__}")
            with self.lock:
                self.next_quotes = next_quotes
            log(f"次主力订阅: {len(next_quotes)}/{len(symbols)}")

            for s in symbols:
                for p, n in PERIODS.items():
                    klines[(s, p)] = api.get_kline_serial(s, int(p), data_length=KLINE_FETCH.get(p, n))

            # 外参合约: 原油(成本端) / 豆二(进口大豆成本) / 豆油 / 玉米淀粉,
            # 供产业信号自动判定（m2 盘面榨利、c3 淀粉加工利润都靠这几腿）
            # 订阅失败不影响主流程, 对应信号显示"数据不足"
            #
            # 对齐月份: 豆二/豆油/淀粉必须跟主合约同月, 否则价差里混进月间结构
            # （比如 cs2611 对 c2701 算"淀粉-玉米价差"，量的是跨月而不是加工利润）。
            # 月份取合约里出现最多的那个（JD2612 这类个别月份不采纳），再按它找同月合约。
            _months = {}
            for _s in symbols:
                _m = re.search(r"(\d{4})$", _s.split(".")[-1])
                if _m:
                    _months[_m.group(1)] = _months.get(_m.group(1), 0) + 1
            main_month = max(_months, key=_months.get) if _months else ""
            if main_month:
                log(f"外参对齐月份: {main_month}（{_months[main_month]}/{len(symbols)} 个合约同月）")

            ext = {}
            for key, ex, prod, label, align in (
                    ("SC", "INE", "sc", "上海原油", False), ("B", "DCE", "b", "豆二", True),
                    ("Y", "DCE", "y", "豆油", True), ("CS", "DCE", "cs", "玉米淀粉", True)):
                try:
                    conts = api.query_cont_quotes(exchange_id=ex, product_id=prod)
                    if not conts:
                        log(f"WARN 外参 {label} 无主力合约")
                        continue
                    codes = list(conts) if not isinstance(conts, dict) else list(conts)
                    # query_cont_quotes 只给主力/次主力，远月拿不到；同月对齐要枚举全合约
                    allins = []
                    try:
                        allins = api.query_quotes(ins_class="FUTURE", exchange_id=ex,
                                                  product_id=prod) or []
                    except Exception:
                        allins = []
                    main = None
                    if align and main_month:
                        want = prod + main_month
                        main = next((c for c in allins
                                     if c.split(".")[-1] == want), None)
                        if not main:
                            log(f"WARN 外参 {label} 无 {want} 合约, 退回主力"
                                f"（跨月，价差含月间结构）")
                    if not main:
                        main = (max(conts.items(), key=lambda kv: kv[1])[0]
                                if isinstance(conts, dict) else codes[0])
                    ext[key] = {
                        "symbol": main,
                        "quote": api.get_quote(main),
                        "kline": api.get_kline_serial(main, 86400, data_length=120),
                    }
                    log(f"外参订阅: {label} -> {main}")

                    # 淀粉-玉米价差必须同月：远月 cs2701/c2701 大多还没挂牌，退回的主力
                    # cs2611 只有配上 c2611 才是干净的加工利润，跟玉米主卡 c2701 混算会
                    # 把月间结构算进价差里。所以给 CS 单独挂一条同月玉米腿。
                    if key == "CS":
                        _mm = re.search(r"(\d{4})$", main.split(".")[-1])
                        if _mm:
                            want = "c" + _mm.group(1)
                            try:
                                call = api.query_quotes(ins_class="FUTURE", exchange_id="DCE",
                                                        product_id="c") or []
                            except Exception:
                                call = []
                            mate = next((c for c in call if c.split(".")[-1] == want), None)
                            if mate:
                                ext["C_STARCH"] = {
                                    "symbol": mate,
                                    "quote": api.get_quote(mate),
                                    "kline": api.get_kline_serial(mate, 86400, data_length=120),
                                }
                                log(f"外参订阅: 淀粉同月玉米腿 -> {mate}")
                            else:
                                log(f"WARN 外参 淀粉同月玉米腿 {want} 未挂牌, 价差退回玉米主合约（跨月）")
                except Exception as exc:
                    log(f"WARN 外参 {label} 订阅失败: {type(exc).__name__}: {str(exc)[:80]}")
            with self.lock:
                self.ext = ext

            # 等待初始数据就绪 (deadline + 退出条件): K线有效且 quote 已带时间戳
            # wait_update 用 1s 短切片: 休市无 tick 时会一直阻塞到 deadline, 而K线其实
            # 在订阅时就已经填充好了, 否则每次休市重启都要空等满 120 秒。
            deadline = time.time() + INIT_READY_TIMEOUT
            ready = set()
            while len(ready) < len(klines) and time.time() < deadline:
                api.wait_update(deadline=min(deadline, time.time() + 1.0))
                for key, kdf in klines.items():
                    if key in ready:
                        continue
                    if kdf.iloc[-1]["datetime"] > 0 and not pd.isna(kdf.iloc[-1]["close"]) \
                       and quotes[key[0]].datetime:
                        ready.add(key)
            missing = len(klines) - len(ready)
            if missing:
                log(f"WARN {missing} 个K线序列超时未就绪, 继续运行")
            log(f"行情就绪: quotes={len(quotes)} klines={len(ready)}/{len(klines)}")

            with self.lock:
                self.connected = True
                self._live = (quotes, klines)
                self._rebuild_init(quotes, klines)
                self._rebuild_update(quotes, klines)
            # 会话就绪/重连后主动补推 init, 覆盖就绪前连入的客户端
            self.push_to_all(self.init_msg)
            log("已向全部在线客户端补推 init")

            # tick 驱动主循环: 每次 wait_update 最多阻塞 0.5s, 期间收到任何 tick 立即返回
            last_rebuild = 0.0
            while True:
                api.wait_update(deadline=time.time() + PUSH_INTERVAL)
                now = time.time()
                if now - last_rebuild >= PUSH_INTERVAL:
                    with self.lock:
                        self._rebuild_update(quotes, klines)
                    last_rebuild = now
        finally:
            with contextlib.suppress(Exception):
                api.close()
            log("TqApi 已关闭")

    # ---- 供 WebSocket 线程调用 ----
    def get_init(self) -> str:
        """init 只在会话就绪时构建一次会过期(价格旧 + K线缺当日柱), 连入时按需重建."""
        with self.lock:
            if self._live is not None and (self.connected is False or time.time() - self.init_built_at > 3.0):
                quotes, klines = self._live
                with contextlib.suppress(Exception):
                    self._rebuild_init(quotes, klines)
            return self.init_msg

    def get_update(self) -> str:
        with self.lock:
            return self.update_msg


class WsServer:
    def __init__(self, hub: MarketHub):
        self.hub = hub
        self.clients = set()
        self.loop = None

    def _push_from_thread(self, msg: str):
        if self.loop and self.clients:
            asyncio.run_coroutine_threadsafe(broadcast(self.clients, msg), self.loop)

    async def handler(self, ws):
        self.clients.add(ws)
        log(f"WS 客户端接入 ({len(self.clients)})")
        try:
            await ws.send(self.hub.get_init())
            async for _ in ws:
                pass  # 客户端不发数据, 保活即可
        except Exception:
            pass
        finally:
            self.clients.discard(ws)
            log(f"WS 客户端断开 ({len(self.clients)})")

    async def push_loop(self):
        while True:
            await asyncio.sleep(PUSH_INTERVAL)
            if not self.clients:
                continue
            with contextlib.suppress(Exception):
                await broadcast(self.clients, self.hub.get_update())

    def run(self):
        async def main():
            self.loop = asyncio.get_running_loop()
            self.hub.attach_push(self._push_from_thread)
            async with serve(self.handler, "0.0.0.0", WS_PORT):
                log(f"WebSocket 服务: ws://localhost:{WS_PORT}")
                await self.push_loop()
        asyncio.run(main())


class StaticServer:
    def run(self):
        static_dir = str(BASE / "static")

        class Handler(http.server.SimpleHTTPRequestHandler):
            def __init__(self, *a, **kw):
                super().__init__(*a, directory=static_dir, **kw)

            def log_message(self, *a):
                pass

        class ReusableServer(socketserver.ThreadingTCPServer):
            allow_reuse_address = True  # 避免 TIME_WAIT 导致 bind 失败

        for attempt in range(5):
            try:
                with ReusableServer(("0.0.0.0", HTTP_PORT), Handler) as httpd:
                    log(f"HTTP 页面: http://localhost:{HTTP_PORT}")
                    httpd.serve_forever()
            except OSError as exc:
                log(f"WARN HTTP 端口绑定失败({attempt+1}/5): {exc}, 3s 后重试")
                time.sleep(3)


if __name__ == "__main__":
    hub = MarketHub(SYMBOLS)
    hub.sina.start()          # 外盘源独立于天勤会话, 断线也不影响美豆/布伦特
    hub.spot.start()          # 现货源同理: 生意社抓取失败不影响行情推送
    hub.sino.start()          # 收储源同理: 中储粮抓取失败不影响行情推送
    hub.lz.start()            # 产业源同理: 隆众 PE/PP 库存与开工率, 30 分钟一轮
    hub.zc.start()            # 卓创: 淘鸡出栏量周数据, 服务鸡蛋 jd2
    hub.ms.start()            # 我的农产品: 豆粕库存周数据, 服务豆粕 m1

    def _backup_loop():
        # SQLite 定时备份: 15:05/23:05 收盘定格后各一次, 保留 60 天 (见 archivedb.py)
        try:
            import archivedb
            archivedb.auto_backup_loop()
        except Exception as exc:
            log(f"db 备份线程异常退出: {exc}")

    threading.Thread(target=_backup_loop, name="db-backup", daemon=True).start()
    ws = WsServer(hub)
    threading.Thread(target=ws.run, name="ws", daemon=True).start()
    threading.Thread(target=StaticServer().run, name="http", daemon=True).start()
    log(f"盯盘工作台启动: {len(SYMBOLS)} 个合约, 推送间隔 {PUSH_INTERVAL}s")
    try:
        hub.run_forever()  # 主线程: TqSdk 会话 + 重连
    except KeyboardInterrupt:
        log("手动退出")
        sys.exit(0)
