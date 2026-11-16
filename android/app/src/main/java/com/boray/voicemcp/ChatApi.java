package com.boray.voicemcp;

import android.content.Context;

import org.json.JSONArray;
import org.json.JSONObject;

import java.io.ByteArrayOutputStream;
import java.io.File;
import java.io.FileOutputStream;
import java.io.InputStream;
import java.io.OutputStream;
import java.net.HttpURLConnection;
import java.net.URL;
import java.net.URLEncoder;
import java.nio.charset.StandardCharsets;
import java.util.ArrayList;
import java.util.Collections;
import java.util.List;

/**
 * 小秘书对话通道（工作台 /api/chat/*、/api/asr）。
 *
 * <p>与 Web 桌面精灵<b>共用同一条流水线</b>：消息落 {@code chat_messages}（线程 {@link #THREAD}），
 * 由 secretary agent 处理，回复带 TTS 语音并经 SSE {@code chat.message} 推送。
 *
 * <p>APP 语音模式的链路（飞书式按住说话）：
 * <ol>
 *   <li>按住录音 → 松开本地得到 16k 单声道 wav；</li>
 *   <li>{@code POST /api/asr?ext=wav}                          ← 只识别，不建线程、不触发回复；</li>
 *   <li>老板选「发送文字」→ {@code POST /api/chat/send}；</li>
 *   <li>老板选「发语音」  → {@code POST /api/chat/voice?text=<已识别文本>}（音频留档，不再重复 ASR）；</li>
 *   <li>轮询 {@code GET /api/chat/messages?thread=app:secretary} 取小秘书回复 + 语音地址。</li>
 * </ol>
 */
public final class ChatApi {

    /** 消息来源（服务端 source 字段）：app = 手机端语音模式 */
    public static final String SOURCE = "app";
    /** 与 Web 精灵面板合并展示的 APP 线程 */
    public static final String THREAD = "app:secretary";

    private static final int CONNECT_MS = 8000;
    private static final int ASR_READ_MS = 60000;      // ASR 一般 1~5 秒，留足余量
    private static final int CHAT_READ_MS = 20000;
    private static final int POLL_READ_MS = 10000;

    private ChatApi() {
    }

    // ------------------------------------------------------------------ 数据结构

    /** ASR 结果 */
    public static final class Asr {
        public boolean ok = false;
        public String transcript = "";
        /** 服务端留档的音频地址（/chat-files/xxx.wav），发语音时后端自己也会存，这里只做展示备用 */
        public String audio = "";
        public String error = "";
    }

    /** 发送结果 */
    public static final class Sent {
        public boolean ok = false;
        public String threadId = "";
        public String transcript = "";
        public String error = "";
    }

    /** 线程里的一条消息 */
    public static final class Msg {
        public long id;
        public boolean outbound;
        public String content = "";
        /** 服务端 TTS 语音（/chat-files/xxx.wav），没有就是空串 */
        public String audio = "";
        public String sender = "";
        /** ack=1 的是「收到，老板」这类即时回执，正式回复不带 */
        public boolean ack;
    }

    // ------------------------------------------------------------------ 识别

    /** 只识别：把本地 wav 交给服务端 ASR，返回文字，不产生消息 */
    public static Asr transcribe(Context c, File wav) {
        Asr r = new Asr();
        if (wav == null || !wav.isFile() || wav.length() < 2000) {
            r.error = "录音太短，请按住多说一会";
            return r;
        }
        HttpURLConnection conn = null;
        try {
            byte[] body = readFile(wav);
            conn = open(c, base(c) + "/api/asr?ext=wav&source=" + SOURCE, "POST", ASR_READ_MS + 40000);
            conn.setRequestProperty("Content-Type", "audio/wav");
            conn.setFixedLengthStreamingMode(body.length);
            try (OutputStream os = conn.getOutputStream()) {
                os.write(body);
            }
            int code = conn.getResponseCode();
            String text = readBody(conn, code);
            JSONObject j = parse(text);
            if (code != 200 || j == null || !j.optBoolean("ok", false)) {
                r.error = errorOf(j, text, "识别失败（HTTP " + code + "）");
                return r;
            }
            r.transcript = j.optString("transcript", "").trim();
            r.audio = j.optString("audio", "");
            r.ok = !r.transcript.isEmpty();
            if (!r.ok) r.error = "没有识别到语音内容";
            return r;
        } catch (Exception e) {
            r.error = "识别失败：" + shortMsg(e);
            return r;
        } finally {
            if (conn != null) conn.disconnect();
        }
    }

    // ------------------------------------------------------------------ 发送

