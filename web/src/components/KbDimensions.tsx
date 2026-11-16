/** 知识库分类维度面板：五行（5 行固定全展示）在上，三才（天地人固定维度）在下，两行同宽对齐。
 *
 * 面板底部把「8 显性 / 7 隐性」翻译成人话：选中 行+才 就告诉你在哪个显性节点或哪条隐性工序上。
 */
import type { CSSProperties } from "react";
import { WUXING, type WuXing } from "../wuxing";
import { WuxingLogo } from "./WuxingLogo";
import {
  ELEMENT_LABEL, SANCAI, elementOf, xianNodesOf, yinStepOf, type SanCai,
} from "../sancai";

type Cat = {
  key: string;
  label: string;
  element: string;
  slogan: string;
  count?: number;
};

const CN_TO_WX: Record<string, WuXing> = {
  木: "wood", 火: "fire", 土: "earth", 金: "metal", 水: "water",
};
const wxOf = (el: string): WuXing => CN_TO_WX[el] ?? "water";

export function KbDimensions({
  categories,
  activeCat,
  activeSancai,
  catCounts,
  sancaiCounts,
  openQuestions,
  todayElement,
  onPickCat,
  onPickSancai,
  onQuestions,
}: {
  categories: Cat[];
  activeCat: string;
  activeSancai: string;
  catCounts: Record<string, number>;
  sancaiCounts: Record<string, number>;
  openQuestions: number;
  todayElement?: WuXing;
  onPickCat: (key: string, element?: string) => void;
  onPickSancai: (next: string) => void;
  onQuestions: () => void;
}) {
  const activeCategory = categories.find((c) => c.key === activeCat);
  const el = elementOf(activeCategory?.element);
  const sc = (activeSancai || "") as SanCai | "";
  const scDef = SANCAI.find((s) => s.key === sc) ?? null;
  const nodes = xianNodesOf(el);
  const yin = yinStepOf(el, sc);

  let noteTag = "口径";
  let noteTone = "";
  let noteText = "8 显性 = 5 行 + 3 才（人机协同锚点，人做验收与边界）；7 隐性 = 行×才 的输入端 / 处理端 / 输出端，封装成内部流转型 Skill，可观测、可干预、可版本化。";
  let noteSkills = "";

  if (el && sc && yin) {
    noteTag = "隐性环节";
    noteTone = "is-yin";
    noteText = `${ELEMENT_LABEL[el]} × ${scDef?.label} · ${yin.stage}｜${yin.name}（衔接 ${yin.bridge}）：自动化流转，异常才推给显性节点人工处理。`;
    noteSkills = yin.skills.join(" / ");
  } else if (el && sc) {
    noteTag = "空位";
    noteTone = "is-gap";
    noteText = `${ELEMENT_LABEL[el]} × ${scDef?.label}：这一格没有独立的内部工序，由 ${nodes.map((n) => n.symbol).join(" / ") || "该行节点"} 直接承担。`;
  } else if (el) {
    noteTag = "显性节点";
    noteTone = "is-xian";
    noteText = nodes.length === 2
      ? `${ELEMENT_LABEL[el]}行 · ${nodes.map((n) => `${n.symbol}·${n.name}（${n.position}）`).join(" / ")}：${nodes[0].role}`
      : `${ELEMENT_LABEL[el]}行 · ${nodes.map((n) => `${n.symbol}·${n.name}`).join(" / ")}：${nodes[0]?.role ?? ""}`;
  } else if (sc && scDef) {
    noteTag = "三才";
    noteTone = "is-sc";
    noteText = `${scDef.label} · ${scDef.verb}（${scDef.slogan}）：${scDef.desc}。再点一次取消，选行可叠加。`;
  }

  return (
    <section className="kb-dim" aria-label="知识库分类维度">
      <div className="kb-dim-head">
        <span className="kb-dim-title">分类维度</span>
        <span className="kb-dim-tip">五行 5 × 三才 3 = 15 格，其中 8 显性、7 隐性</span>
        <button
          type="button"
          className={`kb-dim-extra${activeCat === "questions" ? " is-active" : ""}`}
          onClick={onQuestions}
          title="待解决 · 已提出但还没有知识来认领的问题"
        >
          待解决 <b>{openQuestions}</b>
        </button>
      </div>

      {/* 五行：固定 5 行，全部展示，等宽平铺 */}
      <div className="kb-dim-row">
        <span className="kb-dim-axis">五行<b>5 行</b></span>
        <div className="kb-dim-grid kb-dim-grid--5">
          {categories.map((c) => {
            const w = wxOf(c.element);
            const theme = WUXING[w];
            const isActive = activeCat === c.key;
            const rowNodes = xianNodesOf(elementOf(c.element));
            return (
              <button
                key={c.key}
                type="button"
                className={`kb-dim-cell${isActive ? " is-active" : ""}${w === todayElement ? " is-today" : ""}`}
                style={{ "--c": theme.color } as CSSProperties}
                onClick={() => onPickCat(c.key, c.element)}
                title={`${c.label}（${c.element}）· ${theme.classic}`}
              >
                <span className="kb-dim-cell-logo"><WuxingLogo element={w} size={34} /></span>
                <span className="kb-dim-cell-body">
                  <span className="kb-dim-cell-label">
                    <em className="kb-dim-cell-el">{c.element}</em>{c.label}
                  </span>
                  <span className="kb-dim-cell-slogan">{c.slogan}</span>
                  <span className="kb-dim-cell-nodes">
                    {rowNodes.map((n) => n.symbol).join(" · ") || "—"}
                  </span>
                </span>
                <span className="kb-dim-cell-count">{catCounts[c.key] ?? c.count ?? 0}</span>
                {w === todayElement && <span className="kb-dim-today">当令</span>}
              </button>
            );
          })}
        </div>
      </div>

      {/* 三才：固定维度，落在五行正下方，左右边缘与五行行对齐 */}
      <div className="kb-dim-row">
        <span className="kb-dim-axis kb-dim-axis--sc">三才<b>3 才</b></span>
        <div className="kb-dim-grid kb-dim-grid--3">
          {SANCAI.map((def) => {
            const isActive = sc === def.key;
            return (
              <button
                key={def.key}
                type="button"
                className={`kb-dim-cell kb-dim-sancai${isActive ? " is-active" : ""}`}
                style={{ "--c": def.color } as CSSProperties}
                onClick={() => onPickSancai(isActive ? "" : def.key)}
                title={`${def.label} · ${def.slogan}｜${def.desc}（再点一次取消）`}
              >
                <span className="kb-dim-cell-body">
                  <span className="kb-dim-cell-label">
                    {def.label}
                    <em className="kb-dim-cell-verb">{def.verb}</em>
                  </span>
                  <span className="kb-dim-cell-desc">{def.desc}</span>
                </span>
                <span className="kb-dim-cell-count">{sancaiCounts[def.key] ?? 0}</span>
              </button>
            );
          })}
        </div>
      </div>

      <p className={`kb-dim-note ${noteTone}`}>
        <span className="kb-dim-note-tag">{noteTag}</span>
        <span className="kb-dim-note-text">{noteText}</span>
        {noteSkills && <span className="kb-dim-skills">Skill：{noteSkills}</span>}
      </p>
    </section>
  );
}