"""Structured planning and evidence proposals. The backend owns the final records."""
from typing import Annotated, Literal
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    PositiveInt,
    ValidationError,
    field_validator,
    model_validator,
)
from .world_catalog import PATHS, qualifier_terms

Role = Literal[
    "news", "people", "organizations", "opinion", "related", "specialist", "counter", "dependencies"
]
ROLE_NAMES = {
    "news": "新闻与多模态",
    "people": "人物",
    "organizations": "组织 / 国家",
    "opinion": "社会舆情",
    "related": "关联事件",
    "specialist": "专项研究",
    "counter": "反证研究",
    "dependencies": "预测依赖研究",
}
SECTIONS = {
    "background": "事件背景",
    "people": "人物与组织",
    "opinion": "社会舆情",
    "news": "新闻与多模态",
    "timeline": "时间线",
    "index": "知识索引",
}
ROLE_INSTRUCTIONS = {
    "news": "追溯首发，提取公告、新闻与可定位媒体的直接事实，区分发生和报道时刻。转载不是新的独立事实。",
    "people": "跟踪核心人物、相关公众人物和意见领袖的公开身份、立场和近期公开行动。同名者按可证实的职务或机构消歧。仅在有直接公开依据且与事件有关时记录家庭或利益关系。目的或行为解释属于假设，写出替代解释及反证条件。",
    "organizations": "区分组织、国家及具体主管部门，梳理利益和关系方向。分别记录政策草案、通过和生效，不混淆权限。",
    "opinion": "注明平台、时间段、样本数、去重口径、引文及局限。区分报道、发言与公众观点。不可从选择样本推及总体，口径可比才谈趋势。",
    "related": "从输入的相关事件和共享实体检验上下游依赖、共同原因和变化。记录方向、机制、反证与置信理由；单纯共现只作交叉引用。",
    "specialist": "定向调查本轮指定知识盲区。可查历史。记录原始依据、仍缺哪条证据和结束条件。",
    "counter": "主动找能推翻当前判断的独立证据，区分真正反例与弱反例。未找到时报告已经核查的范围。",
    "dependencies": "对开放预测的假设、观察指标、阈值与中间事件逐题判断影响。区分普通进展与满足原题结算标准。",
}


class Record(BaseModel):
    model_config = ConfigDict(extra="forbid")


class KnowledgeQuery(Record):
    query: str = Field(default="", max_length=200)
    path: str = Field(default="", max_length=100)
    entity_id: int | None = Field(default=None, ge=1)
    entry_id: int | None = Field(default=None, ge=1)
    fields: list[str] = Field(default_factory=list, max_length=8)
    qualifiers: dict[str, str | int | float] = Field(default_factory=dict, max_length=4)

    @field_validator("path")
    @classmethod
    def known_path(cls, value):
        if value and value not in PATHS:
            raise ValueError("知识目录路径未注册")
        return value

    @model_validator(mode="after")
    def known_qualifiers(self):
        qualifier_terms(self.path, self.qualifiers)
        return self


class Guide(Record):
    question: str = Field(min_length=2, max_length=600)
    reason: str = Field(min_length=2, max_length=400)
    wake: str = Field(min_length=2, max_length=400)
    lookup: KnowledgeQuery


class Assignment(Record):
    role: Role
    question: str = Field(min_length=1, max_length=600)
    target: str = Field(max_length=300)
    time_scope: Literal["window", "history"]
    done_when: str = Field(max_length=500)
    priority: int = Field(ge=1, le=5)
    search_query: str = Field(default="", max_length=250)
    knowledge_query: KnowledgeQuery | None = None
    line: str = Field(default="", max_length=120)
    review_checks: list[Annotated[int, Field(ge=1, le=16)]] = Field(default_factory=list, max_length=16)


class Monitor(Record):
    url: str = Field(min_length=1, max_length=2048)
    reason: str = Field(min_length=2, max_length=400)


