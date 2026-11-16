"""流水线无状态辅助（C1 第六刀）：归一化/评审映射/审批摘要/技术门/车间事件。

从 kernel.Kernel 抽出，全部函数以 Kernel 实例为第一参数（staticmethod 除外）。
这些函数只吃 item/dict 与实例属性，不读 PipelineContext，因此可以先于阶段本体独立。
不 import kernel，kernel 单向依赖本模块（薄委托），不成环。
"""
import hashlib
import json
import os
import re
import subprocess

import company_config as CC
import message_feeds as FEED
from hermes_adapter import HERMES
from utils import _to_int


def _deg(kn, aid, what):
    return kn.fallback(aid, what)


def _coerce_cco_topics(kn, data, kb):
    """CCO 知识库选题卡：每项目恰好 2 条（共 6 条）；缺项用知识条目兜底，绝不从热点编造。"""
    raw = (data or {}).get("final") or (data or {}).get("topics") or []
    out = {pid: [] for pid in kn.PIDS}
    if isinstance(raw, list):
        for t in raw:
            if not isinstance(t, dict):
                continue
            pid = str(t.get("projectId") or "")
            title = str(t.get("title") or t.get("angle") or "").strip()
            if pid not in out or not title or len(out[pid]) >= 2:
                continue
            out[pid].append({"projectId": pid, "title": title[:60],
                             "angle": str(t.get("angle") or title)[:80],
                             "form": str(t.get("form") or "内化提炼")[:16],
                             "durationSec": _to_int(t.get("durationSec"), 45),
                             "reason": str(t.get("reason") or "")[:140],
                             "knowledgeRefs": [str(x)[:60] for x in
                                               (t.get("knowledgeRefs") or [])][:3]})
    for pid in kn.PIDS:
        while len(out[pid]) < 2:
            used = {t["title"] for pid2 in kn.PIDS for t in out[pid2]}
            cand = [e for e in kb["entries"]
                    if e.get("projectId") in (pid, "", "all") and e["title"] not in used]
            cand = cand or [e for e in kb["entries"] if e["title"] not in used]
            if not cand:
                break
            e = cand[0]
            out[pid].append({"projectId": pid, "title": e["title"][:60],
                             "angle": e.get("summary") or e["title"], "form": "内化提炼",
                             "durationSec": 45,
                             "reason": f"知识库条目兜底（{e.get('at') or '—'}）",
                             "knowledgeRefs": [e["title"]]})
        out[pid] = out[pid][:2]
    return out


def _coerce_plan(kn, data, topic, idx):
    raw = (data or {}).get("assessment")
    if isinstance(raw, dict) and isinstance(raw.get("items"), list):
        raw = raw["items"]
    items = raw if isinstance(raw, list) else []
    src = items[idx] if idx < len(items) and isinstance(items[idx], dict) else {}
    tts = src.get("tts") if isinstance(src.get("tts"), dict) else {}
    shots = []
    for s in (src.get("storyboardShots") or [])[:8]:
        if isinstance(s, dict) and (s.get("画面") or s.get("scene")):
            shots.append({"no": _to_int(s.get("no"), len(shots) + 1),
                          "画面": str(s.get("画面") or s.get("scene"))[:120],
                          "台词": str(s.get("台词") or s.get("line") or "")[:120],
                          "秒": _to_int(s.get("秒") or s.get("sec"), 6),
                          "素材": str(s.get("素材") or s.get("asset") or "")[:60]})
    vtasks = [{"shot": str(t.get("shot") or i + 1),
               "task": str(t.get("task") or "")[:120]}
              for i, t in list(enumerate(src.get("videoTasks") or []))[:12]
              if isinstance(t, dict)]
    return {"durationSec": _to_int(src.get("durationSec"), topic.get("durationSec", 45)),
            "tts": {"voice": str(tts.get("voice") or "zh-CN-XiaoxiaoNeural")[:40],
                    "style": str(tts.get("style") or "纪录片口播")[:40]},
            "materials": [str(x)[:60] for x in (src.get("materials") or [])][:8],
            "copyBrief": str(src.get("copyBrief") or "按定稿选题写口播稿")[:200],
            "storyboardNotes": str(src.get("storyboardNotes") or "4-6 镜，首镜 3 秒抓人")[:300],
            "storyboardShots": shots, "videoTasks": vtasks}


