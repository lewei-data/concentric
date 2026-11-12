import { NavLink, Outlet, useLocation } from "react-router-dom";
import { useGetIdentity } from "@refinedev/core";
import { authProvider } from "../authProvider";
import type { OpcUser } from "../types";

function ConcentricLogo() {
  return (
    <svg className="brand-logo" viewBox="0 0 40 40" role="img" aria-label="同心圆 LOGO">
      <defs>
        <linearGradient id="brand-gradient" x1="0" y1="0" x2="1" y2="1">
          <stop offset="0%" stopColor="#63a4ff" />
          <stop offset="100%" stopColor="#a78bfa" />
        </linearGradient>
      </defs>
      <circle cx="20" cy="20" r="19" fill="url(#brand-gradient)" />
      <circle cx="20" cy="20" r="13.5" fill="none" stroke="rgba(255,255,255,.75)" strokeWidth="1.6" />
      <circle cx="20" cy="20" r="8.5" fill="none" stroke="#ffffff" strokeWidth="2" />
      <circle cx="20" cy="20" r="3.2" fill="#ffffff" />
    </svg>
  );
}

const PRIMARY_NAV = [
  { to: "/", label: "总览", end: true },
  { to: "/works", label: "作品档案" },
  { to: "/hotspots", label: "热点" },
  { to: "/knowledge", label: "知识库" },
  { to: "/org", label: "组织配置" },
];

export function Layout() {
  const { data: identity } = useGetIdentity<OpcUser>();
  const location = useLocation();
  // 统一消息只对老板开放，后端 /api/chat/* 对其他角色返回 403，这里同步隐藏入口
  const isOwner = String(identity?.role ?? "").includes("老板");
  const isScene =
    location.pathname.startsWith("/office") || location.pathname.startsWith("/meeting");

  const displayName = identity?.name ?? identity?.user ?? "未登录";
  const initial = displayName.slice(0, 1).toUpperCase();

  const handleLogout = async () => {
    await authProvider.logout({});
    window.location.assign("/app/login");
  };

  const navItems = [...PRIMARY_NAV];
  if (isOwner) navItems.splice(4, 0, { to: "/messages", label: "消息中心", end: false });

  return (
    <div className="app">
      <header className="topbar">
        <div className="topbar-inner">
          <NavLink to="/" className="brand-block" aria-label="同心圆 · OPC AI 员工工作台首页">
            <ConcentricLogo />
            <span className="brand-text">
              <b>同心圆</b>
              <small>OPC · AI 员工工作台</small>
            </span>
          </NavLink>

          <nav className="nav nav-main" aria-label="主导航">
            {navItems.map((item) => (
              <NavLink key={item.to} to={item.to} end={item.end}>
                {item.label}
              </NavLink>
            ))}
          </nav>

          <nav className="nav nav-extra" aria-label="场景与旧版入口">
            <span className="nav-divider" aria-hidden="true" />
            <NavLink to="/office" className="nav-3d">Office 3D</NavLink>
            <NavLink to="/meeting" className="nav-3d">Meeting 3D</NavLink>
            <a href="/legacy" className="nav-legacy" title="回到旧版控制台（仍可随时切回新版）">
              旧版控制台
            </a>
          </nav>

          <div className="userbox">
            <span className="user-avatar" aria-hidden="true">{initial}</span>
            <span className="user-meta">
              <b>{displayName}</b>
              <small>{identity?.role || "成员"}</small>
            </span>
            <span className="live-badge" title="已连接实时事件流">
              <i aria-hidden="true" />实时
            </span>
            <button type="button" className="btn btn-ghost logout-btn" onClick={handleLogout}>
              退出
            </button>
          </div>
        </div>
      </header>
      <main className={isScene ? "content content-scene" : "content"}>
        <Outlet />
      </main>
    </div>
  );
}
