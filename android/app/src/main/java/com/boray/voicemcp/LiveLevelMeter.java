package com.boray.voicemcp;

import android.media.MediaPlayer;
import android.os.Handler;
import android.os.Looper;
import android.util.Log;

import org.json.JSONArray;
import org.json.JSONObject;

import java.io.ByteArrayOutputStream;
import java.io.InputStream;
import java.net.HttpURLConnection;
import java.net.URL;

/**
 * 员工侧（下行）实时电平：轮询服务端的 {@code level.json}，按「媒体时间」取格，
 * 让音量条和耳朵里听到的声音同步。
 *
 * <p>服务端契约（v2.6）：
 * <pre>
 * live_call start → level_url / level_url_full / level_fps / level_window_sec
 * level.json     → {session_id, seq, fps, t0, edge, wall, elapsed, status, lv:[0..100]}
 * </pre>
 * {@code lv[i]} 覆盖媒体时间 {@code t0 + i/fps}。播放位置只有「相对本次播放起点」的毫秒数，
 * 所以要先算出「播放位置 0 对应的媒体时间」= {@code EXT-X-MEDIA-SEQUENCE × 单片时长}。
 *
 * <p>单片时长取窗口内 <b>最大</b> 的 {@code EXTINF}：末片常被截断（实测 0.883 vs 0.998），
 * 用窗口均值会偏 1 秒以上，用 TARGETDURATION 会随通话时长累积漂移。
 *
 * <p>只访问与播放列表 <b>同源</b> 的地址；轮询线程里的任何异常都不会让它退出。
 */
public final class LiveLevelMeter {

    private static final String TAG = "LiveLevel";
    private static final long POLL_MS = 300;      // 300~500ms 采一次，够跟又不费电
    private static final int LAG_WINDOW = 8;     // lag 取最近 8 个样本的中位数
    private static final int DEGRADE_AFTER = 3;  // 连续 3 次拉取失败就降级
    private static final float LAG_FALLBACK = 2f; // 未热身时的保守滞后（秒）
    private static final long STALE_MS = 3000;    // 3 秒没有新 seq 就认为断了
    private static final long SEEK_BACK_MS = 500; // 位置回跳超过 0.5s 视为播放器重载

    public interface Listener {
        /** 0~1；员工静音 / 已挂断 / 快照过期时为 0 */
        void onLevel(float level01);
    }

    private final String levelUrl;
    private final String playlistUrl;
    private final MediaPlayer player;
    private final int fpsHint;
    private final float segHint;
    private final Handler main = new Handler(Looper.getMainLooper());

    private volatile Listener listener;
    private volatile boolean running;
    private Thread worker;

    private volatile float[] levels = new float[0];
    private volatile float t0 = 0f;
    private volatile float fps = 10f;
    private volatile float edge = 0f;
    private volatile String status = "live";
    private volatile int lastSeq = -1;
    private volatile long lastSeqAt = 0L;

    /** 最近若干次 (edge − mediaT) 样本，秒 */
    private final java.util.ArrayDeque<Float> lagSamples = new java.util.ArrayDeque<>();
    /** 连续拉取失败次数；≥3 进入降级（用播放头 + 0.3s 代锚点） */
    private volatile int failCount = 0;
    private volatile boolean degraded = false;
    /** 服务端说 ended：停止轮询，字幕按"全部已播"处理 */
    private volatile boolean ended = false;
    /** 将来服务端下发逐句 speech_at 时，这里就是唯一切换点（<0 表示没有） */
    private volatile float speechAtSec = -1f;

    /** 播放位置 0 对应的媒体时间 */
    private volatile float anchorMedia = 0f;
    private volatile long lastPosMs = -1L;
    private volatile long lastLogAt = 0L;

    public LiveLevelMeter(String levelUrl, String playlistUrl, MediaPlayer player,
                          int fpsHint, float segSec) {
        this.levelUrl = levelUrl == null ? "" : levelUrl.trim();
        this.playlistUrl = playlistUrl == null ? "" : playlistUrl.trim();
        this.player = player;
        this.fpsHint = fpsHint > 0 ? fpsHint : 10;
        this.segHint = segSec > 0 ? segSec : 1f;
        this.fps = this.fpsHint;
    }

    public void setListener(Listener l) {
        listener = l;
    }

    /** 服务端流尾的媒体时间（最新分片追加到的地方） */
    public float edgeSec() {
        return edge;
    }

    /** 本机播放头对应的媒体时间 */
    public float mediaSec() {
        return anchorMedia + positionMs() / 1000f;
    }

    /**
     * 「已经播出到哪」的媒体时刻 —— **唯一的锚点入口**。
     *
     * 现在用 {@code edge − 中位数滞后} 代理；将来服务端下发 speech_at_seconds，
     * 只需在这里改一行（{@link #setSpeechAtSec}），调用方完全无感。
     */
    public float frontierSec() {
        if (speechAtSec >= 0f) return speechAtSec;
        if (ended) return Float.MAX_VALUE;          // 流结束：剩余字幕全部放出来
        if (degraded || !hasData()) return mediaSec() + 0.3f;
        return edgeSec() - lagMedianSec();
    }

