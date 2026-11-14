"""消息中心 · 三源埋点（Hermes 任务 / 制片工厂 / 大模型调用）。

只做「写会话线程 + 写消息 + 推 SSE」，不参与任何业务判断；异常只打日志，
绝不阻断流水线。线程约定：

- ``hermes:<trace>``  一轮任务一条线程：任务启动 / 关键阶段节点 / 最终成果
- ``factory:<trace>`` 同一轮任务的车间流水线：工位进出 + 每件产品的工序推进
- ``llm:calls``       全局大模型调用账本（每次真实模型调用一条）

消息落 ``chat_messages``，来源分别写 hermes / factory / llm，前端消息中心
按来源筛选；``emit('chat.message')`` 让已打开的消息中心与小秘书面板即时刷新。
"""
from utils import iso_now, uid

# 来源 → (默认会话标题, 参与者)
SOURCE_TITLES = {
    "hermes": ("Hermes · 任务调度", [{"id": "hermes", "name": "Hermes"}]),
    "factory": ("制片工厂 · 车间流水线", [{"id": "factory", "name": "制片工厂"}]),
    "llm": ("大模型 · 调用账本", [{"id": "llm", "name": "大模型"}]),
}

LLM_THREAD = "llm:calls"

FINISH_LABEL = {
    "completed": "已完成",
    "aborted": "已停止（成本熔断）",
    "error": "编排异常",
    "incomplete": "未完成",
    "interrupted": "进程中断",
    "running": "进行中",
}

STEP_LABEL = {"done": "完成", "stopped": "提前收工", "failed": "失败", "running": "开工"}

TASK_SOURCE_LABEL = {"idea": "创意灵感", "hotspot": "热点采集"}


def hermes_thread(trace):
    return f"hermes:{trace}"


def factory_thread(trace):
    return f"factory:{trace}"


def _post(db, emit, source, tid, title, sender_name, content, kind, trace="", meta=None):
    """写一条三源消息；返回是否真正入库（重复消息不重复推送）。"""
    if db is None:
        return False
    text = str(content or "").strip()
    if not text:
        return False
    at = iso_now()
    # chat_messages 唯一键是 (source, origin_table, origin_id)：三源每条消息都要独立 id，
    # 否则同一线程的消息会被 INSERT OR IGNORE 吞掉。
    row = {"thread_id": tid, "source": source, "modality": "text", "direction": "inbound",
           "sender_id": source, "sender_name": sender_name, "content": text[:4000],
           "content_kind": kind, "origin_table": "opc.feed",
           "origin_id": uid("fd"), "trace_id": trace or None,
           "meta": meta or {}, "created_at": at}
    try:
        db.chat_upsert_thread(tid, source, title or SOURCE_TITLES[source][0],
                              SOURCE_TITLES[source][1], at, at)
        ok = db.chat_insert(row)
    except Exception as e:
        print(f"[msgfeed] {source} 写库失败：{type(e).__name__}: {e}", flush=True)
        return False
    if ok and emit:
        try:
            emit("chat.message", trace or "", {"threadId": tid, "source": source,
                                               "message": dict(row)})
        except Exception as e:
            print(f"[msgfeed] {source} 推送失败：{type(e).__name__}: {e}", flush=True)
    return ok


def hermes_start(db, emit, trace, title, origin="", stage="", items=0):
    """任务启动：热点/灵感、入口阶段、在制内容单数。"""
    origin = TASK_SOURCE_LABEL.get(origin, origin or "手动触发")
    body = f"【任务启动】《{title or trace}》\n来源：{origin}"
    if stage:
        body += f"\n入口阶段：{stage}"
    if items:
        body += f"\n在制内容单：{items} 条"
    body += f"\nTrace：{trace}"
    return _post(db, emit, "hermes", hermes_thread(trace), f"Hermes · {title or trace}",
                 "Hermes", body, "task", trace,
                 {"phase": "start", "stage": stage, "origin": origin})


