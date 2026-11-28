"""User instructions and investigator questions, scoped to one event goal."""
import json

from fastapi import Query
from pydantic import BaseModel, Field

from . import wakes, workflow
from .contracts import Assignment
from .db import one, require, rows, utc


class Reply(BaseModel):
    text: str = Field(min_length=1, max_length=1500)
    nonce: str = Field(min_length=1, max_length=100)
    goal_version: int = Field(ge=1)


class Instruction(Reply):
    round_id: int | None = Field(default=None, ge=1)
    line_key: str = Field(default="", max_length=900)


def current_round(c, event, version):
    return one(
        c,
        """SELECT * FROM tracking_rounds WHERE event_id=? AND goal_version=?
               AND NOT EXISTS (SELECT 1 FROM json_each(reasons) WHERE value='补采缺失窗口')
               ORDER BY window_end DESC,id DESC LIMIT 1""",
        (event, version),
    )


def check_goal(c, event, version):
    current = workflow.current_goal(c, event)
    if current["version"] != version:
        raise workflow.VersionConflict("目标已更新，请刷新后重新选择调查线")
    record = require(c, "events", event)
    if record["demo"] or record["status"] != "active":
        raise ValueError("只有正在追踪的真实事件能接收调查要求")


def owner(c, event, goal, key):
    if not key:
        return None
    line = one(
        c,
        """SELECT * FROM investigation_lines WHERE event_id=? AND goal_version=? AND line_key=?""",
        (event, goal, key),
    )
    if not line:
        raise ValueError("调查线不属于当前事件目标")
    return line


def route_round(c, event, goal, nonce, origin):
    latest = current_round(c, event, goal)
    if origin is not None:
        chosen = one(
            c,
            """SELECT * FROM tracking_rounds WHERE id=? AND event_id=? AND goal_version=?""",
            (origin, event, goal),
        )
        if not chosen:
            raise ValueError("轮次不属于当前事件目标")
        if (
            latest
            and latest["state"] == "queued"
            and chosen["state"] in ("planning", "researching")
            and "补采缺失窗口" not in json.loads(chosen["reasons"])
        ):
            latest = chosen
    if (
        latest
        and latest["state"] in ("queued", "planning", "researching")
        and not one(c, "SELECT 1 FROM research_synthesis_started WHERE round_id=?", (latest["id"],))
    ):
        return latest
    rid, _ = workflow.request_round(c, event, "用户补充调查", key="input/" + nonce, fresh_inputs=True)
    return require(c, "tracking_rounds", rid)


def assign(c, item_id, r, line):
    if not line or not r["plan"]:
        return
    target = wakes.recipient_at(c, r, line["line_key"])
    task = require(c, "research_tasks", target["last_task"]) if target and target["last_task"] else None
    if not task:
        return
    if task["round_id"] == r["id"] and task["state"] == "queued":
        c.execute("UPDATE research_inputs SET target_task_id=? WHERE id=?", (task["id"], item_id))
        return
    item = require(c, "research_inputs", item_id)
    body = item["text"] if item["answer"] is None else f"{item['text']}\n用户答复：{item['answer']}"
    doc_ids = json.loads(task["input_ids"] or "[]") or json.loads(r["input_docs"] or "[]")
    note = wakes.post(c, r, None, line["line_key"], f"用户调查要求 #{item_id}", body, doc_ids, user_input=item_id)
    c.execute("UPDATE research_inputs SET wake_note=? WHERE id=?", (note, item_id))


def destination(c, event, goal, nonce, origin, line):
    r = route_round(c, event, goal, nonce, origin)
    if not line and r["plan"]:
        tasks = json.loads(r["plan"]).get("tasks", [])
        choices = tasks if len(tasks) == 1 else [t for t in tasks if t["role"] == "specialist"]
        if len(choices) == 1:
            chosen = choices[0]
            key = chosen.get("line") or f"{chosen['role']}/{chosen['target'] or chosen['question']}"
            line = owner(c, event, goal, key)
        else:
            rid, _ = workflow.request_round(c, event, "用户补充调查", key="route/" + nonce, fresh_inputs=True)
            r = require(c, "tracking_rounds", rid)
    return r, line


