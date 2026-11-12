package com.boray.voicemcp;

import android.content.Context;
import android.content.SharedPreferences;

import org.json.JSONArray;
import org.json.JSONObject;

import java.io.File;
import java.io.FileInputStream;
import java.io.FileOutputStream;
import java.io.IOException;
import java.io.InputStream;
import java.io.OutputStream;
import java.util.ArrayList;
import java.util.Collections;
import java.util.Comparator;
import java.util.List;

/**
 * 语音指令持久化待发队列（「老板的每一条指令都不能丢」）。
 *
 * 设计要点：
 *  - 录音一结束先落盘到 filesDir/voice_outbox/（不是 cache，系统清缓存也不会丢），
 *    再谈发网络；发送失败/断网/进程被杀，下次启动继续重发；
 *  - 每条带稳定 client_id，服务端按它幂等去重，重发不会让同一条指令执行两次；
 *  - 发送成功后记录 job_id，任务真正完成才删文件，便于追溯。
 */
public final class VoiceOutbox {

    public static final class Item {
        public String id;
        public String file;
        public long createdAt;
        public int attempts;
        public long nextTryAt;
        public String jobId = "";
        public String lastError = "";
        public String profile = "";
        public String avatar = "";

        public File file(Context c) {
            return new File(c.getFilesDir(), file);
        }

        public JSONObject toJson() {
            JSONObject o = new JSONObject();
            try {
                o.put("id", id);
                o.put("file", file);
                o.put("createdAt", createdAt);
                o.put("attempts", attempts);
                o.put("nextTryAt", nextTryAt);
                o.put("jobId", jobId);
                o.put("lastError", lastError);
                o.put("profile", profile);
                o.put("avatar", avatar);
            } catch (Exception ignored) {
            }
            return o;
        }

        public static Item fromJson(JSONObject o) {
            Item it = new Item();
            it.id = o.optString("id");
            it.file = o.optString("file");
            it.createdAt = o.optLong("createdAt");
            it.attempts = o.optInt("attempts");
            it.nextTryAt = o.optLong("nextTryAt");
            it.jobId = o.optString("jobId");
            it.lastError = o.optString("lastError");
            it.profile = o.optString("profile");
            it.avatar = o.optString("avatar");
            return it;
        }
    }

    private static final String PREF = "voice_outbox_index";
    private static final String KEY = "items";
    private static final String DIR = "voice_outbox";
    /** 超过这个时间还没发出去的录音不再重发（避免几天后突然执行旧指令），但会记一条失败日志 */
    private static final long EXPIRE_MS = 24L * 3600 * 1000;
    private static final Object LOCK = new Object();
    private static volatile boolean flushing;

    private VoiceOutbox() {
    }

    private static SharedPreferences prefs(Context c) {
        return c.getSharedPreferences(PREF, Context.MODE_PRIVATE);
    }

    public static File dir(Context c) {
        File d = new File(c.getFilesDir(), DIR);
        if (!d.exists()) //noinspection ResultOfMethodCallIgnored
            d.mkdirs();
        return d;
    }

    private static List<Item> load(Context c) {
        List<Item> out = new ArrayList<>();
        String raw = prefs(c).getString(KEY, "[]");
        try {
            JSONArray arr = new JSONArray(raw);
            for (int i = 0; i < arr.length(); i++) out.add(Item.fromJson(arr.getJSONObject(i)));
        } catch (Exception ignored) {
        }
        Collections.sort(out, new Comparator<Item>() {
            @Override
            public int compare(Item a, Item b) {
                return Long.compare(a.createdAt, b.createdAt);
            }
        });
        return out;
    }

    private static void save(Context c, List<Item> items) {
        JSONArray arr = new JSONArray();
        for (Item it : items) arr.put(it.toJson());
        prefs(c).edit().putString(KEY, arr.toString()).apply();
    }

