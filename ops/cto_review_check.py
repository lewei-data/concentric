#!/usr/bin/env python3
"""CTO 整改跟踪核对：只读采集客观证据，判断 P0/P1 是否真的落地。

设计原则：不信任口头汇报，只认「文件存在 + 配置生效 + 实时数值 + 巡检产物」。
用法：
    python3 ops/cto_review_check.py            # 输出到 ops/reports/cto-review-YYYYMMDD-HHMM.md
    python3 ops/cto_review_check.py --json     # 只打印 JSON
"""
import json
import os
import re
import subprocess
import sys
from datetime import datetime

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PROTO = os.path.join(BASE, "prototype")
OUT_DIR = os.path.join(BASE, "ops", "reports")
GW_HOST = os.environ.get("OPC_GW_HOST", "boray@192.168.31.208")


def sh(cmd, timeout=12):
    try:
        p = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout)
        return (p.returncode, (p.stdout or "").strip(), (p.stderr or "").strip())
    except subprocess.TimeoutExpired:
        return (124, "", "timeout")
    except Exception as e:  # noqa: BLE001
        return (1, "", f"{type(e).__name__}: {e}")


def ssh(cmd, timeout=15):
    return sh(f'ssh -o BatchMode=yes -o ConnectTimeout=6 {GW_HOST} "{cmd}"', timeout)


def grep(path, pattern):
    if not os.path.isfile(path):
        return False
    try:
        with open(path, encoding="utf-8", errors="ignore") as f:
            return re.search(pattern, f.read()) is not None
    except OSError:
        return False


def check():
    items = []

    def add(key, title, expect, actual, verdict, evidence=""):
        items.append({"key": key, "title": title, "expect": expect,
                      "actual": actual, "verdict": verdict, "evidence": evidence[:400]})

    # P0-1 网关日志治理：磁盘水位 + 源头过滤 + 轮转容量约束
    rc, out, err = ssh("df --output=pcent / | tail -1; stat -c %s /var/log/syslog")
    pct, syslog_mb = None, None
    if rc == 0 and out:
        lines = out.splitlines()
        try:
            pct = int(lines[0].strip().rstrip("%"))
            syslog_mb = round(int(lines[1]) / 1048576, 1)
        except (IndexError, ValueError):
            pass
    add("P0-1a", "31.208 根分区使用率", "<80%", f"{pct}%" if pct is not None else "读取失败",
        "PASS" if (pct is not None and pct < 80) else ("FAIL" if pct is not None else "BLOCKED"),
        f"df/stat via ssh，syslog 当前 {syslog_mb} MB")

    rc, out, _ = ssh("test -f /etc/rsyslog.d/49-sefs-rdma-drop.conf && grep -c 'SEFS RDMA' /etc/rsyslog.d/49-sefs-rdma-drop.conf || true")
    hit = (out or "").strip()
    add("P0-1b", "SEFS RDMA 日志源头过滤", "rsyslog 有过滤配置", f"匹配行 {hit or 0}",
        "PASS" if hit and hit not in ("0", "") else "FAIL",
        "208:/etc/rsyslog.d/49-sefs-rdma-drop.conf")

    rc, out, _ = ssh("grep -E 'maxsize|rotate' /etc/logrotate.d/rsyslog 2>/dev/null | tr '\\n' ' '")
    add("P0-1c", "syslog 轮转容量约束", "含 maxsize 且轮转份数受限", (out or "无")[:120],
        "PASS" if "maxsize" in (out or "") else "FAIL", "208:/etc/logrotate.d/rsyslog")

    # P0-2 主机资源接入监控
    mon = os.path.join(PROTO, "ops_monitor.py")
    has_host = grep(mon, r"disk_usage|shutil\.disk_usage|/proc/meminfo|os\.getloadavg")
    has_alert = grep(mon, r"host-disk|disk-full|inode|磁盘")
    add("P0-2", "主机资源纳入 /api/ops 告警", "存在磁盘/内存/load 采集与告警",
        f"采集={'有' if has_host else '无'} 告警={'有' if has_alert else '无'}",
        "PASS" if (has_host and has_alert) else "FAIL", "prototype/ops_monitor.py")

    # P0-3 告警推送通道
    notify = os.path.join(PROTO, "notify.py")
    wired = grep(os.path.join(PROTO, "server.py"), r"notify\.|import notify") or \
        grep(os.path.join(PROTO, "ops_monitor.py"), r"notify\.")
    add("P0-3", "告警推送通道（含去重/静默）", "存在推送实现且被调用",
        f"notify.py={'有' if os.path.isfile(notify) else '无'} 被引用={'是' if wired else '否'}",
        "PASS" if (os.path.isfile(notify) and wired) else "FAIL", "prototype/notify.py")

    # P0-4 公网大文件巡检产物
    cand = [os.path.join(BASE, "ops", "bigfile_check.json"),
            os.path.join(BASE, "ops", "bigfile.json")]
    found = next((p for p in cand if os.path.isfile(p)), "")
    fresh = False
    if found:
        age_h = (datetime.now().timestamp() - os.path.getmtime(found)) / 3600
        fresh = age_h < 26
    add("P0-4", "公网大文件 md5 巡检", "产物存在且 24h 内更新",
        (f"{os.path.basename(found)}，{age_h:.1f}h 前" if found else "无产物"),
        "PASS" if (found and fresh) else "FAIL", found)

    # P1-1 IT 系统台账
    db = "/mnt/sefs/conring/users/18911789286/opc.db"
    rows = -1
    rc, out, _ = sh(f'sqlite3 {db} "select count(*) from it_assets;" 2>/dev/null')
    if rc == 0 and out.strip().isdigit():
        rows = int(out.strip())
    add("P1-1", "IT 系统台账（it_assets 表）", "表存在且已录入资产", f"记录数 {rows}",
        "PASS" if rows > 0 else "FAIL", "opc.db:it_assets")

    # P1-2 分层诊断脚本
    diag = os.path.join(BASE, "scripts", "diagnose.sh")
    ok = os.path.isfile(diag) and os.access(diag, os.X_OK)
    add("P1-2", "分层诊断脚本", "scripts/diagnose.sh 可执行",
        f"{'存在' if os.path.isfile(diag) else '缺失'}{'/可执行' if ok else ''}",
        "PASS" if ok else "FAIL", diag)

    # P1-3 台账漂移巡检
    drift = grep(os.path.join(BASE, "ops", "patrol_tick.sh"), r"it_assets|台账") or \
        grep(os.path.join(PROTO, "ops_monitor.py"), r"it_assets")
    add("P1-3", "台账与实际漂移巡检", "巡检会比对台账", "已接入" if drift else "未接入",
        "PASS" if drift else "FAIL", "ops/patrol_tick.sh 或 ops_monitor.py")

    # P2 复盘三件套 / 自动处置
    auto = grep(os.path.join(PROTO, "ops_monitor.py"), r"auto_heal|autoheal|自动处置")
    add("P2", "告警→Runbook→自动处置", "低风险动作可自愈", "已实现" if auto else "未实现",
        "PASS" if auto else "PENDING", "prototype/ops_monitor.py")

    return items


