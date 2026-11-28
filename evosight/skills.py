"""User-reviewed research procedures distilled from a specific retrospective."""
import json
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from .db import dump, enqueue, one, require, rows, utc


class Method(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    title: str = Field(min_length=2, max_length=100)
    when_to_use: str = Field(min_length=2, max_length=350)
    steps: list[Annotated[str, StringConstraints(strip_whitespace=True, min_length=2, max_length=350)]] = Field(
        min_length=2, max_length=8
    )
    roles: list[Literal[
        "news", "source_scout", "people", "organizations", "opinion", "related", "specialist",
        "counter", "dependencies", "plan", "study", "forecast"
    ]] = Field(min_length=1, max_length=12)
    scope: Literal["event", "workspace"]


class Extraction(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    method: Method | None = None
    reason: str = Field(min_length=2, max_length=600)


class Decision(BaseModel):
    model_config = ConfigDict(extra="forbid")
    decision: Literal["approve", "reject"]
    expected_revision: int = Field(ge=1)
    method: Method | None = None

    @model_validator(mode="after")
    def review_edits(self):
        if self.decision == "reject" and self.method is not None:
            raise ValueError("退回候选时无需填写生效方法")
        return self


EXTRACT = (
    "从预测复盘中提炼可迁移的调查方法。复盘只给特定事实或结果、没有可复用步骤时，method 填 null。"
    "方法需给标题、适用条件、逐项可执行的步骤（含验收）、适用角色和作用域。删除案例特有的主体、数值和结论。"
    "专用于原事件的流程用 event，可在其他事件复用的通用核查步骤用 workspace。方法只作用户待审提案。"
)

SELECT = """SELECT s.*,j.status AS job_status,j.info AS job_info,o.state='withdrawn' AS source_withdrawn
            FROM research_skills s JOIN observations o ON o.id=s.lesson_id
            LEFT JOIN jobs j ON j.id=s.job_id"""


def lesson_record(c, event, lesson):
    if require(c, "events", event)["demo"]:
        raise ValueError("演示事件的复盘不能生成调查方法")
    record = require(c, "observations", lesson)
    if record["event_id"] != event or record["origin"] != "待验证经验" or record["state"] == "withdrawn":
        raise ValueError("请选择本事件仍有效的复盘经验")
    return record


def present(card):
    card["method"] = json.loads(card["method"]) if card["method"] else None
    card["source_withdrawn"] = bool(card["source_withdrawn"])
    job_status = card.pop("job_status")
    info = card.pop("job_info")
    if card["status"] == "generating" and job_status in ("failed", "cancelled", "done"):
        card.update(status="failed", reason=info or "提炼未完成，可重试")
    elif card["status"] == "generating" and job_status == "deferred":
        card["reason"] = info
    return card


def list_cards(c, event):
    require(c, "events", event)
    return [present(s) for s in rows(c, SELECT + " WHERE s.event_id=? ORDER BY s.id DESC", (event,))]


def card(c, event, skill):
    result = one(c, SELECT + " WHERE s.id=? AND s.event_id=?", (skill, event))
    if not result:
        raise KeyError("候选方法不属于本事件")
    return present(result)


def from_verdict(c, event, lesson, method):
    # Idempotent settlement can never overwrite a user's edited or rejected method.
    c.execute(
        """INSERT OR IGNORE INTO research_skills(event_id,lesson_id,status,method,reason,created_at)
           VALUES(?,?,'pending',?,'裁定 Agent 从该次复盘中提出了可复用步骤',?)""",
        (event, lesson, method.model_dump_json(), utc()),
    )


def request(c, event, lesson):
    lesson_record(c, event, lesson)
    existing = one(c, SELECT + " WHERE s.lesson_id=?", (lesson,))
    if existing:
        existing = present(existing)
        if existing["status"] != "failed":
            return existing
        skill = existing["id"]
    else:
        skill = c.execute(
            """INSERT INTO research_skills(event_id,lesson_id,status,created_at) VALUES(?,?,'generating',?)""",
            (event, lesson, utc()),
        ).lastrowid
    job = enqueue(c, "skill_draft", event=event, args={"skill_id": skill, "dispatch_key": str(skill)})
    c.execute("UPDATE research_skills SET job_id=? WHERE id=?", (job, skill))
    return card(c, event, skill)


def generate(worker, job, skill):
    with worker.store.connection() as c:
        entry = card(c, job["event_id"], skill)
        lesson = lesson_record(c, job["event_id"], entry["lesson_id"])
        resolution = one(c, "SELECT outcome,reason FROM resolutions WHERE id=?", (lesson["resolution_id"],))
        reviews = rows(c, "SELECT outcome,reason FROM lesson_reviews WHERE lesson_id=? ORDER BY id DESC LIMIT 8",
                       (lesson["id"],))
        context = {"复盘经验": lesson["question"], "裁定依据": resolution, "独立案例复核": reviews}
    extracted, _ = worker.call(job, "skill_draft", context)
    with worker.store.connection(True) as c:
        method = extracted.method
        status = "pending" if method else "unsuitable"
        lesson_record(c, job["event_id"], entry["lesson_id"])
        c.execute(
            """UPDATE research_skills SET status=?,method=?,reason=?,revision=revision+1
               WHERE id=? AND job_id=? AND status='generating'""",
            (status, method.model_dump_json() if method else None, extracted.reason, skill, job["id"]),
        )
        return worker.receipt(c, job, "调查方法待用户确认" if method else "复盘未提炼出通用步骤")


def decide(c, event, skill, data):
    from .workflow import VersionConflict

    current = card(c, event, skill)
    lesson_record(c, event, current["lesson_id"])
    status = "approved" if data.decision == "approve" else "rejected"
    method = data.method.model_dump() if data.method else current["method"]
    if current["status"] == status and current["method"] == method:
        return current
    if current["status"] != "pending" or current["revision"] != data.expected_revision:
        raise VersionConflict("候选方法已更新，请重新查看后确认")
    c.execute(
        """UPDATE research_skills SET status=?,method=?,revision=revision+1,reviewed_at=? WHERE id=?""",
        (status, dump(method), utc(), skill),
    )
    return card(c, event, skill)


def applicable(c, event, role):
    # The source procedure may be shared across research events, never its source case's evidence.
    matches = rows(c, """SELECT s.id,s.revision,s.method FROM research_skills s
        JOIN observations o ON o.id=s.lesson_id JOIN events e ON e.id=s.event_id
        WHERE s.status='approved' AND e.status!='deleted' AND o.state!='withdrawn'
          AND (s.event_id=? OR json_extract(s.method,'$.scope')='workspace')
          AND EXISTS(SELECT 1 FROM json_each(s.method,'$.roles') WHERE value=?)
        ORDER BY s.id""", (event, role))
    return [{"id": s["id"], "version": s["revision"], **json.loads(s["method"])} for s in matches]


def register(app, store):
    @app.get("/api/events/{event}/skills")
    def listing(event: int):
        with store.connection() as c:
            return list_cards(c, event)

    @app.post("/api/events/{event}/lessons/{lesson}/skill", status_code=202)
    def propose(event: int, lesson: int):
        with store.connection(True) as c:
            return request(c, event, lesson)

    @app.post("/api/events/{event}/skills/{skill}/decision")
    def review(event: int, skill: int, data: Decision):
        with store.connection(True) as c:
            return decide(c, event, skill, data)
