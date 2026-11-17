"""工厂/渲染模块（C1 第三刀）：分镜工作台、素材作业、出片验收门、工具箱。

从 kernel.Kernel 抽出，全部函数以 Kernel 实例为第一参数。不 import kernel，
kernel 单向依赖本模块（委托调用），因此不成环。

依赖的 kernel 属性：data_dir / vault_dir / lock / db / contents / active_trace /
projects / tone / DL / _stage_queue / _stage_thread / stage_launches，
以及实例方法 emit / send / items_emit / resume_pipeline。
"""
import json
import os
import queue
import threading
import time

import company_config as CC
import task_names as TN
from deps import *  # noqa: F401,F403 —— OMR / DL / DBX 等基础设施单例
from opc_paths import *  # noqa: F401,F403 —— VAULT / OWNER_DATA_DIR
from utils import iso_now, uid, _num, _step_view

try:
    import media_jobs as MEDIA
except Exception as _e:  # 素材作业模块缺失不应拖垮整个内核
    MEDIA = None
    print(f"[boot] media_jobs 导入失败：{type(_e).__name__}: {_e}", flush=True)

MEDIA_UNAVAILABLE = "素材作业模块不可用（media_jobs 导入失败，见启动日志）"


# ---------- 重发（分镜素材 / 出片阶段） ----------
_RESEND_SKIP = ("不新增任务", "锁定不动", "已含在", "无新增", "原登记")


_RESEND_AUDIO = ("BGM", "bgm", "音频", "wav", "配音", "音效", "语音")


# ---------- 分镜工作台：多渠道并行生成 + 三环节手工多次启动 ----------
STAGE_BUTTONS = {"render": "素材制作", "sample_review": "剪辑合成", "final": "终版合成"}


# ---------- 工具箱：单技能手工验证（不进流水线，直接排队出片） ----------
TOOLBOX_SUB = {"image": "图片", "video": "视频", "audio": "语音"}


