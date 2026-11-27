"""Local API and Vue application entry point."""
import json
from contextlib import asynccontextmanager
from pathlib import Path
import sqlite3
from typing import Literal
from urllib.parse import urlsplit

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import discussion, forecast, resources, views, workflow
from .agents import Runner, CONTRACTS
from .scenarios import Scale, Outlook
from .workflow_api import register_workflow_api
from .db import Store, add_event, config, dump, enqueue, instant, mark_read, one, require, rows, usage, utc
from .feeds import public_url
from .worker import Worker, admit


class EventIn(BaseModel):
    title: str = Field(min_length=1, max_length=250)
    scope: str = Field(min_length=1, max_length=1500)
    feeds: list[str] = Field(default_factory=list, max_length=6)
    cadence: int = Field(default=6, ge=1, le=168)


class TopicIn(BaseModel):
    name: str = Field(min_length=1, max_length=250)
    feeds: list[str] = Field(default_factory=list, max_length=6)
    keywords: list[str] = Field(max_length=20)
    discovery: bool = False


class Switch(BaseModel):
    enabled: bool


class DiscussionIn(BaseModel):
    timezone: str | None = Field(default=None, max_length=80)
    utc_offset_minutes: int = Field(default=0, ge=-840, le=840)
    revision: int | None = Field(default=None, ge=1)
    basis: Literal["original", "latest"] = "latest"
    provider: Literal["auto", "codex", "dsh"] = "auto"
    question: str = Field(min_length=1, max_length=2400)
    nonce: str = Field(min_length=8, max_length=80)


class ReadIn(BaseModel):
    token: str = Field(max_length=130)


class Life(BaseModel):
    status: Literal["active", "paused", "archived"]


class CandidateAction(BaseModel):
    action: Literal["accept", "dismiss"]


class Limits(BaseModel):
    daily_calls: int = Field(ge=0, le=10000)
    monthly_calls: int = Field(ge=0, le=300000)
    verify_reserve: int = Field(ge=0, le=10000)
    capacity: int = Field(ge=1, le=500)
    auto_limit: int = Field(ge=0, le=20)
    global_pause: bool
    watch_minutes: int | None = Field(default=None, ge=1, le=360)
    watch_sources: int | None = Field(default=None, ge=1, le=8)
    max_agent_concurrency: int | None = Field(default=None, ge=1, le=4)


class Definition(BaseModel):
    question: str = Field(min_length=1, max_length=550)
    deadline: str
    rule: str = Field(min_length=1, max_length=2000)
    authority: str = Field(min_length=1, max_length=500)
    form: Literal["auto", "binary", "ordered"] = "auto"
    window_start: str | None = None
    scale: Scale | None = None


class Version(BaseModel):
    p: int | None = Field(default=None, ge=0, le=100)
    outlook: Outlook | None = None
    reason: str = Field(min_length=1, max_length=2500)
    refs: list[int] = Field(max_length=25)


class NewPrediction(Definition, Version):
    grace_hours: int = Field(default=72, ge=0, le=720)


class Settlement(BaseModel):
    outcome: Literal["yes", "no", "level", "unverifiable", "conflict"]
    observed_level: int | None = Field(default=None, ge=0, le=5)
    observed_at: str = ""
    public_at: str = ""
    reason: str = Field(min_length=1, max_length=2500)
    refs: list[int] = Field(max_length=25)
    lesson: str = Field(default="", max_length=2000)


