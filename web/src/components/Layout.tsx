import { useEffect, useRef, useState } from "react";
import { NavLink, Outlet, useLocation, useNavigate } from "react-router-dom";
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
  { to: "/inspiration", label: "创意灵感" },
  { to: "/works", label: "作品档案" },
  { to: "/factory", label: "制片工厂" },
  { to: "/knowledge", label: "知识库" },
  { to: "/ops", label: "运维监控" },
  { to: "/hotspots", label: "热点" },
];

export function Layout() {
  const { data: identity } = useGetIdentity<OpcUser>();
  const location = useLocation();
  const navigate = useNavigate();
  const [menuOpen, setMenuOpen] = useState(false);
  const menuRef = useRef<HTMLDivElement>(null);
  // 统一消息只对老板开放，后端 /api/chat/* 对其他角色返回 403，这里同步隐藏入口
  const isOwner = String(identity?.role ?? "").includes("老板");
  const isScene =
    location.pathname.startsWith("/office") || location.pathname.startsWith("/meeting");

  const displayName = identity?.name ?? identity?.user ?? "未登录";
  const initial = displayName.slice(0, 1).toUpperCase();

  // 点击菜单外部 / 切换路由时收起下拉
  useEffect(() => {
    const onDown = (e: MouseEvent) => {
      if (menuRef.current && !menuRef.current.contains(e.target as Node)) setMenuOpen(false);
    };
    document.addEventListener("mousedown", onDown);
    return () => document.removeEventListener("mousedown", onDown);
  }, []);
  useEffect(() => setMenuOpen(false), [location.pathname]);

  const handleLogout = async () => {
    await authProvider.logout({});
    window.location.assign("/app/login");
  };

  const navItems = [...PRIMARY_NAV];

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

          <nav className="nav nav-extra" aria-label="场景入口">
            <span className="nav-divider" aria-hidden="true" />
            <NavLink to="/office" className="nav-3d">办公室</NavLink>
            <NavLink to="/meeting" className="nav-3d">会议室</NavLink>
          </nav>

          <div className="userbox" ref={menuRef}>
            <button
              type="button"
              className="user-entry"
              title="个人菜单（基本信息 / 组织配置 / 退出登录）"
              aria-haspopup="menu"
              aria-expanded={menuOpen}
              onClick={() => setMenuOpen((v) => !v)}
            >
              {identity?.avatar ? (
                <img className="user-avatar-img" src={identity.avatar} alt="" />
              ) : (
                <span className="user-avatar" aria-hidden="true">{initial}</span>
              )}
              <span className="user-meta">
                <b>{displayName}</b>
                <small>{identity?.role || "成员"}</small>
              </span>
              <span className="user-caret" aria-hidden="true">▾</span>
            </button>
            {menuOpen && (
              <div className="user-menu" role="menu">
                <button
                  type="button"
                  role="menuitem"
                  onClick={() => { setMenuOpen(false); navigate("/account"); }}
                >
                  个人信息
                </button>
                <button
                  type="button"
                  role="menuitem"
                  onClick={() => { setMenuOpen(false); navigate("/account/assets"); }}
                >
                  数据资产
                </button>
                <button
                  type="button"
                  role="menuitem"
                  onClick={() => { setMenuOpen(false); navigate("/account/settings"); }}
                >
                  系统配置
                </button>
                <div className="user-menu-sep" aria-hidden="true" />
                {isOwner && (
                  <button
                    type="button"
                    role="menuitem"
                    onClick={() => { setMenuOpen(false); navigate("/org"); }}
                  >
                    组织配置
                  </button>
                )}
                <button
                  type="button"
                  role="menuitem"
                  onClick={() => { setMenuOpen(false); navigate("/account/help"); }}
                >
                  帮助
                </button>
                <button type="button" role="menuitem" className="user-menu-danger"
                        onClick={() => void handleLogout()}>
                  退出
                </button>
              </div>
            )}
            <span className="live-badge" title="已连接实时事件流">
              <i aria-hidden="true" />实时
            </span>
          </div>
        </div>
      </header>
      <main className={isScene ? "content content-scene" : "content"}>
        <Outlet />
      </main>
    </div>
  );
}
