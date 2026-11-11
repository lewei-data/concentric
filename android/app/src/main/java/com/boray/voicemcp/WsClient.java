package com.boray.voicemcp;

import java.io.BufferedInputStream;
import java.io.ByteArrayOutputStream;
import java.io.IOException;
import java.io.InputStream;
import java.io.OutputStream;
import java.net.InetSocketAddress;
import java.net.Socket;
import java.net.URI;
import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.security.SecureRandom;
import java.util.Base64;
import java.util.Locale;

import javax.net.ssl.SNIHostName;
import javax.net.ssl.SSLParameters;
import javax.net.ssl.SSLSocket;
import javax.net.ssl.SSLSocketFactory;

/**
 * 极简 WebSocket 客户端（RFC 6455），零第三方依赖。
 *
 * <p>只做实时通话需要的那一部分：文本帧 / 二进制帧 / ping-pong / close，
 * 客户端帧一律加掩码，服务端分片消息自动重组。wss 走系统默认信任库，
 * 并开启 HTTPS 主机名校验（等价于浏览器行为）。
 *
 * <p>线程模型：{@link #connect} 在当前线程完成 TCP + TLS + 握手（会阻塞，调用方放子线程），
 * 之后起一条读线程回调 {@link Listener}；发送方在任意线程调用 {@link #sendText}/{@link #sendBinary}，
 * 内部串行化，不会交叉写坏帧。
 */
public final class WsClient {

    public interface Listener {
        void onOpen();

        void onText(String text);

        void onBinary(byte[] data);

        /** 连接关闭（正常或异常都走这里，code=0 表示没有握手层错误） */
        void onClosed(int code, String reason);
    }

    private static final String GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11";
    private static final int OP_TEXT = 0x1;
    private static final int OP_BIN = 0x2;
    private static final int OP_CLOSE = 0x8;
    private static final int OP_PING = 0x9;
    private static final int OP_PONG = 0xA;

    private Socket raw;
    private Socket socket;
    private InputStream in;
    private OutputStream out;
    private final Object writeLock = new Object();
    private volatile boolean open = false;
    private volatile boolean closing = false;
    private Thread reader;
    private Listener listener;

    public boolean isOpen() {
        return open;
    }

    /**
     * 建立连接并握手。成功返回后读线程已启动。
     *
     * @param wsUrl     ws:// 或 wss:// 地址
     * @param timeoutMs 连接 + 握手超时
     */
    public void connect(String wsUrl, Listener l, int timeoutMs) throws IOException {
        listener = l;
        try {
            doConnect(wsUrl, timeoutMs);
        } catch (IOException | RuntimeException e) {
            release();
            throw e;
        }
    }

    private void doConnect(String wsUrl, int timeoutMs) throws IOException {
        URI uri = URI.create(wsUrl);
        String scheme = uri.getScheme() == null ? "" : uri.getScheme().toLowerCase(Locale.ROOT);
        boolean tls = "wss".equals(scheme) || "https".equals(scheme);
        if (!tls && !"ws".equals(scheme) && !"http".equals(scheme)) {
            throw new IOException("不支持的协议：" + scheme);
        }
        String host = uri.getHost();
        if (host == null || host.isEmpty()) throw new IOException("地址缺少主机名");
        int port = uri.getPort() > 0 ? uri.getPort() : (tls ? 443 : 80);
        String path = uri.getRawPath() == null || uri.getRawPath().isEmpty() ? "/" : uri.getRawPath();
        if (uri.getRawQuery() != null) path += "?" + uri.getRawQuery();

        raw = new Socket();
        raw.setTcpNoDelay(true);
        raw.connect(new InetSocketAddress(host, port), timeoutMs);
        raw.setSoTimeout(0);

        if (tls) {
            SSLSocketFactory f = (SSLSocketFactory) SSLSocketFactory.getDefault();
            SSLSocket ss = (SSLSocket) f.createSocket(raw, host, port, true);
            SSLParameters p = ss.getSSLParameters();
            p.setEndpointIdentificationAlgorithm("HTTPS");     // 证书必须匹配域名
            if (!isIpLiteral(host)) {
                try {
                    p.setServerNames(java.util.Collections.singletonList(new SNIHostName(host)));
                } catch (Exception ignored) {
                }
            }
            ss.setSSLParameters(p);
            ss.startHandshake();
            socket = ss;
        } else {
            socket = raw;
        }
        in = new BufferedInputStream(socket.getInputStream(), 16 * 1024);
        out = socket.getOutputStream();

        StringBuilder sb = new StringBuilder();
        sb.append("GET ").append(path).append(" HTTP/1.1\r\n");
        String hostHeader = port == (tls ? 443 : 80) ? host : host + ":" + port;
        sb.append("Host: ").append(hostHeader).append("\r\n");
        sb.append("Upgrade: websocket\r\n");
        sb.append("Connection: Upgrade\r\n");
        String key = newKey();
        sb.append("Sec-WebSocket-Key: ").append(key).append("\r\n");
        sb.append("Sec-WebSocket-Version: 13\r\n");
        sb.append("User-Agent: TongXinYuan/" + McpConfig.CLIENT_VERSION + "\r\n");
        sb.append("\r\n");
        out.write(sb.toString().getBytes(StandardCharsets.UTF_8));
        out.flush();

        String status = readLine(in);
        if (status == null) throw new IOException("握手无响应");
        if (!status.contains("101")) {
            // 4404 之类由服务端在握手后关，这里只处理 HTTP 错误
            throw new IOException("握手失败：" + status.trim());
        }
        String accept = null;
        String line;
        int guard = 0;
        while ((line = readLine(in)) != null && !line.isEmpty()) {
            int c = line.indexOf(':');
            if (c > 0 && "sec-websocket-accept".equalsIgnoreCase(line.substring(0, c).trim())) {
                accept = line.substring(c + 1).trim();
            }
            if (++guard > 64) break;
        }
        String want = acceptOf(key);
        if (accept == null || !accept.equals(want)) {
            throw new IOException("Sec-WebSocket-Accept 校验失败");
        }
        open = true;
        if (listener != null) listener.onOpen();
        reader = new Thread(this::readLoop, "ws-reader");
        reader.setDaemon(true);
        reader.start();
    }

