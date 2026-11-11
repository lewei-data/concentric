import com.boray.voicemcp.AssetUrls;
import com.boray.voicemcp.StreamContract;

/**
 * 纯 JVM 校验 v2.5 契约：用服务端实测原样 JSON 走解析，不联网。
 * 覆盖：同源校验、双重前缀、外链拒绝、HLS 判定、重试 URL、
 *       live_call(start/say/stop) 返回、intro_video items、playlist 分片与收尾。
 */
public class VerifyStream {

    private static int pass = 0;
    private static int fail = 0;

    private static void check(String name, boolean ok) {
        if (ok) {
            pass++;
            System.out.println("  PASS  " + name);
        } else {
            fail++;
            System.out.println("  FAIL  " + name);
        }
    }

    // 服务端 2026-11-10 实测原样返回（截断无关字段）
    private static final String LIVE_START = "{"
            + "\"session_id\":\"lv_040fd43e\",\"status\":\"live\",\"avatar_id\":\"ceo\","
            + "\"hls_url\":\"/conring/asr/assets/live/lv_040fd43e/index.m3u8\","
            + "\"segment_sec\":1.0,\"started_at\":1794289955.99,\"elapsed\":3.31,"
            + "\"speech_seconds\":0.0,\"segments\":1,"
            + "\"sync\":\"音视频同流复用（AAC+H.264），由 ffmpeg 统一打时间戳\","
            + "\"ready\":true}";

    private static final String LIVE_SAY = "{"
            + "\"session_id\":\"lv_bc17c329\",\"status\":\"live\",\"avatar_id\":\"ceo\","
            + "\"hls_url\":\"/conring/asr/assets/live/lv_bc17c329/index.m3u8\","
            + "\"segment_sec\":1.0,\"elapsed\":11.13,\"speech_seconds\":3.38,"
            + "\"segments\":8,\"queued_seconds\":3.38}";

    private static final String LIVE_STOP = "{"
            + "\"session_id\":\"lv_bc17c329\",\"status\":\"ended\",\"avatar_id\":\"ceo\","
            + "\"hls_url\":\"/conring/asr/assets/live/lv_bc17c329/index.m3u8\","
            + "\"segment_sec\":1.0,\"elapsed\":11.32,\"segments\":9}";

    private static final String INTRO = "{"
            + "\"count\":18,\"dir\":\"/home/boray/opc-workbench/prototype/assets/intro\","
            + "\"url_base\":\"/conring/asr/assets\",\"items\":["
            + "{\"id\":\"boss\",\"name\":\"蒿色莫\",\"ready\":true,\"cached\":true,"
            + "\"video\":\"/conring/asr/assets/intro/boss.mp4\",\"text\":\"我是蒿色莫。\","
            + "\"duration\":21.2,\"size\":664305,\"source\":\"video:boss.talk.mp4\"},"
            + "{\"id\":\"ceo\",\"name\":\"陆清安\",\"ready\":true,\"cached\":true,"
            + "\"video\":\"/conring/asr/assets/intro/ceo.mp4\","
            + "\"text\":\"大家好，我是陆清安，公司管理层的CEO。\","
            + "\"duration\":21.16,\"size\":660258,\"source\":\"video:ceo.talk.mp4\"}]}";

    private static final String PLAYLIST = "#EXTM3U\n#EXT-X-VERSION:6\n"
            + "#EXT-X-TARGETDURATION:1\n#EXT-X-MEDIA-SEQUENCE:0\n"
            + "#EXT-X-PLAYLIST-TYPE:EVENT\n#EXT-X-INDEPENDENT-SEGMENTS\n"
            + "#EXTINF:1.000000,\nseg_00000.ts\n#EXTINF:1.000000,\nseg_00001.ts\n";

    private static final String PLAYLIST_HEADER_ONLY = "#EXTM3U\n#EXT-X-VERSION:6\n"
            + "#EXT-X-TARGETDURATION:1\n#EXT-X-PLAYLIST-TYPE:EVENT\n";

