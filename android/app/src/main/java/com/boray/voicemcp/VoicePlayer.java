package com.boray.voicemcp;

import android.content.Context;
import android.media.MediaPlayer;
import android.speech.tts.TextToSpeech;

import java.util.Locale;

/**
 * 语音播报：优先播放服务端返回的合成音频（voice_chat 自带 audio 内容块），
 * 没有音频时回落到系统 TTS 朗读结果文字。
 */
public final class VoicePlayer {

    public interface PlayCallback {
        void onDone(boolean ok);
    }

    private MediaPlayer mediaPlayer;
    private TextToSpeech tts;
    private volatile boolean ttsReady;

    public VoicePlayer(Context context) {
        try {
            tts = new TextToSpeech(context.getApplicationContext(), status -> {
                if (status == TextToSpeech.SUCCESS && tts != null) {
                    int r = tts.setLanguage(Locale.CHINESE);
                    ttsReady = (r != TextToSpeech.LANG_MISSING_DATA && r != TextToSpeech.LANG_NOT_SUPPORTED);
                }
            });
        } catch (Exception e) {
            tts = null;   // 部分定制系统没有 TTS 引擎，此时只用服务端音频
        }
    }

    public void playFile(String path, PlayCallback callback) {
        stop();
        try {
            mediaPlayer = new MediaPlayer();
            mediaPlayer.setDataSource(path);
            mediaPlayer.setOnCompletionListener(mp -> { if (callback != null) callback.onDone(true); });
            mediaPlayer.setOnErrorListener((mp, what, extra) -> {
                if (callback != null) callback.onDone(false);
                return true;
            });
            mediaPlayer.prepare();
            mediaPlayer.start();
        } catch (Exception e) {
            if (callback != null) callback.onDone(false);
        }
    }

    public void speak(String text) {
        if (tts == null || !ttsReady || text == null || text.trim().isEmpty()) return;
        String s = text.length() > 500 ? text.substring(0, 500) : text;
        try {
            tts.speak(s, TextToSpeech.QUEUE_FLUSH, null, "mcp-result");
        } catch (Exception ignored) {
        }
    }

    public void stop() {
        if (mediaPlayer != null) {
            try {
                if (mediaPlayer.isPlaying()) mediaPlayer.stop();
                mediaPlayer.release();
            } catch (Exception ignored) {
            }
            mediaPlayer = null;
        }
        if (tts != null) {
            try { tts.stop(); } catch (Exception ignored) {
            }
        }
    }

    public void release() {
        stop();
        if (tts != null) {
            try { tts.shutdown(); } catch (Exception ignored) {
            }
            tts = null;
        }
    }
}
