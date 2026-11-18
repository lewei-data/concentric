#!/usr/bin/env python3
"""冒烟测试：初始化三态、密码/key 登录、上传→分析→检索→问答链路。

用法（必须清 LD_LIBRARY_PATH）：
  env -u LD_LIBRARY_PATH OPC_MCP_API_KEYS=testkey-123 \
      python3 mcp/tests/test_smoke.py
数据写到临时目录，不污染 mcp/data。
"""

import os
import sys
import tempfile
import time
from pathlib import Path

TMP = tempfile.mkdtemp(prefix="opc-mcp-test-")
os.environ["OPC_MCP_DATA_DIR"] = TMP
# 冒烟素材只在临时目录里，别回流到生产的内容化队列
os.environ["OPC_MCP_SKIP_CONTENT_ENQUEUE"] = "1"
# 用户产物根目录也隔离，避免测试写到 /mnt/sefs/conring/users/<真实手机号>
os.environ["OPC_USER_ROOT"] = os.path.join(TMP, "users")
os.environ["OPC_MCP_API_KEYS"] = os.environ.get("OPC_MCP_API_KEYS") or "testkey-123"
# 历史用例沿用本机 phone-only；外部部署默认关闭，见下方 need_secret 检查
os.environ["OPC_MCP_PHONE_ONLY"] = "1"

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from opc_mcp import auth, config, graph_brief, knowledge, llm, media, resources  # noqa: E402
from opc_mcp.server import OpcServer  # noqa: E402
from opc_mcp.store import Store  # noqa: E402

PHONE = "18911789286"
PASSED = []


def check(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name + ((" | " + str(detail)[:300]) if detail else ""))
    if not cond:
        raise SystemExit("冒烟测试失败：" + name)
    PASSED.append(name)


