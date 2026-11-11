package com.boray.voicemcp;

import android.media.MediaPlayer;
import android.net.Uri;
import android.os.Handler;
import android.os.Looper;
import android.widget.VideoView;

import java.io.ByteArrayOutputStream;
import java.io.InputStream;
import java.net.HttpURLConnection;
import java.net.URL;

/**
 * v2.5 同源流式播放内核（纯 framework，零第三方依赖）。
 *
 * 实测服务端形态：
 *   - 自我介绍：MP4（avc1+mp4a，音画合一），支持 Range；也可给 HLS VOD
 *   - 实时通话：HLS Live，1s TS 分片，#EXT-X-PLAYLIST-TYPE:EVENT 持续追加，
 *               分片里同时有 0x1b(H.264) 与 0x0f(AAC)，挂断后写 #EXT-X-ENDLIST
 *
 * 因此：Audio 与 Video 由服务端 ffmpeg 混流共用时间戳，MediaPlayer 自动同步，
 * 客户端不做两路时钟对齐、不开第二路音频通道。
 *
 * LIVE 起流有个必须处理的细节：live_call start 之后 playlist 是先空后满的
 * （首个分片落盘要 1–3s），直接 setVideoURI 空 playlist 会立刻报错。
 * 所以 LIVE 模式先用同源 HttpURLConnection 轮询 m3u8 到「有 EXTINF + 至少一个分片」
 * 再交给播放器；超时也放行，让播放器按自己的重试逻辑再试。
 *
 * 断流只重试同一个同源 URL（加无副作用 _retry 参数），绝不改换主机或端口。
 */
public final class StreamPlayback {

    public enum Mode { VOD, LIVE }

    public interface Listener {
        /** 起流前的等待提示（等首个分片、等 playlist 刷新） */
        void onWaiting(String hint);

        void onPreparing(int attempt);

        void onReady(int durationMs);

        void onBuffering(boolean busy);

        void onProgress(int positionMs, int durationMs);

        void onCompleted();

        void onRetry(int attempt, String reason);

        void onFailed(String reason);
    }

    private static final int LIVE_MAX_ATTEMPTS = 4;
    private static final int VOD_MAX_ATTEMPTS = 2;
    /** 等首个分片的最长等待，服务端 first_segment_sec 默认 8s，这里再留一点余量 */
    private static final long FIRST_SEGMENT_WAIT_MS = 10_000L;
    private static final long POLL_INTERVAL_MS = 500L;

    private final Handler ui = new Handler(Looper.getMainLooper());
    private volatile VideoView view;
    private volatile String url = "";
    private volatile Mode mode = Mode.VOD;
    private volatile boolean audio = true;
    private volatile Listener listener;
    private volatile int attempt = 0;
    private volatile boolean released = false;
    private volatile boolean prepared = false;
    private Thread probe;

    private final Runnable ticker = new Runnable() {
        @Override
        public void run() {
            if (released || view == null || !prepared) return;
            try {
                int pos = view.getCurrentPosition();
                int dur = view.getDuration();
                if (listener != null) listener.onProgress(pos, dur);
            } catch (Exception ignored) {
            }
            ui.postDelayed(this, 500);
        }
    };

    /**
     * @param vv        已可见的 VideoView
     * @param streamUrl 经 StreamContract 校验过的同源绝对地址
     * @param playMode  VOD（自我介绍）/ LIVE（实时通话）
     * @param withAudio 两者都为 true：声音就在这条流里
     */
    public void play(VideoView vv, String streamUrl, Mode playMode,
                     boolean withAudio, Listener cb) {
        release();
        if (vv == null || streamUrl == null || streamUrl.isEmpty()) {
            if (cb != null) cb.onFailed("服务端尚未提供可用的同源流地址");
            return;
        }
        this.view = vv;
        this.url = streamUrl;
        this.mode = playMode == null ? Mode.VOD : playMode;
        this.audio = withAudio;
        this.listener = cb;
        this.released = false;
        this.attempt = 0;
        if (this.mode == Mode.LIVE && StreamContract.isHls(streamUrl)) {
            awaitFirstSegment();
        } else {
            open(0);
        }
    }

    // ---------------------------------------------------------------- 起流前等待首帧

    private void awaitFirstSegment() {
        final String target = url;
        if (listener != null) listener.onWaiting("等待服务端首个分片…");
        probe = new Thread(() -> {
            long deadline = System.currentTimeMillis() + FIRST_SEGMENT_WAIT_MS;
            int polls = 0;
            while (!released && System.currentTimeMillis() < deadline) {
                polls++;
                if (StreamContract.hasSegment(fetch(target))) {
                    if (!released) ui.post(() -> open(0));
                    return;
                }
                final int n = polls;
                ui.post(() -> {
                    if (!released && listener != null) {
                        listener.onWaiting("等待服务端首个分片…（第 " + n + " 次轮询）");
                    }
                });
                try {
                    Thread.sleep(POLL_INTERVAL_MS);
                } catch (InterruptedException e) {
                    Thread.currentThread().interrupt();
                    return;
                }
            }
            // 超时也放行：让 MediaPlayer 自己去拉，失败会走 onError -> 重试
            if (!released) ui.post(() -> open(0));
        }, "live-first-segment");
        probe.setDaemon(true);
        probe.start();
    }

