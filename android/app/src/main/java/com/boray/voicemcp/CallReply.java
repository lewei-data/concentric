package com.boray.voicemcp;

/**
 * 一条结构化回复（WS 文本帧 {@code {"type":"reply",...}}）。
 *
 * <p>0.9.6 起服务端把「念出来的话」和「给眼睛看的字」拆成两层：
 * <ul>
 *   <li>{@link #voiceText} 语音稿——就是下行 TTS 念的内容，只能用于字幕/朗读；</li>
 *   <li>{@link #text} 文字详情——给 App 展示，可能含有语音稿里没有的信息，<b>不能</b>拿去 TTS；</li>
 *   <li>{@link #attachments} 附件共享链接，点开用浏览器/WebView 看。</li>
 * </ul>
 */
public final class CallReply {

    public static final class Attachment {
        public final String name;
        public final String url;

        public Attachment(String name, String url) {
            this.name = name == null || name.trim().isEmpty() ? "附件" : name.trim();
            this.url = url == null ? "" : url.trim();
        }

        public boolean openable() {
            String u = url.toLowerCase(java.util.Locale.ROOT);
            return u.startsWith("http://") || u.startsWith("https://");
        }
    }

    public String mode = "";
    /** 服务端多轮并行时的轮次号；0 = 主动话术（问候/催问/等待） */
    public int turn = 0;
    /** 服务端主动问候（turn=0 且 greeting=true） */
    public boolean greeting = false;
    public String voiceText = "";
    public String detail = "";
    public final java.util.List<Attachment> attachments = new java.util.ArrayList<>();

    /** 字幕用：语音稿优先，没有就退回详情 */
    public String spoken() {
        return voiceText.isEmpty() ? detail : voiceText;
    }

    /** 详情是否值得单独展示（和语音稿一样就没必要重复） */
    public boolean hasExtraDetail() {
        if (detail.isEmpty()) return false;
        return !detail.equals(voiceText);
    }

    public boolean empty() {
        return voiceText.isEmpty() && detail.isEmpty() && attachments.isEmpty();
    }

    /**
     * 服务端「妙记式」收尾会先发一条复述帧（{@code mode=readback}），
     * 同一 turn 稍后才发正式执行结果；复述不能当成任务已完成来展示。
     */
    public boolean isReadback() {
        return "readback".equalsIgnoreCase(mode);
    }

    public static CallReply parse(org.json.JSONObject j) {
        CallReply r = new CallReply();
        if (j == null) return r;
        r.mode = j.optString("mode", "");
        r.turn = j.optInt("turn", 0);
        r.greeting = j.optBoolean("greeting", false);
        r.voiceText = j.optString("voice_text", "").trim();
        r.detail = j.optString("text", "").trim();
        org.json.JSONArray arr = j.optJSONArray("attachments");
        if (arr != null) {
            for (int i = 0; i < arr.length(); i++) {
                org.json.JSONObject a = arr.optJSONObject(i);
                if (a == null) continue;
                String url = a.optString("url", "");
                if (url.isEmpty()) continue;
                r.attachments.add(new Attachment(a.optString("name", ""), url));
            }
        }
        return r;
    }
}