    public boolean sendText(String text) {
        if (!open) return false;
        byte[] b = text.getBytes(StandardCharsets.UTF_8);
        return sendFrame(OP_TEXT, b, 0, b.length);
    }

    public boolean sendBinary(byte[] data, int off, int len) {
        if (!open) return false;
        return sendFrame(OP_BIN, data, off, len);
    }

    /** 主动关闭：发 close 帧（等 400ms 让服务端收尾），再断 socket */
    public void close(int code, String reason) {
        if (!open) {
            release();
            return;
        }
        closing = true;
        try {
            byte[] payload = new byte[2 + (reason == null ? 0 : reason.getBytes(StandardCharsets.UTF_8).length)];
            payload[0] = (byte) ((code >> 8) & 0xFF);
            payload[1] = (byte) (code & 0xFF);
            if (reason != null) {
                byte[] r = reason.getBytes(StandardCharsets.UTF_8);
                System.arraycopy(r, 0, payload, 2, r.length);
            }
            sendFrame(OP_CLOSE, payload, 0, payload.length);
        } catch (Exception ignored) {
        }
        open = false;
        sleep(150);
        release();
    }

    private void release() {
        open = false;
        try {
            if (socket != null) socket.close();
        } catch (Exception ignored) {
        }
        try {
            if (raw != null) raw.close();
        } catch (Exception ignored) {
        }
        socket = null;
        raw = null;
    }

    // ------------------------------------------------------------------ 读

    private void readLoop() {
        int code = 0;
        String reason = "";
        try {
            ByteArrayOutputStream frag = new ByteArrayOutputStream();
            int fragOp = 0;
            while (open) {
                Frame f = readFrame();
                if (f == null) break;
                if (f.opcode == OP_CLOSE) {
                    code = f.payload.length >= 2
                            ? ((f.payload[0] & 0xFF) << 8) | (f.payload[1] & 0xFF) : 1005;
                    if (f.payload.length > 2) {
                        reason = new String(f.payload, 2, f.payload.length - 2, StandardCharsets.UTF_8);
                    }
                    break;
                }
                if (f.opcode == OP_PING) {
                    sendFrame(OP_PONG, f.payload, 0, f.payload.length);
                    continue;
                }
                if (f.opcode == OP_PONG) continue;

                if (f.opcode == OP_TEXT || f.opcode == OP_BIN) {
                    if (f.fin) {
                        dispatch(f.opcode, f.payload);
                    } else {
                        frag.reset();
                        frag.write(f.payload);
                        fragOp = f.opcode;
                    }
                } else if (f.opcode == 0x0) {              // 续帧
                    frag.write(f.payload);
                    if (f.fin) {
                        dispatch(fragOp, frag.toByteArray());
                        frag.reset();
                        fragOp = 0;
                    }
                }
            }
        } catch (Exception e) {
            reason = e.getMessage() == null ? e.getClass().getSimpleName() : e.getMessage();
        } finally {
            open = false;
            release();
            // 总是回调：主动 close 由上层用 userClosed 过滤；心跳超时这类「自己断的」
            // 必须让上层知道，否则不会触发重连。
            if (listener != null) listener.onClosed(code, reason);
        }
    }

    private void dispatch(int opcode, byte[] payload) {
        Listener l = listener;
        if (l == null) return;
        if (opcode == OP_TEXT) l.onText(new String(payload, StandardCharsets.UTF_8));
        else l.onBinary(payload);
    }

