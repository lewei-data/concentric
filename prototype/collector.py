"""自动采集：个人定时任务（素材页「自动采集」标签）。

分工：
- 任务与运行记录存在**用户自己的** opc.db（collect_tasks / collect_runs / collect_ideas）；
- 输出目录固定为 ``users/<手机号>/downloads/<日期-序号>/``，素材页「自动采集」标签直接浏览它；
- 执行交给 hermes 的 cco 角色：先把「采集任务描述」读懂，再按数据源/目标数量抓取开放内容；
- 采完立刻做一轮 CCO 分析（结合知识库）产出灵感与结合点，写进 collect_ideas，
  素材页「创意灵感 → 采集洞察」读这张表，选题会也会优先引用。

降级取向：hermes 不可用 / 调用失败只记错误与说明文件，绝不影响主服务。
"""
import json
import os
import re
import threading
import traceback
from datetime import datetime, timedelta

from opc_paths import *  # noqa: F401,F403 —— USERS_DIR / OWNER_DATA_DIR
from utils import iso_now, uid

try:
    import db as DBX
except Exception:               # pragma: no cover - 库层异常时整体降级
    DBX = None

# 数据源：key 用于前端勾选与提示词，label 用于展示
SOURCES = [
    ("douyin", "抖音短视频"),
    ("xiaohongshu", "小红书图文"),
    ("bilibili", "B 站视频"),
    ("wechat", "公众号文章"),
    ("article", "网页文章 / 新闻"),
    ("own_account", "自己账号的内容"),
]
SOURCE_LABELS = dict(SOURCES)
DEFAULT_SOURCES = ["douyin", "xiaohongshu", "article"]

# 定时周期解析：daily@HH:MM（每天几点）/ every@N（每 N 小时）/ hourly（每小时）
_RE_DAILY = re.compile(r"^daily@(\d{1,2}):(\d{2})$")
_RE_EVERY = re.compile(r"^(?:every|interval)@(\d{1,3})$")

_TICK_SEC = 20.0                # 调度轮询间隔
_RUN_LOCK = threading.Lock()    # 同一时刻只跑一个采集（浏览器自动化很吃资源）
_RUNNING = set()                # 正在跑的任务 id
_STOP = threading.Event()
_THREAD = None


# ---------------------------------------------------------------- 目录与小工具

def downloads_dir(data_dir, create=True):
    """用户的自动采集输出目录：users/<手机号>/downloads。"""
    d = os.path.join(os.path.abspath(data_dir or OWNER_DATA_DIR), "downloads")
    if create:
        try:
            os.makedirs(d, exist_ok=True)
        except OSError as e:
            print(f"[collect] 建目录失败 {d}：{e}", flush=True)
    return d


def _store(data_dir):
    """用户库 Store（与 Kernel 共用实例，路径相同即复用）。"""
    if not DBX:
        return None
    return DBX.store_for(os.path.join(os.path.abspath(data_dir or OWNER_DATA_DIR), "opc.db"))


def _now():
    return datetime.now()


def _iso(dt):
    return dt.isoformat(timespec="seconds")


def _slug(text, limit=24):
    s = re.sub(r"[\\/:*?\"<>|\s]+", "-", str(text or "").strip())
    s = s.strip("-.") or "collect"
    return s[:limit]


def parse_schedule(schedule):
    """返回 (kind, value)；非法值按 daily@09:00 处理。"""
    s = str(schedule or "").strip()
    m = _RE_DAILY.match(s)
    if m:
        return "daily", (max(0, min(23, int(m.group(1)))), max(0, min(59, int(m.group(2)))))
    m = _RE_EVERY.match(s)
    if m:
        return "every", max(1, min(168, int(m.group(1))))
    if s in ("hourly", "hour"):
        return "every", 1
    return "daily", (9, 0)


def next_run_of(schedule, base=None):
    """下一次运行时间（本地时间 ISO 串）。"""
    kind, val = parse_schedule(schedule)
    base = base or _now()
    if kind == "every":
        return _iso(base + timedelta(hours=int(val)))
    hh, mm = val
    nxt = base.replace(hour=hh, minute=mm, second=0, microsecond=0)
    if nxt <= base:
        nxt += timedelta(days=1)
    return _iso(nxt)


def schedule_label(schedule):
    kind, val = parse_schedule(schedule)
    if kind == "every":
        return "每小时" if val == 1 else f"每 {val} 小时"
    return f"每天 {val[0]:02d}:{val[1]:02d}"


# ---------------------------------------------------------------- 任务 CRUD

