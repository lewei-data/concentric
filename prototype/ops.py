"""运营支撑（C1 第五刀）：会议记录、Obsidian 热同步、知识归档、交付导出。

从 kernel.Kernel 抽出，全部函数以 Kernel 实例为第一参数。不 import kernel，
kernel 单向依赖本模块（薄委托），因此不成环。

依赖的 kernel 属性：lock / db / DL / agents / meeting / meeting_log / meeting_records /
contents / messages / approvals / traces / projects / tone / knowledge / hotspots /
collection / is_owner / _kb_stamp / _cfg_stamp / _hotspot_scan_at，
以及实例方法 emit / reload_config / save_runtime。
"""
import json
import os
import time

import kb_views
from kb_views import _infer_category
import sancai as SC
from deps import *  # noqa: F401,F403 —— DL 等基础设施单例
from opc_paths import *  # noqa: F401,F403 —— PROJECT_FILE / ROSTER_FILE
from utils import iso_now, uid, _hotkey


def meeting_begin(kn, trace, title, participants, agenda=None):
    rec = {"id": uid("mt"), "traceId": trace, "title": title, "status": "live",
           "participants": [], "agenda": list(agenda or []), "speeches": [],
           "startedAt": iso_now(), "endedAt": "", "conclusion": "",
           "transcriptUrl": "", "minutesUrl": ""}
    with kn.lock:
        for aid in participants:
            a = kn.agents.get(aid)
            if a:
                rec["participants"].append({"id": aid, "name": a["name"],
                                            "role": a["role"], "projectId": a["projectId"]})
        kn.meeting_log = rec
        kn.meeting.update({"active": True, "title": title,
                             "participants": list(participants),
                             "agenda": list(agenda or []), "startedAt": rec["startedAt"]})
    if kn.db:
        kn.db.upsert_meeting(rec)
    kn.emit("meeting.start", trace, rec)
    return rec


def meeting_say(kn, aid, text, kind="speech", trace=""):
    text = str(text or "").strip()
    if not text:
        return None
    a = kn.agents.get(aid, {})
    with kn.lock:
        rec = kn.meeting_log
        if rec.get("status") != "live":
            return None
        item = {"id": uid("sp"), "speakerId": aid, "speaker": a.get("name", aid),
                "role": a.get("role", ""), "projectId": a.get("projectId", "all"),
                "kind": kind, "text": text[:1200], "at": iso_now()}
        rec["speeches"].append(item)
    if kn.db:
        kn.db.add_speech(rec.get("id"), len(rec["speeches"]), item)
    kn.emit("meeting.speech", trace or rec.get("traceId", ""), item)
    return item


def meeting_end(kn, trace, conclusion="", minutes_url=""):
    with kn.lock:
        rec = kn.meeting_log
        if rec.get("status") != "live":
            return None
        rec["status"] = "ended"
        rec["endedAt"] = iso_now()
        rec["conclusion"] = str(conclusion or "").strip()
        if minutes_url:
            rec["minutesUrl"] = minutes_url
        kn.meeting["active"] = False
        out = json.loads(json.dumps(rec, ensure_ascii=False))
        kn.meeting_records.insert(0, out)
        kn.meeting_records = kn.meeting_records[:40]
    if kn.DL:
        try:
            urls = kn.DL.save_meeting_record(out)
            out.update({k: v for k, v in urls.items() if v})
            with kn.lock:
                for r in kn.meeting_records:
                    if r.get("id") == out["id"]:
                        r.update({k: v for k, v in urls.items() if v})
        except Exception as e:
            print(f"[meeting] 会议记录落盘失败：{type(e).__name__}: {e}", flush=True)
    if kn.db:
        kn.db.upsert_meeting(out)
    kn.emit("meeting.end", trace, out)
    return out


def meeting_payload(kn):
    with kn.lock:
        current = json.loads(json.dumps(kn.meeting_log, ensure_ascii=False))
        history = []
        for r in kn.meeting_records:
            history.append({"id": r.get("id"), "traceId": r.get("traceId"),
                            "title": r.get("title"), "status": r.get("status"),
                            "startedAt": r.get("startedAt"), "endedAt": r.get("endedAt"),
                            "participants": r.get("participants") or [],
                            "agenda": r.get("agenda") or [],
                            "speechCount": len(r.get("speeches") or []),
                            "conclusion": r.get("conclusion") or "",
                            "transcriptUrl": r.get("transcriptUrl") or "",
                            "minutesUrl": r.get("minutesUrl") or "",
                            "legacy": bool(r.get("legacy"))})
    return {"ok": True, "active": bool(kn.meeting.get("active")),
            "current": current, "last": history[0] if history else None,
            "history": history}


