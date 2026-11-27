"""A durable queue with bounded model calls. External I/O runs outside transactions."""
from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time
import uuid
from datetime import datetime, timedelta

from . import discussion, feeds, forecast, materials, research, resources, scenarios, watch, workflow
from .agents import Runner, CONTRACTS, DiscussionAnswer
from .db import (
    DispatchDeferred,
    add_event,
    book_run,
    config,
    dump,
    enqueue,
    instant,
    later,
    node,
    one,
    require,
    rows,
    usage,
    utc,
)

log = logging.getLogger("evosight.worker")


STUDY_BATCH = 16


def evidence(c, event, *, pending=False, limit=STUDY_BATCH):
    query = "SELECT d.* FROM documents d JOIN event_docs e ON d.id=e.document_id WHERE e.event_id=?"
    if pending:
        query += " AND e.analyzed=0 AND e.intake IN ('initial','window','revision')"
    else:
        query += " AND e.intake NOT IN ('future','person_candidate')"
    return rows(
        c,
        query + " ORDER BY (e.intake IN ('initial','window','revision')) DESC,d.id DESC LIMIT ?",
        (event, limit),
    )


def compact(docs):
    return [
        {
            k: (v[:1000] if k == "summary" else v)
            for k, v in doc.items()
            if k
            in (
                "id",
                "url",
                "title",
                "summary",
                "published_at",
                "source_updated_at",
                "published_label",
                "supersedes",
            )
            and (k != "published_at" or doc["source_times_checked"])
        }
        for doc in docs
    ]


def forecast_fragments(c, docs):
    """Share a bounded set of original passages, round-robin across the attached documents."""
    result = []
    remaining = 12000
    for fragment in materials.fragments_for(c, [d["id"] for d in docs[:12]]):
        size = min(remaining, 2400 if "摘要" in fragment["locator"] else 1300)
        if size < 150 or len(result) >= 18:
            break
        passage = fragment["text"][:size]
        result.append(
            {
                "id": fragment["id"],
                "document_id": fragment["document_id"],
                "locator": fragment["locator"],
                "text": passage,
                "truncated": len(fragment["text"]) > len(passage),
            }
        )
        remaining -= len(passage)
    return result


def admit(c, ident):
    candidate = require(c, "candidates", ident)
    if candidate["state"] == "accepted":
        return candidate["target_id"]
    if candidate["state"] != "review":
        raise ValueError("该候选已关闭")
    existing = one(c, "SELECT id,status FROM events WHERE title=?", (candidate["title"],))
    if existing:
        target = existing["id"]
    else:
        target = add_event(
            c,
            candidate["title"],
            candidate["scope"],
            json.loads(candidate["feeds"]),
            topic_id=candidate["topic_id"],
        )
        c.executemany(
            "INSERT OR IGNORE INTO event_docs(event_id,document_id) VALUES(?,?)",
            [(target, i) for i in json.loads(candidate["refs"])],
        )
        workflow.begin_event(c, target)
    c.execute("UPDATE candidates SET state='accepted',target_id=? WHERE id=?", (target, ident))
    return target


def candidates(c, proposals, event, topic, refs, sources, *, allow_auto=True):
    limits = config(c)
    used = usage(c)
    available = all(
        used[k + "_used"] < limits[k + "_calls"] - limits["verify_reserve"] for k in ("daily", "monthly")
    )
    accepted = (
        c.execute(
            "SELECT count(*) FROM candidates WHERE topic_id=? AND state='accepted' AND created_at>=?",
            (topic["id"], later(-21600)),
        ).fetchone()[0]
        if topic
        else 0
    )
    for p in proposals[:4]:
        if not p.title.strip() or not p.scope.strip() or not refs:
            continue
        cur = c.execute(
            "INSERT OR IGNORE INTO candidates(event_id,topic_id,title,scope,reason,refs,feeds,created_at) VALUES(?,?,?,?,?,?,?,?)",
            (
                event,
                topic["id"] if topic else None,
                p.title.strip()[:250],
                p.scope,
                p.reason,
                dump(refs),
                dump(sources),
                utc(),
            ),
        )
        if not cur.rowcount:
            continue
        if (
            not allow_auto
            or not available
            or not topic
            or not topic["discovery"]
            or accepted >= limits["auto_limit"]
        ):
            continue
        words = json.loads(topic["keywords"])
        if any(word.casefold() in (p.title + " " + p.scope).casefold() for word in words):
            try:
                admit(c, cur.lastrowid)
                accepted += 1
            except ValueError:
                pass


