package com.boray.voicemcp;

import android.content.Context;
import android.content.SharedPreferences;
import android.net.ConnectivityManager;
import android.net.NetworkCapabilities;
import android.os.Build;

/**
 * 全部连接参数集中在这里持久化（适配 server v2.1.0）。
 * 正式端点：https://ai.boraydata.com/conring/asr/mcp
 * 所有参数都能在 App「设置」页覆盖，无需重新编译。
 *
 * v2.1.0 关键约定：
 *  - voice 留空时，服务端按「被点名/播报员工」的档案音色自动选，客户端不要固定传 voice
 *  - profile 留空时，服务端按点名派单结果自己决定 Hermes 人设
 *  - avatar_id 指定播报形象，mode / bandwidth_kbps 决定播报形态
 *  - 素材地址只用服务端返回的同源路径 /conring/asr/assets/*（MCP 端点 origin 拼接），
 *    不接其它 URL；2026-11-10 服务端已放通，公网 200、无需 VPN 与 Token
 */
public final class McpConfig {

    public static final String CLIENT_VERSION = "0.11.5";

    public static final String DEFAULT_URL = "https://ai.boraydata.com/conring/asr/mcp";
    public static final String TTS_URL = "https://ai.boraydata.com/conring/tts/mcp";
    public static final String MAIL_URL = "https://ai.boraydata.com/conring/mail/ceo/mcp";
    public static final String LEGACY_URL = "http://192.168.31.208/conring/asr/mcp";
    public static final String DEFAULT_TOOL = "voice_chat";
    public static final String DEFAULT_AUDIO_KEY = "audio_base64";
    public static final String DEFAULT_PROTOCOL = "2025-03-26";

    private static final String PREF = "voice_mcp_config";

    public String url = DEFAULT_URL;
    public String tool = DEFAULT_TOOL;
    public String audioKey = DEFAULT_AUDIO_KEY;
    public String language = "zh";
    public String model = "small";
    /** 覆盖音色：留空 = 服务端按员工档案自动选（推荐） */
    public String voice = "";
    /** 覆盖 Hermes profile：留空 = 服务端按点名派单 / avatar_id 决定 */
    public String profile = "";
    public String token = "";
    public String protocol = DEFAULT_PROTOCOL;
    public int timeoutSec = 600;
    /** 结果/消息自动语音播报 */
    public boolean autoPlay = true;
    /**
     * 语音控制总开关（员工档案首屏自我介绍是否出声）。
     * 关闭后：视频照常加载并静音播放，但不再出任何声音，也不会在首次打开时自动播放语音。
     */
    public boolean voiceEnabled = true;
    public boolean reportEnabled = true;
    public boolean reportVideo = true;
    public boolean demo = false;

    /* ===== 异步任务 + 保持监听 ===== */
    public boolean asyncMode = true;
    public boolean keepListening = true;
    public boolean listenSse = true;
    public boolean listenMail = true;
    public int listenIntervalSec = 20;
    public String mailUrl = MAIL_URL;

    /* ===== server v2.1.0 新增参数 ===== */
    /** 播报形象：留空 = 当前汇报人；服务端按语音点名可再派单给别的员工 */
    public String avatarId = "";
    /** auto / voice / avatar / video */
    public String mode = "auto";
    /** 0 = 自动读取链路下行带宽；>0 = 手动覆盖（auto 模式 <600 会降级为纯语音） */
    public int bandwidthKbps = 0;
    /** 是否允许按语音点名自动派单 */
    public boolean delegate = true;
    /** App 启动 / 档案页自动从 profile 拉取 18 人档案（缓存 6 小时） */
    public boolean autoSyncProfile = true;

    /* ===== 0.9.6 调度中心（实时通道）双地址 ===== */
    /** 公网 dispatch：https://ai.boraydata.com/conring/dispatch */
    public String dispatchUrl = DispatchApi.PUBLIC_BASE;
    /** 内网 dispatch（HTTP，端口 9120）：http://192.168.31.206:9120 */
    public String dispatchUrlLan = DispatchApi.LAN_BASE;
    /** 内网 WS（端口 9121）：ws://192.168.31.206:9121 */
    public String dispatchWsLan = DispatchApi.LAN_WS_BASE;

