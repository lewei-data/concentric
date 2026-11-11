package com.boray.voicemcp;

import android.content.Context;
import android.graphics.Bitmap;
import android.graphics.Canvas;
import android.graphics.Color;
import android.graphics.LinearGradient;
import android.graphics.Paint;
import android.graphics.Path;
import android.graphics.RadialGradient;
import android.graphics.Rect;
import android.graphics.RectF;
import android.graphics.Shader;
import android.os.Handler;
import android.os.Looper;
import android.os.SystemClock;
import android.util.AttributeSet;
import android.view.View;

/**
 * 音频驱动的仿真数字员工（纯 Canvas 绘制，零素材依赖、零第三方库）。
 *
 * 分层渲染：背景光晕 / 身体与西装 / 手势 / 头部（发型·眉毛·眼睛·嘴）/ 声波 / 工牌。
 * 外部只需按播放进度喂 setFrame(音量, 口型, 表情, 是否在说话)，
 * View 自己以 ~30fps 维护呼吸、点头、摆头、眨眼等动作。
 *
 * 有真实照片/仿真人视频素材时，本 View 可作为角落里的「驱动小窗」继续提供
 * 真口型与表情；没有素材时它就是主画面，保证开箱即用。
 */
public class DigitalHumanView extends View {

    private static final int DESIGN_W = 400;
    private static final int DESIGN_H = 520;

    private final Paint paint = new Paint(Paint.ANTI_ALIAS_FLAG);
    private final Path path = new Path();
    private final RectF oval = new RectF();
    private final Handler handler = new Handler(Looper.getMainLooper());
    private final Runnable tick = new Runnable() {
        @Override public void run() {
            if (!attached || !animated) return;
            invalidate();
            handler.postDelayed(this, 33);
        }
    };

    private Employee emp;
    /** 服务端真人照片（优先面部特写）：设置后本 View 不再画内置卡通人，
     *  而是用同一人的照片做底片，只叠加随音频开合的嘴形，保证大小屏形象一致 */
    private Bitmap photo;
    private float mouthXRel = 0.50f;
    private float mouthYRel = 0.72f;
    private boolean attached;
    private boolean animated = true;
    private boolean staticPose;          // 列表头像：固定姿势、不动画
    private boolean speaking;
    private float level;
    private int mouthShape = LipSync.SHAPE_CLOSED;
    private String expression = "calm";

    public DigitalHumanView(Context c) { super(c); init(); }
    public DigitalHumanView(Context c, AttributeSet a) { super(c, a); init(); }
    public DigitalHumanView(Context c, AttributeSet a, int s) { super(c, a, s); init(); }

    private void init() {
        paint.setAntiAlias(true);
        paint.setStrokeCap(Paint.Cap.ROUND);
        paint.setStrokeJoin(Paint.Join.ROUND);
    }

    public void setEmployee(Employee e) { emp = e; invalidate(); }

    /** 用服务端真人照片替换内置卡通形象；口型仍由 setFrame 逐帧驱动 */
    public void setPhotoBitmap(Bitmap b) {
        photo = (b == null || b.isRecycled()) ? null : b;
        invalidate();
    }

    public void clearPhotoBitmap() {
        photo = null;
        invalidate();
    }

    /** 面部特写里嘴部的相对位置，默认正中偏下 (0.50, 0.72) */
    public void setMouthAnchor(float xRel, float yRel) {
        mouthXRel = Math.max(0.2f, Math.min(0.8f, xRel));
        mouthYRel = Math.max(0.45f, Math.min(0.92f, yRel));
        invalidate();
    }

    /** 列表缩略头像用：静态、闭嘴、不启动动画循环 */
    public void setStaticPose(boolean on) {
        staticPose = on;
        setAnimated(!on);
        invalidate();
    }

    public void setAnimated(boolean on) {
        animated = on;
        handler.removeCallbacks(tick);
        if (on && attached) handler.post(tick);
        invalidate();
    }

