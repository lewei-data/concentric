package com.boray.voicemcp;

import android.content.Context;

import org.json.JSONObject;

import java.io.ByteArrayOutputStream;
import java.io.InputStream;
import java.io.OutputStream;
import java.net.HttpURLConnection;
import java.net.URL;
import java.nio.charset.StandardCharsets;

/**
 * 调度中心（dispatch）HTTP 层：提交 / 查询 / 取消任务。
 *
 * <p>网络层设计（0.9.6 起）：<b>公网优先，失败切内网</b>。
 * <ul>
 *   <li>公网：{@code https://ai.boraydata.com/conring/dispatch}</li>
 *   <li>内网：{@code http://192.168.31.206:9120}（dispatchd 直连端口，需同一局域网）</li>
 * </ul>
 * 记住上一次成功的线路并优先复用，避免每次都要等一次超时。
 *
 * <p>契约要点（实测 2026-11-11，dispatch v0.3）：
 * <ul>
 *   <li>P0 实时通道必须显式带 {@code channel:"ws"}，否则只给 passthrough，
 *       响应里没有 {@code realtime_url}；</li>
 *   <li>{@code source:"boss"} + {@code modality:"voice"} 才进 P0 立即响应 lane；</li>
 *   <li>响应形如
 *       {@code {"task_id":"rt_..","channel_id":"ch_..","channel_kind":"ws",
 *       "realtime_url":"wss://.../channel/<channel_id>"}}，
 *       把 {@code <channel_id>} 换成真实 channel_id 即得 WS 地址。</li>
 * </ul>
 */
public final class DispatchApi {

    /** 公网（生产） */
    public static final String PUBLIC_BASE = "https://ai.boraydata.com/conring/dispatch";
    /** 内网直连（dispatchd 监听 9120；WS 在 9121） */
    public static final String LAN_BASE = "http://192.168.31.206:9120";
    public static final String LAN_WS_BASE = "ws://192.168.31.206:9121";

    /** 0=未知（先公网） 1=公网 2=内网 */
    private static volatile int preferred = 0;

    private DispatchApi() {
    }

    public static String publicBase(Context c) {
        if (c == null) return PUBLIC_BASE;          // 离线探针/校验用
        McpConfig cfg = McpConfig.load(c);
        return cfg.dispatchUrl == null || cfg.dispatchUrl.isEmpty() ? PUBLIC_BASE : cfg.dispatchUrl;
    }

    public static String lanBase(Context c) {
        if (c == null) return LAN_BASE;
        McpConfig cfg = McpConfig.load(c);
        return cfg.dispatchUrlLan == null || cfg.dispatchUrlLan.isEmpty() ? LAN_BASE : cfg.dispatchUrlLan;
    }

    public static String lanWsBase(Context c) {
        if (c == null) return LAN_WS_BASE;
        McpConfig cfg = McpConfig.load(c);
        return cfg.dispatchWsLan == null || cfg.dispatchWsLan.isEmpty() ? LAN_WS_BASE : cfg.dispatchWsLan;
    }

    public static boolean onLan() {
        return preferred == 2;
    }

    // ------------------------------------------------------------------ 数据结构

    /** /submit 的响应；ok=false 时 message 带原因（可直接显示） */
    public static final class Submit {
        public boolean ok = false;
        public String taskId = "";
        public String channelId = "";
        public String channelKind = "";
        public String realtimeUrl = "";
        public String lane = "";
        public String priority = "";
        public String message = "";
        /** 服务端 0.11.2：凭 session_key / channel_id 接回了旧会话 */
        public boolean resumed = false;
        /** 这条响应是不是走内网拿到的 */
        public boolean lan = false;

        public boolean wsReady() {
            return ok && "ws".equalsIgnoreCase(channelKind)
                    && !channelId.isEmpty() && !realtimeUrl.isEmpty();
        }
    }