def _normalize(phone, payload, old=None):
    """表单 → 任务行（带默认值与合法化）。"""
    p = dict(payload or {})
    srcs = p.get("sources")
    if isinstance(srcs, str):
        srcs = [x.strip() for x in srcs.split(",") if x.strip()]
    srcs = [s for s in (srcs or []) if s in SOURCE_LABELS] or list(DEFAULT_SOURCES)
    try:
        target = int(float(str(p.get("targetCount", p.get("target_count", 20))).strip() or 20))
    except (TypeError, ValueError):
        target = 20
    schedule = str(p.get("schedule") or (old or {}).get("schedule") or "daily@09:00").strip()
    row = dict(old or {})
    row.update({
        "id": str(p.get("id") or (old or {}).get("id") or uid("ct")),
        "phone": phone,
        "title": str(p.get("title") or (old or {}).get("title") or "自动采集").strip()[:60],
        "sources": ",".join(srcs),
        "schedule": schedule,
        "target_count": max(1, min(200, target)),
        "brief": str(p.get("brief") if p.get("brief") is not None
                     else (old or {}).get("brief") or "").strip()[:2000],
        "enabled": 1 if str(p.get("enabled", (old or {}).get("enabled", 1))).lower()
                      not in ("0", "false", "no", "off") else 0,
        "status": (old or {}).get("status") or "idle",
        "last_run": (old or {}).get("last_run") or "",
        "last_note": (old or {}).get("last_note") or "",
        "next_run": (old or {}).get("next_run") or "",
        "created_at": (old or {}).get("created_at") or iso_now(),
        "updated_at": iso_now(),
    })
    if not row["next_run"] or (old or {}).get("schedule") != schedule:
        row["next_run"] = next_run_of(schedule) if row["enabled"] else ""
    if not row["enabled"]:
        row["next_run"] = ""
    return row


def list_tasks(data_dir, phone):
    st = _store(data_dir)
    if not st:
        return []
    rows = st.collect_tasks(phone) if phone else []
    for r in rows:
        r["sourcesList"] = [x for x in str(r.get("sources") or "").split(",") if x]
        r["scheduleLabel"] = schedule_label(r.get("schedule"))
    return rows


def save_task(data_dir, phone, payload):
    st = _store(data_dir)
    if not st:
        return {"ok": False, "error": "过程库未启用，无法保存采集任务"}
    tid = str((payload or {}).get("id") or "").strip()
    old = st.collect_task(tid) if tid else None
    if old and str(old.get("phone") or "") != str(phone or ""):
        return {"ok": False, "error": "任务不属于当前账号"}
    row = _normalize(phone, payload, old)
    if not st.collect_task_save(row):
        return {"ok": False, "error": "保存失败，请查看服务日志"}
    saved = st.collect_task(row["id"]) or row
    saved["sourcesList"] = [x for x in str(saved.get("sources") or "").split(",") if x]
    saved["scheduleLabel"] = schedule_label(saved.get("schedule"))
    return {"ok": True, "task": saved}


def delete_task(data_dir, phone, task_id):
    st = _store(data_dir)
    if not st:
        return {"ok": False, "error": "过程库未启用"}
    return {"ok": st.collect_task_delete(task_id, phone)}


def runs(data_dir, phone, task_id=None, limit=15):
    st = _store(data_dir)
    return st.collect_runs(task_id=task_id, phone=phone, limit=limit) if st else []


def ideas(data_dir, phone="", limit=60):
    st = _store(data_dir)
    return st.collect_ideas(phone=phone, limit=limit) if st else []


def priority_ideas(data_dir, limit=8):
    """给出最近一批采集灵感，供选题会/灵感页优先采用。"""
    return ideas(data_dir, phone="", limit=limit)


# ---------------------------------------------------------------- 执行

def _file_count(d):
    n = 0
    for _dp, _dn, fn in os.walk(d):
        n += len([f for f in fn if not f.startswith(".")])
    return n


def _write(path, text):
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
        return True
    except OSError as e:
        print(f"[collect] 写文件失败 {path}：{e}", flush=True)
        return False


def _source_names(task):
    keys = [x for x in str(task.get("sources") or "").split(",") if x]
    return "、".join(SOURCE_LABELS.get(k, k) for k in keys) or "开放网络"


