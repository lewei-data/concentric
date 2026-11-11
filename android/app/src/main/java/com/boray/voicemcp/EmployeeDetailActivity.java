package com.boray.voicemcp;

import android.app.Activity;
import android.content.Intent;
import android.graphics.Bitmap;
import android.media.MediaPlayer;
import android.net.Uri;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.view.View;
import android.view.ViewGroup;
import android.widget.Button;
import android.widget.ImageView;
import android.widget.TextView;
import android.widget.Toast;

import java.util.Collections;
import java.util.HashSet;
import java.util.List;
import java.util.Locale;
import java.util.Set;

/**
 * 单个数字员工档案。
 *
 * 首屏（舞台）播放该员工的服务端自我介绍视频（画面 + 语音，来自 profile.introVideo）。
 * 播放策略（0.5.2）：
 *   1. **只在本次运行里首次打开该员工时自动播放**；从别处切回来 / 再次进入不再自动播放，
 *      舞台停在首帧并显示播放按钮（进程级记忆，重启 App 后算新的首次）。
 *   2. 顶栏「语音控制」是全局开关并持久化：关闭后视频照常加载最新版并静音播放，
 *      任何场景都不出声（包括首次打开）。
 *   3. 进入页面时先播本地缓存的地址，同时后台拉 profile 与服务端对齐；
 *      视频/时长/文案变了就用带版本参数的新地址重播。
 *
 * 舞台降级链：自我介绍视频 → idle/talk 仿真人视频（静音循环）→ 内置 Canvas 数字人。
 */
public class EmployeeDetailActivity extends Activity {

    public static final String EXTRA_ID = "employee_id";

    private static final int HERO_INTRO = 0;
    private static final int HERO_VIDEO = 1;
    private static final int HERO_CANVAS = 2;

    /** 本次进程内已经自动播放过自我介绍的员工（切回来不再自动播） */
    private static final Set<String> AUTO_PLAYED =
            Collections.synchronizedSet(new HashSet<String>());

    private Employee emp;
    private DigitalHumanView dhvStage;
    private CoverVideoView videoStage;
    private ImageView ivIntroPoster;
    private ImageView ivVoice;
    private TextView tvIntroState;
    private TextView tvStageTag;
    private TextView tvVideoState;
    private View btnStageReplay;
    private View btnStagePlay;
    private Button btnSetCurrent;

    private int heroMode = HERO_CANVAS;
    private String playingUrl = "";
    private String playedSignature = "";
    private boolean syncStarted = false;
    private boolean voiceEnabled = true;
    private boolean userStarted = false;
    private MediaPlayer currentMp;

    private final Handler ui = new Handler(Looper.getMainLooper());

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        setContentView(R.layout.activity_employee_detail);
        WindowInsetsHelper.apply(this);

        List<Employee> all = EmployeeStore.load(this);
        String id = getIntent().getStringExtra(EXTRA_ID);
        emp = EmployeeStore.byId(all, id);
        if (emp == null) {
            Toast.makeText(this, "没有找到员工档案", Toast.LENGTH_SHORT).show();
            finish();
            return;
        }

        View back = findViewById(R.id.btnBack);
        if (back != null) back.setOnClickListener(v -> finish());

        TextView header = findViewById(R.id.tvHeader);
        TextView no = findViewById(R.id.tvNo);
        TextView name = findViewById(R.id.tvName);
        TextView title = findViewById(R.id.tvTitle);
        TextView motto = findViewById(R.id.tvMotto);
        TextView bio = findViewById(R.id.tvBio);
        TextView skills = findViewById(R.id.tvSkills);
        ImageView photo = findViewById(R.id.ivPhoto);
        dhvStage = findViewById(R.id.dhvStage);
        videoStage = findViewById(R.id.videoStage);
        ivIntroPoster = findViewById(R.id.ivIntroPoster);
        ivVoice = findViewById(R.id.ivVoice);
        tvIntroState = findViewById(R.id.tvIntroState);
        tvStageTag = findViewById(R.id.tvStageTag);
        tvVideoState = findViewById(R.id.tvVideoState);
        btnStageReplay = findViewById(R.id.btnStageReplay);
        btnStagePlay = findViewById(R.id.btnStagePlay);
        btnSetCurrent = findViewById(R.id.btnSetCurrent);

