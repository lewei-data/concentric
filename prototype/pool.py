"""用户内核池（C1 拆分）：按工作目录取/建 Kernel，超过上限逐出空闲内核。

每个内核自带 Store 写线程、审批巡检线程和全量 vault 缓存，
用户多了只增不减会拖垮进程，所以池必须有上限与逐出策略。

与 kernel.py 的循环依赖用"函数内延迟导入"解决：本模块顶层不认识 Kernel，
kernel 侧也不需要认识池，只有 server/deps 侧需要。
"""
import os
import threading
import time

from deps import DBX
from obs import log_event
from opc_paths import OWNER_DATA_DIR

KERNELS = {}
KERNELS_LOCK = threading.Lock()
KERNELS_AT = {}                   # 用户内核最近访问时间，供池上限逐出用
KERNEL_POOL_MAX = max(1, int(os.environ.get("OPC_KERNEL_POOL", "8") or 8))


def _kernel_for(data_dir):
    """取/建某用户工作区的内核；owner 目录的内核在 boot 时即创建。"""
    from kernel import Kernel
    data_dir = os.path.abspath(data_dir or OWNER_DATA_DIR)
    evicted, k = [], None
    with KERNELS_LOCK:
        k = KERNELS.get(data_dir)
        if k is None:
            try:
                k = Kernel(data_dir=data_dir)
            except Exception as e:
                log_event("pool.kernel_create", e, level="error", dir=data_dir)
                raise
            KERNELS[data_dir] = k
            # 超上限就逐出最久未访问、且当前没在跑任务的非 owner 内核
            if len(KERNELS) > KERNEL_POOL_MAX:
                idle = sorted(((KERNELS_AT.get(d, 0.0), d) for d in KERNELS
                               if d != OWNER_DATA_DIR and not KERNELS[d].active_trace),
                              key=lambda x: x[0])
                for _, d in idle[:len(KERNELS) - KERNEL_POOL_MAX]:
                    evicted.append((d, KERNELS.pop(d, None)))
                    KERNELS_AT.pop(d, None)
        KERNELS_AT[data_dir] = time.time()
    # 停线程/关连接放锁外，避免阻塞其它用户的请求
    for d, old in evicted:
        try:
            if old:
                old.stop()
            if DBX:
                DBX.drop_store(os.path.join(d, "opc.db"))
            print(f"[pool] 超上限（{KERNEL_POOL_MAX}），逐出空闲内核 {d}", flush=True)
        except Exception as e:
            log_event("pool.evict", e, level="warn", dir=d)
    return k


def _kernel_reset(data_dir):
    """数据导入后重置内核：停后台线程、丢 Store 缓存，下次访问按新数据重建。"""
    data_dir = os.path.abspath(data_dir)
    with KERNELS_LOCK:
        k = KERNELS.pop(data_dir, None)
        KERNELS_AT.pop(data_dir, None)
    if k:
        try:
            k.stop()
        except Exception as e:
            log_event("pool.reset_stop", e, level="warn", dir=data_dir)
    if DBX:
        DBX.drop_store(os.path.join(data_dir, "opc.db"))
    return _kernel_for(data_dir)


def pool_stats():
    """池现状：给 /api/ops 与自检脚本用。"""
    with KERNELS_LOCK:
        return {"size": len(KERNELS), "max": KERNEL_POOL_MAX,
                "dirs": sorted(KERNELS)}