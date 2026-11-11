package com.boray.voicemcp;

import android.app.Activity;
import android.view.WindowManager;

/**
 * 通话期间别让屏幕自动熄灭。
 *
 * 用窗口的 FLAG_KEEP_SCREEN_ON，而不是 WakeLock：它只在页面可见时生效，
 * 不申请权限、不需要释放配对，用户切走/退出后系统自动恢复正常息屏策略，
 * 不会出现「忘了释放电源锁」那种耗电坑。
 */
public final class ScreenOn {

    private ScreenOn() {
    }

    public static void keep(Activity activity, boolean on) {
        if (activity == null) return;
        if (on) {
            if (activity.isFinishing() || activity.isDestroyed()) return;
            activity.getWindow().addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON);
        } else {
            // 即使正在销毁也要清一次，避免把标志带到下一个复用的窗口
            try {
                activity.getWindow().clearFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON);
            } catch (Exception ignored) {
            }
        }
    }
}
