import { useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";
import { authProvider } from "../authProvider";
import { WuxingLogo } from "../components/WuxingLogo";
import { WUXING, useWuxing } from "../wuxing";

// 「记住我」凭据存放键（明文保存，仅适合本机个人使用）
const REMEMBER_KEY = "opc.remember";

type Remembered = { account: string; password: string };

type Captcha = { id: string; question: string; token: string };

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
  const wx = useWuxing();
  const [mode, setMode] = useState<"login" | "register">("login");
  const [remembered] = useState(loadRemembered);
  const [account, setAccount] = useState(remembered?.account ?? "");
  const [password, setPassword] = useState(remembered?.password ?? "");
  const [remember, setRemember] = useState(!!remembered);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(false);
  const navigate = useNavigate();

  // 注册表单
  const [regPhone, setRegPhone] = useState("");
  const [regPassword, setRegPassword] = useState("");
  const [regPassword2, setRegPassword2] = useState("");
  const [captcha, setCaptcha] = useState<Captcha | null>(null);
  const [captchaAnswer, setCaptchaAnswer] = useState("");
  const [regLoading, setRegLoading] = useState(false);

  const refreshCaptcha = () => {
    setCaptcha(null);
    setCaptchaAnswer("");
    fetch("/api/captcha", { cache: "no-store" })
      .then((r) => r.json())
      .then((d) => setCaptcha(d as Captcha))
      .catch(() => setError("校验码加载失败，请重试"));
  };

  useEffect(() => {
    if (mode === "register") refreshCaptcha();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [mode]);

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

  const handleRegister = async (event: React.FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    setError("");
    if (!/^1[3-9]\d{9}$/.test(regPhone)) {
      setError("手机号格式不正确（11 位，1 开头）");
      return;
    }
    if (regPassword.length < 6) {
      setError("密码至少 6 位");
      return;
    }
    if (regPassword !== regPassword2) {
      setError("两次输入的密码不一致");
      return;
    }
    if (!captcha) {
      setError("校验码未加载，请点击刷新");
      return;
    }
    setRegLoading(true);
    try {
      const res = await fetch("/api/register", {
        method: "POST",
        credentials: "include",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          phone: regPhone,
          password: regPassword,
          captchaToken: captcha.token,
          captcha: captchaAnswer,
        }),
      });
      const body = (await res.json()) as { ok?: boolean; error?: string };
      if (!res.ok || !body.ok) {
        setError(body.error || `注册失败（HTTP ${res.status}）`);
        refreshCaptcha();
        return;
      }
      // 注册即登录：服务端已种会话 Cookie
      try {
        localStorage.removeItem(REMEMBER_KEY);
      } catch {
        /* 忽略 */
      }
      navigate("/");
    } catch {
      setError("网络异常，请稍后重试");
      refreshCaptcha();
    } finally {
      setRegLoading(false);
    }
  };

  return (
    <div className="login-wrap">
      {mode === "login" ? (
        <form className="card login-card" onSubmit={handleSubmit}>
          <div className="login-brand">
            <span className="login-logo" aria-hidden="true">
              <WuxingLogo element={wx.element} size={44} />
            </span>
            <div>
              <div className="login-brand-name">同心圆</div>
              <div className="login-brand-tag">一人公司 · AI 员工工作台</div>
            </div>
          </div>
          <p className="login-sub">登录以进入工作台</p>
          <div className="login-wx-note" title={wx.reading.reason}>
            <i aria-hidden="true" />
            {wx.manual ? "手选预览" : `今日${wx.reading.ganzhi}日`} · {WUXING[wx.element].fullName}{wx.manual ? "" : "宜用"} · {WUXING[wx.element].motto}
          </div>
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
          <p className="login-switch">
            还没有账号？
            <button type="button" className="link" onClick={() => { setMode("register"); setError(""); }}>
              注册新用户
            </button>
            <span className="muted">（手机号注册，注册即获得独立工作空间）</span>
          </p>
        </form>
      ) : (
        <form className="card login-card" onSubmit={handleRegister}>
          <div className="login-brand">
            <span className="login-logo" aria-hidden="true">
              <WuxingLogo element={wx.element} size={44} />
            </span>
            <div>
              <div className="login-brand-name">注册新用户</div>
              <div className="login-brand-tag">手机号注册 · 独立 AI 员工工作空间</div>
            </div>
          </div>
          <label className="field">
            <span>手机号</span>
            <input
              className="input"
              value={regPhone}
              onChange={(event) => setRegPhone(event.target.value)}
              placeholder="11 位手机号"
              autoComplete="tel"
              required
            />
          </label>
          <label className="field">
            <span>密码</span>
            <input
              className="input"
              type="password"
              value={regPassword}
              onChange={(event) => setRegPassword(event.target.value)}
              placeholder="至少 6 位"
              autoComplete="new-password"
              required
            />
          </label>
          <label className="field">
            <span>确认密码</span>
            <input
              className="input"
              type="password"
              value={regPassword2}
              onChange={(event) => setRegPassword2(event.target.value)}
              autoComplete="new-password"
              required
            />
          </label>
          <label className="field">
            <span>校验码</span>
            <span className="login-captcha">
              <input
                className="input"
                value={captchaAnswer}
                onChange={(event) => setCaptchaAnswer(event.target.value)}
                placeholder={captcha ? captcha.question : "加载中…"}
                required
              />
              <button type="button" className="btn btn-ghost" onClick={refreshCaptcha} title="换一题">
                ↻
              </button>
            </span>
          </label>
          {error && <p className="error">{error}</p>}
          <button type="submit" className="btn btn-primary" disabled={regLoading}>
            {regLoading ? "注册中…" : "注册并进入工作台"}
          </button>
          <p className="login-switch">
            已有账号？
            <button type="button" className="link" onClick={() => { setMode("login"); setError(""); }}>
              返回登录
            </button>
          </p>
        </form>
      )}
    </div>
  );
}