    /** 由 ReportActivity 按音频进度逐帧调用 */
    public void setFrame(float volume, int shape, String mood, boolean isSpeaking) {
        level = Math.max(0f, Math.min(1f, volume));
        mouthShape = shape;
        expression = mood == null ? "calm" : mood;
        speaking = isSpeaking;
        invalidate();
    }

    @Override protected void onAttachedToWindow() {
        super.onAttachedToWindow();
        attached = true;
        if (animated) { handler.removeCallbacks(tick); handler.post(tick); }
    }

    @Override protected void onDetachedFromWindow() {
        attached = false;
        handler.removeCallbacks(tick);
        super.onDetachedFromWindow();
    }

    // ------------------------------------------------------------------ 绘制

    @Override protected void onDraw(Canvas c) {
        int w = getWidth(), h = getHeight();
        if (w <= 0 || h <= 0) return;
        long t = staticPose ? 0L : SystemClock.uptimeMillis();
        float scale = Math.min(w / (float) DESIGN_W, h / (float) DESIGN_H);
        c.save();
        c.translate((w - DESIGN_W * scale) / 2f, (h - DESIGN_H * scale) / 2f);
        c.scale(scale, scale);

        // 有服务端真人素材时：小窗也用同一个人的照片，只叠加音频驱动的口型，
        // 不再画一张「另一个人」的卡通脸，保证大屏 / 小窗 / 档案照形象一致。
        if (photo != null && !photo.isRecycled()) {
            drawPhotoStage(c, emp == null ? Color.parseColor("#006C63") : emp.accent,
                    mood(expression), t);
            c.restore();
            return;
        }

        Employee e = emp;
        int accent = e == null ? Color.parseColor("#006C63") : e.accent;
        int skin = e == null ? Color.parseColor("#F3C9A6") : e.skin;
        int hair = e == null ? Color.parseColor("#2A2320") : e.hair;
        int suit = e == null ? Color.parseColor("#20514B") : e.suit;
        int shirt = e == null ? Color.parseColor("#F4F8F6") : e.shirt;
        String style = e == null ? "short" : e.hairStyle;
        boolean glasses = e != null && e.glasses;

        Mood m = mood(expression);
        float breath = staticPose ? 0f : (float) Math.sin(t / 1100.0) * 2.6f;
        float nod = (!staticPose && speaking) ? (float) Math.sin(t / 620.0) * level * 3.2f : 0f;
        float sway = staticPose ? 0f
                : (float) Math.sin(t / 1700.0) * 1.8f
                + (speaking ? (float) Math.sin(t / 430.0) * level * 2.4f : 0f);
        float handLift = (!staticPose && speaking) ? level * 30f : 0f;
        float blink = staticPose ? 0f : blinkFactor(t);

        drawBackdrop(c, accent);
        drawNeck(c, skin);
        drawBody(c, suit, shirt, accent);
        drawHands(c, skin, handLift, t);

        c.save();
        c.translate(DESIGN_W / 2f, 210f);
        c.rotate(sway * 0.55f);
        c.translate(-DESIGN_W / 2f, -210f + breath + nod);
        drawHead(c, skin, hair, accent, style, glasses, m, blink, t);
        c.restore();

        drawBadge(c, accent, e == null ? "OPC" : e.no);
        if (!staticPose && speaking && level > 0.05f) drawSoundWaves(c, accent, level, t);

        c.restore();
    }

