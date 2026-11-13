package com.boray.voicemcp;

import android.content.Context;
import android.content.SharedPreferences;

import org.json.JSONObject;

import java.io.ByteArrayOutputStream;
import java.io.InputStream;
import java.io.OutputStream;
import java.net.HttpURLConnection;
import java.net.URL;
import java.nio.charset.StandardCharsets;
import java.util.List;
import java.util.Map;

/**
 * 工作台登录会话（0.12.0）。
 *
 * <p>服务端：{@code https://ai.boraydata.com/workbench}，与 Web 端同一套账号体系
 * （{@code users.db} 的手机号 + 密码，也兼容 Obsidian「登录凭据.md」里的账号）。
 * <ul>
 *   <li>{@code POST /api/login  {"account": 手机号, "password": 密码}}
 *       → {@code {"ok":true,"user":{...}}} + {@code Set-Cookie: opc_sess=...; Path=/}；</li>
 *   <li>{@code GET  /api/session}（带 Cookie）→ 恢复/校验会话，401 表示已过期；</li>
 *   <li>{@code POST /api/logout}（带 Cookie）→ 服务端销毁会话。</li>
 * </ul>
 *
 * <p>会话是 Cookie（不是 Bearer Token），HttpURLConnection 不会自动回传，
 * 所以这里把它存进私有 SharedPreferences，并由 {@link McpConfig#load(Context)}
 * 注入到每个网络客户端（MCP / SSE / dispatch）。服务端会话有效期 7 天、
 * 进程重启即失效；本地只做同样的 7 天兜底判断，网络不可用时不把用户锁在门外。
 */
public final class AuthSession {

    /** 公网工作台入口（nginx 把 /workbench/ 反代到本机 :8848） */
    public static final String DEFAULT_AUTH_BASE = McpConfig.DEFAULT_AUTH_BASE;
    public static final String COOKIE_NAME = "opc_sess";

    private static final String PREF = "conring_auth";
    private static final long LOCAL_TTL_MS = 7L * 24 * 3600 * 1000;

    /** 进程内缓存：DispatchApi 的静态 HTTP 方法拿不到 Context 时用它 */
    private static volatile String cachedCookie = "";

    private AuthSession() {
    }

    private static SharedPreferences prefs(Context c) {
        return c.getSharedPreferences(PREF, Context.MODE_PRIVATE);
    }

    /** 登录服务地址，末尾斜杠去掉；在设置页/登录页可改（内网可填 http://192.168.31.206:8848） */
    public static String base(Context c) {
        String s = McpConfig.load(c).authBase.trim();
        if (s.isEmpty()) s = DEFAULT_AUTH_BASE;
        while (s.endsWith("/")) s = s.substring(0, s.length() - 1);
        return s;
    }

    public static String phone(Context c) {
        return prefs(c).getString("phone", "");
    }

    public static String name(Context c) {
        return prefs(c).getString("name", "");
    }

    public static String role(Context c) {
        return prefs(c).getString("role", "");
    }

    /** 上次成功登录的号码：SIM 读不到号码时给登录页做候选 */
    public static String lastPhone(Context c) {
        return prefs(c).getString("lastPhone", "");
    }

    public static String cookie(Context c) {
        return prefs(c).getString("cookie", "");
    }

    /** 完整的 Cookie 请求头值；空串表示未登录 */
    public static String header(Context c) {
        String s = cookie(c);
        return s.isEmpty() ? "" : COOKIE_NAME + "=" + s;
    }

    public static boolean isLoggedIn(Context c) {
        if (cookie(c).isEmpty()) return false;
        long at = prefs(c).getLong("loginAt", 0);
        return at <= 0 || System.currentTimeMillis() - at < LOCAL_TTL_MS;
    }

    public static void prime(Context c) {
        cachedCookie = cookie(c);
    }

    public static String cachedHeader() {
        String s = cachedCookie;
        return s.isEmpty() ? "" : COOKIE_NAME + "=" + s;
    }

