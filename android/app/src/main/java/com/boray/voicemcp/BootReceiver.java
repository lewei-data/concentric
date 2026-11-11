package com.boray.voicemcp;

import android.content.BroadcastReceiver;
import android.content.Context;
import android.content.Intent;

/** 开机 / 应用更新后，若用户开启了「保持监听」，自动恢复后台监听 */
public class BootReceiver extends BroadcastReceiver {
    @Override
    public void onReceive(Context context, Intent intent) {
        if (intent == null || intent.getAction() == null) return;
        String a = intent.getAction();
        if (!Intent.ACTION_BOOT_COMPLETED.equals(a)
                && !Intent.ACTION_MY_PACKAGE_REPLACED.equals(a)) return;
        McpConfig cfg = McpConfig.load(context);
        if (cfg.keepListening && !cfg.demo) ListenService.start(context);
    }
}