    /** 任务状态（/task/<id>） */
    public static final class Task {
        public String taskId = "";
        public String status = "";
        public String error = "";
        public String channelId = "";
        /** 执行结果（passthrough / 排队任务才有；WS 会话在 utterances 里） */
        public String result = "";

        public boolean finished() {
            return "done".equals(status) || "failed".equals(status)
                    || "cancelled".equals(status);
        }
    }

    // ------------------------------------------------------------------ 提交

    /**
     * 提交一条 P0 实时语音任务（channel=ws）。
     *
     * @param text 任务文案。实时会话本身由 WS 上行语音驱动，这里的 text 只进台账，
     *             但服务端要求非空，所以给一句占位。
     */
    public static Submit submit(Context c, String profile, String voice, String text) {
        return submit(c, profile, voice, text, "voice", "ws");
    }

    /**
     * 通用提交。
     *
     * @param modality voice / video，决定 P0 lane 与话术
     * @param channel  "ws" = 双向实时通道；null/空 = passthrough（只跑一轮，结果回 /task/<id>）
     */
    public static Submit submit(Context c, String profile, String voice, String text,
                                String modality, String channel) {
        JSONObject body = new JSONObject();
        try {
            body.put("text", text == null || text.trim().isEmpty() ? "语音通话" : text.trim());
            body.put("source", "boss");
            body.put("modality", modality == null || modality.isEmpty() ? "voice" : modality);
            if (channel != null && !channel.isEmpty()) body.put("channel", channel);
            body.put("timeout_sec", 600);
            // 0.11.2 服务端新增 session_key：同一账号断线重连 / 换通道重提可自动 resume，
            // 不再每挂一次就新建一个 hermes 会话（响应带 resumed:true）。
            if (c != null) {
                String account = AuthSession.phone(c);
                if (!account.isEmpty()) body.put("session_key", account);
            }
            if (profile != null && !profile.isEmpty()) body.put("profile", profile);
            if (voice != null && !voice.isEmpty()) body.put("voice", voice);
        } catch (Exception ignored) {
        }

        String[][] routes = routes(c);
        Submit last = new Submit();
        for (String[] r : routes) {
            boolean lan = "2".equals(r[0]);
            String base = r[1];
            try {
                // 内网只在局域网内有路由，公网现场等 6s 纯属浪费，3s 足够判定
                String raw = post(base + "/submit", body.toString(), lan ? 3000 : 6000, 25000);
                Submit s = parseSubmit(raw);
                s.lan = lan;
                if (s.ok) {
                    preferred = lan ? 2 : 1;
                    return s;
                }
                last = s;
            } catch (Exception e) {
                last.ok = false;
                last.lan = lan;
                last.message = "调度中心不可达：" + shortMsg(e);
            }
        }
        if (preferred == 0) preferred = 1;
        return last;
    }

    /** 公网优先；上一次走内网成功则内网优先，缩短重连等待 */
    private static String[][] routes(Context c) {
        // 后台服务冷启动时静态缓存是空的，趁有 Context 补一次会话 Cookie
        if (c != null) AuthSession.prime(c);
        String pub = publicBase(c);
        String lan = lanBase(c);
        if (preferred == 2) return new String[][]{{"2", lan}, {"1", pub}};
        return new String[][]{{"1", pub}, {"2", lan}};
    }

    public static Submit parseSubmit(String raw) {
        Submit s = new Submit();
        if (raw == null || raw.trim().isEmpty()) {
            s.message = "调度中心返回为空";
            return s;
        }
        try {
            JSONObject j = new JSONObject(raw.trim());
            if (j.has("error")) {
                s.message = j.optString("error", "提交失败");
                return s;
            }
            s.taskId = j.optString("task_id", "");
            s.channelId = j.optString("channel_id", "");
            s.channelKind = j.optString("channel_kind", "");
            s.realtimeUrl = j.optString("realtime_url", "");
            s.lane = j.optString("lane", "");
            s.priority = j.optString("priority", "");
            s.resumed = j.optBoolean("resumed", false);
            s.ok = !s.taskId.isEmpty();
            if (!s.ok) s.message = "提交失败：没有 task_id";
            else if (!"ws".equalsIgnoreCase(s.channelKind)) {
                s.message = "服务端未建立 ws 通道（channel_kind=" + s.channelKind + "）";
            } else if (s.realtimeUrl.isEmpty()) {
                s.message = "服务端未返回 realtime_url";
            }
        } catch (Exception e) {
            s.message = "响应不是合法 JSON：" + raw.substring(0, Math.min(120, raw.length()));
        }
        return s;
    }

