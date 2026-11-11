package com.boray.voicemcp;

import android.app.Notification;
import android.app.NotificationManager;
import android.app.PendingIntent;
import android.app.Service;
import android.content.Context;
import android.content.Intent;
import android.content.SharedPreferences;
import android.os.Build;
import android.os.IBinder;
import android.util.Log;

import org.json.JSONArray;
import org.json.JSONObject;

import java.util.ArrayList;
import java.util.HashSet;
import java.util.List;
import java.util.Map;
import java.util.Set;
import java.util.concurrent.ConcurrentHashMap;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;

/**
 * 保持监听服务（前台服务，常驻通知「同心圆 · 保持监听」）。
 *
 * 三条数据来源，覆盖「mcpserver 推送过来最新消息」：
 *  1) 语音任务：voice_chat 立即回 job_id（wait=false），本服务用 voice_job 长轮询取结果，
 *     done 后把结果语音直接播报，并唤起数字员工汇报画面。
 *  2) SSE 推送：GET /mcp 的 text/event-stream 长连接，收到服务端通知即播报；
 *     若通知带 job_id，则转交第 1 条链路取完整结果，避免重复播报。
 *  3) CEO 新邮件：定时 mail_list 检查最新 uid，出现新邮件就播报摘要（不改已读状态）。
 *
 * 所有消息统一交给 PushProcessor：写历史 → 播报 → 通知 → 前台弹汇报画面。
 * 待处理 job 与已读邮件 uid 存在 SharedPreferences，服务重启后不丢、不重复播报。
 */
public class ListenService extends Service {

    public static final String ACTION_START = "com.boray.voicemcp.LISTEN_START";
    public static final String ACTION_STOP = "com.boray.voicemcp.LISTEN_STOP";
    public static final String ACTION_ADD_JOB = "com.boray.voicemcp.LISTEN_ADD_JOB";
    public static final String EXTRA_JOB_ID = "job_id";

    private static final int NOTIF_ID = 1001;
    private static final String PREF = "conring_listen";
    private static final String KEY_RUNNING = "running";
    private static final String KEY_JOBS = "pending_jobs";
    private static final String KEY_MAIL_UID = "last_mail_uid";

    private volatile boolean running;
    private boolean loopsStarted;
    private ExecutorService pool;
    private VoicePlayer player;
    private NotificationManager nm;
    private SharedPreferences prefs;

    private McpClient mainClient;
    private String mainClientKey = "";
    private McpClient mailClient;
    private String mailClientKey = "";
    private SseListener sse;
    private String lastSseUrl = "";
    private final Map<String, Integer> failures = new ConcurrentHashMap<>();

    /* ===== 对外静态方法 ===== */