    public static McpConfig load(Context c) {
        SharedPreferences p = c.getSharedPreferences(PREF, Context.MODE_PRIVATE);
        McpConfig x = new McpConfig();
        x.url = p.getString("url", DEFAULT_URL);
        x.tool = p.getString("tool", DEFAULT_TOOL);
        x.audioKey = p.getString("audioKey", DEFAULT_AUDIO_KEY);
        x.language = p.getString("language", "zh");
        x.model = p.getString("model", "small");
        x.voice = p.getString("voice", "");
        x.profile = p.getString("profile", "");
        x.token = p.getString("token", "");
        x.protocol = p.getString("protocol", DEFAULT_PROTOCOL);
        x.timeoutSec = p.getInt("timeoutSec", 600);
        x.autoPlay = p.getBoolean("autoPlay", true);
        x.voiceEnabled = p.getBoolean("voiceEnabled", true);
        x.reportEnabled = p.getBoolean("reportEnabled", true);
        x.reportVideo = p.getBoolean("reportVideo", true);
        x.demo = p.getBoolean("demo", false);
        x.asyncMode = p.getBoolean("asyncMode", true);
        x.keepListening = p.getBoolean("keepListening", true);
        x.listenSse = p.getBoolean("listenSse", true);
        x.listenMail = p.getBoolean("listenMail", true);
        x.listenIntervalSec = p.getInt("listenIntervalSec", 20);
        x.mailUrl = p.getString("mailUrl", MAIL_URL);
        x.avatarId = p.getString("avatarId", "");
        x.mode = p.getString("mode", "auto");
        x.bandwidthKbps = p.getInt("bandwidthKbps", 0);
        x.delegate = p.getBoolean("delegate", true);
        x.autoSyncProfile = p.getBoolean("autoSyncProfile", true);
        x.dispatchUrl = p.getString("dispatchUrl", DispatchApi.PUBLIC_BASE);
        x.dispatchUrlLan = p.getString("dispatchUrlLan", DispatchApi.LAN_BASE);
        x.dispatchWsLan = p.getString("dispatchWsLan", DispatchApi.LAN_WS_BASE);
        return x;
    }

    public void save(Context c) {
        SharedPreferences.Editor e = c.getSharedPreferences(PREF, Context.MODE_PRIVATE).edit();
        e.putString("url", url);
        e.putString("tool", tool);
        e.putString("audioKey", audioKey);
        e.putString("language", language);
        e.putString("model", model);
        e.putString("voice", voice);
        e.putString("profile", profile);
        e.putString("token", token);
        e.putString("protocol", protocol);
        e.putInt("timeoutSec", timeoutSec);
        e.putBoolean("autoPlay", autoPlay);
        e.putBoolean("voiceEnabled", voiceEnabled);
        e.putBoolean("reportEnabled", reportEnabled);
        e.putBoolean("reportVideo", reportVideo);
        e.putBoolean("demo", demo);
        e.putBoolean("asyncMode", asyncMode);
        e.putBoolean("keepListening", keepListening);
        e.putBoolean("listenSse", listenSse);
        e.putBoolean("listenMail", listenMail);
        e.putInt("listenIntervalSec", listenIntervalSec);
        e.putString("mailUrl", mailUrl);
        e.putString("avatarId", avatarId);
        e.putString("mode", mode);
        e.putInt("bandwidthKbps", bandwidthKbps);
        e.putBoolean("delegate", delegate);
        e.putBoolean("autoSyncProfile", autoSyncProfile);
        e.putString("dispatchUrl", dispatchUrl);
        e.putString("dispatchUrlLan", dispatchUrlLan);
        e.putString("dispatchWsLan", dispatchWsLan);
        e.apply();
    }

    public McpConfig copy() {
        McpConfig x = new McpConfig();
        x.url = url; x.tool = tool; x.audioKey = audioKey;
        x.language = language; x.model = model; x.voice = voice; x.profile = profile;
        x.token = token; x.protocol = protocol; x.timeoutSec = timeoutSec;
        x.autoPlay = autoPlay; x.voiceEnabled = voiceEnabled;
        x.reportEnabled = reportEnabled; x.reportVideo = reportVideo;
        x.demo = demo; x.asyncMode = asyncMode; x.keepListening = keepListening;
        x.listenSse = listenSse; x.listenMail = listenMail;
        x.listenIntervalSec = listenIntervalSec; x.mailUrl = mailUrl;
        x.avatarId = avatarId; x.mode = mode; x.bandwidthKbps = bandwidthKbps;
        x.delegate = delegate; x.autoSyncProfile = autoSyncProfile;
        x.dispatchUrl = dispatchUrl; x.dispatchUrlLan = dispatchUrlLan;
        x.dispatchWsLan = dispatchWsLan;
        return x;
    }

    public int listenIntervalMs() {
        return Math.max(5, Math.min(listenIntervalSec, 60)) * 1000;
    }

    public int timeoutMs() {
        return Math.max(10, Math.min(timeoutSec, 900)) * 1000;
    }

    /** mode 只允许服务端支持的四个取值 */
    public String validMode() {
        if ("voice".equals(mode) || "avatar".equals(mode) || "video".equals(mode)) return mode;
        return "auto";
    }

    /** 0=自动测量链路下行带宽；测不到时按 2000 kbps（不触发 <600 降级） */
    public int effectiveBandwidthKbps(Context c) {
        if (bandwidthKbps > 0) return bandwidthKbps;
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q) {
            try {
                ConnectivityManager cm = (ConnectivityManager) c.getSystemService(Context.CONNECTIVITY_SERVICE);
                if (cm != null) {
                    NetworkCapabilities nc = cm.getNetworkCapabilities(cm.getActiveNetwork());
                    if (nc != null) {
                        int kbps = nc.getLinkDownstreamBandwidthKbps();
                        if (kbps > 0) return kbps;
                    }
                }
            } catch (Exception ignored) {
            }
        }
        return 2000;
    }

    public String summary() {
        return url + "  |  " + tool;
    }
}