def meeting_detail(kn, mid):
    mid = str(mid or "")
    with kn.lock:
        for r in kn.meeting_records:
            if mid in (r.get("id"), r.get("traceId")):
                return json.loads(json.dumps(r, ensure_ascii=False))
    return None


def _asset_owned(kn, item, url):
    """素材必须落在本条内容自己的交付目录里（真实路径，不再接受 asset:// 占位）。"""
    if not DL:
        return str(url).startswith(f"asset://{item['projectId']}/")
    try:
        prefix = kn.DL.plan(item["traceId"], item["projectId"], item["id"])["dir"] + "/"
    except Exception:
        return False
    return str(url).startswith(prefix)


def _schedule_export(kn, trace, item):
    """把当前阶段写入 Obsidian；文件地址先算好挂到 item.files，渲染异步串行跑。"""
    if not DL:
        return
    try:
        files = kn.DL.plan(trace, item["projectId"], item["id"])
        item["files"] = files
        project = dict(kn.projects.get(item["projectId"], {"name": item["projectId"], "color": "#64748B"}))
        tone = item.get("toneProfile") or kn.tone.get(item["projectId"], "")
        snap = json.loads(json.dumps(item, ensure_ascii=False))

        def job():
            res = kn.DL.export_item(snap, project, tone)
            if res.get("video_written") and (snap.get("renderCheck") or {}).get("pass"):
                with kn.lock:
                    cur = next((c for c in kn.contents if c["id"] == item["id"]), None)
                if cur:
                    cur["assets"] = [files["video"], files["cover"]]
                    kn.emit("pipeline", trace, cur)
            elif res.get("error"):
                print(f"[export] {item['id']} {res['error'][:120]}", flush=True)

        kn.DL.submit(job)
    except Exception as e:
        print(f"[export] 排期失败 {type(e).__name__}: {e}", flush=True)


def export_trace_docs(kn, trace):
    """本轮交付清单 + 过程记录。"""
    if not DL:
        return
    try:
        with kn.lock:
            contents = [dict(c) for c in kn.contents if c.get("traceId") == trace]
            msgs = [dict(m) for m in kn.messages if m.get("traceId") == trace]
            apprs = [dict(a) for a in kn.approvals if a.get("traceId") == trace]
            tr = dict(kn.traces.get(trace) or {})
        names = {pid: p["name"] for pid, p in kn.projects.items()}
        kn.DL.submit(lambda: kn.DL.export_trace(trace, contents, msgs, apprs, names, tr))
    except Exception as e:
        print(f"[export] trace 文档失败 {type(e).__name__}: {e}", flush=True)


def archive_knowledge(kn, trace, entries, stage_label="选题定稿"):
    """祁南（kb_manager）归档：按 traceId + 标题去重，重复触发只更新不叠加。"""
    made = []
    if not entries:
        return made
    with kn.lock:
        index = {(k.get("sourceTrace") or k.get("traceId"), k.get("title")): k
                 for k in kn.knowledge}
        for e in entries:
            title = str(e.get("title") or "").strip()
            if not title:
                continue
            pid = e.get("projectId") or "全公司"
            body = str(e.get("body") or "").strip()
            tags = [pid, stage_label] + [str(t) for t in (e.get("tags") or []) if t]
            cur = index.get((trace, title))
            if cur:
                if body:
                    cur["body"] = body[:300]
                cur["tags"] = list(dict.fromkeys(list(cur.get("tags") or []) + tags))[:12]
                cur["at"] = iso_now()
                made.append(cur)
                continue
            category = _infer_category({"title": title, "tags": tags})
            entry = {"id": uid("kb"), "projectId": pid, "traceId": trace,
                     "sourceTrace": trace, "title": title, "summary": title[:80],
                     "category": category,
                     "sancai": SC.norm(e.get("sancai")) or SC.infer(
                         {"title": title, "category": category, "tags": tags}),
                     "body": body[:300] or "（归档占位）",
                     "tags": list(dict.fromkeys(tags))[:12], "at": iso_now()}
            kn.knowledge.append(entry)
            index[(trace, title)] = entry
            made.append(entry)
    if made:
        if kn.DL:                                   # 写回 Obsidian，文件与 Web 同步落地
            for k in made:
                try:
                    kn.DL.save_knowledge(k)
                except Exception as e:
                    print(f"[kb] 写回 Obsidian 失败：{type(e).__name__}: {e}", flush=True)
        # 逐条写入 SQLite（带完整字段）
        if kn.db:
            for k in made:
                kn.db.upsert_knowledge(k)
        kn.emit("knowledge", trace, {
            "stage": stage_label, "count": len(made),
            "latest": [{"title": k["title"], "summary": k.get("summary") or k["title"][:80],
                        "projectId": k["projectId"], "id": k["id"]} for k in made[-3:]]})
    try:
        kb_views.rebuild_relations(kn)      # 新条目入档后补边，图谱随之长大
    except Exception as e:
        print(f"[kb] 关系边重建失败：{type(e).__name__}: {e}", flush=True)
    return made


