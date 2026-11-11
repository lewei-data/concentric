package com.boray.voicemcp;

import android.content.Context;
import android.content.SharedPreferences;
import android.graphics.Bitmap;
import android.graphics.BitmapFactory;
import android.graphics.drawable.BitmapDrawable;
import android.net.Uri;
import android.os.Handler;
import android.os.Looper;
import android.widget.ImageView;

import org.json.JSONArray;
import org.json.JSONObject;

import java.io.File;
import java.io.FileOutputStream;
import java.io.InputStream;
import java.io.OutputStream;
import java.net.HttpURLConnection;
import java.net.URL;
import java.nio.charset.StandardCharsets;
import java.text.SimpleDateFormat;
import java.util.ArrayList;
import java.util.Date;
import java.util.List;
import java.util.Locale;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.atomic.AtomicBoolean;

/**
 * 数字员工档案仓库（server v2.1.0）。
 *
 * 数据源优先级：用户自定义 employees.json > profile 接口缓存 employees_server.json > assets 内置。
 * profile 返回 18 人的组织信息、专属音色、性别年龄、素材地址与 asset_url_base；
 * 缓存到 filesDir 后完全离线可用。素材地址只走 AssetUrls（服务端同源绝对路径），
 * 不探测、不回退到其它 URL；服务端 2026-11-10 已放通 /conring/asr/assets/* 静态路径，
 * 公网 200、无需 VPN / Token；个别素材异常时仍由 UI 安静回退内置 Canvas 数字人。
 */
public final class EmployeeStore {

    private static final String PREF = "voice_mcp_employees";
    private static final String KEY_CURRENT = "current_employee";
    private static final String KEY_SYNC_TIME = "profile_sync_time";
    /** 默认汇报员：小秘书（OPC-001，服务端 id=secretary，老板影子分身） */
    public static final String DEFAULT_EMPLOYEE_ID = "secretary";
    /** 一次性迁移标记：从旧版本升级时把默认汇报员重置为 CEO（之后用户选择不再被覆盖） */
    private static final String KEY_DEFAULT_MIGRATED = "default_secretary_migrated";
    private static final String ASSET_JSON = "employees.json";
    private static final String CUSTOM_JSON = "employees.json";
    private static final String SERVER_JSON = "employees_server.json";
    /** 缓存有效期：6 小时 */
    private static final long STALE_MS = 6L * 60 * 60 * 1000;

    private static final ExecutorService IO = Executors.newSingleThreadExecutor();
    private static final Handler UI = new Handler(Looper.getMainLooper());
    private static final AtomicBoolean syncing = new AtomicBoolean(false);

    public interface SyncCallback {
        void onDone(int count, String error);
    }

    private EmployeeStore() {
    }

    // ------------------------------------------------------------------ 档案读取

    public static List<Employee> load(Context c) {
        String json = readText(new File(c.getFilesDir(), CUSTOM_JSON));   // 用户自定义优先
        if (json == null) json = readText(new File(c.getFilesDir(), SERVER_JSON));  // profile 缓存
        if (json == null) {
            try (InputStream in = c.getAssets().open(ASSET_JSON)) {
                json = readAll(in);
            } catch (Exception ignored) {
            }
        }
        if (json == null || json.trim().isEmpty()) json = fallbackJson();
        return parse(json);
    }

    private static List<Employee> parse(String json) {
        List<Employee> out = new ArrayList<>();
        try {
            JSONObject root = new JSONObject(json);
            JSONArray arr = root.optJSONArray("employees");
            if (arr == null) return out;
            for (int i = 0; i < arr.length(); i++) {
                JSONObject o = arr.optJSONObject(i);
                if (o != null) out.add(Employee.fromJson(o));
            }
        } catch (Exception ignored) {
        }
        if (out.isEmpty()) out.addAll(parse(fallbackJson()));
        return out;
    }

    /** 严格按 id 查找，找不到返回 null（用于「形象必须匹配本次返回的 avatar」的场景） */
    public static Employee findById(List<Employee> all, String id) {
        if (all == null || id == null || id.isEmpty()) return null;
        for (Employee e : all) if (e.id.equals(id)) return e;
        return null;
    }

    /**
     * 兼容旧调用：找不到时不随便取列表第 1 位（那是 boss），
     * 而是回落到默认汇报员 CEO，再兜底第 1 位，避免形象与名字错配。
     */
    public static Employee byId(List<Employee> all, String id) {
        if (all == null || all.isEmpty()) return null;
        Employee hit = findById(all, id);
        if (hit != null) return hit;
        hit = findById(all, DEFAULT_EMPLOYEE_ID);
        return hit != null ? hit : all.get(0);
    }

