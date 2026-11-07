import { Link, Outlet } from "react-router-dom";
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
          <Link to="/hotspots">热点</Link>
          <Link to="/knowledge">知识库</Link>
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
