import { useCallback, useEffect, useLayoutEffect, useRef, useState, type ReactNode } from "react";
import { createPortal } from "react-dom";

type Props = {
  /** 触发弹窗的元素：面板贴着它弹，滚动时跟着走 */
  anchor: HTMLElement | null;
  onClose: () => void;
  className?: string;
  ariaLabel?: string;
  /** 拖动条上显示的短标题（可选） */
  anchorLabel?: string;
  children: ReactNode;
};

type Pos = { top: number; left: number };

const EDGE = 12;
const GAP = 12;

const clamp = (value: number, low: number, high: number) =>
  Math.max(low, Math.min(high, value));

/**
 * 贴着触发元素弹出的面板：
 * - 在锚点四周选一个能完整落进视口的位置（右 → 左 → 下 → 上），都不够时取离锚点最近的一处并夹回视口；
 * - 尺寸永远不超过视口，内容超高时面板内部滚动；
 * - 顶部有拖动条，可直接拖到任意位置；拖动后仍随锚点滚动（保留相对偏移），可一键复位；
 * - 挂到 body：页面动画会给祖先留下 transform，那样 position: fixed 就不再相对视口。
 */
export function AnchoredPanel({
  anchor,
  onClose,
  className = "",
  ariaLabel,
  anchorLabel,
  children,
}: Props) {
  const ref = useRef<HTMLDivElement | null>(null);
  const baseRef = useRef<Pos>({ top: 0, left: 0 });
  const dragRef = useRef<{ x: number; y: number; ox: number; oy: number } | null>(null);
  const [pos, setPos] = useState<Pos | null>(null);
  const [offset, setOffset] = useState({ dx: 0, dy: 0 });
  const [dragging, setDragging] = useState(false);

  const place = useCallback(() => {
    const el = ref.current;
    if (!el) return;
    const vw = window.innerWidth;
    const vh = window.innerHeight;
    const dw = Math.min(el.offsetWidth, vw - EDGE * 2);
    const dh = Math.min(el.offsetHeight, vh - EDGE * 2);
    const rect = anchor?.getBoundingClientRect();

    let baseTop: number;
    let baseLeft: number;
    if (rect && rect.width > 0 && rect.height > 0) {
      // 四个候选方位，优先左右（不遮住被点的图/视频），再考虑上下
      const candidates: Pos[] = [
        { top: rect.top, left: rect.right + GAP },
        { top: rect.top, left: rect.left - dw - GAP },
        { top: rect.bottom + GAP, left: rect.left },
        { top: rect.top - dh - GAP, left: rect.left },
      ];
      const fits = candidates.find(
        (c) => c.top >= EDGE && c.top + dh <= vh - EDGE
          && c.left >= EDGE && c.left + dw <= vw - EDGE,
      );
      if (fits) {
        baseTop = fits.top;
        baseLeft = fits.left;
      } else {
        // 都放不下：取中心离锚点最近的一处，最后统一夹回视口
        const cx = rect.left + rect.width / 2;
        const cy = rect.top + rect.height / 2;
        const nearest = candidates.reduce((best, c) => {
          const dBest = Math.hypot(best.left + dw / 2 - cx, best.top + dh / 2 - cy);
          const dCur = Math.hypot(c.left + dw / 2 - cx, c.top + dh / 2 - cy);
          return dCur < dBest ? c : best;
        });
        baseTop = nearest.top;
        baseLeft = nearest.left;
      }
    } else {
      baseTop = (vh - dh) / 2;
      baseLeft = (vw - dw) / 2;
    }
    baseRef.current = { top: baseTop, left: baseLeft };
    setPos({
      top: clamp(baseTop + offset.dy, EDGE, Math.max(EDGE, vh - dh - EDGE)),
      left: clamp(baseLeft + offset.dx, EDGE, Math.max(EDGE, vw - dw - EDGE)),
    });
  }, [anchor, offset.dx, offset.dy]);

  useLayoutEffect(() => {
    place();
    const el = ref.current;
    const ro = el ? new ResizeObserver(() => place()) : null;
    if (el && ro) ro.observe(el);
    window.addEventListener("resize", place);
    // 捕获阶段：页面内任意容器滚动都要重算，面板才跟得住锚点
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

  // 拖动时临时禁用选中，避免拖过文字时一片高亮
  useEffect(() => {
    if (!dragging) return;
    const prev = document.body.style.userSelect;
    document.body.style.userSelect = "none";
    return () => { document.body.style.userSelect = prev; };
  }, [dragging]);

  const onPointerDown = (e: React.PointerEvent<HTMLDivElement>) => {
    if (e.button !== 0) return;
    dragRef.current = { x: e.clientX, y: e.clientY, ox: offset.dx, oy: offset.dy };
    e.currentTarget.setPointerCapture(e.pointerId);
    setDragging(true);
  };
  const onPointerMove = (e: React.PointerEvent<HTMLDivElement>) => {
    const d = dragRef.current;
    if (!d) return;
    setOffset({ dx: d.ox + e.clientX - d.x, dy: d.oy + e.clientY - d.y });
  };
  const onPointerUp = () => {
    const d = dragRef.current;
    dragRef.current = null;
    setDragging(false);
    if (!d || !pos) return;
    // 夹到视口边缘后会有“拖不动的死区”，松手时把 offset 收敛到实际落点
    const base = baseRef.current;
    setOffset({ dx: pos.left - base.left, dy: pos.top - base.top });
  };

  return createPortal(
    <div className="pop-backdrop" onClick={onClose} role="presentation">
      <div
        ref={ref}
        className={`pop-panel${dragging ? " is-dragging" : ""} ${className}`.trim()}
        style={{
          top: pos ? pos.top : -9999,
          left: pos ? pos.left : -9999,
          visibility: pos ? "visible" : "hidden",
          maxWidth: `calc(100vw - ${EDGE * 2}px)`,
          maxHeight: `calc(100vh - ${EDGE * 2}px)`,
        }}
        onClick={(e) => e.stopPropagation()}
        role="dialog"
        aria-label={ariaLabel}
      >
        <div
          className="pop-drag"
          onPointerDown={onPointerDown}
          onPointerMove={onPointerMove}
          onPointerUp={onPointerUp}
          onPointerCancel={onPointerUp}
          title="按住拖动面板"
        >
          <span className="pop-drag-grip" aria-hidden="true">⠿</span>
          <span className="pop-drag-label">{anchorLabel || "拖动"}</span>
          <button
            type="button"
            className="pop-drag-reset"
            onPointerDown={(e) => e.stopPropagation()}
            onClick={() => setOffset({ dx: 0, dy: 0 })}
            title="回到触发元素旁边"
          >
            复位
          </button>
        </div>
        {children}
      </div>
    </div>,
    document.body,
  );
}