    public static Employee current(Context c, List<Employee> all) {
        SharedPreferences p = c.getSharedPreferences(PREF, Context.MODE_PRIVATE);
        // 旧版本默认取列表第 1 位（boss）。升级后首次运行把默认汇报员迁到 CEO，
        // 只做一次，之后用户在档案页切换的选择会一直保留。
        if (!p.getBoolean(KEY_DEFAULT_MIGRATED, false)) {
            p.edit().putString(KEY_CURRENT, DEFAULT_EMPLOYEE_ID)
                    .putBoolean(KEY_DEFAULT_MIGRATED, true).apply();
        }
        String id = p.getString(KEY_CURRENT, DEFAULT_EMPLOYEE_ID);
        return byId(all, id);
    }

    public static void setCurrent(Context c, String id) {
        c.getSharedPreferences(PREF, Context.MODE_PRIVATE).edit()
                .putString(KEY_CURRENT, id).apply();
    }

    /** 当前汇报人的 profile（仅当设置页显式需要覆盖时才用） */
    public static String currentProfile(Context c, String configured) {
        if (configured != null && !configured.trim().isEmpty()) return configured.trim();
        Employee e = current(c, load(c));
        return e == null ? "" : e.profileValue();
    }

    // ------------------------------------------------------------------ profile 同步

    /** 从服务端 profile 拉取全量档案并缓存；回调在 UI 线程 */
    public static void syncFromServer(Context c, McpConfig cfg, SyncCallback cb) {
        if (cfg == null || cfg.demo) {
            if (cb != null) cb.onDone(0, "演示模式不同步档案");
            return;
        }
        if (!syncing.compareAndSet(false, true)) {
            if (cb != null) cb.onDone(-1, "正在同步中");
            return;
        }
        final McpConfig snapshot = cfg.copy();
        IO.execute(() -> {
            String error = "";
            int count = 0;
            try {
                McpClient client = new McpClient(snapshot);
                JSONObject res = client.callTool("profile",
                        new JSONObject().put("include_assets", true));
                String text = "";
                JSONArray content = res.optJSONArray("content");
                if (content != null) {
                    for (int i = 0; i < content.length(); i++) {
                        JSONObject b = content.optJSONObject(i);
                        if (b != null && "text".equals(b.optString("type", ""))) {
                            text = b.optString("text", "");
                            break;
                        }
                    }
                }
                if (text.isEmpty()) throw new Exception("profile 没有返回文本内容");
                JSONObject root = new JSONObject(text);
                JSONArray emps = root.optJSONArray("employees");
                if (emps == null || emps.length() == 0) throw new Exception("profile 员工列表为空");
                count = emps.length();
                // asset_url_base 现在是 /conring/asr/assets；服务端已把它写进 photo 等字段，
                // AssetUrls 只在「裸相对文件名」时才用这个前缀，不会重复叠加。
                AssetUrls.setServerBase(c, root.optString("asset_url_base", ""));
                JSONObject cache = new JSONObject();
                cache.put("fetched_at", System.currentTimeMillis());
                cache.put("count", count);
                cache.put("asset_url_base", root.optString("asset_url_base", ""));
                cache.put("employees", emps);
                File out = new File(c.getFilesDir(), SERVER_JSON);
                // 先写临时文件再原子替换：避免别的页面正好在这时读到被截断的 JSON
                // （读到半截会 parse 失败，退化成内置档案，看起来像「点进去就返回」）
                File tmp = new File(c.getFilesDir(), SERVER_JSON + ".tmp");
                try (OutputStream os = new FileOutputStream(tmp)) {
                    os.write(cache.toString().getBytes(StandardCharsets.UTF_8));
                }
                if (!tmp.renameTo(out)) {
                    // 个别文件系统不支持覆盖式 rename，退回直接写
                    try (OutputStream os = new FileOutputStream(out)) {
                        os.write(cache.toString().getBytes(StandardCharsets.UTF_8));
                    }
                    //noinspection ResultOfMethodCallIgnored
                    tmp.delete();
                }
                prefs(c).edit().putLong(KEY_SYNC_TIME, System.currentTimeMillis()).apply();
            } catch (Exception e) {
                error = e.getMessage() == null ? "同步失败" : e.getMessage();
            } finally {
                syncing.set(false);
            }
            final int n = count;
            final String err = error;
            UI.post(() -> { if (cb != null) cb.onDone(n, err); });
        });
    }

