package com.boray.voicemcp;

import android.content.Context;
import android.content.Intent;
import android.graphics.Typeface;
import android.net.Uri;
import android.text.SpannableStringBuilder;
import android.text.Spanned;
import android.text.TextPaint;
import android.text.method.LinkMovementMethod;
import android.text.style.ClickableSpan;
import android.text.style.ForegroundColorSpan;
import android.text.style.StyleSpan;
import android.util.AttributeSet;
import android.view.LayoutInflater;
import android.view.MotionEvent;
import android.view.View;
import android.view.ViewGroup;
import android.widget.LinearLayout;
import android.widget.ScrollView;
import android.widget.TextView;
import android.widget.Toast;

import org.json.JSONArray;
import org.json.JSONObject;

import java.util.ArrayList;
import java.util.List;
import java.util.regex.Matcher;
import java.util.regex.Pattern;

/**
 * 会话记录面板（首页 / 视频通话页共用）。
 *
 * <p>它不是一个「转写文本框」，而是与某个数字员工的 session 流水：
 * <ul>
 *   <li>数据来自 {@link SessionStore}（SQLite，按人 + 天长期保存），App 启动就加载当天；</li>
 *   <li>最新消息在<b>下面</b>，向上翻就是历史；翻到顶自动加载上一天；</li>
 *   <li>可以按住顶部把手上下拖动改高度，或在折叠 / 半展 / 全展三档之间吸附；</li>
 *   <li>正文里的 http(s) 链接、附件链接都可点击打开（浏览器 / 系统应用）。</li>
 * </ul>
 */
public final class SessionPanel extends LinearLayout {

    private static final int MAX_ROWS = 400;
    private static final int BAR_DP = 76;                  // 一条形态的高度

    private final ArrayList<String> days = new ArrayList<>();
    private String empId = "";
    private String empName = "";

    private View head;
    private TextView tvTitle;
    private TextView tvSub;
    private TextView tvToggle;
    private TextView tvStatus;
    private TextView tvLatest;
    private TextView tvTimer;
    private VoiceLevelView levelMini;
    private View btnHangup;
    private ScrollView svHistory;
    private LinearLayout llMessages;

    private boolean loading = false;
    private boolean noOlder = false;
    /** 每翻到顶部只允许再往前加载一天，避免一次性把全部历史拉出来 */
    private boolean canLoadOlder = true;
    private boolean autoScroll = true;
    /** 整页形态 */
    private boolean fullScreen = false;
    /** 内嵌在首页时：不占满，只显示最新一条，点右上角打开整页 */
    private boolean inlineMode = false;
    private Runnable onOpen;
    private Runnable onCollapsed;
    private Runnable onHangup;
    /** 通话中（用于决定头部是否显示时长/电平/挂断） */
    private boolean callLive = false;
    private int lastCount = 0;
    private int loadedDays = 1;

    private final Runnable updater = () -> post(this::refreshSafely);

    public SessionPanel(Context c) {
        this(c, null);
    }

    public SessionPanel(Context c, AttributeSet a) {
        super(c, a);
        init();
    }

    private void init() {
        setOrientation(VERTICAL);
        setBackgroundResource(R.drawable.bg_sheet);
        setElevation(dp(14));
        LayoutInflater.from(getContext()).inflate(R.layout.view_session_panel, this, true);
        head = findViewById(R.id.llHead);
        tvTitle = findViewById(R.id.tvTitle);
        tvSub = findViewById(R.id.tvSub);
        tvToggle = findViewById(R.id.tvToggle);
        tvStatus = findViewById(R.id.tvStatus);
        tvLatest = findViewById(R.id.tvLatest);
        tvTimer = findViewById(R.id.tvTimer);
        levelMini = findViewById(R.id.levelMini);
        btnHangup = findViewById(R.id.btnHangup);
        if (levelMini != null) {
            levelMini.setBarColor(color(R.color.primary));
            levelMini.setActive(true);
        }
        if (btnHangup != null) {
            btnHangup.setOnClickListener(v -> {
                if (onHangup != null) onHangup.run();
            });
        }
        svHistory = findViewById(R.id.svHistory);
        llMessages = findViewById(R.id.llMessages);
        setClipToPadding(false);

        tvToggle.setOnClickListener(v -> toggle());
        // 只有「一条」形态才允许点标题栏展开；整页里点空白/电平不能误触收起
        head.setOnClickListener(v -> {
            if (!fullScreen) toggle();
        });

        svHistory.setOnScrollChangeListener((v, x, y, oldX, oldY) -> {
            int max = Math.max(0, llMessages.getHeight() - svHistory.getHeight());
            autoScroll = y >= max - dp(24);
            if (y > dp(24)) canLoadOlder = true;
            if (y == 0 && !loading && !noOlder && canLoadOlder && days.size() > 0) {
                canLoadOlder = false;
                loadOlder();
            }
        });
        applyMode();
    }

