import { useEffect, useRef, useState } from "react";
import { NavLink, Outlet, useLocation, useNavigate } from "react-router-dom";
import { useGetIdentity } from "@refinedev/core";
import { authProvider } from "../authProvider";
import { useWuxing } from "../wuxing";
import type { OpcUser } from "../types";
import AssistantWidget from "./AssistantWidget";
import { WuxingBadge, WuxingLogo, WuxingWordmark } from "./WuxingLogo";

const PRIMARY_NAV = [
  { to: "/", label: "总览", end: true },
  { to: "/inspiration", label: "创意灵感" },
  { to: "/works", label: "作品档案" },
  { to: "/factory", label: "制片工厂" },
  { to: "/knowledge", label: "知识库" },
  { to: "/ops", label: "运维监控" },
];

export function Layout() {
  const { data: identity } = useGetIdentity<OpcUser>();
  const wx = useWuxing();
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
  if (isOwner) navItems.push({ to: "/messages", label: "消息中心", end: false });
  navItems.push({ to: "/hotspots", label: "热点" });

  return (
    <div className="app">
      <header className="topbar">
        <div className="topbar-inner">
          <NavLink to="/" className="brand-block" aria-label="同心圆 · OPC AI 员工工作台首页">
            <WuxingLogo element={wx.element} size={42} className="brand-logo" />
            <span className="brand-text">
              <WuxingWordmark element={wx.element} />
              <small>同心圆 · OPC AI 员工工作台</small>
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
            <NavLink to="/office" className="nav-icon-btn" title="办公室">
              <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
                <rect x="2" y="7" width="20" height="14" rx="2" />
                <path d="M16 21V5a2 2 0 0 0-2-2h-4a2 2 0 0 0-2 2v16" />
              </svg>
            </NavLink>
            <NavLink to="/meeting" className="nav-icon-btn" title="会议室">
              <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
                <path d="M17 21v-2a4 4 0 0 0-4-4H5a4 4 0 0 0-4 4v2" />
                <circle cx="9" cy="7" r="4" />
                <path d="M23 21v-2a4 4 0 0 0-3-3.87" />
                <path d="M16 3.13a4 4 0 0 1 0 7.75" />
              </svg>
            </NavLink>
          </nav>

          <WuxingBadge />

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
                  <>
                    <button
                      type="button"
                      role="menuitem"
                      onClick={() => { setMenuOpen(false); navigate("/models"); }}
                    >
                      模型管理
                    </button>
                    <button
                      type="button"
                      role="menuitem"
                      onClick={() => { setMenuOpen(false); navigate("/org"); }}
                    >
                      组织配置
                    </button>
                  </>
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
          </div>
        </div>
      </header>
      <main className={isScene ? "content content-scene" : "content"}>
        <Outlet />
      </main>
      {isOwner && <AssistantWidget />}
    </div>
  );
}