def schedule(c):
    now = utc()
    c.execute("UPDATE predictions SET status='frozen' WHERE status='open' AND deadline<=?", (now,))
    c.execute(
        "UPDATE fixed_questions SET reason='到达截止时间前未提交有效回答' WHERE prediction_id IS NULL AND reason='' AND deadline<=?",
        (now,),
    )
    limits = config(c)
    if limits["global_pause"]:
        return
    used = usage(c)
    for j in rows(c, "SELECT * FROM jobs WHERE status='deferred' ORDER BY id"):
        reserve = 0 if j["kind"] == "verify" else limits["verify_reserve"]
        if any(used[k + "_used"] >= limits[k + "_calls"] - reserve for k in ("daily", "monthly")):
            continue
        if not one(
            c,
            "SELECT 1 FROM jobs WHERE dedupe=? AND id!=? AND (id>? OR status IN ('queued','running'))",
            (j["dedupe"], j["id"], j["id"]),
        ):
            c.execute("UPDATE jobs SET status='queued',retry_at=NULL,finished_at=NULL WHERE id=?", (j["id"],))
    for p in rows(
        c,
        """SELECT p.id,p.event_id FROM predictions p JOIN events e ON e.id=p.event_id WHERE e.demo=0 AND
           (p.status IN ('review','frozen') OR (p.status='open' AND EXISTS(
              SELECT 1 FROM round_prediction_gates g JOIN tracking_rounds r ON r.id=g.round_id
              WHERE g.prediction_id=p.id AND g.state='checking' AND r.state IN ('published','partial'))))""",
    ):
        prev = one(
            c,
            "SELECT finished_at,status FROM jobs WHERE kind='verify' AND event_id=? AND json_extract(args,'$.prediction_id')=? ORDER BY id DESC LIMIT 1",
            (p["event_id"], p["id"]),
        )
        if not prev or (prev["finished_at"] and prev["finished_at"] <= later(-21600)):
            enqueue(c, "verify", event=p["event_id"], args={"prediction_id": p["id"]})
    for e in rows(c, "SELECT * FROM events WHERE status='active' AND demo=0 AND next_run<=?", (now,)):
        if not one(c, "SELECT 1 FROM tracking_rounds WHERE event_id=?", (e["id"],)):
            workflow.begin_event(c, e["id"])
            continue
        if one(
            c,
            "SELECT 1 FROM tracking_rounds WHERE event_id=? AND state IN ('queued','planning','researching') AND job_id IN (SELECT id FROM jobs WHERE status IN ('queued','running','deferred'))",
            (e["id"],),
        ):
            continue
        end = datetime.fromisoformat(e["next_run"])
        span = timedelta(hours=e["cadence"])
        workflow.request_round(
            c, e["id"], "周期检查", start=utc(end - span), end=utc(end), key="slot/" + e["next_run"]
        )
        c.execute("UPDATE events SET next_run=? WHERE id=?", (utc(end + span), e["id"]))
    for t in rows(c, "SELECT * FROM topics WHERE discovery=1 AND next_run<=?", (now,)):
        if not one(c, "SELECT 1 FROM events WHERE topic_id=? AND status='active' LIMIT 1", (t["id"],)):
            enqueue(c, "discover", topic=t["id"], slot=f"topic:{t['id']}:{t['next_run']}")
        c.execute("UPDATE topics SET next_run=? WHERE id=?", (later(21600), t["id"]))