class KnowledgeLink(Record):
    model_config = ConfigDict(str_strip_whitespace=True)
    entry_id: int = Field(ge=1)
    version_id: int = Field(ge=1)
    section: Literal["", "background", "people", "opinion", "news"] = ""
    reason: str = Field(min_length=2, max_length=400)


class Citation(Record):
    fragment_id: int
    quote: str = Field(min_length=2)


class PersonLead(Record):
    name: str = Field(min_length=2, max_length=100)
    identity: str = Field(min_length=2, max_length=200)
    role: Literal["核心人物", "参与者", "公开发言人", "专家", "意见领袖"]
    relevance: str = Field(min_length=3, max_length=400)
    citation: Citation
    identity_quote: str = Field(default="", max_length=250)
    aliases: list[str] = Field(default_factory=list, max_length=4)


class Plan(Record):
    rationale: str = Field(max_length=1200)
    tasks: list[Assignment] = Field(max_length=8)
    postponed: list[str] = Field(default_factory=list, max_length=10)
    watch: list[Monitor] = Field(default_factory=list, max_length=6)
    source_queries: list[str] = Field(default_factory=list, max_length=2)
    use_knowledge: list[KnowledgeLink] = Field(default_factory=list, max_length=8)
    release_knowledge: list[KnowledgeLink] = Field(default_factory=list, max_length=8)
    person_leads: list[PersonLead] = Field(default_factory=list, max_length=4)
    guides: list[Guide] = Field(default_factory=list, max_length=8)
    retire_guides: list[int] = Field(default_factory=list, max_length=8)
    checklist: list[Annotated[str, Field(min_length=2, max_length=400)]] = Field(
        default_factory=list, max_length=16
    )

    @model_validator(mode="after")
    def align_review_positions(self):
        submitted = self.checklist or [task.question for task in self.tasks]
        positions = {question: pos for pos, question in enumerate(dict.fromkeys(submitted), 1)}
        for task in self.tasks:
            unknown = [pos for pos in task.review_checks if pos > len(submitted)]
            if unknown:
                raise ValueError(f"调查任务引用了不存在的交付清单序号 {unknown}")
            task.review_checks = list(dict.fromkeys(positions[submitted[i - 1]] for i in task.review_checks))
        if self.checklist or any(task.review_checks for task in self.tasks):
            self.checklist = list(positions)
        return self

    @model_validator(mode="before")
    @classmethod
    def keep_research_when_optional_lookup_is_invalid(cls, proposal):
        if not isinstance(proposal, dict):
            return proposal
        proposal = dict(proposal)
        gaps = list(proposal.get("postponed") or [])
        if isinstance(proposal.get("guides"), list):
            kept = []
            for guide in proposal["guides"]:
                try:
                    kept.append(Guide.model_validate(guide))
                except ValidationError:
                    if len(gaps) < 10:
                        gaps.append("一项指南的目录或字段不适用，暂缓该查阅规则")
            proposal["guides"] = kept
        if isinstance(proposal.get("tasks"), list):
            tasks = []
            for task in proposal["tasks"]:
                if isinstance(task, dict) and task.get("knowledge_query") is not None:
                    try:
                        KnowledgeQuery.model_validate(task["knowledge_query"])
                    except ValidationError:
                        if len(gaps) < 10:
                            gaps.append(f"{str(task.get('question',''))[:100]}的世界知识查询未通过校验，保留原始材料调查")
                        task = {**task, "knowledge_query": None}
                tasks.append(task)
            proposal["tasks"] = tasks
        return {**proposal, "postponed": gaps}


