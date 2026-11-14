"""内置默认编制（与 Obsidian 员工名册一致）：owner 回退与注册用户模板共用。"""

DEFAULT_AGENT_SPEC = [
    ("secretary", "小秘书", "小秘书（老板影子分身）", "all",
     ["metric.read", "report.render", "kb.read", "kb.search", "mail.read", "mail.send",
      "cron.write", "browser_exec"],
     "全公司（代老板处理日常、巡检汇报与催办；消息中心可操控浏览器查资料）"),
    ("cco", "沈青禾", "首席创意官", "all",
     ["style.guide", "style.write", "copy.read", "copy.write", "asset.read", "asset.write",
      "kb.read", "kb.write", "kb.audit", "kb.benchmark", "kb.retire",
      "web.research", "skill.evolve", "trend.analyze", "audience.insight",
      "mail.read", "mail.send", "cron.write"],
     "全公司 + 三项目创意；五行学习：创意资产盘点、创意趋势对标、风格迭代进化；"
     "职责：内容调性把关、风格指南制定与迭代、创意评审、受众洞察、趋势预测、创意团队能力提升"),
    ("ceo", "陆清安", "CEO", "all",
     ["project.read", "project.write", "metric.read", "metric.write", "report.render",
      "kb.search", "kb.read", "kb.write", "kb.audit", "kb.benchmark", "kb.retire",
      "web.research", "skill.evolve", "strategy.plan", "okr.track", "risk.assess",
      "cron.write", "browser_exec", "mail.read", "mail.send"],
     "全公司；五行学习：战略知识库盘点、行业对标、技能淘汰与升级；"
     "职责：战略规划与OKR管理、跨项目资源调度、风险评估与管控、管理层绩效审视、"
     "知识库健康度治理、行业趋势洞察、文化建设与团队能力提升"),
    ("cto", "扶频", "CTO", "all",
     ["repo.read", "repo.write", "deploy.read", "deploy.write", "arch.review",
      "kb.write", "kb.read", "kb.audit", "kb.benchmark", "kb.retire",
      "web.research", "skill.evolve", "security.audit", "perf.monitor", "cost.optimize",
      "mcp.install", "plugin.install", "cron.write", "alert.write",
      "mail.read", "mail.send"],
     "全公司 + 产品三组；五行学习：技术栈审计、方案对标、技能迭代、知识库体系维护；"
     "职责：架构设计与演进、技术栈保鲜、安全审计与合规、性能监控与优化、"
     "技术债务管理、成本控制、自动化流水线、技术团队能力提升、技术 roadmap 制定"),
    ("pm_wenren", "贺平生", "产品经理", "wenren",
     ["hotspot.read", "metric.read", "backlog.write", "kb.read", "kb.write",
      "script.write", "scene_plan.write", "storyboard.write",
      "terminal", "file.write", "mail.read", "mail.send"],
     "产品一组；职责：剧本与分镜编写、产线推进、派单管理"),
    ("pm_gou", "米珂", "产品经理", "gou",
     ["hotspot.read", "metric.read", "backlog.write", "kb.read", "kb.write",
      "script.write", "scene_plan.write", "storyboard.write",
      "terminal", "file.write", "mail.read", "mail.send"],
     "产品二组；职责：剧本与分镜编写、产线推进、派单管理"),
    ("pm_tongxinyuan", "姜月初", "产品经理", "tongxinyuan",
     ["hotspot.read", "metric.read", "backlog.write", "kb.read", "kb.write",
      "script.write", "scene_plan.write", "storyboard.write",
      "terminal", "file.write", "mail.read", "mail.send"],
     "产品三组；职责：剧本与分镜编写、产线推进、派单管理"),
    ("cw_wenren", "陆川", "配音", "wenren",
     ["kb.search", "style.guide", "copy.read", "media_tts", "media_asr",
      "media_job_status", "subtitle.write", "file.write", "terminal",
      "kb.read", "mail.read", "mail.send"],
     "产品一组；职责：配音合成、字幕生成（含安全区定位）、BGM 配合"),
    ("cw_gou", "叶秋", "配音", "gou",
     ["kb.search", "style.guide", "copy.read", "media_tts", "media_asr",
      "media_job_status", "subtitle.write", "file.write", "terminal",
      "kb.read", "mail.read", "mail.send"],
     "产品二组；职责：配音合成、字幕生成（含安全区定位）、BGM 配合"),
    ("cw_tongxinyuan", "江佑泽", "配音", "tongxinyuan",
     ["kb.search", "style.guide", "copy.read", "media_tts", "media_asr",
      "media_job_status", "subtitle.write", "file.write", "terminal",
      "kb.read", "mail.read", "mail.send"],
     "产品三组；职责：配音合成、字幕生成（含安全区定位）、BGM 配合"),
    ("vd_wenren", "陆离", "视频制作", "wenren",
     ["asset.read", "asset.write", "video.render", "cover.gen", "image.gen",
      "media_tts", "media_asr", "media_job_status", "file.write", "terminal",
      "kb.read", "mail.read", "mail.send"],
     "产品一组；职责：素材生成（图片/视频）、剪辑合成、渲染出片、细节把控"),
    ("vd_gou", "萧执", "视频制作", "gou",
     ["asset.read", "asset.write", "video.render", "cover.gen", "image.gen",
      "media_tts", "media_asr", "media_job_status", "file.write", "terminal",
      "kb.read", "mail.read", "mail.send"],
     "产品二组；职责：素材生成（图片/视频）、剪辑合成、渲染出片、细节把控"),
    ("vd_tongxinyuan", "崔聿棠", "视频制作", "tongxinyuan",
     ["asset.read", "asset.write", "video.render", "cover.gen", "image.gen",
      "media_tts", "media_asr", "media_job_status", "file.write", "terminal",
      "kb.read", "mail.read", "mail.send"],
     "产品三组；职责：素材生成（图片/视频）、剪辑合成、渲染出片、细节把控"),
    ("release_ops", "顾景琛", "发布与监控", "all",
     ["publish.execute", "metric.read", "alert.raise", "kb.read", "mail.read", "mail.send"], "全公司"),
    ("reviewer", "顾凛川", "审核", "all",
     ["review.check", "compliance.check", "copy.read", "media_asr", "media_tts",
      "media_job_status", "file.write", "kb.read", "mail.read", "mail.send", "terminal"],
     "全公司；职责：终审审核、素材质量检查、音画同步验证、合规审查"),
    ("scout", "苏寒", "热点采集", "all",
     ["hotspot.fetch", "asset.fetch", "asset.tag", "kb.read", "mail.read", "mail.send"], "全公司"),
    ("kb_manager", "祁南", "知识库管理", "all",
     ["kb.read", "kb.search", "kb.write", "kb.index", "kb.qa", "kb.audit", "kb.retire",
      "asset.read", "asset.audit", "report.render", "file.write", "terminal",
      "cron.write", "mail.read", "mail.send"],
     "全公司；职责：知识库条目创建/更新、分类打标、索引与检索问答、归档登记与退役标记、"
     "重复合并与孤儿索引修复；资产盘点：每日凌晨自动盘点全部数据库/表结构/数据量、"
     "产品与工厂产出、素材库与知识库规模，发现数据与流程问题，输出《资产盘点日报》并邮件报老板；"
     "知识质量治理：一致性核查、缺口预警、异常升级"),
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