def factory_view(kn):
    """制片工厂聚合：13 类车间 + 各车间产品（工艺决策 + 工位过程 + 运行态）。

    产品分两类：
    1. 正式产品：CCO 审批通过、已发流水号（product_crafts 有记录），带完整工艺轴；
    2. 待定工艺占位：工位已开工（assessment 起）但 CCO 还没定工艺发流水号的内容单——
       审批通过后到评估报告跑完之间有十几分钟空窗，工位消息已经「开工」却看不到产品，
       这里用第 N 号中文名占位显示，评估完成落库后自动被正式产品取代。
    工艺轴只取工厂段（进厂 assessment → 出厂 review），前段选题/开工审批与
    后段发布/归档/汇报在 hermes 调度，不属于工厂工序。
    """
    workshops = [dict(w) for w in CC.OPENMONTAGE_CRAFTS]
    enabled = [s for s in CC.PIPELINE.stages if s.enabled]
    ids = [s.id for s in enabled]
    try:
        lo, hi = ids.index(CC.FACTORY_ENTRY_STAGE), ids.index(CC.FACTORY_EXIT_STAGE)
    except ValueError:
        lo, hi = 0, len(ids) - 1
    axis_stages = enabled[min(lo, hi):max(lo, hi) + 1]
    stage_axis = [{"id": s.id, "name": s.name, "kind": s.kind, "omStage": s.om_stage}
                  for s in axis_stages]
    axis_ids = {s.id for s in axis_stages}
    products = []
    if kn.db:
        runs = {r.get("trace_id"): r for r in kn.db.pipeline_runs_recent(80)}
        steps_by_trace = {}
        for s in kn.db.workshop_steps():
            steps_by_trace.setdefault(s.get("trace_id"), []).append(s)
        craft_traces = set()   # 已进厂（有流水号）的 trace，占位产品要排除
        for row in kn.db.product_crafts(120):
            tid = row.get("trace_id") or ""
            craft_traces.add(tid)
            run = runs.get(tid) or {}
            # 工艺步骤序列：CCO 审批时快照（steps 列），老数据回退到工艺定义现值
            try:
                seq = json.loads(row.get("steps") or "null")
            except ValueError:
                seq = None
            if not seq:
                seq = CC.CRAFT_STEPS.get(row.get("craft_id") or "", [])
            steps_by_stage = {s.get("stage_id"): _step_view(s)
                              for s in steps_by_trace.get(tid, [])}
            craft_steps = []
            for st in seq:
                rec = steps_by_stage.get(st.get("opcStage"))
                craft_steps.append({**st,
                                    "status": (rec or {}).get("status") or "pending",
                                    "startedAt": (rec or {}).get("started_at") or "",
                                    "finishedAt": (rec or {}).get("finished_at") or ""})
            products.append({
                "itemId": row.get("item_id"), "traceId": tid,
                "serialNo": row.get("serial_no") or "",
                "projectId": row.get("project_id"), "title": row.get("title"),
                "workshopNo": row.get("workshop_no"), "craftId": row.get("craft_id"),
                "craftName": row.get("craft_name"), "product": row.get("product"),
                "renderMode": row.get("render_mode"), "reason": row.get("reason"),
                "decidedBy": row.get("decided_by"),
                "decidedAt": row.get("updated_at") or row.get("created_at"),
                "shippedAt": row.get("shipped_at") or "",
                "craftSteps": craft_steps,
                "run": {"status": run.get("status"), "stage": run.get("stage"),
                        "stageIndex": run.get("stage_index"),
                        "updatedAt": run.get("updated_at"), "closedAt": run.get("closed_at")},
                "steps": [v for v in steps_by_stage.values()
                          if v.get("stage_id") in axis_ids],
            })

        # 待定工艺占位：工位已开工（工厂段有 running 步骤）但还没 CCO 定工艺发流水号的在制任务。
        # 让「工位显示开工却看不到产品」的空窗期也在工厂页可见；评估落库后这条自动消失。
        # 判据用「工位步骤 running」而不是 run.status：内核重启会把未跑完的 run 标成 interrupted，
        # 但工位是否真在动以 workshop_steps 为准（用户看到的就是这个）。
        for tid, run in runs.items():
            if tid in craft_traces:
                continue
            factory_steps = [s for s in steps_by_trace.get(tid, [])
                             if s.get("stage_id") in axis_ids
                             and s.get("status") == "running"]
            if not factory_steps:
                continue
            cur = factory_steps[0]
            stage_id = cur.get("stage_id") or ""
            stage_name = next((s["name"] for s in stage_axis if s["id"] == stage_id), stage_id)
            started = cur.get("started_at") or run.get("started_at") or ""
            # 占位工艺轴：已走过的工厂段工位按库内状态点亮，未到的 pending，方便看出进展
            done_ids = {s.get("stage_id") for s in steps_by_trace.get(tid, [])
                        if s.get("status") in ("done", "stopped", "failed")}
            pending_steps = [{
                "id": s["id"], "name": s["name"], "omStage": s.get("omStage", ""),
                "opcStage": s["id"],
                "status": "running" if s["id"] == stage_id
                          else "done" if s["id"] in done_ids else "pending",
                "startedAt": started if s["id"] == stage_id else "",
                "finishedAt": "",
            } for s in stage_axis]
            products.append({
                "itemId": "", "traceId": tid,
                "serialNo": "", "title": TN.label(tid, kn.db),
                "projectId": "", "workshopNo": "", "craftId": "",
                "craftName": "工艺待定", "product": "",
                "renderMode": "", "reason": "CCO 正在评估工艺，流水线评估完成后自动进厂并生成流水号",
                "decidedBy": "待 CCO 定工艺",
                "decidedAt": started,
                "shippedAt": "",
                "craftSteps": pending_steps,
                "run": {"status": run.get("status"), "stage": run.get("stage"),
                        "stageIndex": run.get("stage_index"),
                        "updatedAt": run.get("updated_at"), "closedAt": run.get("closed_at")},
                "steps": [v for v in steps_by_trace.get(tid, [])
                          if v.get("stage_id") in axis_ids],
                "pendingCraft": True,
                "currentStage": stage_name,
            })
    return {"ok": True, "workshops": workshops, "stages": stage_axis, "products": products}


def _stage_render(kn, c):
    """7.5 真实出片：MCP-TTS 旁白 + OpenMontage 渲染 + 画面验收门（多条目并行）"""
    # 视频制作是流水线耗时大头：多条目并行渲染（逐条内部仍按镜串行，
    # 共享的 MCP-TTS 进程在 om_render.TTSClient 内部有锁，不会串流）。
    c.render_blocked = set()
    from concurrent.futures import ThreadPoolExecutor
    total = len(c.items)
    args = [(idx, total, iid, item) for idx, (iid, item) in enumerate(c.items.items(), 1)]
    workers = min(4, total) if total > 1 and os.environ.get("OPC_SKIP_RENDER") != "1" else 1
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for _ in pool.map(lambda a: _render_one(kn, c, *a), args):
            pass


