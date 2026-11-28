"""HTTP entry points for the research loop and the event knowledge workspace."""
from typing import Annotated, Literal
import json
from fastapi import Query
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, Field
from . import board, focus, people, workflow, world, world_search, world_catalog as catalog
from .contracts import Guide
from .db import enqueue, one, require, rows, utc
from .feeds import public_url


class GoalEdit(BaseModel):
    expected_version: int = Field(ge=1)
    title: str | None = Field(default=None, min_length=1, max_length=250)
    scope: str | None = Field(default=None, min_length=1, max_length=1500)
    priority: int | None = Field(default=None, ge=1, le=5)
    related: list[int] | None = Field(default=None, max_length=30)
    status: Literal["active", "paused", "archived"] | None = None


class WatchChoice(BaseModel):
    url: str
    watching: bool


class ClosureChoice(BaseModel):
    choice: Literal["continue", "archived"]


class AccountReview(BaseModel):
    confirmed: bool


class Question(BaseModel):
    question: str = Field(min_length=2, max_length=500)
    condition: str = Field(min_length=2, max_length=600)


class Correction(BaseModel):
    expected_snapshot: int = Field(ge=1)
    text: str = Field(min_length=1, max_length=1600)
    reason: str = Field(min_length=1, max_length=800)
    fragment_id: int = Field(ge=1)
    quote: str = Field(min_length=2, max_length=800)


class LessonReview(BaseModel):
    fragment_id: int = Field(ge=1)
    outcome: Literal["support", "counter", "withdraw"]
    reason: str = Field(min_length=2, max_length=800)


class Reference(BaseModel):
    entry_id: int = Field(ge=1)
    version_id: int = Field(ge=1)
    expected_snapshot: int = Field(ge=0)
    reason: str = Field(min_length=2, max_length=400)
    section: Literal["", "background", "people", "opinion", "news"] = ""


class Unreference(BaseModel):
    expected_snapshot: int = Field(ge=1)
    reason: str = Field(min_length=2, max_length=400)


class Disposition(BaseModel):
    version_id: int = Field(ge=1)
    state: Literal["withdrawn", "merged"]
    target: int | None = Field(default=None, ge=1)
    target_version: int | None = Field(default=None, ge=1)
    reason: str = Field(min_length=2, max_length=400)
    fragment_id: int = Field(ge=1)


class DisputeDecision(BaseModel):
    version_id: int = Field(ge=1)
    fragment_id: int = Field(ge=1)
    reason: str = Field(min_length=2, max_length=400)


class VersionKey(BaseModel):
    entry_id: int = Field(ge=1)
    version_id: int | None = Field(default=None, ge=1)


class RecordRead(BaseModel):
    records: list[VersionKey] = Field(min_length=1, max_length=12)
    fields: list[Annotated[str, Field(min_length=1, max_length=64)]] = Field(
        default_factory=list, max_length=12
    )
    evidence: bool = False