    /** 照片模式：服务端真人底片 + 同一张脸上的实时口型 + 音量条 + 情绪描边 */
    private void drawPhotoStage(Canvas c, int accent, Mood m, long t) {
        float breath = staticPose ? 0f : (float) Math.sin(t / 1100.0) * 1.4f;
        float nod = (!staticPose && speaking) ? (float) Math.sin(t / 620.0) * level * 2.0f : 0f;
        c.save();
        c.translate(0, breath + nod);
        int bw = photo.getWidth(), bh = photo.getHeight();
        float s = Math.max(DESIGN_W / (float) bw, DESIGN_H / (float) bh);
        float srcW = DESIGN_W / s, srcH = DESIGN_H / s;
        float sx = Math.max(0f, (bw - srcW) / 2f);
        float sy = Math.max(0f, (bh - srcH) / 2f);
        Rect src = new Rect((int) sx, (int) sy,
                (int) Math.min(bw, sx + srcW), (int) Math.min(bh, sy + srcH));
        RectF dst = new RectF(0, 0, DESIGN_W, DESIGN_H);
        paint.setShader(null);
        paint.setColorFilter(null);
        paint.setXfermode(null);
        paint.setStyle(Paint.Style.FILL);
        paint.setColor(Color.WHITE);
        paint.setAlpha(255);
        c.drawBitmap(photo, src, dst, paint);
        c.restore();

        LinearGradient shade = new LinearGradient(0, DESIGN_H * 0.72f, 0, DESIGN_H,
                Color.TRANSPARENT, Color.argb(120, 10, 24, 22), Shader.TileMode.CLAMP);
        shader(shade);
        c.drawRect(0, DESIGN_H * 0.72f, DESIGN_W, DESIGN_H, paint);

        drawPhotoMouth(c, m);
        if (!staticPose && speaking && level > 0.04f) drawLevelBars(c, accent, level, t);
        if ("cheerful".equals(expression) || "happy".equals(expression)) {
            stroke(withAlpha(Color.parseColor("#FFB74D"), 120), 6f);
            c.drawRoundRect(3, 3, DESIGN_W - 3, DESIGN_H - 3, 18, 18, paint);
        } else if ("concerned".equals(expression) || "error".equals(expression)) {
            stroke(withAlpha(Color.parseColor("#5C7CFA"), 120), 6f);
            c.drawRoundRect(3, 3, DESIGN_W - 3, DESIGN_H - 3, 18, 18, paint);
        }
    }

    /** 只在真人照片的嘴部叠加动态嘴形；开合量来自 WAV 振幅包络 */
    private void drawPhotoMouth(Canvas c, Mood m) {
        float cx = DESIGN_W * mouthXRel;
        float cy = DESIGN_H * mouthYRel;
        float open = staticPose ? 0f : (speaking ? Math.min(1f, level * 1.3f) : 0f);
        if (open < 0.08f) {
            stroke(Color.argb(170, 150, 72, 74), 4f);
            path.reset();
            path.moveTo(cx - 26, cy);
            path.quadTo(cx, cy + 6f * m.smile, cx + 26, cy);
            c.drawPath(path, paint);
            return;
        }
        float mw = 36, mh = 20;
        switch (mouthShape) {
            case LipSync.SHAPE_A: mw = 42; mh = 30; break;
            case LipSync.SHAPE_E: mw = 50; mh = 14; break;
            case LipSync.SHAPE_O: mw = 26; mh = 28; break;
            case LipSync.SHAPE_U: mw = 22; mh = 22; break;
            default: mw = 36; mh = 20; break;
        }
        mw *= 0.6f + 0.4f * open;
        mh = 3f + mh * open;
        oval.set(cx - mw / 2, cy - mh / 2, cx + mw / 2, cy + mh / 2);
        fill(Color.argb(215, 96, 40, 46));
        c.drawOval(oval, paint);
        fill(Color.argb(225, 250, 248, 244));
        c.drawRect(cx - mw / 2 + 2, cy - mh / 2 + 1,
                cx + mw / 2 - 2, cy - mh / 2 + Math.min(6f, mh * 0.30f), paint);
    }

    private void drawLevelBars(Canvas c, int accent, float vol, long t) {
        int bars = 7;
        float base = DESIGN_H - 20f;
        for (int i = 0; i < bars; i++) {
            float amp = (0.35f + 0.65f * Math.abs((float) Math.sin(t / 180.0 + i))) * vol;
            float h = 6 + amp * 26;
            float x = DESIGN_W / 2f + (i - bars / 2f) * 11f;
            stroke(withAlpha(accent, 160), 5f);
            c.drawLine(x, base - h / 2, x, base + h / 2, paint);
        }
    }