    // ------------------------------------------------------------------ 对外

    /** 绑定当前汇报人；同一个人重复调用只刷新，不丢已加载的历史 */
    public void bind(String id, String name) {
        String newId = id == null ? "" : id;
        String newName = name == null ? "" : name;
        boolean changed = !newId.equals(empId) || !newName.equals(empName);
        empId = newId;
        empName = newName;
        if (changed || days.isEmpty()) {
            days.clear();
            days.add(SessionStore.today());
            noOlder = false;
            canLoadOlder = true;
            autoScroll = true;
        }
        SessionStore.addListener(updater);
        refresh();
    }

    /** 展开成整页会话（通话接通 / 点「打开」时） */
    public void openFull() {
        if (inlineMode) {
            if (onOpen != null) onOpen.run();
            return;
        }
        fullScreen = true;
        applyMode();
        android.util.Log.i("SessionPanel", "openFull live=" + callLive
                + " timer=" + (tvTimer != null && tvTimer.getVisibility() == VISIBLE)
                + " level=" + (levelMini != null && levelMini.getVisibility() == VISIBLE)
                + " hangup=" + (btnHangup != null && btnHangup.getVisibility() == VISIBLE));
        autoScroll = true;
        refresh();
        scrollToBottom(false);
    }

    /** 收回成一条（视频页 收起后停在一条上） */
    public void showBar() {
        fullScreen = false;
        applyMode();
        refreshCountOnly();
    }

    public boolean isFullScreen() {
        return fullScreen && !inlineMode;
    }

    /** 通话中的瞬时状态；空串即隐藏。状态不入库，避免污染历史 */
    public void setStatus(String text) {
        if (tvStatus == null) return;
        String t = text == null ? "" : text.trim();
        if (t.isEmpty()) {
            tvStatus.setVisibility(GONE);
        } else {
            tvStatus.setText(t);
            tvStatus.setVisibility(VISIBLE);
        }
    }

    /** 通话开始/结束：整页头部显示或隐藏 时长·电平·挂断 */
    public void setCallLive(boolean live) {
        callLive = live;
        applyCallChrome();
    }

    /**
     * 每 100ms / 1s 由宿主喂一次：时长文案 + 当前电平。
     *
     * 顺便做一次「兜底可见」：整页 + 通话中时强制这三件套处于 VISIBLE，
     * 避免因为打开顺序/回调时序导致它们被漏掉（例如先 openFull 后 setCallLive）。
     */
    public void setCallStatus(String duration, float level) {
        if (tvTimer != null && duration != null) tvTimer.setText(duration);
        if (levelMini != null) levelMini.setLevel(level);
        if (fullScreen && !inlineMode && callLive) {
            ensureVisible(tvTimer);
            ensureVisible(levelMini);
            ensureVisible(btnHangup);
        }
    }

    private static void ensureVisible(View v) {
        if (v != null && v.getVisibility() != VISIBLE) v.setVisibility(VISIBLE);
    }

    /** 整页头部的小挂断按钮：由宿主接到自己的挂断逻辑 */
    public void setOnHangup(Runnable r) {
        onHangup = r;
    }

    private void applyCallChrome() {
        boolean show = fullScreen && !inlineMode && callLive;
        if (tvTimer != null) tvTimer.setVisibility(show ? VISIBLE : GONE);
        if (levelMini != null) levelMini.setVisibility(show ? VISIBLE : GONE);
        if (btnHangup != null) btnHangup.setVisibility(show ? VISIBLE : GONE);
    }

    /** 头部条数文案：整页只写「N 条」，一条形态写「今天 · N 条」 */
    private void applyCountText() {
        if (tvSub == null) return;
        if (fullScreen && !inlineMode) {
            tvSub.setText(lastCount + " 条");
        } else if (loadedDays > 1) {
            tvSub.setText("已加载 " + loadedDays + " 天 · 共 " + lastCount + " 条");
        } else {
            tvSub.setText("今天 · " + lastCount + " 条");
        }
    }

    /** 内嵌模式：只有最新一条 + 右上角打开图标，点开就是整页 */
    public void setInlineMode(boolean inline) {
        inlineMode = inline;
        fullScreen = false;
        applyMode();
        refreshCountOnly();
    }

