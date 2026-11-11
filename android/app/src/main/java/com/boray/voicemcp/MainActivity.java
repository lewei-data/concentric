package com.boray.voicemcp;

import android.Manifest;
import android.animation.ObjectAnimator;
import android.animation.PropertyValuesHolder;
import android.animation.ValueAnimator;
import android.app.Activity;
import android.content.Intent;
import android.content.pm.PackageManager;
import android.net.Uri;
import android.content.res.ColorStateList;
import android.os.Build;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.util.Base64;
import android.view.LayoutInflater;
import android.view.MotionEvent;
import android.view.View;
import android.widget.CompoundButton;
import android.widget.ImageView;
import android.graphics.Typeface;
import android.text.Spannable;
import android.text.SpannableStringBuilder;
import android.text.style.ForegroundColorSpan;
import android.text.style.StyleSpan;
import android.view.ViewGroup;
import android.widget.HorizontalScrollView;
import android.widget.LinearLayout;
import android.widget.ScrollView;
import android.widget.ProgressBar;
import android.widget.TextView;
import android.widget.Toast;

import org.json.JSONArray;
import org.json.JSONException;
import org.json.JSONObject;

import java.io.File;
import java.io.FileInputStream;
import java.io.IOException;
import java.nio.charset.StandardCharsets;
import java.util.ArrayList;
import java.util.List;
import java.util.Locale;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;

/**
 * 唯一功能：按下说话 → 通过 MCP 把语音发给同心圆 agent → 显示核心内容、语音播报、文件可点开。
 *
 * 服务端已升级为异步任务模型：voice_chat(wait=false) 立即回「收到」+ job_id，
 * 真正的执行结果由常驻的 ListenService 用 voice_job 长轮询取回并播报。
 * 关掉「异步任务」开关可退回旧的 wait=true 同步模式。
 */
public class MainActivity extends Activity {

    private static final int REQ_PERMISSIONS = 1001;

    /** 供监听服务判断 App 是否在前台（前台才弹数字员工汇报画面） */
    public static boolean isVisible = false;

    private final ExecutorService executor = Executors.newSingleThreadExecutor();
    private final Handler ui = new Handler(Looper.getMainLooper());

    private McpConfig cfg;
    private McpClient client;
    private WavRecorder recorder;
    private VoicePlayer player;

    private boolean recording = false;
    private boolean busy = false;
    private McpResult lastResult;

    private TextView tvEndpoint;
    private TextView tvStatus;
    private TextView tvResult;
    private TextView tvNoAttachment;
    private TextView tvRecordHint;
    private TextView tvBadge;
    private TextView tvEmpName;
    private TextView tvEmpTitle;
    private TextView tvListenState;
    private DigitalHumanView dhvMini;
    private ImageView ivMiniPhoto;
    private View statusDot;
    private View micGlow;
    private View btnRecord;
    private View btnProgress;
    private View btnTodo;
    private View btnSettings;
    private View btnVoiceCall;
    private android.widget.ImageView ivVoiceCall;
    private View btnCall;
    private TextView tvCallBtn;
    private TextView tvCallNotice;
    private View connectRing;
    private ObjectAnimator connectAnim;
    private CallTone tone;
    private boolean ringing = false;
    /** 刚点下通话的时刻：服务是异步启动的，这段时间内 isActive() 还是 false，不能据此停拨号音 */
    private long callRequestedAt = 0L;
    private boolean wasConnected = false;
    private boolean wasExpandedForCall = false;
    private final Runnable noticeHide = () -> {
        if (tvCallNotice != null) tvCallNotice.setVisibility(View.GONE);
    };
    private SessionPanel sessionPanel;
    /** 首页内嵌的会话记录：只显示最新一条，右上角「打开」切到弹出层 */
    private SessionPanel sessionTeaser;
    private TextView tvHomeSub;
    private View heroCall;
    private TextView tvCallHint;
    private View llCallBar;
    private TextView tvCallTimer;
    private View btnHangup;
    private VoiceLevelView levelView;
    private int heroBigHeight;
    private int heroCompactHeight;
    private static final int REQ_CALL = 991;
    private CompoundButton cbListen;
    private ProgressBar progressRing;
    private LinearLayout llAttachments;
    private ObjectAnimator pulseAnimator;

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        setContentView(R.layout.activity_main);
        WindowInsetsHelper.apply(this);

