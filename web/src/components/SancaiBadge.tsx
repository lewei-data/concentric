/** 三才徽标与筛选条：知识库 / 素材 / 热点 / 作品 / 处理过程 共用一套视觉。 */
import type { CSSProperties } from "react";
import { SANCAI, SANCAI_MAP, sancaiKey, type SanCai } from "../sancai";

export function SancaiBadge({
  sancai, size = "sm", showVerb = false, fallback = "ren",
}: {
  sancai?: string | null;
  size?: "sm" | "md";
  showVerb?: boolean;
  fallback?: SanCai;
}) {
  const key = (sancaiKey(sancai) || fallback) as SanCai;
  const def = SANCAI_MAP[key];
  return (
    <span
      className={`sancai-badge sancai-badge--${size}`}
      style={{ "--sc": def.color } as CSSProperties}
      title={`${def.label} · ${def.slogan}｜${def.desc}`}
    >
      <i aria-hidden="true">{def.label}</i>
      {showVerb && <em>{def.verb}</em>}
    </span>
  );
}

export function SancaiChips({
  value, onChange, counts, lead = "才位", compact = false,
}: {
  value: string;
  onChange: (next: string) => void;
  counts?: Record<string, number>;
  lead?: string;
  compact?: boolean;
}) {
  return (
    <div className={`sancai-row${compact ? " sancai-row--compact" : ""}`} role="group" aria-label="三才筛选">
      {lead && <span className="sancai-lead">{lead}</span>}
      {SANCAI.map((def) => {
        const active = value === def.key;
        const n = counts?.[def.key];
        return (
          <button
            key={def.key}
            type="button"
            className={`sancai-chip${active ? " is-active" : ""}`}
            style={{ "--sc": def.color } as CSSProperties}
            aria-pressed={active}
            title={`${def.label} · ${def.slogan}｜${def.desc}（再点一次取消）`}
            onClick={() => onChange(active ? "" : def.key)}
          >
            <b>{def.label}</b>
            <span>{def.verb}</span>
            {typeof n === "number" && <i>{n}</i>}
          </button>
        );
      })}
      {value && (
        <button type="button" className="sancai-clear" onClick={() => onChange("")}>清除 ×</button>
      )}
    </div>
  );
}