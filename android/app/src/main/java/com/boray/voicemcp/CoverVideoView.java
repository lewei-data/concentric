package com.boray.voicemcp;

import android.content.Context;
import android.util.AttributeSet;
import android.widget.VideoView;

/**
 * 会「铺满并裁切」的 VideoView（center-crop）。
 *
 * 原生 VideoView 保持视频宽高比并留黑边（letterbox），把 720×960 的竖版介绍视频
 * 放进横向舞台就会上下黑边、画面显得很小。这里在 onMeasure 里把自身量成
 * 「与视频同宽高比、且不小于父容器」的尺寸，再由 FrameLayout（clipChildren=true、
 * layout_gravity=center）裁掉溢出部分，视觉上就是铺满。
 *
 * 视频宽高在 prepare 之后由调用方（MediaPlayer.getVideoWidth/Height）通过
 * {@link #setVideoSize(int, int)} 告知，因为 VideoView 的 mVideoWidth 是隐藏字段。
 */
public class CoverVideoView extends VideoView {

    private int videoWidth = 0;
    private int videoHeight = 0;

    public CoverVideoView(Context context) {
        super(context);
    }

    public CoverVideoView(Context context, AttributeSet attrs) {
        super(context, attrs);
    }

    public CoverVideoView(Context context, AttributeSet attrs, int defStyleAttr) {
        super(context, attrs, defStyleAttr);
    }

    /** prepare 之后调用：写入真实视频尺寸并重新布局 */
    public void setVideoSize(int width, int height) {
        if (width <= 0 || height <= 0) return;
        if (width == videoWidth && height == videoHeight) return;
        videoWidth = width;
        videoHeight = height;
        requestLayout();
        invalidate();
    }

    /** 兼容旧调用名 */
    public void refreshScale() {
        requestLayout();
        invalidate();
    }

    @Override
    protected void onMeasure(int widthMeasureSpec, int heightMeasureSpec) {
        int width = getDefaultSize(getSuggestedMinimumWidth(), widthMeasureSpec);
        int height = getDefaultSize(getSuggestedMinimumHeight(), heightMeasureSpec);
        if (videoWidth > 0 && videoHeight > 0 && width > 0 && height > 0) {
            // 覆盖比例：两个方向都不小于容器，多出来的部分由父容器裁掉
            float scale = Math.max(width / (float) videoWidth, height / (float) videoHeight);
            setMeasuredDimension(Math.round(videoWidth * scale),
                    Math.round(videoHeight * scale));
        } else {
            setMeasuredDimension(width, height);
        }
    }
}
