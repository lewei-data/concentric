package com.boray.voicemcp;

import org.json.JSONArray;
import org.json.JSONException;
import org.json.JSONObject;

import java.io.ByteArrayOutputStream;
import java.io.IOException;
import java.io.InputStream;
import java.io.OutputStream;
import java.net.HttpURLConnection;
import java.net.URL;
import java.nio.charset.StandardCharsets;
import java.util.concurrent.atomic.AtomicInteger;

/**
 * MCP Streamable HTTP 客户端（针对服务器实测：FastMCP / uvicorn，协议 2025-03-26）。
 *
 * 握手流程：POST initialize -> 读取 Mcp-Session-Id -> POST notifications/initialized
 * 之后所有请求带 Mcp-Session-Id；响应可能是 application/json 或 text/event-stream。
 */
public final class McpClient {

    public static final class McpException extends Exception {
        public McpException(String message) { super(message); }
        public McpException(String message, Throwable cause) { super(message, cause); }
    }

    private final McpConfig cfg;
    private final AtomicInteger ids = new AtomicInteger(1);
    private String sessionId;
    private boolean initialized;

    public String serverName = "";
    public String serverVersion = "";
    public String negotiatedProtocol = "";

    public McpClient(McpConfig config) {
        this.cfg = config;
    }

    public synchronized void connect() throws McpException {
        if (initialized) return;
        JSONObject params = new JSONObject();
        JSONObject clientInfo = new JSONObject();
        try {
            params.put("protocolVersion", cfg.protocol);
            params.put("capabilities", new JSONObject());
            clientInfo.put("name", "conring-android");
            clientInfo.put("version", McpConfig.CLIENT_VERSION);
            params.put("clientInfo", clientInfo);
        } catch (JSONException e) {
            throw new McpException("构造 initialize 请求失败", e);
        }

        JSONObject result = rpc("initialize", params, false);
        if (result == null) throw new McpException("initialize 没有返回 result");

        JSONObject info = result.optJSONObject("serverInfo");
        if (info != null) {
            serverName = info.optString("name", "");
            serverVersion = info.optString("version", "");
        }
        negotiatedProtocol = result.optString("protocolVersion", "");

        // 规范要求；部分服务端不实现，失败不阻断后续调用
        try {
            rpc("notifications/initialized", new JSONObject(), true);
        } catch (McpException ignored) {
        }
        initialized = true;
    }

    public JSONObject callTool(String name, JSONObject arguments) throws McpException {
        connect();
        JSONObject params = new JSONObject();
        try {
            params.put("name", name);
            params.put("arguments", arguments == null ? new JSONObject() : arguments);
        } catch (JSONException e) {
            throw new McpException("构造 tools/call 请求失败", e);
        }
        JSONObject result = rpc("tools/call", params, false);
        if (result == null) throw new McpException("tools/call 没有返回 result");
        return result;
    }

    public JSONArray listTools() throws McpException {
        connect();
        JSONObject result = rpc("tools/list", new JSONObject(), false);
        if (result == null) return new JSONArray();
        JSONArray tools = result.optJSONArray("tools");
        return tools == null ? new JSONArray() : tools;
    }

    /** 供 SSE 推送通道复用握手得到的会话 id */
    public synchronized String getSessionId() {
        return sessionId;
    }

    /** 会话失效 / 网络切换后调用，下次请求会重新握手 */
    public synchronized void invalidate() {
        initialized = false;
        sessionId = null;
    }

