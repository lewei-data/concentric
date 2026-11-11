package com.boray.voicemcp;

import android.graphics.Color;

import org.json.JSONArray;
import org.json.JSONObject;

import java.util.ArrayList;
import java.util.List;

/**
 * OPC 内部数字员工档案（server v2.1.0）。
 *
 * 档案来源优先级：用户自定义 filesDir/employees.json > profile 接口缓存 employees_server.json
 * > assets 内置示例。服务端 profile 一次返回 18 人，字段与 docs/v21_profile_all.json 一致。
 *
 * 素材写法（photo / videoIdle / videoTalk / faceUrl）：
 *   /conring/asr/assets/avatars/ceo.png  服务端同源绝对路径，AssetUrls 直接拼 MCP origin
 *   https://...                          服务端直接给的绝对地址
 *   drawable://xxx                       APK 内置矢量资源（离线兜底档案）
 *   assets://xxx / file://                本机素材
 *   留空                                 使用内置 Canvas 数字人
 *
 * 字段名兼容驼峰与蛇形（videoTalk / video_talk、faceUrl / face_url、assets.*），
 * 因为 profile 顶层与 voice_chat.avatar 的写法不完全一致。
 *
 * v2.5 新增（服务端生成，客户端只消费同源地址）：
 *   introVideo     自我介绍视频，音画合一的 MP4（ffmpeg 混流，AAC+H.264），流式边下边播
 *   introReady     服务端是否已生成（true 才点开即播，false 走按需生成）
 *   introText      自我介绍文案，用于字幕
 *   introDuration  实测时长（秒），用于进度与「21.2s」这类展示
 *   liveCall       {supported, protocol:"hls", segment_sec}；
 *                  supported=false 时客户端隐藏通话入口
 *   liveStream     可选：档案里预置的 live playlist（一般由 live_call start 动态分配）
 * 以上字段全部可缺省：服务端未生成时客户端回退到「真实 WAV + 照片口型」，
 * 不白屏、不猜测任何第三方 URL。字段名兼容驼峰与蛇形，见 fromJson。
 */
public final class Employee {

    public String id = "";
    public String name = "";
    public String no = "";
    public String dept = "";
    public String title = "";
    public String role = "";
    public String project = "";
    public String motto = "";
    public String bio = "";
    /** 服务端专属音色 id（m01–m10 / f01–f08）；voice_chat 不传 voice 时服务端自动用它 */
    public String voice = "";
    public String profile = "";

    /** v2.1.0 新增：性别 / 年龄 / Hermes profile 是否就绪 */
    public String gender = "";
    public int age = 0;
    public boolean profileReady = true;

    public String photo = "";
    public String videoIdle = "";
    public String videoTalk = "";
    public String faceUrl = "";
    public boolean hasPhoto = false;
    public boolean hasVideo = false;

    /** v2.5：服务端生成的自我介绍视频 / 文案 / 时长（同源流式播放，可缺省） */
    public String introVideo = "";
    public String introAudio = "";
    public String introText = "";
    public String introPoster = "";
    public boolean hasIntro = false;
    /** 服务端是否已生成（等价 v2.5 的 introReady） */
    public boolean introReady = false;
    /** 自我介绍视频实测时长（秒），来自 introDuration */
    public double introDuration = 0;

    /** v2.5：实时通话能力 {supported, protocol, segment_sec} */
    public boolean liveSupported = false;
    public String liveProtocol = "";
    public double liveSegmentSec = 0;
    /** 可选：档案里预置的 live playlist，一般由 live_call start 动态分配 */
    public String callStream = "";

    /** 外观：发型 short / long / bun / curly，是否戴眼镜，情绪基线 calm / cheerful / serious */
    public String hairStyle = "short";
    public String emotion = "calm";
    public boolean glasses = false;

    public int accent = Color.parseColor("#006C63");
    public int skin = Color.parseColor("#F3C9A6");
    public int hair = Color.parseColor("#2A2320");
    public int suit = Color.parseColor("#20514B");
    public int shirt = Color.parseColor("#F4F8F6");

    public final List<String> skills = new ArrayList<>();