def _review_map(kn, data, ids):
    """样片评审：三票制；评审结果缺失按未通过处理（基础设施故障不得放行）。"""
    raw = (data or {}).get("reviews")
    out = {}
    if isinstance(raw, list):
        for r in raw:
            if not isinstance(r, dict):
                continue
            iid = str(r.get("itemId") or "")
            if iid in ids:
                out[iid] = (bool(r.get("pass")),
                            [str(x)[:100] for x in (r.get("reasons") or [])][:5])
    for iid in ids:
        out.setdefault(iid, (False, ["评审结果未返回，按未通过处理"]))
    return out


def _sample_wave(kn, trace, ids, items):
    """CCO、CTO 各评审全部样片；PM 只评审本项目样片。"""
    if not ids:
        return {}, {}
    payload = [{"itemId": i, "projectId": items[i]["projectId"],
                "title": items[i].get("title"), "copy": items[i].get("copy"),
                "storyboard": items[i].get("storyboard"),
                "durationSec": items[i].get("durationSec"),
                "render": items[i].get("renderCheck")} for i in ids]
    ctx = json.dumps(payload, ensure_ascii=False)
    extra = ("逐条判定并输出 reviews 数组：[{itemId, pass(布尔), reasons(数组≤2条)}]。"
             "只有创意完成度/调性/技术可交付性明显不达标才判 false。")
    schema = "summary, evidence, confidence, tools, reviews"
    jobs = {
        "cco": HERMES.pool.submit(kn.call_agent, "cco",
                                  "以 CCO 身份评审样片：项目调性、创意完成度、人设是否串味。", ctx, extra, schema),
        "cto": HERMES.pool.submit(kn.call_agent, "cto",
                                  "以 CTO 身份评审样片：分镜可执行性、时长、配音/TTS 与素材风险。", ctx, extra, schema),
    }
    for pid in kn.PIDS:
        pid_ids = [i for i in ids if items[i]["projectId"] == pid]
        if pid_ids:
            p_ctx = json.dumps([items[i] for i in pid_ids], ensure_ascii=False)
            jobs[pid] = HERMES.pool.submit(
                kn.call_agent, f"pm_{pid}",
                f"以 {kn.projects[pid]['name']} 产品经理身份评审本项目样片，判断能否进入定版。",
                p_ctx, extra, schema)
    maps, metas = {}, {}
    for who, job in jobs.items():
        data, meta = job.result()
        data = data or {}
        ids_for_role = ids if who in ("cco", "cto") else \
            [i for i in ids if items[i]["projectId"] == who]
        maps[who] = _review_map(kn, data, ids_for_role)
        metas[who] = meta
        kn.send("result", who if who in ("cco", "cto") else f"pm_{who}", "ceo", trace,
                  {"verdict": {"role": who, "reviews": maps[who]}, "engine": meta["engine"]},
                  project="all" if who in ("cco", "cto") else who)
    return maps, metas


def _production_checks(kn, items):
    """开工审批单的成本/素材/合规摘要：调用量按编排静态估算，不伪造金额。"""
    n = len(items)
    return {
        "cost": {
            "modelCallsTypical": 22 + 4 * n,
            "modelCallsWorst": 54 + 3 * n,
            "renders": n,
            "ttsSegments": [4 * n, 6 * n],
            "cashUsd": None,
            "budgetCapUsd": None,
            "note": "渲染/TTS 走本地链路（现金 0）；模型调用按 provider 计费，用量未回传，无法核算",
        },
        "materials": {
            "source": "本地 SVG 插图 + 本地 CosyVoice2 配音",
            "externalAssets": 0,
            "licenseRisk": "无外部素材",
            "assetGapsPending": True,
        },
        "compliance": {
            "toneIsolation": True,
            "projectBinding": True,
            "sourcesRequired": False,
            "reviewerGate": "定版后终审",
        },
    }


