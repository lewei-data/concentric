import Markdown from "react-markdown";
import remarkGfm from "remark-gfm";

/**
 * 知识正文的 Markdown 渲染。
 * 单独成文件是为了让 KnowledgePage 用动态 import 懒加载它：
 * react-markdown + remark-gfm 体积较大，只在打开详情时才下载。
 */
export default function MarkdownBody({ text }: { text: string }) {
  return <Markdown remarkPlugins={[remarkGfm]}>{text}</Markdown>;
}
