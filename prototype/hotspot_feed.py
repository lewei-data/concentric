#!/usr/bin/env python3
"""共享热点消息流：系统定时采集，全部用户共享（公用区域）。

源与方向：
  hn        Hacker News 热榜   （真实采集：Algolia 公开 API）
  github    GitHub 趋势仓库    （真实采集：GitHub Search API，按 star 排序）
  douyin    抖音热榜           （暂无公开通道，源已登记、待接入）
  kuaishou  快手热榜           （同上）

数据落全局过程库 hotspot_feed 表（去重：source+url），与各用户的工作区无关。
"""

import json
import os
import sqlite3
import time
from datetime import datetime, timedelta
from urllib.request import urlopen, Request

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.environ.get("OPC_DB_PATH", os.path.join(BASE_DIR, "opc.db"))
UA = "Mozilla/5.0 (compatible; OPC-Workbench-Feed/1.0)"

SOURCES = [
    {"id": "hn", "name": "Hacker News 热榜", "category": "科技资讯", "live": True,
     "desc": "全球科技创业与工程社区的热门讨论，偏 AI / 编程 / 创业 / 产品",
     "scope": "HN 首页热榜前 30 条（Algolia 公开 API，按热度分排序）",
     "interval": "30 分钟"},
    {"id": "github", "name": "GitHub 趋势仓库", "category": "开源项目", "live": True,
     "desc": "全球开源社区新晋热门项目，适合选题参考与技术选型",
     "scope": "近 7 天新建仓库按 star 排序前 25 条（GitHub Search API）",
     "interval": "30 分钟"},
    {"id": "douyin", "name": "抖音热榜", "category": "短视频", "live": False,
     "desc": "全民短视频话题榜，偏社会热点 / 娱乐 / 民生",
     "scope": "抖音站内热搜榜（Top 50）",
     "interval": "待接入（规划 30 分钟）",
     "note": "无公开 API，待接入抓取通道"},
    {"id": "kuaishou", "name": "快手热榜", "category": "短视频", "live": False,
     "desc": "快手站内全民话题榜，偏民生与下沉市场",
     "scope": "快手站内热搜榜（Top 50）",
     "interval": "待接入（规划 30 分钟）",
     "note": "无公开 API，待接入抓取通道"},
]
SOURCES_BY_ID = {s["id"]: s for s in SOURCES}

# 分类说明：订阅页分类表里每个大类的定位说明
CATEGORY_DESC = {
    "科技资讯": "科技圈在讨论什么：趋势、观点、产品动态",
    "开源项目": "全球开源社区的新项目与上升势头",
    "短视频": "短视频平台的全民话题与大众情绪",
}


def _conn():
    con = sqlite3.connect(DB_PATH, timeout=10)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("""CREATE TABLE IF NOT EXISTS hotspot_feed (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        source       TEXT NOT NULL,
        title        TEXT NOT NULL,
        url          TEXT,
        rank         INTEGER,
        score        INTEGER,
        extra        TEXT,
        collected_at TEXT,
        UNIQUE(source, url)
    )""")
    con.execute("CREATE INDEX IF NOT EXISTS idx_feed_src ON hotspot_feed(source, collected_at DESC)")
    con.execute("""CREATE TABLE IF NOT EXISTS hotspot_feed_state (
        source    TEXT PRIMARY KEY,
        last_run  TEXT,
        ok        INTEGER NOT NULL DEFAULT 0,
        fetched   INTEGER NOT NULL DEFAULT 0,
        message   TEXT
    )""")
    con.commit()
    return con


def _fetch_json(url, timeout=15):
    req = Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
    with urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read(2_000_000).decode("utf-8", "replace"))