    /** 右上角「打开」被点：由宿主决定怎么呈现整页 */
    public void setOnOpen(Runnable r) {
        onOpen = r;
    }

    /** 整页里点「收起」：宿主可以把整页隐藏，回到主界面 */
    public void setOnCollapsed(Runnable r) {
        onCollapsed = r;
    }

    public void toggle() {
        if (inlineMode) {
            if (onOpen != null) onOpen.run();
            return;
        }
        if (fullScreen) {
            fullScreen = false;
            applyMode();
            refreshCountOnly();
            if (onCollapsed != null) onCollapsed.run();
        } else {
            openFull();
        }
    }

    /** 两种形态：整页（占满） / 一条（只有标题 + 最新一句） */
    private void applyMode() {
        boolean full = fullScreen && !inlineMode;
        ViewGroup.LayoutParams lp = getLayoutParams();
        if (lp != null) {
            int want = full ? ViewGroup.LayoutParams.MATCH_PARENT : dp(BAR_DP);
            if (lp.height != want) {
                lp.height = want;
                setLayoutParams(lp);
            }
        }
        if (svHistory != null) svHistory.setVisibility(full ? VISIBLE : GONE);
        if (tvLatest != null) tvLatest.setVisibility(full ? GONE : VISIBLE);

        if (tvToggle != null) {
            if (full) {
                // 整页只有一个动作：收起，回主界面
                tvToggle.setText("收起");
                tvToggle.setCompoundDrawablesWithIntrinsicBounds(0, 0, 0, 0);
                tvToggle.setContentDescription("收起会话");
                tvToggle.setPadding(dp(10), dp(4), dp(6), dp(4));
            } else {
                tvToggle.setText("");
                tvToggle.setContentDescription("打开会话记录");
                tvToggle.setCompoundDrawablesWithIntrinsicBounds(
                        0, 0, R.drawable.ic_open_panel, 0);
                tvToggle.setCompoundDrawablePadding(dp(4));
                tvToggle.setPadding(dp(10), dp(6), dp(10), dp(6));
            }
        }
        applyCallChrome();
        applyCountText();
        if (full) {
            setBackgroundColor(color(R.color.card));
            setElevation(0);
        } else if (inlineMode) {
            setBackgroundResource(R.drawable.bg_card);
            setElevation(0);
        } else {
            setBackgroundResource(R.drawable.bg_sheet);
            setElevation(dp(14));
        }
    }

    // ------------------------------------------------------------------ 数据

    @Override
    protected void onAttachedToWindow() {
        super.onAttachedToWindow();
        SessionStore.addListener(updater);
        refreshSafely();
    }

    @Override
    protected void onDetachedFromWindow() {
        super.onDetachedFromWindow();
        SessionStore.removeListener(updater);
    }

    private void refreshSafely() {
        refresh();
    }

    /** 重建整块列表：行数不多（按天分页，单次最多 400 行），比做 diff 稳 */
    public void refresh() {
        if (llMessages == null || loading) return;
        if (!fullScreen || inlineMode) {
            refreshCountOnly();
            return;
        }
        loading = true;
        try {
            boolean atBottom = autoScroll || isAtBottom();
            int oldY = svHistory.getScrollY();
            llMessages.removeAllViews();
            if (empId.isEmpty()) {
                addHint("还没有选择汇报人");
                tvSub.setText("未选择员工");
                return;
            }
            int total = 0;
            String lastDay = "";
            for (String day : days) {
                List<SessionStore.Row> rows = SessionStore.get(getContext()).day(empId, day, MAX_ROWS);
                total += rows.size();
                boolean needSep = days.size() > 1 || !day.equals(SessionStore.today()) || !rows.isEmpty();
                if (needSep && !day.equals(lastDay)) {
                    addDaySeparator(day);
                    lastDay = day;
                }
                for (SessionStore.Row r : rows) addRow(r);
            }
            if (total == 0) {
                addHint(SessionStore.today().equals(days.get(days.size() - 1))
                        ? "今天还没有对话。通话时这里会实时记录，并按人、按天长期保存。"
                        : "这一天没有记录。");
            }
            lastCount = total;
            loadedDays = Math.max(1, days.size());
            applyCountText();
            tvTitle.setText(empName.isEmpty() ? "会话记录" : empName);

            final boolean toBottom = atBottom;
            final int keepY = oldY;
            svHistory.post(() -> {
                if (toBottom) scrollToBottom(true);
                else svHistory.scrollTo(0, keepY);
            });
        } finally {
            loading = false;
        }
    }

