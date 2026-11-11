package com.boray.voicemcp;

import android.app.Activity;
import android.content.Intent;
import android.graphics.Bitmap;
import android.os.Bundle;
import android.util.Log;
import android.view.View;
import android.widget.Button;
import android.widget.ImageView;
import android.widget.ProgressBar;
import android.widget.TextView;
import android.widget.Toast;

import org.json.JSONObject;

import java.util.Locale;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;

/**
 * 个人档案 · 自我介绍全屏播放（server 端生成，客户端只负责流式播放）。
 *
 * server v2.5：profile.introVideo = /conring/asr/assets/intro/&lt;id&gt;.mp4（720×960，
 * avc1+mp4a，音画合一，支持 Range）；introReady / introText / introDuration 同返回；
 * 工具 intro_video{id, force, wait} 可按需生成并缓存。
 *
 * 本页只做三件事：自动播放、与服务端对齐最新视频、播不了时明确说明并给降级入口。
 * 播放用 CoverVideoView 铺满裁切（竖版视频不再留大黑边）；任何异常都**不再静默关页**，
 * 直接在页面上显示原因，便于判断是网络、地址还是素材问题。
 */
public class IntroPlayerActivity extends Activity {

    public static final String EXTRA_EMPLOYEE = "employee_id";
    private static final String TAG = "IntroPlayer";

    private Employee emp;
    private StreamPlayback playback;
    private ExecutorService io;

