package com.boray.voicemcp;

import java.io.FileInputStream;
import java.io.IOException;
import java.util.Arrays;

/**
 * 唇形同步引擎：把服务端返回的 WAV 解析成「毫秒 -> 音量 / 口型」时间轴。
 *
 * 为什么不用系统 Visualizer：Visualizer 要额外权限、在部分定制 ROM 上不可用，
 * 且拿到的是播放后的实时数据；这里直接从落盘的 PCM 预计算整段包络，
 * 播放时按 MediaPlayer.getCurrentPosition() 查表，稳定、可重播、可离线。
 *
 * 没有音频（系统 TTS / 演示模式）时用文本音节估算一条合成包络，保证汇报画面照样有口型。
 */
public final class LipSync {

    public static final int FRAME_MS = 40;
    public static final int SHAPE_CLOSED = 0;
    public static final int SHAPE_A = 1;
    public static final int SHAPE_E = 2;
    public static final int SHAPE_O = 3;
    public static final int SHAPE_U = 4;

    public final int durationMs;
    public final float[] levels;
    public final int[] shapes;
    /** true = 来自真实 WAV；false = 文本合成包络 */
    public final boolean fromAudio;

    private LipSync(int durationMs, float[] levels, int[] shapes, boolean fromAudio) {
        this.durationMs = Math.max(1, durationMs);
        this.levels = levels;
        this.shapes = shapes;
        this.fromAudio = fromAudio;
    }

    public static LipSync fromWav(String path) {
        if (path == null) return null;
        byte[] all = readFile(path);
        if (all == null || all.length < 44) return null;
        Wav w = parseWav(all);
        if (w == null || w.dataLen < 160) return null;
        float[] rms = computeRms(all, w);
        if (rms == null || rms.length == 0) return null;
        return build(rms, w.durationMs(), true);
    }

    /** 无音频时按文本长度与标点生成音节节奏包络 */
    public static LipSync synthetic(String text) {
        String s = text == null ? "" : text.trim();
        int chars = 0;
        for (int i = 0; i < s.length(); i++) {
            char c = s.charAt(i);
            if (!Character.isWhitespace(c)) chars++;
        }
        int durationMs = Math.max(1800, Math.min(90000, chars * 260 + 900));
        int frames = Math.max(2, durationMs / FRAME_MS);
        float[] rms = new float[frames];
        for (int i = 0; i < frames; i++) {
            long t = (long) i * FRAME_MS;
            float syllable = (float) Math.sin(t * Math.PI / 150.0);
            float envelope = 0.55f + 0.45f * (float) Math.sin(t * Math.PI / 470.0);
            float pause = ((t / 1400) % 7 == 6) ? 0.08f : 1f;   // 句读停顿
            rms[i] = Math.max(0f, syllable) * envelope * pause * 0.8f + 0.05f;
        }
        return build(rms, durationMs, false);
    }

    private static LipSync build(float[] rms, int durationMs, boolean fromAudio) {
        int n = rms.length;
        // 用 90 分位做归一化，避免个别爆音把整段口型压平
        float[] sorted = Arrays.copyOf(rms, n);
        Arrays.sort(sorted);
        float ref = sorted[Math.min(n - 1, (int) (n * 0.90f))];
        if (ref < 0.01f) ref = 0.01f;

        float[] raw = new float[n];
        for (int i = 0; i < n; i++) raw[i] = Math.min(1f, rms[i] / ref);
        // 三点平滑，口型不至于抖动
        float[] levels = new float[n];
        for (int i = 0; i < n; i++) {
            float a = raw[Math.max(0, i - 1)], b = raw[i], c = raw[Math.min(n - 1, i + 1)];
            float v = (a + 2 * b + c) / 4f;
            levels[i] = v < 0.09f ? 0f : v;
        }
        int[] shapes = new int[n];
        for (int i = 0; i < n; i++) {
            if (levels[i] < 0.10f) {
                shapes[i] = SHAPE_CLOSED;
            } else {
                // 确定性伪随机：同一段音频每次播报口型一致，且元音会轮换
                int h = (i * 1103515245 + 12345) >>> 12;
                int pick = h % 10;
                if (levels[i] > 0.72f) shapes[i] = pick < 6 ? SHAPE_A : SHAPE_E;
                else if (pick < 4) shapes[i] = SHAPE_E;
                else if (pick < 7) shapes[i] = SHAPE_A;
                else if (pick < 9) shapes[i] = SHAPE_O;
                else shapes[i] = SHAPE_U;
            }
        }
        return new LipSync(durationMs, levels, shapes, fromAudio);
    }

