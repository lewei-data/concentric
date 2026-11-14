import { useEffect, useRef, useState } from "react";
import {
  WUXING,
  WUXING_ORDER,
  setManualWuXing,
  useWuxing,
  type WuXing,
} from "../wuxing";

/** 五行圆徽：五行各自一张不同的图（芽 / 焰 / 山 / 星 / 澜），外环呼应「同心圆」 */
export function WuxingLogo({
  element,
  size = 40,
  className = "",
}: {
  element: WuXing;
  size?: number;
  className?: string;
}) {
  const t = WUXING[element];
  const gid = `wx-grad-${element}`;
  return (
    <svg
      className={`wx-logo ${className}`.trim()}
      viewBox="0 0 48 48"
      width={size}
      height={size}
      role="img"
      aria-label={`${t.fullName}徽记 · ${t.motto}`}
    >
      <defs>
        <linearGradient id={gid} x1="0" y1="0" x2="1" y2="1">
          <stop offset="0%" stopColor={t.color} />
          <stop offset="100%" stopColor={t.color2} />
        </linearGradient>
      </defs>
      <circle cx="24" cy="24" r="23" fill={`url(#${gid})`} />
      <circle cx="24" cy="24" r="20" fill="none" stroke="#ffffff" strokeOpacity=".35" strokeWidth="1" />
      <g dangerouslySetInnerHTML={{ __html: t.mark }} />
    </svg>
  );
}

/** 五行字标：顶栏品牌区文字，仅显示行名 + 格言 */
export function WuxingWordmark({ element }: { element: WuXing }) {
  const t = WUXING[element];
  return (
    <svg className="wx-wordmark" viewBox="0 0 86 34" role="img" aria-label={`${t.fullName} · ${t.motto}`}>
      <text x="2" y="24" fontSize="21" fontWeight="700" fill="currentColor">{t.name}</text>
      <text x="28" y="22" fontSize="11.5" fontWeight="600" letterSpacing="1.1" fill="currentColor" opacity=".78">
        {t.motto}
      </text>
      <path d="M2 29.5h52" stroke="currentColor" strokeWidth="1" opacity=".22" />
    </svg>
  );
}

/** 八卦图：八 trigram 绕圆心排列，外环缓转；顶栏空间不够时替代文字徽记 */
export function BaguaOrb({ element, size = 32 }: { element: WuXing; size?: number }) {
  const t = WUXING[element];
  const gid = `bagua-g-${element}`;
  // 先天八卦序（乾兑离震巽坎艮坤），每卦三线由下而上
  const trigrams: boolean[][] = [
    [true,true,true],   // 乾 ☰
    [true,true,false],  // 兑 ☱
    [true,false,true],  // 离 ☲
    [false,false,true], // 震 ☳
    [false,true,true],  // 巽 ☴
    [false,true,false], // 坎 ☵
    [true,false,false], // 艮 ☶
    [false,false,false],// 坤 ☷
  ];
  const R = 21, lr = 2.2, ll = 4.8, lg = 1.4;
  return (
    <svg className="wx-bagua" viewBox="0 0 48 48" width={size} height={size}
      role="img" aria-label={`${t.fullName}八卦`}>
      <defs>
        <linearGradient id={gid} x1="0" y1="0" x2="1" y2="1">
          <stop offset="0%" stopColor={t.color} />
          <stop offset="100%" stopColor={t.color2} />
        </linearGradient>
      </defs>
      <circle cx="24" cy="24" r="23" fill="none" stroke="currentColor" strokeOpacity=".25" strokeWidth=".6" />
      <g className="wx-bagua-spin">
        {trigrams.map((lines, i) => {
          const a = i * 45 * Math.PI / 180;
          const cx = 24 + R * Math.sin(a), cy = 24 - R * Math.cos(a);
          return (
            <g key={i} transform={`rotate(${i * 45} 24 24)`}>
              {lines.map((solid, j) => {
                const y = 19.2 + j * (ll + lg);
                return solid
                  ? <rect x="20.8" y={y} width="6.4" height={ll} rx=".7" fill={`url(#${gid})`} />
                  : <g>
                      <rect x="20.8" y={y} width="2.6" height={ll} rx=".6" fill={`url(#${gid})`} />
                      <rect x="24.6" y={y} width="2.6" height={ll} rx=".6" fill={`url(#${gid})`} />
                    </g>;
              })}
            </g>
          );
        })}
      </g>
      <circle cx="24" cy="24" r="8.5" fill={`url(#${gid})`} />
      <path d="M24 15.5A8.5 8.5 0 0 1 24 32.5A4.25 4.25 0 0 1 24 24A4.25 4.25 0 0 0 24 15.5Z"
        fill="rgba(255,255,255,.88)" />
      <circle cx="24" cy="19.75" r="1.3" fill={`url(#${gid})`} />
      <circle cx="24" cy="28.25" r="1.3" fill="rgba(255,255,255,.88)" />
      <circle cx="24" cy="24" r="8.5" fill="none" stroke="rgba(255,255,255,.35)" strokeWidth=".5" />
    </svg>
  );
}

