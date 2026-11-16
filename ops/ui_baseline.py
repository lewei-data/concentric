#!/usr/bin/env python3
"""P2-3 前端结构性基线 + 截图基线（双模式）

为什么先做结构而不是截图：结构基线不依赖浏览器、秒级出结果，适合每次上线跑；
截图基线需要浏览器（用 hermes 自带的 chromium-1208，无需另装）作为补充。
复盘的原则是「整改必须落在代码与配置里、可复现、可自检」。结构基线抓住
复盘 P2-3 点名的四类回归：
    1. 操作按钮位置   —— 弹层里是否又出现「按钮压在底部」（*-modal-foot 反模式）
    2. 空态           —— 列表页是否丢了「暂无数据」分支
    3. 长文本截断     —— 列表项是否还有 ellipsis / line-clamp
    4. 目录/文件区分  —— 素材/作品页是否还有「目录」标签

用法：
    python3 ops/ui_baseline.py --update   # 建立/更新基线（结构性变更被认可时）
    python3 ops/ui_baseline.py            # 与基线比对，报告新增/消失的维度
    python3 ops/ui_baseline.py --shots    # 视觉截图基线（hermes chromium，零安装）
退出码：0 无差异；1 有差异（差异不一定是错，但必须有人看一眼）。
"""
import glob
import json
import os
import subprocess
import sys

REPO = "/opt/sefs/opc-workbench"
SRC = os.path.join(REPO, "web", "src")
BASE_FILE = os.path.join(REPO, "ops", "ui_baseline.json")
SHOT_DIR = os.path.join(REPO, "ops", "ui_shots")

# 截图用浏览器：优先 UI_CHROME，其次 hermes 自带的 chromium（tools/chromium-1208），
# 最后才是系统 chromium。hermes 那份是现成的，不用再下 CDN。
CHROME_CANDIDATES = (
    os.environ.get("UI_CHROME", ""),
    "/opt/sefs/hermes/tools/chromium-1208/chrome-linux64/chrome",
    "/usr/bin/chromium",
    "/usr/bin/chromium-browser",
    "/usr/bin/google-chrome",
)


def _find_chrome():
    for p in CHROME_CANDIDATES:
        if p and os.path.isfile(p) and os.access(p, os.X_OK):
            return p
    return ""

# 维度 -> 关键字（任一命中即视为该维度存在）
DIMS = {
    "empty": ("暂无", "还没有", "没有数据", "empty-state", "emptystate"),
    "truncate": ("ellipsis", "line-clamp", "text-overflow"),
    "modal_foot": ("modal-foot", "dialog-foot", "dialog-actions", "modal-actions"),
    "dir_label": ("目录",),
}

# modal_foot=True 属「疑似反模式」（复盘 #2 就是按钮压底部），只提示不判错
WARN_DIMS = {"modal_foot"}


def scan_file(path):
    try:
        text = open(path, encoding="utf-8").read()
    except Exception:
        return None
    low = text.lower()
    row = {k: any(kw.lower() in low for kw in kws) for k, kws in DIMS.items()}
    row["lines"] = len(text.splitlines())
    return row


def collect():
    files = sorted(glob.glob(os.path.join(SRC, "pages", "*.tsx")) +
                   glob.glob(os.path.join(SRC, "components", "*.tsx")))
    out = {}
    for f in files:
        name = os.path.relpath(f, SRC)
        row = scan_file(f)
        if row:
            out[name] = row
    return out


def diff(base, cur):
    changes = []
    for name, row in cur.items():
        old = base.get(name)
        if old is None:
            changes.append((name, "新增文件", "", ""))
            continue
        for dim in DIMS:
            if old.get(dim) != row.get(dim):
                tag = "⚠ 疑似回归" if (dim in WARN_DIMS and row.get(dim)) else "变化"
                changes.append((name, dim, old.get(dim), row.get(dim), tag))
    for name in base:
        if name not in cur:
            changes.append((name, "文件消失", "", ""))
    return changes


def _boss_credentials():
    """从凭据表取老板账号（角色列含「老板」）。只读，不落盘、不打印。"""
    path = "/opt/sefs/obsidian/公司/登录凭据.md"
    try:
        for line in open(path, encoding="utf-8"):
            line = line.strip()
            if not line.startswith("|") or "用户名" in line or set(line) <= set("|-: "):
                continue
            cells = [c.strip() for c in line.strip("|").split("|")]
            if len(cells) >= 6 and "老板" in cells[3]:
                return cells[1], cells[5]
    except OSError:
        pass
    return "", ""


