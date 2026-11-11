package com.boray.voicemcp;

import android.content.Context;
import android.util.Base64;

import org.json.JSONArray;
import org.json.JSONException;
import org.json.JSONObject;

import java.nio.charset.StandardCharsets;
import java.util.Locale;

/**
 * 解析 MCP tools/call 的结果，统一支持三种形态：
 *  1) voice_chat 立即回执：text(JSON: job_id / status=accepted / ack.text / avatar) + audio(收到语音)
 *  2) voice_job 完成结果：text(JSON: result.speech_text / detail_text / segments / render.avatar) + audio(结果语音)
 *  3) 旧版同步返回 / SSE 通知 / 普通文本消息
 *
 * 展示原则（用户要求）：只留「核心内容」——系统提示、服务端路径、音频文件名一律不进主界面；
 * 文件统一落盘成带语义标签的链接（播报语音 / 文字详情 / 汇报视频…），点开才看原始内容。
 * 所有历史文件写入永久目录 files/history/，配合 HistoryDb 长期保存。
 */
public final class ResultParser {

    private static final int MAX_RAW = 20000;

    public static McpResult parse(Context ctx, JSONObject result) {
        return parse(ctx, result, true);
    }

    /** 长轮询中间态（status=running）只解析元数据，不落盘，避免每 20 秒堆一个文件 */
    public static McpResult parseTransient(Context ctx, JSONObject result) {
        return parse(ctx, result, false);
    }

    private static McpResult parse(Context ctx, JSONObject result, boolean persist) {
        if (result == null) return McpResult.error("服务端没有返回内容");
        McpResult out = new McpResult();
        out.rawJson = abbreviate(result.toString(), MAX_RAW);
        out.isError = result.optBoolean("isError", false);

        StringBuilder raw = new StringBuilder();
        StringBuilder legacy = new StringBuilder();
        JSONArray content = result.optJSONArray("content");

        if (content != null) {
            for (int i = 0; i < content.length(); i++) {
                JSONObject block = content.optJSONObject(i);
                if (block == null) continue;
                String type = block.optString("type", "text");
                if ("text".equals(type)) {
                    String text = block.optString("text", "");
                    raw.append(text).append('\n');
                    legacy.append(humanize(text)).append("\n\n");
                    digestText(ctx, out, text);
                    if (persist) {
                        addTextFile(ctx, out, "result_text_" + (i + 1) + ".txt",
                                readableTextFile(out, text));
                    }
                } else if ("audio".equals(type) || "image".equals(type)
                        || "video".equals(type) || "blob".equals(type)) {
                    handleBinary(ctx, out, block, type, i, persist);
                } else if ("resource".equals(type) || "resource_link".equals(type)) {
                    handleResource(ctx, out, block, raw, legacy, i, persist);
                }
            }
        }

        JSONObject structured = result.optJSONObject("structuredContent");
        if (structured != null) {
            String s = structured.toString();
            raw.append(s);
            digestText(ctx, out, s);
        }
        if (raw.length() == 0 && result.has("text")) {
            String t = result.optString("text", "");
            raw.append(t);
            legacy.append(humanize(t));
            digestText(ctx, out, t);
        }

        out.rawText = abbreviate(raw.toString().trim(), MAX_RAW);
        if (out.coreText.isEmpty()) out.coreText = cleanCore(legacy.toString());
        if (out.coreText.isEmpty()) out.coreText = "（服务端未返回文本内容，请看下方链接）";
        if (out.answerText.isEmpty()) out.answerText = out.coreText;
        if (out.displayText.isEmpty()) {
            out.displayText = out.detailText.isEmpty()
                    ? out.coreText : out.coreText + "\n\n" + out.detailText;
        }
        if (out.messageId.isEmpty()) {
            out.messageId = out.jobId.isEmpty() ? hash(out.rawText + out.coreText) : out.jobId;
        }
        return out;
    }

