"""Content paths and versioned record schemas used by both researchers and the catalog."""
import math
import re
import sys
from decimal import Decimal
import unicodedata

CATEGORIES = {
    "people": "人物",
    "organizations": "组织机构",
    "places": "国家与地区",
    "policies": "制度与政策",
    "indicators": "指标与数据",
    "events": "事实与进展",
    "opinion": "舆情与观点",
    "relations": "关系与依赖",
    "analysis": "分析与假设",
    "background": "背景知识",
}
SCHEMA_VERSION = 1
# Stable machine paths. Aliases route queries as well as previously unclassified material.
TOPICS = {
    "people/identity": ("身份与背景", ("生平", "任职经历", "学历", "履历", "传记")),
    "people/positions": ("公开立场", ("公开表态", "公开立场", "主张", "发言")),
    "people/actions": ("近期行动", ("出席", "会见", "辞职", "就任", "公开行动")),
    "people/relationships": ("公开关系", ("亲属关系", "公开关系", "家庭关系", "合作经历")),
    "organizations/mandate": ("职责与权限", ("主管单位", "机构权限", "法定职责", "主管部门")),
    "organizations/governance": ("治理与人事", ("董事会", "任命", "管理层", "治理结构")),
    "organizations/operations": ("业务与决策", ("企业经营", "机构决定", "财报", "董事会决议")),
    "places/geography": ("地理与区域", ("地理", "地区概况", "行政区划")),
    "places/jurisdiction": ("管辖与边界", ("管辖", "行政边界", "辖区")),
    "policies/proposals": ("草案与征求意见", ("草案", "征求意见", "拟议")),
    "policies/adoption": ("通过与生效", ("生效", "正式通过", "颁布", "签署成法")),
    "policies/enforcement": ("执行与监管", ("实施细则", "监管执法", "实施办法")),
    "indicators/prices": ("物价", ("物价", "价格指数", "通胀", "inflation")),
    "indicators/prices/consumer": ("居民消费价格", ("居民消费价格", "消费者价格指数", "CPI", "consumer price index")),
    "indicators/prices/producer": ("工业生产者价格", ("工业生产者价格", "出厂价格指数", "PPI", "producer price index")),
    "indicators/production": ("产出与增长", ("工业增加值", "国内生产总值", "GDP", "工业产出")),
    "indicators/employment": ("就业与人口", ("失业率", "就业人数", "劳动力", "人口普查")),
    "indicators/markets": ("市场与利率", ("收益率", "基准利率", "汇率", "成交量")),
    "events/technology": ("科技与产品", ("科技", "产品发布", "技术发布")),
    "events/technology/software": (
        "软件版本发布",
        ("软件版本", "预发布版本", "候选版", "release", "releases", "rc.", "changelog"),
    ),
    "events/technology/products": ("产品上市", ("产品上市", "开售", "预购", "发货", "产品供货")),
    "events/operations": ("建设与运营", ("验收", "载客", "投运", "开工", "通车")),
    "events/announcements": ("公告与决定", ("正式公告", "新闻稿", "正式公布", "通告")),
    "opinion/statements": ("公开发言", ("公开声明", "发言", "采访", "公开回应")),
    "opinion/sampling": ("平台与样本", ("抽样", "样本", "去重", "平台舆情", "民调")),
    "relations/actors": ("主体之间的关系", ("合作", "持股", "任职关系", "上下级")),
    "relations/events": ("事件之间的关系", ("关联事件", "传导机制", "共同因素")),
    "relations/dependencies": ("预测依赖", ("预测依赖", "关键假设", "结算标准", "中间事件")),
    "analysis/hypotheses": ("假设与解释", ("动因假设", "替代解释", "推测", "可能动机")),
    "analysis/counterevidence": ("反证与争议", ("反证", "反例", "证据冲突", "争议")),
    "analysis/scenarios": ("情景与条件", ("情景", "场景推演", "触发条件", "情境")),
    "background/history": ("历史脉络", ("历史背景", "起源", "沿革")),
    "background/methods": ("方法与口径", ("查阅口径", "统计口径", "方法", "定义", "计算方法")),
    "background/standards": ("标准与规范", ("技术标准", "标准条文", "规范", "协议版本")),
}
PATHS = {**{k: (v, ()) for k, v in CATEGORIES.items()}, **TOPICS}

