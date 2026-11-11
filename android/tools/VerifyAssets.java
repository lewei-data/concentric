import com.boray.voicemcp.AssetUrls;

/** 纯 JVM 校验：服务端修复后的同源素材路径不会被 asset_url_base 二次叠加。 */
public class VerifyAssets {

    private static int pass = 0;
    private static int fail = 0;

    private static void eq(String label, String expect, String actual) {
        boolean ok = expect.equals(actual);
        if (ok) pass++;
        else fail++;
        System.out.println((ok ? "PASS " : "FAIL ") + label
                + (ok ? "" : "\n     expect = " + expect + "\n     actual = " + actual));
    }

    public static void main(String[] args) {
        final String mcp = "https://ai.boraydata.com/conring/asr/mcp";
        final String base = "/conring/asr/assets";
        final String origin = "https://ai.boraydata.com";

        // 1. 服务端当前形态：字段已含 asset_url_base，必须直接拼 origin
        eq("photo 同源绝对路径",
                origin + "/conring/asr/assets/avatars/ceo.png",
                AssetUrls.resolveSpec(mcp, base, "/conring/asr/assets/avatars/ceo.png"));
        eq("videoTalk 同源绝对路径",
                origin + "/conring/asr/assets/videos/ceo.talk.mp4",
                AssetUrls.resolveSpec(mcp, base, "/conring/asr/assets/videos/ceo.talk.mp4"));
        eq("face_url 同源绝对路径",
                origin + "/conring/asr/assets/faces/ceo.png",
                AssetUrls.resolveSpec(mcp, base, "/conring/asr/assets/faces/ceo.png"));

        // 2. 旧式 /avatars/... 相对路径：不叠加 base（避免 /conring/asr/assets/avatars/...）
        eq("旧式绝对路径不叠加 base",
                origin + "/avatars/ceo.png",
                AssetUrls.resolveSpec(mcp, base, "/avatars/ceo.png"));

        // 3. 裸文件名：才使用 asset_url_base
        eq("裸文件名 + 相对 base",
                origin + "/conring/asr/assets/avatars/ceo.png",
                AssetUrls.resolveSpec(mcp, base, "avatars/ceo.png"));
        eq("裸文件名 + 空 base",
                origin + "/avatars/ceo.png",
                AssetUrls.resolveSpec(mcp, "", "avatars/ceo.png"));
        eq("裸文件名 + 绝对 CDN base",
                "https://cdn.example.com/assets/avatars/ceo.png",
                AssetUrls.resolveSpec(mcp, "https://cdn.example.com/assets", "avatars/ceo.png"));

        // 4. 绝对地址 / 内置资源 / 空值原样返回
        eq("绝对 URL 原样", "https://cdn.example.com/x.png",
                AssetUrls.resolveSpec(mcp, base, "https://cdn.example.com/x.png"));
        eq("内置矢量原样", "drawable://photo_lin",
                AssetUrls.resolveSpec(mcp, base, "drawable://photo_lin"));
        eq("本机文件原样", "file:///sdcard/a.mp4",
                AssetUrls.resolveSpec(mcp, base, "file:///sdcard/a.mp4"));
        eq("空 spec", "", AssetUrls.resolveSpec(mcp, base, ""));
        eq("null spec", "", AssetUrls.resolveSpec(mcp, base, null));
        eq("base 为字符串 null", origin + "/avatars/ceo.png",
                AssetUrls.resolveSpec(mcp, "null", "/avatars/ceo.png"));

        // 5. 关键回归：任何结果都不得出现双重前缀，也不得退化成相对路径
        String[] specs = {
                "/conring/asr/assets/avatars/boss.png",
                "/conring/asr/assets/videos/pm_wenren.talk.mp4",
                "/conring/asr/assets/faces/manual_scout.png",
        };
        for (String s : specs) {
            String got = AssetUrls.resolveSpec(mcp, base, s);
            eq("无双重前缀 " + s.substring(s.lastIndexOf('/') + 1),
                    origin + s, got);
        }

        System.out.println("\nTOTAL " + (pass + fail) + ", PASS " + pass + ", FAIL " + fail);
        System.out.println("PASS_ALL=" + (fail == 0));
        if (fail > 0) System.exit(1);
    }
}