    public float levelAt(int ms) {
        if (levels.length == 0) return 0f;
        float pos = Math.max(0, Math.min(levels.length - 1.001f, (float) ms / FRAME_MS));
        int i = (int) pos;
        float frac = pos - i;
        return levels[i] * (1 - frac) + levels[Math.min(levels.length - 1, i + 1)] * frac;
    }

    public int shapeAt(int ms) {
        if (shapes.length == 0) return SHAPE_CLOSED;
        int i = Math.max(0, Math.min(shapes.length - 1, ms / FRAME_MS));
        return shapes[i];
    }

    // --------------------------------------------------------------- WAV 解析

    private static final class Wav {
        int channels, sampleRate, bits, byteRate, dataOffset, dataLen;

        int durationMs() {
            if (byteRate <= 0) return 1000;
            return (int) Math.min(600000L, (long) dataLen * 1000L / byteRate);
        }
    }

    private static Wav parseWav(byte[] b) {
        if (!ascii(b, 0, 4, "RIFF") || !ascii(b, 8, 4, "WAVE")) return null;
        Wav w = new Wav();
        int pos = 12;
        boolean fmt = false;
        while (pos + 8 <= b.length) {
            String id = new String(b, pos, 4, java.nio.charset.StandardCharsets.US_ASCII);
            int len = le32(b, pos + 4);
            int body = pos + 8;
            if (len < 0 || body + len > b.length) break;
            if ("fmt ".equals(id) && len >= 16) {
                int format = le16(b, body);
                w.channels = le16(b, body + 2);
                w.sampleRate = le32(b, body + 4);
                w.byteRate = le32(b, body + 8);
                w.bits = le16(b, body + 14);
                if (format != 1 || w.channels < 1 || w.channels > 2
                        || w.sampleRate < 8000 || (w.bits != 8 && w.bits != 16)) return null;
                fmt = true;
            } else if ("data".equals(id)) {
                w.dataOffset = body;
                w.dataLen = len;
                break;
            }
            pos = body + len + (len % 2);
        }
        if (!fmt || w.dataLen <= 0 || w.byteRate <= 0) return null;
        return w;
    }

    private static float[] computeRms(byte[] b, Wav w) {
        int bytesPerFrame = w.sampleRate * FRAME_MS / 1000 * w.channels * (w.bits / 8);
        if (bytesPerFrame <= 0) return null;
        int frames = Math.max(1, w.dataLen / bytesPerFrame);
        frames = Math.min(frames, 15000);   // 最长 10 分钟，防止异常文件
        float[] out = new float[frames];
        int bytesPerSample = w.bits / 8;
        for (int f = 0; f < frames; f++) {
            int start = w.dataOffset + f * bytesPerFrame;
            int end = Math.min(w.dataOffset + w.dataLen, start + bytesPerFrame);
            double sum = 0;
            int count = 0;
            for (int i = start; i + bytesPerSample <= end; i += bytesPerSample) {
                float s;
                if (w.bits == 16) {
                    s = (short) (b[i] & 0xff | (b[i + 1] & 0xff) << 8) / 32768f;
                } else {
                    s = ((b[i] & 0xff) - 128) / 128f;
                }
                sum += s * s;
                count++;
            }
            out[f] = count == 0 ? 0f : (float) Math.sqrt(sum / count);
        }
        return out;
    }

    private static boolean ascii(byte[] b, int off, int len, String expect) {
        if (off + len > b.length) return false;
        return expect.equals(new String(b, off, len, java.nio.charset.StandardCharsets.US_ASCII));
    }

    private static int le16(byte[] b, int p) {
        if (p + 2 > b.length) return 0;
        return (b[p] & 0xff) | ((b[p + 1] & 0xff) << 8);
    }

    private static int le32(byte[] b, int p) {
        if (p + 4 > b.length) return 0;
        return (b[p] & 0xff) | ((b[p + 1] & 0xff) << 8)
                | ((b[p + 2] & 0xff) << 16) | ((b[p + 3] & 0xff) << 24);
    }

    private static byte[] readFile(String path) {
        try (FileInputStream in = new FileInputStream(path)) {
            byte[] buf = new byte[in.available()];
            int off = 0, n;
            while (off < buf.length && (n = in.read(buf, off, buf.length - off)) > 0) off += n;
            return buf;
        } catch (IOException | OutOfMemoryError e) {
            return null;
        }
    }
}
