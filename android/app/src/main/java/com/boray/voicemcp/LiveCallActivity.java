package com.boray.voicemcp;

import android.Manifest;
import android.app.Activity;
import android.content.Context;
import android.content.pm.PackageManager;
import android.media.AudioManager;
import android.graphics.Bitmap;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.util.Base64;
import android.util.Log;
import android.view.View;
import android.widget.Button;
import android.widget.ImageView;
import android.widget.ProgressBar;
import android.widget.TextView;
import android.widget.Toast;

import org.json.JSONArray;
import org.json.JSONObject;

import java.io.ByteArrayOutputStream;
import java.io.InputStream;
import java.net.HttpURLConnection;
import java.net.URL;
import java.util.Locale;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.LinkedBlockingQueue;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicBoolean;

/**
 * 个人档案 · 实时视频通话（server v2.5 live_call + voice_chat.live_session）。
 *
 * 交互（0.5.3）：只有「开始 / 结束」两个按钮，没有重连按钮、没有按住说话：
 *   开始 → live_call{action:start} 起流 → 播同源 HLS → **一直开着麦克风**，
 *         LiveMic 用能量 VAD 自动断句，每句话按 voice_chat{live_session} 发给服务端，
 *         服务端把「收到」和正式回复混进同一路直播流，客户端只管播这一个 m3u8。
 *   结束 → live_call{action:stop}（独立守护线程发出，避免被 onDestroy 杀掉）。
 *
 * 实时性：
 *   - 服务端 playlist 是 EVENT 型、从 seg_0 累加。若从 0 起播就会越拉越远，
 *     所以 prepare 后先 seek 到 live edge（总时长 - 1.5s），之后每 1.5s 读一次 playlist，
 *     落后超过 3s 就再追一次，保证画面跟着最新分片走。
 *   - 断流 / 起流失败不再让用户点重连：内部自动重试 4 次，仍失败就静默重建会话。
 *   - 没有时长上限，通话一直保持，直到用户点「结束」。
 */
public class LiveCallActivity extends Activity {

    public static final String EXTRA_EMPLOYEE = "employee_id";
    private static final int REQ_MIC = 4201;
    private static final String TAG = "LiveCall";

    private enum CallMode { IDLE, CONNECTING, LIVE, DEGRADED }

    private Employee emp;
    private Context app;
    private ExecutorService io;
    private StreamPlayback playback;
    private LiveMic mic;

    private CoverVideoView videoStage;
    private ImageView ivPoster;
    private TextView tvCallState;
    private TextView tvNetState;
    private TextView tvCaption;
    private TextView tvNotice;
    private TextView tvProtocol;
    private TextView tvMicState;
    private ProgressBar pbMic;
    private SessionPanel sessionPanel;
    /** 会话记录 id：视频通话与语音通话写同一张表，只是 transport 不同 */
    private String logSessionId = "";
    /** 最近一次麦克风电平，用于整页会话头部的电平条 */
    private volatile float lastLevel = 0f;
    /** 调度中心实时通道：视频只负责渲染，指令入口仍走 dispatch */
    private RealtimeChannel dialog;
    private volatile boolean dialogReady = false;
    private DispatchApi.Submit dialogSub;
    private Button btnStart;
    private Button btnEnd;

    private final Handler ui = new Handler(Looper.getMainLooper());
    private volatile CallMode mode = CallMode.IDLE;
    private volatile String sessionId = "";
    private String streamUrl = "";
    private long connectedAt = 0;
    private boolean greeted = false;
    private boolean streamAlive = false;


    private final AtomicBoolean stopSent = new AtomicBoolean(false);
    /** 语音句子队列：一句一句按顺序发，不丢话也不并发混流 */
    private final LinkedBlockingQueue<byte[]> utterances = new LinkedBlockingQueue<>(6);
    private Thread sender;
    private AudioManager audioManager;
    private int prevAudioMode = -1;
    private final AtomicBoolean rebuilding = new AtomicBoolean(false);

    private final Runnable clock = new Runnable() {
        @Override
        public void run() {
            if (mode != CallMode.LIVE || connectedAt <= 0) return;
            long sec = (System.currentTimeMillis() - connectedAt) / 1000;
            String dur = String.format(Locale.CHINA, "%02d:%02d", sec / 60, sec % 60);
            tvCallState.setText("通话中 " + dur);
            if (sessionPanel != null) {
                sessionPanel.setCallLive(true);
                sessionPanel.setCallStatus(dur, lastLevel);
            }
            ui.postDelayed(this, 1000);
        }
    };

