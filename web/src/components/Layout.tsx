import { NavLink, Outlet, useLocation } from "react-router-dom";
import { useGetIdentity } from "@refinedev/core";
import { authProvider } from "../authProvider";
import type { OpcUser } from "../types";

function ConcentricLogo() {
  return (
    <svg className="brand-logo" viewBox="0 0 40 40" role="img" aria-label="同心圆 LOGO">
      <defs>
        <linearGradient id="brand-gradient" x1="0" y1="0" x2="1" y2="1">
          <stop offset="0%" stopColor="#2f6fed" />
          <stop offset="100%" stopColor="#7c5cff" />
        </linearGradient>
      </defs>
      <circle cx="20" cy="20" r="19" fill="url(#brand-gradient)" />
      <circle cx="20" cy="20" r="13.5" fill="none" stroke="rgba(255,255,255,.55)" strokeWidth="1.6" />
      <circle cx="20" cy="20" r="8.5" fill="none" stroke="#ffffff" strokeWidth="2" />
      <circle cx="20" cy="20" r="3.2" fill="#ffffff" />
    </svg>
  );
}

export function Layout() {
  const { data: identity } = useGetIdentity<OpcUser>();
  const location = useLocation();
  const isScene =
    location.pathname.startsWith("/office") || location.pathname.startsWith("/meeting");

  const handleLogout = async () => {
    await authProvider.logout({});
    window.location.assign("/app/login");
  };

  return (
    <div className="app">
      <header className="topbar">
        <NavLink to="/" className="brand-block" aria-label="同心圆 · OPC AI 员工工作台首页">
          <ConcentricLogo />
          <span className="brand-text">
            <b>同心圆</b>
            <small>OPC · AI 员工工作台</small>
          </span>
        </NavLink>
        <nav className="nav">
          <NavLink to="/" end>首页</NavLink>
          <NavLink to="/works">作品档案</NavLink>
          <NavLink to="/hotspots">热点</NavLink>
          <NavLink to="/knowledge">知识库</NavLink>
          <NavLink to="/org">组织配置</NavLink>
          <span className="nav-divider" aria-hidden="true" />
          <NavLink to="/office" className="nav-3d">Office 3D</NavLink>
          <NavLink to="/meeting" className="nav-3d">Meeting 3D</NavLink>
          <a href="/" className="nav-legacy" title="旧版界面将逐步下线">
            旧版（逐步淘汰）
          </a>
        </nav>
        <div className="userbox">
          <span className="username">{identity?.name ?? identity?.user ?? "未登录"}</span>
          <button type="button" className="btn btn-ghost" onClick={handleLogout}>
            退出
          </button>
        </div>
      </header>
      <main className={isScene ? "content content-scene" : "content"}>
        <Outlet />
      </main>
    </div>
  );
}
