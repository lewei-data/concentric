package com.boray.voicemcp;

import android.content.Context;
import android.media.AudioAttributes;
import android.media.AudioFormat;
import android.media.AudioManager;
import android.media.AudioTrack;
import android.util.Log;

/**
 * 拨号音与接通提示音：本地合成，不依赖任何音频文件、不占 APK 体积。
 *
 * <p>音色走「熟悉的铃声」路子：拨号是一小节三音下行「叮-叮-咚」（988/784/659Hz），
 * 每 1.9s 一轮；接通是两音上行「叮-咚」（784→1047Hz）。
 *
 * <p>播放方式（这是「咔咔响」的根因）：<b>整个会话共用一条 AudioTrack，连续写 PCM</b>。
 * 之前每个音都 new 一条轨、write 完立刻 stop/release —— {@code write()} 只代表数据进了缓冲，
 * 不代表播完，尾巴被截断就成了爆音。现在：
 * <ul>
 *   <li>一次会话只建一条轨（路由切换只发生在开头）；</li>
 *   <li>旋律与静音都预渲染成连续 PCM 循环写入，中间没有"重建轨"的空隙；</li>
 *   <li>收尾先写一段 30ms 淡出到零，再 stop/release，即使被截断也落在静音处，听不见。</li>
 * </ul>
 *
 * <p>另外两个真机坑：{@code USAGE_VOICE_COMMUNICATION}（通话期媒体轨会被压低/走耳筒）；
 * 通话音量为 0 时临时抬到 40%，离开页面还原。
 */
public final class CallTone {

    private static final String TAG = "CallTone";
    private static final int SR = 44100;
    /** 幅度：带衰减包络后 0.45 清晰不刺耳 */
    private static final double AMP = 0.45;
    /** 一次写入的帧数（10ms），够细以便及时响应 stop */
    private static final int CHUNK_FRAMES = 441;
    /** 收尾淡出时长 */
    private static final int FADE_FRAMES = SR * 30 / 1000;
    /** 一轮拨号旋律的总长（含尾部静音） */
    private static final int DIAL_LOOP_MS = 1900;

    private final Context app;

    private volatile boolean dialing = false;
    private Thread worker;
    private int savedVolume = -1;

    public CallTone(Context context) {
        this.app = context == null ? null : context.getApplicationContext();
    }

    public boolean isDialing() {
        return dialing;
    }

    // ------------------------------------------------------------------ 对外

    /** 开始循环拨号音（会先停掉正在播的） */
    public synchronized void startDialing() {
        stopLocked();
        dialing = true;
        boostIfMuted();
        logVolume("startDialing");
        final short[] phrase = renderDialPhrase();
        worker = new Thread(() -> playLoop(phrase), "dial-tone");
        worker.setDaemon(true);
        worker.start();
    }

    /** 接通提示：两声上行「叮-咚」 */
    public synchronized void playConnected() {
        stopLocked();
        worker = new Thread(() -> {
            short[] phrase = renderConnectedPhrase();
            // 起播前等 200ms，让服务把音频模式/外放切好
            sleep(200);
            AudioTrack t = createTrack();
            if (t == null) {
                fallbackBeepOnce();
                return;
            }
            writeAll(t, phrase, 0, phrase.length);
            // 尾部自带 200ms 静音，等它播完再收轨，不会有截断声
            sleep(phrase.length * 1000L / SR + 80);
            fadeAndClose(t);
        }, "connected-tone");
        worker.setDaemon(true);
        worker.start();
    }

    /** 停止一切发声（挂断、失败、页面不可见时都要调） */
    public synchronized void stop() {
        stopLocked();
    }

    /** 页面离开时把临时抬过的通话音量还原 */
    public synchronized void restoreVolumeIfBoosted() {
        if (savedVolume < 0) return;
        try {
            AudioManager am = am();
            if (am != null) am.setStreamVolume(AudioManager.STREAM_VOICE_CALL, savedVolume, 0);
        } catch (Exception ignored) {
        }
        Log.i(TAG, "restore voice-call volume -> " + savedVolume);
        savedVolume = -1;
    }

    private void stopLocked() {
        dialing = false;
        Thread w = worker;
        worker = null;
        if (w != null) w.interrupt();
    }

    // ------------------------------------------------------------------ 播放

