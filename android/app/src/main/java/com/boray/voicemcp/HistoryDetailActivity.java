package com.boray.voicemcp;

import android.app.Activity;
import android.app.AlertDialog;
import android.content.Intent;
import android.media.MediaPlayer;
import android.net.Uri;
import android.os.Bundle;
import android.view.LayoutInflater;
import android.view.View;
import android.widget.ImageView;
import android.widget.LinearLayout;
import android.widget.MediaController;
import android.widget.TextView;
import android.widget.Toast;
import android.widget.VideoView;

import java.io.File;
import java.text.SimpleDateFormat;
import java.util.Date;
import java.util.List;
import java.util.Locale;

/** 单条历史消息：核心内容 + 详细文字 + 音频重播 + 视频播放 + 附件图标链接 + 重新汇报 */
public class HistoryDetailActivity extends Activity {

    public static final String EXTRA_ID = "history_id";

    private HistoryDb.Record rec;
    private VoicePlayer player;
    private VideoView video;
    private LinearLayout llAttachments;
    private TextView tvMediaState;

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        setContentView(R.layout.activity_history_detail);
        WindowInsetsHelper.apply(this);

        long id = getIntent().getLongExtra(EXTRA_ID, -1);
        rec = HistoryDb.get(this).get(id);
        if (rec == null) { toast("记录不存在"); finish(); return; }

        player = new VoicePlayer(this);
        video = findViewById(R.id.videoStage);
        llAttachments = findViewById(R.id.llAttachments);
        tvMediaState = findViewById(R.id.tvMediaState);

        findViewById(R.id.btnBack).setOnClickListener(v -> finish());
        findViewById(R.id.btnDelete).setOnClickListener(v -> confirmDelete());
        findViewById(R.id.btnPlayAudio).setOnClickListener(v -> playAudio());
        findViewById(R.id.btnPlayVideo).setOnClickListener(v -> playVideo());
        findViewById(R.id.btnReport).setOnClickListener(v -> launchReport());

