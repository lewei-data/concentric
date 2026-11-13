package com.boray.voicemcp;

import android.os.Handler;
import android.os.Looper;
import android.util.Log;

import org.json.JSONObject;

import java.util.ArrayList;
import java.util.List;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.atomic.AtomicBoolean;

/**
 * P0 实时通道（dispatch WS，0.9.6）。
 *
 * <p>与旧的 {@code live_call + HLS} 是两条并行的实时链路：
 * <ul>
 *   <li>旧链路：服务端把语音混进 HLS，客户端只播；上行另走 {@code voice_chat}。</li>
 *   <li>本类：一条双向 WebSocket，上行推 WAV、下行收 ack/回复语音+结构化文字，
 *       语音延迟更低，且能拿到 {@code text/voice_text/attachments/mode} 分层数据。</li>
 * </ul>
 *
 * <p>协议（服务端 realtime_ws.py，2026-11-14 校对）：
 * <pre>
 * 上行  {"type":"hello"} / 二进制 WAV 帧 / {"type":"audio_end"} / {"type":"ping"} / {"type":"close"}
 * 下行  session_started / hello_ok / transcript{text,turn,confidence} /
 *      二进制音频（"收到" ack / TTS，44B 头 + 100ms 分块）/
 *      reply{text,voice_text,mode,attachments,turn,greeting} /
 *      reply_end / barge_in / pong / error / bye / timeout
 * </pre>
 *
 * <p><b>后台处理模式（0.12.0 校对）</b>：服务端已改成「妙记式听写」——
 * 每句语音立刻回一声「收到」、ASR 只落草稿（transcript），不再每轮跑一次 hermes；
 * 听到「没了 / 结束 / 就这些」或静音 12s 才收尾，先发一条
 * {@code reply mode="readback"} 复述整段口述，再一次性执行并回正式 reply。
 * 所以 reply 的 turn 会跳号（transcript 也占轮次）、同一 turn 还会连发两条，
 * 客户端不能按 turn 严格递增排队，否则回复会被永久卡在队列里。
 *
 * <p>断线不用重建会话：服务端 REATTACH_GRACE 已从 30s 提到 300s，channel_id 不变即恢复。
 */
public final class RealtimeChannel {

    private static final String TAG = "RealtimeChannel";
    /** 服务端 REATTACH_GRACE=300s；与其同步，别在服务端还等着时放弃重连 */
    private static final long GRACE_MS = 300_000;
    private static final long PING_MS = 12_000;
    /** 连续多少次 ping 没有 pong 就认为连接已死 */
    private static final int PING_MISS_LIMIT = 3;
    private static final int AUDIO_CHUNK = 3200;      // 100ms @16k/16bit
    /** 单帧上限：一句话 ≤1MB（约 32s @16k/16bit）就整段一次发完，不带分片 */
    /**
     * 握手成功不等于服务端认了这个 channel：边缘代理可能"假接受"升级请求
     * （101 回来了但后端没转发），此时上行全丢、还收不到任何帧。
     * 必须等到 session_started / hello_ok 才算真就绪。
     */
    private static final long READY_WAIT_MS = 4000;
    private static final int AUDIO_MAX_FRAME = 1024 * 1024;

    public interface Listener {
        /** 状态文案（重连中、已恢复…） */
        void onStatus(String text);

        /** 通道就绪；reconnected=true 表示断线后恢复 */
        void onReady(boolean reconnected);

        /** 服务端 ASR 回显（自己的话） */
        void onTranscript(String text);

        /** 下行音频帧（可能是 WAV 头，也可能是 PCM 载荷），直接喂播放器 */
        void onAudio(byte[] data);

        void onReply(CallReply reply);

        void onReplyEnd();

        /** 服务端检测到插话，已停止播报 */
        void onBargeIn();

        /** 服务端单独下发的文本消息（非转写、非 reply），也要进会话记录 */
        void onServerText(String text);

        void onServerError(String message);

        /** 通道彻底不可用（重连超时 / 会话结束） */
        void onClosed(String reason);
    }

    private final DispatchApi.Submit submit;
    private final List<String> urls = new ArrayList<>();
    private final Listener listener;
    private final Handler ui = new Handler(Looper.getMainLooper());
    private final ExecutorService io = Executors.newSingleThreadExecutor();
    private final AtomicBoolean userClosed = new AtomicBoolean(false);
    private final AtomicBoolean fatal = new AtomicBoolean(false);