def main():
    print("data_dir =", TMP)
    print("cred     =", config.CRED_TABLE, "exists=", config.CRED_TABLE.exists())
    store = Store(config.DB_PATH)

    # 1. 初始化三态：外部接入只要 key，手机号只用于申请/密码登录
    r = auth.init(store)
    check("无 key 无手机号 → need_secret", r["status"] == "need_secret" and "key" in r["required"], r)

    r = auth.init(store, "123")
    check("手机号格式错 → need_input", r["status"] == "need_input", r)

    r = auth.init(store, PHONE)
    check("名册手机号 → ready", r["status"] == "ready" and r["session_token"], r)
    token = r["session_token"]
    check("用户信息来自名册", r["user"]["name"] == "蒿色莫", r["user"])

    r = auth.init(store, "13900000000")
    check("未登记号 → need_registration", r["status"] == "need_registration", r)

    # 2. 登录与鉴权
    try:
        auth.login(store, PHONE, password="wrong-password")
        check("错误密码应抛错", False)
    except auth.AuthError as e:
        check("错误密码 → AuthError", "密码错误" in str(e), e)

    try:
        auth.login(store, "13900000000", key="bad-key")
        check("错误 key 应抛错", False)
    except auth.AuthError as e:
        check("错误 key → AuthError", "key 无效" in str(e), e)

    r = auth.login(store, "13900000000", key=os.environ["OPC_MCP_API_KEYS"])
    check("正确 key → ready", r["status"] == "ready", r)
    key_token = r["session_token"]

    try:
        auth.authenticate(store, "not-a-token")
        check("无效 token 应抛错", False)
    except auth.AuthError as e:
        check("无效 token → AuthError", "会话无效" in str(e), e)
    check("有效 token 可鉴权", auth.authenticate(store, token)["phone"] == PHONE)
    check("key 登录会话可用", auth.authenticate(store, key_token)["phone"] == "13900000000")

    # 2b. 个人 key：手机号只是身份，key 才发会话；网关注入 client_key 不需要 token
    personal = "opc_test_personal_key"
    store.add_api_key(PHONE, auth.hash_key(personal), "smoke")
    check("个人 key 直接解析用户", auth.resolve(store, {"client_key": personal})["phone"] == PHONE)
    check("个人 key 登录发会话", auth.login(store, PHONE, key=personal)["status"] == "ready")
    check("opc_init 只要 key（无手机号）", auth.init(store, key=personal)["status"] == "ready")
    check("opc_login 只要 key（无手机号）", auth.login(store, key=personal)["status"] == "ready")
    wrong_phone = auth.login(store, "13900000000", key=personal)
    check("key 自带手机号，忽略误传 phone",
          wrong_phone["status"] == "ready" and wrong_phone["user"]["phone"] == PHONE, wrong_phone)
    srv0 = OpcServer()
    who = eval_content(srv0.dispatch("opc_whoami", {"client_key": personal}))
    check("opc_whoami 走网关注入 key", who["phone"] == PHONE, who)

    # 2c. opc_route 路由兜底：触发词命中，且纯提示工具不需要鉴权
    rt = eval_content(srv0.dispatch("opc_route", {"query": "知识库和知识图谱里关于同心圆的说明"}))
    check("opc_route 知识类命中 kb_search+ask",
          rt["knowledge_related"] is True
          and "kb_search" in rt["steps"] and "ask" in rt["steps"], rt)
    check("opc_route 携带 ask 参数模板",
          "text=" in rt["templates"].get("ask", ""), rt["templates"])
    rt2 = eval_content(srv0.dispatch("opc_route", {"query": "我要上传一个大文件"}))
    check("opc_route 非知识类先 whoami",
          rt2["steps"][0] == "opc_whoami" and "upload_begin" in rt2["steps"], rt2)
    rt_shared = eval_content(srv0.dispatch(
        "opc_route", {"query": "公司共享的资料和公开下载区"}))
    check("opc_route 共享资料 → opc_resource_list 而非 material_list",
          "opc_resource_list" in rt_shared["steps"]
          and "material_list" not in rt_shared["steps"]
          and 'kind="shared_materials"' in rt_shared["templates"].get("opc_resource_list", "")
          and 'kind="downloads"' in rt_shared["templates"].get("opc_resource_list", ""),
          rt_shared)
    rt_own = eval_content(srv0.dispatch("opc_route", {"query": "列出我的素材并下载"}))
    check("opc_route 本人素材仍走 material_list",
          "material_list" in rt_own["steps"], rt_own)
    rt_del = eval_content(srv0.dispatch("opc_route", {"query": "删除我上传的那个素材"}))
    check("opc_route 删除意图命中 material_delete",
          "material_list" in rt_del["steps"] and "material_delete" in rt_del["steps"]
          and "material_delete(material_id=" in rt_del["templates"].get("material_delete", ""), rt_del)
    rt_pend = eval_content(srv0.dispatch("opc_route", {"query": "我上传的素材里有哪些没分析"}))
    check("opc_route 待分析意图优先 material_list",
          "material_list" in rt_pend["steps"] and "upload_begin" not in rt_pend["steps"], rt_pend)
    check("opc_route 无需鉴权",
          srv0.dispatch("opc_route", {"query": "OPC"}).get("isError") is False)
    config.PHONE_ONLY = False
    need = eval_content(srv0.dispatch("opc_init", {"phone": PHONE}))
    check("外部模式仅手机号 → need_secret", need["status"] == "need_secret", need)
    config.PHONE_ONLY = True

    # 2d. 跨源资源互通：在隔离目录造一个 owner 库，验证 MCP 可见 OPC 库 + 屏蔽规则
    import sqlite3 as _sq
    oroot = config.phone_root(PHONE)
    oroot.mkdir(parents=True, exist_ok=True)
    odb = oroot / "opc.db"
    con = _sq.connect(str(odb))
    con.executescript(
        "CREATE TABLE knowledge(id TEXT PRIMARY KEY,title TEXT,summary TEXT,body TEXT,path TEXT,"
        "category TEXT,sancai TEXT,updated_at TEXT);"
        "CREATE TABLE reports(id TEXT PRIMARY KEY,title TEXT,kind TEXT,conclusion TEXT,evidence TEXT,"
        "decisions TEXT,raw TEXT,created_at TEXT);")
    con.execute("INSERT INTO knowledge VALUES(?,?,?,?,?,?,?,?)",
                ("kb_web_1", "Web 侧制度说明", "摘要", "同心圆知识图谱与图数据库互通说明",
                 "OPC/知识库/web.md", "skill", "ren", "2026-11-18"))
    con.execute("INSERT INTO reports VALUES(?,?,?,?,?,?,?,?)",
                ("rep_1", "调研报告", "analysis", "结论含知识库关键词", "evidence", "", "", "2026-11-18"))
    con.commit()
    con.close()

    rc = eval_content(srv0.dispatch("opc_resources", {"client_key": personal}))
    check("opc_resources 看见 owner 库", rc.get("ownerDbExists") is True
          and rc.get("knowledge") == 1 and rc.get("reports") == 1, rc)
    xhits = eval_content(srv0.dispatch("kb_search", {"client_key": personal,
                                                     "query": "知识图谱 图数据库"}))
    check("kb_search 跨源命中 OPC 知识", any(h["source"] == "opc.knowledge" for h in xhits.get("hits", [])), xhits)
    klist = eval_content(srv0.dispatch("opc_resource_list", {"client_key": personal,
                                                             "kind": "knowledge"}))
    check("opc_resource_list 列出 OPC 条目", len(klist.get("items", [])) == 1, klist)
    one = eval_content(srv0.dispatch("opc_resource_read", {"client_key": personal,
                                                           "ref": "knowledge:kb_web_1"}))
    check("opc_resource_read 读取 OPC 条目", "知识图谱" in (one.get("item", {}).get("body") or ""), one)
    blocked = srv0.dispatch("opc_resource_read", {"client_key": personal,
                                                   "ref": "vault:公司/登录凭据.md"})
    check("凭据文件被屏蔽", blocked.get("isError") and "屏蔽" in blocked["content"][0]["text"],
          blocked["content"][0]["text"][:160])
    outside = srv0.dispatch("opc_resource_read", {"client_key": personal,
                                                   "ref": "vault:../../etc/passwd"})
    check("vault 越界被拒", outside.get("isError") and "越界" in outside["content"][0]["text"],
          outside["content"][0]["text"][:160])

    # 2e. 全局共享资源：任意 key（含别的手机号）都能看到共享部分
    resources.SHARED_DB.parent.mkdir(parents=True, exist_ok=True)
    sc = _sq.connect(str(resources.SHARED_DB))
    cols = {r[1] for r in sc.execute("PRAGMA table_info(knowledge)")}
    if "project_id" not in cols:
        sc.execute("ALTER TABLE knowledge ADD COLUMN project_id TEXT")
    existing = {r[0] for r in sc.execute("SELECT id FROM knowledge")}
    if "kb_shared_1" not in existing:
        sc.execute("INSERT INTO knowledge(id,title,summary,body,path,category,sancai,project_id,updated_at) "
                   "VALUES(?,?,?,?,?,?,?,?,?)",
                   ("kb_shared_1", "全公司共享制度", "摘要", "共享部分：全员可见的知识图谱规范",
                    "公司/规范.md", "decision", "ren", "全公司", "2026-11-18"))
    if "kb_private_1" not in existing:
        sc.execute("INSERT INTO knowledge(id,title,summary,body,path,category,sancai,project_id,updated_at) "
                   "VALUES(?,?,?,?,?,?,?,?,?)",
                   ("kb_private_1", "私密资料", "", "不该被看到", "OPC/私人.md", "skill", "ren", "gou", "2026-11-18"))
    sc.commit(); sc.close()
    resources.SHARED_MATERIALS.write_text(
        '{"items":[{"id":"mat_share_1","title":"共享素材包","desc":"全员可见共享资料","url":"/materials/x"}]}',
        encoding="utf-8")
    other_key = "opc_other_phone_key"
    store.add_api_key("13900000000", auth.hash_key(other_key), "shared-test")
    other = eval_content(srv0.dispatch("opc_resources", {"client_key": other_key}))
    check("别的手机号也见共享段", other["shared"]["sharedKnowledge"] == 1
          and other["shared"]["sharedMaterials"] == 1, other["shared"])
    shared_hits = eval_content(srv0.dispatch("kb_search", {"client_key": other_key,
                                                           "query": "共享 知识图谱"}))
    check("跨源检索命中共享知识", any(h["source"] == "shared.knowledge" for h in shared_hits.get("hits", [])), shared_hits)
    check("跨源检索不泄漏非全公司条目",
          not any(h.get("id") == "kb_private_1" for h in shared_hits.get("hits", [])), shared_hits)
    slist = eval_content(srv0.dispatch("opc_resource_list", {"client_key": other_key,
                                                             "kind": "shared_knowledge"}))
    check("共享列表只含全公司条目", len(slist.get("items", [])) == 1
          and slist["items"][0]["id"] == "kb_shared_1", slist)
    sread = eval_content(srv0.dispatch("opc_resource_read", {"client_key": other_key,
                                                             "ref": "shared_knowledge:kb_shared_1"}))
    check("共享条目可读", "共享" in (sread.get("item", {}).get("body") or ""), sread)
    priv = srv0.dispatch("opc_resource_read", {"client_key": other_key,
                                               "ref": "shared_knowledge:kb_private_1"})
    check("非全公司共享条目拒读", priv.get("isError"), priv["content"][0]["text"][:120])

    # 3. MCP 服务：上传 → 后台分析 → 检索 → 问答
    srv = OpcServer()

    r = eval_content(srv.dispatch("opc_init", {"phone": PHONE}))
    check("工具 opc_init 走 MCP 结果封装", r["status"] == "ready", r)
    r = eval_content(srv.dispatch("opc_login", {"phone": PHONE, "password": "x"}))
    check("工具 opc_login 错误密码 → need_auth", r.get("status") == "need_auth", r)

    src = Path(TMP) / "物料说明.md"
    src.write_text("# 天狗大陆 素材清单\n\n"
                   "本批素材包含三类：天狗大陆角色立绘、场景概念图、配乐小样。\n\n"
                   "验收标准：立绘需 2048 像素以上，场景图需覆盖昼夜两版，配乐时长不少于 60 秒。\n\n"
                   "交付时间：本周五 18:00 前上传到共享素材库，由 kb_manager 统一内化。\n",
                   encoding="utf-8")
    up = srv.dispatch("material_upload", {"token": token, "path": str(src)})
    body = eval_content(up)
    check("上传成功并返回 material_id", body.get("material_id") and body.get("job_id"), body)
    mid = body["material_id"]

    up2 = srv.dispatch("material_upload", {"token": token, "path": str(src)})
    check("重复上传去重", eval_content(up2).get("duplicate") is True, eval_content(up2))

    # 3b. 大文件分片续传建单
    ub = eval_content(srv.dispatch("upload_begin", {"token": token,
                                                   "filename": "资料.zip", "size": 1024}))
    check("upload_begin 返回 upload_id/put_url",
          ub.get("upload_id", "").startswith("up_")
          and ub["upload_id"] in ub.get("put_url", "")
          and "tus_url" in ub and "part_put_url" in ub, ub)
    check("upload_begin 记录在 uploads 表", store.upload(ub["upload_id"])["size"] == 1024, ub)
    bad = srv.dispatch("upload_begin", {"token": token, "filename": "big.zip",
                                        "size": int(config.UPLOAD_MAX_MB * 1024 * 1024) + 1})
    check("upload_begin 超上限报错", bad.get("isError") is True, bad["content"][0]["text"][:120])
    bad2 = srv.dispatch("upload_begin", {"token": token, "filename": "zero.zip", "size": 0})
    check("upload_begin size=0 报错", bad2.get("isError") is True, bad2["content"][0]["text"][:120])

    job = srv.dispatch("job_get", {"token": token, "job_id": body["job_id"], "wait_sec": 120})
    job_body = eval_content(job)
    check("分析作业完成", job_body.get("status") == "succeeded", job_body)
    items = job_body.get("result", {}).get("items", [])
    check("素材内化成功", items and items[0].get("status") == "analyzed", items)

    lst = eval_content(srv.dispatch("material_list", {"token": token}))
    check("素材列表可见", lst.get("count") == 1, lst)

    caps = eval_content(srv.dispatch("media_capabilities", {"token": token}))
    check("能力查询列出音频/图像/视频渠道",
          "audio" in caps and "image" in caps and "video" in caps
          and len(caps["audio"]["channels"]) >= 2
          and any(c.get("reference_image") for c in caps["image"]["channels"])
          and "auth" in caps, {k: list(v)[:4] for k, v in caps.items() if isinstance(v, dict)})
    bad_caps = eval_content(srv.dispatch("media_capabilities", {"client_key": "bad-key"}))
    check("生成能力与注册共用鉴权", bad_caps.get("status") == "need_auth", bad_caps)

    # 生成产物取回：file_key 登记 + file_link 重取，下载端点单独用 HTTP 验证
    probe_png = Path(TMP) / "file_link_probe.png"
    probe_png.write_bytes(b"\x89PNG\r\n\x1a\n" + b"x" * 64)
    fk = "smokefilekey123456"
    srv.store.add_generated_file(fk, PHONE, str(probe_png), "image",
                                 filename="probe.png", mime="image/png", size=probe_png.stat().st_size)
    link = eval_content(srv.dispatch("file_link", {"token": token, "file_key": fk}))
    check("产物可取回下载链接（同一 key）",
          link.get("file_key") == fk and "/api/mcp/file/" in str(link.get("download_url"))
          and "X-Mail-Agent" in str(link.get("download_auth")), link)
    other = eval_content(srv.dispatch("file_link", {"client_key": "bad-key", "file_key": fk}))
    check("产物链接不越权", other.get("status") == "need_auth", other)

    hits = eval_content(srv.dispatch("kb_search", {"token": token, "query": "立绘 验收标准"}))
    check("知识库检索命中", hits.get("count", 0) > 0, hits)

    ok, detail, _probe_info = llm.probe(timeout=5)
    ans = eval_content(srv.dispatch("ask", {"token": token,
                                            "text": "本批素材的验收标准是什么？", "wait_sec": 120}))
    check("问答作业返回答案", bool(ans.get("result", {}).get("answer")), ans)
    check("问答附带引用", "citations" in ans.get("result", {}), ans.get("result", {}).keys())
    ans_q = eval_content(srv.dispatch("ask", {"token": token,
                                              "question": "本批素材的验收标准是什么？", "wait_sec": 120}))
    check("ask 的 question 别名生效", bool(ans_q.get("result", {}).get("answer")), ans_q)
    check("默认 ask 不返回 knowledge_brief",
          "knowledge_brief" not in ans_q.get("result", {}), ans_q.get("result", {}).keys())
    empty_ask = srv.dispatch("ask", {"token": token})
    check("ask 无问题内容报错并提示两参数",
          empty_ask.get("isError") and "text 或 question" in empty_ask["content"][0]["text"],
          empty_ask["content"][0]["text"][:120])
    print("llm_ok =", ok, "degraded =", ans.get("result", {}).get("degraded"), detail or "")

    status = eval_content(srv.dispatch("kb_status", {"token": token}))
    check("kb_status 统计正确", status.get("materials", {}).get("total") == 1
          and status.get("chunks", 0) > 0, status)
    probe_info = status.get("llm", {}).get("probe") or {}
    check("kb_status 的 LLM 探活走真实 chat 且带冷却字段",
          probe_info.get("method") == "chat" and "status" in probe_info
          and (probe_info.get("status") == 200 or "retry_after_sec" in probe_info),
          status.get("llm"))

    # 4. 未带 token 访问 → need_auth 指引
    err = srv.dispatch("ask", {"text": "hi"})
    check("缺 token → need_auth", err.get("isError") and "need_auth" in err["content"][0]["text"],
          err["content"][0]["text"][:200])

    # 5. 非文本素材 → 归档跳过
    img = Path(TMP) / "占位图.png"
    img.write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 64)
    up3 = eval_content(srv.dispatch("material_upload", {"token": token, "path": str(img)}))
    job3 = eval_content(srv.dispatch("job_get", {"token": token,
                                                 "job_id": up3["job_id"], "wait_sec": 60}))
    item3 = (job3.get("result") or {}).get("items", [{}])[0]
    check("图片素材 → skipped 并给出原因", item3.get("status") == "skipped", item3)

    # 6. 素材删除：本人可删、他人不可删，切块/卡片/原文件同步清理
    doc2 = Path(TMP) / "待删除说明.md"
    doc2.write_text("临时素材：用于验证 MCP 删除入口与切块清理。\n", encoding="utf-8")
    up4 = eval_content(srv.dispatch("material_upload", {"token": token, "path": str(doc2)}))
    job4 = eval_content(srv.dispatch("job_get", {"token": token,
                                                 "job_id": up4["job_id"], "wait_sec": 120}))
    check("待删除素材已分析", job4.get("status") == "succeeded", job4)
    mid4 = up4["material_id"]
    foreign = srv.dispatch("material_delete", {"client_key": other_key, "material_id": mid4})
    check("他人 key 不能删本人素材", foreign.get("isError") is True,
          foreign["content"][0]["text"][:120])
    d4 = eval_content(srv.dispatch("material_delete", {"token": token, "material_id": mid4}))
    check("本人可删除素材", d4.get("deleted") is True and d4.get("deleted_chunks", 0) > 0
          and d4.get("deleted_cards", 0) > 0, d4)
    check("删除后原文件消失", not Path(d4["path"]).exists(), d4)
    lst4 = eval_content(srv.dispatch("material_list", {"token": token}))
    check("删除后素材列表不再包含该素材",
          all(m["material_id"] != mid4 for m in lst4.get("items", [])), lst4)
    hits4 = eval_content(srv.dispatch("kb_search", {"token": token,
                                                    "query": "待删除说明 切块清理"}))
    check("删除后检索不再命中该素材",
          all(h.get("id") != mid4 and h.get("material_id") != mid4
              for h in hits4.get("hits", [])), hits4)
    again = srv.dispatch("material_delete", {"token": token, "material_id": mid4})
    check("重复删除报错", again.get("isError") is True, again["content"][0]["text"][:120])

    # 7. with_brief：五行三才图谱简报（分组/层级/硬预算/屏蔽/零命中回退）
    vd = config.user_vault(PHONE) / "OPC" / "知识库"
    vd.mkdir(parents=True, exist_ok=True)
    kb_nodes = [
        ("kb_b_tian.md", "kb_b_tian", "天狗大陆验收标准·天", "decision", "tian"),
        ("kb_b_di.md", "kb_b_di", "天狗大陆角色立绘规格·地", "analysis", "di"),
        ("kb_b_ren.md", "kb_b_ren", "天狗大陆交付流程·人", "skill", "ren"),
        ("kb_b_other.md", "kb_b_other", "天狗大陆配乐小样清单", "graph", ""),
        ("kb_b_internal.md", "kb_b_internal", "天狗大陆内部地址说明", "analysis", "di"),
        ("敏感台账.md", "kb_b_secret", "天狗大陆验收标准敏感台账", "decision", "ren"),
    ]
    for name, kid, title, cat, sc in kb_nodes:
        body = ("天狗大陆内部地址 http://192.168.21.206:9200/v1 ，服务端路径 /opt/sefs/sbin/x，"
                "端口 206:9105，均不可对外提供。" if kid == "kb_b_internal" else
                "天狗大陆本批素材的验收标准与%s相关，需逐项确认。\n\n正文：立绘 2048 像素以上。" % title)
        (vd / name).write_text(
            "---\nkb_id: %s\ntitle: %s\ncategory: %s\nsancai: %s\ntags: 天狗大陆、验收标准\n---\n\n%s\n"
            % (kid, title, cat, sc, body), encoding="utf-8")
    con = _sq.connect(str(odb))
    # kb_graph.load() 按生产 schema 读 tags/raw，测试库补齐否则关系边会被静默跳过
    exist_cols = {r[1] for r in con.execute("PRAGMA table_info(knowledge)")}
    for col in ("tags", "raw"):
        if col not in exist_cols:
            con.execute("ALTER TABLE knowledge ADD COLUMN %s TEXT" % col)
    if not {r[1] for r in con.execute("PRAGMA table_info(knowledge_relations)")}:
        con.execute("CREATE TABLE knowledge_relations(src TEXT,dst TEXT,kind TEXT,label TEXT,weight REAL)")
    for name, kid, title, cat, sc in kb_nodes:
        con.execute("INSERT OR REPLACE INTO knowledge"
                    "(id,title,summary,body,path,category,sancai,updated_at) VALUES(?,?,?,?,?,?,?,?)",
                    (kid, title, "摘要", "天狗大陆验收标准相关正文",
                     "OPC/知识库/" + name, cat, sc, "2026-11-18"))
    con.execute("DELETE FROM knowledge_relations WHERE src LIKE 'kb_b_%'")
    con.execute("INSERT INTO knowledge_relations VALUES('kb_b_tian','kb_b_other','ref','引用',1.0)")
    con.execute("INSERT INTO knowledge_relations VALUES('kb_b_di','kb_b_secret','tag','#天狗大陆',1.0)")
    con.commit()
    con.close()

    qb = "天狗大陆 验收标准"
    b1 = graph_brief.build(store, PHONE, qb, level=1, budget=700)
    check("图谱简报命中并按五行三才分组", b1["hit"] and b1["chars"] <= 700
          and sum(b1["groups"].values()) == len(b1["items"])
          and any(i["sancai"] in ("tian", "di", "ren") for i in b1["items"]), b1)
    check("屏蔽条目不进简报", "敏感台账" not in b1["text"] and "绝密" not in b1["text"],
          b1["text"][:200])
    check("层级1带一跳邻居", "↳" in b1["text"], b1["text"][:300])
    check("简报尾部提示完整出处而非截断路径",
          "items[].source" in b1["text"] and "勿直接读取" in b1["text"], b1["text"].splitlines()[-1])
    check("items.source 是完整可读路径",
          b1["items"] and all(str(i["source"]).endswith(".md") for i in b1["items"]), b1["items"])
    b0 = graph_brief.build(store, PHONE, qb, level=0, budget=700)
    check("层级0无邻居与图扩展", "↳" not in b0["text"] and "○" not in b0["text"], b0["text"][:200])
    b2 = graph_brief.build(store, PHONE, qb, level=2, budget=1200)
    check("层级2出现图扩展节点", "○" in b2["text"], b2["text"][:300])
    bs = graph_brief.build(store, PHONE, qb, level=1, budget=200)
    check("小预算仍守住硬上限", bs["hit"] and bs["chars"] <= 200, bs)
    bn = graph_brief.build(store, PHONE, "今天天气怎么样 xyz", level=1, budget=700)
    check("零命中返回空串且零开销", not bn["hit"] and bn["text"] == "" and bn["chars"] == 0, bn)
    bp = graph_brief.build(store, PHONE, "天狗大陆 内部地址", level=1, budget=900)
    raw_internal = ("192.168.21.206", "/opt/sefs", "206:9105")
    check("对外简报正文不含内网地址/服务端路径",
          bp["hit"] and not any(x in bp["text"] for x in raw_internal), bp["text"][:250])
    check("简报元数据同样已净化", not any(x in str(bp["items"]) for x in raw_internal))

    ans_b = eval_content(srv.dispatch("ask", {"token": token, "text": qb,
                                              "with_brief": True, "brief_chars": 700,
                                              "wait_sec": 120}))
    kb2 = (ans_b.get("result") or {}).get("knowledge_brief") or {}
    check("ask 返回 knowledge_brief 且进入简报模式",
          kb2.get("hit") is True and kb2.get("chars", 0) <= 700
          and kb2.get("mode") == "graph" and "知识图谱" in kb2.get("text", ""), kb2)
    cits = (ans_b.get("result") or {}).get("citations", [])
    check("with_brief 的引用不带服务端绝对路径",
          all(not str(c.get("path") or "").startswith("/") for c in cits), cits)
    merged = graph_brief.merged_context(store, PHONE, b1, budget=1200)
    check("合并上下文受总预算约束且不再平铺 opc.knowledge",
          len(merged) <= 1200 and "【opc.knowledge】" not in merged, len(merged))
    badb = srv.dispatch("ask", {"token": token, "text": qb, "brief_chars": "abc"})
    check("brief_chars 非整数报错", badb.get("isError") is True,
          badb["content"][0]["text"][:100])

    # 8. LLM 故障时降级回答不嵌套、历史只留短标记
    orig_chat = llm.chat

    def _boom(*a, **k):
        raise llm.LLMError("LLM HTTP 429：所选模型无可用部署，冷却约 50400 秒")

    llm.chat = _boom
    try:
        dg = eval_content(srv.dispatch("ask", {"token": token, "text": qb, "wait_sec": 60}))
    finally:
        llm.chat = orig_chat
    dans = (dg.get("result") or {}).get("answer") or ""
    check("降级回答单层不嵌套",
          dans.count("LLM 通道") == 1 and "先给知识库命中" in dans, dans[:200])

    # 9. 主通道 429/500 自动切兜底；400 不兜底
    orig_post, orig_fb, orig_base = llm._post, llm.fallback_config, config.LLM_BASE_URL
    calls = []

    def fake_post(url, model, key, messages, temperature, max_tokens, timeout):
        calls.append((url, model))
        if "primary" in url:
            raise llm.LLMError("HTTP 429", status=429, retry_after_sec=10)
        return "兜底回答"

    llm._post = fake_post
    llm.fallback_config = lambda force=False: {
        "id": "test-oc", "url": "http://fallback.test/v1", "model": "m", "key": "k"}
    config.LLM_BASE_URL = "http://primary.test/v1"
    try:
        out_fb = llm.chat([{"role": "user", "content": "hi"}])
    finally:
        llm._post, llm.fallback_config, config.LLM_BASE_URL = orig_post, orig_fb, orig_base
    check("429 自动切兜底通道", out_fb == "兜底回答" and len(calls) == 2, calls)

    calls400 = []

    def fake_post_400(url, model, key, messages, temperature, max_tokens, timeout):
        calls400.append(url)
        raise llm.LLMError("HTTP 400", status=400)

    llm._post, orig_base = fake_post_400, config.LLM_BASE_URL
    config.LLM_BASE_URL = "http://primary.test/v1"
    raised = False
    try:
        llm.chat([{"role": "user", "content": "hi"}])
    except llm.LLMError:
        raised = True
    finally:
        llm._post, config.LLM_BASE_URL = orig_post, orig_base
    check("400 不触发兜底", raised and len(calls400) == 1, calls400)

    print("\n共 %d 项通过：\n  - %s" % (len(PASSED), "\n  - ".join(PASSED)))
    print("运行目录（可删除）：", TMP)


def eval_content(result):
    """MCP 结果 → dict。"""
    import json
    return json.loads(result["content"][0]["text"])


if __name__ == "__main__":
    t0 = time.time()
    main()
    print("耗时 %.1fs" % (time.time() - t0))