    /** 折叠时只刷新标题上的条数，避免每次消息都重建几百行视图 */
    private void refreshCountOnly() {
        if (empId.isEmpty()) {
            tvSub.setText("未选择员工");
            return;
        }
        int total = 0;
        for (String day : days) total += SessionStore.get(getContext()).countDay(empId, day);
        lastCount = total;
        loadedDays = Math.max(1, days.size());
        applyCountText();
        tvTitle.setText(empName.isEmpty() ? "会话记录" : empName);
        updateLatest();
    }

    /** 折叠/内嵌时那一行：谁 + 最新一条内容 */
    private void updateLatest() {
        if (tvLatest == null) return;
        if (empId.isEmpty()) {
            tvLatest.setText("还没有选择汇报人");
            return;
        }
        SessionStore.Row r = SessionStore.get(getContext()).latest(empId, SessionStore.today());
        if (r == null) {
            tvLatest.setText("今天还没有对话");
            return;
        }
        String who = r.role == SessionStore.ROLE_USER
                ? "我" : (r.empName == null || r.empName.isEmpty() ? empName : r.empName);
        String text = r.text == null ? "" : r.text.replace('\n', ' ');
        if (text.length() > 80) text = text.substring(0, 80) + "…";
        tvLatest.setText(who + "：" + text);
    }

    private void loadOlder() {
        if (loading || days.isEmpty()) return;
        String older = SessionStore.get(getContext()).previousDay(empId, days.get(0));
        if (older.isEmpty()) {
            noOlder = true;
            addHint("已经是更早的全部记录了");
            return;
        }
        loading = true;
        final int oldH = llMessages.getHeight();
        days.add(0, older);
        loading = false;
        refresh();
        // 前插后把滚动位置补回去，视觉上「停在原处」
        svHistory.post(() -> {
            int delta = llMessages.getHeight() - oldH;
            if (delta > 0) svHistory.scrollTo(0, delta);
        });
    }

    private boolean isAtBottom() {
        int max = Math.max(0, llMessages.getHeight() - svHistory.getHeight());
        return svHistory.getScrollY() >= max - dp(24);
    }

    private void scrollToBottom(boolean smooth) {
        svHistory.post(() -> {
            if (smooth) svHistory.smoothScrollTo(0, Math.max(0, llMessages.getHeight()));
            else svHistory.fullScroll(View.FOCUS_DOWN);
        });
    }

    // ------------------------------------------------------------------ 行渲染

    private void addDaySeparator(String day) {
        TextView tv = new TextView(getContext());
        tv.setText(SessionStore.dayLabel(day) + " · " + day);
        tv.setTextSize(11);
        tv.setTextColor(color(R.color.text_hint));
        tv.setGravity(android.view.Gravity.CENTER);
        tv.setPadding(0, dp(10), 0, dp(6));
        llMessages.addView(tv);
    }

    private void addHint(String text) {
        TextView tv = new TextView(getContext());
        tv.setText(text);
        tv.setTextSize(13);
        tv.setTextColor(color(R.color.text_hint));
        tv.setPadding(0, dp(10), 0, dp(10));
        llMessages.addView(tv);
    }

    private void addRow(SessionStore.Row r) {
        boolean mine = r.role == SessionStore.ROLE_USER;
        String who = mine ? "我" : (r.empName == null || r.empName.isEmpty() ? empName : r.empName);

        SpannableStringBuilder b = new SpannableStringBuilder();
        int start = b.length();
        if (r.kind == SessionStore.KIND_SYSTEM) {
            b.append(r.text);
            mark(b, start, b.length(), color(R.color.text_hint), false, false);
        } else {
            b.append(mine ? "我" : (who.isEmpty() ? "汇报人" : who)).append("：");
            mark(b, start, b.length(), mine ? color(R.color.text_secondary) : color(R.color.primary), true, false);
            if (r.kind == SessionStore.KIND_DETAIL) {
                int ds = b.length();
                b.append("详情 ");
                mark(b, ds, b.length(), color(R.color.text_hint), true, false);
            }
            appendWithLinks(b, r.text, 1200);
        }

        TextView tv = new TextView(getContext());
        tv.setText(b);
        tv.setTextSize(r.kind == SessionStore.KIND_DETAIL ? 13 : (mine ? 14 : 15));
        tv.setTextColor(mine ? color(R.color.text_secondary) : color(R.color.text_primary));
        tv.setLineSpacing(dp(4), 1f);
        tv.setPadding(0, dp(5), 0, dp(5));
        if (containsLink(r.text)) tv.setMovementMethod(LinkMovementMethod.getInstance());
        tv.setHighlightColor(0x00000000);
        llMessages.addView(tv);

        addLinks(r.linksJson);
    }