    /** SSE 推送通知 / 新邮件摘要：没有内容块，只有一段文本 */
    public static McpResult fromText(Context ctx, String text, String source, String messageId) {
        McpResult out = new McpResult();
        out.source = source;
        out.messageId = messageId;
        out.rawText = abbreviate(text, MAX_RAW);
        out.rawJson = out.rawText;
        String core = text == null ? "" : text.trim();
        if (core.startsWith("{")) {
            try {
                JSONObject j = new JSONObject(core);
                String picked = firstNonEmpty(j, "speech_text", "answer", "text", "message",
                        "content", "detail_text", "subject");
                if (picked != null) core = picked;
                // v2.1.0 done / callback 推送：正文在 result 里，并带 error / segments / avatar
                JSONObject res = j.optJSONObject("result");
                if (res != null) {
                    String sp = firstNonEmpty(res, "speech_text", "answer", "text");
                    if (sp != null) core = sp;
                    String dt = firstNonEmpty(res, "detail_text", "detail");
                    if (dt != null) out.detailText = dt;
                    JSONArray segs = res.optJSONArray("segments");
                    if (segs != null) out.segmentsJson = abbreviate(segs.toString(), MAX_RAW);
                }
                String err = j.optString("error", "");
                if (!err.isEmpty()) {
                    core = err;
                    out.errorText = err;
                    out.isError = true;
                }
                String jid = firstNonEmpty(j, "job_id", "jobId");
                if (jid != null) out.jobId = jid;
                out.status = j.optString("status", "");
                readAvatar(ctx, out, j.optJSONObject("avatar"));
                JSONObject params = j.optJSONObject("params");
                if (params != null) {
                    String inner = firstNonEmpty(params, "text", "message", "content", "data");
                    if (inner != null) core = inner;
                }
            } catch (JSONException ignored) {
            }
        }
        core = cleanCore(core);
        out.coreText = core.isEmpty() ? "收到一条新消息" : core;
        out.answerText = out.coreText;
        out.displayText = out.coreText;
        return out;
    }

