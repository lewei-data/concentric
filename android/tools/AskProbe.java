import com.boray.voicemcp.DispatchApi;

/**
 * 真实链路联测：验证「HLS 兜底」用的 passthrough 入口（不建 WS）能拿到结果。
 *
 * 用法：java AskProbe "现在几点了？一句话回答"
 * 判定：提交拿到 task_id（channel_kind=passthrough），/task/<id> 轮询到 done 且有 result。
 */
public class AskProbe {

    public static void main(String[] args) throws Exception {
        String text = args.length > 0 ? args[0] : "现在几点了？用一句话回答";
        long t0 = System.currentTimeMillis();
        DispatchApi.Submit s = DispatchApi.submit(null, "secretary", "", text, "voice", null);
        System.out.println("SUBMIT ok=" + s.ok + " task=" + s.taskId
                + " kind=" + s.channelKind + " msg=" + s.message);
        if (!s.ok) System.exit(1);

        DispatchApi.Task t = null;
        for (int i = 0; i < 60; i++) {
            t = DispatchApi.task(null, s.taskId);
            if (t.finished()) break;
            Thread.sleep(1000);
        }
        long ms = System.currentTimeMillis() - t0;
        System.out.println("STATUS " + (t == null ? "?" : t.status)
                + " in " + ms + "ms");
        if (t != null && t.result != null && !t.result.isEmpty()) {
            String r = t.result.replace('\n', ' ');
            System.out.println("RESULT " + (r.length() > 300 ? r.substring(0, 300) + "…" : r));
        }
        boolean ok = t != null && "done".equals(t.status)
                && t.result != null && !t.result.trim().isEmpty();
        System.out.println((ok ? "PASS" : "FAIL") + " passthrough 入口");
        System.exit(ok ? 0 : 1);
    }
}