def _render_one(kn, c, idx, total, iid, item):
    pid = item["projectId"]
    kn.send("biz.event", f"vd_{pid}", "ceo", c.trace,
              {"text": f"真实出片 {idx}/{total}：《{item['title']}》", "step": "render"},
              project=pid, priority=2)
    if os.environ.get("OPC_SKIP_RENDER") == "1":
        files = item.get("files") or {}
        item["renderCheck"] = {"pass": True, "reasons": ["测试模式：跳过真实渲染"]}
        item["assets"] = [files.get("video", ""), files.get("cover", "")]
        item["renderSkipped"] = True
    elif not OMR:
        item["renderCheck"] = {"pass": False, "reasons": ["渲染模块不可用"]}
    else:
        res = OMR.produce(item, kn.projects[pid], kn.tone.get(pid, ""),
                          CC.DELIVERY.get(pid) or CC.DELIVERY["_default"])
        item["render"] = {k: v for k, v in res.items() if k != "check"}
        item["renderCheck"] = res.get("check") or {
            "pass": False, "reasons": [res.get("reason") or "未知渲染错误"]}
        if res.get("ok"):
            item["assets"] = [res["videoUrl"], res["coverUrl"]]
        else:
            item["assets"] = []
            item["renderError"] = res.get("reason", "")
    if not item["renderCheck"].get("pass"):
        c.render_blocked.add(iid)
        item["blockedBy"] = "render-gate"
        reason = item.get("renderError") or "；".join(
            (item["renderCheck"].get("reasons") or [])[:2])
        kn.send("escalation", f"vd_{pid}", "owner", c.trace,
                  {"text": f"《{item['title']}》样片未过画面验收：{reason}"},
                  project=pid, priority=2)
    kn.emit("render-gate", c.trace, {"itemId": iid,
                                    "pass": item["renderCheck"].get("pass"),
                                    "check": item["renderCheck"]})
    kn.items_emit(c.trace, item)


def resend_render(kn, trace_id, action="assets", shots=None, stage="render",
                  prompts=None, project=""):
    """按 trace 重发制作需求（老板手工补发，不动流水线其它环节）。

    action=assets：从内容单逐镜表（item.plan.storyboardShots）取「画面」，
      逐镜重新提交 ComfyUI 文生图；跳过锁定镜与纯音频镜；后台串行生成，接口立即返回。
    action=render：把过程库断点改到 stage（默认 render），复用 resume_pipeline 重跑该阶段。
    """
    trace_id = str(trace_id or "").strip()
    if not trace_id:
        raise ValueError("缺少 traceId")
    act = str(action or "assets").strip().lower()
    if act in ("render", "video", "reproduce", "stage"):
        return _resend_stage(kn, trace_id, str(stage or "render").strip())
    if act not in ("assets", "asset", "image"):
        raise ValueError(f"不支持的重发类型：{action}（可选 assets / render）")

    with kn.lock:
        items = [dict(c) for c in kn.contents if c.get("traceId") == trace_id]
    if not items:
        raise ValueError("该 trace 下没有内容单，无法重发素材")
    only = {int(s) for s in (shots or []) if str(s).strip().isdigit()}
    reqs = []
    if prompts:
        reqs = [{"no": i + 1, "prompt": str(p).strip(), "itemId": items[0]["id"],
                 "projectId": items[0].get("projectId", "")}
                for i, p in enumerate(prompts) if str(p).strip()]
    else:
        for it in items:
            for idx, shot in enumerate(((it.get("plan") or {}).get("storyboardShots") or []), 1):
                try:
                    no = int(shot.get("no") or idx)
                except (TypeError, ValueError):
                    no = idx
                if only and no not in only:
                    continue
                mat = str(shot.get("素材") or "")
                pic = str(shot.get("画面") or "").strip()
                if not pic:
                    continue
                if any(k in mat for k in _RESEND_SKIP):
                    continue
                if any(k in mat for k in _RESEND_AUDIO):
                    continue
                reqs.append({"no": no, "prompt": pic, "itemId": it["id"],
                             "projectId": it.get("projectId", "")})
    if not reqs:
        raise ValueError("该 trace 的分镜里没有可重发的画面素材需求（可能全是锁定镜或只有音频）")

    threading.Thread(target=_resend_assets_worker, args=(kn, trace_id, reqs),
                     daemon=True).start()
    kn.send("biz.event", "vd_wenren", "owner", trace_id,
              {"text": f"分镜素材重发：{len(reqs)} 条已提交 ComfyUI", "step": "assets-resend"},
              priority=2)
    return {"ok": True, "traceId": trace_id, "action": "assets",
            "queued": len(reqs), "requests": reqs,
            "note": "已在后台逐镜生成，产物落 OPC/交付物/<trace>/<项目-单号>/assets/images/"}


