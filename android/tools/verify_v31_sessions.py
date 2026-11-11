"""0.9.7 静态契约校验：会话记录（SQLite）+ 可拖拽会话面板 + 首页/视频页接入。

覆盖用户这轮提的 6 条：
  1) 长期保存（SQLite 两表 + 索引）
  2) 按人/天组织，启动加载当天（bind → today）
  3) 面板可拖动改高度、能拉到底部（drag + 三档吸附）
  4) 最新在下面（ts ASC）、滚到顶加载上一日
  5) 文本信息（详情/服务端文本帧）也入会话记录，不只语音转写
  6) 链接带标记可点开；视频通话复用同一套
输出 docs/v31_session_check.json。
"""
import json
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "app", "src", "main", "java", "com", "boray", "voicemcp")
RES = os.path.join(ROOT, "app", "src", "main", "res", "layout")
DOCS = os.path.join(ROOT, "docs")

checks = []


def read(name, base=SRC):
    p = os.path.join(base, name)
    if not os.path.isfile(p):
        return ""
    return open(p, encoding="utf-8").read()


def check(name, ok, detail=""):
    checks.append({"name": name, "ok": bool(ok), "detail": detail})
    print(("  PASS  " if ok else "  FAIL  ") + name + ((" | " + detail) if detail and not ok else ""))


