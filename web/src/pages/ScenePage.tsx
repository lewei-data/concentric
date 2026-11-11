import { useEffect, useRef, useState } from "react";
import { Link } from "react-router-dom";

type SceneKey = "office" | "meeting";

interface SceneConfig {
  title: string;
  desc: string;
  src: string;
  other: string;
  otherLabel: string;
}

const SCENES: Record<SceneKey, SceneConfig> = {
  office: {
    title: "3D 办公区",
    desc: "员工工位、实时状态与会议指示牌；点击人物查看任务，拖拽旋转视角。",
    src: "/office3d?embed=1",
    other: "/meeting",
    otherLabel: "前往会议室",
  },
  meeting: {
    title: "3D 会议室",
    desc: "选题会实时投影、发言记录与会议纪要索引，数据来自 Obsidian。",
    src: "/meeting3d?embed=1",
    other: "/office",
    otherLabel: "返回办公区",
  },
};

export function ScenePage({ scene }: { scene: SceneKey }) {
  const cfg = SCENES[scene];
  const [nonce, setNonce] = useState(0);
  const [loaded, setLoaded] = useState(false);
  const [timedOut, setTimedOut] = useState(false);
  const timer = useRef<number | undefined>(undefined);

  useEffect(() => {
    setLoaded(false);
    setTimedOut(false);
    timer.current = window.setTimeout(() => setTimedOut(true), 20000);
    return () => window.clearTimeout(timer.current);
  }, [scene, nonce]);

  const handleLoad = () => {
    window.clearTimeout(timer.current);
    setLoaded(true);
    setTimedOut(false);
  };

  const reload = () => setNonce((n) => n + 1);

  return (
    <section className="scene-page">
      <div className="scene-bar">
        <div>
          <h2>{cfg.title}</h2>
          <p>{cfg.desc}</p>
        </div>
        <div className="scene-actions">
          <Link className="btn" to={cfg.other}>{cfg.otherLabel}</Link>
          <button type="button" className="btn btn-ghost" onClick={reload}>
            重新加载
          </button>
          <a
            className="btn btn-ghost"
            href={cfg.src.replace("?embed=1", "")}
            target="_blank"
            rel="noreferrer"
          >
            独立窗口打开
          </a>
        </div>
      </div>
      <div className="scene-frame">
        {!loaded && !timedOut && <div className="scene-loading">正在加载 3D 场景…</div>}
        {timedOut && !loaded && (
          <div className="scene-error">
            <div>
              <p>场景加载超时，请确认已登录且服务正常。</p>
              <button type="button" className="btn" onClick={reload}>
                重试
              </button>
            </div>
          </div>
        )}
        <iframe
          key={`${scene}-${nonce}`}
          src={cfg.src}
          title={cfg.title}
          onLoad={handleLoad}
          allow="fullscreen"
        />
      </div>
    </section>
  );
}
