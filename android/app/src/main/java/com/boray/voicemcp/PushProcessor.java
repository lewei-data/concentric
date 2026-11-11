package com.boray.voicemcp;

import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.PendingIntent;
import android.content.Context;
import android.content.Intent;
import android.os.Build;

import java.io.File;
import java.io.FileOutputStream;
import java.io.InputStream;
import java.net.HttpURLConnection;
import java.net.URL;
import java.util.List;

/**
 * 所有「新消息」的统一出口：写历史 → 直接播报 → 系统通知 →（App 在前台时）唤起数字员工汇报画面。
 * 语音指令的异步结果、SSE 推送、CEO 新邮件都走这里，避免三套重复逻辑。
 */
public final class PushProcessor {

    public static final String CHANNEL_LISTEN = "conring_listen";
    public static final String CHANNEL_INCOMING = "conring_incoming";

    private PushProcessor() {
    }

    public static void ensureChannels(Context c) {
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.O) return;
        NotificationManager nm = c.getSystemService(NotificationManager.class);
        if (nm == null) return;
        NotificationChannel listen = new NotificationChannel(CHANNEL_LISTEN,
                c.getString(R.string.channel_listen), NotificationManager.IMPORTANCE_LOW);
        listen.setDescription("后台保持监听语音任务结果与服务端推送");
        nm.createNotificationChannel(listen);
        NotificationChannel incoming = new NotificationChannel(CHANNEL_INCOMING,
                c.getString(R.string.channel_incoming), NotificationManager.IMPORTANCE_HIGH);
        incoming.setDescription("收到新消息时提醒，可直接点开查看");
        nm.createNotificationChannel(incoming);
    }

    /** 写入 / 更新历史记录，返回记录 id */
    public static long save(Context c, McpResult r) {
        HistoryDb.Record rec = new HistoryDb.Record();
        rec.timeMs = System.currentTimeMillis();
        rec.kind = r.source == null || r.source.isEmpty() ? "voice" : r.source;
        rec.messageId = r.messageId;
        rec.jobId = r.jobId;
        rec.question = r.questionText;
        rec.answer = r.bestText();
        rec.detail = r.detailText;
        rec.ack = r.ackText;
        rec.status = r.status;
        rec.employee = r.employeeName;
        rec.filesJson = HistoryDb.filesToJson(r.files);
        rec.rawJson = r.rawJson;
        McpResult.Item audio = r.audioItem();
        if (audio != null) rec.audioPath = audio.path;
        McpResult.Item video = r.videoItem();
        if (video != null) rec.videoPath = video.path;
        rec.videoUrl = r.videoUrl;
        rec.photoUrl = r.photoUrl;
        rec.mode = r.mode;
        rec.segmentsJson = r.segmentsJson;
        rec.avatarId = r.avatarId;
        rec.delegate = r.delegateName.isEmpty() ? r.delegateId : r.delegateName;
        rec.voice = r.voice;
        rec.errorText = r.errorText;
        return HistoryDb.get(c).upsert(rec);
    }

    /**
     * 收到消息后的完整动作。
     * @param player 后台 Service 持有的播放器；为 null 时跳过语音播报（只写历史 + 通知）
     */
    public static long process(Context c, McpResult r, VoicePlayer player, boolean allowUi) {
        long id = save(c, r);
        cacheVideoIfNeeded(c, r, id);
        if (player != null && McpConfig.load(c).autoPlay) {
            announce(c, r, player);
        }
        notifyIncoming(c, id, r);
        if (allowUi && MainActivity.isVisible && McpConfig.load(c).reportEnabled) {
            launchReport(c, r);
        }
        return id;
    }

    /**
     * 服务端给的是数字人视频同源路径（/conring/asr/assets/videos/xxx.talk.mp4），
     * AssetUrls 拼成 MCP origin 后尝试缓存到本机，让历史里的视频也能离线长期回看。
     * 2026-11-10 实测公网 200 / Range 206，无需 VPN 或 Token；失败时静默保留 URL，
     * 详情界面仍可在线播放。
     */
    private static void cacheVideoIfNeeded(Context c, McpResult r, long recordId) {
        if (r.videoUrl == null || r.videoUrl.isEmpty() || r.videoItem() != null) return;
        final String url = r.videoUrl;
        new Thread(() -> {
            HttpURLConnection conn = null;
            File target = null;
            try {
                conn = (HttpURLConnection) new URL(url).openConnection();
                conn.setRequestMethod("GET");
                conn.setConnectTimeout(12000);
                conn.setReadTimeout(20000);
                conn.setInstanceFollowRedirects(true);
                // 同源公开静态资源，服务端明确无需 Token；不复用 /mcp 的 Bearer
                int code = conn.getResponseCode();
                if (code != 200) return;
                String mime = conn.getContentType() == null ? "video/mp4" : conn.getContentType();
                if (!mime.startsWith("video/")) mime = "video/mp4";
                String ext = ResultStore.extensionForMime(mime);
                target = new File(ResultStore.historyDir(c),
                        "video_" + System.currentTimeMillis() + "." + ext);
                long total = 0;
                try (InputStream in = conn.getInputStream();
                     FileOutputStream out = new FileOutputStream(target)) {
                    byte[] buf = new byte[16384];
                    int n;
                    while ((n = in.read(buf)) != -1) {
                        out.write(buf, 0, n);
                        total += n;
                        if (total > 60L * 1024 * 1024) {
                            throw new java.io.IOException("video too large");
                        }
                    }
                }
                if (total < 1024) return;
                HistoryDb.Record rec = HistoryDb.get(c).get(recordId);
                if (rec == null) return;
                rec.videoPath = target.getAbsolutePath();
                List<McpResult.Item> files = HistoryDb.jsonToFiles(rec.filesJson);
                McpResult.Item item = new McpResult.Item(target.getName(), mime,
                        rec.videoPath, ResultStore.uriForHistory(c, target.getName()), total);
                item.label = "汇报视频";
                files.add(item);
                rec.filesJson = HistoryDb.filesToJson(files);
                HistoryDb.get(c).upsert(rec);
            } catch (Exception ignored) {
                if (target != null) {
                    //noinspection ResultOfMethodCallIgnored
                    target.delete();
                }
            } finally {
                if (conn != null) conn.disconnect();
            }
        }, "conring-video-cache").start();
    }

    /** 直接播报：有服务端音频就播音频，否则用系统 TTS 朗读核心内容 */
    public static void announce(Context c, McpResult r, VoicePlayer player) {
        McpResult.Item audio = r.audioItem();
        String text = r.answerText.isEmpty() ? r.bestText() : r.answerText;
        if (audio != null && audio.path != null && !audio.path.isEmpty()) {
            player.playFile(audio.path, ok -> {
                if (!ok) player.speak(text);
            });
        } else {
            player.speak(text);
        }
    }

    private static void notifyIncoming(Context c, long recordId, McpResult r) {
        NotificationManager nm = c.getSystemService(NotificationManager.class);
        if (nm == null) return;
        String title = r.isError ? "同心圆 · 任务失败"
                : "mail".equals(r.source) ? "同心圆 · 新邮件"
                : "push".equals(r.source) ? "同心圆 · 新消息" : "同心圆 · 任务完成";
        String body = r.bestText();
        if (body.length() > 90) body = body.substring(0, 90) + "…";
        Intent open = new Intent(c, HistoryDetailActivity.class)
                .putExtra(HistoryDetailActivity.EXTRA_ID, recordId);
        open.setFlags(Intent.FLAG_ACTIVITY_NEW_TASK | Intent.FLAG_ACTIVITY_CLEAR_TOP);
        int flags = PendingIntent.FLAG_UPDATE_CURRENT;
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.M) flags |= PendingIntent.FLAG_IMMUTABLE;
        PendingIntent pi = PendingIntent.getActivity(c, (int) recordId, open, flags);

        Notification.Builder b = Build.VERSION.SDK_INT >= Build.VERSION_CODES.O
                ? new Notification.Builder(c, CHANNEL_INCOMING)
                : new Notification.Builder(c);
        b.setSmallIcon(R.drawable.ic_conring)
                .setContentTitle(title)
                .setContentText(body)
                .setStyle(new Notification.BigTextStyle().bigText(body))
                .setAutoCancel(true)
                .setContentIntent(pi);
        try {
            nm.notify((int) (recordId & 0x7fffffff), b.build());
        } catch (Exception ignored) {
        }
    }

    /** App 在前台时，把消息交给数字员工汇报画面（音频驱动口型） */
    private static void launchReport(Context c, McpResult r) {
        Intent i = new Intent(c, ReportActivity.class);
        i.putExtra(ReportActivity.EXTRA_ANSWER,
                r.answerText.isEmpty() ? r.bestText() : r.answerText);
        i.putExtra(ReportActivity.EXTRA_QUESTION, r.questionText);
        McpResult.Item audio = r.audioItem();
        if (audio != null && audio.path != null) {
            i.putExtra(ReportActivity.EXTRA_AUDIO, audio.path);
        }
        i.putExtra(ReportActivity.EXTRA_IS_ERROR, r.isError);
        i.putExtra(ReportActivity.EXTRA_EMPLOYEE, r.avatarId);
        if (!r.employeeName.isEmpty()) {
            i.putExtra(ReportActivity.EXTRA_EMPLOYEE_NAME, r.employeeName);
        }
        if (!r.segmentsJson.isEmpty()) i.putExtra(ReportActivity.EXTRA_SEGMENTS, r.segmentsJson);
        if (!r.photoUrl.isEmpty()) i.putExtra(ReportActivity.EXTRA_PHOTO, r.photoUrl);
        if (!r.videoUrl.isEmpty()) i.putExtra(ReportActivity.EXTRA_VIDEO, r.videoUrl);
        if (!r.faceUrl.isEmpty()) i.putExtra(ReportActivity.EXTRA_FACE, r.faceUrl);
        i.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK);
        try {
            c.startActivity(i);
        } catch (Exception ignored) {
        }
    }
}
