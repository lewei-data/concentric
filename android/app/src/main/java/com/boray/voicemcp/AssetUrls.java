package com.boray.voicemcp;

import android.content.Context;
import android.content.SharedPreferences;

import java.net.URL;

/**
 * 服务端素材地址解析（server v2.1.0+ / 2026-11-10 素材路径修复版）。
 *
 * 约束（用户明确要求）：只使用 MCP 服务自己返回的地址，不接任何其它 URL 的资源，
 * 不探测其它主机 / 端口 / CDN；素材为同源公开静态资源，无需 VPN、无需 Token。
 *
 * 服务端现状（2026-11-10 实测）：
 *   profile.asset_url_base = "/conring/asr/assets"（相对路径前缀）
 *   photo / videoTalk / videoIdle / assets.face_url / voice_avatars.*.url
 *     已是「含该前缀的同源绝对路径」，例如 /conring/asr/assets/avatars/ceo.png
 *   18 人 × 4 类素材在 MCP 同源 origin 下全部 200，视频 Range 返回 206。
 *
 * 解析规则：
 *   - 绝对 URL / drawable:// / assets:// / file:// → 原样返回
 *   - 以 "/" 开头的同源绝对路径 → MCP 端点 origin + 该路径。
 *     服务端已把 asset_url_base 写进字段，绝不能再次叠加，否则会拼出双重前缀
 *     且结果退化成相对路径（HttpURLConnection 无法打开）。
 *   - 裸相对文件名 → 用 asset_url_base（绝对或相对都先按 origin 规范化）补齐
 *   - 未配置 base → MCP origin + "/" + 文件名
 *
 * origin 由 McpConfig.url（MCP 端点本身）派生，不新增主机、不新增端口。
 */
public final class AssetUrls {

    private static final String PREF = "conring_assets";
    private static final String KEY_SERVER_BASE = "server_asset_url_base";

    private AssetUrls() {
    }

    /** 保存 profile 返回的 asset_url_base；null / "null" 都按「服务端未给前缀」处理 */
    public static void setServerBase(Context c, String base) {
        String v = base == null ? "" : base.trim();
        if ("null".equalsIgnoreCase(v)) v = "";
        c.getSharedPreferences(PREF, Context.MODE_PRIVATE).edit()
                .putString(KEY_SERVER_BASE, v).apply();
    }

    public static String serverBase(Context c) {
        return c.getSharedPreferences(PREF, Context.MODE_PRIVATE)
                .getString(KEY_SERVER_BASE, "");
    }

    public static String resolve(Context c, String spec) {
        return resolveSpec(McpConfig.load(c).url, serverBase(c), spec);
    }

    /**
     * 纯逻辑解析，便于离线契约校验（不依赖 Android Context）。
     *
     * @param mcpUrl     MCP 端点，例如 https://ai.boraydata.com/conring/asr/mcp
     * @param serverBase profile.asset_url_base，可能为空、相对路径或绝对 CDN 地址
     * @param spec       服务端返回的素材写法
     */
    public static String resolveSpec(String mcpUrl, String serverBase, String spec) {
        if (spec == null || spec.trim().isEmpty()) return "";
        String s = spec.trim();
        if (s.startsWith("http://") || s.startsWith("https://")
                || s.startsWith("drawable://") || s.startsWith("assets://")
                || s.startsWith("file://")) {
            return s;
        }
        String origin = origin(mcpUrl);
        if (origin.isEmpty()) return s;      // 无法派生同源地址时原样返回，由调用方回退内置形象
        if (s.startsWith("/")) {
            // 同源绝对路径：服务端已含 asset_url_base，直接拼 origin，不叠加任何前缀
            return join(origin, s);
        }
        String baseAbs = absoluteBase(serverBase, origin);
        return join(baseAbs.isEmpty() ? origin : baseAbs, s);
    }

    /** 从 MCP 端点派生同源地址，不新增主机、不新增端口 */
    private static String origin(String mcpUrl) {
        try {
            URL u = new URL(mcpUrl);
            int port = u.getPort();
            return u.getProtocol() + "://" + u.getHost() + (port > 0 ? ":" + port : "");
        } catch (Exception e) {
            return "";
        }
    }

    /** asset_url_base 可能是 /conring/asr/assets 之类的相对路径，先补成绝对前缀 */
    private static String absoluteBase(String serverBase, String origin) {
        if (serverBase == null || serverBase.trim().isEmpty()) return "";
        String b = serverBase.trim();
        if ("null".equalsIgnoreCase(b)) return "";
        if (b.startsWith("http://") || b.startsWith("https://")) return b;
        return join(origin, b);
    }

    private static String join(String base, String path) {
        String b = base.endsWith("/") ? base.substring(0, base.length() - 1) : base;
        return path.startsWith("/") ? b + path : b + "/" + path;
    }
}
