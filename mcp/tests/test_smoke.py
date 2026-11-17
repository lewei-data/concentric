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
os.environ["OPC_MCP_API_KEYS"] = os.environ.get("OPC_MCP_API_KEYS") or "testkey-123"

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from opc_mcp import auth, config, knowledge, llm, media  # noqa: E402
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

    # 1. 初始化三态
    r = auth.init(store)
    check("无手机号 → need_input", r["status"] == "need_input" and "phone" in r["required"], r)

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

    # 3. MCP 服务：上传 → 后台分析 → 检索 → 问答
    srv = OpcServer()

    r = eval_content(srv.dispatch("opc_init", {"phone": PHONE}))
    check("工具 opc_init 走 MCP 结果封装", r["status"] == "ready", r)
    r = eval_content(srv.dispatch("opc_login", {"phone": PHONE, "password": "x"}))
    check("工具 opc_login 错误密码 → need_init", r.get("status") == "need_init", r)

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

    job = srv.dispatch("job_get", {"token": token, "job_id": body["job_id"], "wait_sec": 120})
    job_body = eval_content(job)
    check("分析作业完成", job_body.get("status") == "succeeded", job_body)
    items = job_body.get("result", {}).get("items", [])
    check("素材内化成功", items and items[0].get("status") == "analyzed", items)

    lst = eval_content(srv.dispatch("material_list", {"token": token}))
    check("素材列表可见", lst.get("count") == 1, lst)

    hits = eval_content(srv.dispatch("kb_search", {"token": token, "query": "立绘 验收标准"}))
    check("知识库检索命中", hits.get("count", 0) > 0, hits)

    ok, detail = llm.probe(timeout=5)
    ans = eval_content(srv.dispatch("ask", {"token": token,
                                            "text": "本批素材的验收标准是什么？", "wait_sec": 120}))
    check("问答作业返回答案", bool(ans.get("result", {}).get("answer")), ans)
    check("问答附带引用", "citations" in ans.get("result", {}), ans.get("result", {}).keys())
    print("llm_ok =", ok, "degraded =", ans.get("result", {}).get("degraded"), detail or "")

    status = eval_content(srv.dispatch("kb_status", {"token": token}))
    check("kb_status 统计正确", status.get("materials", {}).get("total") == 1
          and status.get("chunks", 0) > 0, status)

    # 4. 未带 token 访问 → need_init 指引
    err = srv.dispatch("ask", {"text": "hi"})
    check("缺 token → need_init", err.get("isError") and "need_init" in err["content"][0]["text"],
          err["content"][0]["text"][:200])

    # 5. 非文本素材 → 归档跳过
    img = Path(TMP) / "占位图.png"
    img.write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 64)
    up3 = eval_content(srv.dispatch("material_upload", {"token": token, "path": str(img)}))
    job3 = eval_content(srv.dispatch("job_get", {"token": token,
                                                 "job_id": up3["job_id"], "wait_sec": 60}))
    item3 = (job3.get("result") or {}).get("items", [{}])[0]
    check("图片素材 → skipped 并给出原因", item3.get("status") == "skipped", item3)

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