    /** 追直播边缘：读 playlist 总时长，与当前播放位置比，落后就 seek */
    /** 滑动窗口只有 5 片，不需要再「追边缘」；这里只提示播放是否还在推进 */
    private final Runnable chase = new Runnable() {
        @Override
        public void run() {
            if (mode != CallMode.LIVE) return;
            ui.postDelayed(this, 3000);
        }
    };

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        setContentView(R.layout.activity_live_call);
        WindowInsetsHelper.apply(this);

        app = getApplicationContext();
        io = Executors.newSingleThreadExecutor();
        emp = EmployeeStore.byId(EmployeeStore.load(this),
                getIntent().getStringExtra(EXTRA_EMPLOYEE));
        if (emp == null) { finish(); return; }

        View back = findViewById(R.id.btnBack);
        if (back != null) back.setOnClickListener(v -> endCall());
        videoStage = findViewById(R.id.videoStage);
        ivPoster = findViewById(R.id.ivPoster);
        tvCallState = findViewById(R.id.tvCallState);
        tvNetState = findViewById(R.id.tvNetState);
        tvCaption = findViewById(R.id.tvCaption);
        tvNotice = findViewById(R.id.tvNotice);
        tvProtocol = findViewById(R.id.tvProtocol);
        tvMicState = findViewById(R.id.tvMicState);
        pbMic = findViewById(R.id.pbMic);
        btnStart = findViewById(R.id.btnStart);
        btnEnd = findViewById(R.id.btnEnd);
        sessionPanel = findViewById(R.id.sessionPanel);
        if (sessionPanel != null) {
            sessionPanel.bind(emp.id, emp.name);
            sessionPanel.setOnHangup(this::endCall);   // 整页头部的小挂断
        }

        ((TextView) findViewById(R.id.tvEmpName)).setText(emp.name + " · " + emp.title);
        String proto = emp.liveProtocol.isEmpty() ? "hls" : emp.liveProtocol;
        tvProtocol.setText("同源 HTTPS 443 · " + proto.toUpperCase(Locale.CHINA)
                + " Live 短分片（" + (emp.liveSegmentSec > 0
                ? String.format(Locale.CHINA, "%.0fs", emp.liveSegmentSec) : "1s") + "/片）"
                + " · 音视频 mux 同轨 · 全程持续聆听，说停自动发送");

        String poster = emp.photo.isEmpty() ? emp.faceUrl : emp.photo;
        EmployeeStore.loadPhotoBitmap(this, poster, new EmployeeStore.BitmapCallback() {
            @Override
            public void onLoaded(Bitmap bmp) {
                if (!isFinishing() && bmp != null) ivPoster.setImageBitmap(bmp);
            }
        });

        btnStart.setOnClickListener(v -> startCall());
        btnEnd.setOnClickListener(v -> endCall());