/** 顶栏「今日气运」徽记：展示择行依据，并可预览 / 锁定五行主题 */
export function WuxingBadge() {
  const { element, manual, reading } = useWuxing();
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement>(null);
  const t = WUXING[element];
  const peak = Math.max(1, ...WUXING_ORDER.map((k) => reading.scores[k]));

  useEffect(() => {
    if (!open) return;
    const onDown = (e: MouseEvent) => {
      if (ref.current && !ref.current.contains(e.target as Node)) setOpen(false);
    };
    const onKey = (e: KeyboardEvent) => { if (e.key === "Escape") setOpen(false); };
    document.addEventListener("mousedown", onDown);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onDown);
      document.removeEventListener("keydown", onKey);
    };
  }, [open]);

  return (
    <div className="wx-badge" ref={ref}>
      <button
        type="button"
        className="wx-badge-btn"
        aria-haspopup="dialog"
        aria-expanded={open}
        title="查看今日五行气运 / 切换主题"
        onClick={() => setOpen((v) => !v)}
      >
        <span className="wx-badge-bagua"><BaguaOrb element={element} size={28} /></span>
        <span className="wx-badge-text">
          <b>{manual ? "手选" : "今日"} · {t.name}</b>
          <small>{reading.ganzhi}日 · {reading.hourLabel}</small>
        </span>
      </button>

      {open && (
        <div className="wx-panel" role="dialog" aria-label="今日五行气运">
          <div className="wx-panel-head">
            <span className="wx-panel-title">今日五行气运</span>
            <span className="wx-panel-date">{reading.dateKey} · {reading.ganzhi}日</span>
          </div>

          <div className="wx-panel-hero">
            <WuxingLogo element={element} size={54} />
            <div>
              <div className="wx-hero-name">{t.fullName} · {manual ? "手选预览" : "今日宜用"}</div>
              <div className="wx-hero-motto">{t.classic} ｜ {t.direction} · {t.season}</div>
              <div className="wx-hero-verse">{t.verse}</div>
            </div>
          </div>

          <p className="wx-panel-reason">{reading.reason}</p>
          <div className="wx-panel-advice">{reading.advice}<br />此刻时气：{reading.hourLabel}（{WUXING[reading.hourElement].name}）</div>

          <div className="wx-panel-label">五行气数 · 参日干支与节气月令，日日推算</div>
          {WUXING_ORDER.map((k) => {
            const theme = WUXING[k];
            const score = reading.scores[k];
            return (
              <div className={`wx-score ${k === reading.element ? "is-active" : ""}`} key={k}>
                <span>{theme.name}</span>
                <span className="wx-score-track">
                  <i
                    className="wx-score-fill"
                    style={{ width: `${Math.round((score / peak) * 100)}%`, background: `linear-gradient(90deg, ${theme.color}, ${theme.color2})` }}
                  />
                </span>
                <span className="wx-score-num">{score}</span>
              </div>
            );
          })}

          <div className="wx-panel-label">预览 / 锁定其它行</div>
          <div className="wx-switch">
            {WUXING_ORDER.map((k) => (
              <button
                type="button"
                key={k}
                className={k === element ? "is-active" : ""}
                title={`${WUXING[k].fullName} · ${WUXING[k].motto}`}
                onClick={() => setManualWuXing(k)}
              >
                <WuxingLogo element={k} size={26} />
                {WUXING[k].name}
              </button>
            ))}
          </div>
          <button
            type="button"
            className={`wx-auto-btn ${manual ? "" : "is-auto"}`}
            onClick={() => setManualWuXing(null)}
          >
            {manual ? `回到今日气运（${WUXING[reading.element].fullName}）` : "正随今日气运自动换令"}
          </button>
        </div>
      )}
    </div>
  );
}
