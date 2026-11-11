package com.boray.voicemcp;

import android.app.Activity;
import android.graphics.Color;
import android.graphics.Typeface;
import android.os.Bundle;
import android.text.TextUtils;
import android.view.Gravity;
import android.view.View;
import android.widget.LinearLayout;
import android.widget.TextView;
import android.widget.Toast;

import java.text.SimpleDateFormat;
import java.util.Date;
import java.util.List;
import java.util.Locale;

/**
 * 当前进度：展示小秘书整理的巡检简报（重大事项 / 交付成果 / 完成进度 / 风险）。
 *
 * 数据源 `boss_briefing`（异步 job → voice_job），进页面先显示上次缓存，再后台刷新。
 */
public class ProgressActivity extends Activity {

    private TextView tvUpdated;
    private TextView tvSummary;
    private TextView tvLoading;
    private TextView tvRefresh;
    private LinearLayout llProgress;
    private LinearLayout llRisks;
    private boolean loading = false;

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        setContentView(R.layout.activity_progress);
        WindowInsetsHelper.apply(this);

        View back = findViewById(R.id.btnBack);
        if (back != null) back.setOnClickListener(v -> finish());
        tvUpdated = findViewById(R.id.tvUpdated);
        tvSummary = findViewById(R.id.tvSummary);
        tvLoading = findViewById(R.id.tvLoading);
        tvRefresh = findViewById(R.id.tvRefresh);
        llProgress = findViewById(R.id.llProgress);
        llRisks = findViewById(R.id.llRisks);
        tvRefresh.setOnClickListener(v -> load(true));

        render(Briefing.cached(this), null);
        load(false);
    }

    private void load(boolean force) {
        if (loading) return;
        loading = true;
        tvLoading.setVisibility(View.VISIBLE);
        tvLoading.setText("小秘书正在巡检…（首次约 1–2 分钟）");
        Briefing cached = Briefing.cached(this);
        boolean fresh = cached.fetchedAt > 0
                && System.currentTimeMillis() - cached.fetchedAt < 10 * 60_000L;
        if (fresh && !force) {
            loading = false;
            tvLoading.setVisibility(View.GONE);
            return;
        }
        Briefing.refresh(this, (b, err) -> {
            loading = false;
            tvLoading.setVisibility(View.GONE);
            if (b != null) {
                render(b, null);
                Toast.makeText(this, "已更新小秘书简报", Toast.LENGTH_SHORT).show();
            } else {
                render(Briefing.cached(this), err);
            }
        });
    }

    private void render(Briefing b, String error) {
        if (isFinishing()) return;
        if (b == null || b.empty()) {
            tvSummary.setText(error == null || error.isEmpty()
                    ? "还没有简报，点右上角「刷新」让小秘书巡检一次。"
                    : "读取失败：" + error);
            tvUpdated.setText("小秘书简报 · 未同步");
            llProgress.removeAllViews();
            llRisks.removeAllViews();
            return;
        }
        if (!b.summary.isEmpty()) tvSummary.setText(b.summary);
        tvUpdated.setText("小秘书简报 · " + (b.fetchedAt > 0
                ? new SimpleDateFormat("MM-dd HH:mm", Locale.CHINA).format(new Date(b.fetchedAt))
                : "缓存"));
        fill(llProgress, b.progress, "暂无进展记录", false);
        fill(llRisks, b.risks, "暂无风险", true);
    }

    private void fill(LinearLayout box, List<String> items, String empty, boolean risk) {
        box.removeAllViews();
        if (items == null || items.isEmpty()) {
            box.addView(line(empty, Color.parseColor("#93A7A3"), false));
            return;
        }
        for (String s : items) {
            box.addView(line("· " + s, risk
                    ? Color.parseColor("#B23A3A") : Color.parseColor("#11201E"), false));
        }
    }

    private TextView line(String text, int color, boolean bold) {
        TextView tv = new TextView(this);
        tv.setText(text);
        tv.setTextSize(13f);
        tv.setTextColor(color);
        tv.setLineSpacing(6f, 1f);
        tv.setPadding(0, dp(6), 0, dp(6));
        tv.setGravity(Gravity.START);
        if (bold) tv.setTypeface(Typeface.DEFAULT_BOLD);
        tv.setEllipsize(TextUtils.TruncateAt.END);
        return tv;
    }

    private int dp(int v) {
        return Math.round(v * getResources().getDisplayMetrics().density);
    }
}