def _publish_checks(kn, items, passed_items, review_history):
    """发布审批单的成本/素材/合规摘要。"""
    n = len(items)
    total_assets = sum(len(i.get("assets") or []) for i in passed_items.values())
    rounds = len({h.get("round") for h in review_history}) or 0
    return {
        "cost": {
            "modelCallsTypical": 22 + 4 * n,
            "renders": n,
            "cashUsd": None,
            "budgetCapUsd": None,
            "note": "本轮未使用付费素材/生成接口；模型金额未回传，无法核算",
        },
        "materials": {
            "assets": total_assets,
            "externalAssets": 0,
            "licenseRisk": "无外部素材",
        },
        "compliance": {
            "reviewPassed": len(passed_items),
            "blocked": n - len(passed_items),
            "reviewRounds": rounds,
            "toneIsolation": True,
            "reviewerGate": "已通过",
        },
    }


def _workshop_start(kn, trace, st):
    """阶段开始：车间工位标记 running（过程库关闭时静默跳过）。"""
    if not kn.db:
        return
    kn.db.workshop_step_start(trace, st.id, st.name, st.om_stage)
    try:
        FEED.factory_step(kn.db, kn.emit, trace, kn._trace_title(trace),
                          st.name, "running", kn._factory_products(trace, st.id))
    except Exception as e:
        print(f"[msgfeed] 车间开工记录失败：{type(e).__name__}: {e}", flush=True)


def _workshop_end(kn, trace, st, status, ctx=None):
    """阶段结束：车间工位落定 done/stopped/failed，摘要随行供前端回放。"""
    if not kn.db:
        return
    kn.db.workshop_step_end(trace, st.id, status, {
        "stage": st.id, "name": st.name, "kind": st.kind, "omStage": st.om_stage,
        "items": len(getattr(ctx, "items", ()) or ()),
    })
    # 车间流水线动态（消息中心 factory 源）：本工位收工 + 每件产品推进的工序
    try:
        FEED.factory_step(kn.db, kn.emit, trace, kn._trace_title(trace),
                          st.name, status, kn._factory_products(trace, st.id))
    except Exception as e:
        print(f"[msgfeed] 车间收工记录失败：{type(e).__name__}: {e}", flush=True)


def _content_id(trace, pid, idx, title):
    """内容单稳定 id：同一 trace 下同样一条选题，重跑也得到同一个 id。"""
    raw = f"{trace}|{pid}|{idx}|{title}".encode("utf-8")
    return "c_" + hashlib.sha1(raw).hexdigest()[:12]


