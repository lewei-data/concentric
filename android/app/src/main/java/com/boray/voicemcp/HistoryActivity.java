package com.boray.voicemcp;

import android.app.Activity;
import android.app.AlertDialog;
import android.content.Intent;
import android.os.Bundle;
import android.view.LayoutInflater;
import android.view.View;
import android.widget.ImageView;
import android.widget.LinearLayout;
import android.widget.TextView;
import android.widget.Toast;

import java.io.File;
import java.text.SimpleDateFormat;
import java.util.Date;
import java.util.List;
import java.util.Locale;

/** 本机长期保存的历史消息列表：语音任务结果、SSE 推送、CEO 新邮件 */
public class HistoryActivity extends Activity {

    private LinearLayout llHistory;
    private TextView tvCount, tvEmpty, tvSummary;
    private final SimpleDateFormat fmt = new SimpleDateFormat("MM-dd HH:mm", Locale.CHINA);

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        setContentView(R.layout.activity_history);
        WindowInsetsHelper.apply(this);
        llHistory = findViewById(R.id.llHistory);
        tvCount = findViewById(R.id.tvCount);
        tvEmpty = findViewById(R.id.tvEmpty);
        tvSummary = findViewById(R.id.tvSummary);

        findViewById(R.id.btnBack).setOnClickListener(v -> finish());
        findViewById(R.id.btnClear).setOnClickListener(v -> confirmClear());
    }

    @Override
    protected void onResume() {
        super.onResume();
        render();
    }

    private void render() {
        List<HistoryDb.Record> list = HistoryDb.get(this).list(500);
        llHistory.removeAllViews();
        tvEmpty.setVisibility(list.isEmpty() ? View.VISIBLE : View.GONE);
        int audio = 0, video = 0;
        for (HistoryDb.Record r : list) {
            if (hasFile(r.audioPath)) audio++;
            if (hasFile(r.videoPath) || !r.videoUrl.isEmpty()) video++;
        }
        tvCount.setText("全部记录 · " + list.size() + " 条");
        tvSummary.setText("本机长期保存 · 音频 " + audio + " 条 · 视频 " + video + " 条");

        LayoutInflater inflater = LayoutInflater.from(this);
        for (HistoryDb.Record r : list) {
            View row = inflater.inflate(R.layout.item_history, llHistory, false);
            TextView tvKind = row.findViewById(R.id.tvKind);
            TextView tvTime = row.findViewById(R.id.tvTime);
            TextView tvAnswer = row.findViewById(R.id.tvAnswer);
            TextView tvStatus = row.findViewById(R.id.tvStatus);
            ImageView ivKind = row.findViewById(R.id.ivKind);
            ImageView ivAudio = row.findViewById(R.id.ivAudio);
            ImageView ivVideo = row.findViewById(R.id.ivVideo);

            tvKind.setText(kindLabel(r.kind));
            tvTime.setText(fmt.format(new Date(r.timeMs)));
            String summary = r.answer.isEmpty() ? r.question : r.answer;
            tvAnswer.setText(summary.replace('\n', ' ').trim());
            tvStatus.setText(statusLabel(r.status));
            ivKind.setImageResource(kindIcon(r.kind));
            ivAudio.setVisibility(hasFile(r.audioPath) ? View.VISIBLE : View.GONE);
            ivVideo.setVisibility(hasFile(r.videoPath) || !r.videoUrl.isEmpty()
                    ? View.VISIBLE : View.GONE);
            final long id = r.id;
            row.setOnClickListener(v -> startActivity(
                    new Intent(this, HistoryDetailActivity.class)
                            .putExtra(HistoryDetailActivity.EXTRA_ID, id)));
            llHistory.addView(row);
        }
    }

    private void confirmClear() {
        int n = HistoryDb.get(this).count();
        if (n == 0) { toast("没有可清空的历史消息"); return; }
        new AlertDialog.Builder(this)
                .setTitle("清空历史消息")
                .setMessage("将删除本机保存的 " + n + " 条记录及其音频 / 视频 / 文字文件，此操作不可恢复。")
                .setPositiveButton("确认清空", (d, w) -> {
                    for (HistoryDb.Record r : HistoryDb.get(this).list(100000)) {
                        HistoryDb.deleteFiles(r);
                    }
                    HistoryDb.get(this).clear();
                    render();
                    toast("已清空历史消息");
                })
                .setNegativeButton("取消", null)
                .show();
    }

    private static boolean hasFile(String path) {
        return path != null && !path.isEmpty() && new File(path).isFile();
    }

    private static String kindLabel(String kind) {
        if ("mail".equals(kind)) return "新邮件";
        if ("push".equals(kind)) return "推送消息";
        return "语音任务";
    }

    private static int kindIcon(String kind) {
        if ("mail".equals(kind)) return R.drawable.ic_text;
        if ("push".equals(kind)) return R.drawable.ic_link;
        return R.drawable.ic_mic;
    }

    private static String statusLabel(String status) {
        if (status == null || status.isEmpty()) return "";
        switch (status) {
            case "done": return "已完成";
            case "accepted": return "已接收";
            case "running": return "处理中";
            case "failed": case "error": return "失败";
            default: return status;
        }
    }

    private void toast(String s) {
        Toast.makeText(this, s, Toast.LENGTH_SHORT).show();
    }
}
