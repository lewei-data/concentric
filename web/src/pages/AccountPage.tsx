import { useEffect, useRef, useState } from "react";

// 用户中心：基本信息 / 参数设置 / 数据资产（Obsidian 工作区打包导出与导入）/ 帮助

type AssetItem = {
  key: string;
  label: string;
  desc: string;
  files: number;
  bytes: number;
  included: boolean;
};
type AccountResp = {
  ok: boolean;
  user?: { phone: string; name: string; role: string; tenant: string; createdAt: string };
  stats?: { agents: number; assetFiles: number; assetBytes: number };
  assets?: AssetItem[];
  settings?: Settings;
};
type Settings = { sound: boolean; autoRefreshSec: number; signature: string };

function fmtBytes(n?: number): string {
  const v = Math.max(0, n || 0);
  if (v >= 1024 ** 3) return `${(v / 1024 ** 3).toFixed(2)} GB`;
  if (v >= 1024 ** 2) return `${(v / 1024 ** 2).toFixed(1)} MB`;
  if (v >= 1024) return `${(v / 1024).toFixed(0)} KB`;
  return `${v} B`;
}

async function getJson<T>(url: string): Promise<T> {
  const res = await fetch(url, { credentials: "include" });
  if (res.status === 401) {
    window.location.assign("/app/login");
    throw new Error("unauthorized");
  }
  return (await res.json()) as T;
}

