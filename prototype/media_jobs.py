#!/usr/bin/env python3
"""分镜级多渠道媒体生成作业（图片 / 视频 / 音频）：并行、进度、启停、重试。

设计约定（对应制片工厂「素材制作 / 剪辑合成 / 终版合成」的人工多次启动）：
  - 一个作业 = (trace, 内容单, 分镜号, 类型, 渠道)；同一提示词可同时投多个渠道，
    每个渠道一行作业，互不影响，可分别看进度、停、重来。
  - 渠道注册表 CHANNELS 是唯一事实源：类型（image/video/audio）、端点、参数规格、
    并发额度、时长上限都在这里；前端直接读注册表渲染表单，不在前端写死。
  - 视频时长上限：全局 OPC_SHOT_MAX_SEC（默认 6s）与渠道 maxSec 取小；WAN2.1 受
    num_frames(4k+1, ≤81)/fps 反推（81 帧 @16fps = 5.06s）。超限一律夹到上限并把
    原因写进作业 note，不静默改写。
  - 启停：cancel 置标记并尽力中断（ComfyUI 走 /interrupt；media-gen / TTS 无取消
    接口，只停本地轮询，远端作业自然跑完，结果弃用）。
  - 失败重来：retry 用原参数新建一行作业（run_no+1），原作业保留为历史。
  - 存储：独立 SQLite（默认 <工作区>/media_jobs.db，WAL），不与过程库 opc.db 抢写锁。
  - 只做「生成 + 取回」，不写业务状态；状态落库、事件由调用方（kernel）负责。
"""
import json
import os
import re
import shutil
import sqlite3
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
TZ = timezone(timedelta(hours=8))
# 个人空间根：落在 <USERS_DIR>/<手机号>/ 下的成品一律走 /private 私有前缀
USERS_DIR = os.environ.get("OPC_USERS_DIR", "/mnt/sefs/conring/users")

MEDIAGEN = os.environ.get("OPC_MEDIAGEN_URL", "http://192.168.21.206:9103").rstrip("/")
MEDIAENGINE = os.environ.get("OPC_MEDIAENGINE_URL", "http://192.168.21.206:9200").rstrip("/")
SHOT_MAX_SEC = float(os.environ.get("OPC_SHOT_MAX_SEC", "6"))
MAX_WORKERS = int(os.environ.get("OPC_MEDIA_WORKERS", "6"))
PROBE_TTL = 30.0

KINDS = ("image", "video", "audio")
LANE_LIMITS = {"comfyui": 1, "sd15": 1, "wan21": 1, "minimax_h3": 1, "qwen": 2, "cosyvoice": 2}


# ---------------------------------------------------------------- 基础工具
def now_iso():
    return datetime.now(TZ).isoformat(timespec="seconds")


def _log(msg):
    print(f"[media-jobs] {msg}", flush=True)


def _p(key, label, typ, default, **kw):
    spec = {"key": key, "label": label, "type": typ, "default": default}
    spec.update(kw)
    return spec


def _http(url, timeout=15, data=None, method=None, headers=None):
    req = urllib.request.Request(url, data=data, method=method,
                                 headers=headers or {})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def _http_json(url, timeout=15, data=None, method=None):
    body = None
    headers = {}
    if data is not None:
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    raw = _http(url, timeout=timeout, data=body, method=method, headers=headers)
    return json.loads(raw.decode("utf-8")) if raw else {}


# ---------------------------------------------------------------- 渠道注册表
CHANNELS = {
    "comfyui": {
        "kind": "image", "label": "ComfyUI · 本地出图",
        "notes": "ComfyUI 工作流文生图（端点读 comfyui-ops/config.json），单队列串行",
        "params": [
            _p("width", "宽", "int", 768, min=256, max=1536, step=8),
            _p("height", "高", "int", 1344, min=256, max=1536, step=8),
            _p("steps", "采样步数", "int", 20, min=1, max=50),
            _p("cfg", "提示词权重", "float", 6.5, min=1, max=20, step=0.5),
            _p("negative", "反向提示词", "text", "低质量, 模糊, 水印, 变形"),
        ],
    },
    "sd15": {
        "kind": "image", "label": "media-gen · SD1.5 写实",
        "notes": "media-gen SD15（Realistic Vision V5.1），CPU 串行队列",
        "params": [
            _p("width", "宽", "int", 640, min=256, max=1024, step=8, multiple=8),
            _p("height", "高", "int", 832, min=256, max=1024, step=8, multiple=8),
            _p("steps", "采样步数", "int", 20, min=1, max=50),
            _p("cfg", "提示词权重", "float", 6.0, min=1, max=20, step=0.5),
            _p("negative_prompt", "反向提示词", "text", "低质量, 模糊, 水印"),
        ],
    },
    "wan21": {
        "kind": "video", "label": "WAN2.1 · 文生视频",
        "notes": "WAN2.1-T2V-1.3B（480P），时长由 num_frames/fps 决定，最长 81 帧",
        "max_sec": 5.0,
        "params": [
            _p("width", "宽", "int", 832, min=256, max=1280, step=16, multiple=16),
            _p("height", "高", "int", 480, min=256, max=1280, step=16, multiple=16),
            _p("fps", "帧率", "int", 16, min=8, max=24),
            _p("steps", "采样步数", "int", 20, min=1, max=50),
            _p("cfg", "提示词权重", "float", 5.0, min=1, max=20, step=0.5),
            _p("negative_prompt", "反向提示词", "text", "低质量, 模糊, 水印, 变形"),
        ],
    },
    "minimax_h3": {
        "kind": "video", "label": "MiniMax H3 · 文生视频",
        "notes": "MiniMax H3 开源权重（本机未接入，选择后会明确失败，不静默降级）",
        "max_sec": 6.0,
        "params": [
            _p("width", "宽", "int", 832, min=256, max=1280, step=16, multiple=16),
            _p("height", "高", "int", 480, min=256, max=1280, step=16, multiple=16),
            _p("fps", "帧率", "int", 16, min=8, max=24),
            _p("steps", "采样步数", "int", 20, min=1, max=50),
            _p("cfg", "提示词权重", "float", 5.0, min=1, max=20, step=0.5),
            _p("negative_prompt", "反向提示词", "text", "低质量, 模糊, 水印"),
        ],
    },
    "qwen": {
        "kind": "audio", "label": "Qwen3-TTS · 千问",
        "notes": "media-engine /v1/tts engine=qwen3（离线高质量，同步返回）",
        "params": [
            _p("speaker", "音色", "select", "Vivian",
               options=["Vivian", "Serena", "Uncle_Fu", "Dylan", "Eric", "Ryan",
                        "Aiden", "Ono_Anna", "Sohee"]),
            _p("language", "语言", "text", "zh"),
            _p("instruct", "情绪指令", "text", ""),
            _p("speed", "语速", "float", 1.0, min=0.5, max=2.0, step=0.1),
        ],
    },
    "cosyvoice": {
        "kind": "audio", "label": "CosyVoice · 实时",
        "notes": "media-engine /v1/tts engine=cosyvoice（实时、多 worker）",
        "params": [
            _p("voice", "音色", "select", "female-zh-real",
               options=["female-zh-real", "female-zh", "female-zh-short",
                        "m01", "m02", "m03", "f01", "f02", "f03",
                        "backup-zh-cosy2"]),
            _p("speed", "语速", "float", 1.0, min=0.5, max=2.0, step=0.1),
        ],
    },
}

