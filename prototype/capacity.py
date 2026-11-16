"""产能均衡口径（CEO 产能调度）：各产品组成品数与缺口。

老板 2026-11-16 授权：CEO 在「每个产品组产出 >= 2」的前提下合理安排产能。
成品口径 = contents.stage in ('approved','published')（过审 / 已发布），逐项目计数；
缺口由大到小排序，派单与资源调度先补缺口最大的组。

环境变量：
    OPC_CAPACITY_TARGET  每组目标成品数，默认 2
"""
import os

from opc_paths import TZ  # noqa: F401 —— 保持与其它模块一致的运行时常量来源

TARGET = int(os.environ.get("OPC_CAPACITY_TARGET", "2"))
DONE_STAGES = ("approved", "published")


def output_by_project(kn, stages=DONE_STAGES):
    """各项目已过审成品的条数（读过程库；无库时返回空表）。"""
    if not kn.db:
        return {}
    marks = ",".join("?" for _ in stages)
    try:
        rows = kn.db.fetch(
            f"SELECT project_id AS pid, COUNT(*) AS n FROM contents "
            f"WHERE stage IN ({marks}) GROUP BY project_id", tuple(stages))
    except Exception as e:
        print(f"[capacity] 统计失败：{type(e).__name__}: {e}", flush=True)
        return {}
    # db.fetch 返回 dict 列表（不是 tuple），逐行取值避免解包错位
    return {r["pid"]: int(r["n"] or 0) for r in (rows or []) if r.get("pid")}


def snapshot(kn, target=None):
    """产能快照：每项目 output/target/gap，按缺口降序（缺口大的排前面）。"""
    target = TARGET if target is None else int(target)
    out = output_by_project(kn)
    projects = []
    for pid, meta in (kn.projects or {}).items():
        n = out.get(pid, 0)
        projects.append({"id": pid, "name": (meta or {}).get("name", pid),
                         "output": n, "target": target, "gap": max(0, target - n)})
    projects.sort(key=lambda p: (-p["gap"], p["id"]))
    gaps = [p for p in projects if p["gap"] > 0]
    return {
        "target": target,
        "projects": projects,
        "gaps": gaps,
        "totalOutput": sum(p["output"] for p in projects),
        "balanced": not gaps,
        "summary": "；".join(
            f"{p['name']}={p['output']}/{target}" + (f"(缺{p['gap']})" if p["gap"] else "")
            for p in projects),
    }


def facts(kn):
    """给 CEO 的一句话事实（派单 / 代审批时注入上下文）。"""
    try:
        s = snapshot(kn)
    except Exception as e:                                   # 统计失败不能拖垮派单
        print(f"[capacity] 快照失败：{type(e).__name__}: {e}", flush=True)
        return "产能：暂无数据"
    return f"产能（成品口径 approved/published，每组目标 {s['target']}）：{s['summary']}"


def plan_order(kn):
    """派单顺序：缺口大的项目优先；缺口为 0 的排后面。"""
    try:
        return [p["id"] for p in snapshot(kn)["projects"]]
    except Exception:
        return list((kn.projects or {}).keys())