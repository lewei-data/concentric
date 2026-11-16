#!/usr/bin/env python3
"""任务中文名：把裸 trace 号（tr_d986ad5b）换成「第 007 号 · 《选题标题》」。

老板 2026-11-16 要求：邮件、飞书、语音播报里不要再出现一串字母加数字——

- 每个流水线任务有一个**中文名**：累计序号（3 位补零）+ 作品标题摘要；
- 序号在库里固化（``db.task_names`` 表），同一 trace 永远同一个号，永不回收；
- 历史任务按 ``pipeline_runs.started_at`` 升序一次性回填（``--backfill``）。

对外三个函数：

- ``label(trace, db=None, title="", with_total=False)`` → 「第 007 号 · 《标题》」
- ``speech(text, db=None)`` → 把文本里所有 ``tr_xxxxxxxx`` 换成「第 7 号《标题》」
- ``assign(trace, title="", db=None)`` → 建单即分配编号（标题可后补）

编号来源优先级：入参标题 > task_names.title > pipeline_runs.hotspot > 交付目录
``01-topic.md`` 的「# 选题 · xxx」。查不到就用「未编号任务」，绝不回落到裸 trace。
"""
import os
import re
import sqlite3
import threading
import time

TRACE_RE = re.compile(r"\btr_[0-9a-f]{6,12}\b", re.I)
TITLE_MAX = 24          # 展示用标题上限（汉字按 1 字算）
SPEECH_TITLE_MAX = 14   # 语音播报再短一点
CACHE_SEC = 30

_CACHE = {"at": 0.0, "key": "", "map": {}}
_LOCK = threading.Lock()


# ------------------------------------------------------------------ 基础
def _store(db=None):
    if db is not None:
        return db
    try:
        import db as DB
        return DB.store()
    except Exception:            # noqa: BLE001
        return None


def _db_path(db=None):
    try:
        import db as DB
        return str(getattr(db, "path", "") or DB.DB_PATH)
    except Exception:            # noqa: BLE001
        return ""


def _snapshot(db=None):
    """{trace: {"seq","title"}}，30 秒缓存（每个库路径一份）。"""
    key = _db_path(db)
    now = time.time()
    with _LOCK:
        if _CACHE["map"] and _CACHE["key"] == key and now - _CACHE["at"] < CACHE_SEC:
            return dict(_CACHE["map"])
    st = _store(db)
    data = {}
    if st is not None and hasattr(st, "task_names_all"):
        data = st.task_names_all() or {}
    elif key:
        try:
            con = sqlite3.connect(f"file:{key}?mode=ro", uri=True, timeout=5)
            con.row_factory = sqlite3.Row
            rows = con.execute("SELECT trace_id,seq,title FROM task_names").fetchall()
            data = {r["trace_id"]: {"seq": int(r["seq"]), "title": r["title"] or ""}
                    for r in rows}
            con.close()
        except Exception:        # noqa: BLE001
            data = {}
    works = _works_map(db)
    upgrade = []
    for trace, v in data.items():
        w = works.get(trace) or {}
        v["works"] = int(w.get("n") or 0)
        wt = _clean(w.get("title"))
        if wt and wt != _clean(v.get("title")):
            v["title"] = wt
            upgrade.append(trace)
    if upgrade and st is not None and hasattr(st, "task_name_assign"):
        for trace in upgrade:      # 建单时只有泛化选题名，有作品标题后自动升级
            try:
                st.task_name_assign(trace, data[trace]["title"])
            except Exception:      # noqa: BLE001
                pass
    with _LOCK:
        _CACHE.update({"at": now, "key": key, "map": data})
    return dict(data)


def _bust():
    with _LOCK:
        _CACHE["at"] = 0.0


def _row_work(trace, db=None):
    """→ (作品标题, 作品篇数)。取数：contents 首条标题 > 01-topic.md > hotspot 大类。"""
    key = _db_path(db)
    if not key:
        return "", 0
    title, works = "", 0
    try:
        con = sqlite3.connect(f"file:{key}?mode=ro", uri=True, timeout=5)
        r = con.execute(
            "SELECT (SELECT json_extract(raw,'$.title') FROM contents"
            "        WHERE trace_id=? ORDER BY rowid LIMIT 1),"
            "       (SELECT COUNT(*) FROM contents WHERE trace_id=?)", (trace, trace)).fetchone()
        con.close()
        if r:
            title = str(r[0] or "").strip()
            works = int(r[1] or 0)
    except Exception:            # noqa: BLE001
        pass
    if title:
        return title[:TITLE_MAX], works
    try:
        import deliverables as DL
        folder = os.path.join(DL.ROOT, trace)
        for name in (os.listdir(folder) if os.path.isdir(folder) else []):
            if not name.endswith(".md") or "topic" not in name:
                continue
            with open(os.path.join(folder, name), encoding="utf-8") as f:
                for line in f:
                    m = re.match(r"^#\s*(?:选题\s*·\s*)?(.+?)\s*$", line)
                    if m:
                        return m.group(1).strip()[:TITLE_MAX], works
    except Exception:            # noqa: BLE001
        pass
    try:
        con = sqlite3.connect(f"file:{key}?mode=ro", uri=True, timeout=5)
        r = con.execute("SELECT hotspot FROM pipeline_runs WHERE trace_id=?",
                        (trace,)).fetchone()
        con.close()
        if r and r[0]:
            return _clean(r[0])[:TITLE_MAX], works
    except Exception:            # noqa: BLE001
        pass
    return "", works