    /** 录音结束第一件事：入队落盘。返回 null 表示磁盘写入失败（调用方仍可走原路径直接发）。 */
    public static Item enqueue(Context c, File src, String profile, String avatar) {
        if (src == null || !src.exists()) return null;
        synchronized (LOCK) {
            String id = "v" + System.currentTimeMillis() + "_" + (int) (Math.random() * 1000);
            File dst = new File(dir(c), id + ".wav");
            try {
                copy(src, dst);
            } catch (IOException e) {
                return null;
            }
            Item it = new Item();
            it.id = id;
            it.file = DIR + "/" + dst.getName();
            it.createdAt = System.currentTimeMillis();
            // 给「当场直发」留 90 秒窗口：直发成功会 markSent，失败才轮到后台重发
            it.nextTryAt = it.createdAt + 90_000;
            it.profile = profile == null ? "" : profile;
            it.avatar = avatar == null ? "" : avatar;
            List<Item> items = load(c);
            items.add(it);
            save(c, items);
            prune(c, items);
            return it;
        }
    }

    public static List<Item> all(Context c) {
        synchronized (LOCK) {
            return load(c);
        }
    }

    /** 还没被服务端确认接收的条数（含正在重试的）。 */
    public static int pendingCount(Context c) {
        int n = 0;
        for (Item it : all(c)) if (it.jobId == null || it.jobId.isEmpty()) n++;
        return n;
    }

    public static void markSent(Context c, String id, String jobId) {
        synchronized (LOCK) {
            List<Item> items = load(c);
            for (Item it : items) {
                if (it.id.equals(id)) {
                    it.jobId = jobId == null ? "" : jobId;
                    it.nextTryAt = 0;
                    it.lastError = "";
                }
            }
            save(c, items);
        }
    }

    /** 任务真正完成（或服务端明确失败）：删录音、删记录。 */
    public static void done(Context c, String id) {
        synchronized (LOCK) {
            List<Item> items = load(c);
            List<Item> keep = new ArrayList<>();
            for (Item it : items) {
                if (it.id.equals(id)) {
                    //noinspection ResultOfMethodCallIgnored
                    it.file(c).delete();
                } else {
                    keep.add(it);
                }
            }
            save(c, keep);
        }
    }

    public static void doneByJob(Context c, String jobId) {
        if (jobId == null || jobId.isEmpty()) return;
        for (Item it : all(c)) if (jobId.equals(it.jobId)) done(c, it.id);
    }

    public static void fail(Context c, String id, String error) {
        synchronized (LOCK) {
            List<Item> items = load(c);
            for (Item it : items) {
                if (it.id.equals(id)) {
                    it.attempts++;
                    it.lastError = error == null ? "" : error;
                    long[] backoff = {10, 30, 60, 120, 300, 600};
                    long sec = backoff[Math.min(it.attempts - 1, backoff.length - 1)];
                    it.nextTryAt = System.currentTimeMillis() + sec * 1000;
                }
            }
            save(c, items);
        }
    }

    /** 到点该重发的条目；顺带清掉超过 24 小时的老录音（只留一条失败记录在历史里）。 */
    public static List<Item> due(Context c) {
        long now = System.currentTimeMillis();
        List<Item> out = new ArrayList<>();
        synchronized (LOCK) {
            List<Item> items = load(c);
            List<Item> keep = new ArrayList<>();
            for (Item it : items) {
                if (it.jobId != null && !it.jobId.isEmpty()) {   // 已收到回执，等任务完成即可
                    keep.add(it);
                    continue;
                }
                if (now - it.createdAt > EXPIRE_MS) {
                    //noinspection ResultOfMethodCallIgnored
                    it.file(c).delete();
                    continue;
                }
                keep.add(it);
                if (it.nextTryAt <= now) out.add(it);
            }
            if (keep.size() != items.size()) save(c, keep);
        }
        return out;
    }

