import com.boray.voicemcp.Briefing;

/**
 * 纯 JVM 校验小秘书简报的解析：用服务端 boss_briefing 实样跑 Briefing.parse，
 * 检查「结论 / 进展 / 风险 / 待办」四段与审批指令是否都拆对了。
 */
public class VerifyBriefing {

    private static int pass = 0;
    private static int fail = 0;

    private static void check(String name, boolean ok) {
        if (ok) {
            pass++;
            System.out.println("  PASS  " + name);
        } else {
            fail++;
            System.out.println("  FAIL  " + name);
        }
    }

    public static void main(String[] args) {
        String detail = ""
                + "老板，巡检完了。以下是当前情况（只读 /opt/sefs/obsidian，来源都标在条目后面）。\n\n"
                + "一句话结论\n"
                + "全公司 0 条在制作——两批共 12 条定稿全卡在你这道开工审批上。\n\n"
                + "进展\n"
                + "- ceo 陆清安 04:07 主持第二轮评审会，9 条池定稿 6 条并发纪要（INBOX uid33）。\n"
                + "- cco 沈青禾 04:05 交 9 条入选池（uid90）。\n"
                + "- 三 PM 04:00 各交 4 条候选；文案／视频 6 人 04:00 后无动作记录。\n\n"
                + "风险与异常\n"
                + "- 连续空转：四轮都以「开工审批未通过」收口。\n"
                + "- 门禁顺序倒置：上轮已派单，审批才 timeout。\n\n"
                + "需要你拍板（急件置顶）\n"
                + "【急】1. 开工放行哪批？A 只批 04:07 这批 6 条；B 只批上轮 6 条；C 12 条并批；D 继续挂起。\n"
                + " 建议 A。不决策的后果：产能继续空烧，产出仍是 0。\n"
                + "【急】2. sherlock 条合规三选一，须你书面选一个。A 题面原样；B 隐去仓库名；C 改通用版。\n"
                + " 建议 A（CTO 唯一推荐项）。不决策：该条永远 blocked。\n"
                + "【缓】3. 制度口径互斥：岗位职责写 3 条，实际按 4→12 跑。\n"
                + " 建议授权 ceo 统一文本。不决策：每轮临时裁定。";

        Briefing b = Briefing.parse(detail);
        check("一句话结论非空", b.summary.contains("0 条在制作"));
        check("进展至少 3 条", b.progress.size() >= 3);
        check("风险至少 2 条", b.risks.size() >= 2);
        check("待办 3 条", b.todos.size() == 3);
        if (b.todos.size() == 3) {
            Briefing.Todo t1 = b.todos.get(0);
            check("第 1 条是急件", t1.urgent());
            check("第 1 条标题去掉编号", t1.title.startsWith("开工放行哪批"));
            check("第 1 条抠出建议 A", t1.suggestion.contains("A"));
            check("第 1 条保留不决策后果", t1.detail.contains("空烧"));
            Briefing.Todo t3 = b.todos.get(2);
            check("第 3 条是缓件", !t3.urgent());
            check("第 3 条建议是「授权 ceo 统一文本」",
                    t3.suggestion.contains("授权"));
        }
        Briefing.Todo probe = new Briefing.Todo();
        probe.title = "开工放行哪批";
        probe.suggestion = "建议 A";
        check("通过指令含「批准」与选项", probe.command(true).contains("批准")
                && probe.command(true).contains("A"));
        check("不通过指令含「不批准」与挂起", probe.command(false).contains("不批准")
                && probe.command(false).contains("挂起"));

        // 退化场景：只有一段自由文本时不能崩，尽量给出一个待办
        Briefing raw = Briefing.parse("需要你拍板\n先看看这批怎么放。");
        check("缺段落时不崩且能兜底", raw != null);
        Briefing empty = Briefing.parse("");
        check("空输入 empty()=true", empty.empty());

        System.out.println("VERIFY BRIEFING: " + pass + " PASS / " + fail + " FAIL");
        if (fail > 0) System.exit(1);
    }
}