def fetch_hn():
    """HN 首页热榜（Algolia API，按 points 排序取前 30）。"""
    data = _fetch_json("https://hn.algolia.com/api/v1/search?tags=front_page&hitsPerPage=30")
    hits = sorted(data.get("hits") or [], key=lambda h: h.get("points") or 0, reverse=True)[:30]
    items = []
    for rank, h in enumerate(hits, 1):
        items.append({
            "source": "hn",
            "title": (h.get("title") or "").strip(),
            "url": h.get("url") or f"https://news.ycombinator.com/item?id={h.get('objectID')}",
            "rank": rank,
            "score": int(h.get("points") or 0),
            "extra": {"author": h.get("author"), "comments": h.get("num_comments")},
        })
    return [i for i in items if i["title"]]


def fetch_github():
    """GitHub 近 7 天新建仓库按 star 排序（Search API，未认证限流 60/h，30 分钟一轮足够）。"""
    since = (datetime.utcnow() - timedelta(days=7)).strftime("%Y-%m-%d")
    url = ("https://api.github.com/search/repositories"
           f"?q=created:>{since}&sort=stars&order=desc&per_page=25")
    data = _fetch_json(url)
    items = []
    for rank, repo in enumerate(data.get("items") or [], 1):
        items.append({
            "source": "github",
            "title": f"{repo.get('full_name')}：{(repo.get('description') or '')[:120]}".strip("："),
            "url": repo.get("html_url") or "",
            "rank": rank,
            "score": int(repo.get("stargazers_count") or 0),
            "extra": {"stars": repo.get("stargazers_count"),
                      "language": repo.get("language"), "owner": (repo.get("owner") or {}).get("login")},
        })
    return [i for i in items if i["title"]]


FETCHERS = {"hn": fetch_hn, "github": fetch_github}


def collect_one(source_id):
    """采集单个源并落库；返回 (条数, 错误)。"""
    fetcher = FETCHERS.get(source_id)
    now = datetime.now().isoformat(timespec="seconds")
    con = _conn()
    try:
        if not fetcher:
            con.execute("INSERT INTO hotspot_feed_state(source,last_run,ok,fetched,message) "
                        "VALUES(?,?,0,0,?) ON CONFLICT(source) DO UPDATE SET last_run=excluded.last_run,"
                        "ok=excluded.ok,fetched=excluded.fetched,message=excluded.message",
                        (source_id, now, SOURCES_BY_ID[source_id].get("note") or "待接入"))
            con.commit()
            return 0, SOURCES_BY_ID[source_id].get("note") or "待接入"
        try:
            items = fetcher()
        except Exception as e:
            con.execute("INSERT INTO hotspot_feed_state(source,last_run,ok,fetched,message) "
                        "VALUES(?,?,0,0,?) ON CONFLICT(source) DO UPDATE SET last_run=excluded.last_run,"
                        "ok=excluded.ok,fetched=excluded.fetched,message=excluded.message",
                        (source_id, now, f"{type(e).__name__}: {e}"))
            con.commit()
            return 0, f"{type(e).__name__}: {e}"
        for it in items:
            con.execute("""INSERT INTO hotspot_feed(source,title,url,rank,score,extra,collected_at)
                           VALUES(?,?,?,?,?,?,?)
                           ON CONFLICT(source,url) DO UPDATE SET rank=excluded.rank,
                           score=excluded.score, collected_at=excluded.collected_at""",
                        (it["source"], it["title"], it["url"], it["rank"], it["score"],
                         json.dumps(it.get("extra") or {}, ensure_ascii=False), now))
        con.execute("INSERT INTO hotspot_feed_state(source,last_run,ok,fetched,message) "
                    "VALUES(?,?,1,0,'') ON CONFLICT(source) DO UPDATE SET last_run=excluded.last_run,"
                    "ok=excluded.ok,fetched=excluded.fetched,message=excluded.message",
                    (source_id, now))
        con.commit()
        return len(items), ""
    finally:
        con.close()


def collect_all():
    """采集全部 live 源；返回 {source: {"fetched": n, "error": msg}}。"""
    out = {}
    for s in SOURCES:
        n, err = collect_one(s["id"])
        out[s["id"]] = {"fetched": n, "error": err}
    return out


