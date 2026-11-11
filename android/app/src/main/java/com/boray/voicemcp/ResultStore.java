package com.boray.voicemcp;

import android.content.Context;
import android.net.Uri;

import java.io.File;
import java.io.FileOutputStream;
import java.io.IOException;
import java.text.SimpleDateFormat;
import java.util.Arrays;
import java.util.Date;
import java.util.Locale;
import java.util.UUID;

/**
 * 结果文件落盘。
 * results/ 是临时目录（旧的 60 文件上限只作用于它）；
 * history/ 是历史消息专用目录，永久保留，配合 HistoryDb 做长期回看（音频、视频、文字）。
 */
public final class ResultStore {

    public static final String AUTHORITY_SUFFIX = ".files";
    public static final String HISTORY_PREFIX = "history/";

    public static final class Saved {
        public File file;
        public String name;
        public Uri uri;
        public String mime;
    }

    public static File resultDir(Context c) {
        File dir = new File(c.getFilesDir(), "results");
        if (!dir.exists()) dir.mkdirs();
        return dir;
    }

    public static File historyDir(Context c) {
        File dir = new File(c.getFilesDir(), "history");
        if (!dir.exists()) dir.mkdirs();
        return dir;
    }

    /** 临时结果文件（例如手动「查看文字」导出的副本），仍受 60 个文件上限约束 */
    public static Saved save(Context c, String baseName, byte[] data, String mime) {
        return write(c, resultDir(c), baseName, data, mime, false);
    }

    /** 历史消息文件：永久保留，不参与清理；文件名带随机后缀避免同一秒覆盖 */
    public static Saved savePermanent(Context c, String baseName, byte[] data, String mime) {
        return write(c, historyDir(c), baseName, data, mime, true);
    }

    private static Saved write(Context c, File dir, String baseName, byte[] data,
                               String mime, boolean permanent) {
        String stamp = new SimpleDateFormat("yyyyMMdd_HHmmss", Locale.US).format(new Date());
        String name = stamp + "_" + (permanent ? UUID.randomUUID().toString().substring(0, 6) + "_" : "")
                + sanitize(baseName);
        if (!name.contains(".")) name = name + ".txt";
        File file = new File(dir, name);
        try (FileOutputStream fos = new FileOutputStream(file)) {
            fos.write(data);
            fos.flush();
        } catch (IOException e) {
            return null;
        }
        if (!permanent) trim(dir);
        Saved s = new Saved();
        s.file = file;
        s.name = name;
        s.mime = mime;
        s.uri = permanent ? uriForHistory(c, name) : uriFor(c, name);
        return s;
    }

    public static Uri uriFor(Context c, String fileName) {
        return new Uri.Builder()
                .scheme("content")
                .authority(c.getPackageName() + AUTHORITY_SUFFIX)
                .path(fileName)
                .build();
    }

    public static Uri uriForHistory(Context c, String fileName) {
        return new Uri.Builder()
                .scheme("content")
                .authority(c.getPackageName() + AUTHORITY_SUFFIX)
                .path(HISTORY_PREFIX + fileName)
                .build();
    }

    public static String mimeOf(String name) {
        if (name == null) return "application/octet-stream";
        String n = name.toLowerCase(Locale.US);
        if (n.endsWith(".wav")) return "audio/wav";
        if (n.endsWith(".mp3")) return "audio/mpeg";
        if (n.endsWith(".m4a")) return "audio/mp4";
        if (n.endsWith(".ogg")) return "audio/ogg";
        if (n.endsWith(".flac")) return "audio/flac";
        if (n.endsWith(".mp4") || n.endsWith(".m4v")) return "video/mp4";
        if (n.endsWith(".webm")) return "video/webm";
        if (n.endsWith(".3gp")) return "video/3gpp";
        if (n.endsWith(".png")) return "image/png";
        if (n.endsWith(".jpg") || n.endsWith(".jpeg")) return "image/jpeg";
        if (n.endsWith(".json")) return "application/json";
        if (n.endsWith(".srt")) return "text/plain";
        if (n.endsWith(".md") || n.endsWith(".txt") || n.endsWith(".log") || n.endsWith(".csv")) return "text/plain";
        return "application/octet-stream";
    }

    public static String extensionForMime(String mime) {
        if (mime == null) return "bin";
        switch (mime.toLowerCase(Locale.US)) {
            case "audio/wav": case "audio/x-wav": case "audio/wave": return "wav";
            case "audio/mpeg": return "mp3";
            case "audio/mp4": return "m4a";
            case "audio/ogg": return "ogg";
            case "audio/webm": return "webm";
            case "audio/flac": return "flac";
            case "video/mp4": return "mp4";
            case "video/webm": return "webm";
            case "video/3gpp": return "3gp";
            case "image/png": return "png";
            case "image/jpeg": return "jpg";
            case "application/json": return "json";
            case "text/plain": return "txt";
            default: return "bin";
        }
    }

    /** 附件的语义标签：主界面只显示这个，不显示真实文件名 */
    public static String labelFor(String mime) {
        if (mime == null) return "附件";
        if (mime.startsWith("audio/")) return "播报语音";
        if (mime.startsWith("video/")) return "汇报视频";
        if (mime.startsWith("image/")) return "形象图片";
        if (mime.startsWith("text/")) return "文字详情";
        if (mime.contains("json")) return "结构化数据";
        return "附件";
    }

    private static String sanitize(String name) {
        if (name == null || name.isEmpty()) return "result.txt";
        String base = new File(name).getName();
        String cleaned = base.replaceAll("[^A-Za-z0-9._-]", "_");
        return cleaned.isEmpty() ? "result.txt" : cleaned;
    }

    private static void trim(File dir) {
        File[] all = dir.listFiles();
        if (all == null || all.length <= 60) return;
        Arrays.sort(all, (a, b) -> Long.compare(b.lastModified(), a.lastModified()));
        for (int i = 60; i < all.length; i++) {
            //noinspection ResultOfMethodCallIgnored
            all[i].delete();
        }
    }
}
