import com.boray.voicemcp.DispatchApi;
import com.boray.voicemcp.WsClient;

import java.io.ByteArrayOutputStream;
import java.io.File;
import java.io.InputStream;
import java.io.OutputStream;
import java.net.HttpURLConnection;
import java.net.URL;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicInteger;

/**
 * 真实链路联测（不需要手机）：用 App 里同一份 {@link WsClient} 连生产 dispatch。
 *
 * 流程：POST /submit(channel=ws) → 连 wss → hello → 按 3200B/帧 推一段 WAV →
 * audio_end → 打印 transcript / reply / 二进制帧形态 → close。
 *
 * 用法：java WsProbe <wav 文件> [超时秒]
 * 判定：看到 transcript 且 reply 的 text/voice_text 分层、二进制首帧是 RIFF 头，即通过。
 */
public class WsProbe {

    public static void main(String[] args) throws Exception {
        String wavPath = args.length > 0 ? args[0] : "docs/preview_ceo_m08.wav";
        int timeoutSec = args.length > 1 ? Integer.parseInt(args[1]) : 90;
        byte[] wav = Files.readAllBytes(new File(wavPath).toPath());
        System.out.println("WAV " + wavPath + " " + wav.length + " bytes");

        String body = "{\"text\":\"语音通话接通\",\"source\":\"boss\",\"modality\":\"voice\","
                + "\"channel\":\"ws\",\"profile\":\"secretary\"}";
        String submitted = post(DispatchApi.PUBLIC_BASE + "/submit", body);
        DispatchApi.Submit sub = DispatchApi.parseSubmit(submitted);
        System.out.println("SUBMIT " + submitted);
        if (!sub.wsReady()) {
            System.out.println("FAIL 提交失败：" + sub.message);
            System.exit(1);
        }
        // 服务端 0.11 起 realtime_url 直接是完整公网 wss；老版本给模板则自行替换
        String url = sub.realtimeUrl.isEmpty() ? DispatchApi.publicWsUrl(null, sub)
                : sub.realtimeUrl.replace("<channel_id>", sub.channelId);
        System.out.println("URL " + url);

        final CountDownLatch done = new CountDownLatch(1);
        final AtomicInteger bins = new AtomicInteger();
        final AtomicInteger bytes = new AtomicInteger();
        final AtomicInteger headers = new AtomicInteger();
        final boolean[] transcript = {false};
        final boolean[] reply = {false};
        final boolean[] voiceText = {false};

        WsClient ws = new WsClient();
        ws.connect(url, new WsClient.Listener() {
            @Override
            public void onOpen() {
                System.out.println("OPEN " + url);
            }

            @Override
            public void onText(String text) {
                System.out.println("TEXT " + text);
                if (text.contains("\"transcript\"")) transcript[0] = true;
                if (text.contains("\"type\": \"reply\"") || text.contains("\"type\":\"reply\"")) {
                    reply[0] = true;
                    voiceText[0] = text.contains("voice_text");
                }
                // 只认正式轮次（turn>=1）的 reply_end；turn=0 是主动问候/催问，
                // 服务端说完问候不代表本轮对话结束（旧版没有问候，这里是新协议差异）。
                if (text.contains("reply_end") && !text.contains("\"turn\": 0")
                        && !text.contains("\"turn\":0")) done.countDown();
            }

            @Override
            public void onBinary(byte[] data) {
                int n = bins.incrementAndGet();
                bytes.addAndGet(data.length);
                boolean riff = data.length >= 12 && data[0] == 'R' && data[1] == 'I'
                        && data[2] == 'F' && data[3] == 'F';
                if (riff) headers.incrementAndGet();
                if (n <= 3 || riff) {
                    System.out.println("BIN #" + n + " " + data.length + "B"
                            + (riff ? " (RIFF 头)" : " (PCM 分块)"));
                }
            }

            @Override
            public void onClosed(int code, String reason) {
                System.out.println("CLOSED " + code + " " + reason);
                done.countDown();
            }
        }, 8000);

        ws.sendText("{\"type\":\"hello\"}");
        Thread.sleep(400);
        long t0 = System.currentTimeMillis();
        // 与 App 一致：整句一个 binary 消息一次发完（不分片、不夹文本帧）
        if (!ws.sendBinary(wav, 0, wav.length)) {
            System.out.println("FAIL 上行发送失败");
        }
        ws.sendText("{\"type\":\"audio_end\"}");
        System.out.println("AUDIO_END +" + (System.currentTimeMillis() - t0) + "ms");

        boolean finished = done.await(timeoutSec, TimeUnit.SECONDS);
        ws.sendText("{\"type\":\"close\"}");
        Thread.sleep(500);
        ws.close(1000, "probe");

        System.out.println("---- 结果 ----");
        System.out.println("ack/回复 二进制帧 " + bins.get() + " 个，共 " + bytes.get()
                + " 字节，其中 RIFF 头 " + headers.get() + " 个");
        boolean ok = finished && transcript[0] && reply[0] && voiceText[0]
                && bins.get() > 0 && headers.get() >= 1;
        System.out.println((ok ? "PASS" : "FAIL") + "  transcript=" + transcript[0]
                + " reply=" + reply[0] + " voice_text=" + voiceText[0]);
        System.exit(ok ? 0 : 1);
    }

    private static String post(String url, String json) throws Exception {
        HttpURLConnection c = (HttpURLConnection) new URL(url).openConnection();
        c.setRequestMethod("POST");
        c.setConnectTimeout(6000);
        c.setReadTimeout(20000);
        c.setDoOutput(true);
        c.setRequestProperty("Content-Type", "application/json; charset=utf-8");
        byte[] b = json.getBytes(StandardCharsets.UTF_8);
        c.setFixedLengthStreamingMode(b.length);
        try (OutputStream os = c.getOutputStream()) {
            os.write(b);
        }
        try (InputStream in = c.getInputStream()) {
            ByteArrayOutputStream bos = new ByteArrayOutputStream();
            byte[] buf = new byte[4096];
            int n;
            while ((n = in.read(buf)) > 0) bos.write(buf, 0, n);
            return new String(bos.toByteArray(), StandardCharsets.UTF_8);
        }
    }
}
