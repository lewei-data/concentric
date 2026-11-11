package com.boray.voicemcp;

import java.io.ByteArrayOutputStream;
import java.util.Arrays;

/**
 * 下行 WAV 流解析（纯逻辑，可在 JVM 上校验）。
 *
 * <p>服务端不是「一整段一个 wav」发下来的，而是：
 * <ol>
 *   <li>先单独发一帧 44 字节的 RIFF 头；</li>
 *   <li>再按 100ms 一块发 PCM 载荷（见服务端 {@code _send_wav_paced}）；</li>
 *   <li>一段回复有多句时，句与句之间会<b>紧接着</b>再来一组「头 + 载荷」，
 *       也就是一条连接上会连续出现多个 WAV。</li>
 * </ol>
 * 所以接收端必须自己识别「新的 RIFF 头」并按实际采样率重配播放器，
 * 不能假设整条连接只有一个 wav 头，也不能把二进制帧当成完整文件落盘。
 */
public final class WavStream {

    public interface Sink {
        /** 采样格式发生变化（或首次出现）时回调，播放器据此重建 AudioTrack */
        void onFormat(Format format);

        /** 可直接送进 AudioTrack 的 PCM 数据（16bit little-endian 交错） */
        void onPcm(byte[] data, int off, int len);
    }

    public static final class Format {
        public int sampleRate = 16000;
        public int channels = 1;
        public int bits = 16;

        public int bytesPerFrame() {
            return Math.max(1, channels * Math.max(8, bits) / 8);
        }

        public boolean sameAs(Format o) {
            return o != null && sampleRate == o.sampleRate
                    && channels == o.channels && bits == o.bits;
        }

        public String toString() {
            return sampleRate + "Hz/" + channels + "ch/" + bits + "bit";
        }
    }

    /** 裸 PCM 兜底：攒够这么多字节还没见到 RIFF 就当 16k/16bit/单声道 直接播 */
    private static final int RAW_FALLBACK_BYTES = 2048;

    private final Sink sink;
    private final ByteArrayOutputStream pending = new ByteArrayOutputStream();
    private final Format format = new Format();
    private boolean payload = false;
    /** 是否已经把格式告诉过播放器（首段即使是默认 16k 也必须回调，否则播放器不会建 AudioTrack） */
    private boolean configured = false;

    public WavStream(Sink sink) {
        this.sink = sink;
    }

    public Format format() {
        return format;
    }

    /** 送入一个 WS 二进制帧（可能是一个 44B 头，也可能是任意长度的载荷） */
    public void feed(byte[] data) {
        if (data == null || data.length == 0) return;
        // 载荷刚播完、缓冲为空时，新到的一帧若以 RIFF 开头 → 下一段 wav 的头
        if (payload && pending.size() == 0 && isRiff(data, 0)) payload = false;

        if (payload) {
            sink.onPcm(data, 0, data.length);
            return;
        }

        pending.write(data, 0, data.length);
        byte[] b = pending.toByteArray();

        if (b.length < 12) return;                       // 头还没收全
        if (!isRiff(b, 0)) {
            if (b.length < RAW_FALLBACK_BYTES) return;   // 可能是畸形的头，再等等
            pending.reset();
            payload = true;
            if (!configured) {
                configured = true;
                sink.onFormat(format);
            }
            sink.onPcm(b, 0, b.length);
            return;
        }

        int dataOff = findDataOffset(b);
        if (dataOff < 0) {
            // 服务端所谓「44 字节头」只是把文件前 44 字节单独发一帧；CosyVoice 的 wav
            // 在 fmt 之后还带 LIST/INFO 块，data 往往在 90~120 字节处，因此头的后半段
            // 会混在第一个“载荷”帧里。这里必须继续攒、按 chunk 走到 data 再切，
            // 不能按固定 44 字节切，否则会把 INFO 头当 PCM 播成杂音。
            if (b.length > 65536) {                      // 头畸形超大：放弃识别，按裸流播
                pending.reset();
                payload = true;
                if (!configured) {
                    configured = true;
                    sink.onFormat(format);
                }
                sink.onPcm(b, 0, b.length);
            }
            return;
        }
        Format f = parseFormat(b, format);
        boolean changed = !f.sameAs(format);
        if (changed) {
            format.sampleRate = f.sampleRate;
            format.channels = f.channels;
            format.bits = f.bits;
        }
        if (changed || !configured) {
            configured = true;
            sink.onFormat(format);
        }
        byte[] rest = Arrays.copyOfRange(b, dataOff, b.length);
        pending.reset();
        payload = true;
        if (rest.length > 0) sink.onPcm(rest, 0, rest.length);
    }