    private volatile WsClient ws;
    private volatile boolean ready = false;
    /** 收到 session_started / hello_ok 即放行；用于识别"假连接" */
    private volatile java.util.concurrent.CountDownLatch readyLatch;
    /** 正在推一句音频：这期间不发应用层 ping，避免 JSON 文本帧插进音频流中间 */
    private volatile boolean uplinkBusy = false;
    /** 服务端对 close 的确认（bye）；挂断要等它，别让服务端在收尾时被硬断 */
    private volatile boolean byeSeen = false;
    /** 上行单独一条线程：不能被重连循环或收帧线程挡住 */
    private final java.util.concurrent.ExecutorService senderIo =
            java.util.concurrent.Executors.newSingleThreadExecutor(r -> {
                Thread t = new Thread(r, "ws-uplink");
                t.setDaemon(true);
                return t;
            });
    /**
     * 短时间窗内按轮次整理乱序帧。妙记式下同一 turn 会有 readback + 正式回复两条，
     * 所以用 List 存一桶；turn 跳号是常态（transcript 也占轮次），全部都要放行。
     */
    private final java.util.TreeMap<Integer, List<CallReply>> pendingReplies = new java.util.TreeMap<>();
    private int nextTurn = 1;
    private final Runnable flushReplies = this::drainReplies;
    private volatile long lastPongAt = 0;
    private int pingMiss = 0;

    public RealtimeChannel(DispatchApi.Submit submit, List<String> candidateUrls, Listener l) {
        this.submit = submit;
        this.urls.addAll(candidateUrls);
        this.listener = l;
    }

    public String taskId() {
        return submit == null ? "" : submit.taskId;
    }

    public String channelId() {
        return submit == null ? "" : submit.channelId;
    }

    public boolean isReady() {
        return ready;
    }

    /**
     * 建立连接（阻塞，调用方放子线程）。按 urls 顺序逐个尝试（公网 → 内网）。
     * 连上后不回调 onReady，由调用方接好依赖再主动通知界面。
     *
     * @return 是否连上
     */
    public boolean openBlocking() {
        for (String url : urls) {
            if (url == null || url.isEmpty() || userClosed.get()) continue;
            if (tryConnect(url)) {
                // 就绪回调交给 initiator：它还要把 channel 字段、播放器、transport 都接好，
                // 太早回调会让界面提前「已接通」、开麦时 channel 还是 null。
                return true;
            }
        }
        return false;
    }

    private boolean tryConnect(String url) {
        // 单 socket 原则：先把上一条连接关掉，再连新的。
        // 服务端会把同 channel 的旧连接按 4409 替换，两个 socket 并存只会互相踢。
        WsClient prev = ws;
        ws = null;
        ready = false;
        if (prev != null) {
            try { prev.close(1000, "replace"); } catch (Exception ignored) { }
        }
        WsClient c = new WsClient();
        java.util.concurrent.CountDownLatch latch = new java.util.concurrent.CountDownLatch(1);
        readyLatch = latch;
        try {
            c.connect(url, listenerFor(c), 6000);
        } catch (Exception e) {
            readyLatch = null;
            Log.w(TAG, "connect failed " + url + " : " + e);
            return false;
        }
        boolean accepted;
        try {
            accepted = latch.await(READY_WAIT_MS, java.util.concurrent.TimeUnit.MILLISECONDS);
        } catch (InterruptedException e) {
            Thread.currentThread().interrupt();
            accepted = false;
        }
        if (!accepted) {
            Log.w(TAG, "handshake ok but no session_started in " + READY_WAIT_MS + "ms: " + url);
            readyLatch = null;
            try { c.close(1000, "no session"); } catch (Exception ignored) { }
            return false;
        }
        readyLatch = null;
        ws = c;
        ready = true;
        lastPongAt = System.currentTimeMillis();
        pingMiss = 0;
        Log.i(TAG, "connected " + url);
        c.sendText("{\"type\":\"hello\"}");
        ui.removeCallbacks(pingTask);
        ui.postDelayed(pingTask, PING_MS);
        return true;
    }

    /** 上行一句话：整段 WAV（含 44B 头）分块发出，再显式 audio_end */
    public void sendUtterance(byte[] wav) {
        if (wav == null || wav.length == 0) return;
        final byte[] data = wav;
        try {
            senderIo.execute(() -> sendUtteranceNow(data));
        } catch (java.util.concurrent.RejectedExecutionException e) {
            // 通道已 close：必须回调，让上层把这一句转存待发队列，不能静默丢
            listener.onStatus("通道已关闭，这句没发出去");
        }
    }

