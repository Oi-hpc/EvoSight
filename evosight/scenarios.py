"""Immutable ordered forecast scales and the evidence behind each distribution."""
from __future__ import annotations

import json
from typing import Annotated, Literal
from pydantic import BaseModel, ConfigDict, Field, model_validator

from .db import instant, one

Probability = Annotated[int, Field(strict=True, ge=0, le=100)]
EvidenceId = Annotated[int, Field(strict=True, ge=1)]


class Record(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class Level(Record):
    label: str = Field(min_length=1, max_length=65)
    criterion: str = Field(min_length=2, max_length=450)


class Scale(Record):
    window_start: str = Field(max_length=50)
    baseline: str = Field(min_length=2, max_length=650)
    levels: list[Level] = Field(min_length=3, max_length=6)

    @model_validator(mode="after")
    def unique_levels(self):
        if len({s.label.casefold() for s in self.levels}) != len(self.levels):
            raise ValueError("各情景的名称必须不同")
        if len({s.criterion.casefold() for s in self.levels}) != len(self.levels):
            raise ValueError("每档须有不同的可观察判据")
        return self

    def registered(self, when, deadline):
        start = instant(self.window_start)
        if start[:10] < when[:10] or start >= deadline:
            raise ValueError("趋势预测的观察窗口需从登记当天或之后开始，并早于截止时间")
        return {**self.model_dump(), "window_start": start}


class Argument(Record):
    side: Literal["support", "counter"]
    explanation: str = Field(min_length=2, max_length=650)
    refs: list[EvidenceId] = Field(min_length=1, max_length=6)


class Aspect(Record):
    name: str = Field(min_length=1, max_length=80)
    expectation: str = Field(min_length=2, max_length=500)
    refs: list[EvidenceId] = Field(min_length=1, max_length=6)


class Outlook(Record):
    probabilities: list[Probability] = Field(min_length=3, max_length=6)
    primary_level: int = Field(strict=True, ge=0, le=5)
    confidence: Literal["low", "medium", "high"]
    confidence_reason: str = Field(min_length=2, max_length=600)
    trajectory: str = Field(min_length=2, max_length=800)
    arguments: list[Argument] = Field(min_length=1, max_length=8)
    reconsider_if: str = Field(min_length=2, max_length=600)
    dimensions: list[Aspect] = Field(default_factory=list, max_length=5)

    @model_validator(mode="after")
    def consistent(self):
        if sum(self.probabilities) != 100:
            raise ValueError("各情景的概率合计须为 100%")
        if self.primary_level >= len(self.probabilities) or self.probabilities[self.primary_level] != max(
            self.probabilities
        ):
            raise ValueError("主情景须选概率最高的一档，并列时可择其一")
        if not any(a.side == "support" for a in self.arguments):
            raise ValueError("至少需要一条指向证据的支持理由")
        if len({x.name.casefold() for x in self.dimensions}) != len(self.dimensions):
            raise ValueError("每个预测维度只能登记一次")
        return self

    def checked(self, scale, refs):
        if len(self.probabilities) != len(scale["levels"]):
            raise ValueError("各档概率必须依序覆盖登记时的全部情景")
        if any(not set(line.refs) <= set(refs) for line in [*self.arguments, *self.dimensions]):
            raise ValueError("分项依据只能引用本版本实际采用的证据")
        return self.model_dump()


def definition(c, ident):
    value = one(c, "SELECT payload FROM forecast_scales WHERE prediction_id=?", (ident,))
    return json.loads(value["payload"]) if value else None


def version(c, revision):
    value = one(c, "SELECT payload FROM forecast_outlooks WHERE revision_id=?", (revision,))
    return json.loads(value["payload"]) if value else None


def summary(scale, outlook):
    if not scale or not outlook:
        return ""
    return scale["levels"][outlook["primary_level"]]["label"]


def score(probabilities, observed):
    running = 0
    total = 0
    for rank, probability in enumerate(probabilities[:-1]):
        running += probability
        total += (running / 100 - int(observed <= rank)) ** 2
    return round(total / (len(probabilities) - 1), 4)


def check_draft(draft, context, now):
    if draft.action == "hold":
        return
    fixed = context.get("固定题")
    if fixed and draft.action != "create":
        raise ValueError("固定题首次提交须登记一个新预测")
    if draft.action == "revise":
        registered = next((p for p in context.get("已有预测", []) if p["id"] == draft.prediction_id), None)
        if not registered or draft.scale is not None:
            raise ValueError("修订须引用输入中的已有题和原分档")
        scale = registered.get("scale")
    else:
        due = instant(fixed["deadline"] if fixed else draft.deadline)
        supplied = draft.scale.registered(now, due) if draft.scale else None
        registered_scale = (fixed or {}).get("scale")
        scale = (
            {**registered_scale, "window_start": instant(registered_scale["window_start"])}
            if registered_scale else supplied
        )
        if fixed:
            wanted = fixed.get("form", "binary")
            if (wanted == "ordered" and not scale) or (wanted == "binary" and scale):
                raise ValueError("须采用固定题指定的预测题型")
            registered_start = fixed.get("window_start")
            if (registered_scale and supplied and supplied != scale) or (
                registered_start and (not scale or scale["window_start"] != instant(registered_start))
            ):
                raise ValueError("固定题给定的观察窗口和分档须保持一致")
    if scale:
        if not draft.outlook:
            raise ValueError("分级预测须提交整份情景概率与依据")
        outlook = draft.outlook.checked(scale, draft.evidence_ids)
        if draft.p is not None and draft.p != outlook["probabilities"][outlook["primary_level"]]:
            raise ValueError("分级题的摘要概率与主情景不符")
    elif draft.outlook or draft.p is None:
        raise ValueError("二元题需一个明确的概率，分级题需已定义的分档")
    if not draft.evidence_ids or not set(draft.evidence_ids) <= {d["id"] for d in context["evidence"]}:
        raise ValueError("预测须逐项引用本次收到的证据")


def unresolved_checklist(c, report_version, report):
    """Retain declared gaps while allowing older plans that only listed investigation tasks."""
    if not report_version:
        return []
    row = one(
        c,
        """SELECT r.plan FROM knowledge_snapshots s JOIN tracking_rounds r ON r.id=s.round_id
           WHERE s.id=?""",
        (report_version,),
    )
    plan = json.loads(row["plan"] or "{}") if row else {}
    explicit = set(plan.get("checklist", []))
    if not any(
        item["state"] == "unknown" or (item["question"] in explicit and item["state"] != "confirmed")
        for item in report
    ):
        return []
    return [
        {
            "id": item["position"],
            "question": item["question"],
            "state": item["state"],
            "documents": sorted({ref["document_id"] for ref in item["citations"]}),
        }
        for item in report
    ]


def forecast_check_hold(draft, context):
    checks = context.get("预测核查清单", [])
    if not checks:
        return ""
    if draft.required_checks is None and draft.action != "hold":
        return "研究报告仍有待查项，本次预测尚未说明依赖哪些核查项"
    selected = set(draft.required_checks or [])
    if selected - {row["id"] for row in checks}:
        return "预测引用的核查项未出现在本版报告"
    question = (context.get("固定题") or {}).get("question") or draft.question
    if draft.action == "revise":
        question = next(
            (p["question"] for p in context.get("已有预测", []) if p["id"] == draft.prediction_id),
            question,
        )
    question = question.strip().rstrip("。？?").casefold()
    for row in checks:
        if row["id"] not in selected and row["question"].strip().rstrip("。？?").casefold() != question:
            continue
        if row["state"] != "confirmed":
            return "预测依赖的核查项仍待补证：" + row["question"]
        if draft.action != "hold" and not set(row["documents"]).intersection(draft.evidence_ids):
            return "预测尚未引用所依赖核查项的原始材料：" + row["question"]
    return ""
