package com.boray.voicemcp;

import android.content.Context;
import android.os.Handler;
import android.os.Looper;
import android.util.Base64;

import org.json.JSONArray;
import org.json.JSONObject;

import java.io.ByteArrayOutputStream;
import java.io.File;
import java.io.FileInputStream;
import java.io.FileOutputStream;
import java.io.InputStream;
import java.nio.charset.StandardCharsets;
import java.util.ArrayList;
import java.util.List;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;

/**
 * 小秘书（OPC-001，profile=secretary）的老板简报：当前进度 + 待办审批。
 *
 * 数据来源只有一个服务端工具：`boss_briefing`（异步返回 job_id），
 * 再用 `voice_job` 取结果，`result.detail_text` 就是小秘书整理的简报正文，结构固定：
 *
 *   一句话结论
 *   进展
 *   风险与异常
 *   需要你拍板（急件置顶）
 *   【急】1. 开工放行哪批？A …；B …；
 *    建议 A。不决策的后果：…
 *
 * 客户端把它解析成「进度三段 + 待办条目」，用于首页的「当前进度」「待办事项」两个入口；
 * 原始正文同时缓存到应用私有目录，断网时仍能看上次的简报。
 *
 * 审批动作没有专用工具，沿用语音指令链路：把「批准/不批准 + 选项」合成语音后走
 * `voice_chat`，由小秘书真正执行（与老板口头说一句等价）。
 */
public final class Briefing {

    public static final String SECRETARY_ID = "secretary";

    private static final String CACHE = "boss_briefing.json";
    // 懒加载：让 parse() 可以在纯 JVM 上直接测（类初始化不碰 Android 类）
    private static volatile ExecutorService io;
    private static volatile Handler ui;

    private static ExecutorService io() {
        if (io == null) io = Executors.newSingleThreadExecutor();
        return io;
    }

    private static Handler ui() {
        if (ui == null) ui = new Handler(Looper.getMainLooper());
        return ui;
    }

    public interface Callback {
        void onDone(Briefing b, String error);
    }

    public interface ActionCallback {
        void onDone(boolean ok, String message);
    }

    /** 需要老板拍板的一件事 */
    public static final class Todo {
        public String level = "";        // 急 / 缓
        public String title = "";        // 一句话事项（含编号已去掉）
        public String suggestion = "";   // 建议（如「建议 A」）
        public String detail = "";       // 其余说明（不决策的后果等）
        public String id = "";           // 稳定 id（按标题哈希），用于记录已处理

        public boolean urgent() {
            return "急".equals(level);
        }

        public String command(boolean pass) {
            if (pass) {
                String pick = suggestion.isEmpty() ? "" : "，按" + suggestion.replace("建议", "").trim() + "执行";
                return "批准：" + title + pick + "。请立即推进并回报结果。";
            }
            return "不批准：" + title + "。先挂起，说明原因并给出替代方案。";
        }
    }

    public String summary = "";
    public List<String> progress = new ArrayList<>();
    public List<String> risks = new ArrayList<>();
    public List<Todo> todos = new ArrayList<>();
    public String detailText = "";
    public String speechText = "";
    public long fetchedAt = 0;
    public String error = "";

    public boolean empty() {
        return summary.isEmpty() && progress.isEmpty() && risks.isEmpty() && todos.isEmpty();
    }

    // ------------------------------------------------------------------ 缓存

    private static File file(Context c) {
        return new File(c.getFilesDir(), CACHE);
    }

    public static Briefing cached(Context c) {
        try {
            File f = file(c);
            if (!f.isFile()) return new Briefing();
            ByteArrayOutputStream out = new ByteArrayOutputStream();
            try (InputStream in = new FileInputStream(f)) {
                byte[] buf = new byte[8192];
                int n;
                while ((n = in.read(buf)) > 0) out.write(buf, 0, n);
            }
            JSONObject j = new JSONObject(out.toString("UTF-8"));
            Briefing b = parse(j.optString("detail_text", ""));
            b.speechText = j.optString("speech_text", "");
            b.fetchedAt = j.optLong("fetched_at", 0);
            return b;
        } catch (Exception e) {
            return new Briefing();
        }
    }

    private static void save(Context c, Briefing b) {
        try {
            JSONObject j = new JSONObject();
            j.put("detail_text", b.detailText);
            j.put("speech_text", b.speechText);
            j.put("fetched_at", b.fetchedAt);
            try (FileOutputStream os = new FileOutputStream(file(c))) {
                os.write(j.toString().getBytes(StandardCharsets.UTF_8));
            }
        } catch (Exception ignored) {
        }
    }

    // ------------------------------------------------------------------ 拉取

