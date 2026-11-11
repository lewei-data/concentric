package com.boray.voicemcp;

import android.app.Activity;
import android.content.Intent;
import android.media.MediaPlayer;
import android.net.Uri;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.os.SystemClock;
import android.speech.tts.TextToSpeech;
import android.speech.tts.UtteranceProgressListener;
import android.view.View;
import android.widget.Button;
import android.widget.ImageView;
import android.widget.ProgressBar;
import android.widget.TextView;
import android.widget.VideoView;

import org.json.JSONArray;
import org.json.JSONException;
import org.json.JSONObject;

import java.io.File;
import java.util.ArrayList;
import java.util.List;
import java.util.Locale;

/**
 * 数字员工汇报画面（server v2.1.0）。
 *
 * 渲染指令来自 voice_job 的 result.render：
 *   driver = wav_envelope → 用同一份结果 WAV 的 PCM 振幅包络驱动口型 / 表情 / 动作
 *   lip_sync = amplitude  → 逐帧按 MediaPlayer.getCurrentPosition() 取包络，音画严格同步
 *   timeline = segments   → 句级时间轴（秒）直接做字幕
 *
 * 形象一致性：主舞台（视频 / 照片）与角落「口型小窗」都取同一位员工的服务端素材。
 * 有真人照片时小窗改用该照片做底片，只叠加动态嘴形，不再画另一个卡通人。
 * 服务端视频是 6 秒循环底片，真正「实时」的是按音频逐帧驱动的口型层。
 *
 * 试听链路：档案页先向 /conring/tts/mcp 用员工音色合成真实 WAV 再传入；
 * 没有音频时本页用系统 TTS 的 synthesizeToFile 生成 WAV 兜底，
 * 保证「听到的声音」与「驱动口型的音频」始终是同一份，避免不对嘴。
 */
public class ReportActivity extends Activity {

    public static final String EXTRA_ANSWER = "answer";
    public static final String EXTRA_QUESTION = "question";
    public static final String EXTRA_AUDIO = "audio_path";
    public static final String EXTRA_EMPLOYEE = "employee_id";
    public static final String EXTRA_EMPLOYEE_NAME = "employee_name";
    public static final String EXTRA_IS_ERROR = "is_error";
    /** v2.1.0：result.segments 原始 JSON，句级字幕时间轴 */
    public static final String EXTRA_SEGMENTS = "segments_json";
    /** 当次结果里 avatar.photo.url / videos.talk.url（已按服务端地址解析） */
    public static final String EXTRA_PHOTO = "photo_url";
    public static final String EXTRA_VIDEO = "video_url";
    /** 面部特写图，用于「照片 + 口型」小窗 */
    public static final String EXTRA_FACE = "face_url";

    private static final String UTTERANCE_PREVIEW = "conring_report_preview";

    private final Handler ui = new Handler(Looper.getMainLooper());

    private Employee employee;
    private String employeeName = "";
    private String answer = "";
    private String question = "";
    private String audioPath = "";
    private String segmentsJson = "";
    private String photoUrl = "";
    private String videoUrl = "";
    private String faceUrl = "";
    private boolean isError;
    private boolean hasSegments;

    private DigitalHumanView humanStage, humanPip;
    private VideoView videoStage;
    private ImageView ivPhotoStage;
    private TextView tvEmpName, tvEmpTitle, tvReportState, tvQuestion, tvCaption;
    private TextView tvTime, tvSyncSource, tvMoodTag, tvVideoTag;
    private ProgressBar pbProgress;

    private MediaPlayer media;
    private TextToSpeech tts;
    private LipSync sync;
    private boolean hasAudio, finished, switching, synthPending;
    private boolean audioDriven;          // true = 口型由真实 WAV 包络驱动
    private int durationMs = 1;
    private long startUptime;
    private String lastCaption = "";

    private List<String> sentences = new ArrayList<>();
    private int[] starts = new int[0];