def _tech_gate(kn, item):
    """发布前的技术硬门槛：对实际落盘的成片重新 probe，不达标即不得 PASS。

    制度要求「导出文件必须重新 probe 才算验收」，本函数是这条制度的执行点：
    分辨率/帧率/编码/像素格式/音轨/采样率/声道/时长/黑帧/削波/静音，逐项实测。
    """
    import subprocess
    path = ""
    # 复检对象优先取"定版母版"（05-final.mp4），其次取样片
    cands = list(item.get("finalAssets") or []) + [((item.get("files") or {}).get("final") or "")]
    cands += [a for a in reversed(item.get("assets") or []) if str(a).lower().endswith(".mp4")]
    for a in cands:
        if not a:
            continue
        cand = kn.DL._local(a) if (kn.DL and str(a).startswith("/vault/")) else str(a)
        if os.path.isfile(cand):
            path = cand
            break
    if not path or not os.path.isfile(path):
        return {"pass": False, "reasons": ["缺少可复检的成片文件"], "path": path}
    env = dict(os.environ)
    env.pop("LD_LIBRARY_PATH", None)
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json", path],
            capture_output=True, text=True, timeout=120, env=env).stdout
        info = json.loads(out or "{}")
    except Exception as e:
        return {"pass": False, "reasons": [f"ffprobe 失败：{type(e).__name__}"], "path": path}
    streams = info.get("streams") or []
    v = next((s for s in streams if s.get("codec_type") == "video"), None)
    au = next((s for s in streams if s.get("codec_type") == "audio"), None)
    pid = item.get("projectId") or ""
    spec = CC.DELIVERY.get(pid) or CC.DELIVERY.get("_default") or CC.DEFAULT_DELIVERY["_default"]
    checks_spec = {"projectId": pid, "source": "项目配置.md#交付合同" if pid in CC.DELIVERY else "内置默认",
                   "width": spec["width"], "height": spec["height"], "fps": spec["fps"]}
    reasons, checks = [], {"spec": checks_spec}
    if not v:
        reasons.append("没有视频流")
    if not au:
        reasons.append("没有音轨（旁白缺失）")
    if v:
        checks.update(width=v.get("width"), height=v.get("height"),
                      fps=v.get("r_frame_rate"), codec=v.get("codec_name"),
                      pix_fmt=v.get("pix_fmt"), duration=v.get("duration"))
        if (v.get("width"), v.get("height")) != (spec["width"], spec["height"]):
            reasons.append(f"分辨率 {v.get('width')}x{v.get('height')}，"
                           f"交付合同要求 {spec['width']}x{spec['height']}")
        if str(v.get("r_frame_rate")) != spec["fps"]:
            reasons.append(f"帧率 {v.get('r_frame_rate')}，交付合同要求 {spec['fps']}")
        if v.get("codec_name") != spec["video_codec"]:
            reasons.append(f"视频编码 {v.get('codec_name')}，交付合同要求 {spec['video_codec']}")
        if v.get("pix_fmt") not in spec["pix_fmt"]:
            reasons.append(f"像素格式 {v.get('pix_fmt')}，交付合同要求 {'/'.join(spec['pix_fmt'])}")
    if au:
        checks.update(sample_rate=au.get("sample_rate"), channels=au.get("channels"),
                      audio_codec=au.get("codec_name"))
        if str(au.get("sample_rate")) != str(spec["sample_rate"]):
            reasons.append(f"采样率 {au.get('sample_rate')}，交付合同要求 {spec['sample_rate']}")
        if _to_int(au.get("channels"), 0) != spec["channels"]:
            reasons.append(f"声道数 {au.get('channels')}，交付合同要求 {spec['channels']}")
        if au.get("codec_name") != spec["audio_codec"]:
            reasons.append(f"音频编码 {au.get('codec_name')}，交付合同要求 {spec['audio_codec']}")
    try:
        dur = float((info.get("format") or {}).get("duration") or 0)
    except (TypeError, ValueError):
        dur = 0.0
    checks["duration"] = round(dur, 3)
    want = float(item.get("durationSec") or 0)
    if want and abs(dur - want) > spec["duration_tolerance"]:
        reasons.append(f"时长 {dur:.2f}s 与分镜 {want:.0f}s 偏差超过 "
                       f"{spec['duration_tolerance']}s")
    # 黑帧与音量：只读输入，不产生新文件
    try:
        bd = subprocess.run(
            ["ffmpeg", "-hide_banner", "-nostats", "-i", path, "-vf",
             "blackdetect=d=0.5:pix_th=0.10", "-an", "-f", "null", "-"],
            capture_output=True, text=True, timeout=300, env=env).stderr
        checks["blackFrames"] = bd.count("black_start")
        if checks["blackFrames"]:
            reasons.append(f"检出 {checks['blackFrames']} 段黑帧（>0.5s）")
    except Exception as e:
        reasons.append(f"黑帧检测失败：{type(e).__name__}")
    try:
        vd = subprocess.run(
            ["ffmpeg", "-hide_banner", "-nostats", "-i", path, "-af", "volumedetect",
             "-vn", "-f", "null", "-"],
            capture_output=True, text=True, timeout=300, env=env).stderr
        mx = re.search(r"max_volume:\s*(-?[\d.]+) dB", vd)
        mn = re.search(r"mean_volume:\s*(-?[\d.]+) dB", vd)
        if mx:
            checks["maxVolumeDb"] = float(mx.group(1))
            if checks["maxVolumeDb"] > spec["max_peak_db"]:
                reasons.append(f"峰值 {checks['maxVolumeDb']} dB，超过交付合同上限 "
                               f"{spec['max_peak_db']} dB")
        if mn:
            checks["meanVolumeDb"] = float(mn.group(1))
            if checks["meanVolumeDb"] <= spec["min_mean_db"]:
                reasons.append(f"音轨近乎静音（均值 {checks['meanVolumeDb']} dB，"
                               f"下限 {spec['min_mean_db']} dB）")
    except Exception as e:
        reasons.append(f"音量检测失败：{type(e).__name__}")
    checks["path"] = path
    return {"pass": not reasons, "reasons": reasons, "checks": checks}


