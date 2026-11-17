"""素材内化：文本抽取 → 切块 → 生成知识卡片 → 检索与上下文拼装。"""

import html
import json
import os
import re
import shutil
import stat
import subprocess
import tempfile
import zipfile
from pathlib import Path

from . import config, llm

TEXT_EXT = {".md", ".markdown", ".txt", ".json", ".csv", ".tsv", ".log", ".srt", ".vtt",
            ".yaml", ".yml", ".html", ".htm", ".py", ".js", ".ts", ".sh", ".xml",
            ".ini", ".conf", ".srt"}
# 需要 LibreOffice 转换的旧版/富文本格式
LEGACY_DOC_EXT = {".doc", ".xls", ".ppt", ".rtf", ".odt", ".ods", ".odp"}
CJK = re.compile(r"[\u4e00-\u9fff]+")
ASCII_WORD = re.compile(r"[A-Za-z0-9_]{2,}")
STOP = set("the and for with that this from are was not you all can our will 一个 我们 可以 这个 那个".split())


def extract_text(path):
    """返回 (正文, 跳过原因)。原因非空表示无法抽取文本。"""
    p = Path(path)
    ext = p.suffix.lower()
    try:
        if ext in TEXT_EXT:
            raw = p.read_text(encoding="utf-8", errors="replace")
            if ext in (".html", ".htm"):
                raw = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", raw, flags=re.S | re.I)
                raw = html.unescape(re.sub(r"<[^>]+>", " ", raw))
            return _normalize(raw), ""
        if ext == ".docx":
            try:
                with zipfile.ZipFile(p) as z:
                    xml = z.read("word/document.xml").decode("utf-8", "replace")
                xml = re.sub(r"</w:p>", "\n", xml)
                return _normalize(html.unescape(re.sub(r"<[^>]+>", "", xml))), ""
            except (KeyError, zipfile.BadZipFile):
                return _soffice_text(p)
        if ext == ".pptx":
            return _pptx_text(p), ""
        if ext == ".xlsx":
            return _xlsx_text(p), ""
        if ext == ".pdf":
            return _pdf_text(p)
        if ext == ".zip":
            return _zip_text(p)
        if ext in LEGACY_DOC_EXT:
            return _soffice_text(p)
        if ext in (".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp",
                   ".mp3", ".wav", ".mp4", ".mov", ".mkv"):
            return "", "影音/图片素材已归档，本模块暂不做像素级分析"
        return "", "暂不支持 %s 的文本抽取" % ext
    except Exception as e:                                   # 文件损坏/加密/权限
        return "", "抽取失败：%s" % e


# ---------------------------------------------------------------- 文档抽取实现

def _pdftotext(p):
    exe = shutil.which("pdftotext")
    if not exe:
        return ""
    out = subprocess.run([exe, "-q", "-enc", "UTF-8", str(p), "-"],
                         capture_output=True, text=True, timeout=180)
    return _normalize(out.stdout)


def _pdf_text(p):
    text = _pdftotext(p)
    if text:
        return text, ""
    # pdftotext 缺失或抽不出文本 → 再试 LibreOffice（部分 PDF 只有它认得）
    text, err = _soffice_text(p)
    if text:
        return text, ""
    return "", "PDF 无文本层（可能是扫描件）" + ("；%s" % err if err else "")


def _soffice_bin():
    return shutil.which(config.SOFFICE) or shutil.which("libreoffice") or shutil.which("soffice")


def _soffice_convert(p, outdir, target, timeout=None):
    """调用 LibreOffice 把 p 转成 target 过滤器格式，返回产物路径或 (None, 原因)。"""
    exe = _soffice_bin()
    if not exe:
        return None, "未安装 LibreOffice"
    profile = tempfile.mkdtemp(prefix="lo-profile-")
    env = dict(os.environ)
    env["HOME"] = profile
    env.pop("LD_LIBRARY_PATH", None)                        # 避免加载宿主 Python 的库
    cmd = [exe, "--headless", "--norestore", "--invisible",
           "-env:UserInstallation=file://" + profile,
           "--convert-to", target, "--outdir", str(outdir), str(p)]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True,
                           timeout=timeout or config.SOFFICE_TIMEOUT, env=env)
    except subprocess.TimeoutExpired:
        return None, "LibreOffice 转换超时"
    finally:
        shutil.rmtree(profile, ignore_errors=True)
    produced = Path(outdir) / (Path(p).stem + "." + target.split(":")[0].split(" ")[0])
    if produced.is_file() and produced.stat().st_size > 0:
        return produced, ""
    tail = _normalize(r.stderr or r.stdout)[-300:]
    return None, "LibreOffice 未产出文本%s" % ("：" + tail if tail else "")