def main():
    store = read("SessionStore.java")
    panel = read("SessionPanel.java")
    panel_xml = read("view_session_panel.xml", RES)
    main_xml = read("activity_main.xml", RES)
    live_xml = read("activity_live_call.xml", RES)
    main = read("MainActivity.java")
    live = read("LiveCallActivity.java")
    svc = read("VoiceCallService.java")
    chan = read("RealtimeChannel.java")

    print("== 1. SQLite 长期保存 ==")
    check("库名 conring_sessions.db", "conring_sessions.db" in store)
    check("sessions 表", 'CREATE TABLE sessions(' in store)
    check("messages 表", 'CREATE TABLE messages(' in store)
    check("按人+天+时间建索引", "idx_msg_emp_day ON messages(emp_id, day, ts)" in store)
    check("会话索引", "idx_sess_emp_day" in store)
    check("只增列不重建", "不重建" in store)
    check("begin/finish", "public synchronized String begin(" in store and "public synchronized void finish(" in store)
    check("add/setText/remove", "public synchronized long add(" in store
          and "public synchronized void setText(" in store and "public synchronized void remove(" in store)
    check("按天查询", "public synchronized List<Row> day(" in store)
    check("上一日探测", "public synchronized String previousDay(" in store)
    check("本地日期 yyyy-MM-dd", 'new SimpleDateFormat("yyyy-MM-dd"' in store)
    check("今天/昨天标签", "dayLabel" in store and '"昨天"' in store)
    check("多面板监听（增删）", "addListener" in store and "removeListener" in store)

    print("== 2. 按人/天组织，默认当天 ==")
    check("面板 bind(empId,name)", "public void bind(String id, String name)" in panel)
    check("默认加载 today()", "days.add(SessionStore.today())" in panel)
    check("标题=员工名 / 副标题=日期+条数", "tvTitle.setText(empName" in panel and "tvSub.setText(" in panel)
    check("日期分隔行", "addDaySeparator" in panel and "dayLabel" in panel)
    check("首页绑定当前汇报人", "sessionPanel.bind(" in main and "EmployeeStore.current" in main)
    check("onResume 重新绑定", "bindSession();" in main)

    print("== 3. 拖动改高度、直达底部 ==")
    check("首页整页层 / 视频页底部一条", 'android:layout_height="match_parent"' in main_xml
          and 'android:layout_gravity="bottom"' in live_xml)
    check("XML 可拖动组件", 'com.boray.voicemcp.SessionPanel' in main_xml)
    check("整页占满（MATCH_PARENT）", "ViewGroup.LayoutParams.MATCH_PARENT" in panel and "openFull()" in panel)
    check("不再有拖动/展开档位", "installDrag" not in panel and "STOPS_DP" not in panel)
    check("右上角「收起」（加大的胶囊）", '"收起"' in panel and "onCollapsed.run()" in panel
          and "bg_badge" in panel_xml)
    check("头部：时长/电平/挂断/条数", "tvTimer" in panel_xml and "levelMini" in panel_xml
          and "btnHangup" in panel_xml and "tvSub" in panel_xml)
    check("挂断接宿主逻辑", "setOnHangup" in panel and "setOnHangup" in main
          and "setOnHangup" in live)
    check("三件套兜底可见", "ensureVisible(tvTimer)" in panel and "ensureVisible(btnHangup)" in panel)
    check("整页头部不误触收起", "if (!fullScreen) toggle();" in panel)
    check("时长与电平每帧喂", "setCallStatus(" in panel and "sessionPanel.setCallStatus(" in main)
    check("返回键：整页只收起", "onBackPressed" in main
          and "sessionPanel.getVisibility() == View.VISIBLE" in main)
    check("通话中退出需确认", "confirmLeaveCall" in main and "AlertDialog.Builder" in main
          and "VoiceCallService.stop(this);\n                    finish();" in main)
    check("未接通不自动弹整页", "clearLines();\n        callRequestedAt" in main)
    check("视频页返回键：先收回再确认", "sessionPanel.isFullScreen()" in live
          and "showBar()" in live and "AlertDialog.Builder" in live)
    check("默认是一条形态", "BAR_DP" in panel and "applyMode()" in panel)
    check("一条形态隐藏列表", "svHistory.setVisibility(full ? VISIBLE : GONE)" in panel)
    check("折叠态显示最新一条", "tvLatest" in panel and "updateLatest()" in panel)
    check("点击标题栏也能展开/收起", "tvToggle.setOnClickListener" in panel)
    check("收起回调（宿主可只留内嵌条）", "setOnCollapsed" in panel and "onCollapsed.run()" in panel)
    check("内嵌模式（不拖动、只显示最新）", "setInlineMode" in panel and "inlineMode" in panel)
    check("右上角「打开」入口", "setOnOpen" in panel and "ic_open_panel" in panel)
    check("最新一条取当天", "latest(empId, SessionStore.today())" in panel)
    check("首页内嵌卡 + 整页层两处", "sessionTeaser" in main_xml and "sessionPanel" in main_xml)
    check("顶部人数副标题", "tvHomeSub" in main_xml and "数字员工 " in main)
    check("内嵌卡绑定当前汇报人", "sessionTeaser.bind(" in main)
    check("打开=显示弹出层并展开", "openSessionSheet" in main and "setVisibility(View.VISIBLE)" in main)
    check("收起=隐藏弹出层（内嵌仍在）", "setOnCollapsed(() -> sessionPanel.setVisibility(View.GONE))" in main)

    print("== 4. 最新在下 + 上翻加载上一日 ==")
    check("查询按 ts 正序", '"ts ASC, id ASC"' in store)
    check("滚动监听", "setOnScrollChangeListener" in panel)
    check("到顶加载上一日", "if (y == 0 && !loading && !noOlder" in panel)
    check("加载后保持视口", "svHistory.scrollTo(0, delta)" in panel)
    check("默认贴底", "scrollToBottom" in panel and "smoothScrollTo" in panel)
    check("用户不在底部时不抢滚动", "if (toBottom) scrollToBottom(true)" in panel)

    print("== 5. 文本信息也入会话记录 ==")
    check("角色/类型字段", "ROLE_USER" in store and "KIND_DETAIL" in store)
    check("服务端文本帧 onServerText", "onServerText" in chan)
    check("message/text 帧解析", 'case "message":' in chan and 'case "text":' in chan)
    check("详情行落库", "addDetailLine(reply.detail, linksJson(reply))" in svc)
    check("服务端文本落库", "public void onServerText(String text)" in svc and "addDetailLine(text)" in svc)
    check("瞬时状态不入库", "statusMarker" in svc and 'startsWith("（")' in svc)
    check("同一条不重复插入", "line.dbId > 0" in svc and "setText(line.dbId" in svc)
    check("视频页写同一张表", "SessionStore.get(app).add(" in live and '"video"' in live)
    check("视频页 ASR/ack/回复都记", live.count("logMsg(") >= 4)

    print("== 6. 链接标记与打开 ==")
    check("URL 正则", "https?://" in panel and "Pattern.compile" in panel)
    check("可点击 Span", "ClickableSpan" in panel and "LinkMovementMethod" in panel)
    check("下划线+主色标记", "setUnderlineText(true)" in panel)
    check("只放行 http(s)", 'startsWith("http://")' in panel and "ACTION_VIEW" in panel)
    check("附件胶囊（name+url）", "addLinks(" in panel and "links_json" in store)

    print("== 7. 语音/视频两条链路都接 ==")
    check("语音服务开/封会话", 'ensureLogSession("ws")' in svc and 'ensureLogSession("hls")' in svc
          and "finish(logSessionId" in svc)
    check("定稿回调落库", "setLineSink(this::persistLine)" in svc)
    check("视频页开/封会话", "ensureLogSession()" in live and "finishLogSession()" in live)
    check("视频页展开整页", "sessionPanel.openFull()" in live)
    check("语音接通展开整页", "openSessionSheet()" in main and "openFull()" in main)
    check("接通状态行", "sessionPanel.setStatus(" in main and "tvStatus" in panel_xml)
    check("视频页布局含面板", "SessionPanel" in live_xml)

    print("== 8. 版本 ==")
    g = open(os.path.join(ROOT, "app", "build.gradle"), encoding="utf-8").read()
    ver = re.search(r'def appVersionName\s*=\s*"([^"]+)"', g).group(1)
    code = int(re.search(r'def appVersionCode\s*=\s*(\d+)', g).group(1))
    cfg = read("McpConfig.java")
    check("版本 ≥ 0.9.9", tuple(map(int, ver.split("."))) >= (0, 9, 9), ver)
    check("versionCode ≥ 33", code >= 33, str(code))
    check("CLIENT_VERSION 同步", ('CLIENT_VERSION = "%s"' % ver) in cfg)

    ok = sum(1 for c in checks if c["ok"])
    bad = len(checks) - ok
    out = {"version": ver, "versionCode": code, "total": len(checks),
           "pass": ok, "fail": bad, "checks": checks}
    os.makedirs(DOCS, exist_ok=True)
    with open(os.path.join(DOCS, "v31_session_check.json"), "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    print("SESSION 0.9.7: %d PASS / %d FAIL" % (ok, bad))
    if bad:
        sys.exit(1)


if __name__ == "__main__":
    main()
