package com.boray.voicemcp;

import android.Manifest;
import android.app.Activity;
import android.content.Intent;
import android.content.pm.PackageManager;
import android.graphics.Bitmap;
import android.net.Uri;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.view.View;
import android.widget.Button;
import android.widget.HorizontalScrollView;
import android.widget.ImageView;
import android.widget.LinearLayout;
import android.widget.ProgressBar;
import android.widget.TextView;
import android.widget.Toast;

import java.util.Locale;

/**
 * 实时语音通话控制面板（通话本体在 {@link VoiceCallService}，切页面/后台都不中断）。
 *
 * 这里不展示视频：只显示员工头像、音量电平、实时字幕与通话时长。
 * 接通后「挂断」按钮就是红色按钮，首页头部那个通话图标也会同步变红。
 */
public class VoiceCallActivity extends Activity {

    private static final int REQ_MIC = 4301;

    private TextView tvWho;
    private TextView tvState;
    private TextView tvCaption;
    private TextView tvHint;
    private ImageView ivAvatar;
    private ImageView ivMic;
    private ProgressBar pbLevel;
    private Button btnHangup;
    private TextView tvDetail;
    private HorizontalScrollView svAtts;
    private LinearLayout llAtts;
    private String lastAttKey = "";

    private final Handler ui = new Handler(Looper.getMainLooper());
    private boolean started = false;

    private final Runnable tick = new Runnable() {
        @Override
        public void run() {
            render();
            ScreenOn.keep(VoiceCallActivity.this, VoiceCallService.isActive());
            ui.postDelayed(this, 400);
        }
    };

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        setContentView(R.layout.activity_voice_call);
        WindowInsetsHelper.apply(this);

        View back = findViewById(R.id.btnBack);
        if (back != null) back.setOnClickListener(v -> finish());
        tvWho = findViewById(R.id.tvWho);
        tvState = findViewById(R.id.tvState);
        tvCaption = findViewById(R.id.tvCaption);
        tvHint = findViewById(R.id.tvHint);
        ivAvatar = findViewById(R.id.ivAvatar);
        ivMic = findViewById(R.id.ivMic);
        pbLevel = findViewById(R.id.pbLevel);
        btnHangup = findViewById(R.id.btnHangup);
        tvDetail = findViewById(R.id.tvDetail);
        svAtts = findViewById(R.id.svAtts);
        llAtts = findViewById(R.id.llAtts);

        btnHangup.setOnClickListener(v -> {
            VoiceCallService.stop(this);
            finish();
        });

        // 头像用小秘书（或当前汇报人）的照片
        String id = VoiceCallService.who().isEmpty()
                ? Briefing.SECRETARY_ID : Briefing.SECRETARY_ID;
        Employee emp = EmployeeStore.byId(EmployeeStore.load(this), id);
        tvWho.setText(emp == null ? "小秘书" : emp.name + " · " + emp.title);
        String poster = emp == null ? "" : (emp.photo.isEmpty() ? emp.faceUrl : emp.photo);
        EmployeeStore.loadPhotoBitmap(this, poster, new EmployeeStore.BitmapCallback() {
            @Override
            public void onLoaded(Bitmap bmp) {
                if (!isFinishing() && bmp != null) ivAvatar.setImageBitmap(bmp);
            }
        });