    public static final class Result {
        public boolean ok = false;
        /** 服务不可达但本地会话仍有效：允许离线继续使用 */
        public boolean offline = false;
        public String message = "";
        public String phone = "";
        public String name = "";
    }

    /** 手机号 + 密码登录；成功后本地保存 opc_sess 与用户资料 */
    public static Result login(Context c, String account, String password) {
        Result r = new Result();
        try {
            JSONObject body = new JSONObject();
            body.put("account", account);
            body.put("password", password);
            Resp resp = call(base(c) + "/api/login", "POST", body.toString(), "");
            JSONObject j = parse(resp.body);
            String sess = resp.setCookie;
            if (resp.code != 200 || sess.isEmpty() || j == null || !j.optBoolean("ok", false)) {
                r.message = errorOf(resp.body, "登录失败（HTTP " + resp.code + "）");
                return r;
            }
            JSONObject user = j.optJSONObject("user");
            save(c, sess, user, account);
            r.ok = true;
            r.phone = phone(c);
            r.name = name(c);
            return r;
        } catch (Exception e) {
            r.message = "无法连接登录服务：" + shortMsg(e);
            return r;
        }
    }

    /** 启动时校验会话：401 清掉本地会话；网络错误按「离线继续」处理，不锁死 App */
    public static Result check(Context c) {
        Result r = new Result();
        if (!isLoggedIn(c)) {
            r.message = "未登录";
            return r;
        }
        try {
            Resp resp = call(base(c) + "/api/session", "GET", null, cookie(c));
            JSONObject j = parse(resp.body);
            if (resp.code == 401 || resp.code == 403) {
                clear(c);
                r.message = "登录已过期，请重新登录";
                return r;
            }
            if (resp.code == 200 && j != null && j.optBoolean("ok", false)) {
                saveUser(c, j.optJSONObject("user"));
                r.ok = true;
                r.phone = phone(c);
                r.name = name(c);
                prime(c);
                return r;
            }
            r.ok = true;
            r.offline = true;
            r.message = "登录服务暂不可用，按本机会话继续（HTTP " + resp.code + "）";
            return r;
        } catch (Exception e) {
            r.ok = true;
            r.offline = true;
            r.message = "登录服务暂不可用，按本机会话继续：" + shortMsg(e);
            return r;
        }
    }

    /** 退出登录：无论服务端是否可达，都清掉本机会话 */
    public static Result logout(Context c) {
        Result r = new Result();
        try {
            call(base(c) + "/api/logout", "POST", "{}", cookie(c));
        } catch (Exception ignored) {
            // 离线也要能退出
        }
        clear(c);
        r.ok = true;
        return r;
    }

    /** 会话被服务端判定失效时清掉；保留 lastPhone 供下次登录提示 */
    public static void clear(Context c) {
        String last = phone(c);
        if (last.isEmpty()) last = lastPhone(c);
        prefs(c).edit().clear().putString("lastPhone", last).apply();
        writeConfigCookie(c, "");
        cachedCookie = "";
    }

    private static void save(Context c, String sess, JSONObject user, String account) {
        SharedPreferences.Editor e = prefs(c).edit();
        e.putString("cookie", sess);
        e.putLong("loginAt", System.currentTimeMillis());
        String ph = user == null ? "" : user.optString("phone", "").trim();
        if (ph.isEmpty()) ph = account;
        e.putString("phone", ph);
        e.putString("name", user == null ? "" : user.optString("name", "").trim());
        e.putString("role", user == null ? "" : user.optString("role", "").trim());
        e.putString("lastPhone", ph);
        e.apply();
        writeConfigCookie(c, COOKIE_NAME + "=" + sess);
        cachedCookie = sess;
    }

    /** 会话请求头同步一份到 voice_mcp_config：McpClient / SseListener 只有 McpConfig 也能带上 */
    private static void writeConfigCookie(Context c, String value) {
        c.getSharedPreferences(McpConfig.PREF_NAME, Context.MODE_PRIVATE)
                .edit().putString("sessionCookie", value).apply();
    }

