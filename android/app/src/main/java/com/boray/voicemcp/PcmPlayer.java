package com.boray.voicemcp;

import android.media.AudioAttributes;
import android.media.AudioFormat;
import android.media.AudioManager;
import android.media.AudioTrack;
import android.util.Log;

import java.util.concurrent.LinkedBlockingQueue;
import java.util.concurrent.TimeUnit;

/**
 * 下行语音流播放器：把 WS 二进制帧当成「连续的 WAV 流」边收边播。
 *
 * <p>为什么不用 MediaPlayer：服务端按 100ms 一块推 PCM，一条回复还可能拆成多句、
 * 每句一个独立 WAV 头。MediaPlayer 需要完整数据源，等齐了再播就变成「对方说完了
 * 才听到」，而且打断（barge_in）只能是丢掉整个文件。这里用 AudioTrack 流式写：
 * 收到即播、延迟就是网络 + 一个缓冲区；打断时 pause+flush 立刻静音。
 *
 * <p>同时把 PCM 的 RMS 报给界面——这就是「员工侧电平」，比服务端电平快照更准。
 */
public final class PcmPlayer {

    /** 电平回调（播放线程上调用，调用方自己切主线程） */
    public interface LevelListener {
        void onLevel(float level);
    }

    private static final String TAG = "PcmPlayer";
    private static final int MAX_QUEUE = 80;

    private final LinkedBlockingQueue<byte[]> queue = new LinkedBlockingQueue<>(MAX_QUEUE);
    private final Object lock = new Object();

    private AudioTrack track;
    private int sampleRate = 16000;
    private int channels = 1;
    private int bits = 16;
    private volatile boolean released = false;
    private volatile boolean paused = false;
    /** 被打断后静音：服务端还在路上的残余分块必须丢掉，否则「打断完又接着说」 */
    private volatile boolean muted = false;
    private volatile long lastPcmAt = 0L;
    private volatile float level = 0f;
    private Thread worker;
    private LevelListener levelListener;

    private final WavStream stream = new WavStream(new WavStream.Sink() {
        @Override
        public void onFormat(WavStream.Format f) {
            openTrack(f.sampleRate, f.channels, f.bits);
        }

        @Override
        public void onPcm(byte[] data, int off, int len) {
            if (len <= 0) return;
            if (muted) return;
            if (off == 0 && len == data.length) {
                queue.offer(data);
            } else {
                byte[] copy = new byte[len];
                System.arraycopy(data, off, copy, 0, len);
                queue.offer(copy);
            }
            lastPcmAt = System.currentTimeMillis();
            float v = WavStream.level(data, off, len);
            level = v;
            LevelListener l = levelListener;
            if (l != null) l.onLevel(v);
        }
    });

    public PcmPlayer() {
        worker = new Thread(this::loop, "pcm-player");
        worker.setDaemon(true);
        worker.start();
    }

    public void setLevelListener(LevelListener l) {
        levelListener = l;
    }

    public float level() {
        return level;
    }

    /** 送入一个 WS 二进制帧 */
    public void feed(byte[] data) {
        if (released || data == null) return;
        stream.feed(data);
    }

    /** 是否正在播（含刚收到、还在缓冲区里的） */
    public boolean isPlaying() {
        return !released && (!queue.isEmpty() || System.currentTimeMillis() - lastPcmAt < 450);
    }

    /** 新一轮开始：恢复出声 */
    public void setMuted(boolean m) {
        muted = m;
    }

    public boolean isMuted() {
        return muted;
    }

    /** barge_in / 挂断：立刻静音并丢掉未播完的数据 */
    public void stopAndFlush() {
        muted = true;
        synchronized (lock) {
            queue.clear();
            stream.reset();
            if (track != null) {
                try {
                    track.pause();
                    track.flush();
                    paused = true;
                } catch (Exception ignored) {
                }
            }
        }
        level = 0f;
        lastPcmAt = 0L;
    }

    public void release() {
        released = true;
        queue.clear();
        if (worker != null) {
            worker.interrupt();
            worker = null;
        }
        synchronized (lock) {
            if (track != null) {
                try { track.pause(); } catch (Exception ignored) { }
                try { track.flush(); } catch (Exception ignored) { }
                try { track.release(); } catch (Exception ignored) { }
                track = null;
            }
        }
    }

    // ------------------------------------------------------------------ 播放线程

    private void loop() {
        while (!released) {
            byte[] data;
            try {
                data = queue.poll(300, TimeUnit.MILLISECONDS);
            } catch (InterruptedException e) {
                Thread.currentThread().interrupt();
                break;
            }
            if (data == null) {
                level = 0f;
                continue;
            }
            AudioTrack t;
            synchronized (lock) {
                t = track;
                if (t == null) continue;
                if (paused) {
                    try {
                        t.play();
                        paused = false;
                    } catch (Exception e) {
                        continue;
                    }
                }
            }
            try {
                int off = 0;
                while (off < data.length && !released) {
                    int n = t.write(data, off, data.length - off);
                    if (n <= 0) break;
                    off += n;
                }
            } catch (Exception e) {
                Log.w(TAG, "write failed: " + e);
            }
        }
    }

    private void openTrack(int sr, int ch, int b) {
        synchronized (lock) {
            if (sampleRate == sr && channels == ch && bits == b && track != null) return;
            sampleRate = sr;
            channels = ch;
            bits = b;
            if (track != null) {
                try { track.pause(); } catch (Exception ignored) { }
                try { track.flush(); } catch (Exception ignored) { }
                try { track.release(); } catch (Exception ignored) { }
                track = null;
            }
            try {
                int channelMask = ch == 2 ? AudioFormat.CHANNEL_OUT_STEREO
                        : AudioFormat.CHANNEL_OUT_MONO;
                int enc = bits == 8 ? AudioFormat.ENCODING_PCM_8BIT
                        : (bits == 32 ? AudioFormat.ENCODING_PCM_FLOAT : AudioFormat.ENCODING_PCM_16BIT);
                int min = AudioTrack.getMinBufferSize(sr, channelMask, enc);
                int buf = Math.max(min * 2, sr * Math.max(1, ch) * 2 / 2);
                track = new AudioTrack.Builder()
                        .setAudioAttributes(new AudioAttributes.Builder()
                                .setUsage(AudioAttributes.USAGE_VOICE_COMMUNICATION)
                                .setContentType(AudioAttributes.CONTENT_TYPE_SPEECH)
                                .build())
                        .setAudioFormat(new AudioFormat.Builder()
                                .setEncoding(enc)
                                .setSampleRate(sr)
                                .setChannelMask(channelMask)
                                .build())
                        .setBufferSizeInBytes(buf)
                        .setTransferMode(AudioTrack.MODE_STREAM)
                        .build();
                track.play();
                paused = false;
                Log.i(TAG, "AudioTrack " + sr + "Hz/" + ch + "ch/" + bits + "bit, buf=" + buf);
            } catch (Exception e) {
                Log.w(TAG, "openTrack failed: " + e);
                track = null;
            }
        }
    }
}