def submit(c, event, data):
    body = data.text.strip()
    if not body:
        raise ValueError("请填写具体调查要求")
    check_goal(c, event, data.goal_version)
    signature = workflow.fingerprint(data.goal_version, data.round_id, data.line_key, body)
    old = one(c, "SELECT * FROM research_inputs WHERE event_id=? AND nonce=?", (event, data.nonce))
    if old:
        if old["submission"] != signature:
            raise workflow.VersionConflict("发送编号已用于另一条要求")
        return old["id"]
    line = owner(c, event, data.goal_version, data.line_key)
    r, line = destination(c, event, data.goal_version, data.nonce, data.round_id, line)
    ident = c.execute(
        """INSERT INTO research_inputs(event_id,goal_version,origin_round_id,round_id,kind,text,
                         line_key,role,nonce,submission,created_at) VALUES(?,?,?,?,'requirement',?,?,?,?,?,?)""",
        (
            event,
            data.goal_version,
            data.round_id or r["id"],
            r["id"],
            body,
            line["line_key"] if line else "",
            line["role"] if line else "main",
            data.nonce,
            signature,
            utc(),
        ),
    ).lastrowid
    assign(c, ident, r, line)
    return ident


def ask(c, r, role, line_key, text):
    text = text.strip()
    if not text or not workflow.goal_matches(c, r):
        return
    prior = one(
        c,
        """SELECT id FROM research_inputs WHERE event_id=? AND goal_version=? AND kind='question'
                      AND (origin_round_id=? OR answer IS NULL) AND line_key=? AND text=?""",
        (r["event_id"], r["goal_version"], r["id"], line_key, text),
    )
    if not prior:
        c.execute(
            """INSERT INTO research_inputs(event_id,goal_version,origin_round_id,round_id,kind,text,line_key,
                     role,created_at) VALUES(?,?,?,?,'question',?,?,?,?)""",
            (r["event_id"], r["goal_version"], r["id"], r["id"], text, line_key, role, utc()),
        )


def answer(c, event, ident, data):
    item = one(
        c, "SELECT * FROM research_inputs WHERE event_id=? AND id=? AND kind='question'", (event, ident)
    )
    if not item:
        raise KeyError("调查提问不存在")
    check_goal(c, event, data.goal_version)
    if item["goal_version"] != data.goal_version:
        raise workflow.VersionConflict("提问属于旧目标，请按当前目标提交新要求")
    text = data.text.strip()
    if not text:
        raise ValueError("请填写答复")
    if item["answer"] is not None:
        if item["answer_nonce"] == data.nonce and item["answer"] == text:
            return ident
        raise workflow.VersionConflict("这条问题已经收到答复")
    line = owner(c, event, data.goal_version, item["line_key"])
    r = route_round(c, event, data.goal_version, f"answer/{ident}", item["origin_round_id"])
    c.execute(
        "UPDATE research_inputs SET answer=?,answer_nonce=?,round_id=? WHERE id=?",
        (text, data.nonce, r["id"], ident),
    )
    assign(c, ident, r, line)
    return ident


def pending(c, rid):
    return rows(
        c,
        """SELECT id,kind,text,answer,line_key,role,target_task_id,received_task_id,wake_note
                      FROM research_inputs WHERE round_id=? AND (kind='requirement' OR answer IS NOT NULL)
                      ORDER BY id""",
        (rid,),
    )


def for_plan(c, r):
    return [{k: i[k] for k in ("id", "text", "answer", "line_key")} for i in pending(c, r["id"])]