    private final Runnable driver = new Runnable() {
        @Override public void run() {
            if (finished || isFinishing() || sync == null) return;
            int pos = currentPosition();
            if (pos >= durationMs - 40) { finishReport(); return; }
            float level = sync.levelAt(pos);
            int shape = sync.shapeAt(pos);
            String mood = moodFor(pos);
            humanStage.setFrame(level, shape, mood, true);
            humanPip.setFrame(level, shape, mood, true);
            tvMoodTag.setText(moodLabel(mood));
            String cap = captionAt(pos);
            if (!cap.equals(lastCaption)) {
                lastCaption = cap;
                tvCaption.setAlpha(0.25f);
                tvCaption.setText(cap);
                tvCaption.animate().alpha(1f).setDuration(180).start();
            }
            pbProgress.setProgress(Math.min(100, (int) ((long) pos * 100 / durationMs)));
            tvTime.setText(fmt(pos) + " / " + fmt(durationMs));
            ui.postDelayed(this, 33);
        }
    };

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        setContentView(R.layout.activity_report);
        WindowInsetsHelper.apply(this);

        Intent in = getIntent();
        answer = str(in, EXTRA_ANSWER);
        question = str(in, EXTRA_QUESTION);
        audioPath = str(in, EXTRA_AUDIO);
        segmentsJson = str(in, EXTRA_SEGMENTS);
        photoUrl = str(in, EXTRA_PHOTO);
        videoUrl = str(in, EXTRA_VIDEO);
        faceUrl = str(in, EXTRA_FACE);
        employeeName = str(in, EXTRA_EMPLOYEE_NAME);
        isError = in.getBooleanExtra(EXTRA_IS_ERROR, false);
        String id = in.getStringExtra(EXTRA_EMPLOYEE);
        List<Employee> all = EmployeeStore.load(this);
        if (id == null || id.isEmpty()) {
            employee = EmployeeStore.current(this, all);       // 默认 CEO
        } else {
            employee = EmployeeStore.findById(all, id);
            // 本地档案尚未同步到本次 avatar 时，用服务端返回的字段临时构造，
            // 避免退回列表第 1 位导致「名字是 A、照片/视频却是 B」。
            if (employee == null) employee = employeeFromAvatar(id);
        }

        humanStage = findViewById(R.id.humanStage);
        humanPip = findViewById(R.id.humanPip);
        videoStage = findViewById(R.id.videoStage);
        ivPhotoStage = findViewById(R.id.ivPhotoStage);
        tvEmpName = findViewById(R.id.tvEmpName);
        tvEmpTitle = findViewById(R.id.tvEmpTitle);
        tvReportState = findViewById(R.id.tvReportState);
        tvQuestion = findViewById(R.id.tvQuestion);
        tvCaption = findViewById(R.id.tvCaption);
        tvTime = findViewById(R.id.tvTime);
        tvSyncSource = findViewById(R.id.tvSyncSource);
        tvMoodTag = findViewById(R.id.tvMoodTag);
        tvVideoTag = findViewById(R.id.tvVideoTag);
        pbProgress = findViewById(R.id.pbProgress);

        applyEmployee();
        setupStage();

        tvQuestion.setText(question.isEmpty() ? "识别到的指令：—" : "识别到的指令：" + question);
        if (employee != null) {
            humanStage.setFrame(0f, LipSync.SHAPE_CLOSED, employee.emotion, false);
            humanPip.setFrame(0f, LipSync.SHAPE_CLOSED, employee.emotion, false);
        }

        View close = findViewById(R.id.btnClose);
        if (close != null) close.setOnClickListener(v -> finish());
        Button closeBottom = findViewById(R.id.btnCloseBottom);
        closeBottom.setOnClickListener(v -> finish());
        findViewById(R.id.btnReplay).setOnClickListener(v -> {
            if (sync == null) preparePlayback(); else startPlayback();
        });
        findViewById(R.id.btnSwitch).setOnClickListener(v -> {
            switching = true;
            startActivity(new Intent(this, EmployeeListActivity.class));
        });

