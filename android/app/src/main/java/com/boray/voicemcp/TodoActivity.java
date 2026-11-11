package com.boray.voicemcp;

import android.app.Activity;
import android.content.SharedPreferences;
import android.graphics.Color;
import android.graphics.drawable.GradientDrawable;
import android.os.Bundle;
import android.view.Gravity;
import android.view.View;
import android.widget.Button;
import android.widget.LinearLayout;
import android.widget.TextView;
import android.widget.Toast;

import java.text.SimpleDateFormat;
import java.util.Date;
import java.util.Locale;

/**
 * 待办事项：小秘书简报里「需要你拍板」的条目，逐条审批。
 *
 * 点「通过 / 不通过」不是本地改状态就完事，而是把决定合成语音发给小秘书
 * （tts_synthesize → voice_chat），由她真正执行；本机只记「已提交」，
 * 真正的状态以服务端下一轮简报为准。
 */
public class TodoActivity extends Activity {

    private static final String PREF = "briefing_todos";

    private LinearLayout llTodos;
    private TextView tvEmpty;
    private TextView tvSub;
    private TextView tvLoading;
    private boolean loading = false;

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        setContentView(R.layout.activity_todo);
        WindowInsetsHelper.apply(this);

        View back = findViewById(R.id.btnBack);
        if (back != null) back.setOnClickListener(v -> finish());
        llTodos = findViewById(R.id.llTodos);
        tvEmpty = findViewById(R.id.tvEmpty);
        tvSub = findViewById(R.id.tvSub);
        tvLoading = findViewById(R.id.tvLoading);
        findViewById(R.id.tvRefresh).setOnClickListener(v -> load(true));