_PROBE = {"at": 0.0, "data": {}}
_PROBE_LOCK = threading.Lock()


_CN_NUM = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}
_SHOT_HEAD = re.compile(
    r"镜(?:头)?\s*([一二三四五六七八九十\d]{1,3})\s*[（(]?\s*"
    r"(?:(\d+(?:\.\d+)?)\s*[-–—~至]\s*(\d+(?:\.\d+)?)\s*s|(\d+(?:\.\d+)?)\s*s)?\s*[）)]?\s*"
    r"(?:[（(]\s*(?:\d+(?:\.\d+)?\s*s)?\s*[）)])?\s*[：:，,]?")
# 无「镜」字、直接用序号起行：「1｜0-6s …」「2) 手机怼脸…」「3·8s｜…」
_NUM_HEAD = re.compile(
    r"(?:^|(?<=[\n。；;]))\s*(\d{1,2})\s*[)）.、·:：|｜]\s*"
    r"(?:(\d+(?:\.\d+)?)\s*[-–—~至]\s*(\d+(?:\.\d+)?)\s*s|(\d+(?:\.\d+)?)\s*s)?")
# 连序号都没有，只有时间码：「0–4s …；4–14s …」
_TIME_HEAD = re.compile(
    r"(?:^|(?<=[\n。；;]))\s*(\d+(?:\.\d+)?)\s*[-–—~]\s*(\d+(?:\.\d+)?)\s*s")
_LINE_LABEL = re.compile(r"(?:台词|口播|旁白|配音)\s*[:：]")
_VIS_LABEL = re.compile(r"(?:画面|视觉|镜内)\s*[:：]")
_ASSET_LABEL = re.compile(r"插图\s*[:：]?\s*")
_TRIM = " \t\r\n／/｜|；;。、,，"


def _cn_num(tok):
    if tok.isdigit():
        return int(tok)
    if tok in _CN_NUM:
        return _CN_NUM[tok]
    if len(tok) == 2 and tok[0] == "十":
        return 10 + _CN_NUM.get(tok[1], 0)
    if len(tok) == 2 and tok[1] == "十":
        return _CN_NUM.get(tok[0], 0) * 10
    return None


def split_storyboard(text, total_sec=0):
    """把模型写的整段分镜按「镜一：／镜2 0–8s／镜3（8-17s）」切成清单。

    真实分镜文本一行到底、镜号写法五花八门（中文数字、时间码、括号），
    DL.normalize_storyboard 只按行切，所以这里单独做一个宽松切分器：
    依次尝试「镜N …」「N｜…」「N-Ms …」三种写法，取切出镜数最多的一种，
    切出每镜 画面（提示词）/ 台词（音频用）/ 时长 / 插图标注。
    """
    raw = str(text or "").strip()
    if not raw:
        return []
    heads = []
    is_time = False
    for pat in (_SHOT_HEAD, _NUM_HEAD, _TIME_HEAD):
        found = list(pat.finditer(raw))
        if len(found) >= 2:
            heads, is_time = found, pat is _TIME_HEAD
            break
    if len(heads) < 2:
        return []
    out = []
    for idx, m in enumerate(heads):
        end = heads[idx + 1].start() if idx + 1 < len(heads) else len(raw)
        seg = raw[m.end():end]
        if is_time:
            no = idx + 1
            sec = round(float(m.group(2)) - float(m.group(1)), 2)
        elif m.group(2) and m.group(3):
            no = _cn_num(m.group(1)) or (idx + 1)
            sec = round(float(m.group(3)) - float(m.group(2)), 2)
        else:
            no = _cn_num(m.group(1)) or (idx + 1)
            sec = round(float(m.group(4)), 2) if m.group(4) else 0.0
        line = ""
        lm = _LINE_LABEL.search(seg)
        if lm:
            line = seg[lm.end():]
            seg = seg[:lm.start()]
            am = _ASSET_LABEL.search(line)
            if am:
                line = line[:am.start()]
        asset = ""
        am = _ASSET_LABEL.search(seg)
        if am:
            asset = seg[am.end():]
            seg = seg[:am.start()]
        vm = _VIS_LABEL.search(seg)
        if vm:
            seg = seg[vm.end():]
        if not line:
            # 没写「台词：」的，把引号里那句完整的话当口播（标签式短词如「只读」不算）
            qm = re.search(r"[「“]([^」”]{6,})[」”]", seg)
            if qm and (len(qm.group(1)) >= 8 or qm.group(1)[-1:] in "。！？…"):
                line = qm.group(1)
                seg = (seg[:qm.start()] + seg[qm.end():])
        if not line:
            # 「画面｜台词」用竖线分段的，后半段当口播
            parts = [p.strip(_TRIM) for p in re.split(r"[｜|]", seg)]
            if len(parts) >= 2 and len(parts[-1]) >= 6 and "插图" not in parts[-1]:
                line = parts[-1]
                seg = "｜".join(parts[:-1])
        for cut in ("）", ")", "，淡入", "。"):
            asset = asset.split(cut)[0]
        out.append({"no": no,
                    "prompt": seg.strip(_TRIM + "（("), "line": line.strip(_TRIM),
                    "sec": sec, "asset": asset.strip(_TRIM + "（(")})
    # 没有逐镜时间码的，按总时长均分
    if total_sec and all(not s["sec"] for s in out):
        each = round(float(total_sec) / len(out), 2)
        for s in out:
            s["sec"] = each
    return out


