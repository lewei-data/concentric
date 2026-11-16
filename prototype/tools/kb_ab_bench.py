#!/usr/bin/env python3
"""知识图谱注入 A/B 基准（交叉顺序，避免被顺序效应和方差骗）。

用法：
    cd /opt/sefs/opc-workbench/prototype
    python3 tools/kb_ab_bench.py                      # 全部 5 个任务
    python3 tools/kb_ab_bench.py --only cco,ceo       # 指定角色
    python3 tools/kb_ab_bench.py --rounds 2           # 每轮次数（默认 2 次/组）

设计要点（2026-11-16 实测结论，改之前先读）：
- 每组跑 2 次并按 A,B,B,A 交叉：抵消"越跑越省"（缓存/暖机）造成的伪改善。
- A = kb=0（不注入，agent 自己去 vault 翻）；B = kb=None（注入图谱子图）。
- 判定只看区间是否重叠：同一 cto 任务两次独立测量曾出现 +73% / -63%，单轮 4 次不足以定论。
- 指标：hermes --usage-file 的 totalTokens/apiCalls + 端到端耗时 + 库内独有事实命中数。
- 隔离：默认 OPC_USERS_DB=/tmp/opc_ab_users.db，不污染生产会话。

基线（2026-11-16，32 次调用）：
    cco 选型 -72% token / ceo 五行 -74% / kb_manager 盘点 -35%
    cto 卡死处置 -8%（库内无 SOP，不显著）/ pm_gou 零命中 +2%（api 一致）
"""
import argparse
import json
import os
import statistics
import sys
import time
from concurrent.futures import ThreadPoolExecutor

HERE = os.path.dirname(os.path.abspath(__file__))
PROTO = os.path.dirname(HERE)
os.environ.setdefault("OPC_USERS_DB", "/tmp/opc_ab_users.db")
sys.path.insert(0, PROTO)

from kernel import Kernel  # noqa: E402

# (角色, 任务, 库内独有事实关键词 —— 出现即算命中)
CASES = [
    ("cco", "给编剧组定 ComfyUI 出图模型的选用标准：什么场景用哪个模型、为什么。"
            "evidence 里必须写具体的模型名与实例地址。",
     ["21.157", "MiniMax", "SDXL", "Flux", "H3"]),
    ("ceo", "五行学习这套机制到底怎么落地？金木水火土各自要产出什么文件？"
            "evidence 写具体文件名或动作。",
     ["审计", "技能缺口", "对标", "趋势", "归档"]),
    ("kb_manager", "知识库现在有多少条、哪些是垃圾需要清理？clean 的标准是什么。"
                   "evidence 写具体数字或文件名。",
     ["61", "67", "13", "复核"]),
    ("cto", "21.157 那台 ComfyUI 实例之前卡死是怎么定位和恢复的？下次怎么快速判断。"
            "evidence 写具体命令或端口。",
     ["21.157", "8188", "实例", "重启"]),
    ("pm_gou", "为一句 10 字以内的降温产品 slogan 出 2 个备选。", []),   # 零命中对照
]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", default="", help="逗号分隔的角色 id，如 cco,ceo")
    ap.add_argument("--rounds", type=int, default=2, help="每组次数（A,B,B,A 为一轮，默认 1 轮=2 次/组）")
    ap.add_argument("--out", default="/tmp/kb_ab_result.json")
    args = ap.parse_args()

    only = {s.strip() for s in args.only.split(",") if s.strip()}
    cases = [c for c in CASES if not only or c[0] in only]
    if not cases:
        print("没有匹配的任务；可选：", ", ".join(c[0] for c in CASES))
        return 1

    K = Kernel()
    # 注入开销：与任务无关的固定成本
    ts, blen = [], 0
    for _ in range(5):
        t = time.time()
        blen = len(K.recall_kb(cases[0][1][:160], limit=5))
        ts.append((time.time() - t) * 1000)
    print(f"[注入开销] 召回 {statistics.mean(ts):.1f}ms，注入块 {blen} 字符 ≈ {round(blen / 1.6)} token\n", flush=True)

    def one(aid, task, inject):
        t0 = time.time()
        data, meta = K.call_agent(aid, task, "", "", None, kb=(None if inject else 0))
        usage = (meta or {}).get("usage") or {}
        ev = ""
        if isinstance(data, dict):
            ev = " ".join(str(x) for x in (data.get("evidence") or [])) or json.dumps(data, ensure_ascii=False)
        return {"aid": aid, "inject": inject, "latency": int((time.time() - t0) * 1000),
                "tokens": usage.get("totalTokens", 0), "apiCalls": usage.get("apiCalls", 0),
                "evidence": ev[:300], "ok": bool(data)}

    def job(case):
        aid, task, kws = case
        order = []
        for _ in range(max(1, args.rounds)):
            order += [False, True, True, False]      # A,B,B,A
        out = []
        for inject in order:
            r = one(aid, task, inject)
            r["hit"] = sum(1 for k in kws if k in r["evidence"]) if kws else None
            out.append(r)
            print(f"  {aid:10s} {'图谱' if inject else '无  '} tokens={r['tokens']:7d} "
                  f"api={r['apiCalls']:2d} {r['latency'] / 1000:6.1f}s hit={r['hit']}", flush=True)
        return out

    rows = []
    with ThreadPoolExecutor(max_workers=len(cases)) as ex:
        for res in ex.map(job, cases):
            rows.extend(res)

    print(f"\n{'角色':11s}{'组':5s}{'tokens 均值[区间]':>26s}{'api':>6s}{'耗时s':>8s}{'命中':>6s}")
    summary = []
    for aid, _task, _kws in cases:
        grp = {}
        for r in rows:
            if r["aid"] == aid:
                grp.setdefault(bool(r["inject"]), []).append(r)
        line = {"aid": aid}
        for tag, key in (("无", False), ("图谱", True)):
            rs = grp.get(key) or []
            if not rs:
                continue
            tk = [r["tokens"] for r in rs]
            api = [r["apiCalls"] for r in rs]
            lat = [r["latency"] for r in rs]
            hit = [r["hit"] for r in rs if r["hit"] is not None]
            print(f"{aid:11s}{tag:5s}{statistics.mean(tk):10.0f}[{min(tk)}~{max(tk)}]".ljust(37)
                  + f"{statistics.mean(api):6.1f}{statistics.mean(lat) / 1000:8.1f}"
                  + (f"{statistics.mean(hit):6.1f}" if hit else "     -"))
            line[tag] = {"tokens": statistics.mean(tk), "tmin": min(tk), "tmax": max(tk),
                         "api": statistics.mean(api), "lat": statistics.mean(lat),
                         "hit": statistics.mean(hit) if hit else None}
        if "无" in line and "图谱" in line:
            a, b = line["无"], line["图谱"]
            sig = "显著" if b["tmax"] < a["tmin"] or a["tmax"] < b["tmin"] else "不显著(区间重叠)"
            dt = (b["tokens"] - a["tokens"]) / a["tokens"] * 100 if a["tokens"] else 0
            print(f"{'':11s}{'Δ':5s}tokens {dt:+.0f}%   api {b['api'] - a['api']:+.1f}   "
                  f"耗时 {(b['lat'] - a['lat']) / 1000:+.1f}s   {sig}\n")
            line["sig"], line["delta"] = sig, round(dt, 1)
        summary.append(line)

    json.dump({"rows": rows, "summary": summary}, open(args.out, "w"), ensure_ascii=False, indent=1)
    print(f"原始数据：{args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())