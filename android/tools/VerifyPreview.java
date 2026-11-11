import com.boray.voicemcp.LipSync;

/** 纯 JVM 回归：档案页「试听」用的服务端真实 WAV 能否逐帧驱动口型 */
public class VerifyPreview {
    private static int total, pass;

    private static void ok(String name, boolean cond) {
        total++;
        if (cond) pass++;
        System.out.println((cond ? "PASS " : "FAIL ") + name);
    }

    public static void main(String[] args) {
        LipSync s = LipSync.fromWav(args[0]);
        ok("真实 WAV 可解析", s != null && s.fromAudio);
        if (s == null) { finish(); return; }

        int frames = s.levels.length;
        ok("包络帧数 > 50 (" + frames + ")", frames > 50);
        ok("时长 > 1 秒 (" + s.durationMs + "ms)", s.durationMs > 1000);

        int voiced = 0;
        int[] shapes = new int[5];
        for (int i = 0; i < frames; i++) {
            int t = i * LipSync.FRAME_MS;
            if (s.levelAt(t) > 0.08f) voiced++;
            int sh = s.shapeAt(t);
            if (sh >= 0 && sh < 5) shapes[sh]++;
        }
        int voicedPct = frames == 0 ? 0 : voiced * 100 / frames;
        ok("有效发声帧 > 40% (" + voicedPct + "%)", voicedPct > 40);
        int kinds = 0;
        for (int n : shapes) if (n > 0) kinds++;
        ok("口型形状 >= 4 种 (" + kinds + ")", kinds >= 4);
        ok("存在闭嘴帧", shapes[LipSync.SHAPE_CLOSED] > 0);
        ok("包络数组与口型数组等长", s.levels.length == s.shapes.length);

        // 与「文本音节估算」对比：两者必须是不同来源，试听走的是真实音频
        LipSync fake = LipSync.synthetic("您好，我是陆清安，公司CEO。接下来由我为您汇报本次语音指令的执行结果。");
        ok("试听使用真实音频而非文本估算", s.fromAudio && !fake.fromAudio);
        finish();
    }

    private static void finish() {
        System.out.println();
        System.out.println("TOTAL " + total + ", PASS " + pass + ", FAIL " + (total - pass));
        System.out.println("PASS_ALL=" + (total == pass));
        if (total != pass) System.exit(1);
    }
}