def _shots_login(base_url):
    """登录后截关键页。需要 playwright + hermes 自带 chromium（executable_path 复用，不重装浏览器）。"""
    chrome = _find_chrome()
    if not chrome:
        print("找不到浏览器，设 UI_CHROME 后重试")
        return 2
    user, pwd = _boss_credentials()
    if not user or not pwd:
        print("凭据表里没找到老板账号，跳过")
        return 2
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("当前 python 没装 playwright，用 /opt/sefs/venvs/playwright/bin/python 跑本脚本")
        return 2

    pages = [("app-home", "/app/"), ("app-works", "/app/works"),
             ("app-materials", "/app/materials"), ("app-factory", "/app/factory"),
             ("app-hotspots", "/app/hotspots"), ("app-knowledge", "/app/knowledge"),
             ("app-models", "/app/models"), ("app-ops", "/app/ops")]
    os.makedirs(SHOT_DIR, exist_ok=True)
    ok = 0
    with sync_playwright() as pw:
        b = pw.chromium.launch(executable_path=chrome, args=["--no-sandbox", "--disable-gpu"])
        pg = b.new_page(viewport={"width": 1440, "height": 900})
        pg.goto(base_url + "/app/login", wait_until="networkidle", timeout=30000)
        pg.fill("input[autocomplete=username]", user)
        pg.fill("input[type=password]", pwd)
        pg.click("button[type=submit]")
        try:
            pg.wait_for_selector(".login-wrap", state="detached", timeout=20000)
        except Exception:
            print("  登录未跳转（可能被拒），导出当前页排查")
            pg.screenshot(path=os.path.join(SHOT_DIR, "login-failed.png"))
            b.close()
            return 1
        for name, path in pages:
            try:
                pg.goto(base_url + path, wait_until="networkidle", timeout=30000)
                pg.wait_for_timeout(1200)
                out = os.path.join(SHOT_DIR, name + ".png")
                pg.screenshot(path=out)
                size = os.path.getsize(out) if os.path.exists(out) else 0
                print(f"  {name:<14} {size // 1024:>4}KB  {path}")
                ok += 1
            except Exception as e:
                print(f"  {name:<14} 失败：{type(e).__name__}: {str(e)[:60]}")
        b.close()
    print(f"\n登录后截图 {ok}/{len(pages)} 张 -> {os.path.relpath(SHOT_DIR, REPO)}")
    return 0 if ok == len(pages) else 1


def main():
    cur = collect()
    if "--update" in sys.argv:
        json.dump(cur, open(BASE_FILE, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
        print(f"基线已更新：{len(cur)} 个文件 -> {os.path.relpath(BASE_FILE, REPO)}")
        return
    if "--shots-login" in sys.argv:
        sys.exit(_shots_login(os.environ.get("UI_BASE", "http://127.0.0.1:8848")))
    if "--shots" in sys.argv:
        chrome = _find_chrome()
        if not chrome:
            print("找不到可用浏览器。设 UI_CHROME=<chrome 路径>，或确认 "
                  "/opt/sefs/hermes/tools/chromium-1208/chrome-linux64/chrome 存在。")
            sys.exit(2)
        os.makedirs(SHOT_DIR, exist_ok=True)
        base_url = os.environ.get("UI_BASE", "http://127.0.0.1:8848")
        pages = [("login", base_url + "/login"), ("root", base_url + "/")]
        ok = 0
        for name, url in pages:
            out = os.path.join(SHOT_DIR, name + ".png")
            cmd = [chrome, "--headless=new", "--disable-gpu", "--no-sandbox",
                   "--hide-scrollbars", "--window-size=1440,900",
                   "--virtual-time-budget=5000", f"--screenshot={out}", url]
            try:
                r = subprocess.run(cmd, capture_output=True, timeout=120)
                size = os.path.getsize(out) if os.path.exists(out) else 0
                if size > 2000:
                    print(f"  {name:<6} -> {os.path.relpath(out, REPO)}  {size // 1024}KB")
                    ok += 1
                else:
                    print(f"  {name:<6} 失败：{(r.stderr.decode(errors='replace') or 'no output')[-160:]}")
            except Exception as e:
                print(f"  {name:<6} 异常：{type(e).__name__}: {e}")
        print(f"\n截图 {ok}/{len(pages)} 张，浏览器：{chrome}")
        print("说明：这两张是未登录态（login / 重定向后的首页）。登录后页面需要会话 cookie，"
              "结构基线不受影响。")
        sys.exit(0 if ok else 1)
    if not os.path.exists(BASE_FILE):
        print(f"基线不存在，先执行：python3 ops/ui_baseline.py --update")
        sys.exit(2)
    base = json.load(open(BASE_FILE, encoding="utf-8"))
    changes = diff(base, cur)
    warn = sum(1 for c in changes if len(c) > 4 and c[4].startswith("⚠"))
    if not changes:
        print(f"结构基线与现状一致（{len(cur)} 个文件，未发现维度变化）")
        sys.exit(0)
    print(f"{'文件':<34}{'维度':<14}{'基线':<8}{'现状':<8}标记")
    print("-" * 74)
    for c in changes:
        name, dim = c[0], c[1]
        old = c[2] if len(c) > 2 else ""
        new = c[3] if len(c) > 3 else ""
        tag = c[4] if len(c) > 4 else ""
        print(f"{name[:32]:<34}{dim:<14}{str(old):<8}{str(new):<8}{tag}")
    if warn:
        print(f"\n⚠ {warn} 项疑似回归（按钮位置/交互结构变化），请人工确认后再 --update")
    if "--notify" in sys.argv:
        try:
            sys.path.insert(0, os.path.join(REPO, "prototype"))
            import notify as _n
            detail = "; ".join(f"{c[0]}:{c[1]}" for c in changes[:6])
            _n.notify("warning", "ui-baseline",
                      f"前端结构基线有 {len(changes)} 处变化：{detail}")
        except Exception as e:
            print(f"通知发送失败：{type(e).__name__}: {e}")
    sys.exit(1)


if __name__ == "__main__":
    main()