def _collect_prompt(task, out_dir, day):
    brief = str(task.get("brief") or "").strip() or "采集与公司业务相关的高价值内容"
    return (
        f"你是自动采集任务的执行者。今天日期 {day}。\n"
        f"任务名称：{task.get('title') or '自动采集'}\n"
        f"采集任务描述（先理解再动手）：{brief}\n"
        f"数据源：{_source_names(task)}\n"
        f"目标数量：约 {task.get('target_count')} 条\n"
        f"输出目录（必须写在这里）：{out_dir}\n\n"
        "要求：\n"
        "1) 只采集**公开可访问**的内容；自己账号的内容按账号已登录状态读取；不得绕过登录、付费墙或验证码。\n"
        "2) 每条内容在输出目录下留痕：视频/图片尽量把文件抓到本地（文件名用 序号-标题缩写.扩展名），"
        "文章保存正文 Markdown（序号-标题缩写.md）；抓不到正文的只记链接。\n"
        "3) 必须写一份 manifest.json（UTF-8），结构："
        "{\"items\":[{\"title\":\"\",\"url\":\"\",\"source\":\"\",\"file\":\"相对文件名\","
        "\"kind\":\"video|image|article\",\"note\":\"一句话要点\"}],\"note\":\"整体说明\"}。\n"
        "4) 数量不足就据实记录，不要编造链接或文件。\n"
        "5) 完成后只输出一个 JSON：{\"summary\":\"\",\"count\":数字,\"note\":\"\",\"items\":[...]}"
    )


def _analyze_prompt(task, manifest_text, day):
    brief = str(task.get("brief") or "").strip() or "采集与公司业务相关的高价值内容"
    return (
        f"你是 CCO（首席创意官）。今天 {day}，刚完成一轮自动采集，任务描述：{brief}\n"
        f"下面是最新采集清单：\n{manifest_text}\n\n"
        "请结合公司内部知识库（会一并提供），判断这批素材里哪些真正有做内容的价值，"
        "输出可直接进入选题的灵感与结合点：\n"
        "1) 每条灵感要写清「灵感点」（为什么值得做）与「结合点」（和公司知识库/现有项目的哪条认知或产品结合）；\n"
        "2) 不引用榜单排名数字，不编造事实；素材没支撑的点不要写；\n"
        "3) 最多 8 条，按价值从高到低排序。\n"
        "额外 JSON 字段：ideas(数组，每条 {title,summary,detail,tags(逗号分隔),score(0-100),source,link})，"
        "summary、evidence、confidence、tools。"
    )


def _manifest_text(out_dir, collected):
    """把采集结果整理成给 CCO 看的清单文本。"""
    lines = []
    items = []
    if isinstance(collected, dict):
        items = collected.get("items") or []
    for it in items[:60]:
        if not isinstance(it, dict):
            continue
        lines.append(f"- {it.get('title') or '(无标题)'} | {it.get('source') or ''} | "
                     f"{it.get('url') or ''} | {it.get('note') or ''}")
    files = []
    for dp, _dn, fn in os.walk(out_dir):
        for f in sorted(fn)[:80]:
            if f.startswith(".") or f in ("manifest.json", "创意灵感.md", "采集说明.md"):
                continue
            files.append(os.path.relpath(os.path.join(dp, f), out_dir))
    if files:
        lines.append("\n落盘文件：" + "、".join(files[:60]))
    return "\n".join(lines) or "（本轮没有落盘文件，也没解析出条目）"


def _agent_id(kernel):
    """优先 CCO；名册里没有就退小秘书。"""
    try:
        if "cco" in kernel.agents:
            return "cco"
        if "secretary" in kernel.agents:
            return "secretary"
        return next(iter(kernel.agents))
    except Exception:
        return "cco"