        fill();
    }

    private void fill() {
        SimpleDateFormat fmt = new SimpleDateFormat("yyyy-MM-dd HH:mm:ss", Locale.CHINA);
        StringBuilder meta = new StringBuilder();
        meta.append("时间：").append(fmt.format(new Date(rec.timeMs))).append('\n');
        meta.append("来源：").append(kindLabel(rec.kind));
        if (!rec.employee.isEmpty()) meta.append(" · 汇报员工：").append(rec.employee);
        if (!rec.mode.isEmpty()) meta.append(" · 播报形态：").append(rec.mode);
        if (!rec.voice.isEmpty()) meta.append(" · 音色：").append(rec.voice);
        if (!rec.delegate.isEmpty()) meta.append(" · 派单：").append(rec.delegate);
        if (!rec.errorText.isEmpty()) meta.append(" · 错误：").append(rec.errorText);
        ((TextView) findViewById(R.id.tvMeta)).setText(meta.toString());

        TextView q = findViewById(R.id.tvQuestion);
        q.setText(rec.question.isEmpty() ? "识别到的指令：—" : "识别到的指令：" + rec.question);
        ((TextView) findViewById(R.id.tvAnswer)).setText(
                rec.answer.isEmpty() ? "（无文本内容）" : rec.answer);
        View cardDetail = findViewById(R.id.cardDetail);
        if (rec.detail.isEmpty() || rec.detail.equals(rec.answer)) {
            cardDetail.setVisibility(View.GONE);
        } else {
            ((TextView) findViewById(R.id.tvDetail)).setText(rec.detail);
        }

        boolean hasAudio = hasFile(rec.audioPath);
        boolean hasVideo = hasFile(rec.videoPath) || !rec.videoUrl.isEmpty();
        tvMediaState.setText("媒体：音频 " + (hasAudio ? "本机已保存" : "无")
                + " · 视频 " + (hasFile(rec.videoPath) ? "本机已保存"
                : (rec.videoUrl.isEmpty() ? "无" : "服务端素材（可直接播放）")));

        renderAttachments();
    }

    private void renderAttachments() {
        llAttachments.removeAllViews();
        List<McpResult.Item> files = HistoryDb.jsonToFiles(rec.filesJson);
        if (files.isEmpty()) {
            TextView empty = new TextView(this);
            empty.setText("暂无附件");
            empty.setTextColor(getResources().getColor(R.color.text_hint, getTheme()));
            empty.setPadding(12, 12, 12, 12);
            llAttachments.addView(empty);
            return;
        }
        LayoutInflater inflater = LayoutInflater.from(this);
        for (McpResult.Item item : files) {
            if (item.uri == null) continue;
            View row = inflater.inflate(R.layout.item_attachment, llAttachments, false);
            ((TextView) row.findViewById(R.id.tvFileName)).setText(
                    item.label.isEmpty() ? ResultStore.labelFor(item.mime) : item.label);
            ((TextView) row.findViewById(R.id.tvFileMeta)).setText("点击查看");
            ImageView icon = row.findViewById(R.id.ivFileIcon);
            if (item.mime != null && item.mime.startsWith("audio")) icon.setImageResource(R.drawable.ic_audio);
            else if (item.mime != null && item.mime.startsWith("video")) icon.setImageResource(R.drawable.ic_video);
            else if (item.mime != null && item.mime.startsWith("text")) icon.setImageResource(R.drawable.ic_text);
            else icon.setImageResource(R.drawable.ic_file);
            row.setOnClickListener(v -> openFile(item));
            llAttachments.addView(row);
        }
    }

    private void playAudio() {
        if (hasFile(rec.audioPath)) {
            player.playFile(rec.audioPath, ok -> {
                if (!ok) { player.speak(rec.answer); toast("音频播放失败，改用系统朗读"); }
            });
        } else if (!rec.answer.isEmpty()) {
            player.speak(rec.answer);
        } else {
            toast("这条消息没有可播放的语音");
        }
    }

    private void playVideo() {
        String local = hasFile(rec.videoPath) ? rec.videoPath : "";
        String remote = rec.videoUrl;
        if (local.isEmpty() && remote.isEmpty()) {
            toast("这条消息没有视频素材");
            return;
        }
        video.setVisibility(View.VISIBLE);
        MediaController controller = new MediaController(this);
        controller.setAnchorView(video);
        video.setMediaController(controller);
        video.setOnErrorListener((mp, what, extra) -> {
            toast("视频无法播放：素材地址不可访问或格式不支持");
            video.setVisibility(View.GONE);
            return true;
        });
        try {
            if (!local.isEmpty()) video.setVideoPath(local);
            else video.setVideoURI(Uri.parse(remote));
            video.setOnPreparedListener(MediaPlayer::start);
            video.start();
            video.requestFocus();
        } catch (Exception e) {
            toast("视频打开失败：" + e.getMessage());
        }
    }

    private void launchReport() {
        Intent i = new Intent(this, ReportActivity.class);
        i.putExtra(ReportActivity.EXTRA_ANSWER, rec.answer);
        i.putExtra(ReportActivity.EXTRA_QUESTION, rec.question);
        if (hasFile(rec.audioPath)) i.putExtra(ReportActivity.EXTRA_AUDIO, rec.audioPath);
        if (!rec.avatarId.isEmpty()) i.putExtra(ReportActivity.EXTRA_EMPLOYEE, rec.avatarId);
        if (!rec.employee.isEmpty()) {
            i.putExtra(ReportActivity.EXTRA_EMPLOYEE_NAME, rec.employee);
        }
        if (!rec.segmentsJson.isEmpty()) i.putExtra(ReportActivity.EXTRA_SEGMENTS, rec.segmentsJson);
        if (!rec.photoUrl.isEmpty()) i.putExtra(ReportActivity.EXTRA_PHOTO, rec.photoUrl);
        if (!rec.videoUrl.isEmpty()) i.putExtra(ReportActivity.EXTRA_VIDEO, rec.videoUrl);
        i.putExtra(ReportActivity.EXTRA_IS_ERROR, !rec.errorText.isEmpty());
        startActivity(i);
    }

    private void confirmDelete() {
        new AlertDialog.Builder(this)
                .setTitle("删除这条消息")
                .setMessage("同时删除关联的音频 / 视频 / 文字文件，不可恢复。")
                .setPositiveButton("删除", (d, w) -> {
                    HistoryDb.deleteFiles(rec);
                    HistoryDb.get(this).delete(rec.id);
                    toast("已删除");
                    finish();
                })
                .setNegativeButton("取消", null)
                .show();
    }

    private void openFile(McpResult.Item item) {
        Intent view = new Intent(Intent.ACTION_VIEW);
        view.setDataAndType(item.uri, item.mime);
        view.addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION);
        try {
            startActivity(Intent.createChooser(view, "打开附件"));
        } catch (Exception e) {
            toast("没有可以打开 " + item.mime + " 的应用");
        }
    }

    private static boolean hasFile(String path) {
        return path != null && !path.isEmpty() && new File(path).isFile();
    }

    private static String kindLabel(String kind) {
        if ("mail".equals(kind)) return "CEO 新邮件";
        if ("push".equals(kind)) return "服务端推送";
        return "语音任务";
    }

    @Override
    protected void onDestroy() {
        super.onDestroy();
        if (player != null) player.release();
        if (video != null) {
            try { video.stopPlayback(); } catch (Exception ignored) { }
        }
    }

    private void toast(String s) {
        Toast.makeText(this, s, Toast.LENGTH_SHORT).show();
    }
}
