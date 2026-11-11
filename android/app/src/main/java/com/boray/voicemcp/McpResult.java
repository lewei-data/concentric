package com.boray.voicemcp;

import android.net.Uri;

import java.util.ArrayList;
import java.util.List;

/** 一次 MCP 调用 / 一条推送消息的解析结果：核心文本 + 已落盘、可打开查看的文件 */
public final class McpResult {

    public static final class Item {
        public final String name;
        public final String mime;
        public final String path;
        public final Uri uri;
        public final long size;
        /** 展示用的语义标签（如「播报语音」），不暴露真实文件名 */
        public String label = "";

        public Item(String name, String mime, String path, Uri uri, long size) {
            this.name = name;
            this.mime = mime;
            this.path = path;
            this.uri = uri;
            this.size = size;
        }
    }

    /** 兼容旧字段：完整可读文本（含系统字段），UI 已不再展示它 */
    public String displayText = "";
    public String rawText = "";
    public String rawJson = "";
    public boolean isError = false;

    /** 主界面只展示这个：过滤掉系统提示、服务端路径、音频文件名之后的核心内容 */
    public String coreText = "";
    /** 详细文字（voice_job 的 result.detail_text），历史详情里可展开查看 */
    public String detailText = "";
    /** 供数字员工汇报画面做字幕：question / answer（优先取 speech_text） */
    public String questionText = "";
    public String answerText = "";
    /** 「老板，稍等」这类即时回执 */
    public String ackText = "";

    /* ===== 异步任务与消息元数据 ===== */
    public String jobId = "";
    public String status = "";
    /** voice / push / mail，写入历史 */
    public String source = "voice";
    /** 去重标识：job_id 或邮件 uid */
    public String messageId = "";
    /** 播报员工与形象素材 */
    public String employeeName = "";
    public String employeeRole = "";
    public String avatarId = "";
    public String photoUrl = "";
    public String videoUrl = "";
    public String faceUrl = "";
    public String mode = "";
    /** 服务端实际使用的音色（v2.1.0 done 结果里的 voice） */
    public String voice = "";
    /** 语音点名派单结果 */
    public String delegateId = "";
    public String delegateName = "";
    /** status=error 时的错误文本，供提示与重试 */
    public String errorText = "";
    /** 渲染指令：render.driver=wav_envelope / render.lip_sync=amplitude */
    public String renderDriver = "";
    public String lipSync = "";
    /** 句级时间轴原始 JSON（供汇报画面精确分句字幕） */
    public String segmentsJson = "";

    public final List<Item> files = new ArrayList<>();

    public Item audioItem() {
        for (Item item : files) {
            if (item.mime != null && item.mime.startsWith("audio/")) return item;
        }
        return null;
    }

    public Item textItem() {
        for (Item item : files) {
            if (item.mime != null && item.mime.startsWith("text/")) return item;
        }
        return null;
    }

    public Item videoItem() {
        for (Item item : files) {
            if (item.mime != null && item.mime.startsWith("video/")) return item;
        }
        return null;
    }

    /** 主界面/播报用的文本：核心内容优先 */
    public String bestText() {
        if (!coreText.isEmpty()) return coreText;
        if (!answerText.isEmpty()) return answerText;
        return displayText;
    }

    public static McpResult error(String message) {
        McpResult r = new McpResult();
        r.isError = true;
        r.displayText = message;
        r.coreText = message;
        r.rawText = message;
        r.errorText = message;
        return r;
    }
}
