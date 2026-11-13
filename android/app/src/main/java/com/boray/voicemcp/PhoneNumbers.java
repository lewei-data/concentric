package com.boray.voicemcp;

import android.Manifest;
import android.content.Context;
import android.content.pm.PackageManager;
import android.os.Build;
import android.telephony.SubscriptionInfo;
import android.telephony.SubscriptionManager;
import android.telephony.TelephonyManager;

import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/**
 * 读取本机号码（双卡会返回多个），供启动时「使用本机号码登录」提示。
 *
 * <p>Android 13（API 33）起读号码要 {@code READ_PHONE_NUMBERS}，
 * 更早版本用 {@code READ_PHONE_STATE}。运营商 SIM 未必写入本机号码，
 * 读不到时登录页会退回手动输入（并提示上次登录过的号码）。
 */
public final class PhoneNumbers {

    public static final int REQ_PERMISSION = 1201;

    public static final class Candidate {
        public final String label;
        public final String number;

        Candidate(String label, String number) {
            this.label = label;
            this.number = number;
        }

        /** 对话框里只显示掩码，不整号外泄 */
        public String display() {
            return label + "：" + mask(number);
        }
    }

    private PhoneNumbers() {
    }

    public static String[] permissions() {
        return Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU
                ? new String[]{Manifest.permission.READ_PHONE_NUMBERS}
                : new String[]{Manifest.permission.READ_PHONE_STATE};
    }

    public static boolean hasPermission(Context c) {
        for (String p : permissions()) {
            if (c.checkSelfPermission(p) != PackageManager.PERMISSION_GRANTED) return false;
        }
        return true;
    }

    /** 按 SIM 卡槽顺序去重，返回可用于登录的 11 位手机号 */
    public static List<Candidate> read(Context c) {
        Map<String, Candidate> found = new LinkedHashMap<>();
        if (!hasPermission(c)) return new ArrayList<>(found.values());
        try {
            SubscriptionManager sm = (SubscriptionManager)
                    c.getSystemService(Context.TELEPHONY_SUBSCRIPTION_SERVICE);
            if (sm != null) {
                List<SubscriptionInfo> infos = sm.getActiveSubscriptionInfoList();
                if (infos != null) {
                    for (SubscriptionInfo info : infos) {
                        String raw = numberOf(sm, info);
                        String n = normalize(raw);
                        if (n.isEmpty()) continue;
                        int slot = info.getSimSlotIndex();
                        String carrier = info.getCarrierName() == null
                                ? "" : info.getCarrierName().toString().trim();
                        String label = "卡 " + (slot >= 0 ? slot + 1 : 1)
                                + (carrier.isEmpty() ? "" : "（" + carrier + "）");
                        if (!found.containsKey(n)) found.put(n, new Candidate(label, n));
                    }
                }
            }
        } catch (Exception ignored) {
            // 无 SIM / 权限被系统拒绝 / 定制 ROM 抛异常：走下面的兜底
        }
        try {
            TelephonyManager tm = (TelephonyManager) c.getSystemService(Context.TELEPHONY_SERVICE);
            if (tm != null) {
                String n = normalize(tm.getLine1Number());
                if (!n.isEmpty() && !found.containsKey(n)) {
                    found.put(n, new Candidate("本机号码", n));
                }
            }
        } catch (Exception ignored) {
        }
        return new ArrayList<>(found.values());
    }

    private static String numberOf(SubscriptionManager sm, SubscriptionInfo info) {
        try {
            // API 33+ 官方替代：SubscriptionManager.getPhoneNumber(subId)，需 READ_PHONE_NUMBERS
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU && sm != null) {
                String n = sm.getPhoneNumber(info.getSubscriptionId());
                if (n != null && !n.isEmpty()) return n;
            }
            //noinspection deprecation
            String legacy = info.getNumber();
            return legacy == null ? "" : legacy;
        } catch (Exception e) {
            return "";
        }
    }

    /** 去空格/横线/区号，只接受大陆 11 位手机号 */
    public static String normalize(String raw) {
        if (raw == null) return "";
        String s = raw.replaceAll("[^0-9+]", "");
        if (s.startsWith("+86")) s = s.substring(3);
        else if (s.startsWith("0086")) s = s.substring(4);
        else if (s.length() == 13 && s.startsWith("86")) s = s.substring(2);
        return s.matches("1[3-9]\\d{9}") ? s : "";
    }

    public static String mask(String n) {
        String s = normalize(n);
        if (s.isEmpty()) return "未知号码";
        return s.substring(0, 3) + "****" + s.substring(7);
    }
}
