package com.boray.voicemcp;

import org.json.JSONException;
import org.json.JSONObject;

import java.io.BufferedReader;
import java.io.IOException;
import java.io.InputStreamReader;
import java.net.HttpURLConnection;
import java.net.SocketTimeoutException;
import java.net.URL;
import java.nio.charset.StandardCharsets;

/**
 * MCP Streamable HTTP 的服务器推送通道：GET + Accept: text/event-stream。
 *
 * 服务端实测：GET /conring/asr/mcp 返回 200 text/event-stream 并长期挂起（空闲时无数据），
 * 因此这里按「阻塞读 + 空闲超时重连」实现，收到 data: 行就交给 Sink。
 * 断电/切换网络时自动重连，不抛异常影响其它监听线程。
 */
public final class SseListener {

    public interface Sink {
        void onEvent(JSONObject message);
        void onState(String state);
    }

    private volatile boolean running;
    private HttpURLConnection current;

    /** 阻塞式循环，调用方应在独立线程中执行 */
    public void loop(McpConfig cfg, String sessionId, Sink sink) {
        running = true;
        while (running) {
            HttpURLConnection conn = null;
            try {
                URL url = new URL(cfg.url);
                conn = (HttpURLConnection) url.openConnection();
                conn.setRequestMethod("GET");
                conn.setConnectTimeout(15000);
                conn.setReadTimeout(60000);   // 空闲 60 秒才重连，避免频繁握手
                conn.setUseCaches(false);
                conn.setRequestProperty("Accept", "text/event-stream");
                conn.setRequestProperty("MCP-Protocol-Version", cfg.protocol);
                if (sessionId != null && !sessionId.isEmpty()) {
                    conn.setRequestProperty("Mcp-Session-Id", sessionId);
                }
                if (cfg.token != null && !cfg.token.isEmpty()) {
                    conn.setRequestProperty("Authorization", "Bearer " + cfg.token);
                }
                synchronized (this) { current = conn; }

                int code = conn.getResponseCode();
                if (code != 200) {
                    sink.onState("SSE HTTP " + code);
                    waitBeforeReconnect(6000);
                    continue;
                }
                sink.onState("SSE 已连接");
                BufferedReader reader = new BufferedReader(
                        new InputStreamReader(conn.getInputStream(), StandardCharsets.UTF_8));
                StringBuilder data = new StringBuilder();
                String line;
                while (running && (line = reader.readLine()) != null) {
                    if (line.startsWith(":")) continue;                 // 心跳注释
                    if (line.startsWith("data:")) {
                        data.append(line.substring(5).trim()).append('\n');
                    } else if (line.isEmpty()) {
                        flush(data, sink);
                        data.setLength(0);
                    }
                }
                flush(data, sink);
                sink.onState("SSE 连接关闭，准备重连");
            } catch (SocketTimeoutException e) {
                sink.onState("SSE 空闲超时，重连");
            } catch (IOException e) {
                sink.onState(running ? "SSE 断开：" + e.getMessage() : "SSE 已停止");
            } finally {
                if (conn != null) conn.disconnect();
                synchronized (this) { current = null; }
            }
            waitBeforeReconnect(3000);
        }
    }

    private static void flush(StringBuilder data, Sink sink) {
        if (data.length() == 0) return;
        for (String part : data.toString().split("\\r?\\n")) {
            String s = part.trim();
            if (s.isEmpty() || "[DONE]".equals(s)) continue;
            try {
                sink.onEvent(new JSONObject(s));
            } catch (JSONException ignored) {
                // 非 JSON 事件（纯文本通知）也尝试转成消息
                try {
                    sink.onEvent(new JSONObject().put("text", s));
                } catch (JSONException ignored2) {
                }
            }
        }
    }

    private void waitBeforeReconnect(long ms) {
        long end = System.currentTimeMillis() + ms;
        while (running && System.currentTimeMillis() < end) {
            try {
                Thread.sleep(250);
            } catch (InterruptedException e) {
                Thread.currentThread().interrupt();
                return;
            }
        }
    }

    public void stop() {
        running = false;
        HttpURLConnection c;
        synchronized (this) { c = current; }
        if (c != null) c.disconnect();
    }

    public boolean isRunning() {
        return running;
    }
}