    public static Employee fromJson(JSONObject j) {
        Employee e = new Employee();
        e.id = j.optString("id", "");
        e.name = j.optString("name", "未命名");
        e.no = j.optString("no", e.id);
        e.dept = j.optString("dept", "");
        e.title = j.optString("title", "");
        e.role = j.optString("role", "");
        e.project = j.optString("project", "");
        e.motto = j.optString("motto", "");
        e.bio = j.optString("bio", "");
        e.voice = j.optString("voice", "");
        e.profile = j.optString("profile", e.id);
        e.gender = j.optString("gender", "");
        e.age = j.optInt("age", 0);
        e.profileReady = j.optBoolean("profile_ready", true);

        e.photo = pick(j, "photo");
        e.videoIdle = pick(j, "videoIdle", "video_idle");
        e.videoTalk = pick(j, "videoTalk", "video_talk");
        e.faceUrl = pick(j, "faceUrl", "face_url");
        JSONObject assets = j.optJSONObject("assets");
        if (assets != null) {
            if (e.photo.isEmpty()) e.photo = pick(assets, "photo");
            if (e.videoTalk.isEmpty()) e.videoTalk = pick(assets, "video_talk", "videoTalk");
            if (e.videoIdle.isEmpty()) e.videoIdle = pick(assets, "video_idle", "videoIdle");
            if (e.faceUrl.isEmpty()) e.faceUrl = pick(assets, "face_url", "faceUrl");
        }
        e.hasPhoto = j.optBoolean("has_photo", !e.photo.isEmpty());
        e.hasVideo = j.optBoolean("has_video", !e.videoTalk.isEmpty() || !e.videoIdle.isEmpty());

        // v2.4 自我介绍 / 实时通话流：顶层、assets、intro 子对象三处都兼容，
        // 字段名兼容驼峰与蛇形，服务端按任一种写法返回都能被消费。
        JSONObject intro = j.optJSONObject("intro");
        if (intro == null) intro = j.optJSONObject("self_intro");
        if (intro == null) intro = j.optJSONObject("introduction");
        e.introVideo = firstNonEmpty(
                pick(j, "intro_video", "introVideo", "intro_stream", "introStream",
                        "self_intro_video", "video_intro", "intro_hls", "introHls"),
                assets == null ? "" : pick(assets, "intro_video", "introVideo",
                        "intro_stream", "intro_stream_url", "self_intro_video"),
                intro == null ? "" : pick(intro, "video", "video_url", "hls_url",
                        "stream_url", "url", "playback_url"));
        e.introAudio = firstNonEmpty(
                pick(j, "intro_audio", "introAudio", "self_intro_audio"),
                intro == null ? "" : pick(intro, "audio", "audio_url"));
        e.introText = firstNonEmpty(
                pick(j, "intro_text", "introText", "self_intro_text", "self_intro"),
                intro == null ? "" : pick(intro, "text", "script", "caption"));
        e.introPoster = firstNonEmpty(
                pick(j, "intro_poster", "introPoster"),
                intro == null ? "" : pick(intro, "poster", "poster_url"));
        e.callStream = firstNonEmpty(
                pick(j, "call_stream", "callStream", "live_stream", "live_url",
                        "live_hls", "call_hls", "liveStream"),
                assets == null ? "" : pick(assets, "call_stream", "live_stream"));
        e.hasIntro = j.optBoolean("has_intro", !e.introVideo.isEmpty());
        // v2.5：introReady 是服务端的权威标记；老字段 has_intro 与「有地址」作为兼容兜底
        e.introReady = j.optBoolean("introReady",
                j.optBoolean("intro_ready", e.hasIntro && !e.introVideo.isEmpty()));
        e.introDuration = j.optDouble("introDuration", j.optDouble("intro_duration", 0));
        if (e.introDuration <= 0 && intro != null) {
            e.introDuration = intro.optDouble("duration", 0);
        }
        if (!e.introVideo.isEmpty()) e.hasIntro = true;
        if (!e.introReady && e.hasIntro) e.introReady = true;

        // v2.5：liveCall = {supported, protocol:"hls", segment_sec}
        JSONObject live = j.optJSONObject("liveCall");
        if (live == null) live = j.optJSONObject("live_call");
        if (live != null) {
            e.liveSupported = live.optBoolean("supported", false);
            e.liveProtocol = live.optString("protocol", "hls");
            e.liveSegmentSec = live.optDouble("segment_sec", live.optDouble("segmentSec", 0));
        } else {
            // 顶层布尔写法也兼容；缺省时按「有预置流地址即支持」判断
            e.liveSupported = j.optBoolean("live_supported",
                    j.optBoolean("liveSupported", !e.callStream.isEmpty()));
        }

        e.hairStyle = j.optString("hairStyle", "short");
        e.emotion = j.optString("emotion", "calm");
        e.glasses = j.optBoolean("glasses", false);
        e.accent = color(j, "accent", "#006C63");
        e.skin = color(j, "skin", "#F3C9A6");
        e.hair = color(j, "hair", "#2A2320");
        e.suit = color(j, "suit", "#20514B");
        e.shirt = color(j, "shirt", "#F4F8F6");
        JSONArray arr = j.optJSONArray("skills");
        if (arr != null) {
            for (int i = 0; i < arr.length(); i++) {
                String s = arr.optString(i, "");
                if (!s.isEmpty()) e.skills.add(s);
            }
        }
        return e;
    }