def _probe_media_gen():
    try:
        d = _http_json(MEDIAGEN + "/v1/backends", timeout=4)
        out = {}
        for b in d.get("backends") or []:
            out[b.get("name")] = {"available": bool(b.get("available")),
                                  "reason": b.get("reason") or "",
                                  "loaded": bool(b.get("loaded"))}
        return out
    except Exception as e:
        why = f"media-gen 不可达（{MEDIAGEN}）：{type(e).__name__}: {e}"
        return {k: {"available": False, "reason": why}
                for k in ("sd15", "wan21", "minimax_h3")}


def _probe_comfyui():
    try:
        import asset_fill
        url = asset_fill._comfy_image_url()
        return {"available": True, "reason": "", "endpoint": url}
    except Exception as e:
        return {"available": False, "reason": f"ComfyUI 不可用：{type(e).__name__}: {e}"}


def _probe_media_engine():
    try:
        d = _http_json(MEDIAENGINE + "/health", timeout=4)
        ok = str(d.get("status") or "").lower() in ("ok", "success", "healthy")
        reason = "" if ok else "media-engine 健康检查未通过"
        return {"available": ok, "reason": reason}
    except Exception as e:
        return {"available": False,
                "reason": f"media-engine 不可达（{MEDIAENGINE}）：{type(e).__name__}: {e}"}


def channels(force=False):
    """渠道视图：参数规格 + 实时可用性（探测结果缓存 30s）。"""
    with _PROBE_LOCK:
        if force or time.time() - _PROBE["at"] > PROBE_TTL or not _PROBE["data"]:
            mg = _probe_media_gen()
            comfy = _probe_comfyui()
            me = _probe_media_engine()
            _PROBE["data"] = {
                "comfyui": comfy,
                "sd15": mg.get("sd15") or {"available": False, "reason": "media-gen 未登记 sd15"},
                "wan21": mg.get("wan21") or {"available": False, "reason": "media-gen 未登记 wan21"},
                "minimax_h3": mg.get("minimax_h3") or {"available": False,
                                                       "reason": "media-gen 未登记 minimax_h3"},
                "qwen": me, "cosyvoice": dict(me),
            }
            _PROBE["at"] = time.time()
        probe = dict(_PROBE["data"])
    out = []
    for name, ch in CHANNELS.items():
        st = probe.get(name) or {}
        cap = ch.get("max_sec")
        out.append({
            "name": name, "kind": ch["kind"], "label": ch["label"], "notes": ch["notes"],
            "maxSec": min([x for x in (cap, SHOT_MAX_SEC) if x]) if ch["kind"] == "video" else None,
            "concurrency": LANE_LIMITS.get(name, 1),
            "available": bool(st.get("available")), "reason": st.get("reason") or "",
            "params": ch["params"],
        })
    return out, SHOT_MAX_SEC


def channel(name):
    return CHANNELS.get(str(name or ""))


