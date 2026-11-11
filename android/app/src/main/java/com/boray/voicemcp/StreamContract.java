package com.boray.voicemcp;

import org.json.JSONArray;
import org.json.JSONObject;

import java.net.URL;

/**
 * v2.5 流式播放契约（纯逻辑，可脱离 Android 在 JVM 上校验）。
 *
 * 服务端 2026-11-10 已上线两项能力，实测契约：
 *   1) profile 每人返回
 *        introVideo     /conring/asr/assets/intro/<id>.mp4   音画合一 MP4（avc1+mp4a）
 *        introReady     true/false
 *        introText      自我介绍文案
 *        introDuration  实测时长（秒）
 *        liveCall       {supported:true, protocol:"hls", segment_sec:1.0}
 *   2) 工具
 *        intro_video {id|all, force, wait}   按需生成并缓存自我介绍视频
 *        live_call   {action: start|say|stop|status, session_id, avatar_id,
 *                     text, audio_base64, audio_path, voice, emotion, first_segment_sec}
 *        voice_chat  {..., live_session}     把「收到」与正式回复直接混进该路 HLS 流
 *
 * 选型：同源 HTTPS 443 + HLS。自我介绍为 MP4/HLS VOD 边下边播；实时通话为 1s 短分片
 * HLS（#EXT-X-PLAYLIST-TYPE:EVENT，持续追加，无 ENDLIST），音视频由 ffmpeg 混在同一
 * 分片里共用时间戳，MediaPlayer 自动同步，客户端不做两路时钟对齐。
 *
 * 安全约束（用户明确要求）：流地址只能来自服务端 profile / live_call 返回，且必须与
 * MCP 端点同源。任何外链 host、端口、CDN 一律拒绝，返回空串，调用方据此降级。
 */
public final class StreamContract {

    /** v2.5 实时通话工具（start/say/stop/status 由 action 区分） */
    public static final String TOOL_LIVE_CALL = "live_call";
    /** v2.5 自我介绍视频按需生成 */
    public static final String TOOL_INTRO_VIDEO = "intro_video";
    public static final String TOOL_ASR = "asr_transcribe";
    public static final String TOOL_VOICE_CHAT = "voice_chat";
    public static final String TOOL_PROFILE = "profile";
    public static final String TOOL_VOICE_JOB = "voice_job";

    public static final String ACTION_START = "start";
    public static final String ACTION_SAY = "say";
    public static final String ACTION_STOP = "stop";
    public static final String ACTION_STATUS = "status";

    private StreamContract() {
    }

    /** live_call 的返回，streamUrl 已是校验过的同源绝对地址 */
    public static final class CallSession {
        public String sessionId = "";
        public String streamUrl = "";
        public String status = "";
        public String protocol = "hls";
        public String sync = "";
        public String message = "";
        public boolean audioMuxed = true;
        public int segmentMs = 0;
        /** 员工侧电平快照地址（同源）；服务端 level_url_full，缺省时按 index.m3u8 → level.json 推导 */
        public String levelUrl = "";
        public int levelFps = 10;
        public double levelWindowSec = 0;
        /** 已推入流的总语音秒数（say 之后会增长，可用于「员工正在说」提示） */
        public double speechSeconds = 0;
        /** playlist 当前分片数 */
        public int segments = 0;

        public boolean ready() {
            return !streamUrl.isEmpty();
        }

        public boolean ended() {
            return "ended".equalsIgnoreCase(status) || "stop".equalsIgnoreCase(status);
        }
    }

    /** intro_video 返回里的单条自我介绍 */
    public static final class IntroClip {
        public String employeeId = "";
        public String url = "";
        public String text = "";
        public double duration = 0;
        public long size = 0;
        public boolean ready = false;
        public boolean cached = false;
        public String source = "";
    }

    /**
     * 把服务端返回的流地址解析成可播 URL。
     * 非同源、非法地址一律返回 ""，调用方据此降级。
     */
    public static String resolveStream(String mcpUrl, String serverBase, String spec) {
        String url = AssetUrls.resolveSpec(mcpUrl, serverBase, spec);
        if (url == null || url.isEmpty()) return "";
        if (!url.startsWith("http://") && !url.startsWith("https://")) return "";
        return sameOrigin(mcpUrl, url) ? url : "";
    }

    /** 只比较 scheme / host / 有效端口；路径与 query 不参与同源判断 */
    public static boolean sameOrigin(String mcpUrl, String url) {
        try {
            URL a = new URL(mcpUrl);
            URL b = new URL(url);
            return a.getProtocol().equalsIgnoreCase(b.getProtocol())
                    && a.getHost().equalsIgnoreCase(b.getHost())
                    && effectivePort(a) == effectivePort(b);
        } catch (Exception e) {
            return false;
        }
    }