def feed_view(sources=None, limit=100):
    """热点流视图：sources 过滤（None=全部），按采集时间+排名排序。"""
    con = _conn()
    try:
        state = {r["source"]: dict(r) for r in con.execute("SELECT * FROM hotspot_feed_state")}
        if sources:
            marks = ",".join("?" for _ in sources)
            rows = con.execute(f"SELECT * FROM hotspot_feed WHERE source IN ({marks}) "
                               f"ORDER BY collected_at DESC, rank ASC LIMIT ?",
                               (*sources, int(limit))).fetchall()
        else:
            rows = con.execute("SELECT * FROM hotspot_feed ORDER BY collected_at DESC, rank ASC "
                               "LIMIT ?", (int(limit),)).fetchall()
        counts = {r["source"]: r["n"] for r in con.execute(
            "SELECT source, COUNT(*) AS n FROM hotspot_feed GROUP BY source")}
        return {
            "sources": [{**s, "desc": s.get("desc") or "", "scope": s.get("scope") or "",
                         "interval": s.get("interval") or "",
                         "count": counts.get(s["id"], 0),
                         "lastRun": (state.get(s["id"]) or {}).get("last_run") or "",
                         "ok": bool((state.get(s["id"]) or {}).get("ok")),
                         "error": (state.get(s["id"]) or {}).get("message") or ""}
                        for s in SOURCES],
            "items": [{"id": r["id"], "source": r["source"], "title": r["title"], "url": r["url"],
                       "rank": r["rank"], "score": r["score"],
                       "extra": json.loads(r["extra"] or "{}"),
                       "collectedAt": r["collected_at"]} for r in rows],
        }
    finally:
        con.close()


def categories_view():
    """热点分类表：分类 → 源清单（说明 / 采集范围 / 间隔 / 运行状态），订阅页直接展示。"""
    con = _conn()
    try:
        state = {r["source"]: dict(r) for r in con.execute("SELECT * FROM hotspot_feed_state")}
        counts = {r["source"]: r["n"] for r in con.execute(
            "SELECT source, COUNT(*) AS n FROM hotspot_feed GROUP BY source")}
        cats = []
        for cat in dict.fromkeys(s["category"] for s in SOURCES):   # 保序去重
            srcs = []
            for s in SOURCES:
                if s["category"] != cat:
                    continue
                st = state.get(s["id"]) or {}
                srcs.append({"id": s["id"], "name": s["name"], "live": s["live"],
                             "desc": s.get("desc") or "", "scope": s.get("scope") or "",
                             "interval": s.get("interval") or "", "note": s.get("note") or "",
                             "count": counts.get(s["id"], 0),
                             "lastRun": st.get("last_run") or "", "ok": bool(st.get("ok")),
                             "error": st.get("message") or ""})
            cats.append({"category": cat, "desc": CATEGORY_DESC.get(cat, ""), "sources": srcs})
        return {"ok": True, "collectInterval": "30 分钟/轮", "categories": cats}
    finally:
        con.close()


def _loop_once():
    try:
        out = collect_all()
        print(f"[feed] 采集完成：{ {k: v['fetched'] for k, v in out.items()} }", flush=True)
    except Exception as e:
        print(f"[feed] 采集异常：{type(e).__name__}: {e}", flush=True)


def start(interval_sec=1800, stop_event=None):
    """采集线程：启动先采一轮，之后每 interval_sec（默认 30 分钟）一轮。"""
    import threading
    stop = stop_event or threading.Event()

    def run():
        first = True
        while True:
            if first:
                _loop_once()
                first = False
                if stop.wait(30):
                    return
            _loop_once()
            if stop.wait(interval_sec):
                return

    threading.Thread(target=run, daemon=True).start()


if __name__ == "__main__":
    out = collect_all()
    v = feed_view(limit=5)
    print(json.dumps({"collect": out, "sample": [i["title"] for i in v["items"][:5]],
                      "sources": v["sources"]}, ensure_ascii=False, indent=2))