    private JSONObject rpc(String method, JSONObject params, boolean notification) throws McpException {
        final int id = ids.getAndIncrement();
        JSONObject request = new JSONObject();
        try {
            request.put("jsonrpc", "2.0");
            if (!notification) request.put("id", id);
            request.put("method", method);
            if (params != null) request.put("params", params);
        } catch (JSONException e) {
            throw new McpException("构造 JSON-RPC 请求失败", e);
        }

        HttpURLConnection conn = null;
        try {
            URL url = new URL(cfg.url);
            conn = (HttpURLConnection) url.openConnection();
            conn.setRequestMethod("POST");
            conn.setConnectTimeout(15000);
            // 只有真正执行工具时才会跑几分钟；握手/工具发现用短超时，便于快速报错
            conn.setReadTimeout("tools/call".equals(method) ? cfg.timeoutMs() : 15000);
            conn.setDoOutput(true);
            conn.setUseCaches(false);
            conn.setRequestProperty("Content-Type", "application/json; charset=utf-8");
            conn.setRequestProperty("Accept", "application/json, text/event-stream");
            conn.setRequestProperty("MCP-Protocol-Version", cfg.protocol);
            if (cfg.token != null && !cfg.token.isEmpty()) {
                conn.setRequestProperty("Authorization", "Bearer " + cfg.token);
            }
            // 0.12.0：同域工作台会话，服务端以后给 /conring/* 加鉴权也能直接用
            if (cfg.sessionCookie != null && !cfg.sessionCookie.isEmpty()) {
                conn.setRequestProperty("Cookie", cfg.sessionCookie);
            }
            synchronized (this) {
                if (sessionId != null && !sessionId.isEmpty()) {
                    conn.setRequestProperty("Mcp-Session-Id", sessionId);
                }
            }

            byte[] body = request.toString().getBytes(StandardCharsets.UTF_8);
            conn.setFixedLengthStreamingMode(body.length);
            OutputStream os = conn.getOutputStream();
            os.write(body);
            os.flush();
            os.close();

            int code = conn.getResponseCode();
            String sid = conn.getHeaderField("Mcp-Session-Id");
            if (sid != null && !sid.isEmpty()) {
                synchronized (this) { sessionId = sid; }
            }
            if (notification && (code == 202 || code == 204)) return null;

            InputStream stream = code >= 400 ? conn.getErrorStream() : conn.getInputStream();
            String raw = stream == null ? "" : readAll(stream);
            if (code >= 400) {
                throw new McpException("HTTP " + code + "：" + abbreviate(raw, 300));
            }
            if (raw.trim().isEmpty()) return null;

            String ctype = conn.getContentType();
            JSONObject message;
            String trimmed = raw.trim();
            if ((ctype != null && ctype.contains("text/event-stream"))
                    || trimmed.startsWith("event:") || trimmed.startsWith("data:")) {
                message = parseSse(raw, id);
            } else {
                message = new JSONObject(trimmed);
            }
            if (message == null) return null;
            if (message.has("error")) {
                JSONObject err = message.optJSONObject("error");
                String msg = err == null ? message.toString() : err.optString("message", err.toString());
                throw new McpException("MCP 错误：" + msg);
            }
            if (notification) return null;
            return message.optJSONObject("result");
        } catch (IOException e) {
            throw new McpException("网络错误：" + e.getMessage(), e);
        } catch (JSONException e) {
            throw new McpException("响应解析失败：" + e.getMessage() + " | " + cfg.url, e);
        } finally {
            if (conn != null) conn.disconnect();
        }
    }

    private static JSONObject parseSse(String raw, int expectedId) throws JSONException {
        JSONObject fallback = null;
        for (String line : raw.split("\\r?\\n")) {
            if (!line.startsWith("data:")) continue;
            String data = line.substring(5).trim();
            if (data.isEmpty() || "[DONE]".equals(data)) continue;
            JSONObject msg = new JSONObject(data);
            if (msg.has("result") || msg.has("error")) {
                int rid = msg.optInt("id", -1);
                if (rid == -1 || rid == expectedId) return msg;
            }
            if (fallback == null) fallback = msg;
        }
        return fallback;
    }

    private static String readAll(InputStream in) throws IOException {
        ByteArrayOutputStream out = new ByteArrayOutputStream();
        byte[] buf = new byte[8192];
        int n;
        while ((n = in.read(buf)) != -1) out.write(buf, 0, n);
        return new String(out.toByteArray(), StandardCharsets.UTF_8);
    }

    private static String abbreviate(String s, int max) {
        if (s == null) return "";
        String one = s.replace('\n', ' ').replace('\r', ' ');
        return one.length() > max ? one.substring(0, max) + "…" : one;
    }
}