    private static int effectivePort(URL u) {
        int p = u.getPort();
        if (p > 0) return p;
        return "https".equalsIgnoreCase(u.getProtocol()) ? 443 : 80;
    }

    /** 是否 HLS playlist（忽略 query / fragment） */
    public static boolean isHls(String url) {
        if (url == null) return false;
        String p = url;
        int q = p.indexOf('?');
        if (q >= 0) p = p.substring(0, q);
        int h = p.indexOf('#');
        if (h >= 0) p = p.substring(0, h);
        return p.toLowerCase().endsWith(".m3u8");
    }

    /** live 重试时给同源 URL 加一个无副作用的 cache-buster，仍是同一个地址 */
    public static String retryUrl(String url, int attempt) {
        if (url == null || url.isEmpty() || attempt <= 0) return url;
        return url + (url.contains("?") ? "&" : "?") + "_retry=" + attempt;
    }

    /** playlist 文本里已有可播分片？live 起流后首个分片可能还没落盘 */
    public static boolean hasSegment(String playlist) {
        if (playlist == null || playlist.isEmpty()) return false;
        if (!playlist.contains("#EXTINF")) return false;
        for (String line : playlist.split("\n")) {
            String s = line.trim();
            if (!s.isEmpty() && !s.startsWith("#")) return true;
        }
        return false;
    }

    /** playlist 是否已收尾（挂断后服务端会写 ENDLIST） */
    public static boolean isEnded(String playlist) {
        return playlist != null && playlist.contains("#EXT-X-ENDLIST");
    }

    /**
     * playlist 里所有分片的累计时长（毫秒）。
     *
     * 服务端的直播 playlist 是 EVENT 型、从 seg_0 开始累加，所以这就是「当前直播时间轴
     * 总长」，也就是直播边缘（live edge）。客户端据此可以从边缘起播、并在落后时追帧，
     * 否则会永远从第 0 片播起、越拉越远，看起来「不是实时」。
     */
    public static int playlistTotalMs(String playlist) {
        if (playlist == null || playlist.isEmpty()) return 0;
        double total = 0;
        for (String line : playlist.split("\n")) {
            String s = line.trim();
            if (!s.startsWith("#EXTINF:")) continue;
            int comma = s.indexOf(',');
            String num = (comma > 0 ? s.substring(8, comma) : s.substring(8)).trim();
            try {
                total += Double.parseDouble(num);
            } catch (Exception ignored) {
            }
        }
        return (int) Math.round(total * 1000);
    }

    /** 分片数量（用于判断直播是否刚起流、还没有可播内容） */
    public static int segmentCount(String playlist) {
        if (playlist == null || playlist.isEmpty()) return 0;
        int n = 0;
        for (String line : playlist.split("\n")) {
            String s = line.trim();
            if (!s.isEmpty() && !s.startsWith("#")) n++;
        }
        return n;
    }

    /** 从 live edge 往回留一点缓冲的起播位置，避免一秒就断 */
    public static int liveStartMs(String playlist, int keepEdgeMs) {
        int total = playlistTotalMs(playlist);
        if (total <= 0) return 0;
        return Math.max(0, total - Math.max(500, keepEdgeMs));
    }

    /**
     * 给同源视频地址加一个「内容版本」参数，保证服务端重新生成后不会命中旧缓存。
     *
     * 服务端把自我介绍固定写在 assets/intro/&lt;id&gt;.mp4，文件名不变；重新生成时
     * 时长 / 文案会变，用这两者派生版本号即可判断新旧。nginx 对 query 不敏感
     * （实测 206 正常），所以只是多一个缓存键，不改变资源本身。
     */
    public static String versioned(String url, double durationSec, String text) {
        if (url == null || url.isEmpty()) return "";
        String v = Long.toHexString(Math.round(durationSec * 100))
                + Integer.toHexString(text == null ? 0 : text.hashCode());
        return url + (url.contains("?") ? "&" : "?") + "v=" + v;
    }

    /**
     * 给同源地址加时间戳，避免中间层缓存住旧的 m3u8。
     * 服务端没设 Cache-Control，滑动窗口列表每秒都在变，缓存会让播放器一直读旧表。
     */
    public static String cacheBust(String url) {
        if (url == null || url.isEmpty()) return "";
        return url + (url.contains("?") ? "&" : "?") + "t=" + System.currentTimeMillis();
    }