def make_app(store: Store, runner=None, *, background=True, acquisition=None):
    runner = runner or Runner()
    worker = Worker(store, runner, acquisition=acquisition)

    @asynccontextmanager
    async def lifespan(app):
        if background:
            worker.start()
        yield
        worker.stop.set()
        worker.watcher.stop.set()

    app = FastAPI(title="EvoSight", version="0.1.0", lifespan=lifespan, docs_url=None, redoc_url=None)
    register_workflow_api(app, store)

    @app.middleware("http")
    async def local_only(request: Request, call_next):
        hostname = request.url.hostname
        if hostname not in ("127.0.0.1", "localhost", "::1"):
            return JSONResponse({"detail": "工作台仅支持本机访问"}, 403)
        if request.method not in ("GET", "HEAD", "OPTIONS"):
            origin = request.headers.get("origin")
            if (origin and urlsplit(origin).netloc != request.url.netloc) or request.headers.get(
                "x-evosight"
            ) != "local":
                return JSONResponse({"detail": "无效的本地操作来源"}, 403)
        result = await call_next(request)
        result.headers["X-Content-Type-Options"] = "nosniff"
        result.headers["Referrer-Policy"] = "no-referrer"
        if request.url.path.startswith("/api/"):
            result.headers["Cache-Control"] = "no-store"
        return result

    @app.exception_handler(KeyError)
    async def missing(req, exc):
        return JSONResponse({"detail": "记录不存在"}, 404)

    @app.exception_handler(ValueError)
    async def invalid(req, exc):
        return JSONResponse({"detail": str(exc)}, 422)

    @app.exception_handler(sqlite3.IntegrityError)
    async def conflict(req, exc):
        return JSONResponse({"detail": "记录已存在，或操作与当前记录冲突"}, 409)

    @app.get("/api/home")
    def home():
        with store.connection() as c:
            return {**views.home(c), "mode": runner.mode}

    @app.post("/api/events", status_code=201)
    def create_event(data: EventIn):
        with store.connection(True) as c:
            ident = add_event(
                c, data.title, data.scope, [public_url(u) for u in data.feeds], cadence=data.cadence
            )
            return workflow.begin_event(c, ident)

    @app.get("/api/events/{event}")
    def event_data(event: int):
        with store.connection() as c:
            return views.event_details(c, event)

    @app.get("/api/events/{event}/timeline")
    def timeline(
        event: int,
        kind: str = "",
        order: Literal["known", "occurred"] = "known",
        item: int = 0,
        page: int = Query(default=1, ge=1),
        after: int = Query(default=0, ge=0),
        through: int | None = Query(default=None, ge=0),
    ):
        with store.connection() as c:
            return views.timeline(c, event, kind, order, item, page, after, through)

    @app.get("/api/events/{event}/discussion")
    def read_discussion(
        event: int,
        revision: int | None = Query(default=None, ge=1),
        basis: Literal["original", "latest"] = "latest",
        before: int = Query(0, ge=0),
    ):
        with store.connection() as c:
            return discussion.conversation(c, event, revision, basis, before)

    @app.post("/api/events/{event}/discussion", status_code=202)
    def ask(event: int, data: DiscussionIn):
        with store.connection(True) as c:
            return discussion.submit(c, event, **data.model_dump())

    @app.get("/api/agent/discussion")
    def workspace_chat(before: int = Query(0, ge=0)):
        with store.connection() as c:
            return discussion.conversation(c, None, None, "latest", before)

    @app.post("/api/agent/discussion", status_code=202)
    def ask_workspace(data: DiscussionIn):
        with store.connection(True) as c:
            return discussion.submit(c, None, **data.model_dump())

    @app.get("/api/events/{event}/discussion/archive/{revision}")
    def discussion_archive(event: int, revision: int):
        with store.connection() as c:
            rec = discussion.anchor(c, event, revision)
            if not rec:
                raise KeyError()
            return {"run": discussion.archived_forecast(c, rec)}

    @app.post("/api/events/{event}/read")
    def read(event: int, data: ReadIn):
        with store.connection(True) as c:
            mark_read(c, event, data.token)
            return {"ok": True}

    @app.post("/api/events/{event}/status")
    def event_status(event: int, data: Life):
        with store.connection(True) as c:
            return workflow.edit_goal(
                c, event, workflow.current_goal(c, event)["version"], status=data.status
            )

    @app.post("/api/events/{event}/refresh")
    def refresh(event: int):
        with store.connection(True) as c:
            e = require(c, "events", event)
            if e["demo"] or e["status"] != "active":
                raise ValueError("请使用活跃的真实事件发起采集")
            if config(c)["global_pause"]:
                raise ValueError("全局调度已暂停")
            rid, jid = workflow.request_round(c, event, "手动刷新")
            return {"round": rid, "job": jid}

    @app.post("/api/items/{ident}/pin")
    def pin(ident: int, data: Switch):
        with store.connection(True) as c:
            require(c, "items", ident)
            c.execute("UPDATE items SET pinned=? WHERE id=?", (data.enabled, ident))
            return {"ok": True}

    @app.get("/api/explore")
    def explore():
        with store.connection() as c:
            return views.explore(c)

    @app.post("/api/topics", status_code=201)
    def create_topic(data: TopicIn):
        name = data.name.strip()
        if not name:
            raise ValueError("请填写兴趣主题")
        with store.connection(True) as c:
            ident = c.execute(
                "INSERT INTO topics(name,feeds,keywords,discovery,next_run) VALUES(?,?,?,?,?)",
                (
                    name,
                    dump([public_url(u) for u in data.feeds]),
                    dump([w.strip()[:80] for w in data.keywords if w.strip()]),
                    data.discovery,
                    utc(),
                ),
            ).lastrowid
            return {"id": ident}

    @app.post("/api/topics/{ident}/discovery")
    def topic_discovery(ident: int, data: Switch):
        with store.connection(True) as c:
            require(c, "topics", ident)
            c.execute("UPDATE topics SET discovery=? WHERE id=?", (data.enabled, ident))
            return {"ok": True}

    @app.post("/api/candidates/{ident}")
    def candidate(ident: int, data: CandidateAction):
        with store.connection(True) as c:
            if data.action == "accept":
                return {"id": admit(c, ident)}
            record = require(c, "candidates", ident)
            if record["state"] != "review":
                raise ValueError("该候选已处理")
            c.execute("UPDATE candidates SET state='dismissed' WHERE id=?", (ident,))
            return {"ok": True}

    @app.get("/api/predictions")
    def ledger():
        with store.connection() as c:
            return views.ledger(c)

    @app.post("/api/events/{event}/predictions", status_code=201)
    def predict(event: int, data: NewPrediction):
        if (data.form == "binary" and data.scale) or (data.form == "ordered" and not data.scale):
            raise ValueError("手动登记的题型与分档定义须相符")
        if data.window_start and (
            not data.scale or instant(data.window_start) != instant(data.scale.window_start)
        ):
            raise ValueError("观察起点须与登记的分档一致")
        with store.connection(True) as c:
            require(c, "events", event)
            return {
                "id": forecast.new(
                    c,
                    event,
                    data.question,
                    data.deadline,
                    data.rule,
                    data.authority,
                    data.p,
                    data.reason,
                    data.refs,
                    "手动登记",
                    grace_hours=data.grace_hours,
                    scale=data.scale,
                    outlook=data.outlook,
                )
            }

    @app.post("/api/events/{event}/fixed", status_code=201)
    def fixed(event: int, data: Definition):
        with store.connection(True) as c:
            e = require(c, "events", event)
            if e["demo"] or e["status"] != "active" or instant(data.deadline) <= utc():
                raise ValueError("固定题须在截止前登记到活跃的真实事件")
            if one(
                c,
                "SELECT id FROM fixed_questions WHERE event_id=? AND question=? AND deadline=?",
                (event, data.question.strip(), instant(data.deadline)),
            ):
                raise ValueError("同题面和期限的固定题已登记")
            ident = c.execute(
                "INSERT INTO fixed_questions(event_id,question,rule,authority,deadline,created_at) VALUES(?,?,?,?,?,?)",
                (event, data.question.strip(), data.rule, data.authority, instant(data.deadline), utc()),
            ).lastrowid
            window = instant(data.window_start) if data.window_start else None
            if window and (window[:10] < utc()[:10] or window >= instant(data.deadline)):
                raise ValueError("观察窗口需从登记当天或之后开始，并早于截止时间")
            scale = data.scale.registered(utc(), instant(data.deadline)) if data.scale else None
            if (data.form == "binary" and (scale or window)) or (
                window and scale and scale["window_start"] != window
            ):
                raise ValueError("题型、给定观察起点及情景定义须一致")
            c.execute(
                "INSERT INTO fixed_forecast_requests VALUES(?,?,?,?)",
                (ident, data.form, window, dump(scale) if scale else None),
            )
            return {"id": ident, "job": enqueue(c, "forecast", event=event, args={"fixed_id": ident})}

    @app.post("/api/predictions/{ident}/versions")
    def revision(ident: int, data: Version):
        with store.connection(True) as c:
            return {
                "id": forecast.revise(c, ident, data.p, data.reason, data.refs, "人工修订", outlook=data.outlook)
            }

    @app.post("/api/predictions/{ident}/pin")
    def pin_prediction(ident: int, data: Switch):
        with store.connection(True) as c:
            p = require(c, "predictions", ident)
            c.execute(
                "UPDATE events SET pinned_prediction=? WHERE id=?",
                (ident if data.enabled else None, p["event_id"]),
            )
            return {"ok": True}

    @app.post("/api/predictions/{ident}/settle")
    def settle(ident: int, data: Settlement):
        with store.connection(True) as c:
            return forecast.settle(
                c,
                ident,
                data.outcome,
                data.public_at,
                data.reason,
                data.refs,
                data.lesson,
                "人工裁定",
                observed_level=data.observed_level,
                observed_at=data.observed_at,
            )

    @app.get("/api/operations")
    def operations(job: int | None = None):
        with store.connection() as c:
            return {
                "jobs": rows(
                    c,
                    """SELECT j.*,e.title AS event_title,t.name AS topic_name FROM jobs j
                    LEFT JOIN events e ON e.id=j.event_id LEFT JOIN topics t ON t.id=j.topic_id
                    WHERE (? IS NULL OR j.id=?) ORDER BY j.id DESC LIMIT 100""",
                    (job, job),
                ),
                "runs": rows(
                    c,
                    "SELECT * FROM runs WHERE (? IS NULL OR job_id=?) ORDER BY id DESC LIMIT 100",
                    (job, job),
                ),
                "usage": usage(c),
                "limits": config(c),
                "concurrency": resources.capacity(c),
                "mode": runner.mode,
                "profiles": {role: runner.profile(role) for role in CONTRACTS},
            }

    @app.post("/api/jobs/{ident}/retry")
    def retry(ident: int):
        with store.connection(True) as c:
            j = require(c, "jobs", ident)
            if j["status"] not in ("deferred", "failed"):
                raise ValueError("任务当前无需重试")
            if j["kind"] == "discussion":
                return {"job": discussion.retry(c, j)}
            return {
                "job": enqueue(
                    c, j["kind"], event=j["event_id"], topic=j["topic_id"], args=json.loads(j["args"])
                )
            }

    @app.post("/api/settings")
    def settings(data: Limits):
        if data.verify_reserve > min(data.daily_calls, data.monthly_calls):
            raise ValueError("核验预留不能超过日额度或月额度")
        with store.connection(True) as c:
            for key, value in data.model_dump(exclude_none=True).items():
                c.execute("UPDATE settings SET value=? WHERE key=?", (dump(value), key))
            return config(c)

    dist = Path(__file__).resolve().parents[1] / "frontend/dist"
    if (dist / "assets").is_dir():
        app.mount("/assets", StaticFiles(directory=dist / "assets"), name="assets")

    @app.get("/")
    def index():
        if not (dist / "index.html").is_file():
            raise HTTPException(503, "请先在 frontend 运行 npm run build")
        return FileResponse(dist / "index.html")

    return app
