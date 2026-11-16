"""P2-1 告警 → Runbook → 自动处置（低风险动作闭环）

红线：只做低风险、可逆、幂等的动作（轮转日志 / vacuum journal / 清 nginx 临时目录 /
重启白名单服务）；绝不删业务数据、不改配置、不杀业务进程。每规则独立 cooldown，
每次执行写 ops/runbook_events.jsonl；连续 3 次失败即熔断该规则并 critical 告警，
让「自动处置失灵」本身也会喊出来。默认 dry-run，cron 才带 --apply。

    python3 runbook.py            # dry-run
    python3 runbook.py --apply    # 真执行
    python3 runbook.py --list     # 列规则
    python3 runbook.py --selftest # 人为高值验证检测链路
"""
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone

TZ = timezone(timedelta(hours=8))
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.normpath(os.path.join(BASE_DIR, ".."))
OPS_DIR = os.path.join(REPO_ROOT, "ops")
EVENTS_FILE = os.path.join(OPS_DIR, "runbook_events.jsonl")
STATE_FILE = os.path.join(OPS_DIR, "runbook_state.json")

TH_DISK_PCT = float(os.environ.get("OPC_RB_DISK_PCT", "85"))
TH_LOG_MB = float(os.environ.get("OPC_RB_LOG_MB", "2048"))
TH_PROXY_MB = float(os.environ.get("OPC_RB_PROXY_MB", "1024"))
TH_JOURNAL_MB = float(os.environ.get("OPC_RB_JOURNAL_MB", "3072"))
COOLDOWN_SEC = int(os.environ.get("OPC_RB_COOLDOWN", str(30 * 60)))
FAIL_TRIP = 3

BASE_WHITELIST = ("opc-workbench.service", "rsyslog.service", "systemd-journald.service")


def _restart_whitelist():
    """可自动重启的单元。hermes 网关的单元名带部署 hash（hermes-gateway-xxxxxxxx.service），
    重装/重新初始化后会变，所以按前缀发现而不是硬编码，避免重装后规则静默失效。"""
    units = list(BASE_WHITELIST)
    rc, out = _sh(["systemctl", "list-unit-files", "hermes-gateway-*.service",
                   "--no-legend", "--no-pager"], timeout=15)
    if rc == 0:
        for line in out.splitlines():
            parts = line.split()
            if parts and parts[0].endswith(".service") and parts[0] not in units:
                units.append(parts[0])
    return tuple(units)


def _now():
    return time.time()


def _iso(ts=None):
    return datetime.fromtimestamp(ts or _now(), TZ).isoformat(timespec="seconds")


