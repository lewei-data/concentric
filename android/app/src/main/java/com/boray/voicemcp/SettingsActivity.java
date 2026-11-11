package com.boray.voicemcp;

import android.app.Activity;
import android.app.AlertDialog;
import android.content.Intent;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.widget.Button;
import android.widget.CompoundButton;
import android.widget.EditText;
import android.widget.TextView;
import android.widget.Toast;

import org.json.JSONArray;
import org.json.JSONObject;

import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;

/** 连接设置：端点 URL、工具名、参数名、认证、超时、演示模式，以及连通性/工具发现测试 */
public class SettingsActivity extends Activity {

    private final ExecutorService executor = Executors.newSingleThreadExecutor();
    private final Handler ui = new Handler(Looper.getMainLooper());

    private EditText etUrl, etTool, etAudioKey, etLanguage, etModel, etVoice, etProfile;
    private EditText etToken, etProtocol, etTimeout, etListenInterval, etMailUrl;
    private EditText etAvatarId, etMode, etBandwidth;
    private CompoundButton cbAutoPlay, cbDemo, cbReport, cbReportVideo;
    private CompoundButton cbKeep, cbAsync, cbSse, cbMail, cbDelegate, cbAutoSync;
    private TextView tvProbe;
    private McpConfig cfg;

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        setContentView(R.layout.activity_settings);
        WindowInsetsHelper.apply(this);

        etUrl = findViewById(R.id.etUrl);
        etTool = findViewById(R.id.etTool);
        etAudioKey = findViewById(R.id.etAudioKey);
        etLanguage = findViewById(R.id.etLanguage);
        etModel = findViewById(R.id.etModel);
        etVoice = findViewById(R.id.etVoice);
        etProfile = findViewById(R.id.etProfile);
        etToken = findViewById(R.id.etToken);
        etProtocol = findViewById(R.id.etProtocol);
        etTimeout = findViewById(R.id.etTimeout);
        cbAutoPlay = findViewById(R.id.cbAutoPlay);
        cbDemo = findViewById(R.id.cbDemo);
        cbReport = findViewById(R.id.cbReport);
        cbReportVideo = findViewById(R.id.cbReportVideo);
        cbKeep = findViewById(R.id.cbKeep);
        cbAsync = findViewById(R.id.cbAsync);
        cbSse = findViewById(R.id.cbSse);
        cbMail = findViewById(R.id.cbMail);
        etListenInterval = findViewById(R.id.etListenInterval);
        etMailUrl = findViewById(R.id.etMailUrl);
        etAvatarId = findViewById(R.id.etAvatarId);
        etMode = findViewById(R.id.etMode);
        etBandwidth = findViewById(R.id.etBandwidth);
        cbDelegate = findViewById(R.id.cbDelegate);
        cbAutoSync = findViewById(R.id.cbAutoSync);
        tvProbe = findViewById(R.id.tvProbe);

        findViewById(R.id.btnBack).setOnClickListener(v -> finish());
        findViewById(R.id.btnEmployees).setOnClickListener(v ->
                startActivity(new Intent(this, EmployeeListActivity.class)));
        findViewById(R.id.btnSyncProfile).setOnClickListener(v -> {
            McpConfig snapshot = read();
            tvProbe.setText("正在调用 profile 同步员工档案…");
            EmployeeStore.syncFromServer(this, snapshot, (count, error) -> {
                tvProbe.setText(error.isEmpty()
                        ? "同步成功：" + count + " 位员工\n" + EmployeeStore.syncStatus(this)
                        : "同步失败：" + error + "\n继续使用本机缓存档案");
            });
        });

        cfg = McpConfig.load(this);
        fill(cfg);

        findViewById(R.id.btnSave).setOnClickListener(v -> {
            McpConfig next = read();
            if (!next.url.startsWith("http://") && !next.url.startsWith("https://")) {
                toast("端点 URL 必须以 http:// 或 https:// 开头");
                return;
            }
            if (next.tool.isEmpty() || next.audioKey.isEmpty()) {
                toast("工具名和音频参数名不能为空");
                return;
            }
            next.save(this);
            cfg = next;
            // 监听开关 / 端点变化立即生效，不必重启 App
            if (next.keepListening && !next.demo) ListenService.start(this);
            else ListenService.stop(this);
            toast("已保存，监听配置立即生效");
            finish();
        });

        findViewById(R.id.btnReset).setOnClickListener(v -> {
            fill(new McpConfig());
            toast("已填入默认值，点「保存设置」生效");
        });

        findViewById(R.id.btnTest).setOnClickListener(v -> {
            final McpConfig snapshot = read();
            tvProbe.setText("正在连接 " + snapshot.url + " …");
            executor.execute(() -> {
                try {
                    McpClient client = new McpClient(snapshot);
                    client.connect();
                    JSONArray tools = client.listTools();
                    String info = "连接成功\nserver：" + client.serverName + " " + client.serverVersion
                            + "\n协议：" + client.negotiatedProtocol
                            + "\n可用工具数：" + tools.length();
                    ui.post(() -> tvProbe.setText(info));
                } catch (Exception e) {
                    ui.post(() -> tvProbe.setText("连接失败：" + e.getMessage()));
                }
            });
        });