def run_task(data_dir, phone, task_id, kernel_getter, manual=False):
    """同步执行一轮采集（调度线程/手动触发都走这里）。返回结果 dict。"""
    st = _store(data_dir)
    if not st:
        return {"ok": False, "error": "过程库未启用"}
    task = st.collect_task(task_id)
    if not task:
        return {"ok": False, "error": "任务不存在"}
    if str(task.get("phone") or "") != str(phone or ""):
        return {"ok": False, "error": "任务不属于当前账号"}

    with _RUN_LOCK:
        if task_id in _RUNNING:
            return {"ok": False, "busy": True, "error": "该任务正在采集"}
        _RUNNING.add(task_id)
    started = iso_now()
    day = _now().strftime("%Y-%m-%d")
    root = downloads_dir(data_dir)
    out_dir = os.path.join(root, f"{day}_{_slug(task.get('title'))}")
    n = 2
    while os.path.isdir(out_dir) and _file_count(out_dir) > 0:
        out_dir = os.path.join(root, f"{day}_{_slug(task.get('title'))}-{n}")
        n += 1
    try:
        os.makedirs(out_dir, exist_ok=True)
    except OSError as e:
        _RUNNING.discard(task_id)
        return {"ok": False, "error": f"建输出目录失败：{e}"}

    st.collect_task_patch(task_id, {"status": "running", "last_run": started, "updated_at": started})
    print(f"[collect] 开始采集 phone={phone} task={task_id} → {out_dir}", flush=True)
    note, ok, file_count, idea_count = "", False, 0, 0
    try:
        kernel = kernel_getter(data_dir) if kernel_getter else None
        if kernel is None:
            raise RuntimeError("工作区内核不可用")
        aid = _agent_id(kernel)

        # ① 采集：让 agent 按任务描述抓内容、落到输出目录
        data, meta = kernel.call_agent(
            aid, _collect_prompt(task, out_dir, day),
            f"输出目录：{out_dir}\n历史偏好：优先近 7 天、与公司项目调性一致的内容。",
            "额外 JSON 字段：count(实际条数)、note(遇到的问题)。",
            "summary, count, note, items")
        collected = data if isinstance(data, dict) else {}
        note = str(collected.get("summary") or collected.get("note") or "").strip()
        _write(os.path.join(out_dir, "manifest.json"),
               json.dumps(collected, ensure_ascii=False, indent=2))
        file_count = _file_count(out_dir)

        # ② 分析：CCO 结合知识库产出灵感/结合点
        ideas_rows = []
        try:
            adata, _meta2 = kernel.call_agent(
                aid, _analyze_prompt(task, _manifest_text(out_dir, collected), day),
                "参考这批素材，给出能直接进入选题的灵感；没有支撑就不要写。",
                "ideas 每条还要带 knowledgeRefs（引用了知识库哪条）。",
                "summary, evidence, confidence, tools, ideas",
                preferred="qwen38-local")
            alist = (adata or {}).get("ideas") if isinstance(adata, dict) else None
            for i, it in enumerate((alist or [])[:8]):
                if not isinstance(it, dict) or not str(it.get("title") or "").strip():
                    continue
                ideas_rows.append({
                    "id": uid("ci"), "phone": phone, "task_id": task_id,
                    "title": str(it.get("title"))[:120],
                    "summary": str(it.get("summary") or "")[:500],
                    "detail": str(it.get("detail") or "")[:2000],
                    "tags": it.get("tags") if isinstance(it.get("tags"), str)
                            else ",".join(it.get("tags") or []),
                    "source": f"自动采集 · {task.get('title') or ''}",
                    "link": str(it.get("link") or ""),
                    "asset": os.path.relpath(out_dir, root),
                    "score": max(0.0, min(100.0, float(it.get("score") or 0) or (90 - i * 5))),
                    "created_at": iso_now(),
                })
            if ideas_rows:
                idea_count = st.collect_ideas_insert(ideas_rows)
                md = ["# 创意灵感（自动采集）", "",
                      f"- 任务：{task.get('title') or ''}",
                      f"- 时间：{started}",
                      f"- 数据源：{_source_names(task)}",
                      f"- 素材目录：{os.path.relpath(out_dir, root)}", ""]
                for it in ideas_rows:
                    md += [f"## {it['title']}", f"- 分数：{it['score']:.0f}",
                           f"- 灵感：{it['summary']}", f"- 结合点：{it['detail']}",
                           f"- 标签：{it['tags']}", ""]
                _write(os.path.join(out_dir, "创意灵感.md"), "\n".join(md))
        except Exception as e:      # 分析失败不影响采集结果
            print(f"[collect] 灵感分析失败 task={task_id}：{type(e).__name__}: {e}", flush=True)
            note = (note + f"（灵感分析失败：{type(e).__name__}）").strip()

        if not file_count:
            # 一个文件都没落地也要留痕，前端标签页里能看出这轮干了什么
            _write(os.path.join(out_dir, "采集说明.md"),
                   f"# 采集说明\n\n- 任务：{task.get('title') or ''}\n- 时间：{started}\n"
                   f"- 数据源：{_source_names(task)}\n- 模型说明：{note or '未落盘文件'}\n")
            file_count = _file_count(out_dir)
        ok = True
    except Exception as e:          # noqa: BLE001 —— 采集失败只记录
        note = f"{type(e).__name__}: {e}"
        print(f"[collect] 采集失败 task={task_id}：{note}\n{traceback.format_exc()}", flush=True)
        try:
            _write(os.path.join(out_dir, "采集说明.md"),
                   f"# 采集失败\n\n- 任务：{task.get('title') or ''}\n- 时间：{started}\n"
                   f"- 原因：{note}\n")
        except Exception:
            pass
    finally:
        finished = iso_now()
        status = "ok" if ok else "error"
        try:
            st.collect_run_insert({
                "id": uid("cr"), "task_id": task_id, "phone": phone,
                "started_at": started, "finished_at": finished, "ok": 1 if ok else 0,
                "file_count": file_count, "idea_count": idea_count,
                "out_dir": os.path.relpath(out_dir, root), "note": note[:800],
            })
            st.collect_task_patch(task_id, {
                "status": status, "last_run": started, "last_note": note[:500],
                "next_run": next_run_of(task.get("schedule")) if int(task.get("enabled") or 0) else "",
                "updated_at": finished,
            })
        except Exception as e:
            print(f"[collect] 记录结果失败 task={task_id}：{type(e).__name__}: {e}", flush=True)
        _RUNNING.discard(task_id)
        print(f"[collect] 结束 task={task_id} ok={ok} files={file_count} ideas={idea_count}", flush=True)
    return {"ok": ok, "note": note, "files": file_count, "ideas": idea_count,
            "outDir": os.path.relpath(out_dir, root)}