def _resend_assets_worker(kn, trace_id, reqs):
    """后台串行补图：ComfyUI 单 worker，串行反而稳；失败逐条升级给老板。"""
    try:
        import asset_fill
    except Exception as exc:
        kn.send("escalation", "cto", "owner", trace_id,
                  {"text": f"素材重发失败：素材模块不可用（{type(exc).__name__}: {exc}）"},
                  priority=2)
        return
    done = 0
    for r in reqs:
        pid = r.get("projectId") or ""
        out_dir = os.path.join(VAULT, "OPC", "交付物", trace_id,
                               f"{pid}-{r['itemId']}", "assets", "images")
        out_path = os.path.join(out_dir, f"shot{r['no']:02d}.png")
        try:
            asset_fill.gen_image(r["prompt"], out_path, width=768, height=1344)
            done += 1
            kn.send("biz.event", f"vd_{pid}", "owner", trace_id,
                      {"text": f"素材重发完成：镜{r['no']}", "file": out_path,
                       "step": "assets"}, project=pid, priority=2)
        except Exception as exc:
            kn.send("escalation", f"vd_{pid}", "owner", trace_id,
                      {"text": f"素材重发失败：镜{r['no']}（{type(exc).__name__}: {exc}）"},
                      project=pid, priority=2)
    print(f"[resend] {trace_id} 素材重发收口：成功 {done}/{len(reqs)}", flush=True)


def _resend_stage(kn, trace_id, stage):
    """把断点改到指定阶段并续跑（重出片走这里，不改分镜与文案）。"""
    if not kn.db:
        raise ValueError("过程库未启用（OPC_DB=0），无法重发出片")
    if kn.active_trace and kn.active_trace != trace_id:
        raise ValueError(f"已有任务在跑（{kn.active_trace}），等它结束再重发")
    row = kn.db.pipeline_run(trace_id)
    if not row:
        raise ValueError("过程库里没有这条 trace 的运行记录（无法定位断点）")
    stages = [s.get("id") for s in (CC.PIPELINE.stages or [])]
    if stage not in stages:
        raise ValueError(f"未知阶段：{stage}（可选 {', '.join(x for x in stages if x)}）")
    state = json.loads(row.get("state") or "{}")
    kn.db.pipeline_run_stage(trace_id, stage, stages.index(stage), state)
    info = kn.resume_pipeline(trace_id)
    return {"ok": True, "traceId": trace_id, "action": "render",
            "from": info.get("from"), "note": f"已从 {stage} 阶段重跑"}


def _media(kn):
    """当前工作区的素材作业管理器（按 data_dir 隔离，绑定事件推送）。"""
    if MEDIA is None:
        raise ValueError(MEDIA_UNAVAILABLE)
    root = kn.data_dir or os.path.dirname(os.path.abspath(__file__))
    mgr = MEDIA.manager(os.path.join(root, "media_jobs.db"))
    mgr.notify = kn.emit
    return mgr


def _shot_item(kn, trace_id, item_id=""):
    """取内容单：优先内存（最新），回退过程库 contents.raw。"""
    with kn.lock:
        for it in kn.contents:
            if it.get("traceId") == trace_id and (not item_id or it.get("id") == item_id):
                return dict(it)
    if not kn.db:
        return {}
    try:
        if item_id:
            row = kn.db.fetch_one(
                "SELECT raw FROM contents WHERE trace_id=? AND id=? LIMIT 1",
                (trace_id, item_id))
        else:
            row = kn.db.fetch_one(
                "SELECT raw FROM contents WHERE trace_id=? ORDER BY created_at DESC LIMIT 1",
                (trace_id,))
    except Exception as e:
        print(f"[factory] 读内容单失败：{type(e).__name__}: {e}", flush=True)
        return {}
    if not row:
        return {}
    try:
        return json.loads(row.get("raw") or "{}")
    except ValueError:
        return {}


def _shots_of(kn, item):
    """分镜清单：优先方案里的 storyboardShots（含素材标注），回退 storyboard 文本归一化。"""
    plan = item.get("plan") or {}
    shots = []
    for idx, s in enumerate(plan.get("storyboardShots") or [], 1):
        if not isinstance(s, dict):
            continue
        try:
            no = int(s.get("no") or idx)
        except (TypeError, ValueError):
            no = idx
        shots.append({"no": no,
                      "prompt": str(s.get("画面") or s.get("prompt") or "").strip(),
                      "line": str(s.get("台词") or s.get("line") or "").strip(),
                      "sec": _num(s.get("秒") or s.get("sec") or 0),
                      "asset": str(s.get("素材") or s.get("asset") or "").strip()})
    total = _num(item.get("seconds") or 0)
    if not total:
        total = _num(item.get("durationSec") or 0) or _num((plan or {}).get("durationSec") or 0)
    if not shots and item.get("storyboard"):
        sb = item.get("storyboard")
        # 真实分镜多是「镜一：…镜二：…」一整段，先走宽松切分器
        if isinstance(sb, str) and MEDIA:
            try:
                shots = MEDIA.split_storyboard(sb, total or 45)
            except Exception as e:
                print(f"[factory] 分镜切分失败：{type(e).__name__}: {e}", flush=True)
        if not shots and kn.DL:
            try:
                for idx, s in enumerate(kn.DL.normalize_storyboard(sb, total or 45), 1):
                    try:
                        no = int(s.get("no") or idx)
                    except (TypeError, ValueError):
                        no = idx
                    shots.append({"no": no,
                                  "prompt": str(s.get("visual") or "").strip(),
                                  "line": str(s.get("line") or "").strip(),
                                  "sec": _num(s.get("dur") or 0), "asset": ""})
            except Exception as e:
                print(f"[factory] 分镜归一化失败：{type(e).__name__}: {e}", flush=True)
    # 没有任何时长的，按总时长均分，保证视频渠道有时长可用
    avg = round(total / len(shots), 2) if (total and shots) else 0
    for s in shots:
        if not s["sec"] and avg:
            s["sec"] = avg
    return shots


