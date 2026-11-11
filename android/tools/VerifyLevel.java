import com.boray.voicemcp.LiveLevelMeter;

/**
 * 员工侧电平换算的离线校验，直接用服务端实测会话 lv_742062b5 的数据
 * （见 docs/v26_level_meter.md 12.9.2）。
 */
public class VerifyLevel {

    private static int pass = 0;
    private static int fail = 0;

    private static void check(String name, boolean ok) {
        check(name, ok, "");
    }

    private static void check(String name, boolean ok, String detail) {
        if (ok) {
            pass++;
            System.out.println("  PASS  " + name);
        } else {
            fail++;
            System.out.println("  FAIL  " + name + "  " + detail);
        }
    }

    /** 实测播放列表：MEDIA-SEQUENCE 46，4 片 0.998456 + 末片截断 0.883489 */
    private static final String PLAYLIST = "#EXTM3U\n"
            + "#EXT-X-VERSION:6\n"
            + "#EXT-X-TARGETDURATION:1\n"
            + "#EXT-X-MEDIA-SEQUENCE:46\n"
            + "#EXTINF:0.998456,\nseg_00046.ts\n"
            + "#EXTINF:0.998456,\nseg_00047.ts\n"
            + "#EXTINF:0.998456,\nseg_00048.ts\n"
            + "#EXTINF:0.998456,\nseg_00049.ts\n"
            + "#EXTINF:0.883489,\nseg_00050.ts\n";

    public static void main(String[] args) {
        System.out.println("== 直播电平换算 ==");

        // ---------------- 播放列表锚点
        float anchor = LiveLevelMeter.playlistStartSec(PLAYLIST, 1f);
        check("首片起始 = 46 × 0.998456 ≈ 45.929",
                Math.abs(anchor - 45.929f) < 0.01f, "anchor=" + anchor);
        check("不会用 TARGETDURATION（46.0）",
                Math.abs(anchor - 46.0f) > 0.05f, "anchor=" + anchor);
        // 窗口均值 0.9755 × 46 = 44.87：若误用均值会差 1 秒以上
        check("不会用窗口均值（44.87）",
                Math.abs(anchor - 44.871f) > 1.0f, "anchor=" + anchor);
        check("末片截断不影响锚点（取最大 EXTINF）",
                Math.abs(anchor - 46 * 0.883489f) > 1.0f, "anchor=" + anchor);

        // ---------------- 取格（媒体时间 → 0~1）
        // 真实快照里连续语音段的相邻格几乎一样，这里用两格平台模拟，
        // 这样 45.929s（x=459.29）插值后仍应等于该段电平
        float[] lv = new float[508];
        lv[459] = 0.532f; lv[460] = 0.532f;   // 起播瞬间（mediaT ≈45.9s）
        lv[469] = 0.829f; lv[470] = 0.829f;   // 1 秒后
        lv[479] = 0f;     lv[480] = 0f;       // 2 秒后进静音段

        check("mediaT=45.929 → 53.2%",
                Math.abs(LiveLevelMeter.sample(lv, 0f, 10f, 50.8f, 45.929f) - 0.532f) < 0.01f,
                "" + LiveLevelMeter.sample(lv, 0f, 10f, 50.8f, 45.929f));
        check("mediaT=46.927 → 82.9%",
                Math.abs(LiveLevelMeter.sample(lv, 0f, 10f, 50.8f, 46.927f) - 0.829f) < 0.02f,
                "" + LiveLevelMeter.sample(lv, 0f, 10f, 50.8f, 46.927f));
        check("mediaT=47.929 → 0",
                LiveLevelMeter.sample(lv, 0f, 10f, 50.8f, 47.929f) < 0.01f,
                "" + LiveLevelMeter.sample(lv, 0f, 10f, 50.8f, 47.929f));
        check("mediaT 超过 edge(50.8) 仍取尾格",
                LiveLevelMeter.sample(lv, 0f, 10f, 50.8f, 60f) < 0.01f,
                "" + LiveLevelMeter.sample(lv, 0f, 10f, 50.8f, 60f));

        // ---------------- 插值与边界
        float[] two = new float[]{0.2f, 0.8f};
        check("两格中点线性插值 = 0.5",
                Math.abs(LiveLevelMeter.sample(two, 0f, 10f, 100f, 0.05f) - 0.5f) < 0.02f,
                "" + LiveLevelMeter.sample(two, 0f, 10f, 100f, 0.05f));
        check("早于 t0 取首格",
                Math.abs(LiveLevelMeter.sample(two, 10f, 10f, 100f, 5f) - 0.2f) < 1e-6);
        check("空数组返回 0", LiveLevelMeter.sample(new float[0], 0, 10, 0, 1f) == 0f);
        check("null 安全", LiveLevelMeter.sample(null, 0, 10, 0, 1f) == 0f);

        // ---------------- 同源校验
        check("同源地址放行", LiveLevelMeter.sameOrigin(
                "https://ai.boraydata.com/conring/asr/assets/live/lv_1/level.json",
                "https://ai.boraydata.com/conring/asr/assets/live/lv_1/index.m3u8"));
        check("异域名拒绝", !LiveLevelMeter.sameOrigin(
                "https://evil.example.com/level.json",
                "https://ai.boraydata.com/conring/asr/assets/live/lv_1/index.m3u8"));
        check("异端口拒绝", !LiveLevelMeter.sameOrigin(
                "https://ai.boraydata.com:8443/level.json",
                "https://ai.boraydata.com/conring/asr/assets/live/lv_1/index.m3u8"));
        check("空地址拒绝", !LiveLevelMeter.sameOrigin("", PLAYLIST));

        System.out.println("\nVERIFY LEVEL: " + pass + " PASS / " + fail + " FAIL");
        if (fail > 0) System.exit(1);
    }
}