    private void sendUtteranceNow(byte[] data) {
        WsClient c = ws;
        if (c == null || !c.isOpen() || !ready) {
            listener.onStatus("通道未就绪，这句没发出去");
            return;
        }
        uplinkBusy = true;
        try {
            // 一句话优先「一次发完」——一个 binary 消息装整段 WAV。
            // 多次小帧虽然每帧也 FIN=1，但中间容易被 ping/close 之类的文本帧插进来，
            // 对端在关闭或分片状态机里就会报 "error in fragmented message"。
            if (data.length <= AUDIO_MAX_FRAME) {
                if (!c.sendBinary(data, 0, data.length)) {
                    listener.onStatus("上行发送失败，正在恢复…");
                    return;
                }
                c.sendText("{\"type\":\"audio_end\"}");
                return;
            }
            // 超长句（>1MB，极少见）才退回分块，仍是每块一条完整消息
            for (int off = 0; off < data.length; off += AUDIO_CHUNK) {
                // 每帧都重新确认：断线（ready=false）或挂断时立刻停，
                // 否则会和 close 帧并发写，服务端在关闭流程里收到二进制帧就报 1011
                if (!ready || userClosed.get() || c != ws) return;
                int n = Math.min(AUDIO_CHUNK, data.length - off);
                if (!c.sendBinary(data, off, n)) {
                    listener.onStatus("上行发送失败，正在恢复…");
                    return;
                }
                sleep(20);
            }
            c.sendText("{\"type\":\"audio_end\"}");
        } finally {
            uplinkBusy = false;
        }
    }

    /** 挂断：先 close 帧让服务端收尾（回 bye、结单），再断开 */
    public void close() {
        if (!userClosed.compareAndSet(false, true)) return;
        ui.removeCallbacks(pingTask);
        // 先掐上行，再发 close：绝不能让「音频帧」和「close 文本帧」并发写
        uplinkBusy = false;
        try { senderIo.shutdownNow(); } catch (Exception ignored) { }
        final WsClient c = ws;
        try {
            io.execute(() -> {
                if (c != null && c.isOpen()) {
                    c.sendText("{\"type\":\"close\"}");
                    // 等 bye 再断：服务端收到 close 后要写 bye、停播、结单；
                    // 硬断会让它在「关闭流程中」继续写，对端就报 1011
                    for (int i = 0; i < 18 && !byeSeen; i++) sleep(50);
                    c.close(1000, "hangup");
                }
                ready = false;
                senderIo.shutdown();
                io.shutdown();
            });
        } catch (java.util.concurrent.RejectedExecutionException ignored) {
            // 已经关过了
        }
    }

    private void sleep(long ms) {
        try {
            Thread.sleep(ms);
        } catch (InterruptedException e) {
            Thread.currentThread().interrupt();
        }
    }

    // ------------------------------------------------------------------ 心跳

    private final Runnable pingTask = new Runnable() {
        @Override
        public void run() {
            if (userClosed.get() || fatal.get()) return;
            WsClient c = ws;
            if (c != null && c.isOpen()) {
                if (System.currentTimeMillis() - lastPongAt > PING_MS * PING_MISS_LIMIT) {
                    Log.w(TAG, "心跳超时，主动断开重连");
                    c.close(4000, "ping timeout");
                } else if (!uplinkBusy) {
                    // 一句话通常 2~10s，推流期间不插文本帧；推完下一拍再补 ping
                    c.sendText("{\"type\":\"ping\"}");
                }
            }
            if (!userClosed.get() && !fatal.get()) ui.postDelayed(this, PING_MS);
        }
    };

    // ------------------------------------------------------------------ 收帧

    /**
     * 每条连接一个 listener：回调带上发起它的 client，
     * 这样「旧连接被新连接替换」时的 onClosed 能被识别并忽略，不会触发重连风暴。
     */
    private WsClient.Listener listenerFor(final WsClient owner) {
        return new WsClient.Listener() {
            @Override
            public void onOpen() {
            }

            @Override
            public void onText(String text) {
                if (owner != ws) return;              // 旧连接残留帧，丢弃
                handleText(text);
            }

            @Override
            public void onBinary(byte[] data) {
                if (owner != ws) return;
                lastPongAt = System.currentTimeMillis();
                listener.onAudio(data);
            }

            @Override
            public void onClosed(int code, String reason) {
                if (owner != ws) {
                    Log.i(TAG, "old socket closed(" + code + ")，忽略");
                    return;
                }
                handleClosed(code, reason);
            }
        };
    }

