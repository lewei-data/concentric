package com.boray.voicemcp;

import android.content.ContentValues;
import android.content.Context;
import android.database.Cursor;
import android.database.sqlite.SQLiteDatabase;
import android.database.sqlite.SQLiteOpenHelper;
import android.net.Uri;

import org.json.JSONArray;
import org.json.JSONException;
import org.json.JSONObject;

import java.io.File;
import java.util.ArrayList;
import java.util.List;

/**
 * 本机长期保存的历史消息（SQLite）。
 * 每次语音指令、每条推送/新邮件都写一条记录；异步任务回执时先落一条 status=accepted，
 * job 完成后按 message_id 原地更新，因此一条指令在历史里只占一行。
 * 音频 / 视频 / 文字文件保存在 files/history/ 永久目录，字段里只存路径与 content URI。
 *
 * v2.0 起数据库升级采用 ALTER TABLE 增量迁移，绝不清空用户已有的长期历史。
 */
public final class HistoryDb extends SQLiteOpenHelper {

    private static final String DB_NAME = "conring_history.db";
    private static final int DB_VERSION = 2;
    private static final String TABLE = "history";

    public static final class Record {
        public long id;
        public long timeMs;
        public String kind = "voice";
        public String messageId = "";
        public String jobId = "";
        public String question = "";
        public String answer = "";
        public String detail = "";
        public String ack = "";
        public String status = "";
        public String employee = "";
        public String audioPath = "";
        public String videoUrl = "";
        public String videoPath = "";
        public String photoUrl = "";
        public String mode = "";
        public String filesJson = "[]";
        public String rawJson = "";
        /* v2.1.0 新增 */
        public String segmentsJson = "";
        public String avatarId = "";
        public String delegate = "";
        public String voice = "";
        public String errorText = "";
    }

    private static HistoryDb instance;

    public static synchronized HistoryDb get(Context c) {
        if (instance == null) instance = new HistoryDb(c.getApplicationContext());
        return instance;
    }

    private HistoryDb(Context c) {
        super(c, DB_NAME, null, DB_VERSION);
    }

    @Override
    public void onCreate(SQLiteDatabase db) {
        db.execSQL("CREATE TABLE " + TABLE + " ("
                + "id INTEGER PRIMARY KEY AUTOINCREMENT,"
                + "time_ms INTEGER NOT NULL,"
                + "kind TEXT, message_id TEXT, job_id TEXT,"
                + "question TEXT, answer TEXT, detail TEXT, ack TEXT, status TEXT,"
                + "employee TEXT, audio_path TEXT, video_url TEXT, video_path TEXT,"
                + "photo_url TEXT, mode TEXT, files_json TEXT, raw_json TEXT,"
                + "segments_json TEXT, avatar_id TEXT, delegate TEXT, voice TEXT, error_text TEXT)");
        db.execSQL("CREATE INDEX idx_history_time ON " + TABLE + "(time_ms DESC)");
        db.execSQL("CREATE INDEX idx_history_mid ON " + TABLE + "(message_id)");
    }

    @Override
    public void onUpgrade(SQLiteDatabase db, int oldVersion, int newVersion) {
        // 长期历史不能丢：只补列，绝不 DROP TABLE
        if (oldVersion < 2) {
            addColumn(db, "segments_json");
            addColumn(db, "avatar_id");
            addColumn(db, "delegate");
            addColumn(db, "voice");
            addColumn(db, "error_text");
        }
    }

    private static void addColumn(SQLiteDatabase db, String column) {
        try {
            db.execSQL("ALTER TABLE " + TABLE + " ADD COLUMN " + column + " TEXT");
        } catch (Exception ignored) {
            // 列已存在时忽略
        }
    }

    /** 按 message_id 去重：存在则更新，不存在则插入；返回记录 id */
    public synchronized long upsert(Record r) {
        SQLiteDatabase db = getWritableDatabase();
        long existing = -1;
        long originalTime = r.timeMs;
        if (r.messageId != null && !r.messageId.isEmpty()) {
            Cursor c = db.query(TABLE, new String[]{"id", "time_ms"},
                    "message_id=?", new String[]{r.messageId}, null, null, "id DESC", "1");
            if (c != null) {
                if (c.moveToFirst()) {
                    existing = c.getLong(0);
                    originalTime = c.getLong(1);
                }
                c.close();
            }
        }
        ContentValues v = toValues(r);
        if (existing >= 0) {
            v.remove("time_ms");   // 保留首次收到这条消息的时间
            db.update(TABLE, v, "id=?", new String[]{String.valueOf(existing)});
            return existing;
        }
        v.put("time_ms", originalTime > 0 ? originalTime : System.currentTimeMillis());
        return db.insert(TABLE, null, v);
    }

    public synchronized Record byMessageId(String messageId) {
        if (messageId == null || messageId.isEmpty()) return null;
        Cursor c = getReadableDatabase().query(TABLE, null, "message_id=?",
                new String[]{messageId}, null, null, "id DESC", "1");
        Record r = null;
        if (c != null) {
            if (c.moveToFirst()) r = fromCursor(c);
            c.close();
        }
        return r;
    }

    public synchronized Record get(long id) {
        Cursor c = getReadableDatabase().query(TABLE, null, "id=?",
                new String[]{String.valueOf(id)}, null, null, null, "1");
        Record r = null;
        if (c != null) {
            if (c.moveToFirst()) r = fromCursor(c);
            c.close();
        }
        return r;
    }