    /** 让小秘书现场巡检并出简报（异步任务，通常 30–120s） */
    public static void refresh(Context c, Callback cb) {
        final McpConfig cfg = McpConfig.load(c).copy();
        io().execute(() -> {
            String error = "";
            Briefing b = null;
            try {
                McpClient client = new McpClient(cfg);
                JSONObject start = client.callTool("boss_briefing",
                        new JSONObject().put("mode", "voice"));
                String text = textOf(start);
                JSONObject sj = new JSONObject(text);
                String jobId = sj.optString("job_id", "");
                if (jobId.isEmpty()) throw new Exception(text.length() > 160
                        ? text.substring(0, 160) : text);

                // 长轮询直到 done
                long deadline = System.currentTimeMillis() + 5 * 60_000L;
                JSONObject done = null;
                while (System.currentTimeMillis() < deadline) {
                    JSONObject res = client.callTool("voice_job",
                            new JSONObject().put("job_id", jobId)
                                    .put("wait_sec", 30)
                                    .put("include_audio", false));
                    JSONObject j = new JSONObject(textOf(res));
                    String status = j.optString("status", "");
                    if ("done".equals(status) || "error".equals(status)) {
                        done = j;
                        break;
                    }
                }
                if (done == null) throw new Exception("小秘书还在巡检，稍后再看");
                JSONObject result = done.optJSONObject("result");
                if (result == null) throw new Exception("简报没有返回内容");
                String detail = result.optString("detail_text", "");
                if (detail.trim().isEmpty()) detail = result.optString("speech_text", "");
                b = parse(detail);
                b.speechText = result.optString("speech_text", "");
                b.fetchedAt = System.currentTimeMillis();
                if (b.empty()) throw new Exception("简报为空，可能小秘书没有可汇报的事");
                save(c, b);
            } catch (Exception e) {
                error = e.getMessage() == null ? "取简报失败" : e.getMessage();
            }
            final Briefing r = b;
            final String err = error;
            ui().post(() -> {
                if (cb != null) cb.onDone(r, err);
            });
        });
    }

    private static String textOf(JSONObject result) {
        if (result == null) return "";
        JSONArray content = result.optJSONArray("content");
        if (content == null) return "";
        StringBuilder sb = new StringBuilder();
        for (int i = 0; i < content.length(); i++) {
            JSONObject blk = content.optJSONObject(i);
            if (blk == null) continue;
            if (!"text".equals(blk.optString("type", ""))) continue;
            String t = blk.optString("text", "");
            if (t.isEmpty()) continue;
            if (sb.length() > 0) sb.append('\n');
            sb.append(t);
        }
        return sb.toString();
    }

    // ------------------------------------------------------------------ 解析

    /** 把 detail_text 拆成「结论 / 进展 / 风险 / 待办」 */
    public static Briefing parse(String detail) {
        Briefing b = new Briefing();
        if (detail == null) return b;
        b.detailText = detail;
        String section = "";
        StringBuilder buf = new StringBuilder();
        String[] lines = detail.split("\n");

        for (String raw : lines) {
            String line = raw == null ? "" : raw.trim();
            if (line.isEmpty()) continue;
            String head = line.replace("：", "").replace(":", "").trim();
            if (head.equals("一句话结论") || head.equals("进展") || head.equals("风险与异常")
                    || head.startsWith("需要你拍板")) {
                flush(b, section, buf);
                section = head.startsWith("需要你拍板") ? "todo" : head;
                continue;
            }
            if (buf.length() > 0) buf.append('\n');
            buf.append(line);
        }
        flush(b, section, buf);

        // 没有识别出待办段落时，退一步：把「需要你拍板」之后的所有内容当一个待办
        if (b.todos.isEmpty()) {
            int idx = detail.indexOf("需要你拍板");
            if (idx >= 0) {
                String tail = detail.substring(idx);
                int nl = tail.indexOf('\n');
                if (nl > 0 && tail.length() > nl + 1) {
                    Todo t = new Todo();
                    t.level = tail.contains("急") ? "急" : "";
                    t.title = "小秘书待拍板事项";
                    t.detail = tail.substring(nl + 1).trim();
                    t.id = idOf(t.title);
                    b.todos.add(t);
                }
            }
        }
        return b;
    }

    private static void flush(Briefing b, String section, StringBuilder buf) {
        String text = buf.toString().trim();
        buf.setLength(0);
        if (text.isEmpty()) return;
        if ("一句话结论".equals(section)) {
            b.summary = text.replace("\n", " ");
        } else if ("进展".equals(section)) {
            b.progress.addAll(items(text));
        } else if ("风险与异常".equals(section)) {
            b.risks.addAll(items(text));
        } else if ("todo".equals(section)) {
            b.todos.addAll(parseTodos(text));
        }
    }

    private static List<String> items(String text) {
        List<String> out = new ArrayList<>();
        for (String raw : text.split("\n")) {
            String s = raw.trim();
            if (s.isEmpty()) continue;
            if (s.startsWith("- ") || s.startsWith("•")) s = s.substring(1).trim();
            else if (s.matches("^\\d+[.、].*")) s = s.replaceFirst("^\\d+[.、]", "").trim();
            if (s.length() > 3) out.add(s);
        }
        return out;
    }