def _soffice_text(p):
    ext = Path(p).suffix.lower()
    with tempfile.TemporaryDirectory(prefix="lo-out-") as outdir:
        if ext in (".xls", ".ods"):
            target = "csv:Text - txt - csv (StarCalc):44,34,76"
        elif ext in (".ppt", ".odp"):
            # Impress 没有 txt 导出，先转 PDF 再抽文本
            pdf, err = _soffice_convert(p, outdir, "pdf")
            if not pdf:
                return "", err
            return _pdf_text(pdf)
        else:
            target = "txt:Text (encoded):UTF8"
        produced, err = _soffice_convert(p, outdir, target)
        if not produced:
            return "", err
        raw = produced.read_text(encoding="utf-8", errors="replace")
        text = _normalize(raw)
        return (text, "") if text else ("", "转换后无文本内容")


def _pptx_text(p):
    parts = []
    with zipfile.ZipFile(p) as z:
        names = sorted((n for n in z.namelist()
                        if re.match(r"ppt/slides/slide\d+\.xml$", n)),
                       key=lambda n: int(re.search(r"(\d+)", n).group(1)))
        for n in names:
            xml = z.read(n).decode("utf-8", "replace")
            texts = re.findall(r"<a:t>(.*?)</a:t>", xml, flags=re.S)
            if texts:
                parts.append("【%s】%s" % (Path(n).stem, " ".join(html.unescape(t) for t in texts)))
    return _normalize("\n\n".join(parts))


def _xlsx_text(p):
    import xml.etree.ElementTree as ET
    ns = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main",
          "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships"}
    with zipfile.ZipFile(p) as z:
        shared = []
        if "xl/sharedStrings.xml" in z.namelist():
            root = ET.fromstring(z.read("xl/sharedStrings.xml"))
            for si in root.findall("m:si", ns):
                shared.append("".join(t.text or "" for t in si.iter("{http://schemas.openxmlformats.org/spreadsheetml/2006/main}t")))
        out = []
        for n in sorted(x for x in z.namelist() if re.match(r"xl/worksheets/sheet\d+\.xml$", x)):
            root = ET.fromstring(z.read(n))
            rows = []
            for row in root.findall(".//m:sheetData/m:row", ns):
                cells = []
                for c in row.findall("m:c", ns):
                    v = c.find("m:v", ns)
                    if v is None:
                        continue
                    val = v.text or ""
                    if c.get("t") == "s" and val.isdigit():
                        idx = int(val)
                        val = shared[idx] if idx < len(shared) else ""
                    cells.append(val)
                if any(cells):
                    rows.append("\t".join(cells))
            if rows:
                label = Path(n).stem
                out.append("【%s】\n%s" % (label, "\n".join(rows)))
    return _normalize("\n\n".join(out))


def _zip_text(p, depth=0):
    """安全解压 zip（防 zip-slip / 压缩炸弹），逐条抽文本后拼接。"""
    if depth > 1:
        return "", "zip 嵌套层级过深，已跳过"
    max_entries = config.ZIP_MAX_ENTRIES
    max_bytes = int(config.ZIP_MAX_UNPACK_MB * 1024 * 1024)
    parts, used, skipped = [], 0, 0
    with tempfile.TemporaryDirectory(prefix="zip-in-") as td:
        with zipfile.ZipFile(p) as z:
            infos = [i for i in z.infolist() if not i.is_dir()][:max_entries + 1]
            if len(infos) > max_entries:
                infos = infos[:max_entries]
                parts.append("（压缩包条目超过 %d，仅处理前 %d 条）" % (max_entries, max_entries))
            for info in infos:
                name = info.filename
                if name.startswith("/") or ".." in Path(name).parts:
                    skipped += 1
                    continue
                if stat.S_ISLNK((info.external_attr >> 16) & 0xFFFF):
                    skipped += 1
                    continue
                if used + info.file_size > max_bytes:
                    parts.append("（已达解压总量上限 %dMB，剩余条目跳过）" % config.ZIP_MAX_UNPACK_MB)
                    break
                data = z.read(info)
                used += len(data)
                safe = Path(td) / ("%04d_%s" % (len(parts), Path(name).name))
                safe.write_bytes(data)
                if Path(name).suffix.lower() == ".zip":
                    text, reason = _zip_text(safe, depth + 1)
                    if not text:
                        skipped += 1
                        continue
                else:
                    text, reason = extract_text(safe)
                    if not text:
                        skipped += 1
                        continue
                parts.append("### %s\n%s" % (name, text[:20000]))
    if not parts:
        return "", "压缩包内没有可抽取文本的文件（跳过 %d 个）" % skipped
    head = "压缩包提取完成：%d 段文本，跳过 %d 个不支持或超限文件" % (len(parts), skipped)
    return _normalize(head + "\n\n" + "\n\n".join(parts)), ""


def _normalize(text):
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def chunk_text(text, size=None):
    size = size or config.CHUNK_CHARS
    parts, buf = [], ""
    for para in re.split(r"\n\s*\n", text):
        para = para.strip()
        if not para:
            continue
        if len(buf) + len(para) + 1 <= size:
            buf = (buf + "\n" + para).strip()
            continue
        if buf:
            parts.append(buf)
        while len(para) > size:
            parts.append(para[:size])
            para = para[size:]
        buf = para
    if buf:
        parts.append(buf)
    return parts


