package com.boray.voicemcp;

import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.PendingIntent;
import android.app.Service;
import android.content.Context;
import android.content.Intent;
import android.media.AudioAttributes;
import android.media.AudioManager;
import android.media.MediaPlayer;
import android.os.Build;
import android.os.Handler;
import android.os.IBinder;
import android.os.Looper;
import android.util.Base64;
import android.util.Log;

import org.json.JSONArray;
import org.json.JSONObject;

import java.io.ByteArrayOutputStream;
import java.io.File;
import java.io.FileOutputStream;
import java.io.InputStream;
import java.net.HttpURLConnection;
import java.net.URL;
import java.util.ArrayDeque;
import java.util.ArrayList;
import java.util.List;
import java.util.Locale;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.LinkedBlockingQueue;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicBoolean;

/**
 * 实时语音通话（后台常驻，不展示视频）。
 *
 * 与视频通话共用一条服务端实时流：`live_call start` 拿到同源 HLS（音视频同轨，
 * 这里只取音轨播，不建 Surface）。因为是**同一个会话**，所以：
 *   - 一直连着不断开，直到用户挂断；
 *   - 员工说话（收到 / 正式回复）由服务端混进这路流，客户端解码即播，延迟 2–4s，
 *     不用等 voice_chat 的整轮推理再本地播放；
 *   - 用户说话由 LiveMic 常开 + VAD 自动断句，每句 `voice_chat{live_session}` 上行。
 *
 * 放在 Service 里是为了「切到别的页面/后台也不断」：Activity 只是控制面板。
 */
public class VoiceCallService extends Service {

    private static final String TAG = "VoiceCall";
    public static final String ACTION_START = "com.boray.voicemcp.VOICE_CALL_START";
    public static final String ACTION_STOP = "com.boray.voicemcp.VOICE_CALL_STOP";
    private static final String CHANNEL = "voice_call";
    private static final int NOTIF_ID = 4301;
    private static final String EXTRA_EMPLOYEE = "employee_id";

    // ---------------- 进程内状态（Activity / 首页图标读它）
    private static volatile boolean active = false;
    private static volatile boolean connected = false;
    private static volatile String statusText = "";
    private static volatile String subtitle = "";
    private static volatile String employeeName = "";
    private static volatile long connectedAt = 0;
    private static volatile float level = 0f;
    private static volatile boolean speaking = false;
    /** 最近一条结构化回复（文字详情 + 附件），界面轮询它渲染 */
    private static volatile CallReply lastReply;
    /** 当前走的实时链路：ws（dispatch 双向通道）/ hls（旧的 live_call 混流） */
    private static volatile String transport = "";

    public static CallReply lastReply() {
        return lastReply;
    }

    public static String transport() {
        return transport;
    }

    public static boolean isActive() {
        return active;
    }

    public static boolean isConnected() {
        return connected;
    }

    public static String status() {
        return statusText;
    }

    public static String caption() {
        return subtitle;
    }

    public static String who() {
        return employeeName;
    }

    public static long connectedAt() {
        return connectedAt;
    }

    /**
     * 一条转写记录。
     *
     * pending=true 表示「还在等 ASR」或「还在等回复」，界面会显示成进行中的样式；
     * 一旦真有文字就原地替换，所以同一句话不会先来个占位、再来一条重复的。
     */
    public static final class Line {
        /** 我说的 */
        public static final int KIND_USER = 0;
        /** 汇报人念出来的话（语音稿，字幕用） */
        public static final int KIND_VOICE = 1;
        /** 给眼睛看的文字详情（不朗读） */
        public static final int KIND_DETAIL = 2;

        public final boolean fromUser;
        public final int kind;
        public volatile String text;
        public volatile boolean pending;
        /** SQLite 里的行 id；<=0 表示还没落库 */
        public volatile long dbId = -1;
        /** 这条消息附带的共享链接 [{"name","url"}]，只有「详情」类会用到 */
        public volatile String linksJson = "[]";
        public final long createdAt = System.currentTimeMillis();

        Line(boolean fromUser, String text, boolean pending) {
            this(fromUser, text, pending, fromUser ? KIND_USER : KIND_VOICE);
        }

        Line(boolean fromUser, String text, boolean pending, int kind) {
            this.fromUser = fromUser;
            this.text = text;
            this.pending = pending;
            this.kind = kind;
        }
    }

    /** 转写有任何变化（新增 / 原地更新）就回调，界面不用等定时器 */
    public interface LineListener {
        void onLinesChanged();
    }

    private static final Handler UI = new Handler(Looper.getMainLooper());
    private static volatile LineListener lineListener;

    /** 一句最终定稿时回调，服务把它写进会话库（按人 + 天长期保存） */
    public interface LineSink {
        void onFinal(Line line);
    }

    private static volatile LineSink lineSink;

    public static void setLineSink(LineSink s) {
        lineSink = s;
    }

    public static void setLineListener(LineListener l) {
        lineListener = l;
    }

    private static void notifyLines() {
        LineListener l = lineListener;
        if (l != null) UI.post(l::onLinesChanged);
    }

    /** 实时转写（我 / 汇报人），最多保留 60 行；对方名字留空，由界面按当前汇报人渲染 */
    private static final ArrayDeque<Line> LINES = new ArrayDeque<>();

    public static List<Line> lines() {
        synchronized (LINES) {
            return new ArrayList<>(LINES);
        }
    }

    private static Line addLine(boolean fromUser, String text, boolean pending) {
        return addLine(fromUser, text, pending, fromUser ? Line.KIND_USER : Line.KIND_VOICE);
    }