    private static String firstNonEmpty(String... values) {
        for (String v : values) if (v != null && !v.trim().isEmpty()) return v.trim();
        return "";
    }

    /** 取第一个非空字符串；若字段是 {"url": "..."} 形态则取其中的 url */
    private static String pick(JSONObject j, String... keys) {
        for (String k : keys) {
            Object v = j.opt(k);
            if (v instanceof String) {
                String s = ((String) v).trim();
                if (!s.isEmpty()) return s;
            } else if (v instanceof JSONObject) {
                String u = ((JSONObject) v).optString("url", "").trim();
                if (!u.isEmpty()) return u;
            }
        }
        return "";
    }

    private static int color(JSONObject j, String key, String fallback) {
        try {
            return Color.parseColor(j.optString(key, fallback));
        } catch (Exception ignored) {
            return Color.parseColor(fallback);
        }
    }

    /** 传给 voice_chat 的人设标识；设置页的 profile 若留空就用当前员工 */
    public String profileValue() {
        return profile.isEmpty() ? id : profile;
    }

    public boolean hasVideo() {
        return !videoTalk.isEmpty() || !videoIdle.isEmpty();
    }

    /** 服务端是否已生成可直接流式播放的自我介绍视频 */
    public boolean hasIntroVideo() {
        return introReady && !introVideo.isEmpty();
    }

    /**
     * 是否是老板本人。
     *
     * 老板用这个 App 是「自己用」，不是被别人汇报，所以：
     * 员工列表把他单独成块，详情页也不给他「发起实时通话 / 设为当前汇报人」这两个按钮。
     * 三种口径都认：id=boss、工号 OPC-000、岗位/角色写「老板」。
     */
    public boolean isBoss() {
        if ("boss".equalsIgnoreCase(id)) return true;
        if ("OPC-000".equalsIgnoreCase(no)) return true;
        return "老板".equals(title) || "老板".equals(role);
    }

    /**
     * 自我介绍字幕：服务端给了文案就用，否则用档案字段拼一段，
     * 保证视频未就绪时降级朗读也不会出现空白字幕。
     */
    public String introCaption() {
        if (!introText.trim().isEmpty()) return introText.trim();
        StringBuilder b = new StringBuilder();
        b.append("您好，我是").append(name);
        if (!title.isEmpty()) b.append("，").append(title);
        if (!dept.isEmpty()) b.append("，来自").append(dept);
        b.append("。");
        if (!motto.isEmpty()) b.append(motto);
        if (!bio.isEmpty()) b.append(" ").append(bio);
        return b.toString();
    }

    /** v2.5 素材就绪状态，档案页直接展示，避免用户点了才知道服务端没生成 */
    public String streamLine() {
        StringBuilder b = new StringBuilder();
        b.append("自我介绍视频 ");
        if (hasIntroVideo()) {
            b.append("✓ 已生成");
            if (introDuration > 0) {
                b.append(" ").append(String.format(java.util.Locale.CHINA, "%.1fs",
                        introDuration));
            }
        } else if (!introVideo.isEmpty()) {
            b.append("✓ 有地址");
        } else {
            b.append("— 待生成");
        }
        b.append(" · 实时通话 ");
        if (liveSupported) {
            b.append("✓ ").append(liveProtocol.isEmpty() ? "hls" : liveProtocol);
            if (liveSegmentSec > 0) {
                b.append(" ").append(String.format(java.util.Locale.CHINA, "%.0fs",
                        liveSegmentSec)).append(" 分片");
            }
        } else {
            b.append("— 服务端未开启");
        }
        return b.toString();
    }

    public String shortLabel() {
        return name + " · " + title;
    }

    /** 列表页副标题：性别 / 年龄 / 专属音色 */
    public String metaLine() {
        StringBuilder b = new StringBuilder();
        if ("male".equalsIgnoreCase(gender)) b.append("男");
        else if ("female".equalsIgnoreCase(gender)) b.append("女");
        else if (!gender.isEmpty()) b.append(gender);
        if (age > 0) {
            if (b.length() > 0) b.append(" · ");
            b.append(age).append(" 岁");
        }
        if (!voice.isEmpty()) {
            if (b.length() > 0) b.append(" · ");
            b.append("音色 ").append(voice);
        }
        return b.length() == 0 ? "—" : b.toString();
    }

    /** 素材与人设就绪状态 */
    public String assetLine() {
        return "照片 " + (hasPhoto ? "✓" : "—")
                + " · 视频 " + (hasVideo ? "✓" : "—")
                + " · 人设 " + (profileReady ? "✓" : "未就绪");
    }
}
