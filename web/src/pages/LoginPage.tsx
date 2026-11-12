import { useState } from "react";
import { useNavigate } from "react-router-dom";
import { authProvider } from "../authProvider";

export function LoginPage() {
  const [account, setAccount] = useState("");
  const [password, setPassword] = useState("");
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
        {error && <p className="error">{error}</p>}
        <button type="submit" className="btn btn-primary" disabled={loading}>
          {loading ? "登录中…" : "登录"}
        </button>
      </form>
    </div>
  );
}
