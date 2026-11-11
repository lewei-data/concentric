import com.boray.voicemcp.WsClient;

import java.io.ByteArrayOutputStream;
import java.io.InputStream;
import java.io.OutputStream;
import java.net.ServerSocket;
import java.net.Socket;
import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.util.Base64;
import java.util.List;
import java.util.concurrent.CopyOnWriteArrayList;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.TimeUnit;

/**
 * 帧级自检：用一个极简 WS 服务端逐帧解析 {@link WsClient} 发出的字节。
 *
 * <p>目的：实证上行音频是「多条完整二进制消息（FIN=1 / opcode=0x2）」，
 * 而不是一条分片消息 + continuation。若出现 fin=0 或 opcode=0x0，
 * 对端（Python websockets）就会报 {@code error in fragmented message}（1011）。
 *
 * <p>用法：{@code java FrameCheck}
 */
public class FrameCheck {

    private static final String GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11";
    private static final List<String> frames = new CopyOnWriteArrayList<>();
    private static final CountDownLatch got4 = new CountDownLatch(4);

    public static void main(String[] args) throws Exception {
        final int port = 18999;
        final ServerSocket ss = new ServerSocket(port);
        Thread server = new Thread(() -> {
            try (Socket s = ss.accept()) {
                InputStream in = s.getInputStream();
                OutputStream out = s.getOutputStream();
                String req = readHeaders(in);
                String key = "";
                for (String line : req.split("\r\n")) {
                    if (line.toLowerCase().startsWith("sec-websocket-key:")) {
                        key = line.substring(line.indexOf(':') + 1).trim();
                    }
                }
                String accept = Base64.getEncoder().encodeToString(
                        MessageDigest.getInstance("SHA-1")
                                .digest((key + GUID).getBytes(StandardCharsets.UTF_8)));
                out.write(("HTTP/1.1 101 Switching Protocols\r\n"
                        + "Upgrade: websocket\r\nConnection: Upgrade\r\n"
                        + "Sec-WebSocket-Accept: " + accept + "\r\n\r\n")
                        .getBytes(StandardCharsets.UTF_8));
                out.flush();

                while (got4.getCount() > 0) {
                    int b0 = in.read();
                    if (b0 < 0) break;
                    int b1 = in.read();
                    if (b1 < 0) break;
                    boolean fin = (b0 & 0x80) != 0;
                    int op = b0 & 0x0F;
                    boolean masked = (b1 & 0x80) != 0;
                    long len = b1 & 0x7F;
                    if (len == 126) {
                        len = ((long) in.read() << 8) | in.read();
                    } else if (len == 127) {
                        len = 0;
                        for (int i = 0; i < 8; i++) len = (len << 8) | in.read();
                    }
                    byte[] mask = new byte[4];
                    if (masked) readFully(in, mask, 4);
                    long left = len;
                    byte[] tmp = new byte[8192];
                    while (left > 0) {
                        int n = in.read(tmp, 0, (int) Math.min(tmp.length, left));
                        if (n < 0) break;
                        left -= n;
                    }
                    frames.add("fin=" + (fin ? 1 : 0) + " op=" + op
                            + " len=" + len + " masked=" + (masked ? 1 : 0));
                    got4.countDown();
                }
            } catch (Exception e) {
                System.out.println("SERVER ERR " + e);
            }
        }, "frame-server");
        server.setDaemon(true);
        server.start();

        Thread.sleep(200);
        WsClient c = new WsClient();
        c.connect("ws://127.0.0.1:" + port, new WsClient.Listener() {
            public void onOpen() {
            }

            public void onText(String text) {
            }

            public void onBinary(byte[] data) {
            }

            public void onClosed(int code, String reason) {
            }
        }, 3000);

        byte[] chunk = new byte[3200];
        for (int i = 0; i < chunk.length; i++) chunk[i] = (byte) (i & 0xFF);
        for (int i = 0; i < 3; i++) {
            c.sendBinary(chunk, 0, chunk.length);
        }
        c.sendText("{\"type\":\"audio_end\"}");

        boolean ok4 = got4.await(3, TimeUnit.SECONDS);
        Thread.sleep(150);
        c.close(1000, "done");
        ss.close();

        int badFrag = 0;
        for (String f : frames) {
            if (f.startsWith("fin=0") || f.contains(" op=0")) badFrag++;
            System.out.println("  " + f);
        }
        boolean ok = ok4 && frames.size() == 4 && badFrag == 0
                && frames.get(3).contains("op=1")
                && frames.get(0).contains("op=2")
                && frames.get(0).contains("masked=1");
        System.out.println((ok ? "PASS" : "FAIL")
                + "  frames=" + frames.size() + " 分片帧=" + badFrag);
        System.exit(ok ? 0 : 1);
    }

    private static String readHeaders(InputStream in) throws Exception {
        ByteArrayOutputStream bos = new ByteArrayOutputStream();
        int state = 0;
        while (state < 4) {
            int b = in.read();
            if (b < 0) break;
            bos.write(b);
            if ((state == 0 || state == 2) && b == '\r') state++;
            else if ((state == 1 || state == 3) && b == '\n') state++;
            else state = 0;
        }
        return bos.toString("UTF-8");
    }

    private static void readFully(InputStream in, byte[] buf, int len) throws Exception {
        int off = 0;
        while (off < len) {
            int n = in.read(buf, off, len - off);
            if (n < 0) break;
            off += n;
        }
    }
}
