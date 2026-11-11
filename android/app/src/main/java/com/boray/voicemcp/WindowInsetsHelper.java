package com.boray.voicemcp;

import android.app.Activity;
import android.content.res.Configuration;
import android.graphics.Insets;
import android.os.Build;
import android.view.DisplayCutout;
import android.view.View;
import android.view.Window;
import android.view.WindowInsets;
import android.view.WindowInsetsController;

/**
 * 系统栏安全区适配（纯 Android framework，零 AndroidX 依赖）。
 *
 * 背景：targetSdk 35 在 Android 15+ 上会被系统强制 edge-to-edge，状态栏 / 导航栏
 * 变成透明，应用内容直接绘制到系统栏下方，于是顶部标题被状态栏压住、底部按钮被
 * 手势导航条盖住。这里给内容根视图（android.R.id.content）统一补上
 * 「系统栏 + 刘海」的安全区 padding，并把系统栏背景色 / 图标明暗与当前主题对齐。
 *
 * 兼容策略：
 *  - API 30+：WindowInsets.getInsetsIgnoringVisibility(systemBars | displayCutout)，
 *    取「忽略可见性」的稳定值，键盘弹出或系统栏临时隐藏时不会让页面跳动。
 *  - API 26~29：systemWindowInsets / stableInsets / DisplayCutout 三者取较大值。
 *  - 低版本系统本来就会自己避让，分发到内容根的 insets 为 0，因此不会重复叠加 padding。
 *
 * padding 加在内容根视图而不是各布局上，窗口背景色会自然露出来填满系统栏区域，
 * 视觉上仍是「顶部留白 + 底部留白」，不会出现内容被遮挡或色块错位。
 */
public final class WindowInsetsHelper {

    private WindowInsetsHelper() {
    }

    /** 在 setContentView() 之后调用一次即可。 */
    public static void apply(Activity activity) {
        if (activity == null) return;
        Window window = activity.getWindow();
        if (window == null) return;
        View content = window.getDecorView().findViewById(android.R.id.content);
        if (content == null) return;

        boolean lightTheme = isLightTheme(activity);
        int barColor = activity.getResources().getColor(R.color.window, activity.getTheme());
        window.setStatusBarColor(barColor);
        window.setNavigationBarColor(barColor);
        if (Build.VERSION.SDK_INT >= 29) {
            // 背景色由我们自己保证对比度，不需要系统再叠一层灰色遮罩
            window.setNavigationBarContrastEnforced(false);
        }
        applyBarIconAppearance(window, lightTheme);

        final int baseLeft = content.getPaddingLeft();
        final int baseTop = content.getPaddingTop();
        final int baseRight = content.getPaddingRight();
        final int baseBottom = content.getPaddingBottom();

        content.setOnApplyWindowInsetsListener((v, insets) -> {
            int[] safe = safeInsets(insets);
            v.setPadding(baseLeft + safe[0], baseTop + safe[1],
                    baseRight + safe[2], baseBottom + safe[3]);
            // 不消费 insets，子 View（如 EditText 的 IME 处理）仍可收到
            return insets;
        });
        content.requestApplyInsets();
    }

    private static int[] safeInsets(WindowInsets insets) {
        int l = 0, t = 0, r = 0, b = 0;
        if (Build.VERSION.SDK_INT >= 30) {
            Insets bars = insets.getInsetsIgnoringVisibility(
                    WindowInsets.Type.systemBars() | WindowInsets.Type.displayCutout());
            l = bars.left;
            t = bars.top;
            r = bars.right;
            b = bars.bottom;
        } else {
            // stableInsets 不受键盘影响；顶部再与 systemWindowInsets 取最大值以覆盖刘海
            l = insets.getStableInsetLeft();
            t = Math.max(insets.getStableInsetTop(), insets.getSystemWindowInsetTop());
            r = insets.getStableInsetRight();
            b = insets.getStableInsetBottom();
            if (Build.VERSION.SDK_INT >= 28 && insets.getDisplayCutout() != null) {
                DisplayCutout cutout = insets.getDisplayCutout();
                l = Math.max(l, cutout.getSafeInsetLeft());
                t = Math.max(t, cutout.getSafeInsetTop());
                r = Math.max(r, cutout.getSafeInsetRight());
                b = Math.max(b, cutout.getSafeInsetBottom());
            }
        }
        return new int[]{l, t, r, b};
    }

    /** 浅色主题用深色系统栏图标，深色主题用浅色系统栏图标，保证状态栏文字可读。 */
    private static void applyBarIconAppearance(Window window, boolean lightTheme) {
        if (Build.VERSION.SDK_INT >= 30) {
            WindowInsetsController controller = window.getInsetsController();
            if (controller == null) return;
            int mask = WindowInsetsController.APPEARANCE_LIGHT_STATUS_BARS
                    | WindowInsetsController.APPEARANCE_LIGHT_NAVIGATION_BARS;
            controller.setSystemBarsAppearance(lightTheme ? mask : 0, mask);
        } else {
            View decor = window.getDecorView();
            int mask = View.SYSTEM_UI_FLAG_LIGHT_STATUS_BAR
                    | View.SYSTEM_UI_FLAG_LIGHT_NAVIGATION_BAR;
            int flags = decor.getSystemUiVisibility();
            decor.setSystemUiVisibility(lightTheme ? (flags | mask) : (flags & ~mask));
        }
    }

    private static boolean isLightTheme(Activity activity) {
        int night = activity.getResources().getConfiguration().uiMode
                & Configuration.UI_MODE_NIGHT_MASK;
        return night != Configuration.UI_MODE_NIGHT_YES;
    }
}