        if (VoiceCallService.isActive()) {
            started = true;
            render();
        } else {
            if (checkSelfPermission(Manifest.permission.RECORD_AUDIO)
                    != PackageManager.PERMISSION_GRANTED) {
                requestPermissions(new String[]{Manifest.permission.RECORD_AUDIO}, REQ_MIC);
                tvCaption.setText("需要麦克风权限才能通话");
                return;
            }
            VoiceCallService.start(this);
            started = true;
            tvCaption.setText("正在接通实时语音通道…");
        }
    }

    @Override
    public void onRequestPermissionsResult(int requestCode, String[] permissions, int[] results) {
        super.onRequestPermissionsResult(requestCode, permissions, results);
        if (requestCode == REQ_MIC) {
            if (results.length > 0 && results[0] == PackageManager.PERMISSION_GRANTED) {
                VoiceCallService.start(this);
                started = true;
            } else {
                Toast.makeText(this, "没有麦克风权限无法通话", Toast.LENGTH_LONG).show();
                finish();
            }
        }
    }

    private void render() {
        if (isFinishing()) return;
        boolean active = VoiceCallService.isActive();
        boolean connected = VoiceCallService.isConnected();
        tvState.setText(connected ? "通话中" : (active ? "接通中" : "已结束"));

        if (!active) {
            tvCaption.setText("通话已结束。");
            btnHangup.setText("返回");
            btnHangup.setOnClickListener(v -> finish());
            ivMic.setAlpha(0.4f);
            pbLevel.setProgress(0);
            ui.removeCallbacks(tick);
            return;
        }

        String s = VoiceCallService.status();
        tvHint.setText(connected
                ? "直接说话即可，说完会自动发送；员工的话从这路实时流直接播出来。"
                : s);
        String cap = VoiceCallService.caption();
        if (cap != null && !cap.isEmpty()) tvCaption.setText(cap);
        renderReply();

        float lv = VoiceCallService.micLevel();
        pbLevel.setProgress(Math.round(Math.min(1f, lv * 2.4f) * 100));
        ivMic.setAlpha(VoiceCallService.isSpeaking() ? 1f : 0.55f);

        if (connected && VoiceCallService.connectedAt() > 0) {
            long sec = (System.currentTimeMillis() - VoiceCallService.connectedAt()) / 1000;
            tvWho.setText(String.format(Locale.CHINA, "已通话 %02d:%02d", sec / 60, sec % 60));
        }
    }

    /** 文字详情 + 附件：详情是 reply.text（不朗读），附件点开用系统浏览器 */
    private void renderReply() {
        CallReply r = VoiceCallService.lastReply();
        String detail = r == null ? "" : r.detail;
        if (r != null && detail.equals(r.voiceText)) detail = "";   // 和语音稿一样就不重复
        if (detail.isEmpty()) {
            tvDetail.setVisibility(View.GONE);
        } else {
            tvDetail.setText(detail);
            tvDetail.setVisibility(View.VISIBLE);
        }

        StringBuilder key = new StringBuilder();
        if (r != null) for (CallReply.Attachment a : r.attachments) key.append(a.url).append('|');
        String k = key.toString();
        if (k.equals(lastAttKey)) return;
        lastAttKey = k;
        llAtts.removeAllViews();
        boolean show = r != null && !r.attachments.isEmpty();
        svAtts.setVisibility(show ? View.VISIBLE : View.GONE);
        if (!show) return;
        int padH = Math.round(12 * getResources().getDisplayMetrics().density);
        int padV = Math.round(7 * getResources().getDisplayMetrics().density);
        int gap = Math.round(8 * getResources().getDisplayMetrics().density);
        for (CallReply.Attachment a : r.attachments) {
            TextView chip = new TextView(this);
            chip.setText(a.name);
            chip.setTextSize(12);
            chip.setSingleLine(true);
            chip.setTextColor(getResources().getColor(R.color.primary, getTheme()));
            chip.setBackgroundResource(R.drawable.bg_badge);
            chip.setPadding(padH, padV, padH, padV);
            LinearLayout.LayoutParams lp = new LinearLayout.LayoutParams(
                    LinearLayout.LayoutParams.WRAP_CONTENT,
                    LinearLayout.LayoutParams.WRAP_CONTENT);
            lp.rightMargin = gap;
            chip.setLayoutParams(lp);
            chip.setOnClickListener(v -> {
                String u = a.url.trim();
                if (!u.startsWith("http://") && !u.startsWith("https://")) {
                    Toast.makeText(this, "这个附件不是网页链接", Toast.LENGTH_SHORT).show();
                    return;
                }
                try {
                    startActivity(new Intent(Intent.ACTION_VIEW, Uri.parse(u)));
                } catch (Exception e) {
                    Toast.makeText(this, "没有能打开这个链接的应用", Toast.LENGTH_SHORT).show();
                }
            });
            llAtts.addView(chip);
        }
    }

    @Override
    protected void onResume() {
        super.onResume();
        ScreenOn.keep(this, VoiceCallService.isActive());
        ui.removeCallbacks(tick);
        ui.post(tick);
    }

    @Override
    protected void onPause() {
        super.onPause();
        ui.removeCallbacks(tick);
    }

    @Override
    protected void onDestroy() {
        super.onDestroy();
        ScreenOn.keep(this, false);
        ui.removeCallbacksAndMessages(null);
        // 只关面板，不挂断：通话在 Service 里继续，可回首页点红色图标结束
    }
}