    // ------------------------------------------------------------------ WS 地址

    /**
     * 把 submit 响应里的模板换成真实 WS 地址。
     *
     * @param lan true=强制走内网直连（公网连不上时的备选）
     * @return 合法 ws:// / wss:// 地址；模板非法则返回 ""
     */
    public static String wsUrl(Context c, Submit s, boolean lan) {
        return wsUrl(lanWsBase(c), s, lan);
    }

    /**
     * 由公网 dispatch 地址推导公网 WS 地址。
     *
     * 服务端 `realtime_url` 可能因为 DISPATCH_WS_PUBLIC_URL 没配而回成局域网
     * （`ws://192.168.x.x:9121/...`），外网手机连不上；这里按同源规则自己拼一份：
     * `https://host/conring/dispatch` → `wss://host/conring/dispatch-ws/channel/<id>`。
     *
     * @return 推导出的地址；无法推导返回 ""
     */
    public static String publicWsUrl(Context c, Submit s) {
        if (s == null || s.channelId.isEmpty()) return "";
        String base = publicBase(c);
        int i = base.indexOf("://");
        if (i < 0) return "";
        String scheme = base.substring(0, i).toLowerCase(java.util.Locale.ROOT);
        String rest = base.substring(i + 3);
        String wsScheme = "https".equals(scheme) ? "wss" : "ws";
        while (rest.endsWith("/")) rest = rest.substring(0, rest.length() - 1);
        if (rest.endsWith("/dispatch")) {
            rest = rest.substring(0, rest.length() - "/dispatch".length());
        }
        return wsScheme + "://" + rest + "/dispatch-ws/channel/" + s.channelId;
    }

    /** 纯逻辑版本（不依赖 Context），供 JVM 校验直接调用 */
    public static String wsUrl(String lanWsBase, Submit s, boolean lan) {
        if (s == null || s.channelId.isEmpty()) return "";
        if (lan) {
            String base = lanWsBase == null ? "" : lanWsBase.trim();
            if (base.isEmpty()) return "";
            return base.endsWith("/") ? base + "channel/" + s.channelId
                    : base + "/channel/" + s.channelId;
        }
        String tpl = s.realtimeUrl;
        if (tpl == null || tpl.isEmpty()) return "";
        String url = tpl.replace("<channel_id>", s.channelId);
        if (!url.startsWith("ws://") && !url.startsWith("wss://")) return "";
        return url;
    }

    // ------------------------------------------------------------------ 任务

    public static Task task(Context c, String taskId) {
        Task t = new Task();
        t.taskId = taskId == null ? "" : taskId;
        if (t.taskId.isEmpty()) return t;
        String[][] routes = routes(c);
        for (String[] r : routes) {
            try {
                String raw = get(r[1] + "/task/" + t.taskId, 5000, 8000);
                JSONObject j = new JSONObject(raw);
                t.status = j.optString("status", "");
                t.error = j.optString("error", "");
                t.channelId = j.optString("channel_id", "");
                t.result = j.optString("result", "");
                if (!t.status.isEmpty()) {
                    preferred = "2".equals(r[0]) ? 2 : 1;
                    return t;
                }
            } catch (Exception ignored) {
            }
        }
        return t;
    }

    /** 取消排队中的任务（P0 实时任务已在执行时会返回 409，属正常） */
    public static boolean cancel(String taskId) {
        if (taskId == null || taskId.isEmpty()) return false;
        for (String base : new String[]{PUBLIC_BASE, LAN_BASE}) {
            try {
                post(base + "/task/" + taskId + "/cancel", "{}", 4000, 6000);
                return true;
            } catch (Exception ignored) {
            }
        }
        return false;
    }