    /** App 启动 / 档案页进入时：缓存缺失或过期才静默同步一次，不阻塞 UI */
    public static void syncIfStale(Context c) {
        McpConfig cfg = McpConfig.load(c);
        if (!cfg.autoSyncProfile || cfg.demo) return;
        if (!new File(c.getFilesDir(), SERVER_JSON).isFile()
                || System.currentTimeMillis() - prefs(c).getLong(KEY_SYNC_TIME, 0) > STALE_MS) {
            syncFromServer(c, cfg, null);
        }
    }

    public static String syncStatus(Context c) {
        long t = prefs(c).getLong(KEY_SYNC_TIME, 0);
        int n = load(c).size();
        if (t <= 0) return n + " 位 · 内置档案（未同步服务端）";
        String time = new SimpleDateFormat("MM-dd HH:mm", Locale.CHINA).format(new Date(t));
        return n + " 位 · 服务端已同步 " + time;
    }

    private static SharedPreferences prefs(Context c) {
        return c.getSharedPreferences(PREF, Context.MODE_PRIVATE);
    }

    // ------------------------------------------------------------------ 照片

    /**
     * 加载照片。返回 false 表示档案没有配置照片（调用方直接用内置形象）；
     * 返回 true 表示已发起加载，失败时回调 onFail（同样回退内置形象）。
     */
    public static boolean applyPhoto(Context c, Employee e, ImageView iv, Runnable onFail) {
        return applyPhoto(c, e, iv, null, onFail);
    }

    /** 成功 / 失败都回调，便于调用方在素材不可访问时切回内置 Canvas 数字人 */
    public static boolean applyPhoto(Context c, Employee e, ImageView iv,
                                     Runnable onSuccess, Runnable onFail) {
        if (iv == null || e == null) return false;
        String spec = e.photo == null ? "" : e.photo.trim();
        if (spec.isEmpty()) {
            iv.setImageDrawable(null);
            return false;
        }
        if (spec.startsWith("drawable://")) {
            String name = spec.substring("drawable://".length());
            int id = c.getResources().getIdentifier(name, "drawable", c.getPackageName());
            if (id != 0) {
                iv.setImageResource(id);
                if (onSuccess != null) onSuccess.run();
                return true;
            }
            iv.setImageDrawable(null);
            if (onFail != null) onFail.run();
            return true;
        }
        if (spec.startsWith("http://") || spec.startsWith("https://")
                || spec.startsWith("assets://") || spec.startsWith("file://")
                || spec.startsWith("/")) {
            final String url = AssetUrls.resolve(c, spec);
            IO.execute(() -> {
                Bitmap bmp = loadBitmap(c, url);
                UI.post(() -> {
                    if (bmp != null) {
                        iv.setImageDrawable(new BitmapDrawable(c.getResources(), bmp));
                        if (onSuccess != null) onSuccess.run();
                    } else {
                        iv.setImageDrawable(null);
                        if (onFail != null) onFail.run();
                    }
                });
            });
            return true;
        }
        Bitmap bmp = decodeLocal(c, spec);
        if (bmp != null) {
            iv.setImageDrawable(new BitmapDrawable(c.getResources(), bmp));
            if (onSuccess != null) onSuccess.run();
            return true;
        }
        iv.setImageDrawable(null);
        if (onFail != null) onFail.run();
        return true;
    }

    /** 兼容旧调用点 */
    public static void applyPhoto(Context c, Employee e, ImageView iv) {
        applyPhoto(c, e, iv, null);
    }

    /** 异步把服务端真人照片/面部特写解成 Bitmap，供「照片 + 口型」小窗使用；失败回调 null */
    public interface BitmapCallback {
        void onLoaded(Bitmap bmp);
    }

    public static void loadPhotoBitmap(Context c, String spec, BitmapCallback cb) {
        if (spec == null || spec.trim().isEmpty() || spec.startsWith("drawable://")) {
            UI.post(() -> { if (cb != null) cb.onLoaded(null); });
            return;
        }
        final String s = spec.trim();
        final String url = (s.startsWith("http://") || s.startsWith("https://")
                || s.startsWith("assets://") || s.startsWith("file://") || s.startsWith("/"))
                ? AssetUrls.resolve(c, s) : s;
        IO.execute(() -> {
            Bitmap bmp = loadBitmap(c, url);
            UI.post(() -> { if (cb != null) cb.onLoaded(bmp); });
        });
    }

