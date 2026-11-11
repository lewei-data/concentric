package com.boray.voicemcp;

import android.content.Context;
import android.graphics.Canvas;
import android.graphics.Paint;
import android.graphics.RectF;
import android.util.AttributeSet;
import android.view.View;

/**
 * 通话中的实时音量条。
 *
 * 一排竖条，高度 = 当前音量 × 一条固定的「频谱形状」，再用插值做轻微的涨落，
 * 看起来像在跟着说话的节奏跳，而不是整排一起等比例变化。
 * 纯 Canvas 绘制、零第三方依赖；重绘由外层 tick（100ms）驱动，不做自循环动画，省电。
 */
public class VoiceLevelView extends View {

    private static final int BARS = 23;

    private final Paint paint = new Paint(Paint.ANTI_ALIAS_FLAG);
    private final RectF rect = new RectF();
    /** 每一根柱子的固定形状系数（0.35~1），避免整排一样高 */
    private final float[] shape = new float[BARS];
    /** 平滑后的显示值，做涨落用 */
    private final float[] shown = new float[BARS];

    private volatile float level = 0f;
    private boolean active = false;

    public VoiceLevelView(Context context) {
        super(context);
        init();
    }

    public VoiceLevelView(Context context, AttributeSet attrs) {
        super(context, attrs);
        init();
    }

    public VoiceLevelView(Context context, AttributeSet attrs, int defStyleAttr) {
        super(context, attrs, defStyleAttr);
        init();
    }

    private void init() {
        paint.setStyle(Paint.Style.FILL);
        paint.setColor(0xFF2E9E8F);
        for (int i = 0; i < BARS; i++) {
            // 两个不同周期的正弦叠出「频谱」的错落感
            float a = Math.abs((float) Math.sin(i * 1.13f + 0.4f));
            float b = Math.abs((float) Math.sin(i * 0.47f + 1.9f));
            shape[i] = 0.30f + 0.70f * (0.6f * a + 0.4f * b);
        }
    }

    public void setBarColor(int color) {
        paint.setColor(color);
        invalidate();
    }

    /** 音量 0~1（一般传 micLevel） */
    public void setLevel(float value) {
        level = Math.max(0f, Math.min(1f, value));
        invalidate();
    }

    public void setActive(boolean on) {
        active = on;
        if (!on) {
            level = 0f;
            for (int i = 0; i < BARS; i++) shown[i] = 0f;
        }
        invalidate();
    }

    @Override
    protected void onDraw(Canvas canvas) {
        int w = getWidth();
        int h = getHeight();
        if (w <= 0 || h <= 0) return;

        float gap = Math.max(2f, w * 0.010f);
        float bw = (w - gap * (BARS - 1)) / BARS;
        if (bw <= 0) return;
        float minH = Math.max(3f, h * 0.10f);
        float base = active ? level : 0f;

        for (int i = 0; i < BARS; i++) {
            float want = minH + (h - minH) * Math.min(1f, base * shape[i] * 1.9f);
            // 平滑：上升快、回落慢，观感更接近真实音量表
            float k = want > shown[i] ? 0.65f : 0.22f;
            shown[i] += (want - shown[i]) * k;
            float bh = Math.max(minH, shown[i]);
            float left = i * (bw + gap);
            float top = (h - bh) / 2f;
            rect.set(left, top, left + bw, top + bh);
            canvas.drawRoundRect(rect, bw / 2f, bw / 2f, paint);
        }
    }
}