def _shot_out_dir(kn, trace_id, project_id, item_id, kind):
    sub = {"image": "images", "video": "videos", "audio": "audio"}[kind]
    return os.path.join(kn.vault_dir, "OPC", "交付物", trace_id,
                        f"{project_id or 'x'}-{item_id or 'x'}", "assets", sub)


def factory_channels(kn):
    """渠道注册表 + 实时可用性（前端工作台渲染参数表单与渠道选择）。"""
    if MEDIA is None:
        raise ValueError("素材作业模块不可用（media_jobs 导入失败）")
    chans, cap = MEDIA.channels(force=True)
    return {"channels": chans, "maxSec": cap}


def toolbox_save_frame(kn, raw):
    """校验并保存图生视频首帧，返回文件名（提交时作为 frame 传回）。"""
    if MEDIA is None:
        raise ValueError(MEDIA_UNAVAILABLE)
    if not raw:
        raise ValueError("请求体为空（应上传图片文件）")
    if len(raw) > 8 * 1024 * 1024:
        raise ValueError("首帧图片超过 8MB，请先压缩")
    if raw.startswith(b"\x89PNG\r\n\x1a\n"):
        ext = ".png"
    elif raw.startswith(b"\xff\xd8\xff"):
        ext = ".jpg"
    elif raw[:4] == b"RIFF" and raw[8:12] == b"WEBP":
        ext = ".webp"
    else:
        raise ValueError("仅支持 png / jpg / webp 图片")
    name = "frame-" + os.urandom(6).hex() + ext
    with open(os.path.join(MEDIA.frame_dir(), name), "wb") as f:
        f.write(raw)
    return name


def toolbox_voices(kn):
    """音色表（含 clone- 自建音色）；media-engine 每次合成前重读 voices.json，写入即生效。"""
    if MEDIA is None:
        raise ValueError(MEDIA_UNAVAILABLE)
    d = MEDIA.voices_list(force=True)
    return {"default": d.get("default") or "",
            "voices": d.get("voices") or [],
            "cloned": [v["id"] for v in d.get("voices") or [] if v.get("cloned")]}


def toolbox_clone_voice(kn, raw, name="", text="", owner=""):
    """上传参考音频建音色：转 16k 单声道 wav + 写入共享 voices.json，无需重启 media-engine。"""
    if MEDIA is None:
        raise ValueError(MEDIA_UNAVAILABLE)
    return MEDIA.clone_voice(raw, text, name=name, owner=owner)


def toolbox_tune_voice(kn, vid, direction="", raw=b"", text=""):
    """微调一轮：按方向改参考音频（或换新录音）→ 重新登记 → 自检不过自动还原。"""
    if MEDIA is None:
        raise ValueError(MEDIA_UNAVAILABLE)
    return MEDIA.tune_clone_voice(vid, direction, raw, text)


def toolbox_undo_voice(kn, vid):
    """撤销最后一轮微调，回到上一版参考。"""
    if MEDIA is None:
        raise ValueError(MEDIA_UNAVAILABLE)
    return MEDIA.undo_tune(vid)


def toolbox_voice_profile(kn, vid):
    """微调档案：可选方向 + 已调轮次。"""
    if MEDIA is None:
        raise ValueError(MEDIA_UNAVAILABLE)
    return MEDIA.voice_profile(vid)


def toolbox_delete_voice(kn, vid):
    """删除自建音色（只允许 clone- 前缀，内置音色不可动）。"""
    if MEDIA is None:
        raise ValueError(MEDIA_UNAVAILABLE)
    return MEDIA.delete_clone_voice(vid)


def toolbox_voice_ref(kn, vid):
    """试听用：取某个克隆音色的参考音频路径。"""
    if MEDIA is None:
        raise ValueError(MEDIA_UNAVAILABLE)
    return MEDIA.clone_ref_path(vid)


def voice_preview(kn, channel, voice, text=""):
    """车间音色试听：按渠道+音色合成固定短句，服务端缓存。"""
    if MEDIA is None:
        raise ValueError(MEDIA_UNAVAILABLE)
    return MEDIA.voice_preview(channel, voice, text)