def _works_map(db=None):
    """{trace: {"n": 篇数, "title": 首条作品标题}}（升级泛化标题用）。"""
    key = _db_path(db)
    if not key:
        return {}
    try:
        con = sqlite3.connect(f"file:{key}?mode=ro", uri=True, timeout=5)
        rows = con.execute(
            "SELECT c.trace_id, COUNT(*),"
            " (SELECT json_extract(raw,'$.title') FROM contents c2"
            "   WHERE c2.trace_id=c.trace_id ORDER BY rowid LIMIT 1)"
            " FROM contents c GROUP BY c.trace_id").fetchall()
        con.close()
        return {r[0]: {"n": int(r[1] or 0), "title": str(r[2] or "").strip()} for r in rows}
    except Exception:            # noqa: BLE001
        return {}


def _title_of(raw):
    """hotspot 列等场景存的是 JSON，取其中的 title 字段；普通文本原样返回。"""
    t = str(raw or "").strip()
    if t.startswith("{"):
        try:
            import json as _json
            obj = _json.loads(t)
            if isinstance(obj, dict):
                t = str(obj.get("title") or obj.get("name") or "").strip()
        except Exception:            # noqa: BLE001
            t = ""
    return t


def _clip(text, limit):
    """按标点优先截断，避免断在词中间。"""
    t = str(text or "")
    if len(t) <= limit:
        return t
    head = t[:limit]
    for mark in ("」", "）", "》", "，", "、", "；", "：", "。", " "):
        cut = head.rfind(mark)
        if cut >= limit // 2:
            return head[:cut + (1 if mark in "」）》" else 0)].rstrip("，、；：")
    return head.rstrip("，、；：")


def _clean(title):
    t = re.sub(r"\s+", " ", _title_of(title)).strip().strip("《》")
    return t[:TITLE_MAX]


# ------------------------------------------------------------------ 对外
def assign(trace, title="", db=None):
    """建单即分配编号（幂等）；返回 {"seq","title"}，失败返回 {}。"""
    trace = str(trace or "").strip()
    if not trace:
        return {}
    if not _clean(title):
        title = _row_work(trace, db)[0]
    st = _store(db)
    info = {}
    if st is not None and hasattr(st, "task_name_assign"):
        info = st.task_name_assign(trace, title) or {}
    if info:
        _bust()
    return info


def info(trace, db=None, title=""):
    """取某 trace 的编号信息 {"seq","title"}；库里没有就现场分配。"""
    trace = str(trace or "").strip()
    if not trace:
        return {}
    snap = _snapshot(db)
    if trace in snap:
        cur = dict(snap[trace])
        cur.setdefault("works", 0)
        if title and not cur.get("title"):
            got = assign(trace, title, db)
            cur["title"] = got.get("title") or cur.get("title") or ""
        return cur
    got = assign(trace, title, db)
    if got:
        got["works"] = (_works_map(db).get(trace) or {}).get("n", 0)
    return got


def label(trace, db=None, title="", with_total=False, speech=False):
    """「第 007 号 · 《标题》」；无标题时只给编号，编号也拿不到则「未编号任务」。"""
    got = info(trace, db, title)
    seq = got.get("seq")
    if not seq:
        return "未编号任务"
    num = f"第 {seq} 号" if speech else f"第 {seq:03d} 号"
    if with_total and not speech:
        total = len(_snapshot(db))
        num = f"第 {seq:03d}/{max(total, seq):03d} 号"
    name = str(got.get("title") or "")
    works = int(got.get("works") or 0)
    name = _clip(name, SPEECH_TITLE_MAX if speech else 18)
    if not name:
        return num
    tail = f" 等 {works} 篇" if works > 1 else ""
    sep = "" if speech else " · "
    return f"{num}{sep}《{name}》{tail}"


def speech(text, db=None):
    """把文本里所有裸 trace 号换成中文任务名（语音播报 / 邮件正文通用）。"""
    raw = str(text or "")

    def _rep(m):
        return label(m.group(0), db, speech=True)

    return TRACE_RE.sub(_rep, raw)


# ------------------------------------------------------------------ CLI
def _backfill(db=None):
    """历史回填：按 pipeline_runs.started_at 升序给现有 trace 编号。"""
    st = _store(db)
    key = _db_path(db)
    if st is None or not key:
        print("库不可用")
        return 0
    con = sqlite3.connect(key, timeout=10)
    rows = con.execute(
        "SELECT trace_id,hotspot FROM pipeline_runs "
        "ORDER BY COALESCE(started_at,'') ASC, trace_id ASC").fetchall()
    con.close()
    had = _snapshot(db)
    n = 0
    for trace, hotspot in rows:
        if trace in had:
            continue
        if assign(trace, "", db):
            n += 1
    print(f"回填 {n} 条（现有 {len(_snapshot(db))} 条任务名）")
    return n


def main():
    import argparse
    ap = argparse.ArgumentParser(description="任务中文名（第 N 号 · 《标题》）")
    sub = ap.add_subparsers(dest="cmd")
    sub.add_parser("list", help="列出全部任务名")
    sub.add_parser("backfill", help="按 started_at 回填历史任务编号")
    p_show = sub.add_parser("show", help="查某个 trace 的中文名")
    p_show.add_argument("trace")
    p_sp = sub.add_parser("speech", help="把一段文本里的 trace 换成中文名")
    p_sp.add_argument("text")
    args = ap.parse_args()

    if args.cmd in (None, "list"):
        snap = _snapshot()
        for trace, v in sorted(snap.items(), key=lambda kv: kv[1]["seq"]):
            w = v.get("works") or 0
            suffix = f" 等 {w} 篇" if w > 1 else ""
            print(f"{v['seq']:03d}  {trace}  {v['title']}{suffix}")
        print(f"共 {len(snap)} 条")
    elif args.cmd == "backfill":
        _backfill()
    elif args.cmd == "show":
        print(label(args.trace, with_total=True))
    elif args.cmd == "speech":
        print(speech(args.text))


if __name__ == "__main__":
    main()