    private void drawBackdrop(Canvas c, int accent) {
        RadialGradient g = new RadialGradient(200, 208, 275,
                withAlpha(accent, 46), withAlpha(accent, 0), Shader.TileMode.CLAMP);
        shader(g);
        c.drawRect(0, 0, DESIGN_W, DESIGN_H, paint);
        fill(withAlpha(accent, 18));
        c.drawCircle(200, 208, 168, paint);
        fill(Color.argb(22, 0, 0, 0));
        oval.set(92, 492, 308, 522);
        c.drawOval(oval, paint);
    }

    private void drawNeck(Canvas c, int skin) {
        fill(skin);
        oval.set(174, 282, 226, 374);
        c.drawOval(oval, paint);
        fill(darken(skin, 0.90f));
        oval.set(174, 282, 226, 312);
        c.drawOval(oval, paint);
    }

    private void drawBody(Canvas c, int suit, int shirt, int accent) {
        path.reset();
        path.moveTo(26, DESIGN_H);
        path.cubicTo(32, 428, 96, 366, 160, 354);
        path.lineTo(240, 354);
        path.cubicTo(304, 366, 368, 428, 374, DESIGN_H);
        path.close();
        LinearGradient g = new LinearGradient(0, 350, 0, DESIGN_H,
                lighten(suit, 1.18f), darken(suit, 0.82f), Shader.TileMode.CLAMP);
        shader(g);
        c.drawPath(path, paint);

        // 衬衫 V 领
        path.reset();
        path.moveTo(164, 352);
        path.lineTo(200, 442);
        path.lineTo(236, 352);
        path.close();
        fill(shirt);
        c.drawPath(path, paint);

        // 领带 / 丝巾
        path.reset();
        path.moveTo(190, 360);
        path.lineTo(210, 360);
        path.lineTo(214, 388);
        path.lineTo(200, 470);
        path.lineTo(186, 388);
        path.close();
        fill(accent);
        c.drawPath(path, paint);

        // 西装翻领
        stroke(darken(suit, 0.82f), 5);
        path.reset();
        path.moveTo(164, 352); path.lineTo(128, 386); path.lineTo(174, 438);
        c.drawPath(path, paint);
        path.reset();
        path.moveTo(236, 352); path.lineTo(272, 386); path.lineTo(226, 438);
        c.drawPath(path, paint);
    }

    private void drawHands(Canvas c, int skin, float lift, long t) {
        float left = 126, right = 274;
        float swing = staticPose ? 0f : (float) Math.sin(t / 820.0) * 3f;
        drawHand(c, left, 452 - lift + swing, -20 - lift * 0.5f, skin);
        drawHand(c, right, 452 - lift * 0.65f - swing, 20 + lift * 0.5f, skin);
    }

    private void drawHand(Canvas c, float cx, float cy, float angle, int skin) {
        c.save();
        c.rotate(angle, cx, cy);
        fill(skin);
        oval.set(cx - 23, cy - 20, cx + 23, cy + 26);
        c.drawOval(oval, paint);
        stroke(darken(skin, 0.88f), 2.5f);
        for (int i = -1; i <= 1; i++) {
            path.reset();
            path.moveTo(cx + i * 11, cy - 8);
            path.lineTo(cx + i * 11, cy + 12);
            c.drawPath(path, paint);
        }
        c.restore();
    }