def toolbox_channels(kn):
    """渠道注册表（分组/端点/能力）+ 图生视频可用性。"""
    if MEDIA is None:
        raise ValueError(MEDIA_UNAVAILABLE)
    chans, cap = MEDIA.channels(force=True)
    i2v = [c["name"] for c in chans
           if "i2v" in (c.get("modes") or []) and c.get("available")]
    i2i = [c["name"] for c in chans
           if "i2i" in (c.get("modes") or []) and c.get("available")]
    return {"channels": chans, "groups": MEDIA.groups(), "maxSec": cap,
            "modes": {"i2v": bool(i2v), "i2vChannels": i2v,
                      "i2vReason": "" if i2v else
                      "当前没有可用的图生视频渠道（Wan2.2 TI2V 的视频端点 8189 不可达）",
                      "i2i": bool(i2i), "i2iChannels": i2i,
                      "i2iReason": "" if i2i else
                      "当前没有可用的图生图渠道（ComfyUI 或百炼图像未就绪）"}}


def toolbox_run(kn, kind, channels, prompt="", line="", sec=0.0, params=None,
                frame=""):
    """手工提交一条单技能任务：独立 trace（tb-），成品直接落 vault 素材目录。"""
    if MEDIA is None:
        raise ValueError(MEDIA_UNAVAILABLE)
    kind = str(kind or "").strip()
    if kind not in TOOLBOX_SUB:
        raise ValueError("类型只能是 image / audio / video")
    prompt = str(prompt or "").strip()
    line = str(line or "").strip()
    if kind == "audio":
        if not (line or prompt):
            raise ValueError("口播任务需要台词")
    elif not prompt:
        raise ValueError("出图/出片任务需要提示词")
    chans = [str(c).strip() for c in (channels or []) if str(c).strip()]
    if not chans:
        raise ValueError("至少选择一个渠道")
    params = dict(params or {})
    frame = os.path.basename(str(frame or "").strip())
    if frame:
        fp = os.path.join(MEDIA.frame_dir(), frame)
        if not os.path.isfile(fp):
            raise ValueError("参考图片不存在或已被清理，请重新上传")
        # 只往支持 i2v（视频首帧）或 i2i（图片参考图）的渠道挂输入图，其它渠道忽略
        for name in chans:
            ch = MEDIA.CHANNELS.get(name) or {}
            modes = ch.get("modes") or []
            if "i2v" in modes or "i2i" in modes:
                params[name] = dict(params.get(name) or {}, frame=frame)
    trace_id = "tb-" + os.urandom(4).hex()
    day = time.strftime("%Y-%m-%d")
    # 成品落在个人空间而不是公共 vault：工具箱内容属于本人的私有产物，
    # 因此不经 /vault 或 /toolbox 公开，只走 /private/<手机号>/工具箱/（服务端按会话校验）
    out_dir = os.path.join(kn.data_dir or OWNER_DATA_DIR, "工具箱", day,
                           TOOLBOX_SUB[kind])
    try:
        os.makedirs(out_dir, exist_ok=True)
    except OSError as e:
        raise ValueError(f"素材目录不可写（{out_dir}）：{e}") from e
    mgr = _media(kn)
    res = mgr.submit(trace_id=trace_id, item_id="toolbox", project_id="toolbox",
                     shot_no=0, kind=kind, channels=chans, prompt=prompt,
                     line=line, sec=_num(sec), params=params or {},
                     out_dir=out_dir, vault_dir=kn.vault_dir)
    jobs = res.get("jobs") or []
    if jobs:
        try:
            kn.emit("toolbox.job", trace_id,
                      {"jobIds": [j.get("id") for j in jobs], "kind": kind})
        except Exception:  # noqa: BLE001 - 推送失败不影响已入队的作业
            pass
    return {"traceId": trace_id, "outDir": out_dir,
            "jobs": jobs, "warnings": res.get("warnings") or []}


def toolbox_jobs(kn, limit=40):
    """工具箱最近的手工任务（含产物地址，供工作台轮询）。"""
    if MEDIA is None:
        return []
    return _media(kn).toolbox_jobs(limit=limit)


def forget_output(kn, dest):
    """成品从素材页删掉后，清空作业库里指向它的产物地址（历史记录保留）。"""
    if MEDIA is None:
        return 0
    try:
        return _media(kn).forget_output(dest)
    except Exception as e:
        print(f"[media] 清空产物记录失败：{type(e).__name__}: {e}", flush=True)
        return 0


