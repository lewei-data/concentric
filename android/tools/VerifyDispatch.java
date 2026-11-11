import com.boray.voicemcp.CallReply;
import com.boray.voicemcp.DispatchApi;
import com.boray.voicemcp.WavStream;

import java.io.ByteArrayOutputStream;
import java.util.Arrays;

/**
 * 纯 JVM 校验 0.9.6「调度中心 + WS 实时通道」契约，不联网。
 *
 * 覆盖：
 *  1) /submit 响应解析（带 ws 通道 / 缺 realtime_url / error / 非 JSON）
 *  2) realtime_url 模板替换（公网）与内网直连地址构造、非法模板拒绝
 *  3) reply 帧分层：text=文字详情、voice_text=语音稿、mode、attachments
 *  4) 下行 WAV 流：44B 头 + 100ms 分块、多句连续 wav、采样率切换、裸 PCM 兜底
 *  5) PCM 电平换算
 *
 * 编译：javac -encoding UTF-8 -cp android.jar:json.jar -d out 源文件 tools/VerifyDispatch.java
 */
public class VerifyDispatch {

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

    private static void eq(String name, Object want, Object got) {
        boolean ok = want == null ? got == null : want.equals(got);
        check(name + (ok ? "" : "  want=" + want + " got=" + got), ok);
    }

    // ---------------------------------------------------------------- 服务端原样返回

    private static final String SUBMIT_WS = "{\"task_id\": \"rt_4b7218_9\", \"priority\": \"P0\", "
            + "\"lane\": \"realtime\", \"status\": \"running\", "
            + "\"next\": \"任务已立即派给专属 worker；用 /task/rt_4b7218_9 查询进度\", "
            + "\"channel_id\": \"ch_2b71b9_10\", \"channel_kind\": \"ws\", "
            + "\"realtime_url\": \"wss://ai.boraydata.com/conring/dispatch-ws/channel/<channel_id>\"}";

    private static final String SUBMIT_PASSTHROUGH = "{\"task_id\": \"rt_666749_7\", "
            + "\"priority\": \"P0\", \"lane\": \"realtime\", \"status\": \"running\", "
            + "\"channel_id\": \"ch_e522a1_8\", \"channel_kind\": \"passthrough\"}";

    private static final String SUBMIT_ERROR = "{\"error\":\"text 不能为空\"}";

    private static final String REPLY = "{\"type\": \"reply\", "
            + "\"text\": \"今天 3 个待办：①审合同 ②回客户 ③周报。合同原文在这里。\", "
            + "\"voice_text\": \"老板，今天三件事，最急的是审合同。\", "
            + "\"mode\": \"task\", "
            + "\"attachments\": [{\"name\": \"合同.pdf\", \"url\": \"https://ai.boraydata.com/share/abc\"},"
            + "{\"name\": \"空链接\", \"url\": \"\"}]}";