    private static Bitmap loadBitmap(Context c, String urlOrPath) {
        if (urlOrPath == null || urlOrPath.isEmpty()) return null;
        if (!urlOrPath.startsWith("http://") && !urlOrPath.startsWith("https://")) {
            return decodeLocal(c, urlOrPath);
        }
        HttpURLConnection conn = null;
        try {
            conn = (HttpURLConnection) new URL(urlOrPath).openConnection();
            conn.setConnectTimeout(8000);
            conn.setReadTimeout(12000);
            conn.setInstanceFollowRedirects(true);
            // 素材是 MCP 同源公开静态资源（/conring/asr/assets/*），服务端明确无需 Token；
            // 不复用 /mcp 的 Bearer，避免误带鉴权头导致静态路径被拒。
            int code = conn.getResponseCode();
            if (code != 200) return null;
            try (InputStream in = conn.getInputStream()) {
                return BitmapFactory.decodeStream(in);
            }
        } catch (Exception ignored) {
            return null;
        } finally {
            if (conn != null) conn.disconnect();
        }
    }

    private static Bitmap decodeLocal(Context c, String spec) {
        try {
            if (spec.startsWith("assets://")) {
                try (InputStream in = c.getAssets().open(spec.substring("assets://".length()))) {
                    return BitmapFactory.decodeStream(in);
                }
            }
            String path = spec.startsWith("file://") ? Uri.parse(spec).getPath() : spec;
            if (path != null && new File(path).isFile()) return BitmapFactory.decodeFile(path);
        } catch (Exception ignored) {
        }
        return null;
    }

    // ------------------------------------------------------------------ 仿真人视频形象

    /** 解析视频素材为 VideoView 可播的 Uri；同源绝对路径由 AssetUrls 直接拼 MCP origin */
    public static android.net.Uri resolveVideo(Context c, String spec) {
        if (spec == null || spec.trim().isEmpty()) return null;
        String s = spec.trim();
        if (s.startsWith("http://") || s.startsWith("https://") || s.startsWith("file://")) {
            return android.net.Uri.parse(s);
        }
        if (s.startsWith("assets://")) {
            return copyAssetToCache(c, s.substring("assets://".length()));
        }
        if (s.startsWith("/")) {
            String url = AssetUrls.resolve(c, s);
            return url.isEmpty() ? null : android.net.Uri.parse(url);
        }
        File f = new File(s);
        return f.isFile() ? android.net.Uri.fromFile(f) : null;
    }

    private static android.net.Uri copyAssetToCache(Context c, String assetName) {
        try {
            File dir = new File(c.getCacheDir(), "employee_video");
            if (!dir.exists() && !dir.mkdirs()) return null;
            File out = new File(dir, new File(assetName).getName());
            if (out.isFile() && out.length() > 0) return android.net.Uri.fromFile(out);
            try (InputStream in = c.getAssets().open(assetName);
                 OutputStream os = new FileOutputStream(out)) {
                byte[] buf = new byte[64 * 1024];
                int n;
                while ((n = in.read(buf)) > 0) os.write(buf, 0, n);
                os.flush();
            }
            return android.net.Uri.fromFile(out);
        } catch (Exception e) {
            return null;
        }
    }

    // ------------------------------------------------------------------ 工具

    private static String readAll(InputStream in) throws Exception {
        StringBuilder sb = new StringBuilder();
        byte[] buf = new byte[8192];
        int n;
        while ((n = in.read(buf)) > 0) sb.append(new String(buf, 0, n, StandardCharsets.UTF_8));
        return sb.toString();
    }

    private static String readText(File f) {
        if (!f.isFile()) return null;
        try (InputStream in = new java.io.FileInputStream(f)) {
            return readAll(in);
        } catch (Exception e) {
            return null;
        }
    }

    private static String fallbackJson() {
        return "{\"employees\":["
                + "{\"id\":\"opc-001\",\"name\":\"同心圆助手\",\"no\":\"OPC-001\","
                + "\"dept\":\"数字员工中心\",\"title\":\"语音汇报官\",\"motto\":\"收到，马上汇报。\","
                + "\"bio\":\"内置形象，由服务端返回音频实时驱动口型、表情与动作。\","
                + "\"profile\":\"opc-001\",\"skills\":[\"语音汇报\",\"结果播报\"],"
                + "\"hairStyle\":\"short\",\"emotion\":\"calm\"}]}";
    }
}