    /**
     * 提交一轮指令并等结果（passthrough，不建 WS）。
     *
     * 用于「HLS 只做渲染」的场景：指令进 dispatch（P0 任务 + 台账 + 交付物），
     * 拿回文字后再用 live_call say 渲染进直播流。
     *
     * @return 结果文字；失败/超时返回 ""
     */
    public static String ask(Context c, String profile, String modality, String text,
                             long timeoutMs) {
        Submit s = submit(c, profile, "", text, modality, null);
        if (!s.ok) return "";
        long deadline = System.currentTimeMillis() + Math.max(5000, timeoutMs);
        while (System.currentTimeMillis() < deadline) {
            Task t = task(c, s.taskId);
            if ("done".equals(t.status)) return t.result == null ? "" : t.result.trim();
            if ("failed".equals(t.status) || "cancelled".equals(t.status)) return "";
            try {
                Thread.sleep(700);
            } catch (InterruptedException e) {
                Thread.currentThread().interrupt();
                return "";
            }
        }
        cancel(s.taskId);
        return "";
    }

    /** /health：判断公网/内网哪条线路当前可用 */
    public static String health(Context c) {
        if (c != null) AuthSession.prime(c);
        for (String base : new String[]{publicBase(c), lanBase(c)}) {
            try {
                return get(base + "/health", 3000, 4000);
            } catch (Exception ignored) {
            }
        }
        return "";
    }

    // ------------------------------------------------------------------ HTTP

    private static String get(String url, int connectMs, int readMs) throws Exception {
        HttpURLConnection conn = (HttpURLConnection) new URL(url).openConnection();
        conn.setRequestMethod("GET");
        conn.setConnectTimeout(connectMs);
        conn.setReadTimeout(readMs);
        conn.setRequestProperty("Accept", "application/json");
        String cookie = AuthSession.cachedHeader();
        if (!cookie.isEmpty()) conn.setRequestProperty("Cookie", cookie);
        try (InputStream in = conn.getInputStream()) {
            return readAll(in);
        } finally {
            conn.disconnect();
        }
    }

    private static String post(String url, String json, int connectMs, int readMs) throws Exception {
        HttpURLConnection conn = (HttpURLConnection) new URL(url).openConnection();
        conn.setRequestMethod("POST");
        conn.setConnectTimeout(connectMs);
        conn.setReadTimeout(readMs);
        conn.setDoOutput(true);
        conn.setRequestProperty("Content-Type", "application/json; charset=utf-8");
        conn.setRequestProperty("Accept", "application/json");
        String cookie = AuthSession.cachedHeader();
        if (!cookie.isEmpty()) conn.setRequestProperty("Cookie", cookie);
        byte[] body = json.getBytes(StandardCharsets.UTF_8);
        conn.setFixedLengthStreamingMode(body.length);
        try (OutputStream os = conn.getOutputStream()) {
            os.write(body);
        }
        int code = conn.getResponseCode();
        InputStream in = code >= 400 ? conn.getErrorStream() : conn.getInputStream();
        String text = in == null ? "" : readAll(in);
        conn.disconnect();
        if (code >= 500) throw new java.io.IOException("HTTP " + code);
        return text;
    }

    private static String readAll(InputStream in) throws Exception {
        ByteArrayOutputStream bos = new ByteArrayOutputStream();
        byte[] buf = new byte[4096];
        int n;
        while ((n = in.read(buf)) > 0) bos.write(buf, 0, n);
        return new String(bos.toByteArray(), StandardCharsets.UTF_8);
    }

    private static String shortMsg(Exception e) {
        String m = e.getMessage();
        if (m == null || m.isEmpty()) m = e.getClass().getSimpleName();
        return m.length() > 60 ? m.substring(0, 60) : m;
    }
}
