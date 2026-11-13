"""0.12.0 静态契约：启动登录（本机号码 / 手机号+密码）+ 后台处理模式校对。

不连服务端、不编译 Android 工程，只检查源码里这条链路是否完整：
  LoginActivity -> AuthSession(/api/login|session|logout) -> opc_sess Cookie
  -> MainActivity / ListenService 门禁 -> 网络层注入 Cookie
  -> dispatch 妙记式后台处理（300s 重连、readback、turn 跳号、session_key）。

输出 docs/v34_login_check.json。
"""
import json
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "app", "src", "main", "java", "com", "boray", "voicemcp")
RES = os.path.join(ROOT, "app", "src", "main", "res")
DOCS = os.path.join(ROOT, "docs")

checks = []


def src(name):
    return read(os.path.join(SRC, name))


def read(path):
    return open(path, encoding="utf-8").read() if os.path.isfile(path) else ""


def check(name, ok, detail=""):
    checks.append({"name": name, "ok": bool(ok), "detail": detail})
    print(("  PASS  " if ok else "  FAIL  ") + name
          + ((" | " + detail) if detail and not ok else ""))


def main():
    manifest = read(os.path.join(ROOT, "app", "src", "main", "AndroidManifest.xml"))
    login_xml = read(os.path.join(RES, "layout", "activity_login.xml"))
    settings_xml = read(os.path.join(RES, "layout", "activity_settings.xml"))
    gradle = read(os.path.join(ROOT, "app", "build.gradle"))

    login = src("LoginActivity.java")
    phone = src("PhoneNumbers.java")
    auth = src("AuthSession.java")
    cfg = src("McpConfig.java")
    home = src("MainActivity.java")
    settings = src("SettingsActivity.java")
    boot = src("BootReceiver.java")
    listen = src("ListenService.java")
    mcp = src("McpClient.java")
    sse = src("SseListener.java")
    api = src("DispatchApi.java")
    chan = src("RealtimeChannel.java")
    reply = src("CallReply.java")
    svc = src("VoiceCallService.java")
    outbox = src("VoiceOutbox.java")

    print("== 1. 启动入口与本机号码 ==")
    check("登录页是 LAUNCHER", bool(re.search(
        r'android:name="\.LoginActivity"[\s\S]{0,320}?category android:name="android\.intent\.category\.LAUNCHER"',
        manifest)))
    check("首页不再暴露为启动入口", 'android:name=".MainActivity"\n            android:exported="false"' in manifest)
    check("READ_PHONE_NUMBERS 权限", "android.permission.READ_PHONE_NUMBERS" in manifest)
    check("READ_PHONE_STATE 只到 API 32", bool(re.search(
        r'READ_PHONE_STATE"[\s\S]{0,100}?maxSdkVersion="32"', manifest)))
    check("双卡列表读取", "getActiveSubscriptionInfoList" in phone
          and "getPhoneNumber(info.getSubscriptionId())" in phone)
    check("号码归一化（+86 / 11 位）", "1[3-9]" in phone and "startsWith(\"+86\")" in phone)
    check("运行时申请号码权限", "requestPermissions(PhoneNumbers.permissions()" in login)
    check("单个号码弹确认", 'setPositiveButton("使用本机号码"' in login)
    check("多个号码列选择", "setItems(items" in login and "手动输入其他号码" in login)
    check("读不到号码退回手动输入", "list.isEmpty()" in login and "请手动输入手机号" in login)
    check("登录页有手机号/密码/服务地址", all(i in login_xml for i in
          ("@+id/etPhone", "@+id/etPassword", "@+id/etAuthBase")))

    print("== 2. 手机号 + 密码登录 ==")
    check("手机号格式校验", 'account.matches("1[3-9]' in login)
    check("密码非空校验", "password.isEmpty()" in login)
    check("调用 /api/login", '"/api/login"' in auth and "AuthSession.login(" in login)
    check("请求体 account/password", 'body.put("account", account)' in auth
          and 'body.put("password", password)' in auth)
    check("解析 Set-Cookie", '"Set-Cookie".equalsIgnoreCase' in auth
          and 'COOKIE_NAME = "opc_sess"' in auth)
    check("会话恢复 /api/session", '"/api/session"' in auth and "AuthSession.check(" in home)
    check("退出 /api/logout", '"/api/logout"' in auth and "confirmLogout" in settings)
    check("离线不锁死", "offline" in auth and "按本机会话继续" in auth)
    check("会话写入配置供网络层复用", "writeConfigCookie" in auth
          and "sessionCookie" in cfg and "PREF_NAME" in cfg)
    check("保存设置不重置 dispatch/登录地址", "McpConfig c = McpConfig.load(this);" in settings)

    print("== 3. 登录门禁与会话注入 ==")
    check("首页 onCreate/onResume 拦截", "requireLogin()" in home and home.count("requireLogin()") >= 3)
    check("开机自启检查登录态", "AuthSession.isLoggedIn(context)" in boot)
    check("后台监听检查登录态", "AuthSession.isLoggedIn(c)" in listen)
    check("MCP 请求带 Cookie", 'setRequestProperty("Cookie", cfg.sessionCookie)' in mcp)
    check("SSE 长连接带 Cookie", 'setRequestProperty("Cookie", cfg.sessionCookie)' in sse)
    check("dispatch 请求带 Cookie", "AuthSession.cachedHeader()" in api
          and "AuthSession.prime(c)" in api)
    check("设置页账号与退出入口", "@+id/tvAccount" in settings_xml
          and "@+id/btnLogout" in settings_xml and "refreshAccount" in settings)

    print("== 4. 后台处理模式校对（服务端 dispatch 0.11.2+ / 妙记式）==")
    check("重连窗口 30s → 300s", "300_000" in chan and "REATTACH_GRACE" in chan)
    check("识别复述帧 readback", 'isReadback' in reply and "readback" in reply
          and "reply.isReadback()" in svc)
    check("同 turn 多帧 + 允许跳号", "TreeMap<Integer, List<CallReply>>" in chan
          and "firstKey()" in chan)
    check("缺号 300ms 兜底补发仍在", "postDelayed(flushReplies, 300)" in chan)
    check("逐句记录的状态提示", "已记下，继续说" in svc and "妙记式" in chan)
    check("submit 带 session_key / 解析 resumed", 'body.put("session_key", account)' in api
          and "resumed" in api)
    check("双轨后台链路说明保留", "voice_job" in outbox and "_JOB_Q" in outbox)
    check("设置页说明最新后台模式", "妙记式" in settings_xml)

    print("== 5. 版本 ==")
    ver = re.search(r'appVersionName\s*=\s*"([^"]+)"', gradle)
    code = re.search(r'appVersionCode\s*=\s*(\d+)', gradle)
    check("版本 0.12.0 / b43", ver is not None and ver.group(1) == "0.12.0"
          and code is not None and int(code.group(1)) == 43)
    check("CLIENT_VERSION 同步", 'CLIENT_VERSION = "0.12.0"' in cfg)

    ok = sum(1 for c in checks if c["ok"])
    bad = len(checks) - ok
    os.makedirs(DOCS, exist_ok=True)
    with open(os.path.join(DOCS, "v34_login_check.json"), "w", encoding="utf-8") as f:
        json.dump({"version": "0.12.0", "total": len(checks), "pass": ok,
                   "fail": bad, "checks": checks}, f, ensure_ascii=False, indent=2)
    print("LOGIN 0.12.0: %d PASS / %d FAIL" % (ok, bad))
    return 0 if bad == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
