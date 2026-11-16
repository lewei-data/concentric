import { siteUrl } from "../lib/siteUrl";
import { AnchoredPanel } from "./AnchoredPanel";

export type ViewerItem = {
  /** 站内裸路径（如 /private/...），服务根前缀由本组件补，调用方别自己拼 */
  url: string;
  kind: string;
  title?: string;
};

type Props = {
  item: ViewerItem | null;
  /** 被点的缩略图/按钮：弹窗贴着它出现并可拖动；不传则居中 */
  anchor?: HTMLElement | null;
  onClose: () => void;
};

/** 成品查看层：贴着被点媒体弹出、可拖动、始终留在视口内。Esc 或点遮罩关闭。 */
export function MediaViewer({ item, anchor, onClose }: Props) {
  if (!item) return null;

  // 公网挂在 /workbench 下，裸 /private/... 会落到主站被拦成 401，这里统一补前缀
  const url = siteUrl(item.url);

  return (
    <AnchoredPanel
      anchor={anchor ?? null}
      onClose={onClose}
      className="tb-zoom-panel"
      ariaLabel="查看成品"
      anchorLabel={item.title || item.kind}
    >
      <div className="tb-zoom-inner">
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
    </AnchoredPanel>
  );
}