    private void drawHead(Canvas c, int skin, int hair, int accent,
                          String style, boolean glasses, Mood m, float blink, long t) {
        // 长发后层
        if ("long".equals(style)) {
            fill(hair);
            oval.set(92, 96, 146, 342); c.drawOval(oval, paint);
            oval.set(254, 96, 308, 342); c.drawOval(oval, paint);
        }

        // 耳朵
        fill(skin);
        oval.set(104, 190, 128, 234); c.drawOval(oval, paint);
        oval.set(272, 190, 296, 234); c.drawOval(oval, paint);
        fill(darken(skin, 0.88f));
        oval.set(112, 200, 122, 224); c.drawOval(oval, paint);
        oval.set(278, 200, 288, 224); c.drawOval(oval, paint);

        // 脸
        RadialGradient face = new RadialGradient(186, 176, 168,
                lighten(skin, 1.08f), darken(skin, 0.92f), Shader.TileMode.CLAMP);
        shader(face);
        oval.set(112, 96, 288, 318);
        c.drawOval(oval, paint);
        fill(Color.argb(16, 255, 255, 255));
        oval.set(130, 118, 200, 190);
        c.drawOval(oval, paint);

        // 腮红
        if (m.blush > 0.01f) {
            fill(Color.argb((int) (42 * m.blush), 226, 116, 116));
            oval.set(132, 232, 166, 254); c.drawOval(oval, paint);
            oval.set(234, 232, 268, 254); c.drawOval(oval, paint);
        }

        drawHair(c, hair, style);
        drawBrows(c, hair, m);
        drawEyes(c, m, blink);
        drawNose(c, skin);
        drawMouth(c, m);
        if (glasses) drawGlasses(c);
    }

    private void drawHair(Canvas c, int hair, String style) {
        LinearGradient g = new LinearGradient(0, 70, 0, 210,
                lighten(hair, 1.22f), darken(hair, 0.86f), Shader.TileMode.CLAMP);
        shader(g);
        if ("bun".equals(style)) {
            oval.set(174, 52, 226, 104);
            c.drawOval(oval, paint);
            fill(darken(hair, 0.86f));
            oval.set(180, 70, 220, 98);
            c.drawOval(oval, paint);
            shader(g);
        }
        if ("curly".equals(style)) {
            for (int[] o : new int[][]{{126, 92, 38}, {172, 74, 42}, {220, 76, 42}, {264, 96, 36}}) {
                oval.set(o[0], o[1], o[0] + o[2], o[1] + o[2]);
                c.drawOval(oval, paint);
            }
        }
        path.reset();
        path.moveTo(108, 198);
        path.cubicTo(98, 118, 140, 74, 200, 74);
        path.cubicTo(260, 74, 302, 118, 292, 198);
        path.cubicTo(282, 152, 258, 130, 230, 124);
        path.cubicTo(190, 114, 146, 132, 122, 160);
        path.cubicTo(116, 174, 112, 188, 108, 198);
        path.close();
        c.drawPath(path, paint);
        if ("long".equals(style)) {
            fill(hair);
            oval.set(104, 100, 136, 240); c.drawOval(oval, paint);
            oval.set(264, 100, 296, 240); c.drawOval(oval, paint);
        }
    }

    private void drawBrows(Canvas c, int hair, Mood m) {
        stroke(darken(hair, 1.05f), 7);
        float y = 174 - m.browLift;
        path.reset();
        path.moveTo(136, y + m.browAngle * 8);
        path.quadTo(162, y - 9 - m.browLift * 0.4f, 188, y - m.browAngle * 8);
        c.drawPath(path, paint);
        path.reset();
        path.moveTo(212, y - m.browAngle * 8);
        path.quadTo(238, y - 9 - m.browLift * 0.4f, 264, y + m.browAngle * 8);
        c.drawPath(path, paint);
    }