    public static void start(Context c) {
        if (McpConfig.load(c).demo) return;
        Intent i = new Intent(c, ListenService.class).setAction(ACTION_START);
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) c.startForegroundService(i);
        else c.startService(i);
    }

    public static void stop(Context c) {
        Intent i = new Intent(c, ListenService.class).setAction(ACTION_STOP);
        try { c.startService(i); } catch (Exception ignored) { }
        prefs(c).edit().putBoolean(KEY_RUNNING, false).apply();
    }

    /** voice_chat 回执后登记异步任务；服务未启动会顺带拉起 */
    public static void addJob(Context c, String jobId) {
        if (jobId == null || jobId.isEmpty()) return;
        Set<String> jobs = new HashSet<>(jobs(c));
        jobs.add(jobId);
        prefs(c).edit().putStringSet(KEY_JOBS, jobs).apply();
        start(c);
    }

    public static boolean isRunning(Context c) {
        return prefs(c).getBoolean(KEY_RUNNING, false);
    }

    public static int pendingCount(Context c) {
        return jobs(c).size();
    }

    private static SharedPreferences prefs(Context c) {
        return c.getSharedPreferences(PREF, Context.MODE_PRIVATE);
    }

    private static Set<String> jobs(Context c) {
        Set<String> s = prefs(c).getStringSet(KEY_JOBS, new HashSet<String>());
        return s == null ? new HashSet<String>() : new HashSet<>(s);
    }

    /* ===== 生命周期 ===== */

    @Override
    public void onCreate() {
        super.onCreate();
        prefs = prefs(this);
        nm = getSystemService(NotificationManager.class);
        PushProcessor.ensureChannels(this);
        player = new VoicePlayer(getApplicationContext());
        pool = Executors.newFixedThreadPool(4);   // 任务轮询 / SSE / 邮件 / 待发指令重试
        // 开机自启场景也保证 18 人档案是最新的（缓存 6 小时，离线用本地缓存）
        EmployeeStore.syncIfStale(this);
    }

    @Override
    public int onStartCommand(Intent intent, int flags, int startId) {
        String action = intent == null ? ACTION_START : intent.getAction();
        if (ACTION_STOP.equals(action)) {
            shutdown();
            stopSelf();
            return START_NOT_STICKY;
        }
        if (ACTION_ADD_JOB.equals(action) && intent != null) {
            String id = intent.getStringExtra(EXTRA_JOB_ID);
            if (id != null && !id.isEmpty()) {
                Set<String> set = new HashSet<>(jobs(this));
                set.add(id);
                prefs.edit().putStringSet(KEY_JOBS, set).apply();
            }
        }
        prefs.edit().putBoolean(KEY_RUNNING, true).apply();
        running = true;
        // 端点改了就重建 SSE 长连接（job / mail 循环本来就每轮重读配置）
        String currentUrl = McpConfig.load(this).url;
        if (!currentUrl.equals(lastSseUrl)) {
            lastSseUrl = currentUrl;
            if (sse != null) sse.stop();
        }
        startForeground(NOTIF_ID, buildNotification("正在监听语音任务与服务端消息"));
        if (!loopsStarted) {
            loopsStarted = true;
            pool.execute(this::jobLoop);
            pool.execute(this::sseLoop);
            pool.execute(this::mailLoop);
            pool.execute(this::outboxLoop);
        }
        notifyPending();
        return START_STICKY;
    }

    @Override
    public void onDestroy() {
        shutdown();
        super.onDestroy();
    }

    private void shutdown() {
        running = false;
        prefs.edit().putBoolean(KEY_RUNNING, false).apply();
        if (sse != null) sse.stop();
        if (pool != null) pool.shutdownNow();
        if (player != null) player.release();
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.N) stopForeground(STOP_FOREGROUND_REMOVE);
        else stopForeground(true);
    }

    @Override
    public IBinder onBind(Intent intent) {
        return null;
    }

    /* ===== 1. 语音任务：voice_job 长轮询 ===== */

    private void jobLoop() {
        while (running) {
            Set<String> pending = jobs(this);
            if (pending.isEmpty()) {
                sleep(Math.max(2000, McpConfig.load(this).listenIntervalMs() / 2L));
                continue;
            }
            for (String jobId : new ArrayList<>(pending)) {
                if (!running) break;
                McpConfig cfg = McpConfig.load(this);
                if (cfg.demo) { removeJob(jobId); continue; }
                try {
                    JSONObject args = new JSONObject()
                            .put("job_id", jobId)
                            .put("wait_sec", Math.min(25, Math.max(5, cfg.listenIntervalSec)))
                            .put("include_audio", true);
                    JSONObject raw = clientFor(cfg).callTool("voice_job", args);
                    McpResult probe = ResultParser.parseTransient(this, raw);
                    String core = probe.bestText();
                    String taskStatus = probe.status == null ? "" : probe.status.toLowerCase();
                    boolean terminalError = "error".equals(taskStatus) || "failed".equals(taskStatus);
                    // 任务结果有 TTL：离线过久返回「任务不存在或已过期」，或工具级错误时直接收尾；
                    // status=error 留给下面读取 error 字段，给用户明确提示与重试入口。
                    if (core.contains("不存在") || core.contains("过期")
                            || core.contains("执行失败") || (probe.isError && !terminalError)) {
                        removeJob(jobId);
                        failures.remove(jobId);
                        VoiceOutbox.doneByJob(this, jobId);
                        McpResult expired = ResultParser.fromText(this,
                                "任务结果已过期或不存在：" + shortJob(jobId) + "，请重新发送语音指令",
                                "voice", jobId);
                        expired.status = "failed";
                        PushProcessor.save(this, expired);
                        updateNotification("任务结果已过期");
                        notifyPending();
                        continue;
                    }
                    failures.remove(jobId);
                    String st = taskStatus;
                    boolean done = "done".equals(st) || "failed".equals(st) || "error".equals(st)
                            || probe.audioItem() != null || !probe.detailText.isEmpty();
                    if (done) {
                        McpResult r = ResultParser.parse(this, raw);
                        r.source = "voice";
                        r.jobId = jobId;
                        r.messageId = jobId;
                        removeJob(jobId);
                        VoiceOutbox.doneByJob(this, jobId);
                        // v2.1.0：status=error 带 error 字段，提示重试而不是把错误 JSON 当正文播报
                        if ("error".equals(st) || "failed".equals(st) || r.isError) {
                            String msg = r.errorText.isEmpty() ? r.bestText() : r.errorText;
                            McpResult failed = ResultParser.fromText(this,
                                    "任务执行失败：" + msg + "，请重新发送语音指令", "voice", jobId);
                            failed.status = "failed";
                            failed.isError = true;
                            failed.errorText = msg;
                            failed.jobId = jobId;
                            PushProcessor.save(this, failed);
                            updateNotification("任务失败，请重新发送指令");
                        } else {
                            updateNotification("收到任务结果，正在播报");
                            PushProcessor.process(this, r, player, true);
                        }
                        notifyPending();
                    } else {
                        updateNotification("任务处理中 · " + shortJob(jobId));
                    }
                } catch (Exception e) {
                    invalidateMain();
                    int n = failures.getOrDefault(jobId, 0) + 1;
                    if (n >= 8) {
                        removeJob(jobId);
                        failures.remove(jobId);
                        McpResult failed = ResultParser.fromText(this,
                                "获取任务结果失败（已重试 " + n + " 次）：" + shortJob(jobId),
                                "voice", jobId);
                        failed.status = "failed";
                        PushProcessor.save(this, failed);
                        updateNotification("任务获取失败，已放弃");
                    } else {
                        failures.put(jobId, n);
                        updateNotification("网络重连中 · " + shortJob(jobId));
                    }
                    sleep(3000);
                }
            }
            sleep(1200);
        }
    }

    private void removeJob(String id) {
        Set<String> set = new HashSet<>(jobs(this));
        set.remove(id);
        prefs.edit().putStringSet(KEY_JOBS, set).apply();
    }

    private static String shortJob(String id) {
        return id.length() > 14 ? id.substring(0, 14) + "…" : id;
    }

    /* ===== 1b. 未发出去的语音指令：落盘重发 ===== */

    /**
     * 录音在 MainActivity / 实时通话里已经落盘（VoiceOutbox），这里只负责把「还没被服务端
     * 确认接收」的条目重发出去。没有待发条目时不发请求、只低频检查，不做无意义轮询。
     */
    private void outboxLoop() {
        while (running) {
            int pending = 0;
            try {
                pending = VoiceOutbox.pendingCount(this);
                if (pending > 0) {
                    VoiceOutbox.flush(this, McpConfig.load(this));
                    updateNotification(pending > 1
                            ? "有 " + pending + " 条语音指令待送达，正在重发" : "正在重发语音指令");
                }
            } catch (Exception e) {
                Log.w("ListenService", "outbox 重发失败", e);
            }
            sleep(pending > 0 ? 15000 : 60000);
        }
    }

    /* ===== 2. SSE 服务端推送 ===== */

    private void sseLoop() {
        while (running) {
            McpConfig cfg = McpConfig.load(this);
            if (!cfg.listenSse || cfg.demo) {
                sleep(5000);
                continue;
            }
            String session = "";
            try {
                McpClient c = clientFor(cfg);
                c.connect();
                session = c.getSessionId();
            } catch (Exception e) {
                invalidateMain();
                updateNotification("SSE 握手失败，稍后重试");
                sleep(4000);
                continue;
            }
            SseListener listener = new SseListener();
            sse = listener;
            listener.loop(cfg, session, new SseListener.Sink() {
                @Override public void onEvent(JSONObject message) {
                    handlePush(message);
                }
                @Override public void onState(String state) {
                    updateNotification(state);
                }
            });
            sleep(2000);
        }
    }

    private void handlePush(JSONObject m) {
        if (m == null || !running) return;
        JSONObject params = m.optJSONObject("params");
        String jobId = m.optString("job_id", "");
        if (jobId.isEmpty() && params != null) jobId = params.optString("job_id", "");
        if (!jobId.isEmpty()) {
            // 服务端推送「任务完成」，交给 voice_job 取完整结果（含音频）再播报
            Set<String> set = new HashSet<>(jobs(this));
            if (!set.contains(jobId)) {
                set.add(jobId);
                prefs.edit().putStringSet(KEY_JOBS, set).apply();
                notifyPending();
            }
            return;
        }
        String text = m.optString("text", "");
        if (text.isEmpty() && params != null) {
            text = firstText(params, "text", "message", "content", "data");
        }
        if (text.isEmpty()) {
            String method = m.optString("method", "");
            if (method.startsWith("notifications/") && params != null) text = params.toString();
        }
        if (text.isEmpty()) return;
        String mid = "sse_" + Integer.toHexString((m.optString("method", "") + text).hashCode());
        if (HistoryDb.get(this).byMessageId(mid) != null) return;   // 去重
        McpResult r = ResultParser.fromText(this, text, "push", mid);
        updateNotification("收到服务端推送消息");
        PushProcessor.process(this, r, player, true);
    }

    /* ===== 3. CEO 新邮件 ===== */

    private void mailLoop() {
        while (running) {
            McpConfig cfg = McpConfig.load(this);
            long wait = Math.max(30000L, cfg.listenIntervalMs() * 3L);
            if (!cfg.listenMail || cfg.demo) {
                sleep(wait);
                continue;
            }
            try {
                McpConfig mailCfg = cfg.copy();
                mailCfg.url = cfg.mailUrl;
                JSONObject raw = mailClientFor(mailCfg).callTool("mail_list",
                        new JSONObject().put("folder", "INBOX").put("limit", 6));
                String text = firstContentText(raw);
                JSONObject j = new JSONObject(text);
                JSONArray msgs = j.optJSONArray("messages");
                long last = prefs.getLong(KEY_MAIL_UID, -1L);
                long max = last;
                List<JSONObject> fresh = new ArrayList<>();
                if (msgs != null) {
                    for (int i = 0; i < msgs.length(); i++) {
                        JSONObject o = msgs.optJSONObject(i);
                        if (o == null) continue;
                        long uid = parseUid(o.optString("uid", ""));
                        if (uid <= 0) continue;
                        if (uid > max) max = uid;
                        if (last >= 0 && uid > last && !o.optBoolean("seen", false)) fresh.add(o);
                    }
                }
                // 首次只记录基线，不把历史未读邮件全播一遍
                if (last < 0) {
                    prefs.edit().putLong(KEY_MAIL_UID, max).apply();
                } else {
                    for (int i = fresh.size() - 1; i >= 0; i--) {
                        JSONObject o = fresh.get(i);
                        String from = o.optString("from", "");
                        String subject = o.optString("subject", "");
                        String uid = o.optString("uid", "");
                        String speech = "收到新邮件，来自 " + cleanSender(from) + "，主题：" + subject;
                        String mid = "mail_" + uid;
                        if (HistoryDb.get(this).byMessageId(mid) != null) continue;
                        McpResult r = ResultParser.fromText(this, speech, "mail", mid);
                        r.questionText = "新邮件 · " + cleanSender(from);
                        r.detailText = "发件人：" + from + "\n主题：" + subject
                                + "\n时间：" + o.optString("date", "");
                        PushProcessor.process(this, r, player, true);
                    }
                    if (max > last) prefs.edit().putLong(KEY_MAIL_UID, max).apply();
                }
                updateNotification("已检查新邮件 · 待处理任务 " + jobs(this).size());
            } catch (Exception e) {
                invalidateMail();
                updateNotification("邮件检查失败，稍后重试");
            }
            sleep(Math.max(30000L, McpConfig.load(this).listenIntervalMs() * 3L));
        }
    }

    private static String cleanSender(String from) {
        int a = from.indexOf('<');
        return a > 0 ? from.substring(0, a).trim() : from;
    }

    private static long parseUid(String s) {
        try { return Long.parseLong(s.trim()); } catch (Exception e) { return -1; }
    }

    private static String firstContentText(JSONObject toolResult) {
        JSONArray content = toolResult == null ? null : toolResult.optJSONArray("content");
        if (content != null) {
            for (int i = 0; i < content.length(); i++) {
                JSONObject b = content.optJSONObject(i);
                if (b != null && "text".equals(b.optString("type", ""))) {
                    return b.optString("text", "");
                }
            }
        }
        return toolResult == null ? "{}" : toolResult.toString();
    }

    private static String firstText(JSONObject j, String... keys) {
        for (String k : keys) {
            String v = j.optString(k, "");
            if (v != null && !v.trim().isEmpty()) return v.trim();
        }
        return "";
    }

    /* ===== 客户端缓存 / 通知 / 工具 ===== */

    private synchronized McpClient clientFor(McpConfig cfg) {
        String key = cfg.url + "|" + cfg.token + "|" + cfg.protocol;
        if (mainClient == null || !key.equals(mainClientKey)) {
            mainClient = new McpClient(cfg);
            mainClientKey = key;
        }
        return mainClient;
    }

    private synchronized McpClient mailClientFor(McpConfig cfg) {
        String key = cfg.url + "|" + cfg.token + "|" + cfg.protocol;
        if (mailClient == null || !key.equals(mailClientKey)) {
            mailClient = new McpClient(cfg);
            mailClientKey = key;
        }
        return mailClient;
    }

    private synchronized void invalidateMain() {
        if (mainClient != null) mainClient.invalidate();
    }

    private synchronized void invalidateMail() {
        if (mailClient != null) mailClient.invalidate();
        mailClient = null;
    }

    private void notifyPending() {
        int n = jobs(this).size();
        int pendingOut = VoiceOutbox.pendingCount(this);
        if (n == 0 && pendingOut == 0) {
            updateNotification("正在监听语音任务与服务端消息");
        } else if (n == 0) {
            updateNotification("有 " + pendingOut + " 条语音指令待送达");
        } else {
            updateNotification("正在处理 " + n + " 个语音任务"
                    + (pendingOut > 0 ? " · " + pendingOut + " 条待送达" : ""));
        }
    }

    private Notification buildNotification(String text) {
        Intent open = new Intent(this, MainActivity.class)
                .setFlags(Intent.FLAG_ACTIVITY_NEW_TASK | Intent.FLAG_ACTIVITY_CLEAR_TOP);
        int flags = PendingIntent.FLAG_UPDATE_CURRENT;
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.M) flags |= PendingIntent.FLAG_IMMUTABLE;
        PendingIntent pi = PendingIntent.getActivity(this, 0, open, flags);
        Notification.Builder b = Build.VERSION.SDK_INT >= Build.VERSION_CODES.O
                ? new Notification.Builder(this, PushProcessor.CHANNEL_LISTEN)
                : new Notification.Builder(this);
        b.setSmallIcon(R.drawable.ic_conring)
                .setContentTitle("同心圆 · 保持监听")
                .setContentText(text)
                .setOngoing(true)
                .setContentIntent(pi);
        return b.build();
    }

    private void updateNotification(String text) {
        if (nm == null || !running) return;
        try { nm.notify(NOTIF_ID, buildNotification(text)); } catch (Exception ignored) { }
    }

    private void sleep(long ms) {
        long end = System.currentTimeMillis() + ms;
        while (running && System.currentTimeMillis() < end) {
            try { Thread.sleep(300); } catch (InterruptedException e) {
                Thread.currentThread().interrupt();
                return;
            }
        }
    }
}