class Finding(Record):
    section: Literal["background", "people", "opinion", "news"]
    kind: Literal["fact", "reported", "view", "hypothesis", "relation", "correction", "conflict"]
    subject: str = Field(default="", max_length=250)
    text: str = Field(default="", max_length=1600)
    citations: list[Citation] = Field(min_length=1, max_length=6)
    occurred_at: str = ""
    time_quote: str = ""
    entity_kind: Literal["", "person", "organization", "country"] = ""
    category: Literal[
        "",
        "people",
        "organizations",
        "places",
        "policies",
        "indicators",
        "events",
        "opinion",
        "relations",
        "analysis",
        "background",
    ] = ""
    identity: str = Field(default="", max_length=250)
    aliases: list[str] = Field(default_factory=list, max_length=5)
    reason: str = Field(default="", max_length=800)
    replaces: int = 0
    supports: int = 0
    facets: dict[str, str | int] = Field(default_factory=dict, max_length=12)
    directory: str = Field(default="", max_length=100)
    attributes: dict[str, str | int | float] = Field(default_factory=dict, max_length=12)
    scope: Literal["auto", "world", "event"] = "auto"
    field_citations: dict[str, Citation] = Field(default_factory=dict, max_length=12)

    @field_validator("field_citations", mode="before")
    @classmethod
    def normalize_field_evidence(cls, value):
        if isinstance(value, list):
            fields = {}
            for item in value:
                if not isinstance(item, dict) or not isinstance(item.get("field"), str):
                    raise ValueError("字段依据需要字段名和引文")
                name = item["field"]
                if name in fields:
                    raise ValueError("同一字段有多个无法区分的引文")
                fields[name] = {k: v for k, v in item.items() if k != "field"}
            value = fields
        if isinstance(value, dict):
            return {k: v[0] if isinstance(v, list) and len(v) == 1 else v for k, v in value.items()}
        return value

    @model_validator(mode="after")
    def assertion_or_reference(self):
        if not self.supports and (not self.subject or not self.text):
            raise ValueError("新增结论和订正必须给出主体与完整结论")
        return self


FACETS = {
    "news": ("发布方", "原始出处", "证据形式", "独立来源数"),
    "people": (
        "公开身份",
        "背景",
        "公开立场",
        "近期公开行动",
        "相关公开关系",
        "动因假设",
        "替代解释",
        "反证条件",
        "与事件关系",
        "判断变化",
        "可能影响",
        "资料性质",
    ),
    "organizations": ("机构权限", "政策阶段", "适用对象", "生效时间", "利益与约束"),
    "opinion": ("平台", "样本窗口", "采样口径", "观察数", "去重后样本数", "主要观点", "样本局限"),
    "related": ("关联事件ID", "方向", "机制", "共同因素", "证据限制"),
    "specialist": ("知识盲区", "检索范围", "停止条件"),
    "counter": ("原判断", "反证条件", "反例强度", "已查范围"),
    "dependencies": ("预测ID", "关键假设", "观察指标", "阈值", "中间事件", "影响方向"),
}


class Followup(Record):
    question: str = Field(min_length=1, max_length=500)
    condition: str = Field(min_length=1, max_length=600)
    prediction_id: int = 0
    due_at: str = ""


class ResearchLookup(Record):
    tool: Literal["archive", "read", "web", "world", "handoff"]
    query: str = Field(min_length=2, max_length=2048)
    recipient: str = Field(default="", max_length=120)
    document_ids: list[PositiveInt] = Field(default_factory=list, max_length=4)

    @model_validator(mode="after")
    def limit_search_text(self):
        if len(self.query) > 250 and not (self.tool == "web" and self.query.startswith("https://")):
            raise ValueError("检索词或交接内容最多 250 字，公开原文链接可至 2048 字")
        return self


class PublicationChoice(Record):
    task_id: int = Field(ge=1)
    finding: int = Field(ge=1, le=8)
    scope: Literal["world", "event"]
    reason: str = Field(min_length=2, max_length=400)


class DeliveryAnswer(Record):
    check_id: PositiveInt
    state: Literal["confirmed", "unknown"]
    answer: str = Field(min_length=2, max_length=1500)
    citations: list[Citation] = Field(default_factory=list, max_length=6)