    private void drawEyes(Canvas c, Mood m, float blink) {
        float openness = Math.max(0.06f, (1f - blink * 0.94f) * m.eyeOpen);
        for (int cx : new int[]{162, 238}) {
            float halfH = 14f * openness;
            fill(Color.rgb(252, 252, 250));
            oval.set(cx - 23, 202 - halfH, cx + 23, 202 + halfH);
            c.drawOval(oval, paint);
            if (openness > 0.28f) {
                float r = 9f * openness;
                fill(Color.rgb(48, 62, 60));
                c.drawCircle(cx + m.gaze, 202, r, paint);
                fill(Color.rgb(22, 28, 27));
                c.drawCircle(cx + m.gaze, 202, r * 0.5f, paint);
                fill(Color.argb(220, 255, 255, 255));
                c.drawCircle(cx + m.gaze - 3f, 199, 2.8f, paint);
            }
            stroke(Color.argb(150, 42, 38, 34), 3f);
            path.reset();
            path.moveTo(cx - 23, 198);
            path.quadTo(cx, 186 - 4 * openness, cx + 23, 198);
            c.drawPath(path, paint);
        }
    }

    private void drawNose(Canvas c, int skin) {
        stroke(darken(skin, 0.84f), 3.5f);
        path.reset();
        path.moveTo(200, 210);
        path.quadTo(192, 240, 200, 244);
        path.quadTo(208, 246, 212, 240);
        c.drawPath(path, paint);
    }

    private void drawMouth(Canvas c, Mood m) {
        float cx = 200, cy = 276;
        float open = staticPose ? 0f : (speaking ? Math.min(1f, level * 1.28f) : 0f);
        int lip = Color.rgb(176, 96, 92);
        if (open < 0.09f) {
            stroke(lip, 5f);
            path.reset();
            path.moveTo(166, cy);
            path.quadTo(cx, cy + 8f * m.smile, 234, cy);
            c.drawPath(path, paint);
            if (m.smile > 0.2f) {
                fill(lip);
                c.drawCircle(166, cy - 1, 2.5f, paint);
                c.drawCircle(234, cy - 1, 2.5f, paint);
            }
            return;
        }
        float mw, mh;
        switch (mouthShape) {
            case LipSync.SHAPE_A: mw = 48; mh = 34; break;
            case LipSync.SHAPE_E: mw = 56; mh = 20; break;
            case LipSync.SHAPE_O: mw = 32; mh = 32; break;
            case LipSync.SHAPE_U: mw = 26; mh = 26; break;
            default: mw = 42; mh = 26; break;
        }
        mw = mw * (0.55f + 0.45f * open);
        mh = 5f + mh * open;
        oval.set(cx - mw / 2, cy - mh / 2, cx + mw / 2, cy + mh / 2);
        fill(Color.rgb(96, 42, 48));
        c.drawOval(oval, paint);
        // 上排牙齿
        fill(Color.rgb(250, 248, 244));
        c.drawRect(cx - mw / 2 + 3, cy - mh / 2 + 1,
                cx + mw / 2 - 3, cy - mh / 2 + Math.min(8f, mh * 0.30f), paint);
        // 舌头
        if (mh > 14) {
            fill(Color.rgb(198, 96, 104));
            oval.set(cx - mw * 0.30f, cy + mh * 0.10f,
                    cx + mw * 0.30f, cy + mh / 2 - 1);
            c.drawOval(oval, paint);
        }
        // 嘴角随表情上扬 / 下垂
        stroke(lip, 4f);
        float corner = m.smile * 5f;
        c.drawLine(cx - mw / 2 - 2, cy - corner, cx - mw / 2 + 2, cy - corner + 1, paint);
        c.drawLine(cx + mw / 2 + 2, cy - corner, cx + mw / 2 - 2, cy - corner + 1, paint);
    }

    private void drawGlasses(Canvas c) {
        stroke(Color.argb(210, 52, 68, 66), 4f);
        oval.set(130, 180, 192, 224); c.drawOval(oval, paint);
        oval.set(208, 180, 270, 224); c.drawOval(oval, paint);
        path.reset();
        path.moveTo(192, 198); path.lineTo(208, 198);
        c.drawPath(path, paint);
        path.reset();
        path.moveTo(130, 194); path.lineTo(112, 190);
        c.drawPath(path, paint);
        path.reset();
        path.moveTo(270, 194); path.lineTo(288, 190);
        c.drawPath(path, paint);
    }