def register_workflow_api(app, store):
    @app.post("/api/world/{entry}/disposition")
    def disposition(entry: int, data: Disposition):
        with store.connection(True) as c:
            return board.set_disposition(
                c,
                entry,
                data.version_id,
                data.state,
                data.target,
                data.reason,
                data.fragment_id,
                data.target_version,
            )

    @app.post("/api/world/{entry}/disputes/{dispute}/resolve")
    def resolve(entry: int, dispute: int, data: DisputeDecision):
        with store.connection(True) as c:
            return board.resolve_dispute(c, entry, dispute, data.version_id, data.fragment_id, data.reason)

    @app.get("/api/events/{event}/board")
    def event_board(event: int):
        with store.connection() as c:
            return board.view(c, event, workflow.current_goal(c, event)["version"])

    @app.post("/api/events/{event}/guides")
    def add_guide(event: int, guide: Guide, expected_goal: int):
        with store.connection(True) as c:
            goal = workflow.current_goal(c, event)
            if expected_goal != goal["version"]:
                raise workflow.VersionConflict("研究目标已经更新")
            board.save_guides(
                c, {"event_id": event, "goal_version": expected_goal}, {"guides": [guide.model_dump()]}
            )
            return board.view(c, event, expected_goal)["guides"]

    @app.get("/api/events/{event}/focus")
    def collection_scope(event: int):
        with store.connection() as c:
            require(c, "events", event)
            return focus.listing(c, event)

    @app.post("/api/events/{event}/focus/{ident}/pause")
    def pause_scope(event: int, ident: int):
        with store.connection(True) as c:
            require(c, "events", event)
            found = require(c, "event_focus", ident)
            if found["event_id"] != event:
                raise ValueError("该收集范围不属于当前事件")
            c.execute("UPDATE event_focus SET active=0 WHERE id=?", (ident,))
            return {"id": ident, "active": False}

    @app.get("/api/events/{event}/people")
    def event_people(event: int, target: int = Query(0, ge=0)):
        with store.connection() as c:
            return people.view(c, event, target)

    @app.post("/api/events/{event}/people/refresh", status_code=202)
    def refresh_people(event: int):
        with store.connection(True) as c:
            rid, job = workflow.request_round(c, event, "人物专项")
            return {"round": rid, "job": job}

    @app.post("/api/events/{event}/people/{target}/accounts/{account}")
    def account_review(event: int, target: int, account: int, review: AccountReview):
        with store.connection(True) as c:
            return people.review_account(c, event, target, account, review.confirmed)

    @app.exception_handler(workflow.VersionConflict)
    async def outdated(req, exc):
        return JSONResponse({"detail": str(exc)}, 409)

    @app.get("/api/world")
    def world_catalog(
        q: str = Query("", max_length=2000),
        category: str = "",
        offset: int = 0,
        limit: int = 24,
        path: str = "",
    ):
        with store.connection() as c:
            return world.browse(c, q, category, max(0, offset), max(1, min(limit, 80)), path=path)

    @app.get("/api/world/manifest")
    def world_manifest():
        return world_search.manifest()

    @app.get("/api/world/route")
    def schema_routes(q: str = Query(min_length=1, max_length=2000), limit: int = Query(3, ge=1, le=10)):
        return {"routes": catalog.route(q, limit)}

    @app.get("/api/world/schemas/{schema_id}")
    def record_schema(schema_id: str, version: int | None = None):
        if version is not None and version != catalog.SCHEMA_VERSION:
            raise KeyError("未注册的字段版本")
        return catalog.schema(schema_id)

    @app.get("/api/world/catalog")
    def directory(path: str = ""):
        with store.connection() as c:
            return world_search.directory(c, path)

    @app.get("/api/world/search")
    def search(
        q: str = Query("", max_length=2000),
        path: str = "",
        kind: str = "",
        entity_id: int | None = None,
        occurred_from: str = "",
        occurred_to: str = "",
        qualifiers: str | None = Query(None, max_length=2400),
        offset: int = 0,
        limit: int = 8,
    ):
        values = {} if qualifiers is None else json.loads(qualifiers)
        if not isinstance(values, dict):
            raise ValueError("限定条件须为字段与值的对象")
        with store.connection() as c:
            hits, _ = world_search.search(
                c,
                q,
                path=path,
                kind=kind,
                entity_id=entity_id,
                occurred_from=occurred_from,
                occurred_to=occurred_to,
                qualifiers=values,
                offset=max(0, offset),
                limit=max(1, min(24, limit)),
            )
            return {"entries": hits}

    @app.post("/api/world/records")
    def version_records(data: RecordRead):
        with store.connection() as c:
            return {
                "records": world_search.read_records(
                    c, [k.model_dump(exclude_none=True) for k in data.records], data.fields, data.evidence
                )
            }

    @app.get("/api/world/{entry}")
    def world_entry(entry: int, revision: int | None = None):
        with store.connection() as c:
            return world.detail(c, entry, revision)

    @app.post("/api/events/{event}/knowledge/links", status_code=201)
    def link_world(event: int, data: Reference):
        with store.connection(True) as c:
            return workflow.reference_world(
                c, event, data.entry_id, data.version_id, data.expected_snapshot, data.reason, data.section
            )

    @app.post("/api/events/{event}/knowledge/links/{entry}/remove", status_code=201)
    def unlink_world(event: int, entry: int, data: Unreference):
        with store.connection(True) as c:
            return workflow.remove_reference(c, event, entry, data.expected_snapshot, data.reason)

    @app.patch("/api/events/{event}")
    def edit(event: int, data: GoalEdit):
        with store.connection(True) as c:
            return workflow.edit_goal(
                c,
                event,
                data.expected_version,
                **data.model_dump(exclude={"expected_version"}, exclude_none=True),
            )

    @app.get("/api/events/{event}/knowledge")
    def knowledge(
        event: int,
        section: str | None = None,
        revision: int | None = None,
        compare: int | None = None,
        q: str = "",
    ):
        with store.connection() as c:
            return workflow.knowledge(c, event, section=section, revision=revision, compare=compare, query=q)

    @app.get("/api/events/{event}/rounds")
    def rounds_for_event(event: int, rid: int | None = None):
        with store.connection() as c:
            require(c, "events", event)
            return workflow.list_rounds(c, event, rid)

    @app.post("/api/events/{event}/knowledge/claims/{claim}/corrections", status_code=201)
    def correct(event: int, claim: int, data: Correction):
        with store.connection(True) as c:
            return workflow.correct_claim(
                c, event, data.expected_snapshot, claim, data.text, data.reason, data.fragment_id, data.quote
            )

    @app.post("/api/events/{event}/rounds/{rid}/retry")
    def retry_round(event: int, rid: int, task: int = 0):
        with store.connection(True) as c:
            r = require(c, "tracking_rounds", rid)
            if r["event_id"] != event:
                raise KeyError()
            if not workflow.goal_matches(c, r):
                raise workflow.VersionConflict("该轮目标已更改，请使用当前研究轮次")
            if task and require(c, "research_tasks", task)["round_id"] != rid:
                raise KeyError()
            # The supplemental role owns a separate lease and a frozen prompt.
            # Restoring only research_tasks.state would leave it without an executor.
            wake_jobs = rows(c, """SELECT w.job_id FROM investigation_wakes w JOIN jobs j ON j.id=w.job_id
                            WHERE w.round_id=? AND w.snapshot_id IS NULL AND (?=0 OR w.task_id=?)
                            AND j.status IN ('queued','running','failed','deferred') ORDER BY j.id""",
                             (rid, task, task))
            if wake_jobs:
                c.executemany(
                    """UPDATE jobs SET status='queued',attempts=0,retry_at=NULL,finished_at=NULL,lease=NULL,owner=NULL
                       WHERE id=? AND status IN ('failed','queued')""", [(w["job_id"],) for w in wake_jobs],
                )
                if task or r["state"] in ("published", "unchanged"):
                    return {"job": wake_jobs[0]["job_id"]}
            busy = one(
                c,
                "SELECT id FROM jobs WHERE event_id=? AND json_extract(args,'$.round_id')=? AND status IN ('queued','running')",
                (event, rid),
            )
            if busy:
                c.execute(
                    """UPDATE jobs SET retry_at=NULL WHERE id=? AND kind='research_retry' AND status='queued'""",
                    (busy["id"],),
                )
                return {"job": busy["id"]}
            if r["state"] in ("published", "unchanged"):
                raise ValueError("该轮已经完成")
            if r["state"] == "waiting" and r["world_seed_only"]:
                # No role has run. Recheck publishers and the catalog on this explicit retry.
                c.execute(
                    "DELETE FROM world_task_reads WHERE task_id IN (SELECT id FROM research_tasks WHERE round_id=? AND state='queued')",
                    (rid,),
                )
                c.execute("DELETE FROM research_tasks WHERE round_id=? AND state='queued'", (rid,))
                c.execute(
                    """UPDATE tracking_rounds SET input_docs=NULL,context=NULL,plan=NULL,
                             world_seed_only=0,finished_at=NULL WHERE id=?""",
                    (rid,),
                )
            c.execute(
                "UPDATE research_tasks SET state='queued' WHERE round_id=? AND state IN ('failed','deferred') AND (?=0 OR id=?)",
                (rid, task, task),
            )
            c.execute("UPDATE tracking_rounds SET state='queued' WHERE id=?", (rid,))
            jid = enqueue(c, "research_retry", event=event, args={"round_id": rid})
            c.execute("UPDATE tracking_rounds SET job_id=? WHERE id=?", (jid, rid))
            return {"job": jid}

    @app.post("/api/events/{event}/watch")
    def watch_choice(event: int, data: WatchChoice):
        with store.connection(True) as c:
            require(c, "events", event)
            cur = c.execute(
                "UPDATE event_platforms SET watching=?,pinned=1 WHERE event_id=? AND url=?",
                (int(data.watching), event, public_url(data.url)),
            )
            if not cur.rowcount:
                raise KeyError()
            return {"ok": True}

    @app.post("/api/events/{event}/questions")
    def investigate(event: int, data: Question):
        with store.connection(True) as c:
            workflow.observe(c, event, "用户专项", data.question, data.condition, due=utc(), priority=5)
            rid, jid = workflow.request_round(c, event, "专项补查")
            return {"round": rid, "job": jid}

    @app.post("/api/events/{event}/lessons/{lesson}/reviews", status_code=201)
    def review(event: int, lesson: int, data: LessonReview):
        with store.connection(True) as c:
            return workflow.review_lesson(c, event, lesson, data.fragment_id, data.outcome, data.reason)

    @app.post("/api/events/{event}/closure/{ident}")
    def closure(event: int, ident: int, data: ClosureChoice):
        with store.connection(True) as c:
            return workflow.respond_end(c, event, ident, data.choice)

    @app.get("/api/events/{event}/documents/{ident}/fragments")
    def evidence_fragments(event: int, ident: int):
        with store.connection() as c:
            require(c, "events", event)
            if not one(c, "SELECT 1 FROM event_docs WHERE event_id=? AND document_id=?", (event, ident)):
                raise KeyError()
            assets = rows(
                c,
                "SELECT id,url,kind,state,detail,digest,created_at FROM evidence_assets WHERE document_id=? ORDER BY id",
                (ident,),
            )
            for asset in assets:
                asset["parts"] = rows(
                    c,
                    "SELECT id,locator,text,link FROM fragments WHERE asset_id=? ORDER BY id",
                    (asset["id"],),
                )
            return assets

    @app.get("/api/events/{event}/assets/{ident}/original")
    def original(event: int, ident: int):
        with store.connection() as c:
            require(c, "events", event)
            obj = one(
                c,
                """SELECT b.content,b.mime FROM evidence_assets a JOIN evidence_blobs b ON b.digest=a.digest
                         JOIN event_docs ed ON ed.document_id=a.document_id WHERE ed.event_id=? AND a.id=?""",
                (event, ident),
            )
            if not obj:
                raise KeyError()
            return Response(
                obj["content"],
                media_type="application/octet-stream",
                headers={"Content-Disposition": f'attachment; filename="evidence-{ident}"'},
            )