    /** 待办段落：以【急】/【缓】开头的为一个条目，后续缩进行算说明 */
    private static List<Todo> parseTodos(String text) {
        List<Todo> out = new ArrayList<>();
        Todo cur = null;
        StringBuilder tail = new StringBuilder();
        for (String raw : text.split("\n")) {
            String line = raw.trim();
            if (line.isEmpty()) continue;
            boolean newItem = line.startsWith("【");
            if (newItem) {
                if (cur != null) {
                    cur.detail = tail.toString().trim();
                    out.add(cur);
                }
                cur = new Todo();
                int close = line.indexOf('】');
                cur.level = close > 1 ? line.substring(1, close) : "";
                String rest = close > 0 ? line.substring(close + 1).trim() : line;
                rest = rest.replaceFirst("^\\d+[.、]\\s*", "").trim();
                cur.title = rest;
                cur.id = idOf(cur.title);
                tail.setLength(0);
            } else if (cur != null) {
                if (tail.length() > 0) tail.append('\n');
                tail.append(line);
            }
        }
        if (cur != null) {
            cur.detail = tail.toString().trim();
            out.add(cur);
        }
        for (Todo t : out) {
            // 从说明里抠「建议 A」这类结论，审批时作为默认选项
            java.util.regex.Matcher m = java.util.regex.Pattern
                    .compile("建议\\s*([A-Da-d]|授权[^。；\\n]*|按[^。；\\n]*)")
                    .matcher(t.detail);
            if (m.find()) t.suggestion = "建议 " + m.group(1).trim();
        }
        return out;
    }

    private static String idOf(String title) {
        return Integer.toHexString(title == null ? 0 : title.hashCode());
    }

    // ------------------------------------------------------------------ 审批

    /**
     * 审批：把决定说给小秘书（TTS 合成 → voice_chat），由她真正执行。
     * 走的是同一条 MCP 语音链路，和老板口头说一句效果一致。
     */
    public static void decide(Context c, Todo todo, boolean pass, ActionCallback cb) {
        final McpConfig cfg = McpConfig.load(c).copy();
        final String command = todo.command(pass);
        io().execute(() -> {
            String error = "";
            try {
                McpClient client = new McpClient(cfg);
                JSONObject ttsArgs = new JSONObject();
                ttsArgs.put("text", command);
                ttsArgs.put("return_audio", true);
                String voice = EmployeeStore.byId(EmployeeStore.load(c),
                        SECRETARY_ID) != null
                        ? EmployeeStore.byId(EmployeeStore.load(c), SECRETARY_ID).voice : "";
                if (!voice.isEmpty()) ttsArgs.put("voice", voice);
                JSONObject tts = client.callTool("tts_synthesize", ttsArgs);
                String b64 = audioOf(tts);
                if (b64.isEmpty()) throw new Exception("语音合成失败");

                JSONObject args = new JSONObject();
                args.put("audio_base64", b64);
                args.put("language", cfg.language);
                args.put("avatar_id", SECRETARY_ID);
                args.put("profile", SECRETARY_ID);
                args.put("delegate", true);
                args.put("mode", "voice");
                args.put("wait", false);
                client.callTool("voice_chat", args);
            } catch (Exception e) {
                error = e.getMessage() == null ? "提交失败" : e.getMessage();
            }
            final String err = error;
            ui().post(() -> {
                if (cb != null) cb.onDone(err.isEmpty(),
                        err.isEmpty() ? "已提交给小秘书：" + command : err);
            });
        });
    }

    private static String audioOf(JSONObject result) {
        if (result == null) return "";
        JSONArray content = result.optJSONArray("content");
        if (content != null) {
            for (int i = 0; i < content.length(); i++) {
                JSONObject b = content.optJSONObject(i);
                if (b == null) continue;
                if ("audio".equals(b.optString("type", ""))) {
                    String d = b.optString("data", "");
                    if (!d.isEmpty()) return d;
                }
            }
        }
        try {
            JSONObject j = new JSONObject(textOf(result));
            String d = j.optString("audio_base64", "");
            if (!d.isEmpty()) return d;
            String path = j.optString("audio_path", "");
            if (!path.isEmpty()) {
                File f = new File(path);
                if (f.isFile()) {
                    byte[] raw = new byte[(int) f.length()];
                    try (InputStream in = new FileInputStream(f)) {
                        int off = 0, n;
                        while (off < raw.length && (n = in.read(raw, off, raw.length - off)) > 0) {
                            off += n;
                        }
                    }
                    return Base64.encodeToString(raw, Base64.NO_WRAP);
                }
            }
        } catch (Exception ignored) {
        }
        return "";
    }
}
