package com.boray.voicemcp;

import org.json.JSONArray;
import org.json.JSONObject;

import java.util.ArrayList;
import java.util.Iterator;
import java.util.List;

/**
 * 让「字幕」跟着「声音」出，而不是跟着「网络回执」出。
 *
 * <p>核心是一条时间轴：服务端把 TTS 语音追加到直播流尾（媒体时刻 ≈ {@code edge}），
 * 客户端播放头落后它一段。{@link LiveLevelMeter#frontierSec()} 给出「已经播到哪」的估计，
 * 本类只做一件事：维护句子队列，估算播放位置越过某句的起点时就把这句放出来，
 * 越过它的终点 +200ms 就滚出队列。
 *
 * <p>为什么不按 ack 到达时刻显示：ack 比真实出声早（TTS 合成完就返回），
 * 按它显示必然是「字先说、声音后到」。
 *
 * <p>锚点来源收敛在 {@code meter.frontierSec()}：服务端将来下发 {@code speech_at_seconds} 时，
 * 只需在那一个方法里切换，本类与调用方都不用改。
 */
public final class SpokenTextSync {

    /** 句前提前量：稍微早一点出字，避免"声音到了字还没到" */
    private static final float GUARD_SEC = 0.12f;
    /** 句后保留：说完 200ms 才滚出队列 */
    private static final float TAIL_SEC = 0.20f;
    /** 没有 segments 时的兜底时长 */
    private static final float FALLBACK_SPAN_SEC = 2.0f;
    /** 降级/无锚点时，相对"入流时刻"的固定延迟 */
    private static final float FALLBACK_DELAY_SEC = 0.30f;

    private static final class Seg {
        final float startSec;
        final float endSec;
        final String text;
        /** 这一组的占位行（只有第一句用它替换），可为 null */
        final VoiceCallService.Line placeholder;
        boolean shown;

        Seg(float startSec, float endSec, String text, VoiceCallService.Line placeholder) {
            this.startSec = startSec;
            this.endSec = endSec;
            this.text = text;
            this.placeholder = placeholder;
        }
    }

    public interface Sink {
        void onSegment(VoiceCallService.Line placeholder, String text);
    }

    private final LiveLevelMeter meter;
    private final Sink sink;
    private final List<Seg> queue = new ArrayList<>();

    public SpokenTextSync(LiveLevelMeter meter, Sink sink) {
        this.meter = meter;
        this.sink = sink;
    }

    public synchronized void reset() {
        queue.clear();
    }

    /**
     * 推入一段回复（同一段音频的若干句）。
     *
     * @param placeholder 这段回复的占位行，第一句显示时用它原地替换；可为 null
     * @param pushSec     这段音频入流的媒体时刻（≈ 此刻的 edge）
     * @param segments    服务端的 segments（含 start/end），可为 null
     * @param fallback    没有 segments 时直接用这段文本，起点按 push+0.3s 估
     */
    public synchronized void push(VoiceCallService.Line placeholder, float pushSec,
                                  JSONArray segments, String fallback) {
        if (segments != null && segments.length() > 0) {
            for (int i = 0; i < segments.length(); i++) {
                JSONObject o = segments.optJSONObject(i);
                if (o == null) continue;
                String text = o.optString("text", "").trim();
                if (text.isEmpty()) continue;
                float start = (float) o.optDouble("start", 0);
                float end = (float) o.optDouble("end", start + 1.5);
                queue.add(new Seg(pushSec + start, pushSec + Math.max(end, start + 0.2f),
                        text, i == 0 ? placeholder : null));
            }
            sortQueue();
            return;
        }
        String text = fallback == null ? "" : fallback.trim();
        if (text.isEmpty()) return;
        // 没有逐句时间轴：整段按「入流 300ms 后」出，给足 TTS 播出去的余量
        float start = pushSec + FALLBACK_DELAY_SEC;
        queue.add(new Seg(start, start + FALLBACK_SPAN_SEC, text, placeholder));
        sortQueue();
    }

    /** 由界面每 100ms 调一次 */
    public synchronized void tick() {
        if (queue.isEmpty()) return;
        float now = frontierSec();

        for (Seg s : queue) {
            if (!s.shown && now >= s.startSec - GUARD_SEC) {
                s.shown = true;
                sink.onSegment(s.placeholder, s.text);
            }
        }
        // 说完 200ms 后滚出队列（内容已经进界面，滚出只是别让队列无限长）
        Iterator<Seg> it = queue.iterator();
        while (it.hasNext()) {
            Seg s = it.next();
            if (now >= s.endSec + TAIL_SEC) it.remove();
        }
    }

    /** 唯一的时间源：以后服务端给 speech_at，改 LiveLevelMeter.frontierSec() 即可 */
    private float frontierSec() {
        if (meter == null) return Float.MAX_VALUE;      // 没有锚点：干脆立刻显示
        return meter.frontierSec();
    }

    private void sortQueue() {
        queue.sort((a, b) -> Float.compare(a.startSec, b.startSec));
    }

    /** 当前队列里还有多少句没显示（调试/日志用） */
    public synchronized int pending() {
        int n = 0;
        for (Seg s : queue) if (!s.shown) n++;
        return n;
    }
}