    private static void prune(Context c, List<Item> items) {
        long now = System.currentTimeMillis();
        List<Item> keep = new ArrayList<>();
        for (Item it : items) {
            if (it.jobId != null && it.jobId.isEmpty() && now - it.createdAt > EXPIRE_MS) {
                //noinspection ResultOfMethodCallIgnored
                it.file(c).delete();
            } else {
                keep.add(it);
            }
        }
        if (keep.size() != items.size()) save(c, keep);
    }

    private static void copy(File src, File dst) throws IOException {
        InputStream in = new FileInputStream(src);
        try {
            OutputStream out = new FileOutputStream(dst);
            try {
                byte[] buf = new byte[16384];
                int n;
                while ((n = in.read(buf)) > 0) out.write(buf, 0, n);
                out.flush();
            } finally {
                out.close();
            }
        } finally {
            in.close();
        }
    }

    /**
     * 把到点的录音依次重发（voice_chat，async）。放后台线程调用。
     * 成功：记 job_id 并交给 ListenService 长轮询取结果；失败：指数退避，下次再来。
     */
    public static void flush(Context c, McpConfig cfg) {
        if (flushing) return;
        flushing = true;
        McpClient client = null;
        try {
            List<Item> due = due(c);
            if (due.isEmpty()) return;
            for (Item it : due) {
                File f = it.file(c);
                if (!f.exists()) {
                    done(c, it.id);
                    continue;
                }
                try {
                    if (client == null) client = new McpClient(cfg);
                    String b64;
                    try {
                        b64 = android.util.Base64.encodeToString(readAll(f), android.util.Base64.NO_WRAP);
                    } catch (IOException e) {
                        fail(c, it.id, "读取录音失败：" + e.getMessage());
                        continue;
                    }
                    JSONObject args = new JSONObject();
                    args.put(cfg.audioKey, b64);
                    args.put("client_id", it.id);
                    args.put("language", cfg.language);
                    args.put("model", cfg.model);
                    if (!cfg.voice.isEmpty()) args.put("voice", cfg.voice);
                    String profile = cfg.profile.isEmpty() ? it.profile : cfg.profile;
                    if (profile != null && !profile.isEmpty()) args.put("profile", profile);
                    String avatar = cfg.avatarId.isEmpty() ? it.avatar : cfg.avatarId;
                    if (avatar != null && !avatar.isEmpty()) args.put("avatar_id", avatar);
                    args.put("mode", cfg.validMode());
                    args.put("bandwidth_kbps", cfg.effectiveBandwidthKbps(c));
                    args.put("delegate", cfg.delegate);
                    args.put("timeout_sec", Math.min(cfg.timeoutSec, 300));
                    args.put("wait", false);
                    args.put("include_audio", true);

                    JSONObject result = client.callTool(cfg.tool, args);
                    McpResult parsed = ResultParser.parse(c, result);
                    if (parsed.status != null && parsed.status.startsWith("error")) {
                        fail(c, it.id, parsed.bestText());
                        continue;
                    }
                    if (parsed.jobId.isEmpty()) {
                        // 服务端已处理但没建任务（澄清/仅回执）：直接出队。
                        // 以前 markSent(id, "") 会让 due() 每 15s 再发一次同一段录音，重复触发澄清。
                        done(c, it.id);
                        continue;
                    }
                    markSent(c, it.id, parsed.jobId);
                    PushProcessor.save(c, parsed);
                    ListenService.addJob(c, parsed.jobId);
                } catch (Exception e) {
                    if (client != null) {
                        client.invalidate();
                        client = null;
                    }
                    fail(c, it.id, e.getMessage());
                }
            }
        } finally {
            flushing = false;
        }
    }

    public static boolean isFlushing() {
        return flushing;
    }

    private static byte[] readAll(File f) throws IOException {
        FileInputStream in = new FileInputStream(f);
        try {
            byte[] buf = new byte[(int) f.length()];
            int off = 0;
            while (off < buf.length) {
                int n = in.read(buf, off, buf.length - off);
                if (n <= 0) break;
                off += n;
            }
            return buf;
        } finally {
            in.close();
        }
    }
}