def _kb_norm(item):
    """统一知识条目结构：文件解析结果与内存归档保持同一套字段。"""
    body = str(item.get("body") or "")
    category = item.get("category") or _infer_category(item)
    sancai = SC.norm(item.get("sancai")) or SC.infer(
        {"title": item.get("title"), "summary": item.get("summary") or body[:120],
         "category": category, "tags": item.get("tags")})
    return {"id": item.get("id"), "projectId": item.get("projectId"),
            "traceId": item.get("traceId") or item.get("sourceTrace") or "",
            "sourceTrace": item.get("sourceTrace") or "",
            "title": item.get("title") or "",
            "summary": item.get("summary") or body[:80],
            "category": category, "sancai": sancai,
            "body": body, "tags": item.get("tags") or [], "at": item.get("at") or "",
            # 出处：召回时直接给出，agent 按需读原文，省掉一次全库搜索
            "source": item.get("source") or item.get("path") or ""}


def _sync_knowledge_from_vault(kn):
    """运行中重扫 OPC/知识库：md 有变化就以文件为准替换 Web 端知识库。"""
    if not DL:
        return
    stamp = kn.DL.knowledge_stamp()
    if stamp == kn._kb_stamp:
        return
    # 上限必须远大于当前条目数：limit 截断会让差集清理把"没读到的条目"当死节点删掉
    items = kn.DL.load_knowledge(limit=5000)
    if not items:
        return
    fresh = [_kb_norm(k) for k in items]
    with kn.lock:
        before = len(kn.knowledge)
        kn.knowledge = fresh
    kn._kb_stamp = stamp
    # 同步到 SQLite：upsert 全量 + 差集删除（只 upsert 会把移走的条目留成死节点）
    if kn.db:
        for k in fresh:
            kn.db.upsert_knowledge(k)
        if len(fresh) >= max(1, before // 2):   # 读盘异常导致条目骤减时只写不删
            try:
                kn.db.prune_knowledge([k.get("id") for k in fresh])
            except Exception as e:
                print(f"[kb] 差集清理失败：{type(e).__name__}: {e}", flush=True)
        if len(fresh) != before:
            kn.emit("knowledge", "", {"stage": "vault-sync", "count": len(fresh)})
    try:
        kb_views.rebuild_relations(kn)      # 条目正文/标签变了，边要跟着重建
    except Exception as e:
        print(f"[kb] 关系边重建失败：{type(e).__name__}: {e}", flush=True)


def _sync_config_from_vault(kn):
    """项目配置/员工名册改动后自动热加载，不用手动点重载（仅 owner，共享 Obsidian 配置）。"""
    if not kn.is_owner:
        return
    try:
        stamp = (os.path.getmtime(PROJECT_FILE), os.path.getmtime(ROSTER_FILE))
    except OSError:
        return
    if stamp == kn._cfg_stamp:
        return
    kn._cfg_stamp = stamp
    try:
        info = kn.reload_config()
        print(f"[config] 检测到 Obsidian 配置变更，已热加载：{info}", flush=True)
    except Exception as e:
        print(f"[config] 自动热加载失败：{type(e).__name__}: {e}", flush=True)


def _sync_hotspots_from_vault(kn, force=False):
    """运行中重扫 OPC/热点采集：苏寒新落盘的业务简报无需重启即可进面板。"""
    if not DL:
        return
    now = time.time()
    if not force and now - kn._hotspot_scan_at < 60:
        return
    kn._hotspot_scan_at = now
    try:
        kn.collection = kn.DL.load_collection_status()
    except Exception as e:
        print(f"[hotspot] 读取采集状态失败：{type(e).__name__}: {e}", flush=True)
    try:
        disk = kn.DL.load_hotspots()
    except Exception as e:
        print(f"[hotspot] 扫描热点目录失败：{type(e).__name__}: {e}", flush=True)
        return
    if not disk:
        return
    changed = False
    with kn.lock:
        before = len(kn.hotspots)
        by_key = {_hotkey(h.get("title")): h for h in kn.hotspots if h.get("title")}
        for h in disk:
            key = _hotkey(h.get("title"))
            if not key:
                continue
            cur = by_key.get(key)
            if not cur:
                kn.hotspots.append(h)
                by_key[key] = h
            elif not cur.get("traceId"):
                # 内存里是启动时读入的旧简报，用磁盘上的最新字段覆盖
                for field in ("score", "tags", "at", "source"):
                    if h.get(field):
                        cur[field] = h[field]
        kn.hotspots = kb_views._hotspot_view(kn, 20)
        changed = len(kn.hotspots) != before
    if changed:
        kn.save_runtime()