        findViewById(R.id.btnTools).setOnClickListener(v -> {
            final McpConfig snapshot = read();
            tvProbe.setText("正在读取工具清单…");
            executor.execute(() -> {
                try {
                    McpClient client = new McpClient(snapshot);
                    JSONArray tools = client.listTools();
                    int n = tools.length();
                    String[] names = new String[n];
                    StringBuilder detail = new StringBuilder();
                    for (int i = 0; i < n; i++) {
                        JSONObject t = tools.optJSONObject(i);
                        names[i] = t == null ? "?" : t.optString("name", "?");
                        if (t != null) {
                            detail.append("• ").append(names[i]).append(" — ")
                                    .append(t.optString("description", "")).append('\n');
                        }
                    }
                    ui.post(() -> {
                        tvProbe.setText(detail.toString());
                        new AlertDialog.Builder(this)
                                .setTitle("服务端工具（点选填入工具名）")
                                .setItems(names, (dialog, which) -> etTool.setText(names[which]))
                                .setNegativeButton("关闭", null)
                                .show();
                    });
                } catch (Exception e) {
                    ui.post(() -> tvProbe.setText("读取工具失败：" + e.getMessage()));
                }
            });
        });
    }

    @Override
    protected void onDestroy() {
        super.onDestroy();
        executor.shutdownNow();
    }

    private void fill(McpConfig c) {
        etUrl.setText(c.url);
        etTool.setText(c.tool);
        etAudioKey.setText(c.audioKey);
        etLanguage.setText(c.language);
        etModel.setText(c.model);
        etVoice.setText(c.voice);
        etProfile.setText(c.profile);
        etToken.setText(c.token);
        etProtocol.setText(c.protocol);
        etTimeout.setText(String.valueOf(c.timeoutSec));
        cbAutoPlay.setChecked(c.autoPlay);
        cbDemo.setChecked(c.demo);
        cbReport.setChecked(c.reportEnabled);
        cbReportVideo.setChecked(c.reportVideo);
        cbKeep.setChecked(c.keepListening);
        cbAsync.setChecked(c.asyncMode);
        cbSse.setChecked(c.listenSse);
        cbMail.setChecked(c.listenMail);
        etListenInterval.setText(String.valueOf(c.listenIntervalSec));
        etMailUrl.setText(c.mailUrl);
        etAvatarId.setText(c.avatarId);
        etMode.setText(c.mode);
        etBandwidth.setText(String.valueOf(c.bandwidthKbps));
        cbDelegate.setChecked(c.delegate);
        cbAutoSync.setChecked(c.autoSyncProfile);
    }

    private McpConfig read() {
        McpConfig c = new McpConfig();
        c.url = text(etUrl);
        c.tool = text(etTool);
        c.audioKey = text(etAudioKey);
        c.language = text(etLanguage);
        c.model = text(etModel);
        c.voice = text(etVoice);
        c.profile = text(etProfile);
        c.token = text(etToken);
        c.protocol = text(etProtocol);
        try {
            c.timeoutSec = Integer.parseInt(text(etTimeout));
        } catch (NumberFormatException e) {
            c.timeoutSec = 600;
        }
        c.timeoutSec = Math.max(30, Math.min(c.timeoutSec, 900));
        c.autoPlay = cbAutoPlay.isChecked();
        c.demo = cbDemo.isChecked();
        c.reportEnabled = cbReport.isChecked();
        c.reportVideo = cbReportVideo.isChecked();
        c.keepListening = cbKeep.isChecked();
        c.asyncMode = cbAsync.isChecked();
        c.listenSse = cbSse.isChecked();
        c.listenMail = cbMail.isChecked();
        try {
            c.listenIntervalSec = Integer.parseInt(text(etListenInterval));
        } catch (NumberFormatException e) {
            c.listenIntervalSec = 20;
        }
        c.listenIntervalSec = Math.max(5, Math.min(c.listenIntervalSec, 60));
        c.mailUrl = text(etMailUrl).isEmpty() ? McpConfig.MAIL_URL : text(etMailUrl);
        c.avatarId = text(etAvatarId);
        c.mode = text(etMode).isEmpty() ? "auto" : text(etMode).toLowerCase();
        try {
            c.bandwidthKbps = Integer.parseInt(text(etBandwidth));
        } catch (NumberFormatException e) {
            c.bandwidthKbps = 0;
        }
        c.bandwidthKbps = Math.max(0, Math.min(c.bandwidthKbps, 1000000));
        c.delegate = cbDelegate.isChecked();
        c.autoSyncProfile = cbAutoSync.isChecked();
        return c;
    }

    private static String text(EditText e) {
        return e.getText() == null ? "" : e.getText().toString().trim();
    }

    private void toast(String s) {
        Toast.makeText(this, s, Toast.LENGTH_SHORT).show();
    }
}