# ---------------------------------------------------------------- 参数与时长
def _coerce_params(ch, raw):
    raw = raw if isinstance(raw, dict) else {}
    out = {}
    for spec in ch["params"]:
        key, typ = spec["key"], spec["type"]
        val = raw.get(key, spec.get("default"))
        try:
            if typ == "int":
                val = int(float(val))
                if spec.get("min") is not None:
                    val = max(spec["min"], val)
                if spec.get("max") is not None:
                    val = min(spec["max"], val)
                mult = int(spec.get("multiple") or 0)
                if mult > 1:  # 宽高需对齐（SD 8 的倍数、WAN 16 的倍数），就近取整
                    val = max(mult, int(round(val / mult)) * mult)
                    if spec.get("max") is not None:
                        val = min(int(spec["max"]) // mult * mult, val)
            elif typ == "float":
                val = float(val)
                if spec.get("min") is not None:
                    val = max(spec["min"], val)
                if spec.get("max") is not None:
                    val = min(spec["max"], val)
                val = round(val, 2)
            elif typ == "select":
                opts = spec.get("options") or []
                val = str(val)
                if opts and val not in opts:
                    val = spec.get("default") or opts[0]
            else:
                val = str(val if val is not None else "")
        except (TypeError, ValueError):
            val = spec.get("default")
        out[key] = val
    return out


def _frames_for(sec, fps, cap):
    """把秒数换算成 WAN 可接受的 4k+1 帧数（≤81），并受 cap 约束。"""
    fps = max(8, min(int(fps or 16), 24))
    max_frames = 81
    cap_frames = int(round(float(cap) * fps))
    if cap_frames > 0:
        max_frames = min(81, ((max(5, cap_frames) - 1) // 4) * 4 + 1)
    want = int(round(float(sec or 0) * fps))
    want = max(5, min(max_frames, want))
    return ((want - 1) // 4) * 4 + 1, fps


def plan_duration(ch_name, ch, params, sec):
    """返回 (实际时长秒, params 补丁, 夹取说明)。仅视频渠道有时长概念。"""
    if ch["kind"] != "video":
        return 0.0, {}, ""
    cap = min([x for x in (ch.get("max_sec"), SHOT_MAX_SEC) if x])
    want = float(sec or 0) or cap
    over = want > cap + 1e-6
    capped = min(want, cap)
    if ch_name in ("wan21", "minimax_h3"):
        frames, fps = _frames_for(capped, params.get("fps") or 16, cap)
        actual = round(frames / fps, 2)
        if over:
            note = f"分镜时长 {want:.1f}s 超过 {ch_name} 上限 {cap:.1f}s，已夹到 {actual:.2f}s"
        elif want - actual >= 0.5:  # 明显被帧数约束拉低才提示，避免噪音
            note = f"按帧数约束（{frames} 帧 @{fps}fps）实际 {actual:.2f}s"
        else:
            note = ""
        return actual, {"num_frames": frames, "fps": fps}, note
    if over:
        return round(capped, 2), {}, f"分镜时长 {want:.1f}s 超过 {ch_name} 上限 {cap:.1f}s，已夹到 {capped:.2f}s"
    return round(want, 2), {}, ""


# ---------------------------------------------------------------- 存储
SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
  id TEXT PRIMARY KEY, trace_id TEXT, item_id TEXT, project_id TEXT, shot_no INTEGER,
  kind TEXT, channel TEXT, prompt TEXT, params TEXT, status TEXT, progress INTEGER,
  output_path TEXT, output_url TEXT, duration_sec REAL, error TEXT, note TEXT,
  remote_id TEXT, out_dir TEXT, vault_dir TEXT, run_no INTEGER DEFAULT 1,
  created_at TEXT, started_at TEXT, finished_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_mj_trace ON jobs(trace_id, item_id, shot_no, created_at DESC);
CREATE TABLE IF NOT EXISTS shots (
  trace_id TEXT NOT NULL, item_id TEXT NOT NULL, shot_no INTEGER NOT NULL,
  prompt TEXT, line TEXT, sec REAL, channels TEXT, params TEXT, updated_at TEXT,
  PRIMARY KEY (trace_id, item_id, shot_no)
);
"""

JOB_FIELDS = ("id", "trace_id", "item_id", "project_id", "shot_no", "kind", "channel",
              "prompt", "params", "status", "progress", "output_path", "output_url",
              "duration_sec", "error", "note", "remote_id", "out_dir", "vault_dir",
              "run_no", "created_at", "started_at", "finished_at")


class Manager:
    """一个工作区一份作业库：提交/查询/停止/重试；并发与进度在进程内统一调度。"""

    def __init__(self, path):
        self.path = path
        self.lock = threading.Lock()
        self.notify = None
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        conn = self._conn()
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(SCHEMA)
            conn.commit()
        finally:
            conn.close()

    def _conn(self):
        conn = sqlite3.connect(self.path, timeout=10)
        conn.row_factory = sqlite3.Row
        return conn

    # ---- 读写（连接用完即关，避免长跑进程句柄泄漏）
    def _write(self, sql, params=()):
        try:
            conn = self._conn()
            try:
                conn.execute(sql, params)
                conn.commit()
            finally:
                conn.close()
        except sqlite3.Error as e:
            _log(f"写库失败 {sql[:50]}：{e}")

    def get(self, job_id):
        try:
            conn = self._conn()
            try:
                row = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
                return dict(row) if row else None
            finally:
                conn.close()
        except sqlite3.Error as e:
            _log(f"读作业失败：{e}")
            return None

    def jobs(self, trace_id, item_id="", shot_no=None, limit=400):
        sql = "SELECT * FROM jobs WHERE trace_id=?"
        args = [trace_id]
        if item_id:
            sql += " AND item_id=?"
            args.append(item_id)
        if shot_no is not None:
            sql += " AND shot_no=?"
            args.append(int(shot_no))
        sql += " ORDER BY created_at DESC, id DESC LIMIT ?"
        args.append(max(1, min(int(limit), 1000)))
        try:
            conn = self._conn()
            try:
                return [_view(dict(r)) for r in conn.execute(sql, args).fetchall()]
            finally:
                conn.close()
        except sqlite3.Error as e:
            _log(f"列表失败：{e}")
            return []

    def toolbox_jobs(self, limit=40):
        """工具箱手工任务：trace_id 以 tb- 开头，独立于流水线，按提交时间倒序。"""
        try:
            conn = self._conn()
            try:
                rows = conn.execute(
                    "SELECT * FROM jobs WHERE trace_id LIKE 'tb-%' "
                    "ORDER BY created_at DESC, id DESC LIMIT ?",
                    (max(1, min(int(limit), 200)),)).fetchall()
                return [_view(dict(r)) for r in rows]
            finally:
                conn.close()
        except sqlite3.Error as e:
            _log(f"工具箱列表失败：{e}")
            return []

    def forget_output(self, dest):
        """成品被删除后清掉指向它的产物字段。

        任务历史保留（谁在什么时候跑过什么还能看到），只是不再给出一个必然 404 的地址；
        dest 是目录时按前缀清空整棵子树。
        """
        dest = os.path.abspath(str(dest or "").strip())
        if not dest:
            return 0
        try:
            conn = self._conn()
            try:
                cur = conn.execute(
                    "UPDATE jobs SET output_path=NULL, output_url=NULL "
                    "WHERE output_path=? OR output_path LIKE ?",
                    (dest, dest + "/%"))
                conn.commit()
                return cur.rowcount or 0
            finally:
                conn.close()
        except sqlite3.Error as e:
            _log(f"清空产物记录失败：{e}")
            return 0

    def shots(self, trace_id, item_id):
        try:
            conn = self._conn()
            try:
                rows = conn.execute(
                    "SELECT * FROM shots WHERE trace_id=? AND item_id=?",
                    (trace_id, item_id)).fetchall()
                return {int(r["shot_no"]): dict(r) for r in rows}
            finally:
                conn.close()
        except sqlite3.Error as e:
            _log(f"读分镜覆盖失败：{e}")
            return {}

    def shot_save(self, trace_id, item_id, shot_no, fields):
        cur = self.shots(trace_id, item_id).get(int(shot_no)) or {}
        prompt = fields.get("prompt", cur.get("prompt")) or ""
        line = fields.get("line", cur.get("line")) or ""
        sec = fields.get("sec", cur.get("sec"))
        # 没传渠道/参数（或传 None）时保留上次选择，避免「只改提示词」把方案清空
        chans = fields.get("channels")
        if chans is None:
            chans = json.loads(cur.get("channels") or "[]")
        params = fields.get("params")
        if params is None:
            params = json.loads(cur.get("params") or "{}")
        rec = {"trace_id": trace_id, "item_id": item_id, "shot_no": int(shot_no),
               "prompt": prompt, "line": line, "sec": sec,
               "channels": json.dumps(chans or [], ensure_ascii=False),
               "params": json.dumps(params or {}, ensure_ascii=False),
               "updated_at": now_iso()}
        self._write("""INSERT INTO shots(trace_id,item_id,shot_no,prompt,line,sec,channels,params,updated_at)
                       VALUES(?,?,?,?,?,?,?,?,?)
                       ON CONFLICT(trace_id,item_id,shot_no) DO UPDATE SET
                         prompt=excluded.prompt, line=excluded.line, sec=excluded.sec,
                         channels=excluded.channels, params=excluded.params,
                         updated_at=excluded.updated_at""",
                    tuple(rec[k] for k in ("trace_id", "item_id", "shot_no", "prompt", "line",
                                           "sec", "channels", "params", "updated_at")))
        return self.shots(trace_id, item_id).get(int(shot_no)) or rec

    # ---- 作业状态
    def _set(self, job_id, **fields):
        if not fields:
            return
        keys = [k for k in fields if k in JOB_FIELDS]
        sql = "UPDATE jobs SET " + ", ".join(f"{k}=?" for k in keys) + " WHERE id=?"
        self._write(sql, tuple(fields[k] for k in keys) + (job_id,))

    def _emit(self, job_id, force=False):
        row = self.get(job_id)
        if row and self.notify:
            try:
                self.notify("media-job", row.get("trace_id"), _view(row))
            except Exception as e:
                _log(f"事件推送失败：{type(e).__name__}: {e}")
        return row

    # ---- 提交
    def submit(self, *, trace_id, item_id, project_id, shot_no, kind, channels,
               prompt, line="", sec=0.0, params=None, out_dir="", vault_dir=""):
        kind = str(kind or "").strip()
        if kind not in KINDS:
            raise ValueError(f"未知素材类型：{kind}（可选 {'/'.join(KINDS)}）")
        chans = [str(c) for c in (channels or []) if str(c).strip()]
        if not chans:
            raise ValueError("至少选择一个制作渠道")
        text = str(prompt if kind != "audio" else (line or prompt) or "").strip()
        if not text:
            raise ValueError("音频渠道需要「台词」，图片/视频渠道需要「提示词」")
        params_all = params if isinstance(params, dict) else {}
        jobs, warnings = [], []
        for name in chans:
            ch = CHANNELS.get(name)
            if not ch:
                warnings.append(f"未知渠道 {name}，已跳过")
                continue
            if ch["kind"] != kind:
                warnings.append(f"渠道 {name} 属于 {ch['kind']}，与所选类型 {kind} 不符，已跳过")
                continue
            p = _coerce_params(ch, params_all.get(name) or {})
            dur, patch, note = plan_duration(name, ch, p, sec)
            p.update(patch)
            jid = "mj_" + uuid.uuid4().hex[:12]
            row = {"id": jid, "trace_id": trace_id, "item_id": item_id,
                   "project_id": project_id or "", "shot_no": int(shot_no or 0),
                   "kind": kind, "channel": name, "prompt": text,
                   "params": json.dumps(p, ensure_ascii=False), "status": "queued",
                   "progress": 0, "output_path": "", "output_url": "", "duration_sec": dur,
                   "error": "", "note": note, "remote_id": "", "out_dir": out_dir or "",
                   "vault_dir": vault_dir or "", "run_no": 1,
                   "created_at": now_iso(), "started_at": "", "finished_at": ""}
            self._write(f"INSERT INTO jobs({','.join(JOB_FIELDS)}) "
                        f"VALUES({','.join('?' for _ in JOB_FIELDS)})",
                        tuple(row[k] for k in JOB_FIELDS))
            if note:
                warnings.append(f"{name}：{note}")
            _POOL.submit(_run_job, self, jid)
            jobs.append(_view(row))
        if not jobs and not warnings:
            raise ValueError("没有可提交的渠道")
        return {"jobs": jobs, "warnings": warnings}

    def cancel(self, job_ids):
        out = []
        for jid in job_ids or []:
            row = self.get(str(jid))
            if not row:
                out.append({"id": jid, "ok": False, "error": "作业不存在"})
                continue
            if row["status"] in ("done", "failed", "canceled"):
                out.append({"id": jid, "ok": False, "error": f"作业已{row['status']}，无需停止"})
                continue
            _CANCEL.add(str(jid))
            self._set(str(jid), status="canceled", progress=int(row.get("progress") or 0),
                      finished_at=now_iso(), error="老板手动停止")
            self._emit(str(jid))
            out.append({"id": jid, "ok": True, "status": "canceled"})
        return out

    def retry(self, job_ids):
        out = []
        for jid in job_ids or []:
            row = self.get(str(jid))
            if not row:
                out.append({"id": jid, "ok": False, "error": "作业不存在"})
                continue
            new_id = "mj_" + uuid.uuid4().hex[:12]
            clone = dict(row)
            clone.update(id=new_id, status="queued", progress=0, error="", note="",
                         remote_id="", output_path="", output_url="",
                         run_no=int(row.get("run_no") or 1) + 1,
                         created_at=now_iso(), started_at="", finished_at="")
            self._write(f"INSERT INTO jobs({','.join(JOB_FIELDS)}) "
                        f"VALUES({','.join('?' for _ in JOB_FIELDS)})",
                        tuple(clone.get(k) for k in JOB_FIELDS))
            _CANCEL.discard(new_id)
            _POOL.submit(_run_job, self, new_id)
            out.append({"id": new_id, "retryOf": str(jid), "ok": True, "status": "queued"})
        return out


def _view(row):
    """作业视图（camelCase，前端直接用；不暴露服务器绝对路径）。"""
    if not row:
        return {}
    try:
        params = json.loads(row.get("params") or "{}")
    except ValueError:
        params = {}
    return {
        "id": row.get("id"), "traceId": row.get("trace_id"),
        "itemId": row.get("item_id"), "projectId": row.get("project_id"),
        "shotNo": int(row.get("shot_no") or 0), "kind": row.get("kind"),
        "channel": row.get("channel"), "prompt": row.get("prompt") or "",
        "params": params, "status": row.get("status") or "queued",
        "progress": int(row.get("progress") or 0),
        "outputUrl": row.get("output_url") or "",
        "durationSec": row.get("duration_sec") or 0,
        "error": row.get("error") or "", "note": row.get("note") or "",
        "runNo": row.get("run_no") or 1,
        "createdAt": row.get("created_at") or "",
        "startedAt": row.get("started_at") or "",
        "finishedAt": row.get("finished_at") or "",
    }


# ---------------------------------------------------------------- 调度与执行
_POOL = ThreadPoolExecutor(max_workers=max(1, MAX_WORKERS), thread_name_prefix="media-job")
_LANES = {name: threading.BoundedSemaphore(max(1, n)) for name, n in LANE_LIMITS.items()}
_CANCEL = set()
_MANAGERS = {}
_MANAGERS_LOCK = threading.Lock()


def manager(path):
    path = os.path.abspath(path)
    with _MANAGERS_LOCK:
        mgr = _MANAGERS.get(path)
        if mgr is None:
            mgr = Manager(path)
            _MANAGERS[path] = mgr
        return mgr


class Canceled(Exception):
    pass


def _canceling(job_id):
    if job_id in _CANCEL:
        raise Canceled()


def _progress(mgr, job_id, pct, last):
    pct = max(0, min(99, int(pct)))
    if pct - last >= 4:
        mgr._set(job_id, progress=pct)
        mgr._emit(job_id)
        return pct
    return last


def _run_job(mgr, job_id):
    row = mgr.get(job_id)
    if not row or row.get("status") != "queued":
        return
    if job_id in _CANCEL:
        mgr._set(job_id, status="canceled", finished_at=now_iso(), error="老板手动停止")
        mgr._emit(job_id)
        return
    ch = CHANNELS.get(row.get("channel") or "")
    if not ch:
        mgr._set(job_id, status="failed", finished_at=now_iso(), error="未知渠道")
        mgr._emit(job_id)
        return
    try:
        params = json.loads(row.get("params") or "{}")
    except ValueError:
        params = {}
    mgr._set(job_id, status="running", started_at=now_iso(), progress=2)
    mgr._emit(job_id)
    lane = _LANES.get(row["channel"]) or threading.BoundedSemaphore(1)
    last = [2]

    def on_progress(pct):
        last[0] = _progress(mgr, job_id, pct, last[0])

    try:
        with lane:
            _canceling(job_id)
            if row["kind"] == "image":
                result = (_comfy_image if row["channel"] == "comfyui"
                          else _mediagen_image)(mgr, row, params, on_progress)
            elif row["kind"] == "video":
                result = _mediagen_video(mgr, row, params, on_progress)
            else:
                result = _tts(mgr, row, params, on_progress)
        path, url = _deliver(mgr, row, result)
        mgr._set(job_id, status="done", progress=100, output_path=path, output_url=url,
                 duration_sec=result.get("duration") or row.get("duration_sec") or 0,
                 remote_id=result.get("remote_id") or "", note=result.get("note") or row.get("note") or "",
                 finished_at=now_iso(), error="")
        _CANCEL.discard(job_id)
        mgr._emit(job_id)
    except Canceled:
        mgr._set(job_id, status="canceled", finished_at=now_iso(), error="老板手动停止")
        _CANCEL.discard(job_id)
        mgr._emit(job_id)
    except Exception as e:  # noqa: BLE001 - 失败要落到作业体里给前端看
        mgr._set(job_id, status="failed", finished_at=now_iso(),
                 error=f"{type(e).__name__}: {e}")
        _CANCEL.discard(job_id)
        mgr._emit(job_id)


# ---- 渠道适配器：返回 {"path": 本地成品路径, "duration": 秒, "remote_id": ...}
def _comfy_image(mgr, row, params, on_progress):
    import asset_fill
    url = asset_fill._comfy_image_url()
    ckpt = asset_fill._pick_checkpoint(url)
    port = urllib.parse.urlsplit(url).port or 8188
    client_id = f"opc-ops-{port}"
    steps = int(params.get("steps") or 20)
    graph = {
        "1": {"class_type": "CheckpointLoaderSimple", "inputs": {"ckpt_name": ckpt}},
        "2": {"class_type": "CLIPTextEncode",
              "inputs": {"text": row["prompt"], "clip": ["1", 1]}},
        "3": {"class_type": "CLIPTextEncode",
              "inputs": {"text": params.get("negative") or "低质量, 模糊, 水印, 变形",
                         "clip": ["1", 1]}},
        "4": {"class_type": "EmptyLatentImage",
              "inputs": {"width": int(params.get("width") or 768),
                         "height": int(params.get("height") or 1344), "batch_size": 1}},
        "5": {"class_type": "KSampler",
              "inputs": {"seed": int(time.time() * 1000) % (1 << 31),
                         "steps": steps, "cfg": float(params.get("cfg") or 6.5),
                         "sampler_name": "euler", "scheduler": "normal", "denoise": 1.0,
                         "model": ["1", 0], "positive": ["2", 0],
                         "negative": ["3", 0], "latent_image": ["4", 0]}},
        "6": {"class_type": "VAEDecode", "inputs": {"samples": ["5", 0], "vae": ["1", 2]}},
        "7": {"class_type": "SaveImage",
              "inputs": {"images": ["6", 0], "filename_prefix": "opc-material"}},
    }
    resp = _http_json(url + "/prompt", timeout=20,
                      data={"prompt": graph, "client_id": client_id})
    pid = resp.get("prompt_id")
    if not pid:
        raise RuntimeError(f"ComfyUI 拒绝作业：{resp}")
    on_progress(15)
    t0 = time.time()
    deadline = t0 + float(os.environ.get("OPC_COMFY_TIMEOUT", "900"))
    # ComfyUI 只有队列没有 HTTP 进度，按「模型预估耗时」给粗粒度进度（估算值写在 note）
    expect = max(45.0, steps * 6.0)
    while time.time() < deadline:
        _canceling(row["id"])
        hist = _http_json(f"{url}/history/{pid}", timeout=15)
        entry = hist.get(pid) or {}
        if entry.get("status", {}).get("status_str") == "error":
            raise RuntimeError("ComfyUI 作业执行失败（详见 ComfyUI 日志）")
        outs = (entry.get("outputs") or {}).get("7") or {}
        imgs = outs.get("images") or []
        if imgs:
            q = urllib.parse.urlencode({"filename": imgs[0]["filename"],
                                        "subfolder": imgs[0].get("subfolder", ""),
                                        "type": imgs[0].get("type", "output")})
            blob = _http(f"{url}/view?{q}", timeout=60)
            tmp = os.path.join(BASE_DIR, ".cache", "media", f"{row['id']}.png")
            os.makedirs(os.path.dirname(tmp), exist_ok=True)
            with open(tmp, "wb") as f:
                f.write(blob)
            on_progress(95)
            return {"path": tmp, "remote_id": pid,
                    "note": f"ComfyUI {ckpt} · {round(time.time() - t0, 1)}s"}
        on_progress(15 + int(min(75, max(0.0, (time.time() - t0) / expect) * 75)))
        time.sleep(2)
    # 超时：尽量中断远端执行，避免占着 ComfyUI 队列
    try:
        _http(url + "/interrupt", timeout=5, data=b"", method="POST",
              headers={"Content-Type": "application/json"})
    except Exception:
        pass
    raise TimeoutError("ComfyUI 出图超时")


def _mediagen_image(mgr, row, params, on_progress):
    body = {"prompt": row["prompt"], "negative_prompt": params.get("negative_prompt"),
            "width": int(params.get("width") or 640), "height": int(params.get("height") or 832),
            "steps": int(params.get("steps") or 20), "cfg": float(params.get("cfg") or 6.0),
            "backend": "sd15", "wait": False}
    ack = _http_json(MEDIAGEN + "/v1/image/jobs", timeout=20, data=body)
    return _poll_mediagen(row, ack, on_progress)


def _mediagen_video(mgr, row, params, on_progress):
    body = {"prompt": row["prompt"], "negative_prompt": params.get("negative_prompt"),
            "width": int(params.get("width") or 832), "height": int(params.get("height") or 480),
            "num_frames": int(params.get("num_frames") or 33),
            "steps": int(params.get("steps") or 20), "cfg": float(params.get("cfg") or 5.0),
            "fps": int(params.get("fps") or 16), "backend": row["channel"], "wait": False}
    ack = _http_json(MEDIAGEN + "/v1/video/jobs", timeout=20, data=body)
    return _poll_mediagen(row, ack, on_progress)


def _poll_mediagen(row, ack, on_progress):
    jid = ack.get("id") or ack.get("job_id")
    if not jid:
        raise RuntimeError(f"media-gen 拒绝作业：{ack}")
    on_progress(10)
    deadline = time.time() + float(os.environ.get("OPC_MEDIAGEN_TIMEOUT", "5400"))
    while time.time() < deadline:
        _canceling(row["id"])
        v = _http_json(f"{MEDIAGEN}/v1/jobs/{jid}", timeout=15)
        st = str(v.get("status") or "")
        prog = v.get("progress") or {}
        step, total = int(prog.get("step") or 0), int(prog.get("total") or 0)
        if total > 0:
            on_progress(10 + int(85 * min(1.0, step / total)))
        if st == "succeeded":
            dur = round(params_sec(v), 2) if row["kind"] == "video" else 0
            path = v.get("output_path") or ""
            if path and os.path.isfile(path):
                return {"path": path, "remote_id": jid, "duration": dur}
            url = v.get("output_url") or ""
            if url:
                tmp = os.path.join(BASE_DIR, ".cache", "media",
                                   f"{row['id']}{os.path.splitext(url)[1] or '.bin'}")
                os.makedirs(os.path.dirname(tmp), exist_ok=True)
                with open(tmp, "wb") as f:
                    f.write(_http(MEDIAGEN + url, timeout=120))
                return {"path": tmp, "remote_id": jid, "duration": dur}
            raise RuntimeError("media-gen 作业成功但没有产物路径")
        if st in ("failed", "interrupted"):
            raise RuntimeError(v.get("error") or f"media-gen 作业{st}")
        time.sleep(2)
    raise TimeoutError("media-gen 作业超时（可在运维页看队列）")


def params_sec(view):
    """media-gen 视频结果里 num_frames/fps 换算真实时长；无结果返回 0。"""
    try:
        p = view.get("params") or {}
        r = view.get("result") or {}
        nf = int(r.get("num_frames") or p.get("num_frames") or 0)
        fps = int(r.get("fps") or p.get("fps") or 16)
        return nf / fps if nf and fps else 0
    except (TypeError, ValueError):
        return 0


def _tts(mgr, row, params, on_progress):
    engine = "qwen3" if row["channel"] == "qwen" else "cosyvoice"
    body = {"text": row["prompt"], "engine": engine, "mode": "sync", "format": "json",
            "speed": float(params.get("speed") or 1.0)}
    if engine == "qwen3":
        body.update({"speaker": params.get("speaker") or "Vivian",
                     "language": params.get("language") or "zh",
                     "instruct": params.get("instruct") or None})
    else:
        body.update({"voice": params.get("voice") or "female-zh-real"})
    on_progress(20)
    res = _http_json(MEDIAENGINE + "/v1/tts", timeout=600, data=body)
    if not isinstance(res, dict):
        raise RuntimeError("TTS 返回格式异常")
    if res.get("audio_path") and os.path.isfile(res["audio_path"]):
        on_progress(95)
        return {"path": res["audio_path"], "duration": float(res.get("duration") or 0),
                "remote_id": str(res.get("job_id") or ""),
                "note": f"{engine} · {res.get('voice') or params.get('speaker') or ''}"}
    if res.get("audio_url"):
        tmp = os.path.join(BASE_DIR, ".cache", "media", f"{row['id']}.wav")
        os.makedirs(os.path.dirname(tmp), exist_ok=True)
        with open(tmp, "wb") as f:
            f.write(_http(MEDIAENGINE + res["audio_url"], timeout=120))
        on_progress(95)
        return {"path": tmp, "duration": float(res.get("duration") or 0),
                "remote_id": str(res.get("job_id") or ""),
                "note": f"{engine} · {res.get('voice') or params.get('speaker') or ''}"}
    raise RuntimeError(f"TTS 未产出音频：{str(res)[:200]}")


def _private_url(dest):
    """成品落在 conring/users/<手机号>/ 下时给 /private/<手机号>/...，否则返回空串。

    工具箱成品是本人私有产物，不能落 /vault 那条免登录通道。
    """
    try:
        rel = os.path.relpath(dest, USERS_DIR)
    except ValueError:
        return ""
    if os.path.isabs(rel) or rel == ".." or rel.startswith(".." + os.sep):
        return ""
    parts = rel.split(os.sep)
    # 与 server.PRIVATE_SUBS 保持一致：库里文件（opc.db 等）绝不能拼出可访问的链接
    if len(parts) < 2 or not parts[0].isdigit() or parts[1] not in ("工具箱", "obsidian"):
        return ""
    return "/private/" + "/".join([parts[0]] + [urllib.parse.quote(p) for p in parts[1:]])


def _deliver(mgr, row, result):
    src = (result or {}).get("path") or ""
    if not src or not os.path.isfile(src):
        raise RuntimeError("成品文件不存在")
    sub = {"image": "images", "video": "videos", "audio": "audio"}[row["kind"]]
    out_dir = row.get("out_dir") or os.path.join(BASE_DIR, ".cache", "media")
    vault = row.get("vault_dir") or ""
    os.makedirs(out_dir, exist_ok=True)
    ext = os.path.splitext(src)[1].lower() or {"image": ".png", "video": ".mp4", "audio": ".wav"}[row["kind"]]
    stem = f"shot{int(row.get('shot_no') or 0):02d}-{row['channel']}-r{int(row.get('run_no') or 1)}"
    # 工具箱手工任务没有分镜号，用 trace 后缀命名，避免一堆 shot00 分不清
    if str(row.get("trace_id") or "").startswith("tb-"):
        stem = f"tool-{row['channel']}-{str(row['trace_id'])[3:]}"
    dest = os.path.join(out_dir, stem + ext)
    # 手工任务同渠道重复提交会撞名（流水线靠 shot_no 区分，工具箱没有分镜号），
    # 撞名时追加序号，绝不覆盖已有成品
    if os.path.exists(dest):
        n = 2
        while os.path.exists(os.path.join(out_dir, f"{stem}-{n}{ext}")):
            n += 1
        dest = os.path.join(out_dir, f"{stem}-{n}{ext}")
    shutil.copyfile(src, dest)
    # 临时下载中转文件（.cache/media 下）落盘后即删，成品只在交付目录留一份
    cache_dir = os.path.join(BASE_DIR, ".cache", "media")
    if os.path.abspath(src).startswith(os.path.abspath(cache_dir)):
        try:
            os.remove(src)
        except OSError:
            pass
    url = _private_url(dest)
    if not url and vault:
        try:
            url = "/vault/" + os.path.relpath(dest, vault).replace(os.sep, "/")
        except ValueError:
            url = ""
    return dest, url


def summary(jobs):
    """分镜作业概览：数量与最坏状态，前端表格直接用。"""
    out = {}
    for j in jobs:
        no = int(j.get("shotNo") or j.get("shot_no") or 0)
        rec = out.setdefault(no, {"total": 0, "running": 0, "queued": 0, "done": 0,
                                  "failed": 0, "canceled": 0, "progress": 0})
        st = str(j.get("status") or "")
        rec["total"] += 1
        if st in rec:
            rec[st] += 1
        rec["progress"] = max(rec["progress"], int(j.get("progress") or 0))
    return out