        tvEndpoint = findViewById(R.id.tvEndpoint);
        tvStatus = findViewById(R.id.tvStatus);
        tvResult = findViewById(R.id.tvResult);
        tvNoAttachment = findViewById(R.id.tvNoAttachment);
        tvRecordHint = findViewById(R.id.tvRecordHint);
        tvBadge = findViewById(R.id.tvBadge);
        tvEmpName = findViewById(R.id.tvEmpName);
        tvEmpTitle = findViewById(R.id.tvEmpTitle);
        tvListenState = findViewById(R.id.tvListenState);
        dhvMini = findViewById(R.id.dhvMini);
        ivMiniPhoto = findViewById(R.id.ivMiniPhoto);
        statusDot = findViewById(R.id.statusDot);
        micGlow = findViewById(R.id.micGlow);
        btnRecord = findViewById(R.id.btnRecord);
        btnProgress = findViewById(R.id.btnProgress);
        btnTodo = findViewById(R.id.btnTodo);
        btnSettings = findViewById(R.id.btnSettings);
        btnVoiceCall = findViewById(R.id.btnVoiceCall);
        ivVoiceCall = findViewById(R.id.ivVoiceCall);
        btnCall = findViewById(R.id.btnCall);
        tvCallBtn = findViewById(R.id.tvCallBtn);
        tvCallNotice = findViewById(R.id.tvCallNotice);
        connectRing = findViewById(R.id.connectRing);
        sessionPanel = findViewById(R.id.sessionPanel);
        sessionTeaser = findViewById(R.id.sessionTeaser);
        tvHomeSub = findViewById(R.id.tvHomeSub);
        heroCall = findViewById(R.id.heroCall);
        if (sessionPanel != null) {
            // 收起弹出层 → 只剩首页内嵌那条，不会再「整块消失」
            sessionPanel.setOnCollapsed(() -> sessionPanel.setVisibility(View.GONE));
            // 整页头部的小挂断：直接结束通话并回到主界面
            sessionPanel.setOnHangup(this::hangupFromSession);
        }
        if (sessionTeaser != null) {
            sessionTeaser.setInlineMode(true);
            sessionTeaser.setOnOpen(this::openSessionSheet);
        }
        tvCallHint = findViewById(R.id.tvCallHint);
        llCallBar = findViewById(R.id.llCallBar);
        tvCallTimer = findViewById(R.id.tvCallTimer);
        btnHangup = findViewById(R.id.btnHangup);
        levelView = findViewById(R.id.levelView);
        float d = getResources().getDisplayMetrics().density;
        heroBigHeight = Math.round(264 * d);
        heroCompactHeight = Math.round(84 * d);
        if (levelView != null) levelView.setBarColor(getResources().getColor(R.color.primary, getTheme()));
        cbListen = findViewById(R.id.cbListen);
        progressRing = findViewById(R.id.progressRing);
        llAttachments = findViewById(R.id.llAttachments);

        tone = new CallTone(this);
        cfg = McpConfig.load(this);
        player = new VoicePlayer(this);
        PushProcessor.ensureChannels(this);
        ensurePermissions();
        // v2.1.0：后台从 profile 拉取 18 人档案（缓存 6 小时）；离线时用已缓存 / 内置档案
        EmployeeStore.syncIfStale(this);

        cbListen.setChecked(cfg.keepListening);
        cbListen.setOnCheckedChangeListener((button, checked) -> {
            McpConfig next = McpConfig.load(this);
            next.keepListening = checked;
            next.save(this);
            cfg = next;
            if (checked) {
                ensurePermissions();
                ListenService.start(this);
            } else {
                ListenService.stop(this);
            }
            refreshListenState();
        });

        applyPressEffect(btnRecord);
        applyPressEffect(btnProgress);
        applyPressEffect(btnTodo);
        applyPressEffect(btnSettings);
        applyPressEffect(btnVoiceCall);