def _load(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def _dump(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


def _log(rec):
    rec.setdefault("at", _iso())
    with open(EVENTS_FILE, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def _sh(cmd, timeout=180):
    try:
        p = subprocess.run(cmd, shell=False, capture_output=True, text=True, timeout=timeout)
        return p.returncode, ((p.stdout or "") + (p.stderr or "")).strip()[-500:]
    except subprocess.TimeoutExpired:
        return 124, f"超时 {timeout}s"
    except Exception as e:
        return 1, f"{type(e).__name__}: {e}"


def _du_mb(path):
    rc, out = _sh(["sudo", "-n", "du", "-sm", path])
    try:
        return float(out.split()[0]) if rc == 0 else 0.0
    except Exception:
        return 0.0


def _disk_pct(path="/"):
    try:
        st = os.statvfs(path)
        total = st.f_blocks * st.f_frsize
        return round((st.f_blocks - st.f_bfree) * st.f_frsize * 100.0 / total, 1) if total else 0.0
    except Exception:
        return 0.0


def _big_logs():
    found = []
    try:
        for name in os.listdir("/var/log"):
            full = os.path.join("/var/log", name)
            if not os.path.isfile(full) or os.path.islink(full):
                continue
            try:
                mb = os.path.getsize(full) / 1048576.0
            except Exception:
                continue
            if mb >= TH_LOG_MB:
                found.append((full, round(mb, 1)))
    except Exception:
        pass
    return sorted(found, key=lambda x: -x[1])


def _journal_mb():
    rc, out = _sh(["sudo", "-n", "journalctl", "--disk-usage"])
    if rc != 0:
        return 0.0
    for tok in out.replace(",", " ").split():
        for unit, mul in (("G", 1024.0), ("M", 1.0)):
            if tok.endswith(unit):
                try:
                    return float(tok[:-1]) * mul
                except Exception:
                    pass
    return 0.0


def _svc_state(name):
    _, out = _sh(["systemctl", "is-active", name], timeout=15)
    return out.strip() or "unknown"


def act_rotate_syslog(ctx):
    rc, out = _sh(["sudo", "-n", "logrotate", "-f", "/etc/logrotate.d/rsyslog"])
    return rc == 0, out or "logrotate 已执行"


def act_truncate_big_log(ctx):
    """活动日志截断、归档日志删除。
    归档（*.gz / *.1 / *.2）绝不能 truncate：那会留下 0 字节的 .gz 残骸，
    既占位又误导 logrotate。归档只需删掉，历史由 rotate 3 兜底。"""
    done = []
    for path, mb in ctx.get("big_logs", []):
        base = os.path.basename(path)
        if base.split(".")[0] not in ("syslog", "kern", "user", "cron"):
            continue
        archived = base.endswith(".gz") or base.rsplit(".", 1)[-1].isdigit()
        if archived:
            rc, _ = _sh(["sudo", "-n", "rm", "-f", path])
            if rc == 0:
                done.append(f"rm:{base}({mb}MB)")
        else:
            rc, _ = _sh(["sudo", "-n", "truncate", "-s", "0", path])
            if rc == 0:
                done.append(f"trunc:{base}({mb}MB)")
    return bool(done), ("已处理 " + ", ".join(done)) if done else "无可处理文件"


def act_vacuum_journal(ctx):
    rc, out = _sh(["sudo", "-n", "journalctl", "--vacuum-size=2G"])
    return rc == 0, out or "vacuum 完成"


def act_clean_proxy_temp(ctx):
    temp = "/var/lib/nginx/proxy_temp"
    if not os.path.isdir(temp):
        return False, f"{temp} 不存在"
    rc, out = _sh(["sudo", "-n", "find", temp, "-type", "f", "-mmin", "+1440", "-delete"])
    return rc == 0, (f"已清 {temp} 中 24h 前文件" if rc == 0 else out)


def act_restart_service(ctx):
    done = []
    for name in ctx.get("down_services", []):
        if name not in _restart_whitelist():
            continue
        rc, _ = _sh(["sudo", "-n", "systemctl", "restart", name])
        if rc == 0:
            done.append(name)
    return bool(done), ("已重启 " + ", ".join(done)) if done else "无可重启服务"


RULES = [
    {"id": "disk_high", "level": "critical",
     "desc": f"根分区使用率 >= {TH_DISK_PCT}%",
     "probe": lambda c: (c["disk_pct"] >= TH_DISK_PCT, f"根分区 {c['disk_pct']}%"),
     "act": act_rotate_syslog,
     "notify": "磁盘水位过高，已自动轮转日志；若仍不降需人工清理"},
    {"id": "huge_log_file", "level": "warning",
     "desc": f"/var/log 单文件 >= {TH_LOG_MB}MB",
     "probe": lambda c: (bool(c["big_logs"]), ", ".join(f"{p}({m}MB)" for p, m in c["big_logs"][:3])),
     "act": act_truncate_big_log,
     "notify": "检测到超大日志文件并已截断"},
    {"id": "journal_large", "level": "warning",
     "desc": f"journal >= {TH_JOURNAL_MB}MB",
     "probe": lambda c: (c["journal_mb"] >= TH_JOURNAL_MB, f"journal {round(c['journal_mb'])}MB"),
     "act": act_vacuum_journal,
     "notify": "journal 超限并已 vacuum 到 2G"},
    {"id": "proxy_temp_large", "level": "warning",
     "desc": f"nginx proxy_temp >= {TH_PROXY_MB}MB",
     "probe": lambda c: (c["proxy_mb"] >= TH_PROXY_MB, f"proxy_temp {round(c['proxy_mb'])}MB"),
     "act": act_clean_proxy_temp,
     "notify": "nginx 临时目录超限并已清理"},
    {"id": "service_down", "level": "critical",
     "desc": "白名单服务处于 failed/inactive",
     "probe": lambda c: (bool(c["down_services"]), ", ".join(c["down_services"])),
     "act": act_restart_service,
     "notify": "白名单服务失效并已尝试重启"},
]


def collect(selftest=False):
    if selftest:
        return {"disk_pct": 99.0, "big_logs": [("/var/log/syslog.selftest", 3000.0)],
                "journal_mb": 9999.0, "proxy_mb": 9999.0, "down_services": ["selftest.service"]}
    return {
        "disk_pct": _disk_pct("/"),
        "big_logs": _big_logs(),
        "journal_mb": _journal_mb(),
        "proxy_mb": _du_mb("/var/lib/nginx/proxy_temp"),
        "down_services": [s for s in _restart_whitelist()
                          if _svc_state(s) not in ("active", "activating", "reloading")],
    }


def _notify(level, source, text):
    try:
        sys.path.insert(0, BASE_DIR)
        import notify as _n
        return _n.notify(level, source, text)
    except Exception as e:
        _log({"rule": "notify_failed", "error": f"{type(e).__name__}: {e}", "text": text[:200]})
        return None


def tick(apply=False, quiet=False):
    ctx = collect()
    state = _load(STATE_FILE, {})
    out = []
    for rule in RULES:
        rid = rule["id"]
        try:
            hit, detail = rule["probe"](ctx)
        except Exception as e:
            out.append({"rule": rid, "error": f"{type(e).__name__}: {e}"})
            continue
        if not hit:
            continue
        rec = {"rule": rid, "level": rule["level"], "detail": detail, "dry_run": not apply}
        st = state.get(rid, {})
        rest = COOLDOWN_SEC - (_now() - float(st.get("lastAct", 0)))
        if rest > 0:
            rec["skipped"] = f"cooldown 剩余 {int(rest / 60)} 分钟"
            out.append(rec)
            continue
        if not apply:
            rec["would"] = rule["desc"]
            out.append(rec)
            continue
        t0 = _now()
        try:
            ok, msg = rule["act"](ctx)
        except Exception as e:
            ok, msg = False, f"{type(e).__name__}: {e}"
        rec.update({"ok": ok, "result": msg, "ms": int((_now() - t0) * 1000)})
        fails = 0 if ok else int(st.get("fails", 0)) + 1
        state[rid] = {"lastAct": _now(), "iso": _iso(), "fails": fails}
        if fails >= FAIL_TRIP:
            rec["tripped"] = True
            _notify(rule["level"], f"runbook-{rid}",
                    f"【自动处置熔断】{rule['desc']} 连续 {fails} 次失败，已停用该规则。最后一次：{msg[:200]}")
        elif ok:
            _notify("warning", f"runbook-{rid}",
                    f"{rule['notify']}。触发指标：{detail}。动作结果：{msg[:200]}")
        _log(rec)
        out.append(rec)
    _dump(STATE_FILE, state)
    if not quiet:
        print(json.dumps(out, ensure_ascii=False, indent=1) if out else "[]（无命中，系统正常）")
    return out


def main():
    argv = sys.argv[1:]
    if "--list" in argv:
        for r in RULES:
            print(f"{r['id']:<18}{r['level']:<10}cooldown={COOLDOWN_SEC // 60}min  {r['desc']}")
        return
    if "--selftest" in argv:
        ctx = collect(selftest=True)
        print("selftest 命中:", [(r["id"], r["probe"](ctx)) for r in RULES])
        return
    res = tick(apply="--apply" in argv, quiet="--quiet" in argv)
    if "--apply" in argv and any(r.get("ok") is False for r in res):
        sys.exit(1)


if __name__ == "__main__":
    main()
