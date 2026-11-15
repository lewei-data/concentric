import { useEffect } from "react";
import { siteUrl } from "../lib/siteUrl";

export type ViewerItem = {
  /** 站内裸路径（如 /private/...），服务根前缀由本组件补，调用方别自己拼 */
  url: string;
  kind: string;
  title?: string;
};

/** 成品查看层：图片按原比例、视频/语音带播放器。Esc 或点遮罩关闭。 */
export function MediaViewer({ item, onClose }: { item: ViewerItem | null; onClose: () => void }) {
  useEffect(() => {
    if (!item) return;
    const onKey = (e: KeyboardEvent) => { if (e.key === "Escape") onClose(); };
    window.addEventListener("keydown", onKey);
    const prev = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    return () => {
      window.removeEventListener("keydown", onKey);
      document.body.style.overflow = prev;
    };
  }, [item, onClose]);

  if (!item) return null;

  // 公网挂在 /workbench 下，裸 /private/... 会落到主站被拦成 401，这里统一补前缀
  const url = siteUrl(item.url);

  return (
    <div className="tb-zoom" role="dialog" aria-modal="true" aria-label="查看成品" onClick={onClose}>
      <div className="tb-zoom-inner" onClick={(e) => e.stopPropagation()}>
        <div className="tb-zoom-bar">
          <span className="tb-zoom-title">{item.title || item.kind}</span>
          <a className="tb-link" href={url} target="_blank" rel="noreferrer">新窗口</a>
          <button type="button" className="tb-zoom-x" onClick={onClose} aria-label="关闭">×</button>
        </div>
        {item.kind === "image" && (
          <img className="tb-zoom-img" src={url} alt={item.title || ""} />
        )}
        {item.kind === "video" && (
          <video className="tb-zoom-video" src={url} controls autoPlay />
        )}
        {item.kind === "audio" && (
          <div className="tb-zoom-audio">
            <p>{item.title || "口播"}</p>
            <audio src={url} controls autoPlay />
          </div>
        )}
      </div>
    </div>
  );
}