def hermes_node(db, emit, trace, title, stage_name, status="done", items=0):
    """执行过程关键节点：阶段收口（成功/提前收工/失败）与在制数量。"""
    body = (f"【关键节点】{stage_name or '阶段'} → {STEP_LABEL.get(status, status)}"
            f"\n在制内容单：{items} 条\nTrace：{trace}")
    return _post(db, emit, "hermes", hermes_thread(trace), f"Hermes · {title or trace}",
                 "Hermes", body, "stage", trace,
                 {"phase": "stage", "stage": stage_name, "status": status, "items": items})


def hermes_finish(db, emit, trace, title, status, items=0, done=0, files=0, seconds=0):
    """结束后的成果：最终状态、产出数量、成片文件数、耗时。"""
    label = FINISH_LABEL.get(status, status or "未知")
    body = (f"【任务结束】《{title or trace}》\n最终状态：{label}"
            f"\n产出内容单：{items} 条（已发布/归档 {done} 条，成片文件 {files} 个）")
    if seconds:
        body += f"\n总耗时：{int(seconds)} 秒"
    body += f"\nTrace：{trace}"
    return _post(db, emit, "hermes", hermes_thread(trace), f"Hermes · {title or trace}",
                 "Hermes", body, "result", trace,
                 {"phase": "finish", "status": status, "items": items,
                  "done": done, "files": files, "seconds": int(seconds or 0)})


def factory_step(db, emit, trace, title, stage_name, status, products=()):
    """车间流水线动态：本工位开工/收工 + 每件产品在本工位推进的工序。"""
    label = STEP_LABEL.get(status, status)
    lines = [f"【{stage_name or '工位'}】{label}" + (f" · 在制 {len(products)} 件"
                                                    if products else "")]
    for p in products:
        steps = [str(s) for s in (p.get("steps") or []) if str(s).strip()]
        head = f"· 《{p.get('title') or p.get('itemId') or '产品'}》"
        if p.get("workshopNo") or p.get("craftName"):
            head += f"（车间{p.get('workshopNo') or ''} {p.get('craftName') or ''}）"
        if steps:
            head += f" 工序：{ '、'.join(steps) }"
        lines.append(head)
    lines.append(f"Trace：{trace}")
    return _post(db, emit, "factory", factory_thread(trace), f"制片工厂 · {title or trace}",
                 "制片工厂", "\n".join(lines), "workshop", trace,
                 {"stage": stage_name, "status": status, "products": len(products)})


def llm_call(db, emit, trace, agent_id, agent_name, meta, task=""):
    """每次真实模型调用一条：通道/模型、token、花费、时延、成败。"""
    meta = meta or {}
    engine = str(meta.get("engine") or "")
    usage = meta.get("usage") or {}
    model = str(meta.get("model") or usage.get("model") or "—")
    tokens = usage.get("totalTokens") or 0
    cost = float(usage.get("costUsd") or 0)
    latency = int(meta.get("latencyMs") or 0)
    who = agent_name or agent_id or "大模型"
    if meta.get("llmBlocked"):
        head = f"【拦截】{who} · {meta.get('reason') or '通道不可用'}"
    elif engine == "degraded":
        head = f"【失败】{who} · {meta.get('reason') or '未拿到模型回复'}"
    else:
        head = f"【调用】{who} → {model}"
    body = (f"{head}"
            f"\nTokens：{tokens}（入 {usage.get('inputTokens') or 0} / 出 "
            f"{usage.get('outputTokens') or 0}）"
            f"\n花费：${cost:.4f} · 时延：{latency / 1000:.1f}s"
            + (f" · 重试：{meta.get('attempts')} 次" if meta.get("attempts") else ""))
    if meta.get("channel"):
        body += f"\n通道：{meta.get('channel')}"
    if task:
        body += f"\n任务：{task}"
    if trace:
        body += f"\nTrace：{trace}"
    return _post(db, emit, "llm", LLM_THREAD, SOURCE_TITLES["llm"][0],
                 agent_name or agent_id or "大模型", body, "llm", trace,
                 {"engine": engine, "model": model, "tokens": tokens,
                  "costUsd": cost, "latencyMs": latency, "agentId": agent_id,
                  "phase": "blocked" if meta.get("llmBlocked") else "call"})