    public static void main(String[] args) throws Exception {
        System.out.println("== 1. submit 响应解析 ==");
        DispatchApi.Submit s = DispatchApi.parseSubmit(SUBMIT_WS);
        check("ok", s.ok);
        eq("task_id", "rt_4b7218_9", s.taskId);
        eq("channel_id", "ch_2b71b9_10", s.channelId);
        eq("channel_kind", "ws", s.channelKind);
        check("wsReady", s.wsReady());
        eq("lane", "realtime", s.lane);
        eq("priority", "P0", s.priority);

        DispatchApi.Submit p = DispatchApi.parseSubmit(SUBMIT_PASSTHROUGH);
        check("passthrough 不算 ok", p.ok && !p.wsReady());
        check("passthrough 给出原因", p.message.contains("ws"));

        DispatchApi.Submit e = DispatchApi.parseSubmit(SUBMIT_ERROR);
        check("error → 不 ok", !e.ok);
        eq("error 文案", "text 不能为空", e.message);

        DispatchApi.Submit bad = DispatchApi.parseSubmit("<html>502</html>");
        check("非 JSON → 不 ok", !bad.ok);
        check("非 JSON 有可读原因", bad.message.contains("JSON"));

        System.out.println("== 2. WS 地址 ==");
        eq("公网模板替换",
                "wss://ai.boraydata.com/conring/dispatch-ws/channel/ch_2b71b9_10",
                DispatchApi.wsUrl("ws://192.168.31.206:9121", s, false));
        eq("内网直连",
                "ws://192.168.31.206:9121/channel/ch_2b71b9_10",
                DispatchApi.wsUrl("ws://192.168.31.206:9121", s, true));
        eq("内网尾斜杠不重复",
                "ws://192.168.31.206:9121/channel/ch_2b71b9_10",
                DispatchApi.wsUrl("ws://192.168.31.206:9121/", s, true));
        DispatchApi.Submit noUrl = DispatchApi.parseSubmit(SUBMIT_PASSTHROUGH);
        eq("没有 realtime_url → 空", "", DispatchApi.wsUrl("ws://x:1", noUrl, false));
        DispatchApi.Submit httpUrl = new DispatchApi.Submit();
        httpUrl.channelId = "ch_1";
        httpUrl.realtimeUrl = "https://evil.example/<channel_id>";
        eq("非 ws 协议拒绝", "", DispatchApi.wsUrl("ws://x:1", httpUrl, false));
        eq("没有内网地址 → 空", "", DispatchApi.wsUrl("", s, true));
        eq("自行推导公网 wss（防服务端回内网地址）",
                "wss://ai.boraydata.com/conring/dispatch-ws/channel/ch_2b71b9_10",
                DispatchApi.publicWsUrl(null, s));
        DispatchApi.Submit lanOnly = DispatchApi.parseSubmit(SUBMIT_WS);
        lanOnly.realtimeUrl = "ws://192.168.31.206:9121/channel/<channel_id>";
        eq("realtime_url 是内网时仍推导公网",
                "wss://ai.boraydata.com/conring/dispatch-ws/channel/ch_2b71b9_10",
                DispatchApi.publicWsUrl(null, lanOnly));

        System.out.println("== 3. reply 分层 ==");
        org.json.JSONObject jo = new org.json.JSONObject(REPLY);
        CallReply r = CallReply.parse(jo);
        eq("mode", "task", r.mode);
        eq("语音稿", "老板，今天三件事，最急的是审合同。", r.voiceText);
        check("文字详情 != 语音稿", r.hasExtraDetail());
        eq("详情是另一层内容", true, r.detail.contains("审合同"));
        eq("附件只保留有 url 的", 1, r.attachments.size());
        eq("附件名", "合同.pdf", r.attachments.get(0).name);
        eq("附件 url", "https://ai.boraydata.com/share/abc", r.attachments.get(0).url);
        check("附件可打开", r.attachments.get(0).openable());

        CallReply same = CallReply.parse(new org.json.JSONObject(
                "{\"type\":\"reply\",\"text\":\"嗯，我在呢\",\"voice_text\":\"嗯，我在呢\"}"));
        check("详情与语音稿相同 → 不重复展示", !same.hasExtraDetail());
        eq("spoken 取语音稿", "嗯，我在呢", same.spoken());

        CallReply onlyDetail = CallReply.parse(new org.json.JSONObject(
                "{\"type\":\"reply\",\"text\":\"只有详情\",\"voice_text\":\"\"}"));
        check("没有语音稿 → spoken 退回详情", "只有详情".equals(onlyDetail.spoken()));

        CallReply emptyPing = CallReply.parse(new org.json.JSONObject(
                "{\"type\":\"reply\",\"text\":\"\",\"voice_text\":\"\",\"attachments\":[]}"));
        check("空回复", emptyPing.empty());

        System.out.println("== 4. 下行 WAV 流 ==");
        Recorder rec = new Recorder();
        WavStream ws = new WavStream(rec);
        byte[] pcm1 = pcm(16000, 0.1, 3000);          // 100ms
        byte[] wav1 = wav(16000, 1, 16, pcm1);
        ws.feed(Arrays.copyOfRange(wav1, 0, 20));     // 头被拆成两帧也要能拼
        check("头未收全不回调格式", rec.formats.isEmpty());
        ws.feed(Arrays.copyOfRange(wav1, 20, wav1.length));
        eq("识别采样率", 16000, rec.lastFormat.sampleRate);
        eq("首段载荷", 3000, rec.pcmBytes);
        check("头被吃掉、不混进 PCM", rec.firstPayloadLen == 3000);

        // 第二句：又一整组「头 + 载荷」，紧接着来
        byte[] pcm2 = pcm(16000, 0.1, 3200);
        ws.feed(wav(16000, 1, 16, pcm2));
        eq("多句连续 wav 累加", 6200, rec.pcmBytes);
        eq("同格式不重建 AudioTrack", 1, rec.formats.size());

        // 第三句换成 24k（服务端 TTS 换音色时可能出现）
        byte[] pcm3 = pcm(24000, 0.1, 4800);
        ws.feed(wav(24000, 1, 16, pcm3));
        eq("采样率切换被识别", 24000, rec.lastFormat.sampleRate);
        eq("换采样率回调一次新格式", 2, rec.formats.size());

        // 真实形态：CosyVoice 的 wav 带 LIST/INFO 块，服务端仍只把前 44 字节单独发一帧，
        // 头的后半段混在第一个“载荷”帧里 —— 必须按 chunk 扫到 data 再切。
        Recorder info = new Recorder();
        WavStream infoStream = new WavStream(info);
        byte[] iw = wavWithInfo(24000, pcm(24000, 0.1, 4800));
        infoStream.feed(Arrays.copyOfRange(iw, 0, 44));
        infoStream.feed(Arrays.copyOfRange(iw, 44, 44 + 4800));
        infoStream.feed(Arrays.copyOfRange(iw, 44 + 4800, iw.length));
        eq("INFO 头不计入 PCM", 4800, info.pcmBytes);
        eq("INFO 头里的 24k 被识别", 24000, info.lastFormat.sampleRate);

        // 裸 PCM（不带头）兜底
        Recorder raw = new Recorder();
        WavStream rawStream = new WavStream(raw);
        rawStream.feed(new byte[2048]);
        eq("裸 PCM 兜底按 16k", 16000, raw.lastFormat.sampleRate);
        eq("裸 PCM 直接播", 2048, raw.pcmBytes);

        System.out.println("== 5. 电平 ==");
        byte[] loud = pcm(16000, 0.1, 1600);          // 全幅正弦
        float lv = WavStream.level(loud, 0, loud.length);
        check("满幅 → 接近 1（实际 " + lv + "）", lv > 0.85f);
        byte[] quiet = new byte[3200];
        eq("静音 → 0", 0f, WavStream.level(quiet, 0, quiet.length));
        check("越界长度不崩", WavStream.level(loud, 0, 99999) > 0f);

        System.out.println();
        System.out.println("PASS=" + pass + "  FAIL=" + fail);
        if (fail > 0) System.exit(1);
    }