export function AccountPage() {
  const [data, setData] = useState<AccountResp | null>(null);
  const [settings, setSettings] = useState<Settings>({ sound: true, autoRefreshSec: 15, signature: "" });
  const [msg, setMsg] = useState("");
  const [err, setErr] = useState("");
  const [busy, setBusy] = useState("");
  const fileRef = useRef<HTMLInputElement>(null);

  const load = async () => {
    try {
      const d = await getJson<AccountResp>("/api/account");
      setData(d);
      if (d.settings) setSettings(d.settings);
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
    }
  };

  useEffect(() => {
    void load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const saveSettings = async (patch: Partial<Settings>) => {
    const next = { ...settings, ...patch };
    setSettings(next);
    setBusy("settings");
    setErr("");
    try {
      const res = await fetch("/api/account/settings", {
        method: "POST",
        credentials: "include",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(patch),
      });
      const body = (await res.json()) as { ok?: boolean; error?: string; settings?: Settings };
      if (!res.ok || !body.ok) throw new Error(body.error || `HTTP ${res.status}`);
      if (body.settings) setSettings(body.settings);
      setMsg("设置已保存");
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy("");
    }
  };

  const exportZip = async () => {
    if (!window.confirm("打包并下载全部个人数据（过程库 / 素材 / 知识库 / 名册）？")) return;
    setBusy("export");
    setErr("");
    try {
      const res = await fetch("/api/account/export", { credentials: "include" });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const blob = await res.blob();
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      const cd = res.headers.get("Content-Disposition") || "";
      const m = /filename\*=UTF-8''([^;]+)/.exec(cd);
      a.href = url;
      a.download = m ? decodeURIComponent(m[1]) : "opc-export.zip";
      a.click();
      URL.revokeObjectURL(url);
      setMsg("数据包已开始下载");
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy("");
    }
  };

  const importZip = async (file: File) => {
    if (file.size > 512 * 1024 * 1024) {
      setErr("压缩包超过 512MB 上限");
      return;
    }
    if (!window.confirm(
      `确认导入「${file.name}」？\n包内的过程库 / 素材 / 知识库 / 名册将替换你当前的对应数据（未包含在包内的数据保留）。此操作不可撤销。`,
    )) {
      if (fileRef.current) fileRef.current.value = "";
      return;
    }
    setBusy("import");
    setErr("");
    try {
      const res = await fetch("/api/account/import", {
        method: "POST",
        credentials: "include",
        body: file,
      });
      const body = (await res.json()) as { ok?: boolean; error?: string; imported?: string[] };
      if (!res.ok || !body.ok) throw new Error(body.error || `HTTP ${res.status}`);
      setMsg(`已导入 ${body.imported?.length ?? 0} 项：${(body.imported || []).join("、")}`);
      await load();
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy("");
      if (fileRef.current) fileRef.current.value = "";
    }
  };

  const u = data?.user;
  const stats = data?.stats;

  return (
    <div className="panel-grid single">
      <div className="card">
        <h2>基本信息</h2>
        <div className="account-grid">
          <div className="account-item"><small>手机号</small><b>{u?.phone || "—"}</b></div>
          <div className="account-item"><small>角色</small><b>{u?.role || "—"}
            {u?.tenant === "self" && <span className="muted">（独立工作空间）</span>}
            {u?.tenant === "owner" && <span className="muted">（存量公司空间）</span>}
          </b></div>
          <div className="account-item"><small>注册时间</small><b>{u?.createdAt?.slice(0, 19).replace("T", " ") || "存量账号"}</b></div>
          <div className="account-item"><small>AI 员工</small><b>{stats?.agents ?? "—"} 名</b></div>
          <div className="account-item"><small>数据资产</small><b>{fmtBytes(stats?.assetBytes)} / {stats?.assetFiles ?? 0} 个文件</b></div>
        </div>
      </div>

      <div className="card">
        <h2>数据资产清单 <small className="muted">（一键导出为 zip 下载到本地）</small></h2>
        <div className="table-wrap">
          <table className="table">
            <thead>
              <tr><th>资产</th><th>说明</th><th>文件数</th><th>大小</th><th>导出</th></tr>
            </thead>
            <tbody>
              {(data?.assets || []).map((a) => (
                <tr key={a.key}>
                  <td><b>{a.label}</b><small className="muted">（{a.key}）</small></td>
                  <td className="muted">{a.desc}</td>
                  <td>{a.files}</td>
                  <td>{fmtBytes(a.bytes)}</td>
                  <td><span className="muted">包含</span></td>
                </tr>
              ))}
              {!data?.assets?.length && (
                <tr><td colSpan={5} className="muted">暂无资产数据。</td></tr>
              )}
            </tbody>
          </table>
        </div>
        <div className="account-actions">
          <button type="button" className="btn btn-primary" disabled={busy === "export"} onClick={() => void exportZip()}>
            {busy === "export" ? "打包中…" : "一键导出（zip 下载）"}
          </button>
          <input
            ref={fileRef}
            type="file"
            accept=".zip"
            style={{ display: "none" }}
            onChange={(e) => {
              const f = e.target.files?.[0];
              if (f) void importZip(f);
            }}
          />
          <button
            type="button"
            className="btn btn-ghost"
            disabled={busy === "import"}
            onClick={() => fileRef.current?.click()}
          >
            {busy === "import" ? "导入中…" : "上传 zip 恢复个人数据"}
          </button>
          <span className="muted">导入会用包内数据替换同名资产（obsidian 工作区 / 过程库 / 素材），请先用导出包备份。</span>
        </div>
      </div>

      <div className="card">
        <h2>参数设置</h2>
        <div className="account-settings">
          <label className="account-row">
            <span>工位悬停提示音</span>
            <input
              type="checkbox"
              checked={settings.sound}
              disabled={busy === "settings"}
              onChange={(e) => void saveSettings({ sound: e.target.checked })}
            />
          </label>
          <label className="account-row">
            <span>监控自动刷新间隔</span>
            <select
              value={settings.autoRefreshSec}
              disabled={busy === "settings"}
              onChange={(e) => void saveSettings({ autoRefreshSec: Number(e.target.value) })}
            >
              {[5, 10, 15, 30, 60, 120].map((s) => (
                <option key={s} value={s}>{s} 秒</option>
              ))}
            </select>
          </label>
          <label className="account-row">
            <span>个人签名（显示在汇报署名）</span>
            <input
              className="input"
              value={settings.signature}
              maxLength={50}
              disabled={busy === "settings"}
              onChange={(e) => setSettings({ ...settings, signature: e.target.value })}
              onBlur={(e) => void saveSettings({ signature: e.target.value })}
            />
          </label>
        </div>
      </div>

      <div className="card">
        <h2>帮助</h2>
        <details>
          <summary>我的数据存在哪里？</summary>
          <p className="muted">
            注册用户拥有独立工作空间（以手机号命名的目录）：过程数据库（opc.db）、素材、
            AI 员工名册、知识库 / 热点采集 / 交付物 / 会议纪要 / 邮件、运行时设置全部隔离存放，
            与其他用户互不可见。
          </p>
        </details>
        <details>
          <summary>导出的 zip 里有什么？</summary>
          <p className="muted">
            全部个人数据资产：过程数据库（sqlite 一致性快照）、素材清单与素材文件、
            知识库与工作文档（vault）、AI 员工名册、用户设置与日志，并附带 manifest.json 清单。
            zip 可长期归档，也可在其他环境导入恢复。
          </p>
        </details>
        <details>
          <summary>导入会覆盖哪些数据？</summary>
          <p className="muted">
            导入按"同名替换"执行：包内包含的资产（过程库 / 素材 / vault / 名册等）会替换当前数据，
            包内没有的资产（如新产生的日志）保留。导入完成后工作台数据在下次刷新时按新数据重建，
            建议导入前先"一键导出"当前数据作为备份。
          </p>
        </details>
        <details>
          <summary>忘记密码怎么办？</summary>
          <p className="muted">
            请联系系统管理员重置（管理员可删除注册记录后重新注册，或联系老板在登录凭据中恢复）。
          </p>
        </details>
      </div>

      {(msg || err) && (
        <p className={err ? "error" : "muted"}>{err || msg}</p>
      )}
    </div>
  );
}