        playback = new StreamPlayback();
        videoStage.setVisibility(View.GONE);
        setMicIdle("未开始");
        tvCallState.setText("待接通");
        setState("点「开始」接通实时视频，之后正常说话即可，无需按键");
        caption("接通后会一直聆听：说完一句自动发出去，员工的回应直接混进实时流，音画同步。");
    }

    // ------------------------------------------------------------------ 开始 / 结束

    private void startCall() {
        if (checkSelfPermission(Manifest.permission.RECORD_AUDIO)
                != PackageManager.PERMISSION_GRANTED) {
            requestPermissions(new String[]{Manifest.permission.RECORD_AUDIO}, REQ_MIC);
            Toast.makeText(this, "实时通话需要麦克风权限", Toast.LENGTH_SHORT).show();
            return;
        }
        mode = CallMode.CONNECTING;
        stopSent.set(false);
        btnStart.setEnabled(false);
        btnStart.setText("连接中…");
        btnEnd.setEnabled(true);
        tvCallState.setText("连接中");
        setState("正在向服务端发起实时通话…");
        caption("正在协商同源直播流…");
        connect(true);
    }

    /** @param greet 是否让员工先开口（首次接通时说一句问候） */
    private void connect(boolean greet) {
        if (isFinishing()) return;
        if (playback != null) playback.release();
        videoStage.setVisibility(View.GONE);
        ivPoster.setVisibility(View.VISIBLE);
        streamAlive = false;
        sessionId = "";
        connectedAt = 0;
        greeted = !greet;
        ui.removeCallbacks(clock);
        ui.removeCallbacks(chase);

        io.execute(() -> {
            try {
                McpConfig cfg = McpConfig.load(app).copy();
                String base = AssetUrls.serverBase(app);
                McpClient client = new McpClient(cfg);
                JSONArray tools = client.listTools();
                boolean liveTool = false;
                for (int i = 0; i < tools.length(); i++) {
                    JSONObject t = tools.optJSONObject(i);
                    if (t != null && StreamContract.TOOL_LIVE_CALL.equals(t.optString("name", ""))) {
                        liveTool = true;
                        break;
                    }
                }

                StreamContract.CallSession s;
                if (liveTool) {
                    JSONObject args = new JSONObject();
                    args.put("action", StreamContract.ACTION_START);
                    args.put("avatar_id", emp.id);
                    args.put("first_segment_sec", 8);
                    s = StreamContract.parseCall(cfg.url, base,
                            StreamContract.textOf(client.callTool(StreamContract.TOOL_LIVE_CALL, args)));
                } else {
                    s = new StreamContract.CallSession();
                    s.message = "服务端未提供 live_call 工具";
                }
                if (!s.ready()) {
                    String pre = StreamContract.resolveStream(cfg.url, base, emp.callStream);
                    if (!pre.isEmpty()) s.streamUrl = pre;
                }
                if (!s.ready()) {
                    final String reason = s.message.isEmpty() ? "未返回同源 live 流地址" : s.message;
                    runOnUiThread(() -> onConnectFailed(reason));
                    return;
                }
                final StreamContract.CallSession ready = s;
                runOnUiThread(() -> {
                    enterLive(ready);
                    startListening();
                });
            } catch (Exception e) {
                String msg = e.getMessage() == null ? "发起通话失败" : e.getMessage();
                runOnUiThread(() -> onConnectFailed(msg));
            }
        });
    }

    /** 进入通话音频模式：保证 AEC 生效、声音走扬声器，结束时恢复 */
    private void enterCallAudio() {
        try {
            audioManager = (AudioManager) getSystemService(Context.AUDIO_SERVICE);
            if (audioManager == null) return;
            prevAudioMode = audioManager.getMode();
            audioManager.setMode(AudioManager.MODE_IN_COMMUNICATION);
            audioManager.setSpeakerphoneOn(true);
        } catch (Exception ignored) {
        }
    }

    private void exitCallAudio() {
        try {
            if (audioManager == null) return;
            audioManager.setSpeakerphoneOn(false);
            audioManager.setMode(prevAudioMode >= 0 ? prevAudioMode : AudioManager.MODE_NORMAL);
        } catch (Exception ignored) {
        }
        audioManager = null;
    }

    private void enterLive(StreamContract.CallSession s) {
        if (isFinishing()) return;
        enterCallAudio();
        ScreenOn.keep(this, true);        // 实时视频通话期间保持常亮
        mode = CallMode.LIVE;
        sessionId = s.sessionId;
        streamUrl = s.streamUrl;
        streamAlive = true;
        connectedAt = System.currentTimeMillis();
        btnStart.setEnabled(false);
        btnStart.setText("通话中");
        btnEnd.setEnabled(true);
        videoStage.setVisibility(View.VISIBLE);
        tvCallState.setText("通话中 00:00");
        ui.removeCallbacks(clock);
        ui.post(clock);
        setState("实时流已接入 · 正在持续聆听");
        ensureLogSession();
        startDialog();
        if (sessionPanel != null) {
            sessionPanel.setCallLive(true);
            sessionPanel.openFull();
        }
        caption("正常说话即可，说完一句会自动发送；员工的回应直接混进这路流，画面与语音同步。");
        notice("会话 " + (sessionId.isEmpty() ? "—" : sessionId)
                + (s.segmentMs > 0 ? " · " + s.segmentMs + "ms 分片" : "")
                + " · 无时长限制，点「结束」挂断");

        playback.play(videoStage, streamUrl, StreamPlayback.Mode.LIVE, true,
                new StreamPlayback.Listener() {
                    @Override
                    public void onWaiting(String hint) {
                        net(hint);
                    }

                    @Override
                    public void onPreparing(int attempt) {
                        net(attempt == 0 ? "正在接入实时流…" : "自动重连第 " + attempt + " 次…");
                    }

                    @Override
                    public void onReady(int durationMs) {
                        ivPoster.setVisibility(View.GONE);
                        net("直播中 · 已接入实时画面");
                        if (!greeted) {
                            greeted = true;
                            greet();
                        }
                        ui.removeCallbacks(chase);
                        ui.postDelayed(chase, 3000);
                    }

                    @Override
                    public void onBuffering(boolean busy) {
                        net(busy ? "缓冲中…" : "直播中 · 已追到最新画面");
                    }

                    @Override
                    public void onProgress(int positionMs, int durationMs) {
                    }

                    @Override
                    public void onCompleted() {
                        // 5 片滑动窗口播到末尾就会 onCompletion：重开同一路流（换时间戳），
                        // 不能重建 session，否则正在说话的那一路会丢
                        net("列表播完，正在续接…");
                        if (mode == CallMode.LIVE && playback != null) {
                            ui.postDelayed(() -> {
                                if (mode == CallMode.LIVE) playback.replay();
                            }, 500);
                        }
                    }

                    @Override
                    public void onRetry(int attempt, String reason) {
                        net("断流，自动重连第 " + attempt + " 次…");
                        streamAlive = false;
                    }

                    @Override
                    public void onFailed(String reason) {
                        streamAlive = false;
                        net("实时流中断：" + reason);
                        autoRebuild(reason);
                    }
                });
    }

    /** 自动重建会话：不弹按钮、不打断麦克风，静默换一条新流继续播 */
    private void autoRebuild(String reason) {
        if (mode != CallMode.LIVE || isFinishing()) return;
        if (!rebuilding.compareAndSet(false, true)) return;
        ui.removeCallbacks(chase);
        setState("实时流断开 · 正在自动重连");
        notice("内部自动重连中（" + shortReason(reason) + "），麦克风保持开启，不用手动点。");
        ui.postDelayed(() -> {
            rebuilding.set(false);
            if (mode == CallMode.LIVE && !isFinishing()) connect(false);
        }, 1500);
    }

    private void onConnectFailed(String reason) {
        if (isFinishing()) return;
        boolean cap = reason != null && reason.contains("上限");
        if (cap) {
            // 会话满属于服务端资源问题，不再徒劳重试
            mode = CallMode.DEGRADED;
            btnStart.setEnabled(true);
            btnStart.setText("开始");
            btnEnd.setEnabled(false);
            tvCallState.setText("无法接通");
            stopListening();
            setState("服务端实时通话会话已满");
            caption("服务端同时最多 8 路通话，旧的没挂断会占满名额，稍后再试。");
            notice("请服务端清理遗留会话（live_call 没有 list/cleanup 动作）。");
            return;
        }
        // 其它失败：安静重试，不要求用户点「重新连接」
        if (mode == CallMode.CONNECTING) {
            setState("连接失败，正在自动重试…");
            notice("自动重试中（" + shortReason(reason) + "）");
            ui.postDelayed(() -> {
                if (!isFinishing() && mode == CallMode.CONNECTING) connect(true);
            }, 2500);
        }
    }

    // ------------------------------------------------------------------ 持续聆听

    private void startListening() {
        if (mic != null && mic.isRunning()) return;
        mic = new LiveMic();
        mic.start(new LiveMic.Listener() {
            @Override
            public void onUtterance(byte[] wav, int durationMs) {
                if (mode != CallMode.LIVE) return;
                if (durationMs < 350) return;
                boolean queued = utterances.offer(wav);
                final String size = String.format(Locale.CHINA, "%.1fs", durationMs / 1000.0);
                ui.post(() -> setMicState(queued
                        ? "已拾音 " + size + " · 排队发送（" + utterances.size() + "）"
                        : "上一句还在处理，这句先跳过"));
            }

            @Override
            public void onLevel(float level, boolean speaking) {
                lastLevel = level;
                ui.post(() -> {
                    if (isFinishing() || mode != CallMode.LIVE) return;
                    int pct = Math.round(Math.min(1f, level * 2.4f) * 100);
                    if (pbMic != null) pbMic.setProgress(pct);
                    setMicState(speaking ? "正在听你说…" : "聆听中 · 请说话");
                });
            }
        });
        startSender();
        setMicState(mic.isRunning() ? "聆听中 · 请说话" : "麦克风不可用");
        if (!mic.isRunning()) {
            notice("麦克风启动失败，请检查录音权限或被其它应用占用。");
        }
    }

    private void stopListening() {
        if (mic != null) {
            mic.stop();
            mic = null;
        }
        if (sender != null) {
            sender.interrupt();
            sender = null;
        }
        utterances.clear();
        setMicIdle("已停止聆听");
    }

    /** 串行发送线程：按说话顺序一句一句发，语音回合慢也不会丢话 */
    private void startSender() {
        if (sender != null) return;
        sender = new Thread(() -> {
            while (mode == CallMode.LIVE || !utterances.isEmpty()) {
                try {
                    byte[] wav = utterances.poll(1, TimeUnit.SECONDS);
                    if (wav == null) {
                        if (mode != CallMode.LIVE) break;
                        continue;
                    }
                    sendUtterance(wav);
                } catch (InterruptedException e) {
                    Thread.currentThread().interrupt();
                    break;
                }
            }
        }, "live-sender");
        sender.setDaemon(true);
        sender.start();
    }

    /**
     * 上行一句话。
     *
     * 主链路：调度中心 P0 实时通道（WS）——服务端做 ASR/hermes，回 transcript + reply，
     * 客户端只把 voice_text 用 live_call say 渲染进 HLS（口型/画面），
     * 这样指令有 P0 任务、utterances 台账、lane 分配、通话摘要交付物。
     *
     * 兜底：通道没建立时用 dispatch passthrough 跑一轮，再 say 渲染，入口仍不绕过调度中心。
     */
    private void sendUtterance(byte[] wav) {
        RealtimeChannel ch = dialog;
        if (ch != null && dialogReady) {
            ui.post(() -> setMicState("已发送 · 等回应"));
            ch.sendUtterance(wav);
            return;
        }
        if (dialogSub != null && !dialogSub.taskId.isEmpty()) {
            ui.post(() -> setMicState("通道重连中 · 这句稍后重试"));
            return;
        }
        io.execute(() -> {
            String heard = asrText(wav);
            if (heard.isEmpty()) {
                ui.post(() -> {
                    setMicState("没听清 · 继续聆听");
                    notice("这一句没识别出来，再说一次即可。");
                });
                return;
            }
            logMsg(SessionStore.ROLE_USER, SessionStore.KIND_VOICE, heard);
            ui.post(() -> caption("我听到：「" + heard + "」"));
            String result = DispatchApi.ask(app, emp.id, "video", heard, 180_000);
            if (result.isEmpty()) {
                ui.post(() -> notice("这一句没拿到回复（调度中心未返回），不影响后续。"));
                return;
            }
            logMsg(SessionStore.ROLE_AGENT, SessionStore.KIND_VOICE, result);
            say(result);
            ui.post(() -> {
                caption(result.length() > 220 ? result.substring(0, 220) + "…" : result);
                setMicState("已回复 · 继续聆听");
            });
        });
    }

    /** 本地 ASR：兜底链路需要先把语音变成文字，才能提交给调度中心 */
    private String asrText(byte[] wav) {
        try {
            McpConfig cfg = McpConfig.load(app).copy();
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

    // ------------------------------------------------------------------ 实时通道

    /** 打开 P0 实时通道（modality=video）；失败则走 passthrough 兜底 */
    private void startDialog() {
        final String profile = emp.id;
        io.execute(() -> {
            DispatchApi.Submit s = DispatchApi.submit(app, profile, "", "视频通话接通", "video", "ws");
            if (!s.wsReady()) {
                Log.w(TAG, "dispatch ws unavailable: " + s.message);
                ui.post(() -> notice("调度中心实时通道不可用，指令将按单轮任务提交。"));
                return;
            }
            java.util.List<String> urls = new java.util.ArrayList<>();
            String pub = DispatchApi.publicWsUrl(app, s);
            if (!pub.isEmpty()) urls.add(pub);
            String given = DispatchApi.wsUrl(app, s, false);
            if (!given.isEmpty() && !urls.contains(given)) urls.add(given);
            String lan = DispatchApi.wsUrl(app, s, true);
            if (!lan.isEmpty() && !urls.contains(lan)) urls.add(lan);
            RealtimeChannel ch = new RealtimeChannel(s, urls, dialogListener);
            if (!ch.openBlocking()) {
                Log.w(TAG, "ws connect failed");
                ch.close();
                DispatchApi.cancel(s.taskId);
                ui.post(() -> notice("实时通道未建立，指令将按单轮任务提交。"));
                return;
            }
            if (isFinishing()) {
                ch.close();
                return;
            }
            dialog = ch;
            dialogSub = s;
            dialogReady = true;
            ui.post(() -> net("调度中心实时通道已接入"));
        });
    }

    private final RealtimeChannel.Listener dialogListener = new RealtimeChannel.Listener() {
        @Override
        public void onStatus(String text) {
            ui.post(() -> net(text));
        }

        @Override
        public void onReady(boolean reconnected) {
            dialogReady = true;
        }

        @Override
        public void onTranscript(String text) {
            logMsg(SessionStore.ROLE_USER, SessionStore.KIND_VOICE, text);
            ui.post(() -> caption("我听到：「" + text + "」"));
        }

        @Override
        public void onAudio(byte[] data) {
            // 视频的音轨由 HLS 负责，WS 下行的 ack/TTS 音频直接丢弃，避免双声道
        }

        @Override
        public void onReply(CallReply reply) {
            if (reply == null) return;
            if (reply.turn == 0 && reply.greeting) return;   // 本地已 greet，避免重复问好
            String spoken = reply.spoken();
            if (!spoken.isEmpty()) {
                logMsg(SessionStore.ROLE_AGENT, SessionStore.KIND_VOICE, spoken);
                say(spoken);
            }
            if (reply.hasExtraDetail()) {
                logMsg(SessionStore.ROLE_AGENT, SessionStore.KIND_DETAIL, reply.detail);
            }
            ui.post(() -> {
                caption(spoken.isEmpty() ? reply.detail : spoken);
                setMicState("已回复 · 继续聆听");
            });
        }

        @Override
        public void onReplyEnd() {
        }

        @Override
        public void onBargeIn() {
        }

        @Override
        public void onServerText(String text) {
            logMsg(SessionStore.ROLE_AGENT, SessionStore.KIND_DETAIL, text);
        }

        @Override
        public void onServerError(String message) {
            ui.post(() -> notice(message));
        }

        @Override
        public void onClosed(String reason) {
            // 通道彻底不可用：清掉 submit 标记，后续每句自动走 passthrough 兜底
            dialogReady = false;
            dialog = null;
            dialogSub = null;
            ui.post(() -> net("实时通道已断开，后续指令走单轮调度"));
        }
    };

    // ------------------------------------------------------------------ 会话记录

    private void ensureLogSession() {
        if (!logSessionId.isEmpty() || emp == null) return;
        logSessionId = SessionStore.get(app).begin(emp.id, emp.name, "video",
                System.currentTimeMillis());
    }

    /** 写一条消息（视频通话与语音通话共用同一张表，界面按人 + 天回看） */
    private void logMsg(int role, int kind, String text) {
        if (text == null || text.trim().isEmpty()) return;
        ensureLogSession();
        if (logSessionId.isEmpty()) return;
        SessionStore.get(app).add(logSessionId, emp.id, emp.name, role, kind,
                text.trim(), "[]", System.currentTimeMillis());
    }

    private void finishLogSession() {
        if (logSessionId.isEmpty()) return;
        SessionStore.get(app).finish(logSessionId, System.currentTimeMillis());
        logSessionId = "";
    }

    // ------------------------------------------------------------------ 结束

    /**
     * 挂断：必须真正把 stop 发出去。
     * 独立守护线程 + flag，避免被 onDestroy 里的 shutdownNow 杀掉导致服务端会话泄漏。
     */
    private void sendStopOnce() {
        final String sid = sessionId;
        if (sid.isEmpty() || !stopSent.compareAndSet(false, true)) return;
        final McpConfig cfg = McpConfig.load(app).copy();
        Thread t = new Thread(() -> {
            try {
                JSONObject args = new JSONObject();
                args.put("action", StreamContract.ACTION_STOP);
                args.put("session_id", sid);
                new McpClient(cfg).callTool(StreamContract.TOOL_LIVE_CALL, args);
            } catch (Exception ignored) {
            }
        }, "live-stop");
        t.setDaemon(true);
        t.start();
    }

    private void endCall() {
        finishLogSession();
        if (dialog != null) {
            dialog.close();
            dialog = null;
        }
        dialogReady = false;
        if (sessionPanel != null) sessionPanel.setCallLive(false);
        mode = CallMode.IDLE;
        ScreenOn.keep(this, false);
        ui.removeCallbacks(clock);
        ui.removeCallbacks(chase);
        sendStopOnce();
        stopListening();
        exitCallAudio();
        if (playback != null) playback.release();
        finish();
    }

    /**
     * 返回键：整页会话 → 只收回视频页；通话中 → 先确认再结束，不能直接退。
     */
    @Override
    public void onBackPressed() {
        if (sessionPanel != null && sessionPanel.isFullScreen()) {
            sessionPanel.showBar();
            return;
        }
        if (mode == CallMode.LIVE || mode == CallMode.CONNECTING) {
            new android.app.AlertDialog.Builder(this)
                    .setTitle("正在通话中")
                    .setMessage("退出会先结束当前通话。确定要退出吗？")
                    .setNegativeButton("继续通话", null)
                    .setPositiveButton("结束并退出", (d, w) -> endCall())
                    .show();
            return;
        }
        endCall();
    }

    @Override
    public void onRequestPermissionsResult(int requestCode, String[] permissions, int[] results) {
        super.onRequestPermissionsResult(requestCode, permissions, results);
        if (requestCode == REQ_MIC) {
            boolean ok = results.length > 0
                    && results[0] == PackageManager.PERMISSION_GRANTED;
            if (ok) startCall();
            else Toast.makeText(this, "没有麦克风权限无法通话", Toast.LENGTH_LONG).show();
        }
    }

    @Override
    protected void onDestroy() {
        super.onDestroy();
        ScreenOn.keep(this, false);
        mode = CallMode.IDLE;
        sendStopOnce();
        ui.removeCallbacks(clock);
        ui.removeCallbacks(chase);
        stopListening();
        exitCallAudio();
        if (playback != null) playback.release();
        if (io != null) io.shutdownNow();
    }

    // ------------------------------------------------------------------ 小工具

    private void greet() {
        say("您好，我是" + emp.name + "，" + emp.title + "。您说，我在听。");
    }

    /**
     * 把文字渲染进直播流（数字人口型 + 语音）。
     *
     * 视频通话里 HLS 只当渲染层：指挥链是「dispatch 出文字 → live_call say 出声画」，
     * 不再用 voice_chat 直接 run_hermes 绕过调度中心。
     */
    private void say(String text) {
        if (text == null || text.trim().isEmpty() || sessionId.isEmpty()) return;
        final String t = text.trim();
        io.execute(() -> {
            try {
                McpClient client = new McpClient(McpConfig.load(app).copy());
                JSONObject args = new JSONObject();
                args.put("action", StreamContract.ACTION_SAY);
                args.put("session_id", sessionId);
                args.put("text", t);
                client.callTool(StreamContract.TOOL_LIVE_CALL, args);
            } catch (Exception ignored) {
            }
        });
    }

    private void setState(String state) {
        if (!isFinishing()) tvNetState.setText(state);
    }

    private void net(String s) {
        if (!isFinishing()) tvNetState.setText(s);
    }

    private void caption(String s) {
        if (!isFinishing()) tvCaption.setText(s);
    }

    private void notice(String s) {
        if (!isFinishing()) tvNotice.setText(s);
    }

    private void setMicState(String s) {
        if (!isFinishing() && tvMicState != null) tvMicState.setText(s);
    }

    private void setMicIdle(String s) {
        setMicState(s);
        if (pbMic != null) pbMic.setProgress(0);
    }

    private static String shortReason(String s) {
        if (s == null) return "";
        return s.length() > 90 ? s.substring(0, 90) + "…" : s;
    }
}