    public synchronized List<Record> list(int limit) {
        List<Record> out = new ArrayList<>();
        Cursor c = getReadableDatabase().query(TABLE, null, null, null,
                null, null, "time_ms DESC", String.valueOf(Math.max(1, limit)));
        if (c != null) {
            while (c.moveToNext()) out.add(fromCursor(c));
            c.close();
        }
        return out;
    }

    public synchronized int count() {
        Cursor c = getReadableDatabase().rawQuery("SELECT COUNT(*) FROM " + TABLE, null);
        int n = 0;
        if (c != null) {
            if (c.moveToFirst()) n = c.getInt(0);
            c.close();
        }
        return n;
    }

    public synchronized void delete(long id) {
        getWritableDatabase().delete(TABLE, "id=?", new String[]{String.valueOf(id)});
    }

    public synchronized void clear() {
        getWritableDatabase().delete(TABLE, null, null);
    }

    private static ContentValues toValues(Record r) {
        ContentValues v = new ContentValues();
        v.put("time_ms", r.timeMs);
        v.put("kind", nullToEmpty(r.kind));
        v.put("message_id", nullToEmpty(r.messageId));
        v.put("job_id", nullToEmpty(r.jobId));
        v.put("question", nullToEmpty(r.question));
        v.put("answer", nullToEmpty(r.answer));
        v.put("detail", nullToEmpty(r.detail));
        v.put("ack", nullToEmpty(r.ack));
        v.put("status", nullToEmpty(r.status));
        v.put("employee", nullToEmpty(r.employee));
        v.put("audio_path", nullToEmpty(r.audioPath));
        v.put("video_url", nullToEmpty(r.videoUrl));
        v.put("video_path", nullToEmpty(r.videoPath));
        v.put("photo_url", nullToEmpty(r.photoUrl));
        v.put("mode", nullToEmpty(r.mode));
        v.put("files_json", nullToEmpty(r.filesJson));
        v.put("raw_json", nullToEmpty(r.rawJson));
        v.put("segments_json", nullToEmpty(r.segmentsJson));
        v.put("avatar_id", nullToEmpty(r.avatarId));
        v.put("delegate", nullToEmpty(r.delegate));
        v.put("voice", nullToEmpty(r.voice));
        v.put("error_text", nullToEmpty(r.errorText));
        return v;
    }

    private static Record fromCursor(Cursor c) {
        Record r = new Record();
        r.id = c.getLong(c.getColumnIndexOrThrow("id"));
        r.timeMs = c.getLong(c.getColumnIndexOrThrow("time_ms"));
        r.kind = str(c, "kind");
        r.messageId = str(c, "message_id");
        r.jobId = str(c, "job_id");
        r.question = str(c, "question");
        r.answer = str(c, "answer");
        r.detail = str(c, "detail");
        r.ack = str(c, "ack");
        r.status = str(c, "status");
        r.employee = str(c, "employee");
        r.audioPath = str(c, "audio_path");
        r.videoUrl = str(c, "video_url");
        r.videoPath = str(c, "video_path");
        r.photoUrl = str(c, "photo_url");
        r.mode = str(c, "mode");
        r.filesJson = str(c, "files_json");
        r.rawJson = str(c, "raw_json");
        r.segmentsJson = str(c, "segments_json");
        r.avatarId = str(c, "avatar_id");
        r.delegate = str(c, "delegate");
        r.voice = str(c, "voice");
        r.errorText = str(c, "error_text");
        return r;
    }

    private static String str(Cursor c, String key) {
        int i = c.getColumnIndex(key);
        if (i < 0) return "";
        String s = c.getString(i);
        return s == null ? "" : s;
    }

    private static String nullToEmpty(String s) {
        return s == null ? "" : s;
    }

    /* ===== 附件列表的 JSON 序列化 / 反序列化 ===== */

    public static String filesToJson(List<McpResult.Item> files) {
        JSONArray arr = new JSONArray();
        if (files != null) {
            for (McpResult.Item f : files) {
                JSONObject o = new JSONObject();
                try {
                    o.put("label", f.label.isEmpty() ? ResultStore.labelFor(f.mime) : f.label);
                    o.put("name", f.name);
                    o.put("mime", f.mime);
                    o.put("path", f.path);
                    o.put("uri", f.uri == null ? "" : f.uri.toString());
                    o.put("size", f.size);
                    arr.put(o);
                } catch (JSONException ignored) {
                }
            }
        }
        return arr.toString();
    }

    /** 删除一条历史记录关联的所有本地文件（清空 / 单条删除用） */
    public static void deleteFiles(Record r) {
        if (r == null) return;
        deletePath(r.audioPath);
        deletePath(r.videoPath);
        for (McpResult.Item item : jsonToFiles(r.filesJson)) deletePath(item.path);
    }

    private static void deletePath(String path) {
        if (path == null || path.isEmpty()) return;
        try {
            File f = new File(path);
            if (f.isFile()) f.delete();
        } catch (Exception ignored) {
        }
    }

    public static List<McpResult.Item> jsonToFiles(String json) {
        List<McpResult.Item> out = new ArrayList<>();
        if (json == null || json.isEmpty()) return out;
        try {
            JSONArray arr = new JSONArray(json);
            for (int i = 0; i < arr.length(); i++) {
                JSONObject o = arr.optJSONObject(i);
                if (o == null) continue;
                String uriStr = o.optString("uri", "");
                McpResult.Item item = new McpResult.Item(
                        o.optString("name", ""), o.optString("mime", "application/octet-stream"),
                        o.optString("path", ""), uriStr.isEmpty() ? null : Uri.parse(uriStr),
                        o.optLong("size", 0));
                item.label = o.optString("label", ResultStore.labelFor(item.mime));
                out.add(item);
            }
        } catch (JSONException ignored) {
        }
        return out;
    }
}