        preparePlayback();
    }

    /** 服务端返回的 avatar 不在本地缓存时，构造一个只含本次素材的形象对象 */
    private Employee employeeFromAvatar(String id) {
        Employee e = new Employee();
        e.id = id;
        e.name = employeeName.isEmpty() ? "数字员工" : employeeName;
        e.title = "汇报员";
        e.photo = photoUrl;
        e.faceUrl = faceUrl;
        e.videoTalk = videoUrl;
        e.hasPhoto = !e.photo.isEmpty();
        e.hasVideo = !e.videoTalk.isEmpty();
        return e;
    }

    @Override
    protected void onResume() {
        super.onResume();
        // 从档案页切换汇报员回来：换形象，不打断正在播报的音频
        if (switching) {
            switching = false;
            Employee cur = EmployeeStore.current(this, EmployeeStore.load(this));
            if (cur != null && (employee == null || !cur.id.equals(employee.id))) {
                employee = cur;
                employeeName = cur.name;
                photoUrl = cur.photo;
                faceUrl = cur.faceUrl;
                videoUrl = cur.videoTalk;
                applyEmployee();
                setupStage();
            }
        }
    }

    private void applyEmployee() {
        if (employee == null) return;
        // 以服务端本次返回的 avatar 名字为准，避免与素材来自不同员工
        tvEmpName.setText(employeeName.isEmpty() ? employee.name : employeeName);
        String role = employee.title.isEmpty() ? employee.role : employee.title;
        tvEmpTitle.setText(role + (employee.dept.isEmpty() ? "" : " · " + employee.dept));
        humanStage.setEmployee(employee);
        humanPip.setEmployee(employee);
        humanStage.setAnimated(true);
        humanPip.setAnimated(true);
        applyPipPhoto();
    }

    /** 口型小窗用同一位员工的面部特写 / 照片，不再画一张与真人不同的卡通脸 */
    private void applyPipPhoto() {
        String spec = firstNonEmpty(faceUrl, employee.faceUrl, photoUrl, employee.photo);
        if (spec.isEmpty()) {
            humanPip.clearPhotoBitmap();
            return;
        }
        EmployeeStore.loadPhotoBitmap(this, spec, bmp -> {
            if (isFinishing()) return;
            if (bmp != null) humanPip.setPhotoBitmap(bmp);
            else humanPip.clearPhotoBitmap();
        });
    }

    /** video → avatar(照片) → 内置数字人，逐级回退；素材地址只用服务端返回的那个 */
    private void setupStage() {
        if (employee == null) return;
        String spec = videoUrl;
        if (spec.isEmpty()) {
            spec = employee.videoTalk.trim().isEmpty() ? employee.videoIdle : employee.videoTalk;
        }
        if (McpConfig.load(this).reportVideo && !spec.trim().isEmpty()) {
            Uri uri = EmployeeStore.resolveVideo(this, spec);
            if (uri != null) {
                try {
                    videoStage.stopPlayback();
                    videoStage.setVideoURI(uri);
                    videoStage.setOnPreparedListener(mp -> {
                        mp.setLooping(true);
                        mp.setVolume(0f, 0f);   // 视频无音轨，声音只来自播报音频
                        videoStage.start();
                        tvVideoTag.setText("仿真人视频底片 · 口型实时驱动");
                    });
                    videoStage.setOnErrorListener((mp, what, extra) -> {
                        runOnUiThread(() -> {
                            showPhotoOrHuman();
                            tvVideoTag.setVisibility(View.VISIBLE);
                            tvVideoTag.setText("视频不可用 · 已回退照片");
                        });
                        return true;
                    });
                    videoStage.setVisibility(View.VISIBLE);
                    ivPhotoStage.setVisibility(View.GONE);
                    humanStage.setVisibility(View.GONE);
                    humanPip.setVisibility(View.VISIBLE);
                    tvVideoTag.setVisibility(View.VISIBLE);
                    tvVideoTag.setText("仿真人视频加载中…");
                    return;
                } catch (Exception ignored) {
                    // 落到下一级
                }
            }
        }
        showPhotoOrHuman();
    }

    private void showPhotoOrHuman() {
        videoStage.setVisibility(View.GONE);
        tvVideoTag.setVisibility(View.GONE);
        String photo = firstNonEmpty(photoUrl, employee == null ? "" : employee.photo);
        if (!photo.isEmpty()) {
            Employee tmp = new Employee();
            tmp.photo = photo;
            ivPhotoStage.setVisibility(View.VISIBLE);
            humanStage.setVisibility(View.GONE);
            humanPip.setVisibility(View.VISIBLE);
            EmployeeStore.applyPhoto(this, tmp, ivPhotoStage,
                    () -> { }, this::showHumanOnly);
            return;
        }
        showHumanOnly();
    }

    private void showHumanOnly() {
        videoStage.setVisibility(View.GONE);
        ivPhotoStage.setVisibility(View.GONE);
        tvVideoTag.setVisibility(View.GONE);
        humanStage.setVisibility(View.VISIBLE);
        humanPip.setVisibility(View.GONE);
    }

    // ------------------------------------------------------------------ 播放与驱动

    /** 优先用真实 WAV；没有就本地合成 WAV；都失败才退回「文本包络 + 系统朗读」 */
    private void preparePlayback() {
        sync = LipSync.fromWav(audioPath);
        if (sync != null) {
            audioDriven = true;
            startPlayback();
            return;
        }
        audioDriven = false;
        if (initLocalSynthesis()) {
            synthPending = true;
            tvReportState.setText("正在生成播报语音…");
            tvSyncSource.setText("准备真实音频，口型将逐帧同步");
            return;
        }
        sync = LipSync.synthetic(answer);
        startPlayback();
    }

    /** 用系统 TTS 把答案写成 WAV，之后仍走 MediaPlayer + PCM 包络，口型与语音同源 */
    private boolean initLocalSynthesis() {
        try {
            final File out = new File(getCacheDir(), "report_preview.wav");
            tts = new TextToSpeech(this, status -> {
                if (status != TextToSpeech.SUCCESS || tts == null) {
                    ui.post(ReportActivity.this::onSynthFailed);
                    return;
                }
                tts.setLanguage(Locale.CHINESE);
                tts.setOnUtteranceProgressListener(new UtteranceProgressListener() {
                    @Override public void onStart(String utteranceId) { }
                    @Override public void onDone(String utteranceId) {
                        if (UTTERANCE_PREVIEW.equals(utteranceId)) ui.post(() -> onSynthReady(out));
                    }
                    @Override public void onError(String utteranceId, int errorCode) {
                        ui.post(ReportActivity.this::onSynthFailed);
                    }
                    @Override public void onError(String utteranceId) {
                        ui.post(ReportActivity.this::onSynthFailed);
                    }
                });
                String s = answer.length() > 500 ? answer.substring(0, 500) : answer;
                Bundle params = new Bundle();
                params.putString(TextToSpeech.Engine.KEY_PARAM_UTTERANCE_ID, UTTERANCE_PREVIEW);
                int r = tts.synthesizeToFile(s, params, out, UTTERANCE_PREVIEW);
                if (r != TextToSpeech.SUCCESS) ui.post(ReportActivity.this::onSynthFailed);
            });
            return true;
        } catch (Exception e) {
            tts = null;
            return false;
        }
    }

    private void onSynthReady(File f) {
        if (isFinishing() || !synthPending) return;
        synthPending = false;
        LipSync s = (f.isFile() && f.length() > 1024)
                ? LipSync.fromWav(f.getAbsolutePath()) : null;
        if (s != null) {
            audioPath = f.getAbsolutePath();
            sync = s;
            audioDriven = true;
        } else {
            sync = LipSync.synthetic(answer);
            audioDriven = false;
        }
        startPlayback();
    }

    private void onSynthFailed() {
        if (isFinishing() || !synthPending) return;
        synthPending = false;
        sync = LipSync.synthetic(answer);
        audioDriven = false;
        startPlayback();
    }

    private void startPlayback() {
        stopPlayback();
        finished = false;
        hasAudio = false;
        lastCaption = "";
        durationMs = sync == null ? 1 : sync.durationMs;

        if (!audioPath.isEmpty() && new File(audioPath).isFile()) {
            try {
                media = new MediaPlayer();
                media.setDataSource(audioPath);
                media.setOnCompletionListener(mp -> finishReport());
                media.setOnErrorListener((mp, what, extra) -> {
                    ui.post(this::fallbackToTts);
                    return true;
                });
                media.prepare();
                int d = media.getDuration();
                if (d > 0) durationMs = d;
                media.start();
                hasAudio = true;
            } catch (Exception e) {
                releaseMedia();
                fallbackToTts();
            }
        } else {
            fallbackToTts();
        }
        if (!hasAudio) durationMs = sync == null ? 1 : sync.durationMs;
        buildCaptions(answer, segmentsJson);
        tvSyncSource.setText(syncLabel());
        startUptime = SystemClock.uptimeMillis();
        tvReportState.setText("汇报中");
        pbProgress.setProgress(0);
        tvTime.setText("00:00 / " + fmt(durationMs));
        ui.removeCallbacks(driver);
        if (sync != null) ui.post(driver);
    }

    /** 如实标注口型数据来源：真实音频 PCM 才是严格同步 */
    private String syncLabel() {
        if (audioDriven && hasAudio) {
            return hasSegments ? "口型：真实 WAV 包络逐帧同步 · 字幕：服务端时间轴"
                    : "口型：真实 WAV 包络逐帧同步";
        }
        return "口型：文本音节估算（非音频同步）"
                + (hasSegments ? " · 字幕：服务端时间轴" : "");
    }

    private void fallbackToTts() {
        hasAudio = false;
        audioDriven = false;
        try {
            if (tts == null) {
                tts = new TextToSpeech(this, status -> {
                    if (status == TextToSpeech.SUCCESS && tts != null) {
                        tts.setLanguage(Locale.CHINESE);
                        speakAnswer();
                    }
                });
            } else {
                speakAnswer();
            }
        } catch (Exception ignored) {
        }
    }

    private void speakAnswer() {
        if (tts == null) return;
        String s = answer.length() > 500 ? answer.substring(0, 500) : answer;
        try {
            tts.speak(s, TextToSpeech.QUEUE_FLUSH, null, "digital-employee-report");
        } catch (Exception ignored) {
        }
    }

    private int currentPosition() {
        if (hasAudio && media != null) {
            try { return media.getCurrentPosition(); } catch (Exception ignored) { }
        }
        return (int) (SystemClock.uptimeMillis() - startUptime);
    }

    private void finishReport() {
        if (finished) return;
        finished = true;
        ui.removeCallbacks(driver);
        String mood = isError ? "concerned" : "cheerful";
        humanStage.setFrame(0f, LipSync.SHAPE_CLOSED, mood, false);
        humanPip.setFrame(0f, LipSync.SHAPE_CLOSED, mood, false);
        tvMoodTag.setText(isError ? "关切说明" : "致意");
        tvReportState.setText(isError ? "任务失败" : "汇报完毕");
        pbProgress.setProgress(100);
        tvTime.setText(fmt(durationMs) + " / " + fmt(durationMs));
    }

    private void stopPlayback() {
        ui.removeCallbacks(driver);
        releaseMedia();
        if (tts != null) {
            try { tts.stop(); } catch (Exception ignored) { }
        }
    }

    private void releaseMedia() {
        if (media != null) {
            try {
                if (media.isPlaying()) media.stop();
                media.release();
            } catch (Exception ignored) { }
            media = null;
        }
    }

    // ------------------------------------------------------------------ 表情 / 字幕

    private String moodFor(int pos) {
        float ratio = durationMs <= 0 ? 0f : (float) pos / durationMs;
        if (isError) return "concerned";
        if (ratio < 0.08f) return "confident";
        if (ratio > 0.94f) return "cheerful";
        String a = answer;
        if (a.contains("失败") || a.contains("错误") || a.contains("异常")
                || a.contains("无法") || a.contains("抱歉") || a.contains("未找到")) return "concerned";
        if (a.contains("完成") || a.contains("成功") || a.contains("已发送")
                || a.contains("已生成") || a.contains("已为")) return "cheerful";
        if (a.contains("？") || a.contains("?") || a.contains("请确认") || a.contains("是否")) return "thinking";
        return employee == null ? "calm" : employee.emotion;
    }

    private String moodLabel(String mood) {
        switch (mood) {
            case "confident": return "自信开场";
            case "cheerful": case "happy": return "微笑汇报";
            case "thinking": return "思考中";
            case "concerned": return "关切说明";
            case "serious": return "严谨陈述";
            default: return "专注汇报";
        }
    }

    /** 优先用服务端 segments（秒，与 WAV 严格对齐）；没有时才按字数估算 */
    private void buildCaptions(String text, String segJson) {
        sentences = new ArrayList<>();
        starts = new int[0];
        hasSegments = false;
        if (!segJson.isEmpty()) {
            try {
                JSONArray arr = new JSONArray(segJson);
                List<Integer> times = new ArrayList<>();
                for (int i = 0; i < arr.length(); i++) {
                    JSONObject o = arr.optJSONObject(i);
                    if (o == null) continue;
                    String t = o.optString("text", "").trim();
                    if (t.isEmpty()) continue;
                    double start = o.optDouble("start", -1);
                    if (start < 0) continue;
                    sentences.add(t);
                    times.add((int) Math.round(start * 1000));
                }
                if (!sentences.isEmpty()) {
                    starts = new int[times.size()];
                    for (int i = 0; i < times.size(); i++) starts[i] = times.get(i);
                    hasSegments = true;
                    return;
                }
            } catch (JSONException ignored) {
            }
        }

        String s = text == null ? "" : text.replace('\n', ' ').trim();
        StringBuilder cur = new StringBuilder();
        for (int i = 0; i < s.length(); i++) {
            char ch = s.charAt(i);
            cur.append(ch);
            boolean end = "。！？!?；;".indexOf(ch) >= 0;
            boolean comma = (ch == '，' || ch == ',') && cur.length() >= 20;
            if (end || comma) {
                String v = cur.toString().trim();
                if (!v.isEmpty()) sentences.add(v);
                cur.setLength(0);
            }
        }
        if (cur.length() > 0) sentences.add(cur.toString().trim());
        if (sentences.isEmpty()) sentences.add(s.isEmpty() ? "（服务端未返回文字内容）" : s);

        int total = 0;
        for (String p : sentences) total += Math.max(1, p.length());
        if (total <= 0) total = 1;
        starts = new int[sentences.size()];
        int acc = 0;
        for (int i = 0; i < sentences.size(); i++) {
            starts[i] = (int) ((long) acc * durationMs / total);
            acc += Math.max(1, sentences.get(i).length());
        }
    }

    private String captionAt(int pos) {
        int idx = 0;
        for (int i = 0; i < starts.length; i++) {
            if (starts[i] <= pos) idx = i;
            else break;
        }
        return idx < sentences.size() ? sentences.get(idx) : "";
    }

    private static String firstNonEmpty(String... values) {
        if (values == null) return "";
        for (String v : values) {
            if (v != null && !v.trim().isEmpty()) return v.trim();
        }
        return "";
    }

    private static String str(Intent i, String key) {
        String v = i.getStringExtra(key);
        return v == null ? "" : v;
    }

    private static String fmt(int ms) {
        int total = Math.max(0, ms) / 1000;
        return String.format(Locale.US, "%02d:%02d", total / 60, total % 60);
    }

    @Override
    protected void onDestroy() {
        super.onDestroy();
        stopPlayback();
        if (tts != null) {
            try { tts.shutdown(); } catch (Exception ignored) { }
            tts = null;
        }
        if (videoStage != null) {
            try { videoStage.stopPlayback(); } catch (Exception ignored) { }
        }
    }
}