    /** 预留：服务端逐句锚点就绪后调它，前端逻辑不用动 */
    public void setSpeechAtSec(float sec) {
        speechAtSec = sec;
    }

    public boolean degraded() {
        return degraded;
    }

    public boolean ended() {
        return ended;
    }

    /** 最近 8 个 (edge − mediaT) 的中位数；抗 HLS 分段跳变 */
    private float lagMedianSec() {
        synchronized (lagSamples) {
            if (lagSamples.isEmpty()) return LAG_FALLBACK;
            Float[] a = lagSamples.toArray(new Float[0]);
            java.util.Arrays.sort(a);
            return a[a.length / 2];
        }
    }

    private void pushLag(float lagSec) {
        synchronized (lagSamples) {
            lagSamples.addLast(lagSec);
            while (lagSamples.size() > LAG_WINDOW) lagSamples.pollFirst();
        }
    }

    private void resetLags() {
        synchronized (lagSamples) {
            lagSamples.clear();
        }
    }

    /** 快照是否还活着（3 秒内有新 seq） */
    public boolean hasData() {
        return lastSeq >= 0 && System.currentTimeMillis() - lastSeqAt <= STALE_MS;
    }

    /**
     * 现在听到的声音，距离直播边缘还有多少毫秒。
     *
     * 服务端把 TTS 语音追加到流尾（约等于 edge），客户端却落后这么多在播；
     * 用它就能把「文字」延到「声音真的响起来」的那一刻再显示。
     */
    public long lagMs() {
        if (!hasData()) return 0;
        float mediaT = anchorMedia + positionMs() / 1000f;
        return Math.max(0L, (long) ((edge - mediaT) * 1000));
    }

    /** 是否有可轮询的同源地址（没有就别开线程） */
    public boolean usable() {
        return sameOrigin(levelUrl, playlistUrl) && sameOrigin(playlistUrl, playlistUrl);
    }

    /** MediaPlayer.start() 之后调用 */
    public void start() {
        if (running || !usable()) return;
        running = true;
        worker = new Thread(() -> {
            String pl = getText(playlistUrl);
            if (pl != null && !pl.isEmpty()) {
                anchorMedia = playlistStartSec(pl, segHint);
            }
            resetLags();
            ended = false;
            degraded = false;
            failCount = 0;
            Log.i(TAG, "meter start, anchor=" + anchorMedia + " url=" + levelUrl);
            while (running) {
                try {
                    step();
                } catch (Throwable t) {
                    // 一次失败不能让轮询线程退出
                    Log.w(TAG, "step: " + t);
                }
                try {
                    Thread.sleep(POLL_MS);
                } catch (InterruptedException e) {
                    Thread.currentThread().interrupt();
                    return;
                }
            }
        }, "live-level");
        worker.setDaemon(true);
        worker.start();
    }

    public void stop() {
        running = false;
        Thread w = worker;
        worker = null;
        if (w != null) w.interrupt();
        lastSeq = -1;
        levels = new float[0];
        post(0f);
    }

    // ------------------------------------------------------------------ 主循环

    private void step() {
        long now = System.currentTimeMillis();
        long pos = positionMs();

        if (lastPosMs >= 0 && pos + SEEK_BACK_MS < lastPosMs) {
            // 播放器重载 / 位置回跳：用最新播放列表重新锚定（读不到就保留旧锚点）
            String pl = getText(playlistUrl);
            if (pl != null && !pl.isEmpty()) anchorMedia = playlistStartSec(pl, segHint);
            else anchorMedia = 0f;
            resetLags();          // 重载/seek 后滞后要重估
        }
        lastPosMs = pos;

        if (ended) return;        // 已收尾：不再轮询

        JSONObject js = getJson(levelUrl);
        if (js == null) {
            // 连续失败就降级（用播放头 + 0.3s 代锚点），恢复后自动切回
            if (++failCount >= DEGRADE_AFTER && !degraded) {
                degraded = true;
                Log.w(TAG, "level.json 连续 " + failCount + " 次失败，降级为播放头锚点");
            }
            if (now - lastSeqAt > STALE_MS) post(0f);
            return;
        }
        if (degraded) {
            degraded = false;
            resetLags();
            Log.i(TAG, "level.json 恢复，退出降级");
        }
        failCount = 0;

        int seq = js.optInt("seq", -1);
        // seq 必须单调递增：过期/回退样本直接丢（服务端重写失败时会重复同一 seq）
        if (seq > lastSeq) {
            lastSeq = seq;
            lastSeqAt = now;
            status = js.optString("status", "live");
            t0 = (float) js.optDouble("t0", 0);
            fps = (float) js.optDouble("fps", fpsHint);
            edge = (float) js.optDouble("edge", 0);
            JSONArray arr = js.optJSONArray("lv");
            float[] buf = new float[arr == null ? 0 : arr.length()];
            for (int i = 0; i < buf.length; i++) {
                buf[i] = (float) arr.optDouble(i, 0) / 100f;   // 服务端 0~100 → 0~1
            }
            levels = buf;
        }

        if ("ended".equals(status)) {
            ended = true;
            Log.i(TAG, "level.json status=ended，停止轮询并放出剩余字幕");
            post(0f);
            return;
        }

        float mediaT = anchorMedia + pos / 1000f;
        if (mediaT > 0 && edge > mediaT) pushLag(edge - mediaT);

        if (now - lastSeqAt > STALE_MS) {
            post(0f);
            return;
        }
        post(sample(levels, t0, fps, edge, mediaT));

        // 真机联调用：mediaT 应该贴着 edge 走，明显落后/超前就按 README 35.5 改锚点
        if (now - lastLogAt > 2000) {
            lastLogAt = now;
            Log.d(TAG, "pos=" + pos + "ms mediaT=" + mediaT + " edge=" + edge
                    + " seq=" + seq + " anchor=" + anchorMedia);
        }
    }