    /** 同源读一次 playlist；只用于判断是否有分片，不做缓存 */
    private static String fetch(String playlistUrl) {
        HttpURLConnection c = null;
        try {
            c = (HttpURLConnection) new URL(playlistUrl).openConnection();
            c.setConnectTimeout(6000);
            c.setReadTimeout(6000);
            c.setRequestProperty("Accept", "application/vnd.apple.mpegurl, */*");
            c.setRequestProperty("User-Agent", "conring-android/stream");
            c.setUseCaches(false);
            int code = c.getResponseCode();
            if (code < 200 || code >= 400) return "";
            InputStream in = c.getInputStream();
            ByteArrayOutputStream out = new ByteArrayOutputStream();
            byte[] buf = new byte[4096];
            int n, total = 0;
            while ((n = in.read(buf)) > 0 && total < 64 * 1024) {
                out.write(buf, 0, n);
                total += n;
            }
            in.close();
            return out.toString("UTF-8");
        } catch (Exception e) {
            return "";
        } finally {
            if (c != null) c.disconnect();
        }
    }

    // ---------------------------------------------------------------- 打开与重试

    private void open(int nextAttempt) {
        if (released || view == null) return;
        attempt = nextAttempt;
        prepared = false;
        String target = StreamContract.retryUrl(url, mode == Mode.LIVE ? attempt : 0);
        if (mode == Mode.LIVE) {
            // 服务端未设 Cache-Control，m3u8 又是滑动窗口，必须带时间戳避免读到旧列表
            target = StreamContract.cacheBust(target);
        }
        if (listener != null) listener.onPreparing(attempt);

        view.setOnErrorListener((mp, what, extra) -> {
            if (released) return true;
            String reason = "播放错误 what=" + what + " extra=" + extra;
            int max = mode == Mode.LIVE ? LIVE_MAX_ATTEMPTS : VOD_MAX_ATTEMPTS;
            if (attempt + 1 < max) {
                final int next = attempt + 1;
                if (listener != null) listener.onRetry(next, reason);
                try { view.stopPlayback(); } catch (Exception ignored) { }
                ui.postDelayed(() -> {
                    if (released) return;
                    if (mode == Mode.LIVE) awaitFirstSegment();
                    else open(next);
                }, mode == Mode.LIVE ? 1200L : 600L);
            } else {
                if (listener != null) listener.onFailed(reason);
            }
            return true;
        });

        view.setOnPreparedListener(mp -> {
            if (released) return;
            prepared = true;
            try {
                mp.setVolume(audio ? 1f : 0f, audio ? 1f : 0f);
                mp.setLooping(false);   // live 靠 playlist 持续追加；VOD 播完即停
            } catch (Exception ignored) {
            }
            if (view instanceof CoverVideoView) {
                // 视频尺寸到这一刻才知道，交给 CoverVideoView 才能正确铺满裁切
                try {
                    ((CoverVideoView) view).setVideoSize(mp.getVideoWidth(), mp.getVideoHeight());
                } catch (Exception ignored) {
                }
            }
            int dur = 0;
            try { dur = view.getDuration(); } catch (Exception ignored) { }
            if (listener != null) listener.onReady(dur);
            try { view.start(); } catch (Exception ignored) { }
            ui.removeCallbacks(ticker);
            ui.post(ticker);
        });

        view.setOnCompletionListener(mp -> {
            if (released) return;
            ui.removeCallbacks(ticker);
            if (listener != null) listener.onCompleted();
        });

        view.setOnInfoListener((mp, what, extra) -> {
            if (released || listener == null) return false;
            if (what == MediaPlayer.MEDIA_INFO_BUFFERING_START) listener.onBuffering(true);
            else if (what == MediaPlayer.MEDIA_INFO_BUFFERING_END) listener.onBuffering(false);
            return false;
        });

        try {
            view.setVideoURI(Uri.parse(target));
        } catch (Exception e) {
            if (listener != null) listener.onFailed("打开流失败：" + e.getMessage());
        }
    }

    public void pause() {
        VideoView v = view;
        if (released || v == null) return;
        try { if (v.isPlaying()) v.pause(); } catch (Exception ignored) { }
    }

    public void resume() {
        VideoView v = view;
        if (released || v == null) return;
        try { if (!v.isPlaying()) v.start(); } catch (Exception ignored) { }
    }

    /** 从头重播（自我介绍的「重播」按钮） */
    public void replay() {
        if (released || url.isEmpty()) return;
        ui.removeCallbacks(ticker);
        try { view.stopPlayback(); } catch (Exception ignored) { }
        if (mode == Mode.LIVE) awaitFirstSegment();
        else open(0);
    }

    /** 释放播放器，页面销毁时必须调用，避免后台继续拉流 */
    public void release() {
        released = true;
        prepared = false;
        ui.removeCallbacks(ticker);
        if (probe != null) {
            probe.interrupt();
            probe = null;
        }
        VideoView v = view;
        if (v != null) {
            try {
                v.setOnErrorListener(null);
                v.setOnPreparedListener(null);
                v.setOnCompletionListener(null);
                v.setOnInfoListener(null);
                v.stopPlayback();
            } catch (Exception ignored) {
            }
        }
        view = null;
        listener = null;
    }

    public String url() {
        return url;
    }

    public boolean isLive() {
        return mode == Mode.LIVE;
    }
}