def trigger(data_dir, phone, task_id, kernel_getter, manual=True):
    """异步触发一轮采集（不阻塞 HTTP 请求）。"""
    st = _store(data_dir)
    if not st or not st.collect_task(task_id):
        return {"ok": False, "error": "任务不存在"}
    if task_id in _RUNNING:
        return {"ok": False, "busy": True, "error": "该任务正在采集，请稍后"}

    def _job():
        try:
            run_task(data_dir, phone, task_id, kernel_getter, manual=manual)
        except Exception as e:   # noqa: BLE001
            print(f"[collect] 触发失败 task={task_id}：{type(e).__name__}: {e}", flush=True)

    threading.Thread(target=_job, daemon=True, name=f"collect-{task_id}").start()
    return {"ok": True, "started": True}


# ---------------------------------------------------------------- 调度线程

def _tick(kernel_getter):
    """扫一遍所有用户工作区的任务，跑到点的（串行）。"""
    if not DBX:
        return
    todos = []
    try:
        users_dir = USERS_DIR
        phones = []
        for name in sorted(os.listdir(users_dir)):
            p = os.path.join(users_dir, name)
            if os.path.isdir(p) and re.fullmatch(r"\d{6,20}", name):
                phones.append((name, p))
    except OSError:
        return
    now = iso_now()
    for phone, data_dir in phones:
        st = _store(data_dir)
        if not st:
            continue
        try:
            rows = st.collect_tasks("")
        except Exception as e:   # noqa: BLE001
            print(f"[collect] 读任务失败 {phone}：{type(e).__name__}: {e}", flush=True)
            continue
        for t in rows:
            if not int(t.get("enabled") or 0):
                continue
            due = str(t.get("next_run") or "")
            if not due:
                # 补一次下次时间，避免旧任务永远不跑
                try:
                    st.collect_task_patch(t["id"], {"next_run": next_run_of(t.get("schedule"))})
                except Exception:
                    pass
                continue
            if due <= now:
                todos.append((phone, data_dir, t["id"]))
    for phone, data_dir, tid in todos:
        if _STOP.is_set():
            return
        try:
            run_task(data_dir, phone, tid, kernel_getter)
        except Exception as e:   # noqa: BLE001
            print(f"[collect] 调度执行失败 task={tid}：{type(e).__name__}: {e}", flush=True)


def start(kernel_getter, stop_event=None):
    """启动后台调度线程（server 启动时调用一次）。OPC_COLLECT=0 可关。"""
    global _THREAD
    if os.environ.get("OPC_COLLECT", "1") != "1":
        print("[collect] 已按 OPC_COLLECT=0 关闭自动采集调度", flush=True)
        return None
    if stop_event is not None:
        stop_event.clear()
    if _THREAD and _THREAD.is_alive():
        return _THREAD

    def _loop():
        print("[collect] 自动采集调度启动", flush=True)
        # 启动后先等 15s，避开服务刚起来的资源高峰
        if _STOP.wait(15):
            return
        while not _STOP.is_set():
            try:
                _tick(kernel_getter)
            except Exception as e:   # noqa: BLE001
                print(f"[collect] 调度异常：{type(e).__name__}: {e}", flush=True)
            _STOP.wait(_TICK_SEC)

    _THREAD = threading.Thread(target=_loop, daemon=True, name="collect-ticker")
    _THREAD.start()
    return _THREAD