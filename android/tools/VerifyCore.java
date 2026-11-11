import com.boray.voicemcp.ResultParser;

/** 纯 JVM 校验：核心内容过滤是否去掉系统提示、服务端路径与音频文件名 */
public class VerifyCore {
    public static void main(String[] args) {
        String noisy = "识别到的指令：请帮我查一下今天的安排\n"
                + "回复内容：已经整理好了\n"
                + "audio_path：/opt/sefs/mcp-tts/output/voice_1794257391.wav\n"
                + "srt_path：/opt/sefs/mcp-tts/output/voice_1794257391.srt\n"
                + "segments_path：/opt/sefs/mcp-tts/output/voice_1794257391.segments.json\n"
                + "音频时长(秒)：15.577\n"
                + "tts_1794257305.wav 已生成，可直接播放\n"
                + "这段是真正要给用户看的核心内容。\n"
                + "第二行核心内容：会议在下午三点开始。";
        String clean = ResultParser.cleanCore(noisy);
        System.out.println("=== 过滤前 ===");
        System.out.println(noisy);
        System.out.println("=== 过滤后（主界面显示） ===");
        System.out.println(clean);
        boolean pass = clean.contains("这段是真正要给用户看的核心内容")
                && clean.contains("第二行核心内容")
                && !clean.contains("audio_path")
                && !clean.contains(".wav")
                && !clean.contains(".srt")
                && !clean.contains(".json")
                && !clean.contains("音频时长")
                && !clean.contains("识别到的指令")
                && !clean.contains("回复内容");
        System.out.println("PASS=" + pass);
        if (!pass) System.exit(1);
    }
}
