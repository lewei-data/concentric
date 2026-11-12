package com.boray.voicemcp;

import android.media.AudioFormat;
import android.media.AudioRecord;
import android.media.MediaRecorder;
import android.media.audiofx.AcousticEchoCanceler;
import android.media.audiofx.NoiseSuppressor;

import java.io.ByteArrayOutputStream;
import java.util.concurrent.atomic.AtomicBoolean;

/**
 * 通话期间的「持续聆听」录音器（替代按住说话）。
 *
 * 一直开着麦克风，用能量做简易 VAD 自动断句：
 *   - 20ms 一帧算 RMS，动态估计底噪，超过阈值判定为「在说话」
 *   - 说话前保留 300ms 预录，避免吞字
 *   - 停顿超过 700ms 且累计说话 ≥ 400ms → 认为一句话说完，回调一段 WAV
 *   - 单句最长 12s 强制截断，防止一直说不停导致上传过大
 *   - 下行播报期间阈值抬高、最短语音拉长，抑制扬声器回声被当成老板说话
 *   - WAV 为 16kHz / 16bit / 单声道，服务端 ASR 直接可用
 *
 * 回调在录音线程上，调用方自己切线程发网络请求。
 */
public final class LiveMic {

    public interface Listener {
        /** 一句话说完，给出一段完整 WAV（<b>不含</b> base64，调用方自己编） */
        void onUtterance(byte[] wav, int durationMs);

        /** 实时音量 0..1，用于界面上的「在听」指示 */
        void onLevel(float level, boolean speaking);
    }

    private static final int SAMPLE_RATE = 16000;
    private static final int FRAME_MS = 20;
    private static final int FRAME_BYTES = SAMPLE_RATE * 2 * FRAME_MS / 1000;
    private static final long PREROLL_MS = 300;
    private static final long SILENCE_END_MS = 700;
    private static final long MIN_SPEECH_MS = 400;
    private static final long MAX_UTTERANCE_MS = 12_000;
    /**
     * 正在播放下行语音时，扬声器回声会进麦克风。AEC 只能压低不能清零，
     * 所以播报期间把判定门槛整体抬高：阈值 ×2.8（约 +9dB），最短语音也拉长，
     * 只有明显高于回声残留的持续人声才当成老板插话。
     */
    private static final float PLAYBACK_THRESHOLD_SCALE = 2.8f;
    private static final long PLAYBACK_MIN_SPEECH_MS = 800;

    private final AtomicBoolean running = new AtomicBoolean(false);
    private AudioRecord record;
    private AcousticEchoCanceler aec;
    private NoiseSuppressor ns;
    private Thread thread;
    private Listener listener;
    private volatile float level = 0f;
    private volatile boolean speaking = false;
    /** 下行语音正在播放：抬高 VAD 门槛，避免把扬声器回声当成老板在说话 */
    private volatile boolean playbackActive = false;

    /** 由通话层在播放开始/结束时同步；只读 volatile，可在任意线程调用 */
    public void setPlaybackActive(boolean active) {
        playbackActive = active;
    }

    public void start(Listener l) {
        if (!running.compareAndSet(false, true)) return;
        listener = l;
        int min = AudioRecord.getMinBufferSize(SAMPLE_RATE,
                AudioFormat.CHANNEL_IN_MONO, AudioFormat.ENCODING_PCM_16BIT);
        int buf = Math.max(min, FRAME_BYTES * 8);
        try {
            // VOICE_COMMUNICATION + AEC：通话时扬声器在放员工的声音，必须消掉回声，
            // 否则麦克风会把播放出来的语音当成用户说话，形成自问自答的死循环。
            record = new AudioRecord(MediaRecorder.AudioSource.VOICE_COMMUNICATION, SAMPLE_RATE,
                    AudioFormat.CHANNEL_IN_MONO, AudioFormat.ENCODING_PCM_16BIT, buf);
        } catch (Exception e) {
            record = null;
        }
        if (record == null || record.getState() != AudioRecord.STATE_INITIALIZED) {
            running.set(false);
            if (record != null) {
                record.release();
                record = null;
            }
            return;
        }
        attachAudioEffects();
        thread = new Thread(this::loop, "live-mic");
        thread.setDaemon(true);
        thread.start();
    }

    public void stop() {
        running.set(false);
        if (thread != null) {
            try { thread.join(600); } catch (InterruptedException e) { Thread.currentThread().interrupt(); }
            thread = null;
        }
        release();
    }

    public float level() {
        return level;
    }

    public boolean isSpeaking() {
        return speaking;
    }

    public boolean isRunning() {
        return running.get();
    }

    /** 回声消除 + 噪声抑制；设备不支持就静默跳过 */
    private void attachAudioEffects() {
        if (record == null) return;
        try {
            if (AcousticEchoCanceler.isAvailable()) {
                aec = AcousticEchoCanceler.create(record.getAudioSessionId());
                if (aec != null) aec.setEnabled(true);
            }
        } catch (Exception ignored) {
        }
        try {
            if (NoiseSuppressor.isAvailable()) {
                ns = NoiseSuppressor.create(record.getAudioSessionId());
                if (ns != null) ns.setEnabled(true);
            }
        } catch (Exception ignored) {
        }
    }

    private void release() {
        if (aec != null) {
            try { aec.setEnabled(false); aec.release(); } catch (Exception ignored) { }
            aec = null;
        }
        if (ns != null) {
            try { ns.setEnabled(false); ns.release(); } catch (Exception ignored) { }
            ns = null;
        }
        AudioRecord r = record;
        record = null;
        if (r != null) {
            try {
                if (r.getRecordingState() == AudioRecord.RECORDSTATE_RECORDING) r.stop();
            } catch (Exception ignored) {
            }
            try { r.release(); } catch (Exception ignored) { }
        }
    }

