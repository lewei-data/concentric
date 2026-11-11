package com.boray.voicemcp;

import android.content.ContentValues;
import android.content.Context;
import android.database.Cursor;
import android.database.sqlite.SQLiteDatabase;
import android.database.sqlite.SQLiteOpenHelper;

import java.text.SimpleDateFormat;
import java.util.ArrayList;
import java.util.Date;
import java.util.List;
import java.util.Locale;

/**
 * 会话记录（SQLite，长期保存）。
 *
 * <p>「实时转写」不再是一次性的 UI 文本，而是与某个数字员工的对话流水：
 * <ul>
 *   <li>{@code sessions}：一次通话（或一段会话）——谁、哪一天、什么时候开始/结束、走的哪条链路；</li>
 *   <li>{@code messages}：逐条消息——角色（我 / 员工 / 系统）、类型（语音稿 / 文字详情 / 系统提示）、
 *       文本、附件链接 JSON、时间戳。</li>
 * </ul>
 * 索引按 {@code (emp_id, day, ts)} 建，所以「某人的今天」「再往前一天」都是一次索引扫描。
 *
 * <p>与 {@link HistoryDb} 的区别：HistoryDb 存的是「一次指令的结果」（含音频/文件路径），
 * 这里是「对话流水」，只关心谁在什么时候说了什么，供会话面板逐日回看。
 */
public final class SessionStore extends SQLiteOpenHelper {

    private static final String DB_NAME = "conring_sessions.db";
    private static final int DB_VERSION = 1;

    public static final int ROLE_USER = 0;
    public static final int ROLE_AGENT = 1;
    public static final int ROLE_SYSTEM = 2;

    /** 语音稿（字幕）/ 文字详情 / 系统提示 */
    public static final int KIND_VOICE = 0;
    public static final int KIND_DETAIL = 1;
    public static final int KIND_SYSTEM = 2;

    public static final class Row {
        public long id;
        public String sessionId = "";
        public String empId = "";
        public String empName = "";
        public String day = "";
        public long ts;
        public int role = ROLE_USER;
        public int kind = KIND_VOICE;
        public String text = "";
        /** [{"name":"...","url":"..."}]，没有就是 "[]" */
        public String linksJson = "[]";
    }

    private static SessionStore instance;
    /** 多个面板（首页内嵌 + 弹出层 + 视频页）可能同时存在，用列表而不是单槽 */
    private static final java.util.concurrent.CopyOnWriteArrayList<Runnable> listeners =
            new java.util.concurrent.CopyOnWriteArrayList<>();

    public static synchronized SessionStore get(Context c) {
        if (instance == null) instance = new SessionStore(c.getApplicationContext());
        return instance;
    }

    /** 会话有新消息 / 被修改：面板据此增量刷新（主线程回调由调用方负责） */
    public static void addListener(Runnable l) {
        if (l != null && !listeners.contains(l)) listeners.add(l);
    }

    public static void removeListener(Runnable l) {
        listeners.remove(l);
    }

    private static void notifyChanged() {
        for (Runnable l : listeners) l.run();
    }

    private SessionStore(Context c) {
        super(c, DB_NAME, null, DB_VERSION);
    }

    @Override
    public void onCreate(SQLiteDatabase db) {
        db.execSQL("CREATE TABLE sessions("
                + "id TEXT PRIMARY KEY,"
                + "emp_id TEXT NOT NULL,"
                + "emp_name TEXT,"
                + "day TEXT NOT NULL,"
                + "started_at INTEGER NOT NULL,"
                + "ended_at INTEGER,"
                + "transport TEXT)");
        db.execSQL("CREATE TABLE messages("
                + "id INTEGER PRIMARY KEY AUTOINCREMENT,"
                + "session_id TEXT NOT NULL,"
                + "emp_id TEXT NOT NULL,"
                + "emp_name TEXT,"
                + "day TEXT NOT NULL,"
                + "ts INTEGER NOT NULL,"
                + "role INTEGER NOT NULL,"
                + "kind INTEGER NOT NULL,"
                + "text TEXT NOT NULL,"
                + "links_json TEXT)");
        db.execSQL("CREATE INDEX idx_msg_emp_day ON messages(emp_id, day, ts)");
        db.execSQL("CREATE INDEX idx_msg_sid ON messages(session_id, ts)");
        db.execSQL("CREATE INDEX idx_sess_emp_day ON sessions(emp_id, day)");
    }

    @Override
    public void onUpgrade(SQLiteDatabase db, int oldVersion, int newVersion) {
        // 会话记录是长期数据：只增列，不重建
        if (oldVersion < 1) onCreate(db);
    }

    // ------------------------------------------------------------------ 会话

    /** 开始一段会话，返回 session id */
    public synchronized String begin(String empId, String empName, String transport, long when) {
        long ts = when > 0 ? when : System.currentTimeMillis();
        String id = "s" + ts + "_" + Integer.toHexString((int) (Math.random() * 0xFFFF));
        ContentValues v = new ContentValues();
        v.put("id", id);
        v.put("emp_id", empId == null ? "" : empId);
        v.put("emp_name", empName == null ? "" : empName);
        v.put("day", dayOf(ts));
        v.put("started_at", ts);
        v.put("ended_at", 0);
        v.put("transport", transport == null ? "" : transport);
        getWritableDatabase().insert("sessions", null, v);
        return id;
    }

    public synchronized void finish(String sessionId, long when) {
        if (sessionId == null || sessionId.isEmpty()) return;
        ContentValues v = new ContentValues();
        v.put("ended_at", when > 0 ? when : System.currentTimeMillis());
        getWritableDatabase().update("sessions", v, "id=?", new String[]{sessionId});
    }