    /** 循环写同一段旋律，直到 stop；收尾淡出避免爆音 */
    private void playLoop(short[] phrase) {
        // 先等 350ms：服务刚把音频切到 MODE_IN_COMMUNICATION + 外放，
        // 太早出声会落在切换前的路由上（听筒，声音很小）
        sleep(350);
        if (!dialing) return;
        AudioTrack t = createTrack();
        if (t == null) {
            Log.w(TAG, "AudioTrack 建不起来，改用 ToneGenerator");
            fallbackLoop();
            return;
        }
        Log.i(TAG, "dial loop start, phrase=" + phrase.length + " frames");
        int pos = 0;
        while (dialing) {
            int len = Math.min(CHUNK_FRAMES, phrase.length - pos);
            if (!writeAll(t, phrase, pos, len)) break;
            pos += len;
            if (pos >= phrase.length) pos = 0;
        }
        // 只关自己这条轨：旧会话淡出时不会把新会话的轨关掉（那也是"咔"的来源）
        fadeAndClose(t);
        Log.i(TAG, "dial loop end");
    }

    private AudioTrack createTrack() {
        AudioTrack t = null;
        try {
            int min = AudioTrack.getMinBufferSize(SR, AudioFormat.CHANNEL_OUT_MONO,
                    AudioFormat.ENCODING_PCM_16BIT);
            // 约 200ms 缓冲：够连续，又不至于让声音滞后太多
            int bytes = Math.max(min * 2, SR / 5 * 2);
            t = new AudioTrack.Builder()
                    .setAudioAttributes(new AudioAttributes.Builder()
                            // 通话场景：跟着通话路由走，不会被"通话中压低媒体"波及
                            .setUsage(AudioAttributes.USAGE_VOICE_COMMUNICATION)
                            .setContentType(AudioAttributes.CONTENT_TYPE_SPEECH)
                            .build())
                    .setAudioFormat(new AudioFormat.Builder()
                            .setEncoding(AudioFormat.ENCODING_PCM_16BIT)
                            .setSampleRate(SR)
                            .setChannelMask(AudioFormat.CHANNEL_OUT_MONO)
                            .build())
                    .setBufferSizeInBytes(bytes)
                    .setTransferMode(AudioTrack.MODE_STREAM)
                    .build();
            if (t.getState() != AudioTrack.STATE_INITIALIZED) {
                t.release();
                return null;
            }
            t.setVolume(1f);
            t.play();
            Log.i(TAG, "AudioTrack ready buf=" + bytes + " min=" + min);
            return t;
        } catch (Exception e) {
            Log.w(TAG, "AudioTrack 失败: " + e);
            return null;
        }
    }

    /** 写满一段；返回 false 表示轨已经不可用 */
    private boolean writeAll(AudioTrack t, short[] buf, int off, int len) {
        if (t == null) return false;
        int done = 0;
        while (done < len) {
            int n;
            try {
                n = t.write(buf, off + done, len - done);
            } catch (Exception e) {
                Log.w(TAG, "write: " + e);
                return false;
            }
            if (n <= 0) {
                Log.w(TAG, "write returned " + n);
                return false;
            }
            done += n;
        }
        return true;
    }

    /** 先淡出到零，再停轨——避免"没播完就关"的咔哒声 */
    private void fadeAndClose(AudioTrack t) {
        if (t != null) {
            try {
                short[] fade = new short[FADE_FRAMES];
                for (int i = 0; i < FADE_FRAMES; i++) {
                    // 余弦降到 0，终点严格为静音
                    fade[i] = (short) (Math.cos(Math.PI / 2 * i / (double) FADE_FRAMES) * 6000);
                }
                t.write(fade, 0, fade.length);
            } catch (Exception ignored) {
            }
            // 让淡出真正播出去再收轨（缓冲约 200ms，这里等一小段足够）
            sleep(60);
        }
        closeTrack(t);
    }

    private void closeTrack(AudioTrack t) {
        if (t == null) return;
        try {
            t.stop();
        } catch (Exception ignored) {
        }
        try {
            t.release();
        } catch (Exception ignored) {
        }
    }

    /** 极端情况下 AudioTrack 不可用：退回系统 ToneGenerator 反复响 */
    private void fallbackLoop() {
        android.media.ToneGenerator tg = null;
        try {
            tg = new android.media.ToneGenerator(AudioManager.STREAM_VOICE_CALL, 100);
            while (dialing) {
                tg.startTone(android.media.ToneGenerator.TONE_SUP_DIAL, 800);
                sleep(1800);
            }
        } catch (Exception e) {
            Log.w(TAG, "ToneGenerator 也不可用: " + e);
        } finally {
            if (tg != null) {
                try {
                    tg.stopTone();
                } catch (Exception ignored) {
                }
                try {
                    tg.release();
                } catch (Exception ignored) {
                }
            }
        }
    }