    private void loop() {
        AudioRecord r = record;
        if (r == null) return;
        byte[] frame = new byte[FRAME_BYTES];
        // 预录环形缓冲：保留最近 PREROLL_MS，开句时先补进去
        int preFrames = (int) (PREROLL_MS / FRAME_MS);
        byte[][] preroll = new byte[preFrames][];
        int preIdx = 0, preCount = 0;

        ByteArrayOutputStream utter = new ByteArrayOutputStream();
        boolean inSpeech = false;
        long silenceMs = 0;
        long speechMs = 0;
        float noise = 300f;              // 初始底噪估计
        long lastLevelPush = 0;

        try {
            r.startRecording();
        } catch (Exception e) {
            running.set(false);
            return;
        }

        while (running.get()) {
            int n = r.read(frame, 0, frame.length);
            if (n <= 0) {
                if (n == AudioRecord.ERROR_INVALID_OPERATION || n == AudioRecord.ERROR_BAD_VALUE) break;
                continue;
            }
            float rms = rms(frame, n);
            level = Math.min(1f, rms / 6000f);          // 0..1 显示用
            // 底噪自适应：没人说话时慢慢跟随环境噪声（播报期间不跟，免得把回声吃进底噪）
            boolean playing = playbackActive;
            if (!playing && rms < noise * 1.6f) noise = noise * 0.98f + rms * 0.02f;
            float threshold = Math.max(420f, noise * 2.6f);
            if (playing) threshold *= PLAYBACK_THRESHOLD_SCALE;
            boolean voiced = rms > threshold;

            long now = System.currentTimeMillis();
            if (now - lastLevelPush > 80) {
                lastLevelPush = now;
                if (listener != null) listener.onLevel(level, inSpeech);
            }

            if (!inSpeech) {
                preroll[preIdx] = copy(frame, n);
                preIdx = (preIdx + 1) % preFrames;
                if (preCount < preFrames) preCount++;
                if (voiced) {
                    inSpeech = true;
                    speaking = true;
                    silenceMs = 0;
                    speechMs = 0;
                    utter.reset();
                    // 把预录补到句首
                    for (int i = 0; i < preCount; i++) {
                        int idx = (preIdx - preCount + i + preFrames * 2) % preFrames;
                        if (preroll[idx] != null) utter.write(preroll[idx], 0, preroll[idx].length);
                    }
                    utter.write(frame, 0, n);
                    speechMs = FRAME_MS;
                }
                continue;
            }

            utter.write(frame, 0, n);
            if (voiced) {
                speechMs += FRAME_MS;
                silenceMs = 0;
            } else {
                silenceMs += FRAME_MS;
            }

            // 播报期间要求更长的持续人声，回声很难连成 800ms
            long minSpeech = playing ? PLAYBACK_MIN_SPEECH_MS : MIN_SPEECH_MS;
            boolean done = silenceMs >= SILENCE_END_MS && speechMs >= minSpeech;
            boolean tooLong = utter.size() >= SAMPLE_RATE * 2 * MAX_UTTERANCE_MS / 1000;
            boolean almostSilent = silenceMs >= 1800;   // 一直没声（可能只是噪声抖动）
            if (done || tooLong || almostSilent) {
                byte[] pcm = utter.toByteArray();
                utter.reset();
                inSpeech = false;
                speaking = false;
                preCount = 0;
                preIdx = 0;
                if (speechMs >= minSpeech && pcm.length > 0) {
                    byte[] wav = wav(pcm);
                    if (listener != null) listener.onUtterance(wav, (int) (pcm.length / 32));
                }
                speechMs = 0;
                silenceMs = 0;
            }
        }
        release();
        running.set(false);
    }

    private static byte[] copy(byte[] src, int n) {
        byte[] d = new byte[n];
        System.arraycopy(src, 0, d, 0, n);
        return d;
    }

    private static float rms(byte[] buf, int len) {
        long sum = 0;
        int count = len / 2;
        for (int i = 0; i + 1 < len; i += 2) {
            int s = (short) ((buf[i] & 0xFF) | (buf[i + 1] << 8));
            sum += (long) s * s;
        }
        if (count == 0) return 0;
        return (float) Math.sqrt(sum / (double) count);
    }

    /** 加标准 WAV 头 */
    private static byte[] wav(byte[] pcm) {
        int len = pcm.length;
        byte[] out = new byte[44 + len];
        writeAscii(out, 0, "RIFF");
        writeInt(out, 4, 36 + len);
        writeAscii(out, 8, "WAVE");
        writeAscii(out, 12, "fmt ");
        writeInt(out, 16, 16);
        writeShort(out, 20, 1);
        writeShort(out, 22, 1);
        writeInt(out, 24, SAMPLE_RATE);
        writeInt(out, 28, SAMPLE_RATE * 2);
        writeShort(out, 32, 2);
        writeShort(out, 34, 16);
        writeAscii(out, 36, "data");
        writeInt(out, 40, len);
        System.arraycopy(pcm, 0, out, 44, len);
        return out;
    }

    private static void writeAscii(byte[] d, int off, String s) {
        for (int i = 0; i < s.length(); i++) d[off + i] = (byte) s.charAt(i);
    }

    private static void writeInt(byte[] d, int off, int v) {
        d[off] = (byte) (v & 0xFF);
        d[off + 1] = (byte) ((v >>> 8) & 0xFF);
        d[off + 2] = (byte) ((v >>> 16) & 0xFF);
        d[off + 3] = (byte) ((v >>> 24) & 0xFF);
    }

    private static void writeShort(byte[] d, int off, int v) {
        d[off] = (byte) (v & 0xFF);
        d[off + 1] = (byte) ((v >>> 8) & 0xFF);
    }
}