    // ------------------------------------------------------------------ 消息

    public synchronized long add(String sessionId, String empId, String empName,
                                 int role, int kind, String text, String linksJson, long when) {
        String t = text == null ? "" : text.trim();
        if (t.isEmpty()) return -1;
        long ts = when > 0 ? when : System.currentTimeMillis();
        ContentValues v = new ContentValues();
        v.put("session_id", sessionId == null ? "" : sessionId);
        v.put("emp_id", empId == null ? "" : empId);
        v.put("emp_name", empName == null ? "" : empName);
        v.put("day", dayOf(ts));
        v.put("ts", ts);
        v.put("role", role);
        v.put("kind", kind);
        v.put("text", t);
        v.put("links_json", linksJson == null || linksJson.isEmpty() ? "[]" : linksJson);
        long id = getWritableDatabase().insert("messages", null, v);
        notifyChanged();
        return id;
    }

    /** 原地更新某条消息（占位 → 真文本时用，不新增行） */
    public synchronized void setText(long id, String text, String linksJson) {
        if (id <= 0) return;
        ContentValues v = new ContentValues();
        v.put("text", text == null ? "" : text.trim());
        if (linksJson != null) v.put("links_json", linksJson);
        getWritableDatabase().update("messages", v, "id=?", new String[]{String.valueOf(id)});
        notifyChanged();
    }

    public synchronized void remove(long id) {
        if (id <= 0) return;
        getWritableDatabase().delete("messages", "id=?", new String[]{String.valueOf(id)});
        notifyChanged();
    }

    /** 某人的某一天，按时间正序（最旧在上、最新在下） */
    public synchronized List<Row> day(String empId, String day, int limit) {
        List<Row> out = new ArrayList<>();
        if (day == null || day.isEmpty()) return out;
        Cursor c = getReadableDatabase().query("messages", null,
                "emp_id=? AND day=?", new String[]{empId == null ? "" : empId, day},
                null, null, "ts ASC, id ASC", String.valueOf(Math.max(1, limit)));
        if (c != null) {
            while (c.moveToNext()) out.add(fromCursor(c));
            c.close();
        }
        return out;
    }

    /** 比 beforeDay 更早、且有消息的那一天；没有返回 "" */
    public synchronized String previousDay(String empId, String beforeDay) {
        if (beforeDay == null || beforeDay.isEmpty()) return "";
        Cursor c = getReadableDatabase().rawQuery(
                "SELECT MAX(day) FROM messages WHERE emp_id=? AND day<?",
                new String[]{empId == null ? "" : empId, beforeDay});
        String day = "";
        if (c != null) {
            if (c.moveToFirst()) {
                String s = c.getString(0);
                day = s == null ? "" : s;
            }
            c.close();
        }
        return day;
    }

    public synchronized int countDay(String empId, String day) {
        Cursor c = getReadableDatabase().rawQuery(
                "SELECT COUNT(*) FROM messages WHERE emp_id=? AND day=?",
                new String[]{empId == null ? "" : empId, day});
        int n = 0;
        if (c != null) {
            if (c.moveToFirst()) n = c.getInt(0);
            c.close();
        }
        return n;
    }

    /** 最近一条消息（跨天，用于兜底预览） */
    public synchronized Row latest(String empId) {
        return latest(empId, null);
    }

    /** 最近一条消息；day 非空时只看那一天（默认「今天」用这个） */
    public synchronized Row latest(String empId, String day) {
        String where = day == null || day.isEmpty() ? "emp_id=?" : "emp_id=? AND day=?";
        String[] args = day == null || day.isEmpty()
                ? new String[]{empId == null ? "" : empId}
                : new String[]{empId == null ? "" : empId, day};
        Cursor c = getReadableDatabase().query("messages", null, where,
                args, null, null, "ts DESC, id DESC", "1");
        Row r = null;
        if (c != null) {
            if (c.moveToFirst()) r = fromCursor(c);
            c.close();
        }
        return r;
    }

    private static Row fromCursor(Cursor c) {
        Row r = new Row();
        r.id = c.getLong(c.getColumnIndexOrThrow("id"));
        r.sessionId = str(c, "session_id");
        r.empId = str(c, "emp_id");
        r.empName = str(c, "emp_name");
        r.day = str(c, "day");
        r.ts = c.getLong(c.getColumnIndexOrThrow("ts"));
        r.role = c.getInt(c.getColumnIndexOrThrow("role"));
        r.kind = c.getInt(c.getColumnIndexOrThrow("kind"));
        r.text = str(c, "text");
        r.linksJson = str(c, "links_json");
        return r;
    }

    private static String str(Cursor c, String key) {
        int i = c.getColumnIndex(key);
        if (i < 0) return "";
        String s = c.getString(i);
        return s == null ? "" : s;
    }

    // ------------------------------------------------------------------ 日期

    /** 本地时区的 yyyy-MM-dd；跨天按手机本地时间算，和用户看到的「今天」一致 */
    public static String dayOf(long ts) {
        return new SimpleDateFormat("yyyy-MM-dd", Locale.CHINA).format(new Date(ts));
    }

    public static String today() {
        return dayOf(System.currentTimeMillis());
    }

    /** 2026-11-11 → 11 月 11 日；今天/昨天单独标注 */
    public static String dayLabel(String day) {
        String today = today();
        if (day.equals(today)) return "今天";
        String y = dayOf(System.currentTimeMillis() - 86400_000L);
        if (day.equals(y)) return "昨天";
        String[] p = day.split("-");
        if (p.length == 3) {
            try {
                return Integer.parseInt(p[1]) + " 月 " + Integer.parseInt(p[2]) + " 日";
            } catch (Exception ignored) {
            }
        }
        return day;
    }
}
