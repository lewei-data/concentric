import { appRoot } from "../appRoot";

/**
 * 给站内绝对路径补上「服务根」前缀。
 *
 * 内网直连是 http://host:8848/app/...，公网是 https://域名/workbench/app/...，
 * 网关会把 /workbench 去掉再转给服务，所以前端拼 /private/...、/materials/...
 * 这类地址时必须自己把前缀带回去，否则公网会落到主站而不是 workbench。
 *
 * 前缀口径统一交给 appRoot()：/workbench/app/materials → /workbench/，/app/materials → /。
 * 完整 URL（http://、blob:、data:）原样返回。
 */
export function siteUrl(path: string): string {
  if (!path || !path.startsWith("/")) return path;
  return appRoot() + path.slice(1);
}