    /**
     * 从 MCP 返回里的 live 对象读「这条流是否已经结束」。
     * 服务端约定：live.ended=true 表示通话已挂断，这段回复没混进流，得客户端自己播。
     */
    public static boolean liveEnded(JSONObject j) {
        if (j == null) return false;
        JSONObject live = j.optJSONObject("live");
        if (live == null) live = j.optJSONObject("stream");
        if (live == null) return false;
        return live.optBoolean("ended", false)
                || live.optBoolean("closed", false)
                || "ended".equalsIgnoreCase(live.optString("status", ""));
    }

    /** 从 MCP tools/call 的 result 里取第一个 text 内容块 */
    public static String textOf(JSONObject result) {
        if (result == null) return "";
        JSONArray content = result.optJSONArray("content");
        if (content == null) return "";
        StringBuilder b = new StringBuilder();
        for (int i = 0; i < content.length(); i++) {
            JSONObject block = content.optJSONObject(i);
            if (block == null) continue;
            if (!"text".equals(block.optString("type", ""))) continue;
            String t = block.optString("text", "");
            if (t.isEmpty()) continue;
            if (b.length() > 0) b.append('\n');
            b.append(t);
        }
        return b.toString();
    }

    /**
     * 解析 live_call(start / say / status) 返回。实测为扁平结构：
     *   {"session_id":"lv_040fd43e","status":"live","avatar_id":"ceo",
     *    "hls_url":"/conring/asr/assets/live/lv_040fd43e/index.m3u8",
     *    "segment_sec":1.0,"started_at":...,"elapsed":3.31,
     *    "speech_seconds":0.0,"segments":1,"sync":"音视频同流复用…","ready":true}
     * 同时兼容 {"data":{...}} / {"stream":{"url":...}} / {"session":{...}} 包装，
     * 以及纯文本错误回执（message 里带原文）。
     */
    public static CallSession parseCall(String mcpUrl, String serverBase, String text) {
        CallSession s = new CallSession();
        if (text == null || text.trim().isEmpty()) return s;
        String raw = text.trim();
        JSONObject root;
        try {
            root = new JSONObject(raw);
        } catch (Exception e) {
            s.message = raw.length() > 300 ? raw.substring(0, 300) : raw;
            return s;
        }

        JSONObject[] scopes = new JSONObject[]{
                root,
                root.optJSONObject("data"),
                root.optJSONObject("result"),
                root.optJSONObject("session"),
                root.optJSONObject("stream"),
                root.optJSONObject("call")
        };
        s.status = firstString(new JSONObject[]{root}, "status", "state");
        s.message = firstString(new JSONObject[]{root}, "message", "msg", "detail", "note");
        s.sessionId = firstString(scopes, "session_id", "call_id", "callId", "sessionId", "id");
        s.protocol = firstString(new JSONObject[]{root}, "protocol");
        if (s.protocol.isEmpty()) s.protocol = "hls";
        s.sync = firstString(new JSONObject[]{root}, "sync");
        s.audioMuxed = root.optBoolean("audio_muxed", true);
        // segment_sec 是秒（1.0），segment_ms 是毫秒，两种都接
        double sec = root.optDouble("segment_sec", root.optDouble("segmentSec", 0));
        s.segmentMs = root.optInt("segment_ms", root.optInt("segmentMs", (int) Math.round(sec * 1000)));
        String lvFull = firstString(new JSONObject[]{root}, "level_url_full", "levelUrlFull");
        String lvRel = firstString(new JSONObject[]{root}, "level_url", "levelUrl");
        s.levelUrl = !lvFull.isEmpty() ? lvFull
                : (!lvRel.isEmpty() ? resolveStream(mcpUrl, serverBase, lvRel) : "");
        s.levelFps = root.optInt("level_fps", root.optInt("levelFps", 10));
        s.levelWindowSec = root.optDouble("level_window_sec", 0);
        s.speechSeconds = root.optDouble("speech_seconds", 0);
        s.segments = root.optInt("segments", 0);

        // 服务端现在同时给 hls_url（相对）与 hls_url_full（完整）。优先用后者，
        // 并且只在必要时补 origin —— 任何情况下都不能把 /conring/... 再拼一次。
        String full = firstString(scopes, "hls_url_full", "stream_url_full", "playback_url_full");
        String spec = full;
        for (JSONObject o : scopes) {
            if (o == null) continue;
            if (!spec.isEmpty()) break;
            spec = firstString(new JSONObject[]{o}, "stream_url", "hls_url",
                    "playback_url", "play_url", "live_url", "m3u8", "stream");
            if (!spec.isEmpty()) break;
            JSONObject nested = o.optJSONObject("stream");
            if (nested != null) {
                spec = firstString(new JSONObject[]{nested}, "url", "stream_url",
                        "hls_url", "playback_url");
                if (!spec.isEmpty()) break;
            }
        }
        s.streamUrl = resolveStream(mcpUrl, serverBase, spec);
        if (s.streamUrl.isEmpty() && !spec.isEmpty()) {
            s.message = "服务端返回的流地址不是 MCP 同源地址，已拒绝（只使用同源资源）";
        }
        return s;
    }

