import { useState } from "react";
import { useNavigate } from "react-router-dom";
import { authProvider } from "../authProvider";

// 「记住我」凭据存放键（明文保存，仅适合本机个人使用）
const REMEMBER_KEY = "opc.remember";

type Remembered = { account: string; password: string };

function loadRemembered(): Remembered | null {
  try {
    const raw = localStorage.getItem(REMEMBER_KEY);
    if (!raw) return null;
    const v = JSON.parse(raw) as Partial<Remembered>;
    if (v && typeof v.account === "string" && typeof v.password === "string") {
      return { account: v.account, password: v.password };
    }
  } catch {
    /* 解析失败时当作未记住 */
  }
  return null;
}

export function LoginPage() {
  const [remembered] = useState(loadRemembered);
  const [account, setAccount] = useState(remembered?.account ?? "");
  const [password, setPassword] = useState(remembered?.password ?? "");
  const [remember, setRemember] = useState(!!remembered);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(false);
  const navigate = useNavigate();

  const handleSubmit = async (event: React.FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    setError("");
    setLoading(true);
    try {
      const result = await authProvider.login({ account, password });
      if (result.success) {
        // 记住我：保存账号密码；未勾选则清除之前保存的
        try {
          if (remember) {
            localStorage.setItem(REMEMBER_KEY, JSON.stringify({ account, password }));
          } else {
            localStorage.removeItem(REMEMBER_KEY);
          }
        } catch {
          /* 隐私模式下写入失败则忽略 */
        }
        navigate("/");
      } else {
        setError(result.error?.message ?? "登录失败");
      }
    } catch {
      setError("网络异常，请稍后重试");
    } finally {
      setLoading(false);
    }
  };

  return (
    <div className="login-wrap">
      <form className="card login-card" onSubmit={handleSubmit}>
        <div className="login-brand">
          <span className="login-logo" aria-hidden="true">
            <svg viewBox="0 0 48 48" width="44" height="44">
              <circle cx="24" cy="24" r="22" fill="none" stroke="#2f6fed" strokeWidth="2.5" opacity="0.25" />
              <circle cx="24" cy="24" r="14" fill="none" stroke="#2f6fed" strokeWidth="2.5" opacity="0.55" />
              <circle cx="24" cy="24" r="6" fill="#2f6fed" />
            </svg>
          </span>
          <div>
            <div className="login-brand-name">同心圆</div>
            <div className="login-brand-tag">一人公司 · AI 员工工作台</div>
          </div>
        </div>
        <p className="login-sub">登录以进入工作台</p>
        <label className="field">
          <span>账号</span>
          <input
            className="input"
            value={account}
            onChange={(event) => setAccount(event.target.value)}
            autoComplete="username"
            required
          />
        </label>
        <label className="field">
          <span>密码</span>
          <input
            className="input"
            type="password"
            value={password}
            onChange={(event) => setPassword(event.target.value)}
            autoComplete="current-password"
            required
          />
        </label>
        <label className="login-remember">
          <input
            type="checkbox"
            checked={remember}
            onChange={(event) => setRemember(event.target.checked)}
          />
          <span>记住我（在本机保存账号与密码，下次免输入）</span>
        </label>
        {error && <p className="error">{error}</p>}
        <button type="submit" className="btn btn-primary" disabled={loading}>
          {loading ? "登录中…" : "登录"}
        </button>
      </form>
    </div>
  );
}