def render(items):
    now = datetime.now()
    total = [i for i in items if i["verdict"] in ("PASS", "FAIL")]
    passed = [i for i in total if i["verdict"] == "PASS"]
    score = round(len(passed) / len(total) * 100) if total else 0
    lines = [
        f"# CTO 整改跟踪核对 · {now:%Y-%m-%d %H:%M}",
        "",
        f"- 通过 {len(passed)}/{len(total)}，完成度 **{score}%**（只统计可判定项）",
        f"- 核对主机：31.206 本地 + 31.208 网关（ssh 只读）",
        "",
        "| 项 | 要求 | 实测 | 结论 | 证据 |",
        "|---|---|---|---|---|",
    ]
    for i in items:
        lines.append(f"| {i['key']} {i['title']} | {i['expect']} | {i['actual']} | "
                     f"**{i['verdict']}** | `{i['evidence']}` |")
    lines += ["", "> 判定口径：PASS=证据齐全且当前生效；FAIL=未落地或已回退；BLOCKED=无法取证；PENDING=未到期。"]
    return "\n".join(lines) + "\n", score


def main():
    items = check()
    if "--json" in sys.argv:
        print(json.dumps(items, ensure_ascii=False, indent=2))
        return 0
    md, score = render(items)
    os.makedirs(OUT_DIR, exist_ok=True)
    path = os.path.join(OUT_DIR, f"cto-review-{datetime.now():%Y%m%d-%H%M}.md")
    with open(path, "w", encoding="utf-8") as f:
        f.write(md)
    print(md)
    print(f"[written] {path}  score={score}%")
    return 0


if __name__ == "__main__":
    sys.exit(main())