    // ---------------------------------------------------------------- 构造 WAV

    private static byte[] wav(int rate, int ch, int bits, byte[] pcm) {
        byte[] out = new byte[44 + pcm.length];
        ascii(out, 0, "RIFF");
        le32(out, 4, 36 + pcm.length);
        ascii(out, 8, "WAVE");
        ascii(out, 12, "fmt ");
        le32(out, 16, 16);
        le16(out, 20, 1);
        le16(out, 22, ch);
        le32(out, 24, rate);
        le32(out, 28, rate * ch * bits / 8);
        le16(out, 32, ch * bits / 8);
        le16(out, 34, bits);
        ascii(out, 36, "data");
        le32(out, 40, pcm.length);
        System.arraycopy(pcm, 0, out, 44, pcm.length);
        return out;
    }

    /** 带 LIST/INFO 块的真实 TTS wav（data 不在 44 字节处） */
    private static byte[] wavWithInfo(int rate, byte[] pcm) {
        byte[] info = new byte[]{'I', 'N', 'F', 'O', 'I', 'S', 'F', 'T',
                26, 0, 0, 0, 'C', 'o', 's', 'y', 'V', 'o', 'i', 'c', 'e', ' ', 'v', '2', 0, 0};
        int dataOff = 12 + 24 + 8 + info.length + 8;
        byte[] out = new byte[dataOff + pcm.length];
        ascii(out, 0, "RIFF");
        le32(out, 4, out.length - 8);
        ascii(out, 8, "WAVE");
        ascii(out, 12, "fmt ");
        le32(out, 16, 16);
        le16(out, 20, 1);
        le16(out, 22, 1);
        le32(out, 24, rate);
        le32(out, 28, rate * 2);
        le16(out, 32, 2);
        le16(out, 34, 16);
        ascii(out, 36, "LIST");
        le32(out, 40, info.length);
        System.arraycopy(info, 0, out, 44, info.length);
        ascii(out, 44 + info.length, "data");
        le32(out, 48 + info.length, pcm.length);
        System.arraycopy(pcm, 0, out, dataOff, pcm.length);
        return out;
    }

    /** 指定频率的正弦 PCM16 */
    private static byte[] pcm(int rate, double seconds, int bytes) {
        int frames = Math.max(1, bytes / 2);
        byte[] out = new byte[frames * 2];
        for (int i = 0; i < frames; i++) {
            int v = (int) (12000 * Math.sin(2 * Math.PI * 440 * i / rate));
            out[i * 2] = (byte) (v & 0xFF);
            out[i * 2 + 1] = (byte) ((v >> 8) & 0xFF);
        }
        return out;
    }

    private static void ascii(byte[] d, int off, String s) {
        for (int i = 0; i < s.length(); i++) d[off + i] = (byte) s.charAt(i);
    }

    private static void le16(byte[] d, int off, int v) {
        d[off] = (byte) (v & 0xFF);
        d[off + 1] = (byte) ((v >>> 8) & 0xFF);
    }

    private static void le32(byte[] d, int off, int v) {
        d[off] = (byte) (v & 0xFF);
        d[off + 1] = (byte) ((v >>> 8) & 0xFF);
        d[off + 2] = (byte) ((v >>> 16) & 0xFF);
        d[off + 3] = (byte) ((v >>> 24) & 0xFF);
    }

    private static final class Recorder implements WavStream.Sink {
        final java.util.List<WavStream.Format> formats = new java.util.ArrayList<>();
        WavStream.Format lastFormat = new WavStream.Format();
        int pcmBytes = 0;
        int firstPayloadLen = -1;

        @Override
        public void onFormat(WavStream.Format f) {
            WavStream.Format copy = new WavStream.Format();
            copy.sampleRate = f.sampleRate;
            copy.channels = f.channels;
            copy.bits = f.bits;
            formats.add(copy);
            lastFormat = copy;
        }

        @Override
        public void onPcm(byte[] data, int off, int len) {
            if (firstPayloadLen < 0) firstPayloadLen = len;
            pcmBytes += len;
        }
    }
}
