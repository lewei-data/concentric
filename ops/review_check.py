#!/usr/bin/env python3
"""P2-2 复盘三件套校验：缺一项或内容为空即判「未关闭」。

规则（来自复盘 P2-2）：
    每次 P1 事故必须产出「新增检测规则 + 新增巡检用例 + 台账更新」三件套，否则不算关闭。

除了「有没有写」，还查「写的落地位置是否真实存在」——避免为了过检查而填一个不存在的文件。
用法：python3 ops/review_check.py [--json]
退出码：0 全部关闭；1 存在未关闭的复盘（未关闭不是错误，是待办）。
"""
import json
import os
import re
import sys

REVIEW_DIR = "/opt/sefs/obsidian/公司/事故与复盘"
REPO = "/opt/sefs/opc-workbench"
VAULT = "/opt/sefs/obsidian"

SECTIONS = [
    ("detect", "新增检测规则"),
    ("patrol", "新增巡检用例"),
    ("ledger", "台账更新"),
]
PLACEHOLDER = ("待补", "待定", "TODO", "TBD", "xxxx", "占位")
MIN_CHARS = 30

# 三件套里允许出现的落地位置（仓库相对路径或绝对路径）
PATH_RE = re.compile(r"`([\w./-]+\.(?:py|md|db|sh|json|yaml|conf))`")


def section_text(text, keyword):
    """取包含 keyword 的标题到下一个同级或更高级标题之间的正文。"""
    lines = text.splitlines()
    start = None
    level = None
    for i, ln in enumerate(lines):
        if keyword in ln and ln.lstrip().startswith("#"):
            start = i
            level = len(ln) - len(ln.lstrip("#"))
            break
    if start is None:
        return None
    body = []
    for ln in lines[start + 1:]:
        if ln.lstrip().startswith("#"):
            lv = len(ln) - len(ln.lstrip("#"))
            if lv <= level:
                break
        body.append(ln)
    return "\n".join(body).strip()


def check_file(path):
    text = open(path, encoding="utf-8").read()
    res = {"file": os.path.basename(path), "sections": {}, "missing": [], "fake_paths": []}
    for key, kw in SECTIONS:
        body = section_text(text, kw)
        if body is None:
            res["sections"][key] = "缺失"
            res["missing"].append(kw)
            continue
        clean = body.replace("|", "").replace("-", "").strip()
        if len(clean) < MIN_CHARS:
            res["sections"][key] = "内容过少"
            res["missing"].append(f"{kw}(内容过少)")
            continue
        if any(p in body for p in PLACEHOLDER):
            res["sections"][key] = "含占位符"
            res["missing"].append(f"{kw}(含占位符)")
            continue
        for m in PATH_RE.finditer(body):
            rel = m.group(1)
            if not (os.path.exists(os.path.join(REPO, rel)) or os.path.exists(os.path.join(VAULT, rel))
                    or os.path.exists(rel)):
                res["fake_paths"].append(rel)
        res["sections"][key] = "OK"
    res["closed"] = not res["missing"]
    if res["fake_paths"]:
        res["closed"] = False
    return res


def main():
    if not os.path.isdir(REVIEW_DIR):
        print(f"复盘目录不存在：{REVIEW_DIR}")
        sys.exit(2)
    files = sorted(f for f in os.listdir(REVIEW_DIR)
                   if f.endswith(".md") and not f.startswith("_"))
    rows = [check_file(os.path.join(REVIEW_DIR, f)) for f in files]
    if "--json" in sys.argv:
        print(json.dumps(rows, ensure_ascii=False, indent=1))
    else:
        if not rows:
            print("暂无复盘文件")
        else:
            print(f"{'复盘文件':<44}{'检测':<8}{'巡检':<8}{'台账':<8}{'结论'}")
            print("-" * 84)
            for r in rows:
                s = r["sections"]
                print(f"{r['file'][:42]:<44}{s.get('detect',''):<8}{s.get('patrol',''):<8}"
                      f"{s.get('ledger',''):<8}{'已关闭' if r['closed'] else '未关闭'}")
            bad = [r for r in rows if not r["closed"]]
            if bad:
                print("\n未关闭明细：")
                for r in bad:
                    if r["missing"]:
                        print(f"  - {r['file']}：缺 {', '.join(r['missing'])}")
                    if r["fake_paths"]:
                        print(f"  - {r['file']}：引用了不存在的落地位置 {', '.join(r['fake_paths'][:3])}")
    sys.exit(0 if all(r["closed"] for r in rows) else 1)


if __name__ == "__main__":
    main()