    /** 从 text 块（JSON 或纯文本）里抽取核心内容与异步任务元数据（server v2.1.0） */
    private static void digestText(Context ctx, McpResult out, String text) {
        String s = text == null ? "" : text.trim();
        if (s.isEmpty()) return;
        if (!s.startsWith("{")) {
            if (out.coreText.isEmpty()) out.coreText = cleanCore(s);
            return;
        }
        try {
            JSONObject j = new JSONObject(s);
            if (out.jobId.isEmpty()) {
                String id = firstNonEmpty(j, "job_id", "jobId");
                if (id != null) out.jobId = id;
            }
            if (out.status.isEmpty()) out.status = j.optString("status", "");
            if (out.questionText.isEmpty()) {
                String q = firstNonEmpty(j, "question", "asr_text", "transcript");
                if (q != null) out.questionText = q;
            }
            if (out.ackText.isEmpty()) {
                JSONObject ack = j.optJSONObject("ack");
                if (ack != null) out.ackText = ack.optString("text", "");
            }
            if (out.mode.isEmpty()) out.mode = j.optString("mode", "");
            if (out.voice.isEmpty()) out.voice = j.optString("voice", "");
            if (out.errorText.isEmpty()) out.errorText = j.optString("error", "");
            JSONObject delegate = j.optJSONObject("delegate");
            if (delegate != null && out.delegateId.isEmpty()) {
                out.delegateId = delegate.optString("id", "");
                out.delegateName = delegate.optString("name", "");
            }

            readAvatar(ctx, out, j.optJSONObject("avatar"));
            JSONObject res = j.optJSONObject("result");
            String speech = null, detail = null;
            if (res != null) {
                speech = firstNonEmpty(res, "speech_text", "answer", "text", "reply");
                detail = firstNonEmpty(res, "detail_text", "detail", "full_text");
                JSONArray segs = res.optJSONArray("segments");
                if (segs != null) out.segmentsJson = abbreviate(segs.toString(), MAX_RAW);
                JSONObject render = res.optJSONObject("render");
                if (render != null) {
                    if (out.renderDriver.isEmpty()) out.renderDriver = render.optString("driver", "");
                    if (out.lipSync.isEmpty()) out.lipSync = render.optString("lip_sync", "");
                    readAvatar(ctx, out, render.optJSONObject("avatar"));
                }
                if (out.errorText.isEmpty()) out.errorText = res.optString("error", "");
                if (out.voice.isEmpty()) out.voice = res.optString("voice", "");
            }
            if (speech == null) {
                speech = firstNonEmpty(j, "speech_text", "answer", "text", "reply",
                        "response", "message", "content");
            }
            if (detail == null) detail = firstNonEmpty(j, "detail_text", "detail");
            if (out.detailText.isEmpty() && detail != null) out.detailText = detail;
            if (out.answerText.isEmpty() && speech != null) out.answerText = cleanCore(speech);

            String core = speech != null ? cleanCore(speech) : "";
            if (core.isEmpty() && out.ackText.isEmpty() == false) core = cleanCore(out.ackText);
            if (core.isEmpty() && detail != null) core = cleanCore(detail);
            if (core.isEmpty()) {
                // 兜底：遍历顶层字符串字段，跳过系统字段
                StringBuilder b = new StringBuilder();
                java.util.Iterator<String> it = j.keys();
                while (it.hasNext()) {
                    String k = it.next();
                    if (isSystemKey(k)) continue;
                    Object v = j.opt(k);
                    if (v instanceof String && ((String) v).length() > 1) {
                        b.append(cleanCore((String) v)).append('\n');
                    }
                }
                core = b.toString().trim();
            }
            if (out.coreText.isEmpty()) out.coreText = core;
            // status=error：把服务端 error 字段作为可提示、可重试的核心内容
            String st = out.status == null ? "" : out.status.toLowerCase(Locale.US);
            if (("error".equals(st) || "failed".equals(st)) && !out.errorText.isEmpty()) {
                out.isError = true;
                out.coreText = cleanCore(out.errorText);
                out.answerText = out.coreText;
            }
            if (out.answerText.isEmpty()) out.answerText = out.coreText;
            if (out.messageId.isEmpty() && !out.jobId.isEmpty()) out.messageId = out.jobId;
        } catch (JSONException e) {
            if (out.coreText.isEmpty()) out.coreText = cleanCore(s);
        }
    }

    /**
     * 读取 avatar 对象（voice_chat / voice_job / render.avatar）。
     * 素材是相对路径时，只按服务端 asset_url_base 或 MCP 同源地址解析，不接其它 URL。
     */
    private static void readAvatar(Context ctx, McpResult out, JSONObject avatar) {
        if (avatar == null) return;
        if (out.avatarId.isEmpty()) out.avatarId = avatar.optString("id", "");
        if (out.employeeName.isEmpty()) {
            String n = firstNonEmpty(avatar, "name", "role", "id");
            out.employeeName = n == null ? "" : n;
        }
        if (out.employeeRole.isEmpty()) out.employeeRole = avatar.optString("role", "");
        if (out.faceUrl.isEmpty()) {
            out.faceUrl = AssetUrls.resolve(ctx, avatar.optString("face_url", ""));
        }
        Object photo = avatar.opt("photo");
        if (out.photoUrl.isEmpty()) {
            if (photo instanceof JSONObject) {
                out.photoUrl = AssetUrls.resolve(ctx, ((JSONObject) photo).optString("url", ""));
            } else if (photo instanceof String) {
                out.photoUrl = AssetUrls.resolve(ctx, (String) photo);
            }
        }
        if (out.videoUrl.isEmpty()) {
            JSONObject videos = avatar.optJSONObject("videos");
            if (videos != null) {
                JSONObject talk = videos.optJSONObject("talk");
                if (talk == null) talk = videos.optJSONObject("idle");
                if (talk != null) out.videoUrl = AssetUrls.resolve(ctx, talk.optString("url", ""));
            } else {
                String direct = firstNonEmpty(avatar, "videoTalk", "video_talk", "videoIdle");
                if (direct != null) out.videoUrl = AssetUrls.resolve(ctx, direct);
            }
        }
    }