class Worker:
    def __init__(self, store, runner=None, fetcher=None, *, acquisition=None):
        self.store = store
        self.runner = runner or Runner()
        self.poll = (
            feeds.poll if fetcher is None else lambda source, cached: feeds.FeedPage(fetcher(source), source)
        )
        self.acquisition = acquisition or materials.Acquisition()
        if fetcher is not None and acquisition is None:
            self.acquisition.poll = lambda source, cached=None: self.poll(source.url, cached)
        self.watcher = watch.Watcher(store, self.acquisition)
        self.owner = uuid.uuid4().hex
        self.stop = threading.Event()
        self.thread = None

    def start(self):
        self.dispatchers = [
            threading.Thread(target=self.loop, args=(i,), name=f"evosight-worker-{i+1}", daemon=True)
            for i in range(resources.MAX_AGENTS)
        ]
        self.thread = self.dispatchers[0]
        for dispatcher in self.dispatchers:
            dispatcher.start()
        self.watch_thread = threading.Thread(
            target=self.watcher.loop, name="evosight-news-watch", daemon=True
        )
        self.watch_thread.start()

    def loop(self, index=0):
        while not self.stop.is_set():
            try:
                if index:
                    with self.store.connection() as c:
                        enabled = index < config(c)["max_agent_concurrency"]
                    if not enabled:
                        self.stop.wait(4)
                        continue
                worked = self.step()
            except Exception:
                log.exception("任务调度失败")
                worked = False
            self.stop.wait(0.2 if worked else 4)

    def step(self):
        with self.store.connection(True) as c:
            schedule(c)
            expired = rows(c, "SELECT id FROM jobs WHERE status='running' AND lease<?", (utc(),))
            for old in expired:
                c.execute(
                    "UPDATE runs SET status='interrupted' WHERE job_id=? AND status='running'", (old["id"],)
                )
                c.execute(
                    "UPDATE jobs SET status='queued',owner=NULL,info='上次运行中断，已恢复排队' WHERE id=?", (old["id"],)
                )
            if config(c)["global_pause"]:
                return False
            running = rows(c, "SELECT kind FROM jobs WHERE status='running'")
            if len(running) >= resources.capacity(c)["effective"]:
                return False
            writer_busy = any(j["kind"] != "discussion" for j in running)
            job = one(
                c,
                """SELECT * FROM jobs WHERE status='queued' AND (retry_at IS NULL OR retry_at<=?)
                      AND (?=0 OR kind='discussion')
                      ORDER BY CASE WHEN kind='verify' THEN 0 WHEN kind='discussion' THEN 1
                      WHEN kind='collect' AND EXISTS(SELECT 1 FROM tracking_rounds r,json_each(r.reasons) why
                        WHERE r.job_id=jobs.id AND why.value='新增收集范围') THEN 2
                      WHEN kind='forecast' THEN 3 ELSE 4 END,id LIMIT 1""",
                (utc(), writer_busy),
            )
            if not job:
                return False
            c.execute(
                "UPDATE jobs SET status='running',attempts=attempts+1,owner=?,started_at=?,lease=? WHERE id=?",
                (self.owner, utc(), later(90), job["id"]),
            )
        done = threading.Event()

        def heartbeat():
            while not done.wait(20):
                with self.store.connection(True) as c:
                    c.execute(
                        "UPDATE jobs SET lease=? WHERE id=? AND owner=? AND status='running'",
                        (later(90), job["id"], self.owner),
                    )

        thread = threading.Thread(target=heartbeat, daemon=True)
        thread.start()
        status = "done"
        try:
            msg = self.process(job)
        except resources.ResourcePressure as err:
            msg, status = str(err), "resource_wait"
        except DispatchDeferred as err:
            msg, status = str(err), "deferred"
        except (ValueError, RuntimeError, OSError, sqlite3.IntegrityError) as err:
            msg = str(err)[:350] if isinstance(err, (ValueError, RuntimeError)) else "本地执行失败，请检查服务、权限与数据"
            status = "failed"
        except Exception:
            log.exception("任务执行失败")
            msg, status = "任务异常，已保留已采集的材料", "failed"
        finally:
            done.set()
            thread.join(timeout=2)
        with self.store.connection(True) as c:
            if status == "resource_wait":
                c.execute(
                    """UPDATE jobs SET status='queued',info=?,retry_at=?,owner=NULL,lease=NULL,
                       attempts=max(0,attempts-1) WHERE id=? AND owner=?""",
                    (msg, later(15), job["id"], self.owner),
                )
            elif status == "failed" and job["attempts"] < 1 and not self.stop.is_set():
                c.execute(
                    "UPDATE jobs SET status='queued',info=?,retry_at=?,owner=NULL WHERE id=? AND owner=?",
                    (msg, later(45), job["id"], self.owner),
                )
            else:
                c.execute(
                    "UPDATE jobs SET status=?,info=?,finished_at=?,owner=NULL WHERE id=? AND owner=?",
                    (status, msg, utc(), job["id"], self.owner),
                )
                rid = json.loads(job["args"]).get("round_id")
                if rid and status == "done":
                    c.execute(
                        """UPDATE tracking_rounds SET state='recovered',detail=?,finished_at=?
                              WHERE state='waiting' AND id IN (SELECT round_id FROM round_sources WHERE recovery_round=? AND state='recovered')
                              AND NOT EXISTS (SELECT 1 FROM round_sources s WHERE s.round_id=tracking_rounds.id AND s.state='missing')
                              AND EXISTS(SELECT 1 FROM tracking_rounds r WHERE r.id=? AND r.state IN ('unchanged','published','partial'))""",
                        (f"缺失来源已由轮次 #{rid} 补查", utc(), rid, rid),
                    )
                if rid and status in ("failed", "deferred"):
                    c.execute(
                        """UPDATE tracking_rounds SET state=?,detail=? WHERE id=? AND state
                              NOT IN ('superseded','published','unchanged')""",
                        (status, msg, rid),
                    )
                elif (
                    rid
                    and status == "done"
                    and job["kind"] == "research_retry"
                    and workflow.goal_matches(c, require(c, "tracking_rounds", rid))
                    and one(
                        c,
                        "SELECT 1 FROM research_tasks WHERE round_id=? AND state='deferred' LIMIT 1",
                        (rid,),
                    )
                ):
                    c.execute(
                        "UPDATE jobs SET status='deferred',info='额度恢复后补查未完成角色' WHERE id=?", (job["id"],)
                    )
        return True

    def call(self, job, role, ctx):
        value, model, _ = self.call_recorded(job, role, ctx)
        return value, model

    def call_recorded(self, job, role, ctx):
        model, effort = self.runner.profile(role)
        input_hash = workflow.fingerprint(model, effort, ctx)
        while True:
            with self.store.connection(True) as c:
                saved = one(
                    c,
                    "SELECT response,model,run_id FROM model_receipts WHERE job_id=? AND role=? AND input_hash=?",
                    (job["id"], role, input_hash),
                )
                if saved:
                    return (
                        CONTRACTS[role].model_validate_json(saved["response"]),
                        saved["model"],
                        saved["run_id"],
                    )
                if self.stop.is_set():
                    raise resources.ResourcePressure("服务正在停止，恢复后继续排队")
                slots = resources.capacity(c)
                same_input = one(
                    c,
                    """SELECT 1 FROM runs r JOIN run_inputs i ON i.run_id=r.id
                    WHERE r.job_id=? AND r.role=? AND i.input_hash=? AND r.status='running' LIMIT 1""",
                    (job["id"], role, input_hash),
                )
                if not same_input and slots["active"] < slots["effective"]:
                    run = book_run(c, job["id"], role, model, effort)
                    c.execute("INSERT INTO run_inputs VALUES(?,?,?)", (run, input_hash, dump(ctx)))
                    break
                if not slots["effective"] and not slots["active"]:
                    raise resources.ResourcePressure("可用内存不足，等待资源恢复后继续")
            self.stop.wait(0.25)
        start = time.monotonic()
        try:
            value, tokens = self.runner.call(role, ctx)
            if role in ("discuss_codex", "discuss_dsh"):
                discussion.validate_answer(value, ctx)
            elif role == "forecast":
                scenarios.check_draft(value, ctx, utc())
            elif role == "verify" and value.outcome in ("yes", "no", "level"):
                scale = ctx["预测"].get("scale")
                if bool(scale) != (value.outcome == "level"):
                    raise ValueError("核验须沿用已登记的题型")
                if scale and (
                    value.observed_level is None
                    or value.observed_level >= len(scale["levels"])
                    or not value.observed_at
                    or not scale["window_start"] <= instant(value.observed_at) <= ctx["预测"]["deadline"]
                ):
                    raise ValueError("分级核验须给出有效档位及窗口内的观察时点")
        except Exception:
            with self.store.connection(True) as c:
                c.execute(
                    "UPDATE runs SET status='failed',elapsed=? WHERE id=?", (time.monotonic() - start, run)
                )
            raise
        with self.store.connection(True) as c:
            receipt = c.execute(
                "INSERT OR IGNORE INTO model_receipts VALUES(?,?,?,?,?,?)",
                (job["id"], role, input_hash, value.model_dump_json(), model, run),
            )
            c.execute(
                "UPDATE runs SET status='done',elapsed=?,input_tokens=?,output_tokens=? WHERE id=?",
                (time.monotonic() - start, *tokens, run),
            )
            if not receipt.rowcount:
                saved = one(
                    c,
                    "SELECT response,model,run_id FROM model_receipts WHERE job_id=? AND role=? AND input_hash=?",
                    (job["id"], role, input_hash),
                )
                return CONTRACTS[role].model_validate_json(saved["response"]), saved["model"], saved["run_id"]
        return value, model, run

    def pull(self, event, sources):
        success = 0
        ids = []
        for source in sources:
            checked = utc()
            state = None
            if event is not None:
                with self.store.connection() as c:
                    state = one(
                        c,
                        """SELECT s.last_success,v.resource,v.etag,v.modified FROM source_state s
                                      LEFT JOIN rss_validators v USING(event_id,feed)
                                      WHERE s.event_id=? AND s.feed=?""",
                        (event, source),
                    )
            try:
                page = self.poll(source, state)
                error = None
            except Exception as err:
                error = str(err)[:170] if isinstance(err, ValueError) else "无法读取该来源的 RSS/Atom"
            with self.store.connection(True) as c:
                if event is not None:
                    c.execute(
                        "UPDATE source_state SET last_attempt=?,error=?,last_success=CASE WHEN ? IS NULL THEN ? ELSE last_success END WHERE event_id=? AND feed=?",
                        (checked, error, error, checked, event, source),
                    )
                if error is not None:
                    continue
                success += 1
                since = state["last_success"] if state else None
                for article in reversed(page.entries):
                    intake = feeds.intake_period(article, since, checked)
                    ident = feeds.ingest(c, event, source, article, intake=intake)
                    if intake != "future":
                        ids.append(ident)
                if event is not None:
                    c.execute(
                        "INSERT INTO rss_validators VALUES(?,?,?,?,?) ON CONFLICT(event_id,feed) DO UPDATE SET resource=excluded.resource,etag=excluded.etag,modified=excluded.modified",
                        (event, source, page.resource, page.etag, page.modified),
                    )
        if event is not None and success:
            with self.store.connection(True) as c:
                # A future-dated entry can become eligible even when the feed returns 304.
                feeds.release_future(c, event, utc())
                # Archive the whole initial feed, but keep the initial model workload bounded.
                c.execute(
                    """UPDATE event_docs SET intake='backfill'
                    WHERE event_id=? AND intake='initial' AND analyzed=0 AND document_id NOT IN (
                        SELECT ed.document_id FROM event_docs ed JOIN documents d ON d.id=ed.document_id
                        WHERE ed.event_id=? AND ed.intake='initial' AND ed.analyzed=0
                        ORDER BY max(coalesce(d.published_at,''),coalesce(d.source_updated_at,''),
                                     coalesce(strftime('%Y-%m-%d',replace(d.published_label,'/','-')),'')) DESC,d.id DESC LIMIT ?)""",
                    (event, event, STUDY_BATCH),
                )
        return success, ids

    def process(self, job):
        event = job["event_id"]
        args = json.loads(job["args"])
        with self.store.connection() as c:
            finished = one(c, "SELECT info FROM job_results WHERE job_id=?", (job["id"],))
            if finished:
                return finished["info"]
            if job["kind"] == "discussion":
                turn = discussion.job_turn(c, event, job["id"], args["turn_id"])
        if job["kind"] == "discussion":
            ctx = json.loads(turn["context"])
            choice = ctx.get("workspace", {}).get("selection") or {}
            if choice.get("pending_actions"):
                answer = DiscussionAnswer(
                    kind="workflow", answer="已选择目标事件", citations=[], actions=choice["pending_actions"]
                )
                run = None
            else:
                answer, _, run = self.call_recorded(job, "discuss_" + turn["provider"], ctx)
            with self.store.connection(True) as c:
                existing = one(c, "SELECT info FROM job_results WHERE job_id=?", (job["id"],))
                if existing:
                    return existing["info"]
                discussion.save_answer(c, turn["id"], run, answer, ctx)
                if answer.kind == "workflow" or (answer.kind == "tracking" and ctx.get("workspace")):
                    applied = one(c, "SELECT 1 FROM workflow_actions WHERE turn_id=?", (turn["id"],))
                    return self.receipt(c, job, "已执行工作流指令" if applied else "等待澄清工作流指令")
                return self.receipt(c, job, "已处理收集范围指令" if answer.kind == "tracking" else "已回答讨论问题")
        if job["kind"] == "scheduled_collect":
            with self.store.connection(True) as c:
                done = one(c, "SELECT info FROM job_results WHERE job_id=?", (job["id"],))
                if done:
                    return done["info"]
                e = require(c, "events", event)
                if e["status"] != "active" or e["demo"]:
                    return self.receipt(c, job, "到时事件未开启跟踪，已跳过这次采集")
                rid, _ = workflow.request_round(
                    c, event, "定时调度", end=args["run_at"], key=args["dispatch_key"], fresh_inputs=True
                )
                c.execute(
                    "UPDATE workflow_actions SET round_id=? WHERE job_id=? AND action='schedule_run'",
                    (rid, job["id"]),
                )
                return self.receipt(c, job, f"定时采集已进入轮次 #{rid}")
        with self.store.connection() as c:
            if event:
                e = require(c, "events", event)
                sources = [
                    r["feed"] for r in rows(c, "SELECT feed FROM source_state WHERE event_id=?", (event,))
                ]
                topic = require(c, "topics", e["topic_id"]) if e["topic_id"] else None
                if e["demo"] or (e["status"] != "active" and job["kind"] != "verify"):
                    return "追踪已暂停或为演示事件"
            else:
                topic = require(c, "topics", job["topic_id"])
                sources = json.loads(topic["feeds"])
        if job["kind"] == "discover":
            if not topic["discovery"]:
                return "主题发现已关闭"
            if sources:
                ok, ids = self.pull(None, sources)
            else:
                found, _ = self.acquisition.discover(
                    topic["name"], " ".join(json.loads(topic["keywords"])), []
                )
                ids = []
                ok = 0
                for s in found:
                    with self.store.connection(True) as c:
                        c.execute(
                            "INSERT OR IGNORE INTO platforms(url,kind,name,next_poll) VALUES(?,?,?,?)",
                            (s.url, s.kind, s.name, utc()),
                        )
                    if self.watcher.poll(s):
                        ok += 1
                        with self.store.connection() as c:
                            ids.extend(
                                row[0]
                                for row in c.execute(
                                    "SELECT document_id FROM platform_entries WHERE platform=? ORDER BY id DESC LIMIT 12",
                                    (s.url,),
                                )
                            )
                sources = [s.url for s in found if s.kind == "feed"]
            if not ok:
                raise ValueError("本轮主题来源均未能核查")
            with self.store.connection() as c:
                docs = [require(c, "documents", i) for i in list(dict.fromkeys(ids))[-12:]]
            if not docs:
                return "来源暂无可发现的条目"
            draft, _ = self.call(
                job,
                "discover",
                {"主题": topic["name"], "授权词": json.loads(topic["keywords"]), "evidence": compact(docs)},
            )
            refs = self.allowed(draft.evidence_ids, docs, bool(draft.discoveries))
            with self.store.connection(True) as c:
                current = require(c, "topics", topic["id"])
                if current["discovery"]:
                    candidates(c, draft.discoveries, None, current, refs, sources)
                return self.receipt(c, job, f"发现 {len(draft.discoveries)} 个候选")
        if job["kind"] in ("collect", "research_retry"):
            return research.run_round(self, job, args.get("round_id"), candidates)
        with self.store.connection() as c:
            docs = evidence(c, event, limit=22)
            snapshot = workflow.latest(c, event)
            goal_version = workflow.current_goal(c, event)["version"]
            ctx = {
                "事件": e["title"],
                "范围": e["scope"],
                "研究判断": rows(
                    c,
                    "SELECT title,body,watch FROM nodes WHERE event_id=? AND kind='judgment' ORDER BY id DESC LIMIT 3",
                    (event,),
                ),
                "知识版本": snapshot["id"] if snapshot else None,
                "可用复盘经验": [x for x in workflow.applicable_lessons(c, event) if x["state"] != "withdrawn"],
                "关键观察与经验": rows(
                    c,
                    "SELECT id,origin,question,condition,answer,prediction_id FROM observations WHERE event_id=? ORDER BY (origin='待验证经验') DESC,id DESC LIMIT 14",
                    (event,),
                ),
            }
        if job["kind"] == "forecast":
            with self.store.connection() as c:
                if snapshot and snapshot["goal_version"] != goal_version:
                    return "目标已更新，等待当前目标的研究档案"
                if args.get("snapshot_id") and (not snapshot or args["snapshot_id"] != snapshot["id"]):
                    return "已有更新的预测审核轮次"
                if snapshot:
                    docs = workflow.documents_at(c, snapshot["id"])
                ctx["已有预测"] = rows(
                    c,
                    "SELECT p.id,p.question,p.deadline,p.rule,p.authority,r.p AS probability,r.id AS revision_id FROM predictions p JOIN revisions r ON r.id=(SELECT max(id) FROM revisions WHERE prediction_id=p.id) WHERE p.event_id=? AND p.status='open' AND p.deadline>?",
                    (event, utc()),
                )
                fixed = require(c, "fixed_questions", args["fixed_id"]) if args.get("fixed_id") else None
                if fixed:
                    if fixed["prediction_id"] or fixed["deadline"] <= utc():
                        return "固定题已提交或到期"
                    request = (
                        one(c, "SELECT * FROM fixed_forecast_requests WHERE fixed_id=?", (fixed["id"],)) or {}
                    )
                    fixed.update(
                        form=request.get("kind", "binary"),
                        window_start=request.get("window_start"),
                        scale=json.loads(request["scale"]) if request.get("scale") else None,
                    )
                    ctx["固定题"] = fixed
                available = []
                for pred in ctx["已有预测"]:
                    try:
                        workflow.assert_revisable(c, {**pred, "event_id": event})
                    except ValueError:
                        continue
                    scale = scenarios.definition(c, pred["id"])
                    if scale:
                        pred.update(scale=scale, outlook=scenarios.version(c, pred["revision_id"]))
                    available.append(pred)
                if ctx["已有预测"] and not available and not fixed:
                    return "已有预测正在核验，等待决定性证据"
                ctx["已有预测"] = available
                ctx["evidence_fragments"] = forecast_fragments(c, docs)
            if not docs:
                return self.note(job, event, fixed, "暂无可引用的证据")
            if job["attempts"] and job["info"]:
                ctx["上次校验反馈"] = job["info"]
            draft, model, run = self.call_recorded(job, "forecast", {**ctx, "evidence": compact(docs)})
            if draft.action == "hold":
                return self.note(job, event, fixed, draft.reason)
            refs = self.allowed(draft.evidence_ids, docs)
            if not set(draft.lesson_ids) <= {x["id"] for x in ctx["可用复盘经验"]}:
                raise ValueError("引用的复盘经验不属于本次输入")
            with self.store.connection(True) as c:
                current = workflow.latest(c, event)
                if (
                    require(c, "events", event)["status"] != "active"
                    or workflow.current_goal(c, event)["version"] != goal_version
                    or (current or {}).get("id") != (snapshot or {}).get("id")
                ):
                    return self.receipt(c, job, "事件状态或知识版本已改变，等待重新规划")
                if fixed or draft.action == "create":
                    fields = fixed or draft.model_dump()
                    ident = forecast.new(
                        c,
                        event,
                        fields["question"],
                        fields["deadline"],
                        fields["rule"],
                        fields["authority"],
                        draft.p,
                        draft.reason,
                        refs,
                        model,
                        cohort="fixed" if fixed else "auto",
                        scale=(fixed or {}).get("scale") or draft.scale,
                        outlook=draft.outlook,
                    )
                    if fixed:
                        c.execute(
                            "UPDATE fixed_questions SET prediction_id=?,reason='' WHERE id=?",
                            (ident, fixed["id"]),
                        )
                else:
                    if draft.prediction_id not in [p["id"] for p in ctx["已有预测"]]:
                        raise ValueError("模型返回了不属于此次任务的预测")
                    ident = draft.prediction_id
                    forecast.revise(c, ident, draft.p, draft.reason, refs, model, outlook=draft.outlook)
                rev = c.execute("SELECT max(id) FROM revisions WHERE prediction_id=?", (ident,)).fetchone()[0]
                c.execute(
                    "INSERT INTO revision_runs SELECT ?,run_id FROM run_inputs WHERE run_id=?", (rev, run)
                )
                c.executemany(
                    "INSERT OR IGNORE INTO revision_lessons VALUES(?,?)", [(rev, i) for i in draft.lesson_ids]
                )
                dependencies = [d.model_dump() for d in draft.dependencies]
                c.execute(
                    "UPDATE revision_contexts SET dependencies=? WHERE revision_id=?",
                    (dump(dependencies), rev),
                )
                for d in draft.dependencies:
                    workflow.observe(
                        c, event, "预测依赖", d.question, d.condition, refs=refs, pred=ident, due=d.due_at
                    )
                return self.receipt(c, job, "已提交可追溯的概率版本")
        if job["kind"] == "verify":
            ident = args["prediction_id"]
            with self.store.connection() as c:
                pred = forecast.detail(c, ident)
                registered = watch.sources(c, event)
                snap_id = args.get("snapshot_id") or (snapshot or {}).get("id")
                gate_ids = [
                    v[0]
                    for v in c.execute(
                        """SELECT g.round_id FROM round_prediction_gates g WHERE g.prediction_id=? AND g.state='checking'
                        AND EXISTS(SELECT 1 FROM knowledge_snapshots k WHERE k.round_id=g.round_id AND k.id<=?)""",
                        (ident, snap_id or 0),
                    )
                ]
            if pred["status"] in ("resolved", "unverifiable"):
                return "预测已裁定"
            if pred["scale"] and utc() < pred["deadline"]:
                with self.store.connection(True) as c:
                    self.checked(c, ident, snap_id, gate_ids, "clear", "分级观察窗口尚未结束，可按新进展修订预测")
                    return self.receipt(c, job, "分级观察窗口尚未结束")
            if registered:
                ok = 0
                unavailable = []
                for source in registered:
                    good = self.watcher.poll(source, fresh=True)
                    with self.store.connection(True) as c:
                        watch.take_entries(c, event, source, {"window_end": utc()}, good)
                    ok += int(good)
                    if not good:
                        unavailable.append(source)
            else:
                unavailable = []
                ok, _ = self.pull(event, sources)
            if not ok:
                raise ValueError("等待核验，认可来源暂不可用")
            with self.store.connection() as c:
                docs = evidence(c, event, limit=24)
                excerpts = forecast_fragments(c, docs) if pred["scale"] else []
            if not docs:
                return "等待决定性证据"
            verdict, model = self.call(
                job,
                "verify",
                {
                    **ctx,
                    "预测": {
                        k: pred[k]
                        for k in ("question", "rule", "authority", "deadline", "grace_hours", "scale")
                    },
                    "evidence": compact(docs),
                    **({"evidence_fragments": excerpts} if pred["scale"] else {}),
                },
            )
            if verdict.outcome == "pending":
                with self.store.connection(True) as c:
                    domains = workflow.authority_domains(pred["authority"])
                    authority_down = any(
                        workflow.at_authority(s.url, domains) or s.name in pred["authority"]
                        for s in unavailable
                    )
                    state = "checking" if verdict.decisive_candidate or authority_down else "clear"
                    self.checked(c, ident, snap_id, gate_ids, state, verdict.reason)
                    return self.receipt(c, job, "待补证 · " + verdict.reason)
            refs = self.allowed(verdict.evidence_ids, docs, verdict.outcome != "unverifiable")
            with self.store.connection(True) as c:
                outcome, public_at, reason = verdict.outcome, verdict.public_at, verdict.reason
                if outcome in ("yes", "no", "level"):
                    domains = workflow.authority_domains(pred["authority"])
                    published = {
                        d["published_at"]
                        for d in docs
                        if d["id"] in refs
                        and d["source_times_checked"]
                        and (not domains or workflow.at_authority(d["url"], domains))
                    }
                    try:
                        matched = instant(public_at) in published
                    except ValueError:
                        matched = False
                    if not matched:
                        outcome, public_at, reason = (
                            "conflict",
                            "",
                            "引用材料的认可来源或首次公开时间尚未核实，等待人工复核。" + reason,
                        )
                forecast.settle(
                    c,
                    ident,
                    outcome,
                    public_at,
                    reason,
                    refs,
                    verdict.lesson,
                    model,
                    observed_level=verdict.observed_level if outcome == "level" else None,
                    observed_at=verdict.observed_at,
                )
                self.checked(
                    c, ident, snap_id, gate_ids, "checking" if outcome == "conflict" else "settled", reason
                )
                return self.receipt(c, job, "已记录裁定及复盘")
        raise ValueError("未知任务类型")

    @staticmethod
    def checked(c, ident, snapshot, gate_ids, state, detail):
        if snapshot:
            c.execute(
                """INSERT INTO prediction_checks VALUES(?,?,?,?,?) ON CONFLICT(prediction_id,snapshot_id)
                         DO UPDATE SET state=excluded.state,detail=excluded.detail,updated_at=excluded.updated_at""",
                (ident, snapshot, state, detail, utc()),
            )
        c.executemany(
            "UPDATE round_prediction_gates SET state=? WHERE round_id=? AND prediction_id=?",
            [(state, i, ident) for i in gate_ids],
        )

    @staticmethod
    def allowed(ids, docs, required=True):
        valid = {d["id"] for d in docs}
        if (required and not ids) or any(i not in valid for i in ids):
            raise ValueError("模型未提供有效的输入证据引用")
        return list(dict.fromkeys(ids))

    @staticmethod
    def receipt(c, job, info):
        c.execute("INSERT INTO job_results(job_id,info) VALUES(?,?)", (job["id"], info))
        return info

    def note(self, job, event, fixed, reason):
        with self.store.connection(True) as c:
            c.execute(
                "INSERT INTO forecast_notes(event_id,decision,reason,created_at) VALUES(?,'hold',?,?)",
                (event, reason, utc()),
            )
            if fixed:
                c.execute("UPDATE fixed_questions SET reason=? WHERE id=?", (reason, fixed["id"]))
            return self.receipt(c, job, "维持观察 · " + reason)