def terms(text):
    out = set()
    for m in CJK.findall(text):
        for i in range(len(m) - 1):
            out.add(m[i:i + 2])
    for w in ASCII_WORD.findall(text.lower()):
        if w not in STOP:
            out.add(w)
    return out


def _score(qterms, body):
    low = body.lower()
    covered = sum(1 for t in qterms if t in low)
    hits = sum(low.count(t) for t in qterms)
    return covered * 3.0 + min(hits, 30) * 0.2


def search(store, phone, query, k=6):
    qterms = terms(query)
    if not qterms:
        return []
    scored = []
    for row in store.chunks(phone, limit=4000):
        s = _score(qterms, row["body"] or "")
        if s > 0:
            scored.append((s, row))
    scored.sort(key=lambda x: -x[0])
    out = []
    for s, row in scored[:max(1, int(k))]:
        body = row["body"] or ""
        pos = min((body.find(t) for t in qterms if t in body), default=0)
        start = max(0, pos - 100)
        out.append({"material_id": row["material_id"], "seq": row["seq"], "score": round(s, 2),
                    "snippet": body[start:start + 320].replace("\n", " ")})
    return out


def build_context(store, phone, query, budget=None):
    budget = budget or config.CONTEXT_BUDGET
    lines, used = [], 0
    for card in store.cards(phone, limit=5):
        block = "【素材卡片】%s\n%s\n%s" % (
            card.get("filename") or card.get("title"),
            card.get("summary") or "",
            "；".join(json.loads(card.get("bullets") or "[]")))
        if used + len(block) > budget * 0.5:
            break
        lines.append(block)
        used += len(block)
    for hit in search(store, phone, query, k=6):
        mat = store.material(hit["material_id"]) or {}
        block = "【片段】%s #%s\n%s" % (mat.get("filename", "?"), hit["seq"], hit["snippet"])
        if used + len(block) > budget:
            break
        lines.append(block)
        used += len(block)
    return "\n\n".join(lines)


def _fallback_card(filename, text):
    kw = sorted({t for t in terms(text) if len(t) > 1}, key=lambda t: -text.count(t))[:8]
    lines = [ln.strip(" -*\t") for ln in text.splitlines() if ln.strip()][:6]
    return {"title": filename, "summary": text[:280].replace("\n", " "),
            "bullets": lines[:5], "tags": kw[:6], "degraded": True}


def analyze_material(store, phone, material_id):
    """后台作业主体：抽取 → 切块入库 → 生成知识卡片。"""
    mat = store.material(material_id)
    if not mat:
        raise ValueError("素材不存在：%s" % material_id)
    # 内容化钩子：图片/影音在本模块会被跳过像素分析，统一交给工作台内容化引擎
    # （OCR / ASR → 知识卡片 → 知识库 + 知识图谱 → 评分 → 小秘书/飞书汇报）。
    try:
        import sys as _sys
        from pathlib import Path as _Path
        _root = str(_Path(__file__).resolve().parents[2])
        if _root not in _sys.path:
            _sys.path.insert(0, _root)
        _proto = str(_Path(_root) / "prototype")
        if _proto not in _sys.path:
            _sys.path.insert(0, _proto)
        import material_content as _MC
        _MC.enqueue(mat["path"], filename=mat["filename"], source="mcp",
                    phone=phone, sha256=mat.get("sha256") or "", note="MCP 上传")
    except Exception as _e:                                  # 内容化不可用不影响 MCP 原流程
        print("[knowledge] 内容化入队失败：%s: %s" % (type(_e).__name__, _e), flush=True)
    store.set_material_status(material_id, "running")
    text, reason = extract_text(mat["path"])
    if not text:
        store.set_material_status(material_id, "skipped", reason)
        return {"material_id": material_id, "status": "skipped", "reason": reason}

    chunks = chunk_text(text)
    n = store.replace_chunks(material_id, phone, chunks)

    head = text[:12000]
    try:
        card = llm.chat_json([
            {"role": "system", "content": "你是 OPC 公司的资料整理员。只输出 JSON，不要解释。"},
            {"role": "user", "content":
                "阅读以下素材，输出 JSON：{\"title\":\"≤20字标题\",\"summary\":\"120字内摘要\","
                "\"bullets\":[\"3-5条关键信息\"],\"tags\":[\"4-8个标签\"]}。\n\n素材名：%s\n\n%s"
                % (mat["filename"], head)},
        ], max_tokens=800)
        card.setdefault("degraded", False)
    except llm.LLMError as e:
        card = _fallback_card(mat["filename"], text)
        card["llm_error"] = str(e)[:200]

    store.add_card(material_id, phone, str(card.get("title") or mat["filename"]),
                   str(card.get("summary") or ""),
                   json.dumps(card.get("bullets") or [], ensure_ascii=False),
                   json.dumps(card.get("tags") or [], ensure_ascii=False))
    store.set_material_status(material_id, "analyzed")
    return {"material_id": material_id, "status": "analyzed", "chunks": n,
            "chars": len(text), "card": card}