    private void drawBadge(Canvas c, int accent, String no) {
        fill(Color.argb(226, 255, 255, 255));
        c.drawRoundRect(266, 396, 336, 442, 7, 7, paint);
        fill(accent);
        c.drawRoundRect(266, 396, 336, 404, 3, 3, paint);
        stroke(Color.argb(110, 70, 90, 88), 2.5f);
        c.drawLine(276, 416, 326, 416, paint);
        c.drawLine(276, 426, 312, 426, paint);
    }

    private void drawSoundWaves(Canvas c, int accent, float vol, long t) {
        int a = (int) (60 + 120 * vol);
        stroke(withAlpha(accent, a), 3.5f);
        for (int side = -1; side <= 1; side += 2) {
            for (int i = 0; i < 3; i++) {
                float r = 118 + i * 15 + vol * 12 + (float) Math.sin(t / 300.0 + i) * 2;
                oval.set(200 - r, 210 - r, 200 + r, 210 + r);
                float start = side < 0 ? 145 - i * 8 : -35 + i * 8;
                c.drawArc(oval, start, 38, false, paint);
                c.drawArc(oval, 180 + start, 38, false, paint);
            }
        }
    }

    // ------------------------------------------------------------------ 表情 / 动作

    private static final class Mood {
        float browLift, browAngle, eyeOpen, smile, blush, gaze;
    }

    private static Mood mood(String name) {
        Mood m = new Mood();
        m.browLift = 0; m.browAngle = 0; m.eyeOpen = 1f; m.smile = 0.5f;
        m.blush = 0.15f; m.gaze = 0;
        if (name == null) return m;
        switch (name) {
            case "cheerful": case "happy":
                m.browLift = 5; m.browAngle = -0.4f; m.smile = 1.4f;
                m.blush = 0.85f; m.eyeOpen = 0.86f; break;
            case "confident":
                m.browLift = 3; m.browAngle = 0.5f; m.smile = 0.85f;
                m.blush = 0.1f; break;
            case "thinking":
                m.browLift = -2; m.browAngle = 0.8f; m.eyeOpen = 0.78f;
                m.smile = -0.1f; m.gaze = 5; break;
            case "concerned": case "error":
                m.browLift = -6; m.browAngle = -1.1f; m.eyeOpen = 0.92f;
                m.smile = -1.2f; m.blush = 0; break;
            case "serious":
                m.browLift = -3; m.browAngle = 0.4f; m.eyeOpen = 0.9f;
                m.smile = 0f; m.blush = 0; break;
            case "calm": default:
                break;
        }
        return m;
    }

    private float blinkFactor(long t) {
        long phase = t % 3800;
        if (phase < 120) return 1f - phase / 120f;
        if (phase < 240) return (phase - 120) / 120f;
        return 0f;
    }

    // ------------------------------------------------------------------ Paint 辅助

    private void fill(int color) {
        paint.setShader(null);
        paint.setStyle(Paint.Style.FILL);
        paint.setColor(color);
        paint.setStrokeWidth(0f);
    }

    private void stroke(int color, float width) {
        paint.setShader(null);
        paint.setStyle(Paint.Style.STROKE);
        paint.setColor(color);
        paint.setStrokeWidth(width);
    }

    private void shader(Shader s) {
        paint.setShader(s);
        paint.setStyle(Paint.Style.FILL);
        paint.setStrokeWidth(0f);
    }

    private static int withAlpha(int color, int alpha) {
        return Color.argb(Math.max(0, Math.min(255, alpha)),
                Color.red(color), Color.green(color), Color.blue(color));
    }

    private static int lighten(int color, float f) { return scale(color, f); }
    private static int darken(int color, float f) { return scale(color, f); }

    private static int scale(int color, float f) {
        return Color.rgb(clamp(Color.red(color) * f),
                clamp(Color.green(color) * f), clamp(Color.blue(color) * f));
    }

    private static int clamp(float v) {
        return Math.max(0, Math.min(255, (int) v));
    }
}