    private static final class Frame {
        boolean fin;
        int opcode;
        byte[] payload;
    }

    private Frame readFrame() throws IOException {
        int b0 = in.read();
        if (b0 < 0) return null;
        int b1 = in.read();
        if (b1 < 0) return null;
        Frame f = new Frame();
        f.fin = (b0 & 0x80) != 0;
        f.opcode = b0 & 0x0F;
        boolean masked = (b1 & 0x80) != 0;
        long len = b1 & 0x7F;
        if (len == 126) {
            len = ((long) byteAt() << 8) | byteAt();
        } else if (len == 127) {
            len = 0;
            for (int i = 0; i < 8; i++) len = (len << 8) | byteAt();
        }
        if (len > 8L * 1024 * 1024) throw new IOException("帧过大：" + len);
        byte[] mask = null;
        if (masked) {
            mask = new byte[4];
            readFully(mask, 0, 4);
        }
        byte[] payload = new byte[(int) len];
        readFully(payload, 0, payload.length);
        if (masked) {
            for (int i = 0; i < payload.length; i++) payload[i] ^= mask[i & 3];
        }
        f.payload = payload;
        return f;
    }

    private int byteAt() throws IOException {
        int v = in.read();
        if (v < 0) throw new IOException("连接已断开");
        return v;
    }

    private void readFully(byte[] buf, int off, int len) throws IOException {
        int got = 0;
        while (got < len) {
            int n = in.read(buf, off + got, len - got);
            if (n < 0) throw new IOException("连接已断开");
            got += n;
        }
    }

    // ------------------------------------------------------------------ 写

    /**
     * 发一帧。
     *
     * <p>首字节固定 {@code 0x80 | opcode}：FIN=1 表示这是**一条完整消息**，
     * 不做 continuation 分片；opcode 0x2 = binary、0x1 = text。
     * 音频按 3200B 切成多条独立消息（服务端顺序累加），而不是切成一条分片消息，
     * 否则中间插入 JSON 文本帧会让对端报 "error in fragmented message"。
     */
    private boolean sendFrame(int opcode, byte[] data, int off, int len) {
        if (!open || socket == null) return false;
        synchronized (writeLock) {
            try {
                ByteArrayOutputStream hdr = new ByteArrayOutputStream(14);
                hdr.write(0x80 | opcode);
                if (len <= 125) {
                    hdr.write(0x80 | len);
                } else if (len <= 0xFFFF) {
                    hdr.write(0x80 | 126);
                    hdr.write((len >>> 8) & 0xFF);
                    hdr.write(len & 0xFF);
                } else {
                    hdr.write(0x80 | 127);
                    long l = len;
                    for (int i = 7; i >= 0; i--) hdr.write((int) ((l >>> (8 * i)) & 0xFF));
                }
                byte[] mask = new byte[4];
                RANDOM.nextBytes(mask);
                hdr.write(mask);
                byte[] head = hdr.toByteArray();

                byte[] buf = new byte[head.length + len];
                System.arraycopy(head, 0, buf, 0, head.length);
                for (int i = 0; i < len; i++) {
                    buf[head.length + i] = (byte) (data[off + i] ^ mask[i & 3]);
                }
                out.write(buf);
                out.flush();
                return true;
            } catch (Exception e) {
                return false;
            }
        }
    }

    // ------------------------------------------------------------------ 工具

    private static final SecureRandom RANDOM = new SecureRandom();

    private static String newKey() {
        byte[] nonce = new byte[16];
        RANDOM.nextBytes(nonce);
        return Base64.getEncoder().encodeToString(nonce);
    }

    private static String acceptOf(String key) {
        try {
            MessageDigest sha1 = MessageDigest.getInstance("SHA-1");
            byte[] d = sha1.digest((key + GUID).getBytes(StandardCharsets.UTF_8));
            return Base64.getEncoder().encodeToString(d);
        } catch (Exception e) {
            return "";
        }
    }

    private static String readLine(InputStream in) throws IOException {
        ByteArrayOutputStream bos = new ByteArrayOutputStream();
        int c;
        while ((c = in.read()) >= 0) {
            if (c == '\n') break;
            if (c != '\r') bos.write(c);
            if (bos.size() > 8192) break;
        }
        if (c < 0 && bos.size() == 0) return null;
        return new String(bos.toByteArray(), StandardCharsets.ISO_8859_1);
    }

    private static boolean isIpLiteral(String host) {
        if (host.indexOf(':') >= 0) return true;
        return host.matches("^\\d{1,3}(\\.\\d{1,3}){3}$");
    }

    private static void sleep(long ms) {
        try {
            Thread.sleep(ms);
        } catch (InterruptedException e) {
            Thread.currentThread().interrupt();
        }
    }
}