    /** /api/session 回来了更完整的资料，只更新展示字段，不动 Cookie */
    private static void saveUser(Context c, JSONObject user) {
        if (user == null) return;
        String ph = user.optString("phone", "").trim();
        String nm = user.optString("name", "").trim();
        String rl = user.optString("role", "").trim();
        SharedPreferences.Editor e = prefs(c).edit();
        if (!ph.isEmpty()) e.putString("phone", ph).putString("lastPhone", ph);
        if (!nm.isEmpty()) e.putString("name", nm);
        if (!rl.isEmpty()) e.putString("role", rl);
        e.apply();
    }

    private static final class Resp {
        final int code;
        final String body;
        final String setCookie;

        Resp(int code, String body, String setCookie) {
            this.code = code;
            this.body = body;
            this.setCookie = setCookie;
        }
    }

    private static Resp call(String url, String method, String json, String cookie) throws Exception {
        HttpURLConnection conn = null;
        try {
            conn = (HttpURLConnection) new URL(url).openConnection();
            conn.setRequestMethod(method);
            conn.setConnectTimeout(8000);
            conn.setReadTimeout(12000);
            conn.setUseCaches(false);
            conn.setRequestProperty("Accept", "application/json");
            if (cookie != null && !cookie.isEmpty()) {
                conn.setRequestProperty("Cookie", COOKIE_NAME + "=" + cookie);
            }
            if (json != null) {
                conn.setDoOutput(true);
                conn.setRequestProperty("Content-Type", "application/json; charset=utf-8");
                byte[] b = json.getBytes(StandardCharsets.UTF_8);
                conn.setFixedLengthStreamingMode(b.length);
                OutputStream os = conn.getOutputStream();
                os.write(b);
                os.close();
            }
            int code = conn.getResponseCode();
            InputStream in = code >= 400 ? conn.getErrorStream() : conn.getInputStream();
            String body = in == null ? "" : readAll(in);
            String sess = "";
            // Set-Cookie 可能有多条；只取 opc_sess=<非空值>
            for (Map.Entry<String, List<String>> entry : conn.getHeaderFields().entrySet()) {
                if (entry.getKey() == null || !"Set-Cookie".equalsIgnoreCase(entry.getKey())) continue;
                if (entry.getValue() == null) continue;
                for (String raw : entry.getValue()) {
                    if (raw == null) continue;
                    for (String part : raw.split(";")) {
                        String s = part.trim();
                        String prefix = COOKIE_NAME + "=";
                        if (s.startsWith(prefix)) {
                            String v = s.substring(prefix.length()).trim();
                            if (!v.isEmpty()) sess = v;
                        }
                    }
                }
            }
            return new Resp(code, body, sess);
        } finally {
            if (conn != null) conn.disconnect();
        }
    }

    private static String readAll(InputStream in) throws Exception {
        ByteArrayOutputStream bos = new ByteArrayOutputStream();
        byte[] buf = new byte[4096];
        int n;
        while ((n = in.read(buf)) > 0) bos.write(buf, 0, n);
        return new String(bos.toByteArray(), StandardCharsets.UTF_8);
    }

    private static JSONObject parse(String body) {
        if (body == null || body.trim().isEmpty()) return null;
        try {
            return new JSONObject(body.trim());
        } catch (Exception e) {
            return null;
        }
    }

    private static String errorOf(String body, String fallback) {
        JSONObject j = parse(body);
        if (j == null) return fallback;
        String msg = j.optString("error", "");
        if (msg.isEmpty()) msg = j.optString("message", "");
        return msg.isEmpty() ? fallback : msg;
    }

    private static String shortMsg(Exception e) {
        String m = e.getMessage();
        if (m == null || m.isEmpty()) m = e.getClass().getSimpleName();
        return m.length() > 80 ? m.substring(0, 80) : m;
    }
}
