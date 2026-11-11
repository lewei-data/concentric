"""离线契约校验：server v2.1.0 样本字段 + App 端「不使用其它 URL」约束。不联网。"""
import json
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DOCS = os.path.join(ROOT, "docs")
SRC = os.path.join(ROOT, "app", "src", "main", "java", "com", "boray", "voicemcp")


def load(name):
    with open(os.path.join(DOCS, name), encoding="utf-8") as f:
        return json.load(f)


def content_text(doc):
    """兼容 {content:[...]} 与 {jsonrpc:..., result:{content:[...]}} 两种样本"""
    for b in doc.get("result", doc).get("content", []):
        if b.get("type") == "text":
            return b.get("text", "")
    return "{}"


def read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


def main():
    checks = []

    def ok(label, cond, detail=""):
        checks.append((label, bool(cond), detail))

    # 1. 工具清单：v2.1.0 五个关键工具与新参数
    tools = {t["name"]: set((t.get("inputSchema") or {}).get("properties", {}).keys())
             for t in load("v21_tools.json")}
    for name in ("profile", "voice_avatars", "voice_chat", "voice_job"):
        ok("tool " + name, name in tools)
    need = {"avatar_id", "mode", "bandwidth_kbps", "delegate", "callback_url",
            "wait", "wait_sec", "include_audio"}
    ok("voice_chat 新参数齐全", need.issubset(tools.get("voice_chat", set())),
       "缺少 " + str(sorted(need - tools.get("voice_chat", set()))))
    ok("voice_job 长轮询参数", {"job_id", "wait_sec", "include_audio"}
       .issubset(tools.get("voice_job", set())))

    # 2. profile 全量 18 人，字段与 Employee.fromJson 对齐
    profile = json.loads(content_text(load("v21_profile_all.json")))
    emps = profile.get("employees", [])
    ok("profile 返回 18 人", profile.get("count") == 18 and len(emps) == 18,
       "count=%s len=%s" % (profile.get("count"), len(emps)))
    ok("v21 历史样本 asset_url_base 为 null", profile.get("asset_url_base") is None)
    required = ("id", "no", "name", "dept", "title", "project", "role", "voice",
                "gender", "age", "profile_ready", "photo", "videoTalk", "videoIdle",
                "has_photo", "has_video", "assets", "skills", "accent", "skin")
    missing = {k for e in emps for k in required if k not in e}
    ok("18 人字段齐全", not missing, "缺少 " + str(sorted(missing)))
    ok("18 个音色互不相同", len({e["voice"] for e in emps}) == 18)
    ok("素材缺件清单为空", not profile.get("missing_photo") and not profile.get("missing_video"))
    not_ready = [e["id"] for e in emps if not e.get("profile_ready")]
    ok("profile_ready 标记存在", not_ready == ["boss", "manual_scout"], str(not_ready))

    # 3. voice_avatars 素材清单结构
    av = json.loads(content_text(load("v21_voice_avatars.json")))
    ok("voice_avatars 18 条", av.get("count") == 18 and len(av.get("avatars", [])) == 18)
    ok("voice_avatars 含 photo/videos/has_video",
       all({"photo", "videos", "has_video"}.issubset(a) for a in av.get("avatars", [])))
    ok("v21 历史样本为 /avatars/ 旧路径",
       all(a["photo"]["url"].startswith("/avatars/") for a in av.get("avatars", [])))

    # 4. 用户约束：App 不接任何其它 URL 的素材
    assets = read(os.path.join(SRC, "AssetUrls.java"))
    store = read(os.path.join(SRC, "EmployeeStore.java"))
    cfg = read(os.path.join(SRC, "McpConfig.java"))
    java = assets + store + cfg
    ok("源码无 8848 端口", "8848" not in java)
    ok("源码无其它主机/端口拼接", not re.search(r"31\.206|31\.208.*8848|https?://\S+:8848", assets))
    ok("素材前缀只来自 asset_url_base 或 MCP 同源",
       "serverBase(c)" in assets and "McpConfig.load(c).url" in assets
       and "origin(mcpUrl)" in assets)
    ok("无候选列表 / 无可用前缀探测",
       "candidates" not in assets and "probe" not in assets.lower())

    # 5. 新协议默认行为：不再固定传 voice / profile
    main_act = read(os.path.join(SRC, "MainActivity.java"))
    ok("voice 只在显式配置时传", 'if (!config.voice.isEmpty()) args.put("voice", config.voice);' in main_act)
    ok("profile 只在显式配置时传", 'if (!config.profile.isEmpty()) args.put("profile", config.profile);' in main_act)
    ok("请求带 avatar_id / mode / bandwidth_kbps / delegate",
       all(x in main_act for x in ('args.put("avatar_id"', 'args.put("mode"',
                                  'args.put("bandwidth_kbps"', 'args.put("delegate"')))
    ok("done 结构使用 segments 时间轴",
       "EXTRA_SEGMENTS" in read(os.path.join(SRC, "ReportActivity.java")))
    ok("历史数据库升级不丢数据",
       "ALTER TABLE" in read(os.path.join(SRC, "HistoryDb.java")))

    # 6. 2026-11-10 服务端素材路径修复后的当前契约（v22 样本）
    cur = json.loads(content_text(load("v22_profile_all.json")))
    cur_emps = cur.get("employees", [])
    ok("asset_url_base 已修复为 /conring/asr/assets",
       cur.get("asset_url_base") == "/conring/asr/assets",
       repr(cur.get("asset_url_base")))
    ok("修复后 profile 仍为 18 人", len(cur_emps) == 18)
    prefix = "/conring/asr/assets/"
    paths = []
    for e in cur_emps:
        a = e.get("assets") or {}
        paths += [e.get("photo", ""), e.get("videoTalk", ""),
                  e.get("videoIdle", ""), a.get("face_url", "")]
    ok("18 人 × 4 类素材均为同源绝对路径",
       len(paths) == 72 and all(isinstance(p, str) and p.startswith(prefix) for p in paths),
       "异常 " + str([p for p in paths if not isinstance(p, str) or not p.startswith(prefix)][:3]))
    probe = load("v22_assets_check.json")
    ok("18 人素材公网实测全部可达", probe.get("bad") == 0 and probe.get("ok") == 90,
       "ok=%s bad=%s" % (probe.get("ok"), probe.get("bad")))

    with open(os.path.join(ROOT, "app", "src", "main", "assets", "employees.json"),
              encoding="utf-8") as f:
        offline = json.load(f)
    ok("离线档案同步为 18 人新路径",
       offline.get("count") == 18 and all(
           prefix in (e.get(k) or "") for e in offline["employees"]
           for k in ("photo", "videoTalk", "videoIdle", "faceUrl")))

    # 关键回归：以 / 开头的路径直接拼 origin，绝不叠加 asset_url_base
    ok("AssetUrls 对绝对路径直接拼 origin",
       "if (s.startsWith(\"/\"))" in assets and "return join(origin, s);" in assets)
    ok("AssetUrls 提供纯逻辑 resolveSpec 供离线校验",
       "public static String resolveSpec(" in assets)
    ok("素材加载不带 /mcp 的 Bearer（无需 Token）",
       "Authorization" not in store and "Authorization" not in read(
           os.path.join(SRC, "PushProcessor.java")))
    ok("源码仍无其它素材 URL", "8848" not in (assets + store + cfg)
       and not re.search(r"31\.206|31\.208|http://\S+:8848", assets))

    # ---- 0.3.2：名称一致 / 默认 CEO / 试听对嘴 / 形象一致 ----
    main_xml = read(os.path.join(ROOT, "app", "src", "main", "res", "layout",
                                 "activity_main.xml"))
    strings = read(os.path.join(ROOT, "app", "src", "main", "res", "values",
                                "strings.xml"))
    store2 = read(os.path.join(SRC, "EmployeeStore.java"))
    report = read(os.path.join(SRC, "ReportActivity.java"))
    detail = read(os.path.join(SRC, "EmployeeDetailActivity.java"))
    preview = read(os.path.join(SRC, "PreviewAudio.java"))
    human = read(os.path.join(SRC, "DigitalHumanView.java"))
    gradle = read(os.path.join(ROOT, "app", "build.gradle"))

    ok("首页标题引用 app_name（同心圆）",
       'android:text="@string/app_name"' in main_xml
       and 'name="app_name">同心圆' in strings)
    ok("首页不再出现 MCP 语音指令", "MCP 语音指令" not in main_xml)
    ok("默认汇报员为 CEO（含旧版一次性迁移）",
       'DEFAULT_EMPLOYEE_ID = "ceo"' in store2
       and "getString(KEY_CURRENT, DEFAULT_EMPLOYEE_ID)" in store2
       and "KEY_DEFAULT_MIGRATED" in store2)
    ok("avatar 找不到时回退 CEO 而非列表首位",
       "findById(all, DEFAULT_EMPLOYEE_ID)" in store2
       and "employeeFromAvatar" in report)
    ok("试听用服务端专属音色生成真实 WAV",
       "cfg.url = McpConfig.TTS_URL" in preview
       and 'args.put("voice", voice)' in preview
       and "return_audio" in preview
       and "EXTRA_AUDIO" in detail)
    ok("试听失败有本地 synthesizeToFile 兜底",
       "synthesizeToFile" in report and "onSynthReady" in report
       and "onSynthFailed" in report)
    ok("口型按 MediaPlayer 真实进度取 WAV 包络",
       "media.getCurrentPosition()" in report
       and "LipSync.fromWav" in report
       and "audioDriven" in report)
    ok("形象一致：小窗改用同人照片 + 口型",
       "setPhotoBitmap" in human and "drawPhotoMouth" in human
       and "loadPhotoBitmap" in report and "applyPipPhoto" in report)
    ok("形象一致：档案页 / 汇报页传同一员工素材",
       "EXTRA_FACE" in detail and "EXTRA_EMPLOYEE_NAME" in detail
       and "EXTRA_FACE" in report)
    ok("视频状态可见且如实标注为循环底片",
       "仿真人视频底片 · 口型实时驱动" in report
       and "视频不可用 · 已回退照片" in report)
    ok("版本升为 0.3.2 且 clientInfo 同步",
       'appVersionName = "0.3.2"' in gradle
       and 'CLIENT_VERSION = "0.3.2"' in cfg)
    compare = load("v23_assets_compare.json")
    ok("服务端 profile 与 APK 离线档案素材 72 项全一致",
       compare.get("compared") == 72 and compare.get("mismatches") == [],
       "compared=%s mismatches=%s" % (compare.get("compared"),
                                      len(compare.get("mismatches") or [])))
    ok("服务端与客户端默认汇报员均为 ceo / m08",
       (compare.get("ceo") or {}).get("id") == "ceo"
       and (compare.get("ceo") or {}).get("voice") == "m08")

    failed = [c for c in checks if not c[1]]
    for label, passed, detail in checks:
        print(("PASS " if passed else "FAIL ") + label + (" · " + detail if detail and not passed else ""))
    print("\nTOTAL %d, PASS %d, FAIL %d" % (len(checks), len(checks) - len(failed), len(failed)))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