    /** 丢弃缓冲（barge-in / 重连时用） */
    public void reset() {
        pending.reset();
        payload = false;
        configured = false;
    }

    // ------------------------------------------------------------------ 解析

    static boolean isRiff(byte[] b, int off) {
        return b.length - off >= 12
                && b[off] == 'R' && b[off + 1] == 'I' && b[off + 2] == 'F' && b[off + 3] == 'F'
                && b[off + 8] == 'W' && b[off + 9] == 'A' && b[off + 10] == 'V' && b[off + 11] == 'E';
    }

    /** 顺序扫 chunk，返回 data 载荷起始偏移；找不到返回 -1 */
    static int findDataOffset(byte[] b) {
        int pos = 12;
        while (pos + 8 <= b.length) {
            int size = (b[pos + 4] & 0xFF) | ((b[pos + 5] & 0xFF) << 8)
                    | ((b[pos + 6] & 0xFF) << 16) | ((b[pos + 7] & 0xFF) << 24);
            if (b[pos] == 'd' && b[pos + 1] == 'a' && b[pos + 2] == 't' && b[pos + 3] == 'a') {
                return pos + 8;
            }
            if (size < 0) return -1;
            pos += 8 + size + (size & 1);
        }
        return -1;
    }

    /** 读 fmt chunk；缺失时保留默认值 */
    static Format parseFormat(byte[] b, Format def) {
        Format f = new Format();
        int pos = 12;
        while (pos + 8 <= b.length) {
            int size = (b[pos + 4] & 0xFF) | ((b[pos + 5] & 0xFF) << 8)
                    | ((b[pos + 6] & 0xFF) << 16) | ((b[pos + 7] & 0xFF) << 24);
            boolean fmt = b[pos] == 'f' && b[pos + 1] == 'm' && b[pos + 2] == 't' && b[pos + 3] == ' ';
            if (fmt && pos + 24 <= b.length) {
                int channels = (b[pos + 10] & 0xFF) | ((b[pos + 11] & 0xFF) << 8);
                int rate = (b[pos + 12] & 0xFF) | ((b[pos + 13] & 0xFF) << 8)
                        | ((b[pos + 14] & 0xFF) << 16) | ((b[pos + 15] & 0xFF) << 24);
                int bits = (b[pos + 22] & 0xFF) | ((b[pos + 23] & 0xFF) << 8);
                if (rate > 4000 && rate <= 192000) f.sampleRate = rate;
                if (channels >= 1 && channels <= 8) f.channels = channels;
                if (bits == 8 || bits == 16 || bits == 24 || bits == 32) f.bits = bits;
                return f;
            }
            if (size < 0) break;
            pos += 8 + size + (size & 1);
        }
        if (def != null) {
            f.sampleRate = def.sampleRate;
            f.channels = def.channels;
            f.bits = def.bits;
        }
        return f;
    }

    /** PCM 音量（0..1），给「对方在说话」的电平条用 */
    public static float level(byte[] pcm, int off, int len) {
        if (pcm == null || len < 2) return 0f;
        long sum = 0;
        int count = 0;
        int end = Math.min(off + len, pcm.length);
        for (int i = off; i + 1 < end; i += 2) {
            int s = (short) ((pcm[i] & 0xFF) | (pcm[i + 1] << 8));
            sum += (long) s * s;
            count++;
        }
        if (count == 0) return 0f;
        float rms = (float) Math.sqrt(sum / (double) count);
        return Math.min(1f, rms / 7000f);
    }
}
