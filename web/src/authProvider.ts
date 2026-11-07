import type { AuthProvider, CheckResponse } from "@refinedev/core";

const USER_KEY = "opc.user";

// Refine 4.x 的新版 AuthProvider 用 check 承担旧版 checkAuth 的职责；
// 这里保留 checkAuth 命名，语义与任务要求一致：GET /api/state 判断 200/401。
async function checkAuth(): Promise<CheckResponse> {
  const res = await fetch("/api/state", { credentials: "include" });
  if (res.ok) return { authenticated: true };
  if (res.status === 401) return { authenticated: false, redirectTo: "/login" };
  return {
    authenticated: false,
    error: { name: "AuthError", message: `HTTP ${res.status}` } as any,
  };
}

export const authProvider: AuthProvider = {
  login: async (params: any) => {
    const account = String(params?.account ?? "");
    const password = String(params?.password ?? "");
    const res = await fetch("/api/login", {
      method: "POST",
      credentials: "include",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ account, password }),
    });
    const body = await res.json().catch(() => ({}) as Record<string, unknown>);
    if (!res.ok || !body.ok) {
      return {
        success: false,
        error: {
          name: "LoginError",
          message: String(body.error || `登录失败（HTTP ${res.status}）`),
        },
      };
    }
    localStorage.setItem(USER_KEY, JSON.stringify(body.user ?? {}));
    return { success: true, redirectTo: "/hotspots" };
  },
  check: checkAuth,
  getIdentity: async () => {
    // 以服务端会话为准，避免本地缓存与真实登录态不一致
    try {
      const res = await fetch("/api/session", { credentials: "include" });
      if (res.ok) {
        const body = await res.json();
        localStorage.setItem(USER_KEY, JSON.stringify(body.user ?? {}));
        return body.user ?? null;
      }
    } catch {
      // 网络异常时退回本地缓存
    }
    const raw = localStorage.getItem(USER_KEY);
    if (!raw) return null;
    try {
      return JSON.parse(raw);
    } catch {
      return null;
    }
  },
  logout: async () => {
    try {
      await fetch("/api/logout", { method: "POST", credentials: "include" });
    } catch {
      // 后端不可达时仍然清理本地状态
    }
    localStorage.removeItem(USER_KEY);
    return { success: true, redirectTo: "/login" };
  },
  onError: async (error: any) => {
    if (error?.statusCode === 401) {
      window.location.assign("/app/login");
      return { logout: true, error };
    }
    return { error };
  },
};