def factory_shots(kn, trace_id, item_id=""):
    """分镜清单 + 渠道注册表 + 每镜作业状态（前端工作台一次拉全）。"""
    trace_id = str(trace_id or "").strip()
    if not trace_id:
        raise ValueError("缺少 traceId")
    mgr = _media(kn)
    item = _shot_item(kn, trace_id, item_id)
    item_id = str(item_id or item.get("id") or "").strip()
    project_id = str(item.get("projectId") or "").strip()
    shots = _shots_of(kn, item)
    overrides = mgr.shots(trace_id, item_id) if item_id else {}
    job_rows = mgr.jobs(trace_id, item_id, limit=400) if item_id else []
    by_shot = {}
    for j in job_rows:
        by_shot.setdefault(int(j.get("shotNo") or 0), []).append(j)
    chans, cap = MEDIA.channels()
    out = []
    for s in shots:
        ov = overrides.get(s["no"]) or {}
        try:
            saved_ch = json.loads(ov.get("channels") or "[]")
        except ValueError:
            saved_ch = []
        try:
            saved_params = json.loads(ov.get("params") or "{}")
        except ValueError:
            saved_params = {}
        jobs = by_shot.get(s["no"]) or []
        active = [j for j in jobs if j.get("status") in ("queued", "running")]
        done = [j for j in jobs if j.get("status") == "done"]
        out.append({
            "no": s["no"],
            "prompt": ov.get("prompt") if ov.get("prompt") not in (None, "") else s["prompt"],
            "line": ov.get("line") if ov.get("line") not in (None, "") else s["line"],
            "sec": _num(ov.get("sec")) if ov.get("sec") not in (None, "") else s["sec"],
            "asset": s["asset"],
            "channels": saved_ch,
            "params": saved_params,
            "jobs": jobs,
            "summary": {"total": len(jobs), "active": len(active), "done": len(done),
                        "failed": len([j for j in jobs if j.get("status") == "failed"]),
                        "progress": max([int(j.get("progress") or 0) for j in jobs] or [0])},
        })
    seconds = _num(item.get("seconds") or item.get("durationSec")
                   or (item.get("plan") or {}).get("durationSec") or 0)
    if not seconds:  # 内容单没记总时长时，用分镜时长之和兜底（前端显示要一致）
        seconds = round(sum(s.get("sec") or 0 for s in out), 2)
    return {"traceId": trace_id, "itemId": item_id, "projectId": project_id,
            "title": item.get("title") or "", "tone": item.get("tone") or "",
            "stage": item.get("stage") or "", "seconds": seconds,
            "maxSec": cap, "channels": chans, "shots": out,
            "activeTrace": kn.active_trace,
            "stages": [{"stage": k, "label": v} for k, v in STAGE_BUTTONS.items()]}


def factory_shot_save(kn, trace_id, item_id, shot_no, fields):
    """保存分镜提示词/台词/时长/渠道与参数（同时回写内容单，后续重跑沿用）。"""
    mgr = _media(kn)
    rec = mgr.shot_save(trace_id, item_id, shot_no, fields or {})
    # 回写内容单，保证「素材制作」重跑时用的是改过的提示词
    with kn.lock:
        target = None
        for it in kn.contents:
            if it.get("traceId") == trace_id and it.get("id") == item_id:
                target = it
                break
    if target is None:
        target = _shot_item(kn, trace_id, item_id) or None
    if target:
        plan = target.setdefault("plan", {})
        arr = plan.get("storyboardShots") or []
        changed = False
        for idx, s in enumerate(arr, 1):
            if not isinstance(s, dict):
                continue
            try:
                no = int(s.get("no") or idx)
            except (TypeError, ValueError):
                no = idx
            if no != int(shot_no):
                continue
            if fields.get("prompt") not in (None, ""):
                s["画面"] = fields["prompt"]
            if fields.get("line") not in (None, ""):
                s["台词"] = fields["line"]
            if fields.get("sec") not in (None, ""):
                s["秒"] = _num(fields["sec"])
            changed = True
            break
        if changed:
            try:
                if kn.db:
                    kn.db.upsert_content(target)
            except Exception as e:
                print(f"[factory] 分镜回写失败：{type(e).__name__}: {e}", flush=True)
    return rec