# Field keys are stable across roles, UI languages, and event-local chapters.
SCHEMA_FIELDS = {
    "people": {
        "identity": "公开身份",
        "background": "背景",
        "position": "公开立场",
        "action": "近期公开行动",
        "relationship": "相关公开关系",
    },
    "organizations": {"mandate": "机构权限", "jurisdiction": "管辖范围", "decision": "公开决策", "constraints": "利益与约束"},
    "places": {"name": "地区名称", "jurisdiction": "所属辖区", "boundary": "范围", "geography": "地理特征"},
    "policies": {
        "issuer": "发布机构",
        "stage": "政策阶段",
        "applies_to": "适用对象",
        "effective_on": "生效时间",
        "jurisdiction": "适用地区",
    },
    "indicators": {
        "measure": "指标名称",
        "value": "数值",
        "unit": "单位",
        "period": "统计期",
        "region": "覆盖地区",
        "comparison": "比较基准",
        "method": "统计口径",
    },
    "events": {
        "publisher": "发布方",
        "source": "原始出处",
        "format": "证据形式",
        "independent_sources": "独立来源数",
        "stage": "进展阶段",
    },
    "opinion": {
        "platform": "平台",
        "window": "样本窗口",
        "method": "采样口径",
        "observed_count": "观察数",
        "unique_count": "去重后样本数",
        "views": "主要观点",
        "limitations": "样本局限",
    },
    "relations": {
        "from_subject": "关系起点",
        "to_subject": "关系终点",
        "direction": "方向",
        "mechanism": "机制",
        "common_factors": "共同因素",
        "limitations": "证据限制",
    },
    "analysis": {
        "hypothesis": "动因假设",
        "alternative": "替代解释",
        "falsifier": "反证条件",
        "prior": "原判断",
        "scope": "已查范围",
    },
    "background": {"definition": "定义", "method": "方法", "scope": "适用范围", "source": "原始出处", "gap": "知识盲区"},
}
NUMBER_FIELDS = {
    "value": "number",
    "independent_sources": "integer",
    "observed_count": "integer",
    "unique_count": "integer",
}


def normalize(value):
    return " ".join(unicodedata.normalize("NFKC", value or "").casefold().split())


def prefixes(path):
    if path not in PATHS:
        raise ValueError("未知世界知识目录")
    parts = path.split("/")
    return ["/".join(parts[:i]) for i in range(1, len(parts) + 1)]


def breadcrumbs(path):
    return [{"path": p, "label": PATHS[p][0]} for p in prefixes(path)] if path else []


def contains(text, term):
    term = normalize(term)
    if term.isascii():
        left = r"(?<![a-z0-9])" if term[0].isalnum() else ""
        right = r"(?![a-z0-9])" if term[-1].isalnum() else ""
        return re.search(left + re.escape(term) + right, text) is not None
    return term in text


def route(query, limit=3):
    """Locate record types and their fields even before any evidence has been collected."""
    text = normalize(query)
    matches = []
    for path, (label, aliases) in PATHS.items():
        category = path.partition("/")[0]
        signals = (label, *aliases, path) if path != category else (label, category)
        matched = {term for term in signals if contains(text, term)}
        fields = {
            key
            for key, title in SCHEMA_FIELDS[category].items()
            if contains(text, title) or contains(text, key)
        }
        if not matched and (path != category or not fields):
            continue
        # A content name or acronym is stronger than field names shared between schemas.
        score = 10 * bool(matched) + min(len(matched), 3) + min(len(fields), 3)
        matches.append(
            (
                score,
                {
                    "path": path,
                    "schema_id": category,
                    "schema_version": SCHEMA_VERSION,
                    "matched_terms": sorted(matched),
                    "matched_fields": sorted(fields),
                    "breadcrumbs": breadcrumbs(path),
                },
            )
        )
    matches.sort(key=lambda item: (-item[0], -item[1]["path"].count("/"), item[1]["path"]))
    found = []
    for _, item in matches:
        if not any(s["path"].startswith(item["path"] + "/") for s in found):
            found.append(item)
        if len(found) >= limit:
            break
    return found


