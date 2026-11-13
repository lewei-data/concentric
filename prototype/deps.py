"""基础设施单例：过程库/摄取/交付物/作品档案/渲染/补素材/聚合面板。

按环境整体降级（缺依赖时为 None，不影响主流程）；server 与 kernel 共同引用。
"""
import os

try:
    import db as DBX
    import ingest as ING
except Exception as _db_err:
    DBX = None
    ING = None
    print(f"[boot] 过程数据入库不可用：{type(_db_err).__name__}: {_db_err}", flush=True)
DB_ON = bool(DBX and DBX.ENABLED)
INGEST_ON = DB_ON and os.environ.get("OPC_INGEST", "0") == "1"

try:
    import deliverables as DL
except Exception as _dl_err:
    DL = None
    print(f"[boot] 交付物落盘不可用：{type(_dl_err).__name__}: {_dl_err}", flush=True)

try:
    import works as WK
except Exception as _wk_err:
    WK = None
    print(f"[boot] 作品档案不可用：{type(_wk_err).__name__}: {_wk_err}", flush=True)

try:
    import om_render as OMR
except Exception as _om_err:
    OMR = None
    print(f"[boot] 真实渲染不可用：{type(_om_err).__name__}: {_om_err}", flush=True)

try:
    import asset_fill
except Exception as _af_err:
    asset_fill = None
    print(f"[boot] 补素材模块不可用：{type(_af_err).__name__}: {_af_err}", flush=True)

try:
    import insights
    import ops_monitor
except Exception as _panel_err:
    insights = ops_monitor = None
    print(f"[boot] 灵感/运维聚合不可用：{type(_panel_err).__name__}: {_panel_err}", flush=True)

__all__ = ["DBX", "ING", "DB_ON", "INGEST_ON", "DL", "WK", "OMR", "asset_fill",
           "insights", "ops_monitor"]