        btnRecord.setOnClickListener(v -> {
            if (busy) {
                toast("正在等待服务端返回，请稍候");
            } else if (recording) {
                stopAndSend();
            } else {
                startRecording();
            }
        });
        // 当前进度：小秘书整理的简报（重大事项 / 交付成果 / 完成进度）
        btnProgress.setOnClickListener(v -> startActivity(new Intent(this, ProgressActivity.class)));
        // 待办事项：需要老板拍板的事，可审批
        btnTodo.setOnClickListener(v -> startActivity(new Intent(this, TodoActivity.class)));
        btnSettings.setOnClickListener(v -> startActivity(new Intent(this, SettingsActivity.class)));
        // 实时语音通话：点一下接通（图标变红），再点挂断
        btnVoiceCall.setOnClickListener(v -> toggleCall());
        if (btnHangup != null) {
            btnHangup.setOnClickListener(v -> toggleCall());   // 通话条上的挂断键，与首页图标同一套逻辑
            applyPressEffect(btnHangup);
        }
        if (btnCall != null) {
            btnCall.setOnClickListener(v -> toggleCall());
            applyPressEffect(btnCall);
        }
        findViewById(R.id.cardEmployee).setOnClickListener(v ->
                startActivity(new Intent(this, EmployeeListActivity.class)));
    }

    @Override
    protected void onResume() {
        super.onResume();
        isVisible = true;
        ringing = VoiceCallService.isActive() && !VoiceCallService.isConnected();
        if (ringing) tone.startDialing();
        wasConnected = VoiceCallService.isConnected();
        callTick.removeCallbacks(callTickRunnable);
        callTick.post(callTickRunnable);
        cfg = McpConfig.load(this);
        client = null;              // 设置改动后立即按新端点重新握手
        EmployeeStore.syncIfStale(this);   // 档案缓存过期则后台刷新
        refreshEndpointLabel();
        refreshEmployeeCard();
        cbListen.setOnCheckedChangeListener(null);
        cbListen.setChecked(cfg.keepListening);
        cbListen.setOnCheckedChangeListener((button, checked) -> {
            McpConfig next = McpConfig.load(this);
            next.keepListening = checked;
            next.save(this);
            cfg = next;
            if (checked) { ensurePermissions(); ListenService.start(this); }
            else ListenService.stop(this);
            refreshListenState();
        });
        if (cfg.keepListening && !cfg.demo && !ListenService.isRunning(this)) {
            ListenService.start(this);
        }
        refreshListenState();
        restoreLastResult();
        bindSession();          // 会话面板：按当前汇报人 + 当天加载历史
    }

    /**
     * 返回键：
     * 1) 整页会话开着 → 只收起回首页，不退出；
     * 2) 通话中 → 不能直接退，先确认，确认后先中断会话再退出；
     * 3) 其它 → 正常返回。
     */
    @Override
    public void onBackPressed() {
        if (sessionPanel != null && sessionPanel.getVisibility() == View.VISIBLE) {
            sessionPanel.setVisibility(View.GONE);
            return;
        }
        if (VoiceCallService.isActive()) {
            confirmLeaveCall();
            return;
        }
        super.onBackPressed();
    }

    /** 通话中退出确认：先挂断，再退出，不允许「带着通话」直接退 */
    private void confirmLeaveCall() {
        new android.app.AlertDialog.Builder(this)
                .setTitle("正在通话中")
                .setMessage("退出会先结束当前通话。确定要退出吗？")
                .setNegativeButton("继续通话", null)
                .setPositiveButton("结束并退出", (d, w) -> {
                    ringing = false;
                    tone.stop();
                    VoiceCallService.stop(this);
                    finish();
                })
                .show();
    }

    @Override
    protected void onPause() {
        super.onPause();
        isVisible = false;
        tone.stop();                 // 页面不可见就别在后台响拨号音
        tone.restoreVolumeIfBoosted();
        startConnectAnim(false);
        callTick.removeCallbacks(callTickRunnable);
    }

    // ------------------------------------------------------------------ 实时语音通话

    private void toggleCall() {
        if (VoiceCallService.isActive()) {
            ringing = false;
            tone.stop();
            VoiceCallService.stop(this);
            toast("已结束通话");
            applyCallState();
            renderCallUi();
            return;
        }
        if (!VoiceCallService.hasMicPermission(this)) {
            requestPermissions(new String[]{Manifest.permission.RECORD_AUDIO}, REQ_CALL);
            toast("通话需要麦克风权限，请允许后重试");
            return;
        }
        VoiceCallService.clearLines();
        callRequestedAt = System.currentTimeMillis();
        ringing = true;
        VoiceCallService.start(this);
        tone.startDialing();          // 拨号音：接通后自动换成「已接通」提示音
        applyCallState();
        renderCallUi();
    }

    /** 当前汇报人名字：通话中用服务端确认的那位，否则取档案里的当前汇报人 */
    private String reporterName() {
        if (VoiceCallService.isActive()) {
            String who = VoiceCallService.who();
            if (who != null && !who.isEmpty()) return who;
        }
        Employee e = EmployeeStore.current(this, EmployeeStore.load(this));
        if (e != null && e.name != null && !e.name.isEmpty()) return e.name;
        return "小秘书";
    }

    /** 汇报人换了，提示文案与转写里的称呼一起换（不再写死「小秘书」） */
    private void applyReporterLabels() {
        String name = reporterName();
        if (tvCallHint != null && !VoiceCallService.isConnected()) {
            tvCallHint.setText("点击通话，和 " + name + " 实时对话");
        }
        bindSession();
    }

    /** 会话面板绑定当前汇报人（按人 + 天组织，默认当天） */
    private void bindSession() {
        Employee e = EmployeeStore.current(this, EmployeeStore.load(this));
        String id = e == null ? Briefing.SECRETARY_ID : e.id;
        String name = e == null || e.name == null ? "小秘书" : e.name;
        if (sessionTeaser != null) sessionTeaser.bind(id, name);
        if (sessionPanel != null) sessionPanel.bind(id, name);
    }

    /** 整页会话头部的小挂断按钮 */
    private void hangupFromSession() {
        if (VoiceCallService.isActive()) {
            ringing = false;
            tone.stop();
            VoiceCallService.stop(this);
            toast("已结束通话");
            applyCallState();
            renderCallUi();
        }
        if (sessionPanel != null) sessionPanel.setVisibility(View.GONE);
    }

    /** 打开完整会话面板（首页内嵌卡右上角「打开」或通话接通时） */
    private void openSessionSheet() {
        if (sessionPanel == null) return;
        bindSession();
        sessionPanel.setVisibility(View.VISIBLE);
        sessionPanel.openFull();
    }

    private static void setGone(View v, boolean gone) {
        if (v == null) return;
        int want = gone ? View.GONE : View.VISIBLE;
        if (v.getVisibility() != want) v.setVisibility(want);
    }

    /** 主按钮 / 通话条 / 状态 / 转写 一次刷新（接通后每 100ms 走一次，做音量动态） */
    private void renderCallUi() {
        boolean active = VoiceCallService.isActive();
        boolean connected = active && VoiceCallService.isConnected();
        String name = reporterName();

        // 接通后整块收紧：大圆钮、光环、提示文案收起，只留「时长 · 音量 · 挂断」
        if (heroCall != null) {
            int want = connected ? heroCompactHeight : heroBigHeight;
            ViewGroup.LayoutParams lp = heroCall.getLayoutParams();
            if (lp.height != want) {
                lp.height = want;
                heroCall.requestLayout();
            }
        }
        boolean connecting = active && !connected;

        // 接通的那一下：停拨号音、放接通提示音、闪一条「已接通」
        if (connected && !wasConnected) {
            wasConnected = true;
            if (ringing) {
                ringing = false;
                tone.stop();
                tone.playConnected();
                showNotice("已接通", 1600);
            }
        } else if (!connected) {
            wasConnected = false;
        }
        if (!active) {
            wasConnected = false;
            // 服务是 startForegroundService 异步拉起的，紧接着这一帧 isActive() 必然还是 false。
            // 这里给 3 秒宽限，避免「一点通话就把拨号音掐掉」；超时还没起来才算失败。
            boolean starting = ringing
                    && System.currentTimeMillis() - callRequestedAt < 3000;
            if (!starting) {
                ringing = false;
                tone.stop();
            }
        }

        // 接通中不显示任何提示文字，只留动画；接通后收成一条
        setGone(tvCallHint, active);
        setGone(tvCallBtn, active);
        setGone(micGlow, connected);
        setGone(btnCall, connected);
        setGone(connectRing, !connecting);
        setGone(llCallBar, !connected);
        startConnectAnim(connecting);

        if (levelView != null) {
            if (connected) {
                levelView.setActive(true);
                // 以麦克风电平为主；没人说话时给一点很轻的呼吸，避免整排死平
                // 自己的电平 + 员工侧电平（服务端就绪后自动生效，现在是 0）
                float mic = Math.max(VoiceCallService.micLevel(),
                        VoiceCallService.remoteLevel());
                float idle = VoiceCallService.isSpeaking() ? 0f
                        : (float) (0.05 + 0.04 * Math.sin(System.currentTimeMillis() / 320.0));
                levelView.setLevel(Math.max(mic, idle));
            } else {
                levelView.setActive(false);
            }
        }
        float liveLevel = Math.max(VoiceCallService.micLevel(), VoiceCallService.remoteLevel());
        String dur = "00:00";
        if (connected && VoiceCallService.connectedAt() > 0) {
            long sec = Math.max(0,
                    (System.currentTimeMillis() - VoiceCallService.connectedAt()) / 1000);
            dur = String.format(Locale.CHINA, "%02d:%02d", sec / 60, sec % 60);
            if (tvCallTimer != null && !dur.contentEquals(tvCallTimer.getText())) {
                tvCallTimer.setText(dur);
            }
        }
        if (sessionPanel != null) {
            sessionPanel.setCallLive(connected);
            sessionPanel.setCallStatus(dur, liveLevel);
        }

        if (tvCallBtn != null) tvCallBtn.setText("通话");
        if (btnCall != null) {
            btnCall.setBackgroundResource(connected ? R.drawable.bg_call_end : R.drawable.bg_mic);
        }
        if (sessionPanel != null) {
            sessionPanel.setStatus(active ? VoiceCallService.caption() : "");
        }
        if (connected && !wasExpandedForCall) {
            wasExpandedForCall = true;
            openSessionSheet();
        } else if (!connected) {
            wasExpandedForCall = false;
        }
    }

    /** 顶部瞬时提示（已接通 / 连接失败），到时自动隐藏 */
    private void showNotice(String text, long ms) {
        if (tvCallNotice == null) return;
        tvCallNotice.setText(text);
        tvCallNotice.setAlpha(1f);
        tvCallNotice.setVisibility(View.VISIBLE);
        callTick.removeCallbacks(noticeHide);
        callTick.postDelayed(noticeHide, ms);
    }

    /** 接通中：大圆钮呼吸，配合旋转的进度环表示「正在连接」 */
    private void startConnectAnim(boolean on) {
        if (btnCall == null) return;
        if (on) {
            if (connectAnim == null) {
                connectAnim = ObjectAnimator.ofFloat(btnCall, "alpha", 0.45f, 1f);
                connectAnim.setDuration(620);
                connectAnim.setRepeatMode(ValueAnimator.REVERSE);
                connectAnim.setRepeatCount(ValueAnimator.INFINITE);
            }
            if (!connectAnim.isStarted()) connectAnim.start();
        } else {
            if (connectAnim != null && connectAnim.isStarted()) connectAnim.cancel();
            btnCall.setAlpha(1f);
        }
    }

    // ------------------------------------------------------------------ 通话图标

    private final Handler callTick = new Handler(Looper.getMainLooper());

    private final Runnable callTickRunnable = new Runnable() {
        @Override
        public void run() {
            applyCallState();
            renderCallUi();
            callTick.postDelayed(this, VoiceCallService.isConnected() ? 100 : 800);
        }
    };

    /** 接通中/已连接：图标变红，再点就是挂断 */
    private void applyCallState() {
        if (isFinishing()) return;
        boolean active = VoiceCallService.isActive();
        ScreenOn.keep(this, active);      // 通话中（含接通中）别自动息屏
        boolean connected = VoiceCallService.isConnected();
        int color = getResources().getColor(active ? R.color.call_red : R.color.primary,
                getTheme());
        if (ivVoiceCall != null) {
            ivVoiceCall.setImageTintList(ColorStateList.valueOf(color));
            ivVoiceCall.setAlpha(active ? 1f : 0.9f);
        }
        if (btnVoiceCall != null) {
            btnVoiceCall.setBackgroundResource(active
                    ? R.drawable.bg_call_active : R.drawable.bg_icon_button);
        }
        if (active) {
            tvBadge.setText(connected ? "通话中" : "接通中");
        } else if (tvBadge.getText().toString().startsWith("通话")
                || tvBadge.getText().toString().startsWith("接通")) {
            refreshEndpointLabel();      // 挂断后恢复成普通的就绪/监听徽章
        }
    }

    /** 回到主界面时恢复最新一条历史结果（后台监听收到的结果也已写入历史） */
    private void restoreLastResult() {
        List<HistoryDb.Record> all = HistoryDb.get(this).list(1);
        if (all.isEmpty()) return;
        HistoryDb.Record rec = all.get(0);
        if (rec.answer == null || rec.answer.isEmpty()) return;
        McpResult r = new McpResult();
        r.coreText = rec.answer;
        r.answerText = rec.answer;
        r.displayText = rec.detail.isEmpty() ? rec.answer : rec.answer + "\n\n" + rec.detail;
        r.questionText = rec.question;
        r.status = rec.status;
        r.segmentsJson = rec.segmentsJson;
        r.avatarId = rec.avatarId;
        r.photoUrl = rec.photoUrl;
        r.videoUrl = rec.videoUrl;
        r.files.addAll(HistoryDb.jsonToFiles(rec.filesJson));
        lastResult = r;
        tvResult.setText(r.coreText);
        renderAttachments(r);
    }

    /** 主界面顶部展示当前汇报人；服务端素材不可访问时自动回退内置数字人 */
    /** 顶部副标题：数字员工 N 名（与徽章同处一行，不占额外高度） */
    private void refreshHeaderCount() {
        if (tvHomeSub == null) return;
        int n = 0;
        for (Employee e : EmployeeStore.load(this)) {
            if (!e.isBoss()) n++;      // 老板单独一栏，不计入员工数
        }
        tvHomeSub.setText(n > 0 ? "数字员工 " + n + " 名" : "数字员工");
    }

    private void refreshEmployeeCard() {
        refreshHeaderCount();
        Employee e = EmployeeStore.current(this, EmployeeStore.load(this));
        if (e == null) return;
        tvEmpName.setText(e.name);
        String role = e.title.isEmpty() ? e.role : e.title;
        tvEmpTitle.setText(role + " · " + e.dept);
        applyReporterLabels();          // 提示文案与转写称呼跟着当前汇报人走
        showBuiltInAvatar(e);
        EmployeeStore.applyPhoto(this, e, ivMiniPhoto,
                () -> {
                    ivMiniPhoto.setVisibility(View.VISIBLE);
                    dhvMini.setVisibility(View.GONE);
                },
                () -> showBuiltInAvatar(e));
    }

    private void showBuiltInAvatar(Employee e) {
        dhvMini.setVisibility(View.VISIBLE);
        ivMiniPhoto.setVisibility(View.GONE);
        dhvMini.setEmployee(e);
        dhvMini.setStaticPose(true);
    }

    private void refreshListenState() {
        if (tvListenState == null) return;
        if (cfg.demo) {
            tvListenState.setText("演示模式下不监听");
            return;
        }
        if (!cfg.keepListening) {
            tvListenState.setText("已关闭 · 只在使用时接收结果");
            return;
        }
        int pending = ListenService.pendingCount(this);
        String state = ListenService.isRunning(this) ? "监听中" : "正在启动…";
        tvListenState.setText(state + " · 待处理 " + pending + " 个任务 · 收到消息自动播报");
    }

    @Override
    protected void onDestroy() {
        super.onDestroy();
        ScreenOn.keep(this, false);
        tone.stop();
        tone.restoreVolumeIfBoosted();
        startConnectAnim(false);
        stopPulse();
        if (recording && recorder != null) recorder.stop();
        player.release();
        executor.shutdownNow();
        // 后台监听由用户开关控制，Activity 退出不停止
    }

    private void refreshEndpointLabel() {
        tvEndpoint.setText("端点：" + cfg.summary());
        tvBadge.setText(cfg.demo ? "演示模式" : (cfg.asyncMode ? "异步监听" : "已就绪"));
    }

    /** LocalSend 风格：按压缩放反馈 */
    private void applyPressEffect(View v) {
        v.setOnTouchListener((view, event) -> {
            if (event.getAction() == MotionEvent.ACTION_DOWN) {
                view.animate().scaleX(0.95f).scaleY(0.95f).setDuration(90).start();
            } else if (event.getAction() == MotionEvent.ACTION_UP
                    || event.getAction() == MotionEvent.ACTION_CANCEL) {
                view.animate().scaleX(1f).scaleY(1f).setDuration(160).start();
            }
            return false;
        });
    }

    /** 录音时的呼吸光环 */
    private void startPulse() {
        stopPulse();
        pulseAnimator = ObjectAnimator.ofPropertyValuesHolder(micGlow,
                PropertyValuesHolder.ofFloat(View.SCALE_X, 1f, 1.22f),
                PropertyValuesHolder.ofFloat(View.SCALE_Y, 1f, 1.22f),
                PropertyValuesHolder.ofFloat(View.ALPHA, 0.9f, 0f));
        pulseAnimator.setDuration(1500);
        pulseAnimator.setRepeatCount(ValueAnimator.INFINITE);
        pulseAnimator.start();
    }

    private void stopPulse() {
        if (pulseAnimator != null) {
            pulseAnimator.cancel();
            pulseAnimator = null;
        }
        if (micGlow != null) {
            micGlow.setScaleX(1f);
            micGlow.setScaleY(1f);
            micGlow.setAlpha(1f);
        }
    }

    private void ensurePermissions() {
        List<String> need = new ArrayList<>();
        if (checkSelfPermission(Manifest.permission.RECORD_AUDIO) != PackageManager.PERMISSION_GRANTED) {
            need.add(Manifest.permission.RECORD_AUDIO);
        }
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU
                && checkSelfPermission(Manifest.permission.POST_NOTIFICATIONS)
                != PackageManager.PERMISSION_GRANTED) {
            need.add(Manifest.permission.POST_NOTIFICATIONS);
        }
        if (!need.isEmpty()) {
            requestPermissions(need.toArray(new String[0]), REQ_PERMISSIONS);
        }
    }

    @Override
    public void onRequestPermissionsResult(int requestCode, String[] permissions, int[] results) {
        super.onRequestPermissionsResult(requestCode, permissions, results);
        if (requestCode == REQ_CALL) {
            if (results.length > 0 && results[0] == PackageManager.PERMISSION_GRANTED) {
                toggleCall();
            } else {
                toast("通话需要麦克风权限，请允许后重试");
            }
            return;
        }
        if (requestCode != REQ_PERMISSIONS) return;
        for (int i = 0; i < permissions.length; i++) {
            boolean granted = i < results.length && results[i] == PackageManager.PERMISSION_GRANTED;
            if (!granted && Manifest.permission.RECORD_AUDIO.equals(permissions[i])) {
                toast("没有录音权限，无法接收语音指令");
            } else if (!granted && Manifest.permission.POST_NOTIFICATIONS.equals(permissions[i])) {
                toast("没有通知权限，后台监听仍可用但不会弹出提醒");
            }
        }
    }

    private void startRecording() {
        if (checkSelfPermission(Manifest.permission.RECORD_AUDIO) != PackageManager.PERMISSION_GRANTED) {
            ensurePermissions();
            toast("请先授予录音权限");
            return;
        }
        File target = new File(getCacheDir(), "cmd_" + System.currentTimeMillis() + ".wav");
        recorder = new WavRecorder();
        try {
            recorder.start(target);
            recording = true;
            btnRecord.setSelected(true);
            tvRecordHint.setText("录音中 · 点击发送");
            startPulse();
            setStatus("录音中…（最长 60 秒）");
        } catch (Exception e) {
            recorder = null;
            toast("无法开始录音：" + e.getMessage());
        }
    }

    private void stopAndSend() {
        File wav = recorder == null ? null : recorder.stop();
        recorder = null;
        recording = false;
        btnRecord.setSelected(false);
        stopPulse();
        if (wav == null || wav.length() < 2000) {
            toast("录音太短，请再说一次");
            tvRecordHint.setText("点击说话");
            setStatus("准备就绪");
            return;
        }

        final McpConfig snapshot = cfg;
        final String audioBase64;
        try {
            audioBase64 = Base64.encodeToString(readAllBytes(wav), Base64.NO_WRAP);
        } catch (IOException e) {
            toast("读取录音失败：" + e.getMessage());
            return;
        }

        busy = true;
        btnRecord.setEnabled(false);
        progressRing.setVisibility(View.VISIBLE);
        tvRecordHint.setText("发送中…");
        setStatus("已发送，等待服务端回执…");
        tvResult.setText("请求中，请稍候…");

        executor.execute(() -> {
            McpResult result = doRequest(snapshot, audioBase64);
            ui.post(() -> {
                busy = false;
                btnRecord.setEnabled(true);
                progressRing.setVisibility(View.GONE);
                tvRecordHint.setText("点击说话");
                showResult(result);
            });
        });
    }

    private McpResult doRequest(McpConfig config, String audioBase64) {
        try {
            if (config.demo) {
                return ResultParser.parse(this, demoResult());
            }
            McpClient local = client;
            if (local == null) {
                local = new McpClient(config);
                client = local;
            }
            JSONObject args = new JSONObject();
            args.put(config.audioKey, audioBase64);
            args.put("language", config.language);
            args.put("model", config.model);
            // v2.1.0：音色与人设交给服务端按员工档案 / 语音点名决定，只有显式覆盖才传
            if (!config.voice.isEmpty()) args.put("voice", config.voice);
            if (!config.profile.isEmpty()) args.put("profile", config.profile);
            String avatar = config.avatarId.isEmpty() ? currentEmployeeId() : config.avatarId;
            if (!avatar.isEmpty()) args.put("avatar_id", avatar);
            args.put("mode", config.validMode());
            args.put("bandwidth_kbps", config.effectiveBandwidthKbps(this));
            args.put("delegate", config.delegate);
            args.put("timeout_sec", Math.min(config.timeoutSec, 300));
            // 异步：立即回「收到」+ job_id，结果由 ListenService 长轮询取回
            args.put("wait", !config.asyncMode);
            if (!config.asyncMode) args.put("wait_sec", Math.min(config.timeoutSec, 240));
            args.put("include_audio", true);

            JSONObject result = local.callTool(config.tool, args);
            return ResultParser.parse(this, result);
        } catch (Exception e) {
            if (client != null) client.invalidate();
            return McpResult.error("调用失败：" + e.getMessage());
        }
    }

    /** 当前汇报人在服务端档案里的 id（ceo / pm_wenren …），用作 avatar_id */
    private String currentEmployeeId() {
        Employee e = EmployeeStore.current(this, EmployeeStore.load(this));
        return e == null ? "" : e.id;
    }

    private void showResult(McpResult result) {
        lastResult = result;
        tvResult.setAlpha(0f);
        tvResult.setText(result.bestText());
        tvResult.animate().alpha(1f).setDuration(240).start();
        renderAttachments(result);
        PushProcessor.save(this, result);      // 回执先落一条历史，结果到达后原地更新

        boolean asyncDone = cfg.asyncMode && !result.jobId.isEmpty()
                && !"done".equalsIgnoreCase(result.status);
        if (asyncDone) {
            ListenService.addJob(this, result.jobId);
            applyServerAvatar(result);   // 服务端点名派单：立即切换汇报员工形象
            setStatus("已收到，正在后台处理…");
            tvResult.setText(result.bestText().isEmpty()
                    ? "已收到指令，正在后台处理，完成后自动播报" : result.bestText());
            if (cfg.autoPlay) playAck(result);
            refreshListenState();
        } else {
            setStatus(result.isError ? "服务端返回了错误" : "完成");
            if (cfg.autoPlay) playResult(result);
        }
    }

    /**
     * v2.1.0：voice_chat 回执里的 avatar（语音点名派单时是 delegate）决定实际汇报员工。
     * 服务端决定由谁出面，App 立刻把顶部形象卡与当前汇报人切过去，后续结果也由他汇报。
     */
    private void applyServerAvatar(McpResult r) {
        String id = !r.avatarId.isEmpty() ? r.avatarId : r.delegateId;
        if (id.isEmpty()) return;
        List<Employee> all = EmployeeStore.load(this);
        boolean exists = false;
        for (Employee e : all) {
            if (e.id.equals(id)) { exists = true; break; }
        }
        if (!exists) return;           // 服务端档案还没同步到这个人时不误切
        EmployeeStore.setCurrent(this, id);
        refreshEmployeeCard();
    }

    /** 异步回执的「老板，稍等」语音，只播这一句，不弹汇报画面 */
    private void playAck(McpResult result) {
        McpResult.Item audio = result.audioItem();
        if (audio != null && audio.path != null) {
            player.playFile(audio.path, ok -> { });
        } else if (!result.ackText.isEmpty()) {
            player.speak(result.ackText);
        }
    }

    private void playResult(McpResult result) {
        if (cfg.reportEnabled) {
            launchReport(result);
            return;
        }
        McpResult.Item audio = result.audioItem();
        if (audio != null && audio.path != null) {
            player.playFile(audio.path, ok -> ui.post(() -> {
                if (!ok) {
                    setStatus("音频播放失败，改用系统朗读");
                    player.speak(result.bestText());
                } else {
                    setStatus("正在播报结果语音");
                }
            }));
        } else {
            player.speak(result.bestText());
            setStatus("服务端未返回音频，已用系统朗读");
        }
    }

    /** 结果返回后拉起数字员工汇报画面：文字做字幕、WAV 做音源与口型驱动 */
    private void launchReport(McpResult result) {
        Intent i = new Intent(this, ReportActivity.class);
        i.putExtra(ReportActivity.EXTRA_ANSWER,
                result.answerText.isEmpty() ? result.bestText() : result.answerText);
        i.putExtra(ReportActivity.EXTRA_QUESTION, result.questionText);
        McpResult.Item audio = result.audioItem();
        if (audio != null && audio.path != null) {
            i.putExtra(ReportActivity.EXTRA_AUDIO, audio.path);
        }
        i.putExtra(ReportActivity.EXTRA_IS_ERROR, result.isError);
        String empId = !result.avatarId.isEmpty() ? result.avatarId : currentEmployeeId();
        if (!empId.isEmpty()) i.putExtra(ReportActivity.EXTRA_EMPLOYEE, empId);
        if (!result.employeeName.isEmpty()) {
            i.putExtra(ReportActivity.EXTRA_EMPLOYEE_NAME, result.employeeName);
        }
        if (!result.segmentsJson.isEmpty()) {
            i.putExtra(ReportActivity.EXTRA_SEGMENTS, result.segmentsJson);
        }
        if (!result.photoUrl.isEmpty()) i.putExtra(ReportActivity.EXTRA_PHOTO, result.photoUrl);
        if (!result.videoUrl.isEmpty()) i.putExtra(ReportActivity.EXTRA_VIDEO, result.videoUrl);
        if (!result.faceUrl.isEmpty()) i.putExtra(ReportActivity.EXTRA_FACE, result.faceUrl);
        try {
            startActivity(i);
            setStatus("数字员工汇报中");
        } catch (Exception e) {
            toast("无法打开汇报画面：" + e.getMessage());
        }
    }

    /** 附件：只显示语义标签的图标链接，不显示文件名 */
    private void renderAttachments(McpResult result) {
        llAttachments.removeAllViews();
        if (result == null || result.files.isEmpty()) {
            llAttachments.addView(tvNoAttachment);
            return;
        }
        LayoutInflater inflater = LayoutInflater.from(this);
        for (McpResult.Item item : result.files) {
            if (item.uri == null) continue;
            View row = inflater.inflate(R.layout.item_attachment, llAttachments, false);
            TextView name = row.findViewById(R.id.tvFileName);
            TextView meta = row.findViewById(R.id.tvFileMeta);
            ImageView icon = row.findViewById(R.id.ivFileIcon);
            String label = item.label.isEmpty() ? ResultStore.labelFor(item.mime) : item.label;
            name.setText(label);
            meta.setText("点击查看");
            if (item.mime != null && item.mime.startsWith("audio")) {
                icon.setImageResource(R.drawable.ic_audio);
            } else if (item.mime != null && item.mime.startsWith("video")) {
                icon.setImageResource(R.drawable.ic_video);
            } else if (item.mime != null && item.mime.startsWith("text")) {
                icon.setImageResource(R.drawable.ic_text);
            } else {
                icon.setImageResource(R.drawable.ic_file);
            }
            row.setOnClickListener(v -> openFile(item));
            llAttachments.addView(row);
        }
        if (llAttachments.getChildCount() == 0) llAttachments.addView(tvNoAttachment);
    }

    private void openTextResult() {
        if (lastResult == null) {
            toast("还没有结果");
            return;
        }
        McpResult.Item existing = lastResult.textItem();
        if (existing != null && existing.uri != null) {
            openFile(existing);
            return;
        }
        ResultStore.Saved saved = ResultStore.savePermanent(this, "核心内容.txt",
                lastResult.bestText().getBytes(StandardCharsets.UTF_8), "text/plain");
        if (saved != null) {
            McpResult.Item item = new McpResult.Item(saved.name, saved.mime,
                    saved.file.getAbsolutePath(), saved.uri, saved.file.length());
            item.label = "文字详情";
            openFile(item);
        }
    }

    private void openFile(McpResult.Item item) {
        Intent view = new Intent(Intent.ACTION_VIEW);
        view.setDataAndType(item.uri, item.mime);
        view.addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION);
        Intent chooser = Intent.createChooser(view, "打开结果");
        chooser.addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION);
        try {
            startActivity(chooser);
        } catch (Exception e) {
            toast("没有可以打开 " + item.mime + " 的应用");
        }
    }

    private JSONObject demoResult() throws JSONException {
        JSONObject payload = new JSONObject();
        payload.put("question", "演示指令：查看结果");
        payload.put("answer", "演示模式：未连接服务器。这里模拟服务端返回的文字结果。\n"
                + "点「文字」或下方的图标链接，即可用系统应用打开查看。");
        JSONArray content = new JSONArray();
        content.put(new JSONObject().put("type", "text").put("text", payload.toString()));
        JSONObject resource = new JSONObject();
        resource.put("uri", "file:///demo_note.txt");
        resource.put("mimeType", "text/plain");
        resource.put("text", "演示附件\n这是返回文件时，App 落盘后打开查看的效果。");
        content.put(new JSONObject().put("type", "resource").put("resource", resource));
        JSONObject result = new JSONObject();
        result.put("content", content);
        result.put("isError", false);
        return result;
    }

    private static byte[] readAllBytes(File file) throws IOException {
        byte[] data = new byte[(int) file.length()];
        try (FileInputStream in = new FileInputStream(file)) {
            int offset = 0;
            int n;
            while (offset < data.length && (n = in.read(data, offset, data.length - offset)) > 0) {
                offset += n;
            }
        }
        return data;
    }

    /** 状态点颜色随阶段变化：正常 / 进行中 / 错误 */
    private void setStatus(String text) {
        tvStatus.setText(text);
        int drawable = R.drawable.bg_dot_ok;
        if (text.contains("错误") || text.contains("失败")) {
            drawable = R.drawable.bg_dot_error;
        } else if (text.contains("录音中") || text.contains("已发送")
                || text.contains("正在") || text.contains("请求中")) {
            drawable = R.drawable.bg_dot_busy;
        }
        statusDot.setBackgroundResource(drawable);
    }

    private void toast(String text) {
        Toast.makeText(this, text, Toast.LENGTH_SHORT).show();
    }
}