def complete_assignments(c, r, plan):
    """Ensure that unassigned user instructions actually reach an evidence-gathering worker."""
    items = [i for i in pending(c, r["id"]) if i["kind"] == "requirement" and not i["line_key"]]
    unmatched = [i for i in items if not any(i["id"] in t.request_ids for t in plan.tasks)]
    if not unmatched or len(plan.tasks) == 1:
        return
    investigator = next((t for t in plan.tasks if t.role == "specialist"), None)
    if not investigator and len(plan.tasks) < 8:
        investigator = Assignment(
            role="specialist",
            line="用户专项调查",
            target="用户补充的核查事项",
            question=f"核查用户调查要求 #{unmatched[0]['id']} 等补充事项",
            time_scope="window",
            done_when="按用户要求核对原文，交代结论与缺口",
            priority=5,
        )
        plan.tasks.append(investigator)
    if investigator is None:
        investigator = max(plan.tasks, key=lambda t: t.priority)
    investigator.request_ids.extend(i["id"] for i in unmatched)


def bind_plan(c, r, plan):
    """Use the planner's explicit assignments; retain a user's chosen investigator."""
    r = require(c, "tracking_rounds", r["id"])
    for i in pending(c, r["id"]):
        if i["wake_note"] or i["target_task_id"] or (i["kind"] == "question" and not i["line_key"]):
            continue
        key = i["line_key"]
        if not key:
            chosen = next((t for t in plan.tasks if i["id"] in t.request_ids), None)
            if chosen is None and len(plan.tasks) == 1:
                chosen = plan.tasks[0]
            if chosen:
                key = chosen.line or f"{chosen.role}/{chosen.target or chosen.question}"
        if key:
            line = owner(c, r["event_id"], r["goal_version"], key)
            c.execute("UPDATE research_inputs SET line_key=?,role=? WHERE id=?", (key, line["role"], i["id"]))
            assign(c, i["id"], r, line)


def receive(c, r, task_id, job_id):
    items = rows(
        c,
        """SELECT i.id,i.kind,i.text,i.answer FROM research_inputs i
                   LEFT JOIN investigation_wake_notes w ON w.note_id=i.wake_note
                   WHERE i.round_id=? AND (i.target_task_id=? OR w.job_id=?)
                   AND (i.kind='requirement' OR i.answer IS NOT NULL) ORDER BY i.id""",
        (r["id"], task_id, job_id),
    )
    c.executemany(
        "UPDATE research_inputs SET received_task_id=? WHERE id=?", [(task_id, i["id"]) for i in items]
    )
    return items


def for_synthesis(c, r):
    """Seal this intake before building the prompt; new submissions get an active continuation."""
    c.execute("INSERT OR IGNORE INTO research_synthesis_started VALUES(?)", (r["id"],))
    c.execute(
        """UPDATE research_inputs SET main_received=1 WHERE round_id=? AND line_key=''
                 AND (kind='requirement' OR answer IS NOT NULL)""",
        (r["id"],),
    )
    return rows(
        c,
        """SELECT i.id,i.kind,i.text,i.answer,i.line_key,i.role FROM research_inputs i
                      LEFT JOIN research_tasks t ON t.id=i.received_task_id
                      WHERE i.round_id=? AND (i.main_received=1 OR t.state='done') ORDER BY i.id""",
        (r["id"],),
    )


def recover_unread(c, r):
    """An empty source window can finish before dispatch; retry with the next live window."""
    c.execute(
        """UPDATE research_inputs SET round_id=?,target_task_id=NULL
                 WHERE event_id=? AND goal_version=? AND received_task_id IS NULL AND main_received=0
                 AND wake_note IS NULL AND (kind='requirement' OR answer IS NOT NULL)
                 AND round_id IN (
                   SELECT s.id FROM tracking_rounds s LEFT JOIN jobs j ON j.id=s.job_id
                   WHERE s.event_id=? AND s.goal_version=? AND (s.window_end,s.id)<(?,?)
                   AND s.state IN ('waiting','failed','unchanged')
                   AND (j.status IS NULL OR j.status NOT IN ('queued','running','deferred')))""",
        (
            r["id"],
            r["event_id"],
            r["goal_version"],
            r["event_id"],
            r["goal_version"],
            r["window_end"],
            r["id"],
        ),
    )


