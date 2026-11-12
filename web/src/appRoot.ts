/**
 * 计算应用的「服务根」前缀。
 *
 * 部署形态：SPA 挂在 `<root>app/...`，而 3D 页（office3d / meeting3d）、
 * /vendor、/faces、/avatars、/api 都在「服务根」，不在 /app 下面。
 *
 * 公网 nginx 会把首页 302 到 `https://ai.boraydata.com/workbench/app`（**没有尾斜杠**），
 * 旧实现「去掉末尾两段」会把 `/workbench/app` 算成 `/`，
 * 于是 iframe/独立窗口去请求 `/office3d`，被网关落到 sefs-mcp，返回：
 *   {"ok":false,"error":"unauthorized","message":"未携带 Token…"}
 *
 * 这里改为按路径中的 `app` 段定位：
 *   /app/office            → /
 *   /app                   → /
 *   /workbench/app         → /workbench/
 *   /workbench/app/office  → /workbench/
 */
export function appRoot(pathname: string = window.location.pathname): string {
  const parts = pathname.split("/");
  const idx = parts.lastIndexOf("app");
  if (idx > 0) return `${parts.slice(0, idx).join("/")}/`;
  return "/";
}
