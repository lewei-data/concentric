#!/usr/bin/env python3
"""server.py 拆分脚本 B：抽取基础设施单例 → deps.py；内核块 → kernel.py。

1. deps.py：db/ingest/deliverables/works/om_render/asset_fill/insights/ops_monitor
   的 try-import 降级单例（server 与 kernel 共同引用）；
2. kernel.py：BudgetAbort/BusyError/_PIPELINE_STOP/PipelineContext/Kernel/
   KERNELS/_kernel_for/_kernel_reset（不反向依赖 server）；
3. server.py：删除被搬段，改为 from deps import * 与 from kernel import ...；
4. 生成后做 AST 未定义名检查。
"""
import ast
import os
import re
import builtins

BASE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(BASE, "server.py")
lines = open(SRC, encoding="utf-8").read().splitlines(keepends=True)
src = "".join(lines)
tree = ast.parse(src)

nodes = {}
for node in tree.body:
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        start = node.lineno
        while start - 2 >= 0 and lines[start - 2].lstrip().startswith("#"):
            start -= 1
        nodes[node.name] = (start, node.end_lineno)
    elif isinstance(node, ast.Assign):
        for t in node.targets:
            if isinstance(t, ast.Name):
                nodes.setdefault(t.id, (node.lineno, node.end_lineno))

def cut(names):
    spans = sorted(nodes[n][:2] for n in names if n in nodes)
    missing = [n for n in names if n not in nodes]
    if missing:
        raise SystemExit(f"未找到符号: {missing}")
    merged = []
    for s, e in spans:
        if merged and s <= merged[-1][1] + 1:
            merged[-1] = (merged[-1][0], max(merged[-1][1], e))
        else:
            merged.append((s, e))
    out, removed = [], set()
    for s, e in merged:
        out.append("".join(lines[s - 1:e]))
        removed.update(range(s, e + 1))
    return "\n".join(out), removed

# ---------- 1. deps.py（基础设施单例） ----------
DEPS_CODE = '''"""基础设施单例：过程库/摄取/交付物/作品档案/渲染/补素材/聚合面板。

按环境整体降级（缺依赖时为 None，不影响主流程）；server 与 kernel 共同引用。
"""
import os

try:
    import db as DBX
    import ingest as ING
except Exception as _db_err:
    DBX = None
    ING = None
    print(f"[boot] 过程数据入库不可用：{type(_db_err).__name__}: {_db_err}", flush=True)
DB_ON = bool(DBX and DBX.ENABLED)
INGEST_ON = DB_ON and os.environ.get("OPC_INGEST", "0") == "1"

try:
    import deliverables as DL
except Exception as _dl_err:
    DL = None
    print(f"[boot] 交付物落盘不可用：{type(_dl_err).__name__}: {_dl_err}", flush=True)

try:
    import works as WK
except Exception as _wk_err:
    WK = None
    print(f"[boot] 作品档案不可用：{type(_wk_err).__name__}: {_wk_err}", flush=True)

try:
    import om_render as OMR
except Exception as _om_err:
    OMR = None
    print(f"[boot] 真实渲染不可用：{type(_om_err).__name__}: {_om_err}", flush=True)

try:
    import asset_fill
except Exception as _af_err:
    asset_fill = None
    print(f"[boot] 补素材模块不可用：{type(_af_err).__name__}: {_af_err}", flush=True)

try:
    import insights
    import ops_monitor
except Exception as _panel_err:
    insights = ops_monitor = None
    print(f"[boot] 灵感/运维聚合不可用：{type(_panel_err).__name__}: {_panel_err}", flush=True)

__all__ = ["DBX", "ING", "DB_ON", "INGEST_ON", "DL", "WK", "OMR", "asset_fill",
           "insights", "ops_monitor"]
'''
open(os.path.join(BASE, "deps.py"), "w", encoding="utf-8").write(DEPS_CODE)

# ---------- 2. kernel.py（内核块） ----------
KERNEL_NAMES = ["BudgetAbort", "BusyError", "_PIPELINE_STOP", "PipelineContext",
                "Kernel", "KERNELS", "KERNELS_LOCK", "_kernel_for", "_kernel_reset"]
code_kernel, rm_kernel = cut(KERNEL_NAMES)