def question_count(c, event):
    return c.execute(
        """SELECT count(*) FROM research_inputs WHERE event_id=?
                      AND goal_version=(SELECT max(version) FROM event_goals WHERE event_id=?)
                      AND kind='question' AND answer IS NULL""",
        (event, event),
    ).fetchone()[0]


def describe(c, i, goal):
    from . import steering_reports

    report = steering_reports.for_input(c, i["id"])
    r = require(c, "tracking_rounds", i["round_id"])
    stop = (
        one(
            c,
            """SELECT coalesce(s.reason,CASE WHEN j.status IN ('failed','cancelled') THEN j.info END) AS reason
                     FROM investigation_wake_notes n JOIN jobs j ON j.id=n.job_id
                     LEFT JOIN investigation_wake_stops s ON s.note_id=n.note_id WHERE n.note_id=?""",
            (i["wake_note"],),
        )
        if i["wake_note"]
        else None
    )
    detail = (stop or {}).get("reason")
    if not detail and i["received_task_id"]:
        failure = one(
            c,
            """SELECT detail FROM research_tasks WHERE id=? AND state IN ('cancelled','failed')""",
            (i["received_task_id"],),
        )
        if failure:
            detail = failure["detail"] or "本次调查未完成"
    if report and report.get("outcome") not in ("missing", "rejected"):
        status = "reported"
    elif i["goal_version"] != goal:
        status = "superseded"
    elif i["kind"] == "question" and i["answer"] is None:
        status = "awaiting_user"
    elif detail:
        status = "unavailable"
    elif i["received_task_id"] or i["main_received"]:
        status = "received"
    elif r["state"] in ("waiting", "failed", "cancelled", "superseded", "unchanged"):
        status = "unavailable"
    elif i["target_task_id"] or i["wake_note"] or r["plan"]:
        status = "assigned"
    else:
        status = "queued"
    return {
        **{
            k: i[k]
            for k in (
                "id",
                "origin_round_id",
                "round_id",
                "goal_version",
                "kind",
                "text",
                "answer",
                "line_key",
                "role",
                "created_at",
            )
        },
        "status": status,
        "report": report,
        "detail": (detail or r["detail"]) if status == "unavailable" else "",
    }


def register(app, store):
    @app.get("/api/events/{event}/steering")
    def view(event: int, before: int = Query(0, ge=0)):
        with store.connection() as c:
            goal = workflow.current_goal(c, event)["version"]
            r = current_round(c, event, goal)
            lines = rows(
                c,
                """SELECT line_key,role,question FROM investigation_lines
                            WHERE event_id=? AND goal_version=? ORDER BY id""",
                (event, goal),
            )
            items = rows(
                c,
                """SELECT * FROM research_inputs WHERE event_id=? AND (?=0 OR id<?)
                            ORDER BY id DESC LIMIT 50""",
                (event, before, before),
            )
            unanswered = rows(
                c,
                """SELECT * FROM research_inputs WHERE event_id=? AND goal_version=?
                                    AND kind='question' AND answer IS NULL ORDER BY id""",
                (event, goal),
            )
            return {
                "goal_version": goal,
                "current_round_id": r["id"] if r else None,
                "lines": lines,
                "pending_questions": [describe(c, i, goal) for i in unanswered],
                "items": [describe(c, i, goal) for i in items],
            }

    @app.post("/api/events/{event}/steering", status_code=201)
    def add(event: int, data: Instruction):
        with store.connection(True) as c:
            ident = submit(c, event, data)
            return describe(c, require(c, "research_inputs", ident), data.goal_version)

    @app.post("/api/events/{event}/steering/{ident}/answer")
    def respond(event: int, ident: int, data: Reply):
        with store.connection(True) as c:
            answer(c, event, ident, data)
            return describe(c, require(c, "research_inputs", ident), data.goal_version)