class ClaimVerdict(Record):
    check_id: PositiveInt
    decision: Literal["supported", "return"]
    reason: str = Field(min_length=2, max_length=600)
    citations: list[Citation] = Field(default_factory=list, max_length=6)


class ClaimReview(Record):
    items: list[ClaimVerdict] = Field(max_length=16)


class ResolvedGap(Record):
    gap: str = Field(min_length=1)
    fragment_id: PositiveInt


class Report(Record):
    findings: list[Finding] = Field(default_factory=list, max_length=8)
    use_knowledge: list[KnowledgeLink] = Field(default_factory=list, max_length=4)
    gaps: list[str] = Field(default_factory=list, max_length=8)
    resolved_gaps: list[ResolvedGap] = Field(default_factory=list, max_length=8)
    followups: list[Followup] = Field(default_factory=list, max_length=5)
    answered: list[int] = Field(default_factory=list, max_length=6)
    lookup: ResearchLookup | None = None


PLANNER = (
    "checklist 按用户原始研究目标列出逐项可独立验收的问题，一项只核查一件事。"
    "即使已有结论，也保留仍属本轮范围的核查点。取证任务的数量不限制交付清单的项数。"
    "对会改变用户决策或预测、容易混淆适用范围或有相反材料的关键项，在负责取证的那条任务的 review_checks 填 checklist 的序号（从1开始）。"
    "每项指定一个负责补证的调查者；普通事实可留空。"
    "你负责事件规划。优先只安排一条能回答开放问题的调查线，存在独立问题才并行。line 复用黑板上同一调查线的名称。"
    "guides 按问题保存值得长期查阅的世界知识目的地。每项写 question、reason、wake、lookup。目录为空也可保留，旧指南失效时填 retire_guides 的 ID。"
    "collection_scope 为用户在本事件中指定的每轮收集范围。优先辨认其中的具名人物、核对本轮材料与关注点，尚无直接材料时记录核查缺口。"
    "根据事件主题，在 person_leads 提出有材料明确点名的核心人物、参与者、发言人、专家或意见领袖。"
    "person_leads.role 使用中文枚举，tasks.role 使用英文枚举；每项任务须给 question、target、time_scope、done_when、priority，不适用的可选字段可省略。"
    "每人须给出具体公共身份、与研究问题的关系及包含原文姓名的逐字引文与片段编号。"
    "身份用可核对的职务或机构；identity_quote 逐字抄录同一片段中能消歧的职务或身份短语，材料是英文就保留英文。"
    "name 用材料中实际出现的姓名；中文译名等只有材料支持时才作为 aliases。已跟踪者保持同一姓名与身份写法。"
    "材料不足则保留缺口，必要时安排 people 专项查找发布方的团队名单、采访或历史背景。"
    "catalog 给出类型和相关内容目录。角色若需从知识库补查，填任务的 knowledge_query，可传关键词、目录 path、实体编号和所需英文字段名。"
    "要精确限定条目的已知统计期、地区或其他字段，用 qualifiers 指明字段与值。旧记录可能缺字段，专项补证可以从较宽的目录开始。"
    "阅读当前目标、轮次窗口、已知档案、置顶事项、预测、知识缺口和本轮材料。"
    "根据价值选择必要角色，给出不同的具体研究问题和可检验的完成条件，优先级 5 最高。"
    "仅初始化、专项和反证可以使用 history。需要外查时给出精确搜索词，材料充分则留空。"
    "不要为了凑齐八种角色而派任务。延期项需说明原因。"
    "从候选平台里给当前事件挑少量能最先披露重大进展的新闻或主管单位入口，在 watch 中逐字填已给的 URL 和理由。"
    "若确有重要信息缺口，可在 source_queries 中给出一到两个找发布方栏目或订阅的搜索词。"
    "优先复核世界知识库候选。适用的旧知识通过 use_knowledge 填入候选的 entry_id、version_id 和本事件引用理由。避免重复建条目。"
    "若目标已改变，可在 release_knowledge 填入 prior 中已不适用的 world_id、id（分别对应 entry_id、version_id）及移出理由。"
)
ROLE_INSTRUCTIONS["people"] += (
    "使用 people_watch 中每个人的稳定实体编号与身份，subject 使用本人姓名，identity 沿用已核实的身份。"
    "明确区分本人公开发帖、媒体引述、他人讨论和历史背景。只有资料性质为本人公开发帖才据此称其本人在平台表态；"
    "未核实账号、未取得正文和转载不算本人发言。按日期说明过去与现在的立场、行动与这次事件的关系，给出引文、"
    "变化、可能影响及未知之处。心理或动机解释列为假设，须给替代解释和反证条件。"
)
RESEARCH = (
    "接到交接补查时，如果本次直接原文已经回答了上次调查结果中的某条 gaps，在 resolved_gaps 逐字填那条 gap，"
    "并填本次事实或订正实际引用的 fragment_id。只补了一部分时，在本次 gaps 写清剩余问题。其他情况填空数组。"
    "资料卡的发布方由系统读取归档元数据。其他字段由你提议，逐项给 field_citations。"
    "source 可填引用片段所附原始 URL，format 填档案实际形态（例如网页正文、订阅摘要），"
    "stage 尽可能逐字摘取能说明实际阶段的短语。无法定位的值留空。"
    "缺证且还有工具预算时，可在 lookup 请求 archive、read、world、web 或 handoff，query 写检索词、目的或交接内容。"
    "web 的 query 也可填已经找到的公开 HTTPS 原文 URL，后台回访并返回归档编号。"
    "read 用 document_ids 重读本事件归档的原文。handoff 的 recipient 填黑板已有的 line_key 或主 Agent，"
    "可附至多四个已查的 document_ids。读到工具回执再决定下一步，结束时 lookup=null。"
    "本轮缺口、排除项和预测依赖标 scope=event。能独立引用且原文支持的陈述标 scope=world。"
    "field_citations 的键可用 text、subject、occurred_at、attributes.属性名或 facets.字段名。"
    "每条字段引文须取自同一 fragment_id 的 citations 原句，可仅摘录支持字段的短语。核实对象、统计期和数值属于同一陈述。"
    "world_records 是本任务按固定版本读取的结构化记录。要把合适记录加入事件索引，可在 use_knowledge 填其 entry_id、version_id、章节和理由。"
    "record_schemas 定义各类型可填的英文字段。可在 attributes 保存引文支持的值、单位、统计期等，未提供的字段省略。directory 仅填所示且与 category 相符的目录路径。"
    "指标的 attributes.value 只登记引文中可逐项核对的原始读数。自行计算的涨跌幅、差额和倍数另写为分析，列出原数、引文和公式。"
    "选填目录和属性时逐项核对该 category 的可用路径和字段名；没有匹配项时留空，保留有出处的正文结论。"
    "输出中文研究结果。每项结论的 citations 必须逐字复制输入 fragments 中对应 id 的原文短句，不可翻译引文。"
    "occurred_at 只有原文能支持实际发生时间才填，精确时刻含时区，只有日期填 YYYY-MM-DD，同时逐字摘录 time_quote。"
    "来源更新时间和获得时间不是发生时间。identity 仅填可区分同名者的公开身份。"
    "replaces 只指向输入快照内被明确更正的声明 ID。证据不足写入 gaps。"
    "answered 只填输入 observations 中已用新依据回答的观察项 ID。observations 为空时填 []。"
    "新增条目会进入共享世界知识库。给出明确主体、资料的适用范围和对应类别。来自其他事件的历史事实沿用原日期。"
    "若可从已展示的原文补齐已有条目的缺失字段，或独立原文能印证原结论，可在 supports 填 prior 的 id 或 world_records 的 version_id。"
    "补证时省略 subject 和 text，由系统从该版本继承；只给 supports、已核实的新属性、逐字引文及协议要求的 section、kind。"
)
