"""内置默认编制（与 Obsidian 员工名册一致）：owner 回退与注册用户模板共用。"""

DEFAULT_AGENT_SPEC = [
    ("secretary", "小秘书", "小秘书（老板影子分身）", "all",
     ["metric.read", "report.render", "kb.read", "kb.search", "mail.read", "mail.send", "cron.write"],
     "全公司（代老板处理日常、巡检汇报与催办）"),
    ("cco", "沈青禾", "首席创意官", "all",
     ["style.guide", "copy.read", "asset.read", "kb.read", "mail.read", "mail.send"],
     "全公司 + 三项目创意"),
    ("ceo", "陆清安", "CEO", "all",
     ["project.read", "metric.read", "report.render", "kb.search", "kb.read", "mail.read", "mail.send"],
     "全公司"),
    ("cto", "扶频", "CTO", "all",
     ["repo.read", "deploy.read", "arch.review", "kb.write", "kb.read",
      "mcp.install", "plugin.install", "cron.write", "mail.read", "mail.send"],
     "全公司 + 产品三组"),
    ("pm_wenren", "贺平生", "产品经理", "wenren",
     ["hotspot.read", "metric.read", "backlog.write", "kb.read", "mail.read", "mail.send"], "产品一组"),
    ("pm_gou", "米珂", "产品经理", "gou",
     ["hotspot.read", "metric.read", "backlog.write", "kb.read", "mail.read", "mail.send"], "产品二组"),
    ("pm_tongxinyuan", "姜月初", "产品经理", "tongxinyuan",
     ["hotspot.read", "metric.read", "backlog.write", "kb.read", "mail.read", "mail.send"], "产品三组"),
    ("cw_wenren", "陆川", "配音", "wenren",
     ["kb.search", "style.guide", "copy.read", "media_tts", "media_job_status",
      "kb.read", "mail.read", "mail.send"], "产品一组"),
    ("cw_gou", "叶秋", "配音", "gou",
     ["kb.search", "style.guide", "copy.read", "media_tts", "media_job_status",
      "kb.read", "mail.read", "mail.send"], "产品二组"),
    ("cw_tongxinyuan", "江佑泽", "配音", "tongxinyuan",
     ["kb.search", "style.guide", "copy.read", "media_tts", "media_job_status",
      "kb.read", "mail.read", "mail.send"], "产品三组"),
    ("vd_wenren", "陆离", "视频制作", "wenren",
     ["asset.read", "video.render", "cover.gen", "media_tts", "media_asr",
      "media_job_status", "kb.read", "mail.read", "mail.send"], "产品一组"),
    ("vd_gou", "萧执", "视频制作", "gou",
     ["asset.read", "video.render", "cover.gen", "media_tts", "media_asr",
      "media_job_status", "kb.read", "mail.read", "mail.send"], "产品二组"),
    ("vd_tongxinyuan", "崔聿棠", "视频制作", "tongxinyuan",
     ["asset.read", "video.render", "cover.gen", "media_tts", "media_asr",
      "media_job_status", "kb.read", "mail.read", "mail.send"], "产品三组"),
    ("release_ops", "顾景琛", "发布与监控", "all",
     ["publish.execute", "metric.read", "alert.raise", "kb.read", "mail.read", "mail.send"], "全公司"),
    ("reviewer", "顾凛川", "审核", "all",
     ["review.check", "compliance.check", "copy.read", "media_asr", "media_tts",
      "media_job_status", "kb.read", "mail.read", "mail.send", "terminal"], "全公司"),
    ("scout", "苏寒", "热点采集", "all",
     ["hotspot.fetch", "asset.fetch", "asset.tag", "kb.read", "mail.read", "mail.send"], "全公司"),
    ("kb_manager", "祁南", "知识库管理", "all",
     ["kb.write", "kb.index", "kb.qa", "kb.read", "mail.read", "mail.send"], "全公司"),
    ("manual_scout", "灰太狼", "人工采集", "all",
     ["browser.login", "browser.collect", "web.fetch", "file.write", "kb.read", "mail.read", "mail.send"],
     "全公司"),
]

AGENTS_REQUIRED = {"pm_wenren", "pm_gou", "pm_tongxinyuan",
                   "cw_wenren", "cw_gou", "cw_tongxinyuan",
                   "vd_wenren", "vd_gou", "vd_tongxinyuan",
                   "scout", "reviewer", "kb_manager", "release_ops"}

# ---------------------------------------------------------------------------
# 默认员工姓名：短剧虚拟人物风格随机生成（仅注册新用户时用；存量名册不被改名）
# ---------------------------------------------------------------------------
import random

# 短剧常见姓氏与名字字池（霸总 / 古风 / 甜宠系角色风格）
_DRAMA_SURNAMES = ["顾", "陆", "沈", "傅", "霍", "江", "秦", "温", "萧", "厉",
                   "裴", "燕", "白", "洛", "慕", "靳", "凌", "容", "池", "纪"]
_DRAMA_GIVEN = ["晏辞", "时衍", "司寒", "聿深", "景琛", "南栀", "念卿", "梨落",
                "栀夏", "月棠", "云曦", "听澜", "观棋", "望舒", "既明", "扶摇",
                "知许", "青梧", "疏影", "惊鸿", "折枝", "映雪", "沉舟", "枕鹤",
                "听风", "踏歌", "归晚", "照野", "栖迟", "拾星"]


def random_agent_names(spec=None):
    """给编制里每个岗位随机取一个短剧风格的虚拟人物姓名（同一份名册内不重名）。"""
    spec = spec or DEFAULT_AGENT_SPEC
    combos = [(s, g) for s in _DRAMA_SURNAMES for g in _DRAMA_GIVEN]
    random.shuffle(combos)
    return [f"{s}{g}" for s, g in combos[:len(spec)]]