    private static boolean isSystemKey(String k) {
        if (k == null) return true;
        String s = k.toLowerCase(Locale.US);
        String[] keys = {"job_id", "status", "created_at", "started_at", "finished_at",
                "callback_url", "delegate", "asr", "mode", "avatar", "ack", "render",
                "audio_path", "srt_path", "segments_path", "wav_path", "json_path",
                "out_dir", "sample_rate", "duration", "audio_mime", "has_video",
                "voice", "iserror", "segments", "rtf", "model_load_sec", "synth_sec"};
        for (String x : keys) if (s.equals(x)) return true;
        return s.endsWith("_path") || s.endsWith("_url") || s.endsWith("_sec")
                || s.endsWith("_at") || s.equals("error");
    }

    /** 过滤系统提示 / 服务端路径 / 音频文件名，只留能读的核心内容 */
    public static String cleanCore(String text) {
        if (text == null) return "";
        String[] lines = text.replace("\r", "").split("\n");
        StringBuilder b = new StringBuilder();
        for (String line : lines) {
            String t = line.trim();
            if (t.isEmpty()) continue;
            if (looksLikeNoise(t)) continue;
            b.append(t).append('\n');
        }
        return b.toString().trim();
    }

    private static boolean looksLikeNoise(String t) {
        String lower = t.toLowerCase(Locale.US);
        // 含服务端文件路径 / 音频、字幕、视频文件名
        if (lower.matches(".*[\\w./\\\\-]+\\.(wav|mp3|m4a|ogg|flac|srt|json|txt|png|jpg|jpeg|mp4|webm|m4v)\\b.*")) {
            return true;
        }
        if (lower.startsWith("/opt/") || lower.startsWith("/home/") || lower.startsWith("file://")) {
            return true;
        }
        String[] prefixes = {"识别到的指令：", "回复内容：", "执行结果：", "音频时长", "识别信息：",
                "分句文本：", "资源路径：", "audio_path", "srt_path", "segments_path",
                "job_id", "status", "created_at", "finished_at", "sample_rate", "out_dir",
                "识别到的指令:", "回复内容:", "audio时长"};
        for (String p : prefixes) if (lower.startsWith(p.toLowerCase(Locale.US))) return true;
        if (t.matches("^[\\{\\}\\[\\]\",:\\s]+$")) return true;
        return false;
    }

    /** 附件里保存的「文字详情」文件：干净的核心内容 + 详细正文，不含原始 JSON 系统字段 */
    private static String readableTextFile(McpResult out, String original) {
        StringBuilder b = new StringBuilder();
        if (!out.questionText.isEmpty()) b.append("指令：").append(out.questionText).append("\n\n");
        if (!out.coreText.isEmpty()) b.append(out.coreText).append('\n');
        if (!out.detailText.isEmpty() && !out.detailText.equals(out.coreText)) {
            b.append("\n—— 详细内容 ——\n").append(out.detailText).append('\n');
        }
        if (b.toString().trim().isEmpty()) b.append(cleanCore(original));
        if (b.toString().trim().isEmpty()) b.append(original);
        return b.toString();
    }

    /** 兼容旧版：把结构化 JSON 展开成带标签的文本，仅用于历史 raw 展示 */
    private static String humanize(String text) {
        String s = text == null ? "" : text.trim();
        if (!s.startsWith("{")) return s;
        try {
            JSONObject j = new JSONObject(s);
            StringBuilder b = new StringBuilder();
            String q = firstNonEmpty(j, "question", "asr_text", "transcript");
            String a = firstNonEmpty(j, "answer", "text", "reply", "response", "message");
            if (q != null) b.append("指令：").append(q).append('\n');
            if (a != null) b.append("回复：").append(a).append('\n');
            JSONObject res = j.optJSONObject("result");
            if (res != null) {
                String sp = firstNonEmpty(res, "speech_text", "answer", "text");
                String dt = firstNonEmpty(res, "detail_text", "detail");
                if (sp != null) b.append("播报：").append(sp).append('\n');
                if (dt != null) b.append("详细：").append(dt).append('\n');
            }
            return b.length() == 0 ? s : b.toString().trim();
        } catch (JSONException e) {
            return s;
        }
    }