    public static void main(String[] args) {
        String mcp = "https://ai.boraydata.com/conring/asr/mcp";
        String base = "/conring/asr/assets";
        String origin = "https://ai.boraydata.com";

        // ---------------- 同源与地址解析
        check("介绍视频同源绝对路径不叠加 asset_url_base",
                AssetUrls.resolveSpec(mcp, base, "/conring/asr/assets/intro/ceo.mp4")
                        .equals(origin + "/conring/asr/assets/intro/ceo.mp4"));

        check("裸相对文件名才补 asset_url_base",
                AssetUrls.resolveSpec(mcp, base, "ceo.mp4")
                        .equals(origin + "/conring/asr/assets/ceo.mp4"));

        check("第三方 CDN 地址被拒绝",
                StreamContract.resolveStream(mcp, base,
                        "https://cdn.example.com/intro.mp4").isEmpty());

        check("同 host 不同端口被拒绝",
                StreamContract.resolveStream(mcp, base,
                        "https://ai.boraydata.com:8443/live/a.m3u8").isEmpty());

        check("http 与 https 视为不同源",
                !StreamContract.sameOrigin(mcp, "http://ai.boraydata.com/a.m3u8"));

        check("默认 443 端口判定同源",
                StreamContract.sameOrigin(mcp, origin + "/conring/asr/live/a.m3u8"));

        check("file:// 本地路径不作为流地址",
                StreamContract.resolveStream(mcp, base, "file:///sdcard/a.mp4").isEmpty());

        check("空地址安全返回",
                StreamContract.resolveStream(mcp, base, "").isEmpty());

        check("HLS 判定忽略 query",
                StreamContract.isHls(origin + "/live/a.m3u8?token=abc"));

        check("MP4 判定为渐进式而非 HLS",
                !StreamContract.isHls(origin + "/intro/ceo.mp4"));

        String retry = StreamContract.retryUrl(origin + "/live/a.m3u8", 2);
        check("重试 URL 仍是同源地址",
                StreamContract.sameOrigin(mcp, retry) && retry.contains("_retry=2"));

        check("attempt=0 不加重试参数",
                StreamContract.retryUrl(origin + "/live/a.m3u8", 0)
                        .equals(origin + "/live/a.m3u8"));

        // ---------------- live_call 返回
        StreamContract.CallSession s = StreamContract.parseCall(mcp, base, LIVE_START);
        check("start 解析出 session_id", "lv_040fd43e".equals(s.sessionId));
        check("start 的 hls_url 解析为同源绝对地址",
                s.streamUrl.equals(origin + "/conring/asr/assets/live/lv_040fd43e/index.m3u8"));
        check("segment_sec=1.0 换算成 1000ms", s.segmentMs == 1000);
        check("status=live 且 ready()", "live".equals(s.status) && s.ready());
        check("start 不是 ended", !s.ended());
        check("sync 字段被保留", s.sync.contains("AAC"));

        StreamContract.CallSession say = StreamContract.parseCall(mcp, base, LIVE_SAY);
        check("say 复用同一路流地址", say.streamUrl.equals(s.streamUrl.replace("040fd43e", "bc17c329")));
        check("say 带出 speech_seconds / segments",
                say.speechSeconds == 3.38 && say.segments == 8);

        StreamContract.CallSession stop = StreamContract.parseCall(mcp, base, LIVE_STOP);
        check("stop 后 status=ended", stop.ended());

        // 服务端现在同时给 hls_url 与 hls_url_full：必须优先用 full，且不能拼两次
        String both = "{\"session_id\":\"lv_1\",\"mode\":\"audio\","
                + "\"hls_url\":\"/conring/asr/assets/live/lv_1/index.m3u8\","
                + "\"hls_url_full\":\"" + origin + "/conring/asr/assets/live/lv_1/index.m3u8\"}";
        StreamContract.CallSession full = StreamContract.parseCall(mcp, base, both);
        check("优先使用 hls_url_full", full.streamUrl.equals(
                origin + "/conring/asr/assets/live/lv_1/index.m3u8"));
        check("播放地址没有二次拼接 /conring",
                !full.streamUrl.contains("/conring/asr/assets/conring"));
        check("只有相对 hls_url 时也只拼一次 origin",
                StreamContract.parseCall(mcp, base,
                        "{\"hls_url\":\"/conring/asr/assets/live/lv_2/index.m3u8\"}")
                        .streamUrl.equals(origin + "/conring/asr/assets/live/lv_2/index.m3u8"));
        check("cacheBust 带时间戳参数",
                StreamContract.cacheBust(full.streamUrl).contains("?t="));
        check("live.ended=true 能被识别",
                StreamContract.liveEnded(new org.json.JSONObject(
                        "{\"live\":{\"ended\":true}}")));
        check("live 缺 ended 字段时按未结束处理",
                !StreamContract.liveEnded(new org.json.JSONObject(
                        "{\"live\":{\"session_id\":\"lv_1\"}}")));

        StreamContract.CallSession bad = StreamContract.parseCall(mcp, base,
                "{\"status\":\"live\",\"hls_url\":\"https://evil.example.com/a.m3u8\"}");
        check("live_call 返回外链被拒绝", bad.streamUrl.isEmpty());
        check("外链拒绝时给出可读原因", bad.message.contains("同源"));

        StreamContract.CallSession plain = StreamContract.parseCall(mcp, base,
                "Error: live_call not found");
        check("纯文本回执不抛异常且带原文", plain.message.contains("not found"));

        // ---------------- intro_video 返回
        StreamContract.IntroClip ceo = StreamContract.parseIntro(mcp, base, INTRO, "ceo");
        check("intro 按 id 选中本人", "ceo".equals(ceo.employeeId));
        check("intro 视频解析为同源地址",
                ceo.url.equals(origin + "/conring/asr/assets/intro/ceo.mp4"));
        check("intro ready / cached 为真", ceo.ready && ceo.cached);
        check("intro 时长 21.16s", Math.abs(ceo.duration - 21.16) < 0.001);
        check("intro 文案非空", ceo.text.contains("陆清安"));
        check("intro size 660258", ceo.size == 660258);

        StreamContract.IntroClip boss = StreamContract.parseIntro(mcp, base, INTRO, "boss");
        check("intro 按 id 选中另一人", "boss".equals(boss.employeeId)
                && boss.url.endsWith("/intro/boss.mp4"));

        StreamContract.IntroClip miss = StreamContract.parseIntro(mcp, base, INTRO, "unknown");
        check("未知 id 回退首条而不是空", !miss.url.isEmpty());

        StreamContract.IntroClip evil = StreamContract.parseIntro(mcp, base,
                "{\"items\":[{\"id\":\"ceo\",\"ready\":true,"
                        + "\"video\":\"https://cdn.evil.com/ceo.mp4\",\"duration\":1}]}", "ceo");
        check("intro 外链被拒绝且标记未就绪", evil.url.isEmpty() && !evil.ready);

        // ---------------- playlist 判定
        check("playlist 有分片才放行", StreamContract.hasSegment(PLAYLIST));
        check("只有表头不放行", !StreamContract.hasSegment(PLAYLIST_HEADER_ONLY));
        check("空字符串不放行", !StreamContract.hasSegment(""));
        check("ENDLIST 判定为已收尾", StreamContract.isEnded(PLAYLIST + "#EXT-X-ENDLIST\n"));
        check("进行中的 EVENT 流不算收尾", !StreamContract.isEnded(PLAYLIST));

        // ---------------- 直播边缘（EVENT 型 playlist 从 seg_0 累加）
        StringBuilder pl = new StringBuilder("#EXTM3U\n#EXT-X-VERSION:6\n"
                + "#EXT-X-TARGETDURATION:1\n#EXT-X-MEDIA-SEQUENCE:0\n"
                + "#EXT-X-PLAYLIST-TYPE:EVENT\n");
        for (int i = 0; i < 7; i++) {
            pl.append("#EXTINF:1.000000,\nseg_0000").append(i).append(".ts\n");
        }
        String livePl = pl.toString();
        check("playlist 总时长 = 7s（直播边缘）", StreamContract.playlistTotalMs(livePl) == 7000);
        check("分片数 = 7", StreamContract.segmentCount(livePl) == 7);
        check("边缘起播位 = 总长 - 1.2s",
                StreamContract.liveStartMs(livePl, 1200) == 5800);
        check("空 playlist 时长为 0",
                StreamContract.playlistTotalMs("") == 0
                        && StreamContract.liveStartMs("", 1200) == 0);
        check("只有表头时长为 0", StreamContract.playlistTotalMs(PLAYLIST_HEADER_ONLY) == 0);

        // ---------------- 上行回复取文
        check("voice_chat ack 取出「收到」",
                StreamContract.replyText("{\"job_id\":\"vj_1\",\"ack\":{\"text\":\"老板，稍等。\"}}")
                        .equals("老板，稍等。"));
        check("纯文本回复原样返回",
                StreamContract.replyText("已发送").equals("已发送"));

        System.out.println("VERIFY STREAM: " + pass + " PASS / " + fail + " FAIL");
        if (fail > 0) System.exit(1);
    }
}
