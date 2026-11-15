import { useCallback, useEffect, useLayoutEffect, useRef, useState, type ReactNode } from "react";
import { createPortal } from "react-dom";

type Props = {
  /** 触发弹窗的元素：面板贴着它弹，滚动时跟着走 */
  anchor: HTMLElement | null;
  onClose: () => void;
  className?: string;
  ariaLabel?: string;
  children: ReactNode;
};

/**
 * 贴着触发元素弹出的小面板。
 * 默认垂在下方，下方放不下翻到上方，左右自动收进视口；滚动、缩放、内容变高都会重算位置。
 * 挂到 body 上：页面的进入动画会给祖先留下 transform，那样 position: fixed 就不再相对视口了。
 */
export function AnchoredPanel({ anchor, onClose, className = "", ariaLabel, children }: Props) {
  const ref = useRef<HTMLDivElement | null>(null);
  const [pos, setPos] = useState<{ top: number; left: number } | null>(null);

  const place = useCallback(() => {
    const el = ref.current;
    if (!el || !anchor) return;
    const gap = 10;
    const edge = 12;
    const rect = anchor.getBoundingClientRect();
    const dw = el.offsetWidth;
    const dh = el.offsetHeight;
    const vw = window.innerWidth;
    const vh = window.innerHeight;
    let top = rect.bottom + gap;
    if (top + dh > vh - edge) {
      const above = rect.top - gap - dh;
      top = above >= edge ? above : Math.max(edge, vh - dh - edge);
    }
    let left = rect.left;
    if (left + dw > vw - edge) left = vw - dw - edge;
    left = Math.max(edge, left);
    setPos({ top, left });
  }, [anchor]);

  useLayoutEffect(() => {
    place();
    const el = ref.current;
    const ro = el ? new ResizeObserver(() => place()) : null;
    if (el && ro) ro.observe(el);
    window.addEventListener("resize", place);
    window.addEventListener("scroll", place, true);
    return () => {
      ro?.disconnect();
      window.removeEventListener("resize", place);
      window.removeEventListener("scroll", place, true);
    };
  }, [place]);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  return createPortal(
    <div className="pop-backdrop" onClick={onClose} role="presentation">
      <div
        ref={ref}
        className={`pop-panel ${className}`.trim()}
        style={{
          top: pos ? pos.top : -9999,
          left: pos ? pos.left : -9999,
          visibility: pos ? "visible" : "hidden",
        }}
        onClick={(e) => e.stopPropagation()}
        role="dialog"
        aria-label={ariaLabel}
      >
        {children}
      </div>
    </div>,
    document.body,
  );
}