open(os.path.join(BASE, "kernel.py"), "w", encoding="utf-8").write(
    '"""流水线内核：PipelineContext / Kernel（16 阶段编排）/ 多工作区内核注册表。\n'
    '\n'
    '不反向依赖 server：基础设施单例来自 deps，公司配置来自 company_config，\n'
    '模型调用统一走 hermes_adapter（HERMES）；HTTP 路由与会话留在 server.Handler。\n'
    '"""\n'
    "import hashlib\n"
    "import json\n"
    "import os\n"
    "import queue\n"
    "import re\n"
    "import shutil\n"
    "import subprocess\n"
    "import threading\n"
    "import time\n"
    "from datetime import datetime\n"
    "from urllib.parse import parse_qs\n\n"
    "from opc_paths import *  # noqa: F401,F403 —— 路径与运行常量\n"
    "from utils import iso_now, uid, _to_int, _hotkey, _step_view, _iso_shift, _num\n"
    "from agent_defaults import DEFAULT_AGENT_SPEC, AGENTS_REQUIRED\n"
    "from deps import *  # noqa: F401,F403 —— 基础设施单例\n"
    "import company_config as CC\n"
    "import mail as MAILM\n"
    "import pipeline_defs as PIPE\n"
    "from hermes_adapter import HERMES\n\n\n" + code_kernel)

# ---------- 3. 重写 server.py ----------
# 3a. 删除被搬的内核块
keep = [ln for i, ln in enumerate(lines, 1) if i not in rm_kernel]
server = "".join(keep)

# 3b. 基础设施 try-import 段 → from deps import *
server = re.sub(
    r"# 交付物落盘模块.*?(?=# 内容流水线定义)",
    "", server, flags=re.S)
server = re.sub(
    r"# 过程数据落库（SQLite/WAL）.*?INGEST_ON = [^\n]*\n",
    "", server, flags=re.S)

# 3c. "全公司" 映射移入 company_config（PROJECT_BY_NAME 建好后立即补默认编制）
server = re.sub(r"\nCC\.PROJECT_BY_NAME\[\"全公司\"\] = \"all\"\n", "\n", server)

# 3d. 导入头补 deps 与 kernel
server = server.replace(
    "from hermes_adapter import HERMES\n",
    "from hermes_adapter import HERMES\n"
    "from deps import *  # noqa: F401,F403 —— 基础设施单例（DBX/ING/DL/WK/OMR/asset_fill/insights/ops_monitor）\n"
    "from kernel import BusyError, _kernel_for, _kernel_reset\n")

# 3e. company_config 末尾补 "全公司" 映射
cc_path = os.path.join(BASE, "company_config.py")
cc = open(cc_path, encoding="utf-8").read()
cc = cc.replace('PROJECT_BY_NAME = {v["name"]: k for k, v in PROJECTS.items()}\n',
                'PROJECT_BY_NAME = {v["name"]: k for k, v in PROJECTS.items()}\n'
                'PROJECT_BY_NAME["全公司"] = "all"     # 默认编制的 ceo/cco/cto/秘书等挂"全公司"\n')
open(cc_path, "w", encoding="utf-8").write(cc)

open(SRC, "w", encoding="utf-8").write(server)

# ---------- 4. kernel.py 未定义名静态检查 ----------
kcode = open(os.path.join(BASE, "kernel.py"), encoding="utf-8").read()
ktree = ast.parse(kcode)
defined = set(dir(builtins))
for node in ast.walk(ktree):
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        defined.add(node.name)
        for a in node.args.args + node.args.kwonlyargs:
            defined.add(a.arg)
        if node.args.vararg:
            defined.add(node.args.vararg.arg)
        if node.args.kwarg:
            defined.add(node.args.kwarg.arg)
        for sub in ast.walk(node):
            if isinstance(sub, ast.Name) and isinstance(sub.ctx, (ast.Store, ast.Del)):
                defined.add(sub.id)
            elif isinstance(sub, (ast.Import, ast.ImportFrom)):
                for al in sub.names:
                    defined.add((al.asname or al.name).split(".")[0])
            elif isinstance(sub, ast.ExceptHandler) and sub.name:
                defined.add(sub.name)
            elif isinstance(sub, ast.comprehension):
                for t in ast.walk(sub.target):
                    if isinstance(t, ast.Name):
                        defined.add(t.id)
    elif isinstance(node, ast.Import):
        for al in node.names:
            defined.add((al.asname or al.name).split(".")[0])
    elif isinstance(node, ast.ImportFrom):
        for al in node.names:
            defined.add(al.asname or al.name)
    elif isinstance(node, ast.Assign):
        for t in node.targets:
            for sub in ast.walk(t):
                if isinstance(sub, ast.Name):
                    defined.add(sub.id)

loaded = set()
for node in ast.walk(ktree):
    if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
        loaded.add(node.id)
    elif isinstance(node, ast.Attribute):
        v = node.value
        while isinstance(v, ast.Attribute):
            v = v.value
        if isinstance(v, ast.Name) and isinstance(v.ctx, ast.Load):
            loaded.add(v.id)

undef = sorted(loaded - defined)
# self.xxx 与局部噪音已在 Store 收集；剩余即疑似漏导入
print("kernel.py 疑似未定义名:", undef if undef else "无")