    /**
     * 解析 intro_video 返回：
     *   {"count":18,"dir":"...","url_base":"/conring/asr/assets",
     *    "items":[{"id":"ceo","name":"陆清安","ready":true,"cached":true,
     *              "video":"/conring/asr/assets/intro/ceo.mp4","text":"...",
     *              "duration":21.16,"size":660258,"source":"video:ceo.talk.mp4"}]}
     * 也兼容只返回单条对象（不带 items）的情况。
     */
    public static IntroClip parseIntro(String mcpUrl, String serverBase, String text, String empId) {
        IntroClip c = new IntroClip();
        c.employeeId = empId == null ? "" : empId;
        if (text == null || text.trim().isEmpty()) return c;
        try {
            JSONObject root = new JSONObject(text.trim());
            JSONArray items = root.optJSONArray("items");
            JSONObject one = null;
            if (items != null && items.length() > 0) {
                for (int i = 0; i < items.length(); i++) {
                    JSONObject it = items.optJSONObject(i);
                    if (it == null) continue;
                    if (one == null) one = it;
                    if (empId != null && empId.equals(it.optString("id", ""))) {
                        one = it;
                        break;
                    }
                }
            } else {
                one = root.optJSONObject("item");
                if (one == null) one = root;
            }
            if (one == null) return c;

            c.employeeId = one.optString("id", c.employeeId);
            c.ready = one.optBoolean("ready", false);
            c.cached = one.optBoolean("cached", false);
            c.text = one.optString("text", "");
            c.duration = one.optDouble("duration", 0);
            c.size = one.optLong("size", 0);
            c.source = one.optString("source", "");
            String spec = firstString(new JSONObject[]{one},
                    "video", "video_url", "hls_url", "url", "stream_url");
            JSONObject v = one.optJSONObject("video");
            if (spec.isEmpty() && v != null) spec = v.optString("url", "");
            c.url = resolveStream(mcpUrl, serverBase, spec);
            if (c.url.isEmpty() && !spec.isEmpty()) c.ready = false;
        } catch (Exception ignored) {
        }
        return c;
    }

    /** 从 MCP result 里取第一个 audio 内容块的 base64（可能为空） */
    public static String audioOf(JSONObject result) {
        if (result == null) return "";
        JSONArray content = result.optJSONArray("content");
        if (content == null) return "";
        for (int i = 0; i < content.length(); i++) {
            JSONObject block = content.optJSONObject(i);
            if (block == null) continue;
            if (!"audio".equals(block.optString("type", ""))) continue;
            String d = block.optString("data", "");
            if (!d.isEmpty()) return d;
        }
        return "";
    }

    /** 从一轮上行回复里取给用户看的文本，兼容 JSON 与纯文本 */
    public static String replyText(String text) {
        if (text == null || text.trim().isEmpty()) return "";
        String raw = text.trim();
        try {
            JSONObject j = new JSONObject(raw);
            // 服务端的「收到」回执是 {ack:{text:"老板，稍等。"}}，必须取内层文本；
            // 直接 optString("ack") 在 Android 上会返回整个对象的 JSON 串。
            JSONObject ack = j.optJSONObject("ack");
            if (ack != null) {
                String v = ack.optString("text", "").trim();
                if (!v.isEmpty()) return v;
            }
            String[] keys = {"reply_text", "speech_text", "detail_text", "text",
                    "answer", "reply", "message", "transcript"};
            for (String k : keys) {
                Object o = j.opt(k);
                if (!(o instanceof String)) continue;
                String v = ((String) o).trim();
                if (!v.isEmpty() && !"null".equalsIgnoreCase(v)) return v;
            }
        } catch (Exception ignored) {
        }
        return raw.length() > 600 ? raw.substring(0, 600) : raw;
    }

    private static String firstString(JSONObject[] scopes, String... keys) {
        for (JSONObject o : scopes) {
            if (o == null) continue;
            for (String k : keys) {
                Object v = o.opt(k);
                if (v instanceof String) {
                    String s = ((String) v).trim();
                    if (!s.isEmpty() && !"null".equalsIgnoreCase(s)) return s;
                } else if (v instanceof JSONObject) {
                    String s = ((JSONObject) v).optString("url", "").trim();
                    if (!s.isEmpty()) return s;
                }
            }
        }
        return "";
    }
}