    private void addLinks(String linksJson) {
        if (linksJson == null || linksJson.isEmpty() || "[]".equals(linksJson)) return;
        List<String[]> links = new ArrayList<>();
        try {
            JSONArray arr = new JSONArray(linksJson);
            for (int i = 0; i < arr.length(); i++) {
                JSONObject o = arr.optJSONObject(i);
                if (o == null) continue;
                String url = o.optString("url", "");
                if (url.isEmpty()) continue;
                links.add(new String[]{o.optString("name", "附件"), url});
            }
        } catch (Exception ignored) {
        }
        if (links.isEmpty()) return;
        LinearLayout row = new LinearLayout(getContext());
        row.setOrientation(HORIZONTAL);
        row.setPadding(0, 0, 0, dp(6));
        for (String[] l : links) {
            TextView chip = new TextView(getContext());
            chip.setText(l[0]);
            chip.setTextSize(11);
            chip.setSingleLine(true);
            chip.setTextColor(color(R.color.primary));
            chip.setBackgroundResource(R.drawable.bg_badge);
            chip.setPadding(dp(10), dp(5), dp(10), dp(5));
            LinearLayout.LayoutParams lp = new LinearLayout.LayoutParams(
                    ViewGroup.LayoutParams.WRAP_CONTENT, ViewGroup.LayoutParams.WRAP_CONTENT);
            lp.rightMargin = dp(8);
            chip.setLayoutParams(lp);
            final String url = l[1];
            chip.setOnClickListener(v -> openUrl(url));
            row.addView(chip);
        }
        llMessages.addView(row);
    }

    private void mark(SpannableStringBuilder b, int s, int e, int color, boolean bold, boolean underline) {
        b.setSpan(new ForegroundColorSpan(color), s, e, Spanned.SPAN_EXCLUSIVE_EXCLUSIVE);
        if (bold) b.setSpan(new StyleSpan(Typeface.BOLD), s, e, Spanned.SPAN_EXCLUSIVE_EXCLUSIVE);
        if (underline) b.setSpan(new android.text.style.UnderlineSpan(), s, e, Spanned.SPAN_EXCLUSIVE_EXCLUSIVE);
    }

    private static final Pattern URL = Pattern.compile("(https?://[^\\s，。；、）)\\]】\"'<>]+)");

    private boolean containsLink(String text) {
        return text != null && URL.matcher(text).find();
    }

    /** 把正文里的链接标成可点：点开浏览器 / 系统应用，不在 App 内解析 */
    private void appendWithLinks(SpannableStringBuilder b, String text, int max) {
        if (text == null) return;
        String t = text.length() > max ? text.substring(0, max) + "…" : text;
        Matcher m = URL.matcher(t);
        int last = 0;
        while (m.find()) {
            b.append(t, last, m.start());
            int s = b.length();
            String url = m.group(1);
            b.append(url);
            b.setSpan(new LinkSpan(url), s, b.length(), Spanned.SPAN_EXCLUSIVE_EXCLUSIVE);
            last = m.end();
        }
        b.append(t, last, t.length());
    }

    private final class LinkSpan extends ClickableSpan {
        private final String url;

        LinkSpan(String url) {
            this.url = url;
        }

        @Override
        public void updateDrawState(TextPaint ds) {
            ds.setColor(color(R.color.primary_bright));
            ds.setUnderlineText(true);
        }

        @Override
        public void onClick(View widget) {
            openUrl(url);
        }
    }

    private void openUrl(String url) {
        String u = url == null ? "" : url.trim();
        if (!u.startsWith("http://") && !u.startsWith("https://")) {
            Toast.makeText(getContext(), "这个链接不是网页地址", Toast.LENGTH_SHORT).show();
            return;
        }
        try {
            Intent i = new Intent(Intent.ACTION_VIEW, Uri.parse(u));
            i.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK);
            getContext().startActivity(i);
        } catch (Exception e) {
            Toast.makeText(getContext(), "没有能打开这个链接的应用", Toast.LENGTH_SHORT).show();
        }
    }

    private int color(int res) {
        return getResources().getColor(res, getContext().getTheme());
    }

    private int dp(int v) {
        return Math.round(v * getResources().getDisplayMetrics().density);
    }
}