    private void fallbackBeepOnce() {
        android.media.ToneGenerator tg = null;
        try {
            tg = new android.media.ToneGenerator(AudioManager.STREAM_VOICE_CALL, 100);
            tg.startTone(android.media.ToneGenerator.TONE_PROP_BEEP, 200);
            sleep(220);
        } catch (Exception e) {
            Log.w(TAG, "ToneGenerator 也不可用: " + e);
        } finally {
            if (tg != null) {
                try {
                    tg.stopTone();
                } catch (Exception ignored) {
                }
                try {
                    tg.release();
                } catch (Exception ignored) {
                }
            }
        }
    }

    // ------------------------------------------------------------------ 合成

    /** 一轮拨号：988 → 784 → 659，余下补静音到 DIAL_LOOP_MS */
    private static short[] renderDialPhrase() {
        int total = SR * DIAL_LOOP_MS / 1000;
        short[] buf = new short[total];
        int at = 0;
        at = renderNote(buf, at, 988, 150);
        at = gap(buf, at, 90);
        at = renderNote(buf, at, 784, 150);
        at = gap(buf, at, 90);
        renderNote(buf, at, 659, 240);
        return buf;
    }

    /** 接通：784 → 1047，尾部补 200ms 静音，让衰减自然收尾 */
    private static short[] renderConnectedPhrase() {
        int total = SR * (130 + 70 + 220 + 200) / 1000;
        short[] buf = new short[total];
        int at = 0;
        at = renderNote(buf, at, 784, 130);
        at = gap(buf, at, 70);
        renderNote(buf, at, 1047, 220);
        return buf;
    }

    private static int renderNote(short[] dst, int offset, int freq, int ms) {
        int n = Math.max(1, SR * ms / 1000);
        int attack = Math.max(1, SR * 8 / 1000);
        int release = Math.max(1, SR * 30 / 1000);
        for (int i = 0; i < n && offset + i < dst.length; i++) {
            double t = i / (double) SR;
            // 基频 + 25% 二次谐波：比纯正弦更"亮"，像敲铃
            double v = Math.sin(2 * Math.PI * freq * t)
                    + 0.25 * Math.sin(2 * Math.PI * freq * 2 * t);
            v /= 1.25;
            double env = Math.exp(-3.2 * t);      // 指数衰减：120ms 掉到约 35%
            if (i < attack) env *= i / (double) attack;
            if (i >= n - release) env *= (n - i) / (double) release;
            dst[offset + i] = (short) (v * AMP * 32767 * env);
        }
        return offset + n;
    }

    private static int gap(short[] dst, int offset, int ms) {
        return Math.min(dst.length, offset + SR * ms / 1000);
    }

    // ------------------------------------------------------------------ 音量

    private AudioManager am() {
        return app == null ? null : (AudioManager) app.getSystemService(Context.AUDIO_SERVICE);
    }

    /** 通话音量是 0 时抬到 40%，否则不动 */
    private void boostIfMuted() {
        AudioManager am = am();
        if (am == null) return;
        try {
            int cur = am.getStreamVolume(AudioManager.STREAM_VOICE_CALL);
            int max = am.getStreamMaxVolume(AudioManager.STREAM_VOICE_CALL);
            if (cur > 0 || max <= 0) return;
            savedVolume = cur;
            int want = Math.max(1, Math.round(max * 0.4f));
            am.setStreamVolume(AudioManager.STREAM_VOICE_CALL, want, 0);
            Log.i(TAG, "voice-call volume was 0, bumped to " + want + "/" + max);
        } catch (Exception e) {
            Log.w(TAG, "boost: " + e);
        }
    }

    private void logVolume(String where) {
        AudioManager am = am();
        if (am == null) return;
        try {
            Log.i(TAG, where + " vol voice=" + am.getStreamVolume(AudioManager.STREAM_VOICE_CALL)
                    + "/" + am.getStreamMaxVolume(AudioManager.STREAM_VOICE_CALL)
                    + " music=" + am.getStreamVolume(AudioManager.STREAM_MUSIC)
                    + "/" + am.getStreamMaxVolume(AudioManager.STREAM_MUSIC)
                    + " mode=" + am.getMode()
                    + " speaker=" + am.isSpeakerphoneOn());
        } catch (Exception ignored) {
        }
    }

    private static void sleep(long ms) {
        try {
            Thread.sleep(ms);
        } catch (InterruptedException ignored) {
            Thread.currentThread().interrupt();
        }
    }
}
