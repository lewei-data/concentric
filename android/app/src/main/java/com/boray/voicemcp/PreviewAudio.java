package com.boray.voicemcp;

import android.content.Context;
import android.os.Handler;
import android.os.Looper;
import android.util.Base64;

import org.json.JSONArray;
import org.json.JSONObject;

import java.io.File;
import java.io.FileOutputStream;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;

/**
 * 档案页「试听汇报」的真实语音来源。
 *
 * 旧实现跳进 ReportActivity 时不带音频，只能用「文本音节估算包络 + 系统 TTS」，
 * 口型与听到的声音并不是同一份音频，所以看起来不对嘴。
 * 这里改用服务端 tts_synthesize（同一 MCP 服务、员工档案里的专属音色）合成 WAV，
 * 把真实音频交给 LipSync.fromWav + MediaPlayer.getCurrentPosition()，
 * 口型就能逐帧跟随真实语音；失败时 ReportActivity 还有本地 synthesizeToFile 兜底。
 */
public final class PreviewAudio {

    private static final ExecutorService IO = Executors.newSingleThreadExecutor();
    private static final Handler UI = new Handler(Looper.getMainLooper());

    public interface Callback {
        /** wavPath 为空表示失败（调用方会走本地合成兜底） */
        void onDone(String wavPath, String error);
    }

    private PreviewAudio() {
    }

    public static void synthesize(Context c, Employee e, String text, Callback cb) {
        final Context app = c.getApplicationContext();
        final String voice = e == null ? "" : e.voice;
        final String id = (e == null || e.id.isEmpty()) ? "unknown" : e.id;
        final String body = text.length() > 500 ? text.substring(0, 500) : text;
        IO.execute(() -> {
            String path = "";
            String error = "";
            try {
                McpConfig cfg = McpConfig.load(app).copy();
                cfg.url = McpConfig.TTS_URL;     // 同一服务端的 /conring/tts/mcp，不是第三方地址
                JSONObject args = new JSONObject();
                args.put("text", body);
                if (!voice.isEmpty()) args.put("voice", voice);
                args.put("return_audio", true);
                JSONObject res = new McpClient(cfg).callTool("tts_synthesize", args);

                byte[] wav = null;
                JSONArray content = res.optJSONArray("content");
                if (content != null) {
                    for (int i = 0; i < content.length(); i++) {
                        JSONObject b = content.optJSONObject(i);
                        if (b == null) continue;
                        String type = b.optString("type", "");
                        String mime = b.optString("mimeType", "");
                        String data = b.optString("data", b.optString("blob", ""));
                        if (data.isEmpty()) continue;
                        if (!"audio".equals(type) && !"blob".equals(type) && !mime.startsWith("audio")) {
                            continue;
                        }
                        byte[] decoded = Base64.decode(data, Base64.DEFAULT);
                        if (decoded.length > 1024) { wav = decoded; break; }
                    }
                }
                if (wav == null) throw new Exception("TTS 未返回音频内容块");

                File out = new File(app.getCacheDir(), "preview_" + id + ".wav");
                try (FileOutputStream os = new FileOutputStream(out)) {
                    os.write(wav);
                }
                path = out.getAbsolutePath();
            } catch (Exception ex) {
                error = ex.getMessage() == null ? "试听语音合成失败" : ex.getMessage();
            }
            final String p = path, m = error;
            UI.post(() -> { if (cb != null) cb.onDone(p, m); });
        });
    }
}