        // 老板是 App 的使用者本人，标题不叫「员工档案」
        header.setText(emp.isBoss() ? "老板档案" : "员工档案");
        no.setText(emp.no);
        name.setText(emp.name);
        title.setText(emp.title + " · " + emp.dept);
        motto.setText("「" + emp.motto + "」");
        bio.setText(emp.bio);
        skills.setText("擅长：" + join(emp.skills)
                + "\n" + emp.metaLine()
                + "\n项目：" + (emp.project.isEmpty() ? "—" : emp.project)
                + "\n角色：" + (emp.role.isEmpty() ? emp.title : emp.role)
                + (emp.profileReady ? "" : "\n注意：该员工尚无独立 Hermes profile，语音点名派单可能失败"));
        photo.setImageResource(R.drawable.ic_people);
        EmployeeStore.applyPhoto(this, emp, photo,
                () -> { }, () -> photo.setImageResource(R.drawable.ic_people));

        dhvStage.setEmployee(emp);
        dhvStage.setAnimated(true);
        dhvStage.setFrame(0f, LipSync.SHAPE_CLOSED, emp.emotion, false);

        loadPoster();
        resizeHero();
        setupVoiceToggle();

        btnStageReplay.setOnClickListener(v -> replayByUser());
        if (btnStagePlay != null) btnStagePlay.setOnClickListener(v -> startByUser());
        videoStage.setOnClickListener(v -> togglePlayback());
        videoStage.setOnLongClickListener(v -> {
            // 长按舞台：全屏播放页（原「播放自我介绍」按钮的能力，不再占底部按钮位）
            Intent i = new Intent(this, IntroPlayerActivity.class);
            i.putExtra(IntroPlayerActivity.EXTRA_EMPLOYEE, emp.id);
            startActivity(i);
            return true;
        });

        refreshStreamState();
        refreshCurrentButton();

        Button btnCall = findViewById(R.id.btnCall);

        if (emp.isBoss()) {
            // 老板本人用这个 App：「发起实时通话」「设为当前汇报人」对他没有意义，直接不显示
            btnSetCurrent.setVisibility(View.GONE);
            btnCall.setVisibility(View.GONE);
        } else {
            btnSetCurrent.setOnClickListener(v -> {
                EmployeeStore.setCurrent(this, emp.id);
                refreshCurrentButton();
                Toast.makeText(this, emp.name + " 已成为当前汇报人", Toast.LENGTH_SHORT).show();
            });

            // 服务端 liveCall.supported=false 时按契约隐藏通话入口，避免点了才失败
            if (emp.liveSupported) {
                btnCall.setOnClickListener(v -> {
                    Intent i = new Intent(this, LiveCallActivity.class);
                    i.putExtra(LiveCallActivity.EXTRA_EMPLOYEE, emp.id);
                    startActivity(i);
                });
            } else {
                btnCall.setEnabled(false);
                btnCall.setAlpha(0.45f);
                btnCall.setText("实时通话未开放（服务端 liveCall.supported=false）");
            }
        }