    private static Line addLine(boolean fromUser, String text, boolean pending, int kind) {
        Line line = new Line(fromUser, text == null ? "" : text.trim(), pending, kind);
        synchronized (LINES) {
            // 对方留空：由界面填当前汇报人名字，切换汇报人后旧记录也跟着改称呼
            LINES.addLast(line);
            while (LINES.size() > 60) LINES.pollFirst();
        }
        notifyLines();
        if (!pending) notifySink(line);
        return line;
    }

    private static void notifySink(Line line) {
        LineSink s = lineSink;
        if (s != null) s.onFinal(line);
    }

    /** 文字详情单独一行：不朗读、可能较长，界面上给「详情」样式 */
    private static Line addDetailLine(String text) {
        return addDetailLine(text, "[]");
    }

    private static Line addDetailLine(String text, String linksJson) {
        if (text == null || text.trim().isEmpty()) return null;
        // 附件必须在入队/落库之前挂上，否则第一笔写入会是空的
        Line line = new Line(false, text.trim(), false, Line.KIND_DETAIL);
        line.linksJson = linksJson == null ? "[]" : linksJson;
        synchronized (LINES) {
            LINES.addLast(line);
            while (LINES.size() > 60) LINES.pollFirst();
        }
        notifyLines();
        notifySink(line);
        return line;
    }

    private static Line addPendingLine(boolean fromUser) {
        return addLine(fromUser, "", true);
    }

    /** 原地更新某一行（占位 → 真文本），不新增重复行 */
    private static void setLine(Line line, String text, boolean pending) {
        if (line == null) return;
        String t = text == null ? "" : text.trim();
        // 详情是「给眼睛看的」，允许更长；语音稿按一句的长度截断即可
        int max = line.kind == Line.KIND_DETAIL ? 1500 : 300;
        if (t.length() > max) t = t.substring(0, max) + "…";
        line.text = t;
        line.pending = pending;
        notifyLines();
        if (!pending) notifySink(line);
    }

    /** 把某一行整条删掉（占位行被真正的第一句替换时用） */
    private static void removeLine(Line line) {
        if (line == null) return;
        synchronized (LINES) {
            LINES.remove(line);
        }
        notifyLines();
    }

    private static void updateLine(Line line, String text) {
        setLine(line, text, false);
    }

    public static void clearLines() {
        synchronized (LINES) {
            LINES.clear();
        }
        lastReply = null;
        notifyLines();
    }

    /** 员工侧（下行）电平 0~1；服务端在完善，字段没给之前一直是 0 */
    private static volatile float remoteLevel = 0f;
    private static volatile long remoteLevelAt = 0L;

    private static void setRemoteLevel(float v) {
        remoteLevel = Math.max(0f, Math.min(1f, v));
        remoteLevelAt = System.currentTimeMillis();
    }

    public static float remoteLevel() {
        // 3 秒没更新就归零，避免动画卡在最后一帧
        return System.currentTimeMillis() - remoteLevelAt > 3000 ? 0f : remoteLevel;
    }

    /**
     * 从服务端返回里读员工侧电平。
     * 现在接口还没定字段名，这里兼容 level / speech_level / audio_level / energy / volume，
     * 支持 0~1 与 0~100 两种量纲；读不到就什么都不做。
     */
    private static void readRemoteLevel(JSONObject o) {
        if (o == null) return;
        JSONObject[] scopes = {o, o.optJSONObject("live"), o.optJSONObject("result"),
                o.optJSONObject("ack"), o.optJSONObject("speech")};
        for (JSONObject sc : scopes) {
            if (sc == null) continue;
            for (String k : new String[]{"level", "speech_level", "audio_level", "energy", "volume"}) {
                Object raw = sc.opt(k);
                if (!(raw instanceof Number)) continue;   // 非数字（如 "high"）直接忽略
                double v = ((Number) raw).doubleValue();
                if (v < 0) continue;
                if (v > 1) v = Math.min(1, v / 100.0);
                setRemoteLevel((float) v);
                return;
            }
        }
    }

    public static float micLevel() {
        return level;
    }

    public static boolean isSpeaking() {
        return speaking;
    }

    /** 录���权限检查（Activity 用；服务里也会在起流前用到） */
    public static boolean hasMicPermission(Context c) {
        return c.checkSelfPermission(android.Manifest.permission.RECORD_AUDIO)
                == android.content.pm.PackageManager.PERMISSION_GRANTED;
    }

