import sys, time, traceback
sys.path.insert(0, "/opt/sefs/opc-workbench/mcp-asr")
import server

ids = [i for i in server.load_profiles()]
print("total", len(ids), ids, flush=True)
ok, fail = [], []
for i, aid in enumerate(ids):
    t0 = time.time()
    try:
        r = server.intro_build(aid, force=True, refresh_text=False)
        print("[%d/%d] OK %s %.0fs src=%s" % (i + 1, len(ids), aid, time.time() - t0, r.get("source")), flush=True)
        ok.append(aid)
    except Exception as e:
        print("[%d/%d] FAIL %s: %s" % (i + 1, len(ids), aid, e), flush=True)
        traceback.print_exc()
        fail.append(aid)
print("DONE ok=%d fail=%d %s" % (len(ok), len(fail), fail), flush=True)