def directory_for(category, subject, text, hint=""):
    if hint:
        if hint not in PATHS or hint.split("/")[0] != category:
            raise ValueError("知识目录与记录类型不匹配")
        return hint
    scores = []
    title, body = normalize(subject), normalize(text[:700])
    for path, (label, aliases) in TOPICS.items():
        if not path.startswith(category + "/"):
            continue
        signals = {label, *aliases}
        score = max((10 if contains(title, s) else 2 if contains(body, s) else 0 for s in signals), default=0)
        if score:
            scores.append((score, path.count("/"), path))
    if not scores:
        return category
    best = max(score for score, _, _ in scores)
    paths = {p for score, _, p in scores if score == best}
    leaves = [p for p in paths if not any(other.startswith(p + "/") for other in paths)]
    return next(
        p for p in reversed(prefixes(leaves[0])) if all(q == p or q.startswith(p + "/") for q in leaves)
    )


def schema(category):
    if category not in CATEGORIES:
        raise ValueError("未知世界知识类型")
    properties = {
        k: {"title": v, "type": NUMBER_FIELDS.get(k, "string")} for k, v in SCHEMA_FIELDS[category].items()
    }
    return {
        "schema_id": category,
        "schema_version": SCHEMA_VERSION,
        "title": CATEGORIES[category],
        "type": "object",
        "additionalProperties": False,
        "properties": properties,
    }


def compact_schemas(categories):
    return {
        key: {
            field: label + "(" + NUMBER_FIELDS.get(field, "string") + ")"
            for field, label in SCHEMA_FIELDS[key].items()
        }
        for key in dict.fromkeys(categories)
        if key in SCHEMA_FIELDS
    }


def attributes(category, facets, explicit=None):
    fields = SCHEMA_FIELDS[category]
    result = {}
    for key, label in fields.items():
        if label in facets:
            v = facets[label]
            typ = NUMBER_FIELDS.get(key)
            if typ and isinstance(v, str) and len(v) < 80 and re.fullmatch(r"-?\d+(?:\.\d+)?", v.strip()):
                v = float(v) if "." in v else int(v)
            if (
                (not typ and isinstance(v, str) and v.strip())
                or (typ == "integer" and type(v) is int and 0 <= v < 2**63)
                or (typ == "number" and finite_number(v))
            ):
                result[key] = v
    for key, v in (explicit or {}).items():
        if key not in fields:
            raise ValueError(f"{category} 类型未定义字段 {key}")
        typ = NUMBER_FIELDS.get(key, "string")
        if typ == "string" and (not isinstance(v, str) or not v.strip() or len(v) > 500):
            raise ValueError(f"{key} 需要有依据的短文本")
        if typ == "number" and not finite_number(v):
            raise ValueError(f"{key} 需要有限数值")
        if typ == "integer" and (type(v) is not int or not 0 <= v < 2**63):
            raise ValueError(f"{key} 需要非负整数")
        result[key] = v.strip() if isinstance(v, str) else v
    return result


def finite_number(value):
    return (type(value) is int and abs(value) <= sys.float_info.max) or (
        type(value) is float and math.isfinite(value)
    )


def qualifier_token(category, field, value):
    if isinstance(value, str):
        normalized = normalize(value)
        if category == "indicators" and field == "period":
            # Preserve the value quoted from the publisher. Normalize only the lookup key.
            month = re.fullmatch(r"(\d{4})\s*(?:年\s*(\d{1,2})\s*月份?|[-/](\d{1,2}))", normalized)
            if month and 1 <= int(month[2] or month[3]) <= 12:
                normalized = f"{month[1]}-{int(month[2] or month[3]):02}"
    else:
        normalized = format(Decimal(str(value)), "f")
        if "." in normalized:
            normalized = normalized.rstrip("0").rstrip(".")
        if normalized == "-0":
            normalized = "0"
    return f"@{category}/{field}={normalized}"


def qualifier_terms(path, values):
    if not isinstance(values, dict) or len(values) > 4:
        raise ValueError("限定条件须为至多四个字段组成的对象")
    if not values:
        return []
    category = path.partition("/")[0]
    if path not in PATHS:
        raise ValueError("按字段筛选需要有效的内容目录或类型")
    verified = attributes(category, {}, values)
    return [qualifier_token(category, k, v) for k, v in verified.items()]


def seed(c):
    c.executemany(
        """INSERT INTO world_directories(code,parent_code,label) VALUES(?,?,?) ON CONFLICT(code)
                  DO UPDATE SET label=excluded.label,parent_code=excluded.parent_code
                  WHERE label!=excluded.label OR parent_code IS NOT excluded.parent_code""",
        [(p, p.rpartition("/")[0] or None, label) for p, (label, _) in PATHS.items()],
    )