    /** 发文字（识别后的文本）：走 /api/chat/send，和小秘书线程完全一致 */
    public static Sent sendText(Context c, String text) {
        Sent r = new Sent();
        String t = text == null ? "" : text.trim();
        if (t.isEmpty()) {
            r.error = "内容为空";
            return r;
        }
        HttpURLConnection conn = null;
        try {
            JSONObject body = new JSONObject();
            body.put("text", t);
            body.put("source", SOURCE);
            conn = open(c, base(c) + "/api/chat/send", "POST", CHAT_READ_MS);
            conn.setRequestProperty("Content-Type", "application/json; charset=utf-8");
            writeJson(conn, body.toString());
            int code = conn.getResponseCode();
            String raw = readBody(conn, code);
            JSONObject j = parse(raw);
            if (code != 200 || j == null || !j.optBoolean("ok", false)) {
                r.error = errorOf(j, raw, "发送失败（HTTP " + code + "）");
                return r;
            }
            r.ok = true;
            r.threadId = j.optString("threadId", THREAD);
            r.transcript = t;
            return r;
        } catch (Exception e) {
            r.error = "发送失败：" + shortMsg(e);
            return r;
        } finally {
            if (conn != null) conn.disconnect();
        }
    }

    /**
     * 发语音：音频原样上传（服务端留档，Web/APP 会话里能看到），
     * 同时把已经识别好的文字带上，避免服务端再跑一次 ASR。
     */
    public static Sent sendVoice(Context c, File wav, String transcript) {
        Sent r = new Sent();
        if (wav == null || !wav.isFile() || wav.length() < 2000) {
            r.error = "录音太短，请按住多说一会";
            return r;
        }
        HttpURLConnection conn = null;
        try {
            byte[] body = readFile(wav);
            String t = transcript == null ? "" : transcript.trim();
            StringBuilder url = new StringBuilder(base(c))
                    .append("/api/chat/voice?ext=wav&source=").append(SOURCE);
            if (!t.isEmpty()) {
                url.append("&text=").append(URLEncoder.encode(t, "UTF-8"));
            }
            conn = open(c, url.toString(), "POST", ASR_READ_MS + 40000);
            conn.setRequestProperty("Content-Type", "audio/wav");
            conn.setFixedLengthStreamingMode(body.length);
            try (OutputStream os = conn.getOutputStream()) {
                os.write(body);
            }
            int code = conn.getResponseCode();
            String raw = readBody(conn, code);
            JSONObject j = parse(raw);
            if (code != 200 || j == null || !j.optBoolean("ok", false)) {
                r.error = errorOf(j, raw, "发送失败（HTTP " + code + "）");
                return r;
            }
            r.ok = true;
            r.threadId = j.optString("threadId", THREAD);
            r.transcript = j.optString("transcript", t);
            return r;
        } catch (Exception e) {
            r.error = "发送失败：" + shortMsg(e);
            return r;
        } finally {
            if (conn != null) conn.disconnect();
        }
    }

    // ------------------------------------------------------------------ 回复

    /** 线程最新一条消息的 id（发消息前记基线，之后只看比它新的） */
    public static long latestId(Context c) {
        List<Msg> rows = messages(c, 0);
        long max = 0;
        for (Msg m : rows) if (m.id > max) max = m.id;
        return max;
    }

    /**
     * 拉线程最近的若干条消息（服务端按 id 倒序返回，这里统一改成时间正序）。
     *
     * @param afterId 只要比它新的；0 = 不过滤
     */
    public static List<Msg> messages(Context c, long afterId) {
        List<Msg> out = new ArrayList<>();
        HttpURLConnection conn = null;
        try {
            String url = base(c) + "/api/chat/messages?thread="
                    + URLEncoder.encode(THREAD, "UTF-8") + "&limit=20";
            conn = open(c, url, "GET", POLL_READ_MS);
            int code = conn.getResponseCode();
            String raw = readBody(conn, code);
            JSONObject j = parse(raw);
            if (code != 200 || j == null) return out;
            JSONArray arr = j.optJSONArray("messages");
            if (arr == null) return out;
            for (int i = 0; i < arr.length(); i++) {
                JSONObject o = arr.optJSONObject(i);
                if (o == null) continue;
                Msg m = new Msg();
                m.id = o.optLong("id", 0);
                if (m.id <= afterId) continue;
                m.outbound = "outbound".equalsIgnoreCase(o.optString("direction", ""));
                m.content = o.optString("content", "");
                m.sender = o.optString("sender_name", "");
                JSONObject meta = o.optJSONObject("meta");
                if (meta == null) {
                    // 有的库里 meta 存的是 JSON 字符串，服务端不一定帮我们解过
                    String s = o.optString("meta", "");
                    if (!s.isEmpty()) meta = parse(s);
                }
                if (meta != null) {
                    m.audio = meta.optString("audio", "");
                    m.ack = meta.optInt("ack", 0) == 1;
                }
                out.add(m);
            }
        } catch (Exception ignored) {
        } finally {
            if (conn != null) conn.disconnect();
        }
        Collections.sort(out, (a, b) -> Long.compare(a.id, b.id));
        return out;
    }