    public static void start(Context c) {
        Intent i = new Intent(c, VoiceCallService.class).setAction(ACTION_START);
        // 必须带上当前汇报人：不带的话服务端只会用小秘书，用户切换了汇报人却仍是小秘书接
        Employee cur = EmployeeStore.current(c, EmployeeStore.load(c));
        if (cur != null && cur.id != null && !cur.id.isEmpty()) {
            i.putExtra(EXTRA_EMPLOYEE, cur.id);
        }
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) c.startForegroundService(i);
        else c.startService(i);
    }

    public static void stop(Context c) {
        Intent i = new Intent(c, VoiceCallService.class).setAction(ACTION_STOP);
        try {
            c.startService(i);
        } catch (Exception ignored) {
        }
    }

    // ---------------- 内部
    private final Handler ui = new Handler(Looper.getMainLooper());
    private ExecutorService io;
    private MediaPlayer player;
    private LiveMic mic;
    private AudioManager audioManager;
    private int prevAudioMode = -1;
    /**
     * 本地抢先打断的持续门限：VAD 连续判到人声超过这个时长才停播。
     * 原来一帧（20ms）就停，扬声器回声的瞬时尖峰会被当成插话，
     * 变成「小秘书自己把自己掐掉」；与服务端 BARGE_MIN_MS=240 对齐并留点余量。
     */
    private static final long LOCAL_BARGE_MS = 320;
    /** 本轮「疑似插话」的起始时刻，0 表示当前没在累计 */
    private long bargeStartAt = 0;

    private Employee emp;
    private volatile String sessionId = "";
    /** dispatch 任务 id（WS 链路） */
    private volatile String taskId = "";
    private String streamUrl = "";
    private final AtomicBoolean stopSent = new AtomicBoolean(false);
    private Thread sender;
    /** 最近一句上行音频：WS 发送失败（通道挂了）时用它落盘重发，避免指令丢失 */
    private volatile byte[] lastUtteranceWav;
    // 不设上限：老板连着说几句也不能因为队列满就丢（VAAD 切句后每句都要送到）
    private final LinkedBlockingQueue<Utterance> utterances = new LinkedBlockingQueue<>();

    /** 一句话 + 它对应的「我」那一行占位（抢占位就能立刻看到自己说了话） */
    private static final class Utterance {
        final byte[] wav;
        final Line mine;

        Utterance(byte[] wav, Line mine) {
            this.wav = wav;
            this.mine = mine;
        }
    }
    /** 看门狗：滑动窗口播完/卡住就用新时间戳重开同一路流 */
    private int lastPos = -1;
    private long lastPosAt = 0;
    private LiveLevelMeter levelMeter;
    /** 字幕按「声音播到哪」调度；电平计重建时一起重建 */
    private SpokenTextSync textSync;
    private String levelUrl = "";
    private int levelFps = 10;
    private float segmentSec = 1f;
    /** 0.9.6：dispatch WS 双向实时通道（主链路） */
    private RealtimeChannel channel;
    private PcmPlayer wsPlayer;
    private DispatchApi.Submit dispatch;
    /** 本句话的「我」那一行 / 本轮的回复占位行 */
    private volatile Line pendingMine;
    private volatile Line pendingAnswer;
    /** 本次通话在 SessionStore 里的会话 id（用于把每句话长期保存） */
    private volatile String logSessionId = "";
    /** 本次通话里 dispatch 已尝试次数（避免服务端异常时每 2.5s 建一个 P0 任务） */
    private int dispatchTries = 0;
    /** 最近一次 dispatch 失败原因：界面要能说清「指令没进后台」是网络还是服务端 */
    private volatile String dispatchError = "";
    /** 挂断后仍要把没进流的回复播出来，所以任务轮询用独立线程池，不随挂断关闭 */
    private final ExecutorService postIo = Executors.newSingleThreadExecutor();

    private final Runnable clock = new Runnable() {
        @Override
        public void run() {
            if (!active) return;
            if (connected && connectedAt > 0) {
                long sec = (System.currentTimeMillis() - connectedAt) / 1000;
                statusText = "通话中 " + String.format(Locale.CHINA, "%02d:%02d", sec / 60, sec % 60);
            }
            ui.postDelayed(this, 1000);
        }
    };

    private final Runnable chase = new Runnable() {
        @Override
        public void run() {
            if (!active || !connected || player == null) return;
            int pos;
            try {
                pos = player.getCurrentPosition();
            } catch (Exception e) {
                pos = -1;
            }
            if (pos == lastPos) {
                // 位置不再前进（滑动窗口播完 / 中间层卡住）→ 换时间戳重开
                if (lastPosAt > 0 && System.currentTimeMillis() - lastPosAt > 6000) {
                    lastPosAt = 0;
                    reopenPlayer("位置停滞");
                }
            } else {
                lastPos = pos;
                lastPosAt = System.currentTimeMillis();
            }
            ui.postDelayed(this, 2000);
        }
    };

    @Override
    public void onCreate() {
        super.onCreate();
        io = Executors.newSingleThreadExecutor();
        setLineSink(this::persistLine);
        startForeground(NOTIF_ID, notification("语音通话准备中…"));
    }

    @Override
    public int onStartCommand(Intent intent, int flags, int startId) {
        String action = intent == null ? ACTION_START : intent.getAction();
        if (ACTION_STOP.equals(action)) {
            endCall();
            // 可能还有没进流的回复在跑，等它播完再停（最长 5 分钟兜底）
            maybeStopSelf();
            return START_NOT_STICKY;
        }
        String id = intent == null ? null : intent.getStringExtra(EXTRA_EMPLOYEE);
        if (!active) {
            emp = EmployeeStore.byId(EmployeeStore.load(this),
                    id == null || id.isEmpty() ? Briefing.SECRETARY_ID : id);
            employeeName = emp == null ? "小秘书" : emp.name;
            active = true;
            connected = false;
            connectedAt = 0;
            subtitle = "";
            statusText = "正在接通…";
            stopSent.set(false);
            dispatchTries = 0;
            logSessionId = "";
            ui.post(clock);
            enterAudioMode();
            connect(true);
        }
        return START_NOT_STICKY;
    }

    @Override
    public IBinder onBind(Intent intent) {
        return null;
    }

    // ------------------------------------------------------------------ 连接

    private void connect(boolean greet) {
        if (!active) return;
        connected = false;
        statusText = "正在接通 " + employeeName + "…";
        updateNotification(statusText);
        releaseHlsPlayer();
        io.execute(() -> {
            // 主链路：dispatch 建 P0 会话 + 双向 WS（语音往返最快，且有结构化文字/附件）
            if (startDispatchChannel()) return;
            // 兜底：旧的 live_call HLS 混流（服务端未开 ws 通道时仍可用）
            String why = dispatchError;
            if (why != null && !why.isEmpty()) {
                Log.w(TAG, "dispatch unavailable: " + why);
                ui.post(() -> subtitle = "调度中心不可达（" + why + "），指令进不去后台");
            }
            transport = "hls";
            connectHls(greet);
        });
    }

    /** 已经建立的 HLS 播放器要彻底放掉，否则会和新通道抢音频焦点 */
    private void releaseHlsPlayer() {
        if (player != null) {
            try { player.stop(); } catch (Exception ignored) { }
            try { player.release(); } catch (Exception ignored) { }
            player = null;
        }
    }

    /**
     * 建立 dispatch 实时通道。
     *
     * @return true=已连上（后续语音全走 WS）；false=失败，调用方回退 HLS
     */
    private boolean startDispatchChannel() {
        if (!active) return false;
        if (dispatchTries >= 3) return false;      // 连续失败就别再建新任务，直接走 HLS
        dispatchTries++;
        final String profile = emp == null ? Briefing.SECRETARY_ID : emp.id;
        // 会话内容完全由 WS 上行驱动，submit 的 text 只进台账，服务端只要求非空
        DispatchApi.Submit s = DispatchApi.submit(this, profile, "", "语音通话接通");
        if (!s.wsReady()) {
            Log.w(TAG, "dispatch ws unavailable: " + s.message);
            dispatchError = s.message;
            if (!s.taskId.isEmpty()) DispatchApi.cancel(s.taskId);
            return false;
        }
        List<String> urls = new ArrayList<>();
        // 公网优先：先用同源规则推导的 wss（服务端 realtime_url 可能回成内网地址），
        // 再试服务端给的原样地址，最后内网直连
        addUrl(urls, DispatchApi.publicWsUrl(this, s));
        addUrl(urls, DispatchApi.wsUrl(this, s, false));
        addUrl(urls, DispatchApi.wsUrl(this, s, true));
        if (urls.isEmpty()) return false;

        RealtimeChannel ch = new RealtimeChannel(s, urls, wsListener);
        if (!ch.openBlocking()) {
            Log.w(TAG, "ws connect failed, fallback to hls");
            dispatchError = "实时通道连不上（公网/内网均失败）";
            ch.close();
            DispatchApi.cancel(s.taskId);
            return false;
        }
        if (!active) {                    // 用户在连接过程中已挂断
            ch.close();
            DispatchApi.cancel(s.taskId);
            return true;
        }
        channel = ch;
        dispatch = s;
        taskId = s.taskId;
        transport = "ws";
        if (wsPlayer == null) {
            wsPlayer = new PcmPlayer();
            // 员工侧电平：直接从下行 PCM 算，比 level.json 快照更贴耳朵
            wsPlayer.setLevelListener(v -> setRemoteLevel(v));
        }
        ui.post(() -> onChannelConnected(false));
        return true;
    }

    private static void addUrl(List<String> urls, String u) {
        if (u == null || u.isEmpty()) return;
        if (!urls.contains(u)) urls.add(u);
    }

    /** 旧的 HLS 链路（与 0.9.5 行为一致，作为兜底） */
    private void connectHls(boolean greet) {
        try {
            McpConfig cfg = McpConfig.load(this).copy();
            String base = AssetUrls.serverBase(this);
            McpClient client = new McpClient(cfg);
            JSONObject args = new JSONObject();
            args.put("action", StreamContract.ACTION_START);
            // 纯语音：服务端只推 AAC 一路流，不需要照片/视频素材，分片 1.5KB 级
            args.put("mode", "audio");
            args.put("avatar_id", emp == null ? Briefing.SECRETARY_ID : emp.id);
            args.put("first_segment_sec", 8);
            StreamContract.CallSession s = StreamContract.parseCall(cfg.url, base,
                    StreamContract.textOf(client.callTool(StreamContract.TOOL_LIVE_CALL, args)));
            if (!s.ready()) {
                String reason = s.message.isEmpty() ? "未返回同源流地址" : s.message;
                ui.post(() -> onFailed(reason, greet));
                return;
            }
            sessionId = s.sessionId;
            streamUrl = s.streamUrl;
            // 员工侧电平：优先服务端给的 level_url_full，没有就按 index.m3u8 → level.json 推导
            levelUrl = !s.levelUrl.isEmpty() ? s.levelUrl
                    : (s.streamUrl.contains("index.m3u8")
                    ? s.streamUrl.replace("index.m3u8", "level.json") : "");
            levelFps = s.levelFps > 0 ? s.levelFps : 10;
            segmentSec = s.segmentMs > 0 ? s.segmentMs / 1000f : 1f;
            ui.post(() -> startPlayer(greet));
        } catch (Exception e) {
            String msg = e.getMessage() == null ? "接通失败" : e.getMessage();
            ui.post(() -> onFailed(msg, greet));
        }
    }

    private void startPlayer(boolean greet) {
        if (!active || streamUrl.isEmpty()) return;
        try {
            player = new MediaPlayer();
            player.setAudioAttributes(new AudioAttributes.Builder()
                    .setUsage(AudioAttributes.USAGE_VOICE_COMMUNICATION)
                    .setContentType(AudioAttributes.CONTENT_TYPE_SPEECH)
                    .build());
            // 服务端没设 Cache-Control，且 m3u8 是 -hls_list_size 5 的滑动窗口，
            // 每次打开都带时间戳，避免中间层把旧列表喂回来
            player.setDataSource(StreamContract.cacheBust(streamUrl));
            player.setOnPreparedListener(mp -> {
                connected = true;
                if (connectedAt == 0) connectedAt = System.currentTimeMillis();
                statusText = "通话中 00:00";
                updateNotification("正在与 " + employeeName + " 通话");
                ensureLogSession("hls");
                mp.start();
                startLevelMeter(mp);
                // 真正接通后才开麦：起流等待期外界在响拨号音，早开麦会把它当成人声录进去
                setMicRunning(true);
                lastPos = -1;
                lastPosAt = System.currentTimeMillis();
                ui.removeCallbacks(chase);
                ui.postDelayed(chase, 2000);
                if (greet) greet();
            });
            player.setOnErrorListener((mp, what, extra) -> {
                Log.w(TAG, "player error " + what + "/" + extra);
                ui.post(() -> reopenPlayer("播放错误 " + what + "/" + extra));
                return true;
            });
            // 滑动窗口列表播到末尾就会 onCompletion：重开同一个 hls_url_full（换时间戳），
            // 不能重新起 session —— 那会把正在说话的那一路丢掉
            player.setOnCompletionListener(mp -> ui.post(() -> reopenPlayer("列表播完")));
            player.prepareAsync();
        } catch (Exception e) {
            onFailed(e.getMessage() == null ? "播放器启动失败" : e.getMessage(), greet);
        }
    }

    // ------------------------------------------------------------------ WS 通道事件

    /** WS 连上（含断线恢复）：这时才算「已接通」，才开始开麦 */
    private void onChannelConnected(boolean reconnected) {
        if (!active) return;
        boolean first = !connected;
        connected = true;
        if (connectedAt == 0) connectedAt = System.currentTimeMillis();
        statusText = "通话中 00:00";
        updateNotification("正在与 " + employeeName + " 通话");
        lastPos = -1;
        if (first) {
            ensureLogSession("ws");
            ui.removeCallbacks(chase);
            subtitle = "直接说话，我在听。";
            setMicRunning(true);
        } else {
            subtitle = "已恢复连接";
        }
    }

    private final RealtimeChannel.Listener wsListener = new RealtimeChannel.Listener() {
        @Override
        public void onStatus(String text) {
            // 上行失败时通道已不可用：这句音频转存待发队列，由 voice_chat 补发
            if (text != null && (text.contains("没发出去") || text.contains("上行发送失败"))) {
                keepForRetry(lastUtteranceWav, null, "上行中断");
            }
            ui.post(() -> {
                subtitle = text;
                statusText = text;
            });
        }

        @Override
        public void onReady(boolean reconnected) {
            ui.post(() -> onChannelConnected(reconnected));
        }

        @Override
        public void onTranscript(String text) {
            if (wsPlayer != null) wsPlayer.setMuted(false);
            ui.post(() -> {
                Line mine = pendingMine;
                if (mine != null) {
                    updateLine(mine, text);       // 占位换成本人原话，不新增行
                    pendingMine = null;
                } else {
                    addLine(true, text, false);
                }
                subtitle = "我听到：「" + text + "」";
            });
        }

        @Override
        public void onAudio(byte[] data) {
            if (wsPlayer != null) wsPlayer.feed(data);
            // ack 短音频先到：先挂一行「处理中」，正式回复到了原地替换
            if (pendingAnswer == null) {
                ui.post(() -> {
                    if (pendingAnswer == null) pendingAnswer = addPendingLine(false);
                });
            }
            subtitle = "正在听 / 思考…";
        }

        @Override
        public void onReply(CallReply reply) {
            if (reply == null) return;
            if (wsPlayer != null) wsPlayer.setMuted(false);
            ui.post(() -> {
                lastReply = reply;
                String spoken = reply.spoken();
                if (!spoken.isEmpty()) {
                    // 多轮并行时新一轮不能被上一轮的行「覆盖」，已有文字就另起一行
                    if (pendingAnswer != null
                            && (pendingAnswer.text == null || pendingAnswer.text.isEmpty())) {
                        setLine(pendingAnswer, spoken, false);
                    } else {
                        pendingAnswer = addLine(false, spoken, false);
                    }
                    subtitle = spoken;
                }
                // 文字详情单独一行：它不朗读，纯给眼睛看
                if (reply.hasExtraDetail()) addDetailLine(reply.detail, linksJson(reply));
            });
        }

        @Override
        public void onReplyEnd() {
            ui.post(() -> {
                if (pendingAnswer != null) {
                    if (pendingAnswer.text == null || pendingAnswer.text.isEmpty()) {
                        removeLine(pendingAnswer);
                    } else {
                        setLine(pendingAnswer, pendingAnswer.text, false);
                    }
                }
                pendingAnswer = null;
            });
        }

        @Override
        public void onBargeIn() {
            if (wsPlayer != null) wsPlayer.stopAndFlush();
            ui.post(() -> {
                subtitle = "（已打断，我在听）";
                pendingAnswer = null;
            });
        }

        @Override
        public void onServerText(String text) {
            ui.post(() -> {
                addDetailLine(text);
                subtitle = text.length() > 60 ? text.substring(0, 60) + "…" : text;
            });
        }

        @Override
        public void onServerError(String message) {
            ui.post(() -> subtitle = message);
        }

        @Override
        public void onClosed(String reason) {
            ui.post(() -> {
                if (!active) return;
                connected = false;
                statusText = "已断开";
                subtitle = reason;
                endCall();
                stopSelf();
            });
        }
    };

    private void onFailed(String reason, boolean greet) {
        connected = false;
        statusText = "接通失败，正在重试…";
        updateNotification(statusText);
        if (reason.contains("上限")) {
            subtitle = "服务端并发通话已满（最多 8 路），稍后再试。";
            statusText = "无法接通";
            endCall();
            stopSelf();
            return;
        }
        subtitle = "自动重试中：" + reason;
        ui.postDelayed(() -> {
            if (active) connect(greet);
        }, 2500);
    }

    private void greet() {
        final String hello = "您好，我是" + employeeName + "。您说，我在听。";
        io.execute(() -> {
            say(hello);
            // 开场白也进转写：同样等声音真的响起来再显示
            ui.post(() -> showSpokenText(addPendingLine(false), hello, null));
        });
    }

    // ------------------------------------------------------------------ 断流重开

    /** 员工侧电平：轮询 level.json，按媒体时间取格，和听到的声音对齐 */
    private void startLevelMeter(MediaPlayer mp) {
        stopLevelMeter();
        if (levelUrl.isEmpty() || mp == null) return;
        LiveLevelMeter meter = new LiveLevelMeter(levelUrl, streamUrl, mp, levelFps, segmentSec);
        if (!meter.usable()) {
            Log.w(TAG, "level meter skipped: url 非同源 " + levelUrl);
            return;
        }
        meter.setListener(v -> setRemoteLevel(v));
        meter.start();
        levelMeter = meter;
        textSync = new SpokenTextSync(meter, (placeholder, text) -> {
            if (placeholder != null) removeLine(placeholder);   // 占位换成第一句
            if (text != null && !text.trim().isEmpty()) addLine(false, text, false);
        });
        ui.removeCallbacks(syncTick);
        ui.post(syncTick);
    }

    private void stopLevelMeter() {
        ui.removeCallbacks(syncTick);
        if (textSync != null) {
            textSync.tick();       // 收尾前把还压着的句子放出来
            textSync = null;
        }
        if (levelMeter != null) {
            levelMeter.stop();
            levelMeter = null;
        }
        setRemoteLevel(0f);
    }

    /** 每 100ms 让调度器看一眼「播放头越过了哪句」 */
    private final Runnable syncTick = new Runnable() {
        @Override
        public void run() {
            SpokenTextSync s = textSync;
            if (s != null) s.tick();
            if (active || s != null) ui.postDelayed(this, 100);
        }
    };

    /**
     * 重开同一路直播（不重建会话）。
     * 服务端是 5 片滑动窗口，MediaPlayer 对滑窗续播不可靠，播完/卡住都走这里。
     */
    private void reopenPlayer(String reason) {
        if (!active || streamUrl.isEmpty()) return;
        stopLevelMeter();
        connected = false;
        statusText = "重连中…";
        updateNotification("正在重连…");
        if (player != null) {
            try { player.stop(); } catch (Exception ignored) { }
            try { player.release(); } catch (Exception ignored) { }
            player = null;
        }
        Log.i(TAG, "reopen stream: " + reason);
        ui.postDelayed(() -> {
            if (active && !streamUrl.isEmpty()) startPlayer(false);
        }, 600);
    }

    // ------------------------------------------------------------------ 会话落库

    private void ensureLogSession(String transportName) {
        if (!logSessionId.isEmpty()) return;
        String empId = emp == null ? Briefing.SECRETARY_ID : emp.id;
        logSessionId = SessionStore.get(this).begin(empId, employeeName, transportName,
                System.currentTimeMillis());
    }

    /**
     * 一句定稿 → 写会话库。
     *
     * 只存「有意义的话」：状态提示（（没听清）/（处理中）…）这种一过性文字不入库；
     * 同一条消息后续更新走 UPDATE，不会重复插入。
     */
    private void persistLine(Line line) {
        if (line == null) return;
        String sid = logSessionId;
        if (sid.isEmpty()) return;
        String text = line.text == null ? "" : line.text.trim();
        if (text.isEmpty() || statusMarker(text)) return;
        String empId = emp == null ? Briefing.SECRETARY_ID : emp.id;
        int role = line.fromUser ? SessionStore.ROLE_USER : SessionStore.ROLE_AGENT;
        int kind = line.kind == Line.KIND_DETAIL ? SessionStore.KIND_DETAIL : SessionStore.KIND_VOICE;
        String links = line.kind == Line.KIND_DETAIL ? line.linksJson : "[]";
        if (line.dbId > 0) {
            SessionStore.get(this).setText(line.dbId, text, links);
        } else {
            line.dbId = SessionStore.get(this).add(sid, empId, employeeName,
                    role, kind, text, links, line.createdAt);
        }
    }

    /** （没听清）/（已发送）/（这句没发出去）这类瞬时状态，不该进历史 */
    private static boolean statusMarker(String text) {
        return text.length() <= 40 && text.startsWith("（") && text.endsWith("）");
    }

    private static String linksJson(CallReply reply) {
        if (reply == null || reply.attachments.isEmpty()) return "[]";
        JSONArray arr = new JSONArray();
        for (CallReply.Attachment a : reply.attachments) {
            try {
                JSONObject o = new JSONObject();
                o.put("name", a.name);
                o.put("url", a.url);
                arr.put(o);
            } catch (Exception ignored) {
            }
        }
        return arr.toString();
    }

    // ------------------------------------------------------------------ 麦克风

    private void setMicRunning(boolean on) {
        if (on) {
            if (mic != null && mic.isRunning()) return;
            mic = new LiveMic();
            mic.start(new LiveMic.Listener() {
                @Override
                public void onUtterance(byte[] wav, int durationMs) {
                    if (!active || durationMs < 350) return;
                    // 先落一行「我」：说完立刻可见，文字等 ASR 回来再原地补上
                    Line mine = addPendingLine(true);
                    utterances.offer(new Utterance(wav, mine));
                    subtitle = "听到一句 " + String.format(Locale.CHINA, "%.1fs",
                            durationMs / 1000.0) + "，正在发送…";
                }

                @Override
                public void onLevel(float l, boolean sp) {
                    level = l;
                    speaking = sp;
                    boolean playing = "ws".equals(transport) && wsPlayer != null && wsPlayer.isPlaying();
                    // 播放期间抬高 VAD 门槛：让 LiveMic 把回声和真插话区分开
                    LiveMic m = mic;
                    if (m != null) m.setPlaybackActive(playing);
                    // 本地抢先打断：必须连续判到人声 LOCAL_BARGE_MS 才停播，
                    // 单帧回声不至于把自己的播报掐掉；仍比等服务端 barge_in 快
                    if (sp && playing) {
                        long now = System.currentTimeMillis();
                        if (bargeStartAt == 0) bargeStartAt = now;
                        if (now - bargeStartAt >= LOCAL_BARGE_MS) {
                            bargeStartAt = 0;
                            wsPlayer.stopAndFlush();
                            subtitle = "（我在听）";
                        }
                    } else {
                        bargeStartAt = 0;
                    }
                }
            });
            startSender();
        } else {
            if (mic != null) {
                mic.stop();
                mic = null;
            }
            if (sender != null) {
                sender.interrupt();
                sender = null;
            }
            utterances.clear();
        }
    }

    private void startSender() {
        if (sender != null) return;
        sender = new Thread(() -> {
            while (active) {
                try {
                    Utterance u = utterances.poll(1, TimeUnit.SECONDS);
                    if (u == null) continue;
                    sendUtterance(u);
                } catch (InterruptedException e) {
                    Thread.currentThread().interrupt();
                    break;
                }
            }
        }, "voice-call-sender");
        sender.setDaemon(true);
        sender.start();
    }

    /**
     * 实时通道送不出去的那一句，转存到持久化待发队列（VoiceOutbox），由后台按 voice_chat
     * 重发。老板的每条指令都必须落地，这里宁可重复报「已存本地」也不静默丢弃。
     */
    private void keepForRetry(byte[] wav, Line mine, String why) {
        if (wav == null || wav.length < 2000) return;
        lastUtteranceWav = null;                     // 同一句只存一次
        try {
            File tmp = new File(getCacheDir(), "retry_" + System.currentTimeMillis() + ".wav");
            FileOutputStream out = new FileOutputStream(tmp);
            try {
                out.write(wav);
                out.flush();
            } finally {
                out.close();
            }
            String profile = emp == null ? Briefing.SECRETARY_ID : emp.id;
            VoiceOutbox.enqueue(this, tmp, profile, profile);
            //noinspection ResultOfMethodCallIgnored
            tmp.delete();
            ListenService.start(this);
            if (mine != null) {
                ui.post(() -> setLine(mine, "（" + why + "，已存本地，联网后自动重发）", false));
            }
        } catch (Exception e) {
            Log.w("VoiceCallService", "指令转存失败：" + e.getMessage());
        }
    }

    private void sendUtterance(Utterance u) {
        Line mine = u.mine;
        byte[] wav = u.wav;
        lastUtteranceWav = wav;
        if ("ws".equals(transport)) {
            // WS 链路：服务端自己做 ASR 并回 transcript，不用再并行调一次 ASR
            Line prev = pendingMine;
            if (prev != null && prev.pending) setLine(prev, "（没听清）", false);
            pendingMine = mine;
            RealtimeChannel ch = channel;
            if (ch == null || !ch.isReady()) {
                keepForRetry(wav, mine, "通道未就绪");
                return;
            }
            subtitle = "已发送，等回应…";
            ch.sendUtterance(wav);
            return;
        }
        // 兜底 HLS 链路：指令仍然进调度中心（passthrough 一轮），拿回文字后用
        // live_call say 渲染进直播流——live_call 只当播放/渲染层，不再直接 run_hermes。
        final String profile = emp == null ? Briefing.SECRETARY_ID : emp.id;
        postIo.execute(() -> {
            String heard = asrText(wav);
            if (heard.isEmpty()) {
                // 识别为空也要「有声」回应：以前只写一行「（没听清）」，老板听不到任何声音，
                // 体感就是这句指令被丢了。这里用直播流回一句澄清语，并保留屏幕记录。
                ui.post(() -> setLine(mine, "（没听清，已提示重说）", false));
                say("老板，这句我没听清，麻烦您再说一遍。");
                return;
            }
            ui.post(() -> {
                updateLine(mine, heard);
                subtitle = "我听到：「" + heard + "」";
            });
            String result = DispatchApi.ask(this, profile, "voice", heard, 180_000);
            if (result.isEmpty()) {
                // 调度中心没接住：这句指令转存待发队列，由 voice_chat 重发，不丢
                keepForRetry(wav, mine, "后台没接住");
                return;
            }
            ui.post(() -> {
                Line answer = addPendingLine(false);
                subtitle = result.length() > 120 ? result.substring(0, 120) + "…" : result;
                showSpokenText(answer, result, null);
            });
            say(result);
        });
    }

    /** 本地 ASR：兜底链路先把语音变文字，才能提交给调度中心 */
    private String asrText(byte[] wav) {
        try {
            McpConfig cfg = McpConfig.load(this).copy();
            McpClient client = new McpClient(cfg);
            JSONObject args = new JSONObject();
            args.put(cfg.audioKey, Base64.encodeToString(wav, Base64.NO_WRAP));
            args.put("language", cfg.language);
            JSONObject res = client.callTool(StreamContract.TOOL_ASR, args);
            return new JSONObject(StreamContract.textOf(res)).optString("text", "").trim();
        } catch (Exception e) {
            Log.w(TAG, "asr failed: " + e.getMessage());
            return "";
        }
    }

    /** 把文字渲染进直播流（HLS 只做播放层） */
    private void say(String text) {
        final String sid = sessionId;
        if (sid.isEmpty() || text == null || text.trim().isEmpty()) return;
        final String t = text.trim();
        try {
            McpClient client = new McpClient(McpConfig.load(this).copy());
            JSONObject args = new JSONObject();
            args.put("action", StreamContract.ACTION_SAY);
            args.put("session_id", sid);
            args.put("text", t);
            client.callTool(StreamContract.TOOL_LIVE_CALL, args);
        } catch (Exception ignored) {
        }
    }

    /**
     * 员工说话的文字：等声音真的响起来再显示。
     *
     * 服务端把 TTS 语音追加到流尾，客户端还落后 lagMs() 在播；直接把文字甩上去
     * 就会「字先到、声音后到」。拿不到电平快照时退回立即显示。
     */
    /**
     * 把一段回复交给「按声音调度」的队列。
     *
     * @param placeholder 占位行（会先显示"处理中"，第一句真文本到达时替换掉）
     * @param text        整段文本（没有 segments 时兜底用）
     * @param segments    服务端的 segments（含每句 start/end，相对这段音频）
     */
    private void showSpokenText(Line placeholder, String text, JSONArray segments) {
        SpokenTextSync s = textSync;
        if (s == null) {                       // 没有电平计（拿不到锚点）：直接显示
            if (placeholder != null) updateLine(placeholder, text);
            return;
        }
        LiveLevelMeter m = levelMeter;
        float pushSec;
        if (m != null && m.hasData()) {
            pushSec = m.edgeSec();             // 这段音频追加到的媒体时刻
        } else if (m != null) {
            pushSec = m.mediaSec();            // 未热身：用播放头近似（会稍微晚一点）
        } else {
            pushSec = 0f;
        }
        s.push(placeholder, pushSec, segments, text);
        if (segments != null && segments.length() > 0) {
            Log.i(TAG, "reply queued: " + segments.length() + " 句, pushSec=" + pushSec);
        }
    }

    private void maybeStopSelf() {
        if (!active) stopSelf();
    }

    // ------------------------------------------------------------------ 音频路由

    private void enterAudioMode() {
        try {
            audioManager = (AudioManager) getSystemService(Context.AUDIO_SERVICE);
            if (audioManager == null) return;
            prevAudioMode = audioManager.getMode();
            audioManager.setMode(AudioManager.MODE_IN_COMMUNICATION);
            audioManager.setSpeakerphoneOn(true);
        } catch (Exception ignored) {
        }
    }

    private void exitAudioMode() {
        try {
            if (audioManager == null) return;
            audioManager.setSpeakerphoneOn(false);
            audioManager.setMode(prevAudioMode >= 0 ? prevAudioMode : AudioManager.MODE_NORMAL);
        } catch (Exception ignored) {
        }
        audioManager = null;
    }

    // ------------------------------------------------------------------ 挂断

    private void endCall() {
        active = false;
        connected = false;
        ui.removeCallbacks(clock);
        ui.removeCallbacks(chase);
        // 先静音播放器，再发 stop：旧实现反了，会出现「挂断后还在响」
        if (player != null) {
            try { player.stop(); } catch (Exception ignored) { }
            try { player.release(); } catch (Exception ignored) { }
            player = null;
        }
        final String sid = sessionId;
        if (!sid.isEmpty() && stopSent.compareAndSet(false, true)) {
            final McpConfig cfg = McpConfig.load(this).copy();
            Thread t = new Thread(() -> {
                try {
                    McpClient client = new McpClient(cfg);
                    JSONObject args = new JSONObject();
                    args.put("action", StreamContract.ACTION_STOP);
                    args.put("session_id", sid);
                    client.callTool(StreamContract.TOOL_LIVE_CALL, args);
                } catch (Exception ignored) {
                }
            }, "voice-call-stop");
            t.setDaemon(true);
            t.start();
        }
        setMicRunning(false);
        // WS 链路：先让服务端收尾（close → bye → 任务结单），再释放播放器
        RealtimeChannel ch = channel;
        channel = null;
        if (ch != null) ch.close();
        if (wsPlayer != null) {
            wsPlayer.stopAndFlush();
            wsPlayer.release();
            wsPlayer = null;
        }
        pendingMine = null;
        pendingAnswer = null;
        lastReply = null;
        transport = "";
        if (!logSessionId.isEmpty()) {
            SessionStore.get(this).finish(logSessionId, System.currentTimeMillis());
            logSessionId = "";
        }
        taskId = "";
        if (player != null) {
            try { player.stop(); } catch (Exception ignored) { }
            try { player.release(); } catch (Exception ignored) { }
            player = null;
        }
        stopLevelMeter();
        exitAudioMode();
        statusText = "已结束";
        connectedAt = 0;
        sessionId = "";
        streamUrl = "";
        if (io != null) io.shutdownNow();
        stopForeground(true);
    }

    @Override
    public void onDestroy() {
        super.onDestroy();
        if (active) endCall();
        setLineSink(null);
        postIo.shutdownNow();
    }

    // ------------------------------------------------------------------ 通知

    private void updateNotification(String text) {
        try {
            NotificationManager nm = (NotificationManager) getSystemService(NOTIFICATION_SERVICE);
            if (nm != null) nm.notify(NOTIF_ID, notification(text));
        } catch (Exception ignored) {
        }
    }

    private Notification notification(String text) {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            NotificationManager nm = (NotificationManager) getSystemService(NOTIFICATION_SERVICE);
            if (nm != null && nm.getNotificationChannel(CHANNEL) == null) {
                NotificationChannel ch = new NotificationChannel(CHANNEL, "实时语音通话",
                        NotificationManager.IMPORTANCE_LOW);
                ch.setDescription("通话中保持连接");
                nm.createNotificationChannel(ch);
            }
        }
        Intent open = new Intent(this, VoiceCallActivity.class)
                .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK | Intent.FLAG_ACTIVITY_SINGLE_TOP);
        PendingIntent pi = PendingIntent.getActivity(this, 0, open,
                Build.VERSION.SDK_INT >= 23
                        ? PendingIntent.FLAG_UPDATE_CURRENT | PendingIntent.FLAG_IMMUTABLE
                        : PendingIntent.FLAG_UPDATE_CURRENT);
        Notification.Builder b = Build.VERSION.SDK_INT >= Build.VERSION_CODES.O
                ? new Notification.Builder(this, CHANNEL)
                : new Notification.Builder(this);
        return b.setContentTitle("同心圆 · 实时语音通话")
                .setContentText(text)
                .setSmallIcon(R.drawable.ic_call)
                .setOngoing(true)
                .setContentIntent(pi)
                .build();
    }
}