    private static String firstNonEmpty(JSONObject j, String... keys) {
        if (j == null) return null;
        for (String k : keys) {
            String v = j.optString(k, "");
            if (v != null && !v.trim().isEmpty()) return v.trim();
        }
        return null;
    }

    private static void handleBinary(Context ctx, McpResult out, JSONObject block,
                                     String type, int index, boolean persist) {
        String data = block.optString("data", block.optString("blob", ""));
        String defaultMime = "audio".equals(type) ? "audio/wav"
                : "video".equals(type) ? "video/mp4" : "image/png";
        String mime = block.optString("mimeType", defaultMime);
        if (!persist) {
            // 只登记类型，供「是否已完成」判断使用
            McpResult.Item stub = new McpResult.Item("", mime, null, null, 0);
            stub.label = ResultStore.labelFor(mime);
            out.files.add(stub);
            return;
        }
        byte[] bytes = decodeBase64(data);
        if (bytes == null || bytes.length == 0) return;
        String ext = ResultStore.extensionForMime(mime);
        addFile(ctx, out, "result_" + (index + 1) + "." + ext, bytes, mime);
    }

    private static void handleResource(Context ctx, McpResult out, JSONObject block,
                                       StringBuilder raw, StringBuilder legacy, int index,
                                       boolean persist) {
        JSONObject r = block.optJSONObject("resource");
        if (r == null) return;
        String text = r.optString("text", "");
        String blob = r.optString("blob", "");
        String mime = r.optString("mimeType", "text/plain");
        if (!text.isEmpty()) {
            raw.append(text).append('\n');
            digestText(ctx, out, text);
            if (persist) {
                addFile(ctx, out, "result_resource_" + (index + 1) + ".txt",
                        readableTextFile(out, text).getBytes(StandardCharsets.UTF_8), "text/plain");
            }
        } else if (!blob.isEmpty() && persist) {
            byte[] bytes = decodeBase64(blob);
            if (bytes != null && bytes.length > 0) {
                addFile(ctx, out, "result_resource_" + (index + 1) + "."
                        + ResultStore.extensionForMime(mime), bytes, mime);
            }
        }
    }

    private static byte[] decodeBase64(String value) {
        if (value == null || value.isEmpty()) return null;
        String s = value.trim();
        if (s.startsWith("data:")) {
            int comma = s.indexOf(',');
            if (comma > 0) s = s.substring(comma + 1);
        }
        try {
            return Base64.decode(s, Base64.DEFAULT);
        } catch (IllegalArgumentException e) {
            return null;
        }
    }

    private static void addTextFile(Context ctx, McpResult out, String name, String text) {
        addFile(ctx, out, name, text.getBytes(StandardCharsets.UTF_8), "text/plain");
    }

    private static void addFile(Context ctx, McpResult out, String name, byte[] data, String mime) {
        ResultStore.Saved saved = ResultStore.savePermanent(ctx, name, data, mime);
        if (saved != null) {
            McpResult.Item item = new McpResult.Item(saved.name, mime,
                    saved.file.getAbsolutePath(), saved.uri, saved.file.length());
            item.label = ResultStore.labelFor(mime);
            out.files.add(item);
        }
    }

    private static void addFile(Context ctx, McpResult out, String name, String text, String mime) {
        addFile(ctx, out, name, text.getBytes(StandardCharsets.UTF_8), mime);
    }

    private static String abbreviate(String s, int max) {
        if (s == null) return "";
        return s.length() > max ? s.substring(0, max) + "…" : s;
    }

    private static String hash(String s) {
        return "m" + Integer.toHexString(s == null ? 0 : s.hashCode());
    }
}