        render(Briefing.cached(this), null);
        load(false);
    }

    private void load(boolean force) {
        if (loading) return;
        loading = true;
        tvLoading.setVisibility(View.VISIBLE);
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
                Toast.makeText(this, "待办已同步", Toast.LENGTH_SHORT).show();
            } else {
                render(Briefing.cached(this), err);
            }
        });
    }

    private void render(Briefing b, String error) {
        if (isFinishing()) return;
        llTodos.removeAllViews();
        if (b == null || b.todos.isEmpty()) {
            tvEmpty.setVisibility(View.VISIBLE);
            tvEmpty.setText(error == null || error.isEmpty()
                    ? "暂无待拍板事项。\n小秘书巡检后，需要你决定的事会出现在这里。"
                    : "读取失败：" + error + "\n可点右上角「刷新」重试。");
            tvSub.setText("需要老板拍板的事项 · 未同步");
            return;
        }
        tvEmpty.setVisibility(View.GONE);
        int urgent = 0;
        for (Briefing.Todo t : b.todos) if (t.urgent()) urgent++;
        tvSub.setText("共 " + b.todos.size() + " 项"
                + (urgent > 0 ? " · 急件 " + urgent : "")
                + (b.fetchedAt > 0 ? " · " + new SimpleDateFormat("MM-dd HH:mm", Locale.CHINA)
                .format(new Date(b.fetchedAt)) : ""));

        for (Briefing.Todo t : b.todos) {
            llTodos.addView(card(t));
        }
    }

    private View card(Briefing.Todo t) {
        LinearLayout card = new LinearLayout(this);
        card.setOrientation(LinearLayout.VERTICAL);
        GradientDrawable bg = new GradientDrawable();
        bg.setColor(Color.parseColor("#FFFFFF"));
        bg.setCornerRadius(dp(18));
        bg.setStroke(dp(1), Color.parseColor("#DCEAE6"));
        card.setBackground(bg);
        card.setPadding(dp(16), dp(14), dp(16), dp(14));
        LinearLayout.LayoutParams lp = new LinearLayout.LayoutParams(
                LinearLayout.LayoutParams.MATCH_PARENT, LinearLayout.LayoutParams.WRAP_CONTENT);
        lp.bottomMargin = dp(12);
        card.setLayoutParams(lp);

        LinearLayout top = new LinearLayout(this);
        top.setOrientation(LinearLayout.HORIZONTAL);
        top.setGravity(Gravity.CENTER_VERTICAL);

        TextView badge = new TextView(this);
        String level = t.urgent() ? "急" : (t.level.isEmpty() ? "待办" : t.level);
        badge.setText(level);
        badge.setTextSize(10f);
        badge.setTextColor(Color.WHITE);
        GradientDrawable badgeBg = new GradientDrawable();
        badgeBg.setColor(t.urgent() ? Color.parseColor("#D93B3B") : Color.parseColor("#0FA694"));
        badgeBg.setCornerRadius(dp(9));
        badge.setBackground(badgeBg);
        badge.setPadding(dp(8), dp(3), dp(8), dp(3));
        top.addView(badge);

        TextView title = new TextView(this);
        title.setText(t.title);
        title.setTextSize(14f);
        title.setTextColor(Color.parseColor("#11201E"));
        title.setTypeface(android.graphics.Typeface.DEFAULT_BOLD);
        LinearLayout.LayoutParams tlp = new LinearLayout.LayoutParams(0,
                LinearLayout.LayoutParams.WRAP_CONTENT, 1f);
        tlp.leftMargin = dp(8);
        title.setLayoutParams(tlp);
        top.addView(title);
        card.addView(top);

        if (!t.suggestion.isEmpty()) {
            TextView sug = new TextView(this);
            sug.setText("小秘书" + t.suggestion);
            sug.setTextSize(12f);
            sug.setTextColor(Color.parseColor("#006C63"));
            sug.setPadding(0, dp(8), 0, 0);
            card.addView(sug);
        }

        if (!t.detail.isEmpty()) {
            TextView d = new TextView(this);
            d.setText(t.detail);
            d.setTextSize(12f);
            d.setTextColor(Color.parseColor("#5B706C"));
            d.setLineSpacing(dp(3), 1f);
            d.setPadding(0, dp(8), 0, 0);
            card.addView(d);
        }

        SharedPreferences p = getSharedPreferences(PREF, MODE_PRIVATE);
        String decided = p.getString(t.id, "");

        LinearLayout actions = new LinearLayout(this);
        actions.setOrientation(LinearLayout.HORIZONTAL);
        actions.setPadding(0, dp(12), 0, 0);

        if (!decided.isEmpty()) {
            TextView ok = new TextView(this);
            ok.setText("pass".equals(decided) ? "✓ 已提交：通过（等小秘书执行）"
                    : "✓ 已提交：不通过（等小秘书处理）");
            ok.setTextSize(12f);
            ok.setTextColor("pass".equals(decided)
                    ? Color.parseColor("#006C63") : Color.parseColor("#B23A3A"));
            ok.setPadding(0, dp(4), 0, dp(4));
            actions.addView(ok);
            card.addView(actions);
            return card;
        }

        Button pass = pill("通过", true);
        Button reject = pill("不通过", false);
        LinearLayout.LayoutParams b1 = new LinearLayout.LayoutParams(0, dp(42), 1f);
        b1.rightMargin = dp(6);
        pass.setLayoutParams(b1);
        LinearLayout.LayoutParams b2 = new LinearLayout.LayoutParams(0, dp(42), 1f);
        b2.leftMargin = dp(6);
        reject.setLayoutParams(b2);
        actions.addView(pass);
        actions.addView(reject);
        card.addView(actions);

        pass.setOnClickListener(v -> decide(t, true, pass, reject));
        reject.setOnClickListener(v -> decide(t, false, pass, reject));
        return card;
    }

    private void decide(Briefing.Todo t, boolean ok, Button pass, Button reject) {
        pass.setEnabled(false);
        reject.setEnabled(false);
        Toast.makeText(this, ok ? "正在提交「通过」给小秘书…" : "正在提交「不通过」给小秘书…",
                Toast.LENGTH_SHORT).show();
        Briefing.decide(this, t, ok, (done, msg) -> {
            if (isFinishing()) return;
            if (done) {
                getSharedPreferences(PREF, MODE_PRIVATE).edit()
                        .putString(t.id, ok ? "pass" : "reject").apply();
                Toast.makeText(this, msg.length() > 90 ? msg.substring(0, 90) + "…" : msg,
                        Toast.LENGTH_LONG).show();
                render(Briefing.cached(this), null);
            } else {
                pass.setEnabled(true);
                reject.setEnabled(true);
                Toast.makeText(this, "提交失败：" + msg, Toast.LENGTH_LONG).show();
            }
        });
    }

    private Button pill(String text, boolean primary) {
        Button b = new Button(this);
        b.setText(text);
        b.setAllCaps(false);
        b.setTextSize(13f);
        GradientDrawable bg = new GradientDrawable();
        if (primary) {
            bg.setColor(Color.parseColor("#006C63"));
            b.setTextColor(Color.WHITE);
        } else {
            bg.setColor(Color.parseColor("#FBE7E7"));
            b.setTextColor(Color.parseColor("#B23A3A"));
        }
        bg.setCornerRadius(dp(21));
        b.setBackground(bg);
        b.setStateListAnimator(null);
        return b;
    }

    private int dp(int v) {
        return Math.round(v * getResources().getDisplayMetrics().density);
    }
}
