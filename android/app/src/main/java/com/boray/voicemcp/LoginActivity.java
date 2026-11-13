package com.boray.voicemcp;

import android.app.Activity;
import android.app.AlertDialog;
import android.content.Intent;
import android.content.pm.PackageManager;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.widget.Button;
import android.widget.EditText;
import android.widget.TextView;

import java.util.List;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;

/**
 * 启动入口（0.12.0）。
 *
 * <p>启动时先读本机号码：
 * <ul>
 *   <li>一个号码 → 弹确认「是否使用本机号码登录」，确认后自动填好账号；</li>
 *   <li>双卡多个号码 → 列出候选让用户选一个，也可选「手动输入其他号码」；</li>
 *   <li>读不到 / 拒绝权限 → 直接手动输入手机号。</li>
 * </ul>
 * 本机号码只用于<b>自动填账号</b>，服务端没有免密登录，密码必须输入。
 * 登录走工作台 {@code POST /api/login {account, password}}，成功后带 {@code opc_sess}
 * 会话进入主界面；已有有效会话则直接进主界面（离线时不锁死）。
 */
public class LoginActivity extends Activity {

    private final ExecutorService io = Executors.newSingleThreadExecutor();
    private final Handler ui = new Handler(Looper.getMainLooper());

    private EditText etPhone, etPassword, etAuthBase;
    private TextView tvStatus;
    private Button btnLogin, btnLocal;

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        AuthSession.prime(this);
        if (AuthSession.isLoggedIn(this)) {
            goMain();
            return;
        }

        setContentView(R.layout.activity_login);
        WindowInsetsHelper.apply(this);

        etPhone = findViewById(R.id.etPhone);
        etPassword = findViewById(R.id.etPassword);
        etAuthBase = findViewById(R.id.etAuthBase);
        tvStatus = findViewById(R.id.tvLoginStatus);
        btnLogin = findViewById(R.id.btnLogin);
        btnLocal = findViewById(R.id.btnLocalNumber);

        etAuthBase.setText(McpConfig.load(this).authBase);
        String last = AuthSession.lastPhone(this);
        if (!last.isEmpty()) etPhone.setText(last);
        setStatus("正在检查本机号码…");

        btnLocal.setOnClickListener(v -> chooseLocalNumber());
        btnLogin.setOnClickListener(v -> doLogin());

        if (PhoneNumbers.hasPermission(this)) {
            chooseLocalNumber();
        } else {
            requestPermissions(PhoneNumbers.permissions(), PhoneNumbers.REQ_PERMISSION);
        }
    }

    @Override
    public void onRequestPermissionsResult(int requestCode, String[] permissions, int[] results) {
        super.onRequestPermissionsResult(requestCode, permissions, results);
        if (requestCode != PhoneNumbers.REQ_PERMISSION) return;
        boolean granted = results != null && results.length > 0
                && results[0] == PackageManager.PERMISSION_GRANTED;
        if (!granted) {
            setStatus("未授予读取本机号码权限，可以直接手动输入手机号。");
            return;
        }
        chooseLocalNumber();
    }

    /** 单个号码 → 确认框；多个号码 → 选择列表；没有 → 提示手动输入 */
    private void chooseLocalNumber() {
        final List<PhoneNumbers.Candidate> list = PhoneNumbers.read(this);
        if (list.isEmpty()) {
            setStatus("未读到本机号码（SIM 未写入号码或权限未授予），请手动输入手机号。");
            return;
        }
        if (list.size() == 1) {
            final PhoneNumbers.Candidate c = list.get(0);
            new AlertDialog.Builder(this)
                    .setTitle("使用本机号码登录")
                    .setMessage("检测到本机号码 " + PhoneNumbers.mask(c.number)
                            + "（" + c.label + "）。\n是否用这个号码作为登录账号？密码仍需输入。")
                    .setPositiveButton("使用本机号码", (d, w) -> applyNumber(c.number))
                    .setNegativeButton("手动输入", (d, w) -> etPhone.requestFocus())
                    .setCancelable(true)
                    .show();
            return;
        }
        CharSequence[] items = new CharSequence[list.size() + 1];
        for (int i = 0; i < list.size(); i++) items[i] = list.get(i).display();
        items[list.size()] = "手动输入其他号码";
        new AlertDialog.Builder(this)
                .setTitle("检测到多个本机号码，请选择要使用的号码")
                .setItems(items, (d, w) -> {
                    if (w < list.size()) applyNumber(list.get(w).number);
                    else {
                        etPhone.requestFocus();
                        setStatus("请手动输入手机号和密码。");
                    }
                })
                .setNegativeButton("稍后", null)
                .show();
    }

    private void applyNumber(String number) {
        etPhone.setText(number);
        etPassword.requestFocus();
        setStatus("已填入本机号码 " + PhoneNumbers.mask(number) + "，请输入密码登录。");
    }

    private void doLogin() {
        final String account = text(etPhone);
        final String password = text(etPassword);
        final String base = text(etAuthBase);
        if (!account.matches("1[3-9]\\d{9}")) {
            setStatus("请输入正确的 11 位手机号");
            return;
        }
        if (password.isEmpty()) {
            setStatus("请输入密码");
            return;
        }
        if (!base.startsWith("http://") && !base.startsWith("https://")) {
            setStatus("登录服务地址必须以 http:// 或 https:// 开头");
            return;
        }
        McpConfig cfg = McpConfig.load(this);
        cfg.authBase = base;
        cfg.save(this);

        setBusy(true);
        setStatus("正在登录…");
        io.execute(() -> {
            AuthSession.Result r = AuthSession.login(LoginActivity.this, account, password);
            ui.post(() -> {
                if (isFinishing()) return;
                setBusy(false);
                if (r.ok) {
                    setStatus("登录成功：" + (r.name.isEmpty() ? r.phone : r.name));
                    goMain();
                } else {
                    setStatus(r.message);
                }
            });
        });
    }

    private void setBusy(boolean busy) {
        btnLogin.setEnabled(!busy);
        btnLogin.setText(busy ? "登录中…" : "登录");
        btnLocal.setEnabled(!busy);
    }

    private void setStatus(String s) {
        tvStatus.setText(s);
    }

    private static String text(EditText e) {
        return e.getText() == null ? "" : e.getText().toString().trim();
    }

    private void goMain() {
        AuthSession.prime(this);
        Intent i = new Intent(this, MainActivity.class);
        i.addFlags(Intent.FLAG_ACTIVITY_CLEAR_TOP | Intent.FLAG_ACTIVITY_NEW_TASK);
        startActivity(i);
        finish();
    }

    @Override
    protected void onDestroy() {
        super.onDestroy();
        io.shutdownNow();
    }
}
