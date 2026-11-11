"""从实测的 profile 样本生成 assets/employees.json 离线兜底档案（18 人）。

只保留客户端需要的字段，去掉服务器本地绝对路径（assets.*.path 不下发）。
2026-11-10 服务端修复素材路径后，photo / videoTalk / videoIdle / face_url
已是含 /conring/asr/assets 前缀的同源绝对路径，原样写入即可；
客户端 AssetUrls 只拼 MCP 端点 origin，不叠加 asset_url_base、不接其它 URL。

先运行 tools/probe_assets_v22.py 更新 docs/v22_profile_all.json，再运行本脚本。
"""
import json
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SAMPLE = os.path.join(ROOT, "docs", "v22_profile_all.json")
OUT = os.path.join(ROOT, "app", "src", "main", "assets", "employees.json")

KEEP = ("id", "no", "name", "dept", "title", "project", "role", "motto", "bio",
        "skills", "profile", "voice", "gender", "age", "accent", "skin", "hair",
        "suit", "shirt", "hairStyle", "glasses", "emotion", "profile_ready",
        "photo", "videoTalk", "videoIdle", "has_photo", "has_video")


def content_text(doc):
    """兼容 {content:[...]} 与 {jsonrpc:..., result:{content:[...]}} 两种样本"""
    for b in doc.get("result", doc).get("content", []):
        if b.get("type") == "text":
            return b.get("text", "")
    return "{}"


def main():
    with open(SAMPLE, encoding="utf-8") as f:
        raw = json.load(f)
    root = json.loads(content_text(raw))
    employees = []
    for e in root.get("employees", []):
        item = {k: e[k] for k in KEEP if k in e}
        assets = e.get("assets") or {}
        face = e.get("face_url") or e.get("faceUrl") or assets.get("face_url") or ""
        item["faceUrl"] = face
        employees.append(item)
    out = {
        "version": 3,
        "source": "server v2.1.0+ profile (offline snapshot, 2026-11-10 assets fixed)",
        "asset_url_base": root.get("asset_url_base"),
        "count": len(employees),
        "employees": employees,
    }
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    print("WROTE", OUT, "employees =", len(employees),
          "bytes =", os.path.getsize(OUT),
          "asset_url_base =", repr(root.get("asset_url_base")))


if __name__ == "__main__":
    main()