def factory_run(kn, trace_id, item_id, kind, channels, shots=None, params=None):
    """把所选分镜按所选渠道并发提交（同一提示词可同时投多个渠道）。"""
    mgr = _media(kn)
    data = factory_shots(kn, trace_id, item_id)
    if not data["shots"]:
        raise ValueError("该内容单没有分镜清单（先在方案里生成分镜）")
    want = {int(s) for s in (shots or []) if str(s).strip().isdigit()}
    targets = [s for s in data["shots"] if not want or s["no"] in want]
    if not targets:
        raise ValueError("所选分镜不存在")
    jobs, warnings = [], []
    for s in targets:
        merged = {}
        for name, p in (params or {}).items():
            base = dict((s.get("params") or {}).get(name) or {})
            base.update(p or {})
            merged[name] = base
        for name in channels or []:
            if name not in merged:
                merged[name] = dict((s.get("params") or {}).get(name) or {})
        try:
            res = mgr.submit(
                trace_id=data["traceId"], item_id=data["itemId"],
                project_id=data["projectId"], shot_no=s["no"], kind=kind,
                channels=channels, prompt=s["prompt"], line=s["line"], sec=s["sec"],
                params=merged, out_dir=_shot_out_dir(kn, data["traceId"], data["projectId"],
                                                          data["itemId"], kind),
                vault_dir=kn.vault_dir)
        except ValueError as e:
            raise ValueError(f"分镜 {s['no']}：{e}")
        jobs.extend(res["jobs"])
        warnings.extend(f"分镜{s['no']}：{w}" for w in res["warnings"])
    if jobs:
        kn.emit("media-jobs", data["traceId"],
                  {"itemId": data["itemId"], "kind": kind, "channels": list(channels or []),
                   "count": len(jobs), "shots": [s["no"] for s in targets]})
    return {"ok": True, "jobs": jobs, "warnings": warnings,
            "traceId": data["traceId"], "itemId": data["itemId"]}


def factory_jobs(kn, trace_id, item_id=""):
    mgr = _media(kn)
    return {"jobs": mgr.jobs(str(trace_id or ""), str(item_id or ""), limit=400)}


def factory_cancel(kn, job_ids):
    mgr = _media(kn)
    res = mgr.cancel(job_ids)
    return {"ok": True, "results": res}


def factory_retry(kn, job_ids):
    mgr = _media(kn)
    res = mgr.retry(job_ids)
    return {"ok": True, "results": res}


# ---- 三环节手工多次启动：排队串行下发，不阻塞 HTTP
def _stage_lane(kn):
    if kn._stage_queue is None:
        kn._stage_queue = queue.Queue()
        kn._stage_thread = threading.Thread(
            target=_stage_lane_loop, args=(kn,), name=f"stage-lane-{id(kn) % 10000}", daemon=True)
        kn._stage_thread.start()
    return kn._stage_queue


def factory_stage_start(kn, trace_id, stage):
    stage = str(stage or "").strip()
    if stage not in STAGE_BUTTONS:
        raise ValueError(f"未知环节：{stage}（可选 {'/'.join(STAGE_BUTTONS)}）")
    if not kn.db:
        raise ValueError("过程库未启用（OPC_DB=0），无法手工启动环节")
    if not kn.db.pipeline_run(trace_id):
        raise ValueError("过程库里没有这条 trace 的运行记录")
    rec = {"id": uid("srun"), "traceId": trace_id, "stage": stage,
           "label": STAGE_BUTTONS[stage], "status": "queued",
           "queuedAt": iso_now(), "startedAt": "", "finishedAt": "", "note": ""}
    kn.stage_launches.append(rec)
    del kn.stage_launches[:-60]        # 只留最近 60 次手工启动记录
    _stage_queue_put(kn, rec)
    kn.emit("factory-stage", trace_id, dict(rec))
    return dict(rec)


def _stage_queue_put(kn, rec):
    _stage_lane(kn).put(rec)


def _stage_lane_loop(kn):
    q = kn._stage_queue
    while True:
        rec = q.get()
        try:
            _stage_lane_run(kn, rec)
        except Exception as e:
            rec.update(status="failed", finishedAt=iso_now(),
                       note=f"{type(e).__name__}: {e}")
            kn.emit("factory-stage", rec.get("traceId") or "", dict(rec))
        finally:
            q.task_done()


def _stage_lane_run(kn, rec):
    trace_id, stage = rec["traceId"], rec["stage"]
    deadline = time.time() + 3600
    # 手工多次启动允许排队：等当前这轮（含同一 trace）跑完再下一条，不覆盖别人
    while kn.active_trace and time.time() < deadline:
        time.sleep(3)
    rec.update(status="running", startedAt=iso_now(), note="已下发流水线")
    kn.emit("factory-stage", trace_id, dict(rec))
    info = _resend_stage(kn, trace_id, stage)
    rec.update(status="done", finishedAt=iso_now(),
               note=f"{info.get('note') or '已下发'}（断点 {info.get('from')}）")
    kn.emit("factory-stage", trace_id, dict(rec))


def factory_stage_runs(kn, trace_id):
    runs = [dict(r) for r in kn.stage_launches if r.get("traceId") == trace_id]
    current = ""
    if kn.db:
        try:
            row = kn.db.pipeline_run(trace_id)
            current = (row or {}).get("stage") or ""
        except Exception:
            current = ""
    return {"runs": list(reversed(runs)), "current": current,
            "activeTrace": kn.active_trace,
            "stages": [{"stage": k, "label": v} for k, v in STAGE_BUTTONS.items()]}


