#!/usr/bin/env python3
"""方向打分对拍：后端 dirdir.py vs 前端 dirScore()。

为什么需要：后端是唯一真相源，前端留了一份兜底副本（离线/旧数据时用）。
两份实现一旦漂移，页面显示「中性 +1」、聊天里算出「偏多 +1.5」，谁都不知道该信谁。
这个脚本用同一批构造数据喂两边，逐字段（s / level / label / terms）比对。

    python3 scripts/dir_parity.py            # 50 组随机 + 边界用例
    python3 scripts/dir_parity.py --cases 200

退出码 0 = 完全一致；1 = 有差异（会打印第一处不一致的明细）。
"""

from __future__ import annotations

import argparse
import json
import random
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "workbench"))

import dirdir  # noqa: E402

QUADS = ["long_attack", "short_cover", "flat", "long_exit", "short_attack"]


def extract_js() -> str:
    html = (ROOT / "workbench/static/index.html").read_text("utf-8")
    m = re.search(r"/\* dir-score:begin.*?/\* dir-score:end \*/", html, re.S)
    if not m:
        raise SystemExit("index.html 里找不到 dir-score:begin/end 标记，无法对拍")
    return m.group(0)


def make_case(rnd: random.Random) -> dict:
    n = rnd.choice([0, 3, 10, 19, 20, 21, 25, 45, 45, 45])
    px = rnd.uniform(2000, 6000)
    rows = []
    for i in range(n):
        px *= 1 + rnd.uniform(-0.02, 0.02)
        hi = px * (1 + rnd.uniform(0, 0.012))
        lo = px * (1 - rnd.uniform(0, 0.012))
        rows.append([f"D{i:03d}", round(px, 2), round(hi, 2), round(lo, 2), round(px, 2), 100 + i])
    last = round((rows[-1][4] if rows else px) * (1 + rnd.uniform(-0.04, 0.04)), 2)
    return {
        "rows": rows,
        "last": last,
        "quad": rnd.choice(QUADS),
        "net_ratio": rnd.choice([0, 0, 4.9, 5.0, 5.1, -4.9, -5.0, -5.1, 12.3, -18.7, 30.0]),
        "flow_bars": rnd.choice([0, 0, 1, 120, 400]),
    }


def make_edge_cases() -> list[dict]:
    # 20 日区间正好卡在 0.4 / 0.6 边界、价格贴区间顶/底
    rows = []
    for i in range(20):
        rows.append([f"D{i:03d}", 3000.0, 3100.0, 2900.0, 3000.0, 100])
    cases = []
    for last in (2900.0, 2900.0 + 200 * 0.39, 2900.0 + 200 * 0.40, 3000.0,
                 2900.0 + 200 * 0.60, 2900.0 + 200 * 0.61, 3100.0):
        for quad in QUADS:
            cases.append({"rows": rows, "last": round(last, 2), "quad": quad,
                          "net_ratio": 6.0, "flow_bars": 200})
    return cases


def run_js(cases: list[dict]) -> list[dict]:
    node = shutil.which("node")
    if not Path(node).exists():
        raise SystemExit("找不到 node，无法对拍（装个 node 或改脚本里的路径）")
    js = (
        "const fs=require('fs');\n"
        + extract_js()
        + "\nconst cases=JSON.parse(fs.readFileSync(process.argv[2],'utf8'));\n"
        "const out=cases.map(c=>dirScore(c.rows,c.last,c.quad,c.net_ratio,c.flow_bars));\n"
        "process.stdout.write(JSON.stringify(out));\n"
    )
    with tempfile.TemporaryDirectory() as td:
        jsf = Path(td) / "parity.js"
        cf = Path(td) / "cases.json"
        jsf.write_text(js, "utf-8")
        cf.write_text(json.dumps(cases, ensure_ascii=False), "utf-8")
        r = subprocess.run([node, str(jsf), str(cf)], capture_output=True, text=True)
        if r.returncode != 0:
            raise SystemExit(f"node 执行失败：{r.stderr[:500]}")
        return json.loads(r.stdout)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cases", type=int, default=50)
    args = ap.parse_args()

    rnd = random.Random(20261021)
    cases = [make_case(rnd) for _ in range(args.cases)] + make_edge_cases()
    js_out = run_js(cases)

    bad = 0
    for i, (c, j) in enumerate(zip(cases, js_out)):
        p = dirdir.score(c["rows"], c["last"], c["quad"], c["net_ratio"], c["flow_bars"])
        diffs = [k for k in ("s", "level", "label", "terms") if p[k] != j[k]]
        if diffs:
            bad += 1
            if bad == 1:
                print(f"[不一致] 用例 #{i}: {c}")
                print(f"  python: {p}")
                print(f"  js    : {j}")
    total = len(cases)
    print(f"对拍 {total} 组，不一致 {bad} 组")
    return 0 if bad == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