        // 首次自动播放判定：本次运行内没给这名员工播过，才自动开播
        boolean firstTime = !AUTO_PLAYED.contains(emp.id);
        playHero(firstTime);
        syncLatest();
    }

    // ------------------------------------------------------------------ 语音控制开关

    private void setupVoiceToggle() {
        voiceEnabled = McpConfig.load(this).voiceEnabled;
        applyVoiceIcon();
        View btn = findViewById(R.id.btnVoice);
        if (btn == null) return;
        btn.setOnClickListener(v -> {
            McpConfig cfg = McpConfig.load(this).copy();
            cfg.voiceEnabled = !cfg.voiceEnabled;
            cfg.save(this);
            voiceEnabled = cfg.voiceEnabled;
            applyVoiceIcon();
            applyVolume();
            Toast.makeText(this, voiceEnabled
                            ? "语音控制已开启：首次进入档案会自动播放介绍语音"
                            : "语音控制已关闭：介绍视频静音播放，不再自动出声",
                    Toast.LENGTH_SHORT).show();
            if (heroMode == HERO_INTRO) {
                tvIntroState.setText(voiceEnabled
                        ? "语音控制已开启 · 点按画面可播放"
                        : "语音控制已关闭 · 视频静音播放");
            }
        });
    }

    private void applyVoiceIcon() {
        if (ivVoice == null) return;
        ivVoice.setImageResource(voiceEnabled ? R.drawable.ic_volume_up
                : R.drawable.ic_volume_off);
        ivVoice.setAlpha(voiceEnabled ? 1f : 0.75f);
    }

    /** 把当前开关状态应用到正在播的播放器（不重启视频） */
    private void applyVolume() {
        if (currentMp != null) {
            try {
                float v = voiceEnabled ? 1f : 0f;
                currentMp.setVolume(v, v);
            } catch (Exception ignored) {
            }
        }
    }

    // ------------------------------------------------------------------ 舞台

    /**
     * 按视频宽高比给舞台上定高度（舞台是全宽出血，宽度就是屏宽）。
     *
     * 这是「视频填满、边上不留白」的关键：VideoView 的 Surface 默认等比缩放并留边，
     * 只要容器比例和视频比例一致，画面就是 1:1 铺满，不存在左右露背景。
     * 之前舞台固定 3:4 宽（4:3 横比）+ 视频是 720×960 竖版，两者比例不等，
     * 于是左右各留一条空白，露出舞台渐变——就是看到的那圈背景。
     */
    private void fitStageTo(int videoW, int videoH) {
        View stage = findViewById(R.id.stage);
        if (stage == null) return;

        if (videoW > 0 && videoH > 0) {
            videoStage.setVideoSize(videoW, videoH);   // CoverVideoView 内部会去重
        }

        final int vw = videoW;
        final int vh = videoH;
        // 高度严格按视频比例来：容器比例 == 视频比例时，Surface 正好 1:1 铺满，
        // 既不裁切、也不会在旁边露出舞台背景（不留百分比上限，否则比例会被破坏）
        int w = stage.getWidth() > 0 ? stage.getWidth()
                : getResources().getDisplayMetrics().widthPixels;
        applyStageHeight(stage, vw, vh, w);

        // 首帧前 stage 还没量过（width=0）时用的是屏宽，布局完成后再按真实宽度校正一次
        stage.post(() -> {
            if (isFinishing()) return;
            int realW = stage.getWidth();
            if (realW > 0 && realW != w) applyStageHeight(stage, vw, vh, realW);
        });
    }

    private void applyStageHeight(View stage, int videoW, int videoH, int width) {
        int h = (videoW > 0 && videoH > 0)
                ? Math.round(width * (float) videoH / videoW)
                : Math.round(width * 4f / 3f);        // 服务端视频实测 720×960；未知时按 3:4
        ViewGroup.LayoutParams lp = stage.getLayoutParams();
        if (lp.height == h) return;
        lp.height = h;
        stage.requestLayout();
    }

    private void resizeHero() {
        fitStageTo(0, 0);
    }

    /** 少数机型 onPrepared 时 getVideoWidth() 仍为 0，轮询几次拿到真实尺寸再定高 */
    private void fitStageFromPlayer(MediaPlayer mp) {
        if (mp == null) return;
        final int[] tries = {0};
        Runnable probe = new Runnable() {
            @Override
            public void run() {
                if (isFinishing()) return;
                int vw = 0, vh = 0;
                try {
                    vw = mp.getVideoWidth();
                    vh = mp.getVideoHeight();
                } catch (Exception ignored) {
                }
                if (vw > 0 && vh > 0) {
                    fitStageTo(vw, vh);
                    return;
                }
                if (tries[0]++ < 12) ui.postDelayed(this, 80);
            }
        };
        probe.run();
    }

    private String introSpec() {
        return emp.introVideo == null ? "" : emp.introVideo.trim();
    }

    /** 服务端介绍视频的「内容版本」，用于判断是否需要重播 */
    private String introSignature() {
        return introSpec() + "|" + Math.round(emp.introDuration * 100) + "|"
                + (emp.introText == null ? "" : emp.introText.hashCode());
    }

    private void loadPoster() {
        String poster = emp.introPoster != null && !emp.introPoster.trim().isEmpty()
                ? emp.introPoster : (emp.photo.isEmpty() ? emp.faceUrl : emp.photo);
        EmployeeStore.loadPhotoBitmap(this, poster, new EmployeeStore.BitmapCallback() {
            @Override
            public void onLoaded(Bitmap bmp) {
                if (isFinishing()) return;
                if (bmp != null) ivIntroPoster.setImageBitmap(bmp);
                else ivIntroPoster.setImageResource(R.drawable.ic_people);
            }
        });
    }

    /** @param autoStart true 才自动开播；false 只准备到首帧并显示播放按钮 */
    private void playHero(boolean autoStart) {
        McpConfig cfg = McpConfig.load(this);
        String base = AssetUrls.serverBase(this);
        String url = StreamContract.resolveStream(cfg.url, base, introSpec());

        if (!url.isEmpty()) {
            String sig = introSignature();
            if (heroMode == HERO_INTRO && url.equals(playingUrl)
                    && sig.equals(playedSignature) && !autoStart) {
                return;                       // 同一份内容已在舞台，不重开
            }
            playIntro(url, sig, autoStart);
            return;
        }
        if (!introSpec().isEmpty()) {
            tvIntroState.setText("介绍视频地址非同源，已拒绝");
            tvStageTag.setText("地址已拒绝");
        }
        playIdleOrCanvas();
    }

    private void playIntro(String url, String signature, boolean autoStart) {
        heroMode = HERO_INTRO;
        playingUrl = url;
        playedSignature = signature;
        userStarted = false;
        tvStageTag.setText("server 端介绍视频");
        tvIntroState.setText("正在加载服务端介绍视频…");
        ivIntroPoster.setVisibility(View.VISIBLE);
        dhvStage.setVisibility(View.GONE);
        videoStage.setVisibility(View.VISIBLE);
        btnStageReplay.setVisibility(View.VISIBLE);
        showPlayHint(false);

        final String playUrl = StreamContract.versioned(url, emp.introDuration, emp.introText);
        final boolean sound = voiceEnabled;

        videoStage.setOnPreparedListener(mp -> {
            if (isFinishing()) return;
            currentMp = mp;
            try {
                mp.setLooping(false);
                mp.setVolume(sound ? 1f : 0f, sound ? 1f : 0f);
            } catch (Exception ignored) {
            }
            fitStageFromPlayer(mp);
            if (autoStart) {
                ivIntroPoster.setVisibility(View.GONE);
                videoStage.start();
                AUTO_PLAYED.add(emp.id);       // 本次运行内不再自动播第二次
                showPlayHint(false);
                tvIntroState.setText(sound
                        ? "正在播放自我介绍 · 音画同轨（服务端）"
                        : "正在播放自我介绍 · 语音控制已关闭（静音）");
            } else {
                // 不起播就出不了首帧：先静音起播 320ms 再暂停回 0 点，只显示画面不出声
                try {
                    mp.setVolume(0f, 0f);
                } catch (Exception ignored) {
                }
                videoStage.start();
                ui.postDelayed(() -> {
                    if (isFinishing() || userStarted) return;
                    try {
                        videoStage.pause();
                        videoStage.seekTo(0);
                    } catch (Exception ignored) {
                    }
                    ivIntroPoster.setVisibility(View.GONE);
                    applyVolume();
                    showPlayHint(true);
                    tvIntroState.setText(sound
                            ? "点按画面播放自我介绍"
                            : "语音控制已关闭 · 点按画面静音播放");
                }, 320);
            }
        });
        videoStage.setOnCompletionListener(mp -> {
            if (isFinishing()) return;
            showPlayHint(true);
            tvIntroState.setText("自我介绍播放完毕 · 点按画面重播");
        });
        videoStage.setOnErrorListener((mp, what, extra) -> {
            if (isFinishing()) return true;
            tvIntroState.setText("介绍视频加载失败（" + what + "/" + extra + "）");
            playIdleOrCanvas();
            return true;
        });
        try {
            videoStage.setVideoURI(Uri.parse(playUrl));
        } catch (Exception e) {
            tvIntroState.setText("打开介绍视频失败：" + e.getMessage());
            playIdleOrCanvas();
        }
    }

    /** 没有介绍视频时的降级：仿真人 idle/talk 视频（静音循环）→ 内置数字人 */
    private void playIdleOrCanvas() {
        String spec = emp.videoIdle.trim().isEmpty() ? emp.videoTalk : emp.videoIdle;
        boolean enabled = McpConfig.load(this).reportVideo;
        Uri uri = (!enabled || spec.isEmpty()) ? null : EmployeeStore.resolveVideo(this, spec);
        if (uri == null) {
            showCanvas();
            return;
        }
        heroMode = HERO_VIDEO;
        playingUrl = "";
        userStarted = false;
        tvStageTag.setText("仿真人形象视频");
        tvVideoState.setText("仿真人视频形象：播放中（服务端素材地址）");
        dhvStage.setVisibility(View.GONE);
        videoStage.setVisibility(View.VISIBLE);
        showPlayHint(false);
        videoStage.setOnPreparedListener(mp -> {
            currentMp = mp;
            try {
                mp.setLooping(true);
                mp.setVolume(0f, 0f);        // 形象底片不出声，声音只来自介绍/播报
            } catch (Exception ignored) {
            }
            fitStageFromPlayer(mp);
            videoStage.start();
            ivIntroPoster.setVisibility(View.GONE);
            tvIntroState.setText("暂无服务端介绍视频 · 播放形象视频");
        });
        videoStage.setOnErrorListener((mp, what, extra) -> {
            showCanvas();
            return true;
        });
        videoStage.setVideoURI(uri);
    }

    private void showCanvas() {
        heroMode = HERO_CANVAS;
        playingUrl = "";
        currentMp = null;
        try {
            videoStage.stopPlayback();
        } catch (Exception ignored) {
        }
        videoStage.setVisibility(View.GONE);
        dhvStage.setVisibility(View.VISIBLE);
        ivIntroPoster.setVisibility(View.GONE);
        btnStageReplay.setVisibility(View.GONE);
        showPlayHint(false);
        tvStageTag.setText("内置数字人形象");
        tvIntroState.setText("服务端尚未生成自我介绍视频");
        if (tvVideoState != null) {
            tvVideoState.setText("仿真人视频形象：未配置（当前使用内置实时驱动形象）");
        }
    }

    private void showPlayHint(boolean show) {
        if (btnStagePlay == null) return;
        btnStagePlay.setVisibility(show ? View.VISIBLE : View.GONE);
    }

    /** 用户点画面：暂停 / 继续播放 */
    private void togglePlayback() {
        if (heroMode != HERO_INTRO) return;
        try {
            if (videoStage.isPlaying()) {
                videoStage.pause();
                showPlayHint(true);
                tvIntroState.setText("已暂停 · 点按画面继续");
            } else {
                startByUser();
            }
        } catch (Exception ignored) {
        }
    }

    /** 用户主动播放：按语音控制开关决定是否出声 */
    private void startByUser() {
        userStarted = true;
        showPlayHint(false);
        applyVolume();
        try {
            int pos = videoStage.getCurrentPosition();
            int dur = videoStage.getDuration();
            if (dur > 0 && pos >= dur - 300) videoStage.seekTo(0);
            videoStage.start();
        } catch (Exception ignored) {
        }
        ivIntroPoster.setVisibility(View.GONE);
        tvIntroState.setText(voiceEnabled
                ? "正在播放自我介绍 · 音画同轨（服务端）"
                : "正在播放自我介绍 · 语音控制已关闭（静音）");
    }

    private void replayByUser() {
        if (heroMode == HERO_INTRO) {
            try {
                videoStage.seekTo(0);
            } catch (Exception ignored) {
            }
            startByUser();
        } else {
            playHero(true);
        }
    }

    // ------------------------------------------------------------------ 与服务端对齐

    /**
     * 拉一次服务端 profile，把该员工的最新介绍视频/时长/文案同步过来；
     * 变了就立即换成新地址重播（这是内容更新，不是「自动播放」）。
     */
    private void syncLatest() {
        if (syncStarted) return;
        syncStarted = true;
        McpConfig cfg = McpConfig.load(this);
        if (cfg.demo) {
            tvIntroState.setText(tvIntroState.getText() + " · 演示模式不同步");
            return;
        }
        final String before = introSignature();
        EmployeeStore.syncFromServer(this, cfg, (count, error) -> {
            if (isFinishing() || count <= 0) {
                if (error != null && !error.isEmpty() && heroMode != HERO_INTRO) {
                    tvIntroState.setText("未取到服务端最新视频：" + shortErr(error));
                }
                return;
            }
            Employee fresh = EmployeeStore.byId(EmployeeStore.load(this), emp.id);
            if (fresh == null) return;
            String after = fresh.introVideo + "|" + Math.round(fresh.introDuration * 100)
                    + "|" + (fresh.introText == null ? "" : fresh.introText.hashCode());
            emp = fresh;
            loadPoster();
            refreshStreamState();
            if (!after.equals(before)) {
                final boolean sound = voiceEnabled;
                tvIntroState.setText("已同步服务端最新视频，正在重播…");
                ui.postDelayed(() -> {
                    String u = StreamContract.resolveStream(McpConfig.load(this).url,
                            AssetUrls.serverBase(this), introSpec());
                    if (!u.isEmpty()) playIntro(u, introSignature(), sound);
                }, 600);
            } else if (heroMode == HERO_INTRO) {
                tvIntroState.setText("已是最新（服务端 " + time(emp.introDuration) + "）");
            }
        });
    }

    /** 详情页已不再显示说明性文字（简介卡保留在「员工资料」里），这里留空以免调用点报错 */
    private void refreshStreamState() {
    }

    private void refreshCurrentButton() {
        if (emp != null && emp.isBoss()) return;   // 老板没有「设为当前汇报人」这回事
        Employee cur = EmployeeStore.current(this, EmployeeStore.load(this));
        boolean isCurrent = cur != null && cur.id.equals(emp.id);
        btnSetCurrent.setText(isCurrent ? "✓ 当前汇报人" : "设为当前汇报人");
    }

    // ------------------------------------------------------------------ 生命周期

    @Override
    protected void onResume() {
        super.onResume();
        // 刻意不自动恢复播放：从别的页面/App 切回来时保持暂停，由用户决定
        if (dhvStage != null) dhvStage.setAnimated(true);
        applyVoiceIcon();
    }

    @Override
    protected void onPause() {
        super.onPause();
        try {
            if (videoStage != null && videoStage.isPlaying()) {
                videoStage.pause();
                if (heroMode == HERO_INTRO) showPlayHint(true);
            }
        } catch (Exception ignored) {
        }
        if (dhvStage != null) dhvStage.setAnimated(false);
    }

    @Override
    protected void onDestroy() {
        super.onDestroy();
        ui.removeCallbacksAndMessages(null);
        if (videoStage != null) {
            try { videoStage.setOnPreparedListener(null); } catch (Exception ignored) { }
            try { videoStage.setOnErrorListener(null); } catch (Exception ignored) { }
            try { videoStage.setOnCompletionListener(null); } catch (Exception ignored) { }
            try { videoStage.stopPlayback(); } catch (Exception ignored) { }
        }
    }

    // ------------------------------------------------------------------ 小工具

    private static String join(List<String> items) {
        if (items == null || items.isEmpty()) return "—";
        StringBuilder b = new StringBuilder();
        for (int i = 0; i < items.size(); i++) {
            if (i > 0) b.append(" · ");
            b.append(items.get(i));
        }
        return b.toString();
    }

    private static String time(double seconds) {
        if (seconds <= 0) return "—";
        int s = (int) Math.round(seconds);
        return String.format(Locale.CHINA, "%02d:%02d", s / 60, s % 60);
    }

    private static String shortErr(String s) {
        if (s == null) return "";
        return s.length() > 60 ? s.substring(0, 60) + "…" : s;
    }
}