    // ------------------------------------------------------------------ 纯逻辑（可单测）

    /** 媒体时间 → 0~1；越界取首/尾格，格间线性插值 */
    public static float sample(float[] a, float t0, float fps, float edge, float mediaT) {
        if (a == null || a.length == 0) return 0f;
        float t = mediaT;
        if (edge > 0 && t > edge) t = edge;        // 还没写进流的未来时间不查
        float x = (t - t0) * (fps > 0 ? fps : 10f);
        if (x <= 0f) return a[0];
        int i = (int) Math.floor(x);
        if (i >= a.length - 1) return a[a.length - 1];
        float k = x - i;
        return a[i] + (a[i + 1] - a[i]) * k;
    }

    /**
     * 播放列表首片的媒体起始时间 = MEDIA-SEQUENCE × 单片时长。
     * 单片时长取窗口内最大的 EXTINF（末片可能被截断）。
     */
    public static float playlistStartSec(String playlist, float segHint) {
        if (playlist == null || playlist.isEmpty()) return 0f;
        int seq = 0;
        float target = segHint > 0 ? segHint : 1f;
        float unit = -1f;
        for (String raw : playlist.split("\n")) {
            String s = raw.trim();
            if (s.startsWith("#EXT-X-MEDIA-SEQUENCE:")) {
                seq = parseInt(s.substring(s.indexOf(':') + 1), 0);
            } else if (s.startsWith("#EXT-X-TARGETDURATION:")) {
                target = parseFloat(s.substring(s.indexOf(':') + 1), segHint);
            } else if (s.startsWith("#EXTINF:")) {
                String v = s.substring(8);
                int comma = v.indexOf(',');
                if (comma >= 0) v = v.substring(0, comma);
                float d = parseFloat(v.trim(), -1f);
                if (d > unit) unit = d;
            }
        }
        float step = unit > 0f ? unit : target;
        return seq * step;
    }

    // ------------------------------------------------------------------ 工具

    private long positionMs() {
        try {
            return Math.max(0L, player.getCurrentPosition());
        } catch (Exception e) {
            return 0L;
        }
    }

    private void post(float v) {
        Listener l = listener;
        if (l == null) return;
        main.post(() -> l.onLevel(v));
    }

    private JSONObject getJson(String url) {
        String text = getText(url);
        if (text == null) return null;
        try {
            return new JSONObject(text);
        } catch (Exception e) {
            return null;
        }
    }

    private static String getText(String url) {
        if (url == null || url.isEmpty()) return null;
        HttpURLConnection c = null;
        try {
            c = (HttpURLConnection) new URL(url).openConnection();
            c.setConnectTimeout(3000);
            c.setReadTimeout(3000);
            c.setUseCaches(false);
            c.setRequestProperty("Cache-Control", "no-cache");
            if (c.getResponseCode() != HttpURLConnection.HTTP_OK) return null;
            try (InputStream in = c.getInputStream()) {
                ByteArrayOutputStream bos = new ByteArrayOutputStream();
                byte[] b = new byte[4096];
                int n;
                while ((n = in.read(b)) > 0) bos.write(b, 0, n);
                return bos.toString("UTF-8");
            }
        } catch (Exception e) {
            return null;
        } finally {
            if (c != null) c.disconnect();
        }
    }

    /**
     * 同源校验：只允许访问和播放列表同一 scheme+host+port 的地址。
     * 服务端偶尔会给相对路径或换域名的绝对地址，这里一律拒绝，避免把「只用同源」的约定破掉。
     */
    public static boolean sameOrigin(String a, String b) {
        String ha = origin(a);
        String hb = origin(b);
        return !ha.isEmpty() && ha.equals(hb);
    }

    private static String origin(String url) {
        if (url == null || url.isEmpty()) return "";
        try {
            URL u = new URL(url);
            int port = u.getPort();
            return u.getProtocol() + "://" + u.getHost() + ":" + (port > 0 ? port : u.getDefaultPort());
        } catch (Exception e) {
            return "";
        }
    }

    private static int parseInt(String s, int def) {
        try {
            return Integer.parseInt(s.trim());
        } catch (Exception e) {
            return def;
        }
    }

    private static float parseFloat(String s, float def) {
        try {
            return Float.parseFloat(s.trim());
        } catch (Exception e) {
            return def;
        }
    }
}