    private CoverVideoView videoStage;
    private ImageView ivPoster;
    private TextView tvState;
    private TextView tvCaption;
    private TextView tvSource;
    private TextView tvTime;
    private TextView tvNotice;
    private TextView tvStreamTag;
    private ProgressBar pbProgress;
    private Button btnFallback;
    private boolean fallbackBusy = false;
    private boolean ready = false;
    private boolean syncStarted = false;

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        try {
            setContentView(R.layout.activity_intro_player);
            WindowInsetsHelper.apply(this);
            bindViews();
        } catch (Throwable t) {
            // 布局/主题级异常也要留下痕迹，而不是让页面直接消失（用户反馈过「点进去就返回」）
            Log.e(TAG, "init failed", t);
            Toast.makeText(this, "页面初始化失败：" + t.getClass().getSimpleName(),
                    Toast.LENGTH_LONG).show();
        }
    }

    private void bindViews() {
        emp = EmployeeStore.byId(EmployeeStore.load(this),
                getIntent().getStringExtra(EXTRA_EMPLOYEE));
        if (emp == null) {
            Log.w(TAG, "employee not found: " + getIntent().getStringExtra(EXTRA_EMPLOYEE));
            Toast.makeText(this, "没有找到该员工档案", Toast.LENGTH_LONG).show();
            finish();
            return;
        }

        io = Executors.newSingleThreadExecutor();

        View back = findViewById(R.id.btnBack);
        if (back != null) back.setOnClickListener(v -> finish());
        Button btnClose = findViewById(R.id.btnClose);
        if (btnClose != null) btnClose.setOnClickListener(v -> finish());
        Button btnReplay = findViewById(R.id.btnReplay);
        btnFallback = findViewById(R.id.btnFallback);

        videoStage = findViewById(R.id.videoStage);
        ivPoster = findViewById(R.id.ivPoster);
        tvState = findViewById(R.id.tvState);
        tvCaption = findViewById(R.id.tvCaption);
        tvSource = findViewById(R.id.tvSource);
        tvTime = findViewById(R.id.tvTime);
        tvNotice = findViewById(R.id.tvNotice);
        tvStreamTag = findViewById(R.id.tvStreamTag);
        pbProgress = findViewById(R.id.pbProgress);

        ((TextView) findViewById(R.id.tvEmpName)).setText(emp.name + " · " + emp.title);
        if (tvCaption != null) tvCaption.setText(emp.introCaption());

        loadPoster();

        if (btnReplay != null) {
            btnReplay.setOnClickListener(v -> {
                if (playback != null && !playback.url().isEmpty() && ready) {
                    ivPoster.setVisibility(View.GONE);
                    playback.replay();
                } else {
                    startStreamOrGenerate();
                }
            });
        }
        if (btnFallback != null) {
            btnFallback.setOnClickListener(v -> {
                if (!ready) generateThenPlay();
                else localPreview();
            });
        }

        playback = new StreamPlayback();
        startStreamOrGenerate();
        syncLatest();
    }

    // ---------------------------------------------------------------- 首帧照片

    private void loadPoster() {
        String poster = emp.introPoster != null && !emp.introPoster.trim().isEmpty()
                ? emp.introPoster : (emp.photo.isEmpty() ? emp.faceUrl : emp.photo);
        EmployeeStore.loadPhotoBitmap(this, poster, new EmployeeStore.BitmapCallback() {
            @Override
            public void onLoaded(Bitmap bmp) {
                if (isFinishing()) return;
                if (bmp != null) ivPoster.setImageBitmap(bmp);
                else ivPoster.setImageResource(R.drawable.ic_people);
            }
        });
    }

    // ---------------------------------------------------------------- 有地址就播

    private void startStreamOrGenerate() {
        if (playback == null) return;
        McpConfig cfg = McpConfig.load(this);
        String url = StreamContract.resolveStream(
                cfg.url, AssetUrls.serverBase(this), emp.introVideo);
        if (url.isEmpty()) {
            ready = false;
            if (!emp.introVideo.isEmpty()) {
                state("流地址非 MCP 同源，已拒绝");
                notice("客户端只使用服务端返回的同源地址，不会改连其它 URL。");
                tvStreamTag.setText("地址已拒绝");
            } else {
                state("服务端尚未生成自我介绍视频");
                tvStreamTag.setText("待生成");
                notice("点下方「生成自我介绍」让服务端生成：大模型润色文案 + TTS 语音 + 画面，"
                        + "合成后缓存在员工档案里，之后点开即播。");
            }
            btnFallback.setText("生成自我介绍");
            videoStage.setVisibility(View.GONE);
            ivPoster.setVisibility(View.VISIBLE);
            tvSource.setText("服务端档案暂无介绍视频");
            return;
        }

        boolean hls = StreamContract.isHls(url);
        tvStreamTag.setText(hls ? "HLS 流式" : "MP4 流式");
        String dur = emp.introDuration > 0
                ? String.format(Locale.CHINA, "%.1fs", emp.introDuration) : "";
        tvSource.setText("server 端生成" + (dur.isEmpty() ? "" : " · " + dur)
                + " · " + (hls ? "HLS VOD 分片" : "MP4 Range 边下边播") + " · 音轨随片");
        btnFallback.setText("听本机合成");
        notice("视频与语音都由 server 端合成后放在员工档案里，客户端按同源地址边下边播。");
        state("正在连接同源视频…");
        videoStage.setVisibility(View.VISIBLE);
        ready = true;

        String playUrl = StreamContract.versioned(url, emp.introDuration, emp.introText);
        boolean sound = McpConfig.load(this).voiceEnabled;
        if (!sound) {
            notice("语音控制已关闭：视频静音播放（顶栏喇叭图标可开启）。");
        }
        playback.play(videoStage, playUrl, StreamPlayback.Mode.VOD, sound,
                new StreamPlayback.Listener() {
                    @Override
                    public void onWaiting(String hint) {
                        state(hint);
                    }

                    @Override
                    public void onPreparing(int attempt) {
                        state(attempt == 0 ? "正在缓冲视频…" : "重试第 " + attempt + " 次…");
                    }

                    @Override
                    public void onReady(int durationMs) {
                        ivPoster.setVisibility(View.GONE);
                        state(sound ? "播放中 · 服务端视频自带语音"
                                : "播放中 · 已静音（语音控制关闭）");
                    }

                    @Override
                    public void onBuffering(boolean busy) {
                        if (busy) state("缓冲中…");
                    }

                    @Override
                    public void onProgress(int positionMs, int durationMs) {
                        if (durationMs > 0) {
                            pbProgress.setProgress(Math.min(100, positionMs * 100 / durationMs));
                        }
                        tvTime.setText(time(positionMs) + " / " + time(durationMs));
                    }

                    @Override
                    public void onCompleted() {
                        pbProgress.setProgress(100);
                        state("播放完毕 · 可点重播");
                    }

                    @Override
                    public void onRetry(int attempt, String reason) {
                        state("断流重试 " + attempt + "…");
                    }

                    @Override
                    public void onFailed(String reason) {
                        ready = false;
                        state("视频播放失败：" + reason);
                        notice("服务端素材暂不可访问，已保留照片形象。可点「生成自我介绍」重试，"
                                + "或听本机合成版本。");
                        videoStage.setVisibility(View.GONE);
                        ivPoster.setVisibility(View.VISIBLE);
                        btnFallback.setText("生成自我介绍");
                    }
                });
    }

    // ---------------------------------------------------------------- 按需生成

    /** 调 intro_video{id, wait:true} 让服务端现生成（已缓存时秒回），成功后直接播 */
    private void generateThenPlay() {
        if (fallbackBusy || io == null) return;
        fallbackBusy = true;
        btnFallback.setEnabled(false);
        btnFallback.setText("服务端生成中…");
        state("正在让服务端生成自我介绍（文案 + TTS + 画面合成）…");
        notice("首次生成约十几秒；生成后缓存在员工档案 assets/intro/" + emp.id
                + ".mp4，之后所有客户端点开即播。");

        io.execute(() -> {
            StreamContract.IntroClip clip = null;
            String error = "";
            try {
                McpConfig cfg = McpConfig.load(this).copy();
                McpClient client = new McpClient(cfg);
                JSONObject args = new JSONObject();
                args.put("id", emp.id);
                args.put("force", false);
                args.put("wait", true);
                JSONObject res = client.callTool(StreamContract.TOOL_INTRO_VIDEO, args);
                clip = StreamContract.parseIntro(cfg.url, AssetUrls.serverBase(this),
                        StreamContract.textOf(res), emp.id);
            } catch (Exception e) {
                error = e.getMessage() == null ? "生成失败" : e.getMessage();
            }
            final StreamContract.IntroClip c = clip;
            final String err = error;
            runOnUiThread(() -> {
                if (isFinishing()) return;
                fallbackBusy = false;
                btnFallback.setEnabled(true);
                btnFallback.setText("生成自我介绍");
                if (c != null && !c.url.isEmpty()) {
                    emp.introVideo = c.url;
                    emp.introReady = true;
                    emp.hasIntro = true;
                    if (c.duration > 0) emp.introDuration = c.duration;
                    if (!c.text.isEmpty()) {
                        emp.introText = c.text;
                        tvCaption.setText(c.text);
                    }
                    Toast.makeText(this, c.cached ? "服务端已缓存，直接播放" : "生成完成，开始播放",
                            Toast.LENGTH_SHORT).show();
                    state("生成完成，开始播放");
                    startStreamOrGenerate();
                } else if (!err.isEmpty()) {
                    state("生成失败：" + shortErr(err));
                    notice("服务端未返回可用视频，可改用「本机合成试听」。");
                } else {
                    state("服务端未返回可用视频");
                    notice("intro_video 未给出同源视频地址，可先用本机合成试听。");
                }
            });
        });
    }

    // ---------------------------------------------------------------- 与服务端对齐最新

    /**
     * 进入页面后拉一次 profile：服务端若重新生成过介绍视频（时长/文案变化），
     * 立即换成新地址重播，保证看到的永远是最新那条。
     */
    private void syncLatest() {
        if (syncStarted) return;
        syncStarted = true;
        McpConfig cfg = McpConfig.load(this);
        if (cfg.demo) return;
        final String before = emp.introVideo + "|" + Math.round(emp.introDuration * 100);
        EmployeeStore.syncFromServer(this, cfg, (count, error) -> {
            if (isFinishing() || count <= 0) return;
            Employee fresh = EmployeeStore.byId(EmployeeStore.load(this), emp.id);
            if (fresh == null) return;
            String after = fresh.introVideo + "|" + Math.round(fresh.introDuration * 100);
            emp = fresh;
            loadPoster();
            if (!after.equals(before)) {
                state("服务端已更新介绍视频，正在重播…");
                startStreamOrGenerate();
            } else if (ready) {
                notice("已与服务端对齐：当前就是最新版本（"
                        + String.format(Locale.CHINA, "%.1fs", emp.introDuration) + "）。");
            }
        });
    }

    // ---------------------------------------------------------------- 降级试听

    /** 降级：真实 WAV + 汇报画面口型，形象沿用同一员工的照片 */
    private void localPreview() {
        if (fallbackBusy) return;
        fallbackBusy = true;
        btnFallback.setEnabled(false);
        btnFallback.setText("正在生成语音…");
        final String speech = emp.introCaption();
        PreviewAudio.synthesize(this, emp, speech, (wav, err) -> {
            if (isFinishing()) return;
            fallbackBusy = false;
            btnFallback.setEnabled(true);
            btnFallback.setText("听本机合成");
            if (err != null && !err.isEmpty() && (wav == null || wav.isEmpty())) {
                Toast.makeText(this, "服务端语音不可用，汇报画面会本机合成兜底",
                        Toast.LENGTH_SHORT).show();
            }
            Intent i = new Intent(this, ReportActivity.class);
            i.putExtra(ReportActivity.EXTRA_EMPLOYEE, emp.id);
            i.putExtra(ReportActivity.EXTRA_EMPLOYEE_NAME, emp.name);
            i.putExtra(ReportActivity.EXTRA_FACE, emp.faceUrl);
            i.putExtra(ReportActivity.EXTRA_PHOTO, emp.photo);
            i.putExtra(ReportActivity.EXTRA_VIDEO, emp.videoTalk);
            i.putExtra(ReportActivity.EXTRA_QUESTION, "自我介绍（本机合成）");
            i.putExtra(ReportActivity.EXTRA_ANSWER, speech);
            if (wav != null && !wav.isEmpty()) i.putExtra(ReportActivity.EXTRA_AUDIO, wav);
            startActivity(i);
        });
    }

    // ---------------------------------------------------------------- 小工具

    private void state(String s) {
        if (!isFinishing() && tvState != null) tvState.setText(s);
    }

    private void notice(String s) {
        if (!isFinishing() && tvNotice != null) tvNotice.setText(s);
    }

    private static String shortErr(String s) {
        return s.length() > 100 ? s.substring(0, 100) + "…" : s;
    }

    private static String time(int ms) {
        if (ms <= 0) return "00:00";
        int sec = ms / 1000;
        return String.format(Locale.CHINA, "%02d:%02d", sec / 60, sec % 60);
    }

    @Override
    protected void onResume() {
        super.onResume();
        if (playback != null && !playback.url().isEmpty()) playback.resume();
    }

    @Override
    protected void onPause() {
        super.onPause();
        if (playback != null) playback.pause();
    }

    @Override
    protected void onDestroy() {
        super.onDestroy();
        if (playback != null) playback.release();
        if (io != null) io.shutdownNow();
    }
}