    /** 把服务端的 /chat-files/xxx.wav 下载到本地缓存（播放需要带登录 Cookie，MediaPlayer 直连做不到） */
    public static File download(Context c, String path, File dir) {
        if (path == null || path.isEmpty()) return null;
        HttpURLConnection conn = null;
        try {
            String url = path.startsWith("http") ? path : base(c) + path;
            conn = open(c, url, "GET", CHAT_READ_MS);
            int code = conn.getResponseCode();
            if (code != 200) return null;
            String name = path.substring(path.lastIndexOf('/') + 1);
            if (name.isEmpty()) name = "reply.wav";
            File out = new File(dir, name);
            try (InputStream in = conn.getInputStream();
                 FileOutputStream fos = new FileOutputStream(out)) {
                byte[] buf = new byte[8192];
                int n;
                while ((n = in.read(buf)) > 0) fos.write(buf, 0, n);
            }
            return out.isFile() && out.length() > 0 ? out : null;
        } catch (Exception e) {
            return null;
        } finally {
            if (conn != null) conn.disconnect();
        }
    }

    // ------------------------------------------------------------------ HTTP 基础

    private static String base(Context c) {
        String b = AuthSession.base(c);
        while (b.endsWith("/")) b = b.substring(0, b.length() - 1);
        return b;
    }

    /** 统一带上登录会话 Cookie（/api/asr、/api/chat/* 都是「仅老板」接口） */
    private static HttpURLConnection open(Context c, String url, String method, int readMs)
            throws Exception {
        HttpURLConnection conn = (HttpURLConnection) new URL(url).openConnection();
        conn.setRequestMethod(method);
        conn.setConnectTimeout(CONNECT_MS);
        conn.setReadTimeout(readMs);
        conn.setUseCaches(false);
        conn.setRequestProperty("Accept", "application/json");
        String cookie = AuthSession.header(c);
        if (!cookie.isEmpty()) conn.setRequestProperty("Cookie", cookie);
        if ("POST".equals(method)) conn.setDoOutput(true);
        return conn;
    }

    private static void writeJson(HttpURLConnection conn, String json) throws Exception {
        byte[] b = json.getBytes(StandardCharsets.UTF_8);
        conn.setFixedLengthStreamingMode(b.length);
        try (OutputStream os = conn.getOutputStream()) {
            os.write(b);
        }
    }

    private static String readBody(HttpURLConnection conn, int code) throws Exception {
        InputStream in = code >= 400 ? conn.getErrorStream() : conn.getInputStream();
        if (in == null) return "";
        try (InputStream src = in) {
            ByteArrayOutputStream bos = new ByteArrayOutputStream();
            byte[] buf = new byte[4096];
            int n;
            while ((n = src.read(buf)) > 0) bos.write(buf, 0, n);
            return new String(bos.toByteArray(), StandardCharsets.UTF_8);
        }
    }

    private static byte[] readFile(File f) throws Exception {
        byte[] data = new byte[(int) f.length()];
        try (InputStream in = new java.io.FileInputStream(f)) {
            int off = 0;
            int n;
            while (off < data.length && (n = in.read(data, off, data.length - off)) > 0) off += n;
        }
        return data;
    }

    private static JSONObject parse(String body) {
        if (body == null || body.trim().isEmpty()) return null;
        try {
            return new JSONObject(body.trim());
        } catch (Exception e) {
            return null;
        }
    }

    private static String errorOf(JSONObject j, String raw, String fallback) {
        String m = "";
        if (j != null) {
            m = j.optString("error", "");
            if (m.isEmpty()) m = j.optString("message", "");
        }
        if (m.isEmpty() && raw != null && !raw.trim().isEmpty()
                && raw.trim().length() < 160 && !raw.trim().startsWith("<")) {
            m = raw.trim();
        }
        if (m.isEmpty()) return fallback;
        // 把服务端的机读错误换成老板看得懂的话
        if ("forbidden".equalsIgnoreCase(m)) return "当前账号不是老板，语音模式只对老板开放";
        if ("unauthorized".equalsIgnoreCase(m)) return "登录已过期，请重新登录";
        return m;
    }

    private static String shortMsg(Exception e) {
        String m = e.getMessage();
        if (m == null || m.isEmpty()) m = e.getClass().getSimpleName();
        return m.length() > 80 ? m.substring(0, 80) : m;
    }
}
