#!/usr/bin/env python3
"""项目自检：一条命令跑完改动后的硬性回归项。

设计原则：
- 只读。不写库、不动 vault、不建内核实例（避免体检本身制造状态）。
- 每项独立判定，失败不中断，最后统一输出 PASS/WARN/FAIL 与退出码。
- 覆盖的是"改动后一定会踩"的六类问题：语法、导入链、服务存活、
  鉴权边界、知识库一致性、后台错误。

用法：
    python3 selfcheck.py            # 人类可读
    python3 selfcheck.py --json     # 给 CI/自动化读
退出码：0 全通过（允许 WARN）；1 存在 FAIL。
"""
import ast
import glob
import json
import os
import subprocess
import sys
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.abspath(__file__))
BASE = os.environ.get("OPC_SELFCHECK_BASE", "http://127.0.0.1:8848")
OWNER_DB = os.environ.get("OPC_OWNER_DB", "/mnt/sefs/conring/users/18911789286/opc.db")
VAULT_KB = os.environ.get("OPC_VAULT_KB", "/opt/sefs/obsidian/OPC/知识库")
SERVICE = os.environ.get("OPC_SERVICE", "opc-workbench.service")
CHECKS = []


def add(name, status, detail=""):
    CHECKS.append({"name": name, "status": status, "detail": detail})


def fail_if(cond, name, detail=""):
    add(name, "FAIL" if cond else "PASS", detail)
    return not cond


def http_code(path):
    try:
        with urllib.request.urlopen(BASE + path, timeout=5) as resp:
            return resp.status
    except urllib.error.HTTPError as e:
        return e.code
    except Exception:
        return 0


def sudo_sql(sql):
    try:
        r = subprocess.run(["sudo", "-n", "sqlite3", OWNER_DB, sql],
                           capture_output=True, text=True, timeout=20)
        return r.stdout.strip() if r.returncode == 0 else ""
    except Exception:
        return ""


def main():
    # 1 语法：全量 py 文件
    bad = []
    for f in glob.glob(os.path.join(ROOT, "*.py")):
        try:
            ast.parse(open(f, encoding="utf-8").read())
        except SyntaxError as e:
            bad.append(f"{os.path.basename(f)}:{e.lineno}:{e.msg}")
    fail_if(bad, "语法", "; ".join(bad[:3]))

    # 2 导入链：关键模块在隔离子进程导入，避免污染体检进程
    code = ("import sys; sys.path.insert(0, %r); "
            "import events, pool, kb_views, factory, approvals, ops, pipeline_kit, pipeline, kernel, db, obs, deliverables, works; print('ok')" % ROOT)
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                       env={**os.environ, "OPC_DB_PATH": "/tmp/selfcheck_import.db"},
                       timeout=180)
    tail = (r.stderr or "").strip().splitlines()[-1] if r.returncode else ""
    fail_if(r.returncode != 0, "关键模块导入", tail)

    # 3 服务存活与鉴权边界
    code_home = http_code("/")
    fail_if(code_home not in (200, 302), "首页可访问", f"实际 {code_home or '无响应'}")
    for path in ("/api/ops", "/api/directives", "/api/stream"):
        c = http_code(path)
        fail_if(c != 401, f"未登录 {path} 返回 401", f"实际 {c or '无响应'}")

    # 4 知识库一致性：只比 kb_ 前缀（rep_ 是交付报告，不进 vault 知识库）
    vault_n = len(glob.glob(os.path.join(VAULT_KB, "**", "*.md"), recursive=True))
    kb_n = sudo_sql("select count(*) from knowledge where id like 'kb_%';")
    ghost = sudo_sql("select count(*) from knowledge where id is null or id='';")
    if kb_n.isdigit() and ghost.isdigit():
        gap = abs(vault_n - int(kb_n))
        ok = gap <= 2 and int(ghost) == 0
        add("知识库一致性", "PASS" if ok else "WARN", f"vault {vault_n} / db kb_ {kb_n} / 幽灵 {ghost}")
    else:
        add("知识库一致性", "WARN", f"vault {vault_n} / db {kb_n or '读取失败'}")

    # 5 后台错误：近 30 分钟 error 级日志（C3 统一格式 [error] where ...）
    try:
        r = subprocess.run(["sudo", "-n", "journalctl", "-u", SERVICE, "--since", "-30 min", "--no-pager"],
                           capture_output=True, text=True, timeout=30)
        lines = [ln.split(": ", 1)[-1] for ln in (r.stdout or "").splitlines() if "[error]" in ln]
    except Exception:
        lines = []
    add("近 30 分钟后台错误", "PASS" if not lines else "WARN",
        "无" if not lines else f"{len(lines)} 条，最近：{lines[-1][:80]}")

    # 6 内核池规模（日志口径，进程内看不到时跳过）
    try:
        r = subprocess.run(["sudo", "-n", "journalctl", "-u", SERVICE, "--since", "-30 min", "--no-pager"],
                           capture_output=True, text=True, timeout=30)
        evict = sum(1 for ln in (r.stdout or "").splitlines() if "[pool] 超上限" in ln)
        add("内核池逐出", "PASS" if evict <= 3 else "WARN", f"近 30 分钟 {evict} 次")
    except Exception:
        add("内核池逐出", "WARN", "无法读取日志")

    fails = [c for c in CHECKS if c["status"] == "FAIL"]
    if "--json" in sys.argv:
        print(json.dumps({"ok": not fails, "checks": CHECKS}, ensure_ascii=False, indent=2))
    else:
        for c in CHECKS:
            print(f"[{c['status']:4s}] {c['name']}" + (f" —— {c['detail']}" if c["detail"] else ""))
        print(f"\n{'全通过' if not fails else f'{len(fails)} 项失败'}"
              f"（{sum(1 for c in CHECKS if c['status'] == 'PASS')} PASS / "
              f"{sum(1 for c in CHECKS if c['status'] == 'WARN')} WARN）")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())