    private void handleText(String text) {
        lastPongAt = System.currentTimeMillis();
        if (text == null || text.isEmpty()) return;
        JSONObject j;
        try {
            j = new JSONObject(text);
        } catch (Exception e) {
            return;
        }
        String type = j.optString("type", "");
        switch (type) {
            case "session_started":
            case "hello_ok": {
                java.util.concurrent.CountDownLatch l = readyLatch;
                if (l != null) l.countDown();
                break;
            }
            case "transcript": {
                String t = j.optString("text", "").trim();
                if (!t.isEmpty()) listener.onTranscript(t);
                else listener.onStatus(j.optString("note", "没听清"));
                break;
            }
            case "reply":
                enqueueReply(CallReply.parse(j));
                break;
            case "reply_end":
                listener.onReplyEnd();
                break;
            case "barge_in":
                listener.onBargeIn();
                break;
            case "message":
            case "text": {
                String t = j.optString("text", j.optString("message", "")).trim();
                if (!t.isEmpty()) listener.onServerText(t);
                break;
            }
            case "pong":
                pingMiss = 0;
                break;
            case "bye":
                byeSeen = true;
                break;
            case "timeout":
                // 服务端 IDLE_TIMEOUT=300s：这段时间一句都没说，会话已经结束，
                // 继续挂着只会显示「通话中」却没人应答，直接收尾更诚实。
                listener.onClosed("长时间无对话，通话已结束");
                break;
            case "error":
                listener.onServerError(j.optString("message", "服务端错误"));
                break;
            default:
                Log.d(TAG, "ignore frame " + type);
                break;
        }
    }

    /**
     * 只做 300ms 的乱序整理，不再要求 turn 从 1 连续递增：
     * 妙记式下 transcript 消耗轮次，第一条 reply 的 turn 往往已经是 4、5，
     * 旧的「等 nextTurn」逻辑会把所有回复永久留在队列里。
     * turn<=0 是服务端主动话术（问候/催问），立即显示。
     */
    private void enqueueReply(CallReply r) {
        if (r == null) return;
        if (r.turn <= 0) {
            listener.onReply(r);
            return;
        }
        synchronized (pendingReplies) {
            List<CallReply> bucket = pendingReplies.get(r.turn);
            if (bucket == null) {
                bucket = new ArrayList<>();
                pendingReplies.put(r.turn, bucket);
            }
            bucket.add(r);                    // 同 turn：readback 在前、正式回复在后
        }
        ui.removeCallbacks(flushReplies);
        ui.postDelayed(flushReplies, 300);
    }

    /** 按 turn 升序把队列全部放行；同轮次保持服务端下发顺序，不丢帧 */
    private void drainReplies() {
        while (true) {
            List<CallReply> bucket;
            synchronized (pendingReplies) {
                if (pendingReplies.isEmpty()) return;
                int key = pendingReplies.firstKey();
                bucket = pendingReplies.remove(key);
                nextTurn = key + 1;
            }
            if (bucket == null) continue;
            for (CallReply r : bucket) listener.onReply(r);
        }
    }

    private void handleClosed(int code, String reason) {
        ready = false;
        ui.removeCallbacks(pingTask);
        if (userClosed.get() || fatal.get()) return;
        if (code == 4404) {                       // channel 不存在或已结束
            fatal.set(true);
            listener.onClosed("会话已结束");
            return;
        }
        if (code == 4409) {                       // 同一 channel 被新连接替换
            fatal.set(true);
            listener.onClosed("同一会话已在别处接入，本条连接已退出");
            return;
        }
        Log.w(TAG, "closed " + code + " " + reason + "，尝试用同一 channel 重连");
        listener.onStatus("连接中断，正在恢复…");
        try {
            io.execute(() -> reconnectLoop());
        } catch (java.util.concurrent.RejectedExecutionException ignored) {
            // 已经 close() 过了
        }
    }

    private void reconnectLoop() {
        long deadline = System.currentTimeMillis() + GRACE_MS;
        int attempt = 0;
        while (!userClosed.get() && !fatal.get() && System.currentTimeMillis() < deadline) {
            for (String url : urls) {
                if (userClosed.get() || fatal.get()) return;
                if (url == null || url.isEmpty()) continue;
                if (tryConnect(url)) {
                    listener.onStatus("已恢复");
                    listener.onReady(true);
                    return;
                }
            }
            attempt++;
            sleep(Math.min(4000, 400L * attempt));
        }
        if (userClosed.get()) return;
        fatal.set(true);
        listener.onClosed("实时通道断开超过 5 分钟（重连窗口已过）");
    }
}
