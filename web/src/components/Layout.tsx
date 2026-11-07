import { NavLink, Outlet } from "react-router-dom";
import { useGetIdentity } from "@refinedev/core";
import { authProvider } from "../authProvider";
import type { OpcUser } from "../types";

export function Layout() {
  const { data: identity } = useGetIdentity<OpcUser>();

  const handleLogout = async () => {
    await authProvider.logout({});
    window.location.assign("/app/login");
  };

  return (
    <div className="app">
      <header className="topbar">
        <div className="brand">OPC 内容生产工作台</div>
        <nav className="nav">
          <NavLink to="/" end>首页</NavLink>
          <NavLink to="/works">作品档案</NavLink>
          <NavLink to="/hotspots">热点</NavLink>
          <NavLink to="/knowledge">知识库</NavLink>
          <NavLink to="/org">组织配置</NavLink>
          <a href="/">返回旧版</a>
        </nav>
        <div className="userbox">
          <span className="username">{identity?.name ?? identity?.user ?? "未登录"}</span>
          <button type="button" className="btn btn-ghost" onClick={handleLogout}>
            退出
          </button>
        </div>
      </header>
      <main className="content">
        <Outlet />
      </main>
    </div>
  );
}
