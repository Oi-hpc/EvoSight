"""Durable, evidence-scoped handoffs to an existing investigation session."""
import json

from . import board, materials, workflow
from .db import DispatchDeferred, dump, enqueue, one, require, rows


def permitted_documents(c, r, ids):
    """An admitted original must belong to the event and the sender's frozen window."""
    return [
        doc for doc in dict.fromkeys(ids)
        if one(
            c,
            """SELECT 1 FROM event_docs e JOIN documents d ON d.id=e.document_id
               WHERE e.event_id=? AND d.id=? AND e.intake NOT IN ('future','person_candidate')
               AND (d.published_at IS NULL OR d.published_at<=?)
               AND (d.source_updated_at IS NULL OR d.source_updated_at<=?)
               AND (d.observed_at<=? OR d.id IN (SELECT value FROM json_each(?)) OR EXISTS (
                 SELECT 1 FROM research_tasks t,json_each(t.input_ids) i WHERE t.round_id=? AND i.value=d.id))""",
            (r["event_id"], doc, r["window_end"], r["window_end"], r["window_end"],
             r["input_docs"] or "[]", r["id"]),
        )
    ]


def recipient_at(c, r, key):
    line = one(c, """SELECT l.*,t.round_id FROM investigation_lines l JOIN research_tasks t ON t.id=l.last_task
                     WHERE l.event_id=? AND l.goal_version=? AND l.line_key=?""",
               (r["event_id"], r["goal_version"], key))
    if not line or line["round_id"] <= r["id"]:
        return line
    # Planning a later round replaces last_task immediately, before that round
    # publishes. The earlier round's immutable plan still identifies its owner.
    historical = one(c, """SELECT t.id FROM tracking_rounds s JOIN research_tasks t ON t.round_id=s.id
                      JOIN json_each(s.plan,'$.tasks') a ON json_extract(a.value,'$.role')=t.role
                       AND json_extract(a.value,'$.question')=t.question
                      WHERE s.event_id=? AND s.goal_version=? AND s.id<=?
                        AND COALESCE(NULLIF(json_extract(a.value,'$.line'),''),
                                     t.role||'/'||COALESCE(NULLIF(t.target,''),t.question))=?
                      ORDER BY s.id DESC,t.id DESC LIMIT 1""", (r["event_id"], r["goal_version"], r["id"], key))
    return {**line, "last_task": historical["id"]} if historical else None


def post(c, r, source_task_id, recipient_line_key, question, body, document_ids, *,
         review_key=None, frozen_ids=None, user_input=None):
    """Save the board note and durable delivery intent in the caller's transaction."""
    event, goal = r["event_id"], r["goal_version"]
    docs = sorted(permitted_documents(c, r, document_ids))
    fragments = (materials.fragments_for(c, docs, per_document=None) if frozen_ids is None
                 else frozen_fragments(c, frozen_ids))
    fragment_ids = [f["id"] for f in fragments if f["document_id"] in docs]
    line = recipient_at(c, r, recipient_line_key)
    key = workflow.fingerprint(
        event, goal, r["id"], recipient_line_key, "review" if review_key is not None else "handoff",
        ("user_input", user_input) if user_input is not None else
        (review_key if review_key is not None else (docs, fragment_ids, body)),
    )
    prior = one(c, "SELECT note_id FROM investigation_wake_notes WHERE delivery_key=?", (key,))
    if prior:
        return prior["note_id"]
    ident = board.note(
        c, event, goal, question, recipient_line_key, body, "handoff",
        "用户调查要求" if user_input is not None else ("独立复核退回原调查线" if review_key is not None else "侦查员交接"),
        [], rid=r["id"], task=source_task_id, documents=docs,
        handoff_version=key if line and (fragment_ids or user_input) else None,
    )
    c.execute("INSERT OR IGNORE INTO handoff_inbox(note_id,documents) VALUES(?,?)", (ident, dump(docs)))
    if one(c, "SELECT 1 FROM investigation_wake_notes WHERE note_id=?", (ident,)):
        return ident
    if not line or not (fragment_ids or user_input) or not workflow.goal_matches(c, r):
        return ident
    if source_task_id == line["last_task"] and review_key is None:
        return ident
    # Supplemental research is terminal. Do not bounce the same assignment
    # between two investigators and spend the event's remaining inference budget.
    if review_key is None and one(c, "SELECT 1 FROM investigation_wakes WHERE task_id=?", (source_task_id,)):
        return ident
    grouped = one(
        c, """SELECT w.job_id FROM investigation_wakes w JOIN jobs j ON j.id=w.job_id
              WHERE w.round_id=? AND w.line_id=? AND w.owner_task=? AND w.context IS NULL
              AND j.status='queued' AND (SELECT count(*) FROM investigation_wake_notes n
                                          WHERE n.job_id=w.job_id)<12 ORDER BY w.job_id LIMIT 1""",
        (r["id"], line["id"], line["last_task"]),
    )
    if grouped:
        job_id = grouped["job_id"]
    else:
        job_id = enqueue(
            c, "investigation_wake", event=event, args={"source_round": r["id"], "dispatch_key": key},
            slot=f"investigation-wake/{key}",
        )
        c.execute("INSERT INTO investigation_wakes(job_id,round_id,line_id,owner_task) VALUES(?,?,?,?)",
                  (job_id, r["id"], line["id"], line["last_task"]))
    c.execute("INSERT INTO investigation_wake_notes(delivery_key,note_id,job_id,fragment_ids) VALUES(?,?,?,?)",
              (key, ident, job_id, dump(fragment_ids)))
    return ident


def run(worker, job):
    from . import research

    with worker.store.connection(True) as c:
        wake = one(c, "SELECT * FROM investigation_wakes WHERE job_id=?", (job["id"],))
        r = require(c, "tracking_rounds", wake["round_id"])
        line = require(c, "investigation_lines", wake["line_id"])
        if not workflow.goal_matches(c, r) or line["goal_version"] != r["goal_version"]:
            return stop_job(c, job["id"], "目标已更新，交接已停止")
        if message := forward_to_latest(c, r):
            return message
        if wake["context"]:
            ctx = json.loads(wake["context"])
            task = require(c, "research_tasks", wake["task_id"])
        else:
            owner = require(c, "research_tasks", wake["owner_task"])
            messages = rows(
                c, """SELECT n.id,n.question,n.body,i.documents,k.fragment_ids FROM investigation_wake_notes k
                      JOIN event_notes n ON n.id=k.note_id JOIN handoff_inbox i ON i.note_id=n.id
                      WHERE k.job_id=? ORDER BY n.id""", (job["id"],),
            )
            for message in messages:
                message["documents"] = permitted_documents(c, r, json.loads(message["documents"]))
                allowed = set(message["documents"])
                message["fragment_ids"] = [f["id"] for f in frozen_fragments(c, json.loads(message["fragment_ids"]))
                                           if f["document_id"] in allowed]
            inputs = list(dict.fromkeys(d for m in messages for d in m["documents"]))
            user_instruction = one(c, """SELECT 1 FROM research_inputs i JOIN investigation_wake_notes n
                                       ON n.note_id=i.wake_note WHERE n.job_id=?""", (job["id"],))
            if not inputs and not user_instruction:
                return stop_job(c, job["id"], "交接已无可用于本轮的原文")
            ctx = json.loads(r["context"]) if r["context"] else research.study_context(c, r)
            ctx.update({
                "待处理交接": messages, "原调查问题": owner["question"],
                "交接原文片段": list(dict.fromkeys(f for m in messages for f in m["fragment_ids"])),
                "上次调查结果": json.loads(owner["result"] or "{}"),
                "事件黑板": board.view(c, r["event_id"], r["goal_version"]),
            })
            question = f"交接 #{messages[0]['id']} · {owner['question']}"
            task_id = c.execute(
                """INSERT INTO research_tasks(round_id,role,question,target,time_scope,done_when,priority,input_ids)
                   VALUES(?,?,?,?,?,?,?,?)""",
                (r["id"], owner["role"], question, owner["target"], owner["time_scope"], owner["done_when"],
                 owner["priority"], dump(inputs)),
            ).lastrowid
            task = require(c, "research_tasks", task_id)
            c.execute("UPDATE investigation_wakes SET context=?,task_id=? WHERE job_id=?",
                      (dump(ctx), task_id, job["id"]))
    if task["state"] != "done":
        assignment = {k: task[k] for k in ("role", "question", "target", "time_scope", "done_when", "priority")}
        assignment["line"] = line["line_key"]
        budget = research.run_role(worker, job, r, {"tasks": [assignment]}, ctx, [], task)
        if budget:
            raise DispatchDeferred(budget)
    with worker.store.connection(True) as c:
        if not workflow.goal_matches(c, r):
            return stop_job(c, job["id"], "目标已更新，交接已停止")
        task = require(c, "research_tasks", task["id"])
        if task["state"] != "done":
            raise ValueError(task["detail"] or "交接调查尚未完成")
        last_run = one(c, "SELECT max(run_id) AS id FROM model_receipts WHERE job_id=? AND role=?",
                       (job["id"], task["role"]))
        c.execute(
            """UPDATE handoff_inbox SET received_run=? WHERE received_run IS NULL AND note_id IN
               (SELECT note_id FROM investigation_wake_notes WHERE job_id=?)""", (last_run["id"], job["id"]),
        )
        enqueue(c, "handoff_publish", event=r["event_id"],
                args={"source_round": r["id"], "dispatch_key": str(job["id"])},
                slot=f"handoff-publish/{job['id']}")
        return worker.receipt(c, job, f"原调查线已核对交接，补查任务 #{task['id']}")


def unpublished(c, rid):
    return one(
        c, """SELECT 1 FROM investigation_wakes w JOIN research_tasks t ON t.id=w.task_id
              WHERE w.round_id=? AND t.state='done' AND w.snapshot_id IS NULL AND EXISTS (
                  SELECT 1 FROM investigation_wake_notes n WHERE n.job_id=w.job_id AND n.forwarded_to IS NULL
                  AND NOT EXISTS(SELECT 1 FROM investigation_wake_stops s WHERE s.note_id=n.note_id))
              LIMIT 1""", (rid,),
    )


def stop_job(c, job, reason):
    c.execute("""INSERT OR IGNORE INTO investigation_wake_stops SELECT note_id,? FROM investigation_wake_notes
                 WHERE job_id=? AND forwarded_to IS NULL""", (reason, job))
    return reason


def forward_to_latest(c, r):
    """Do not make an earlier round's judgment current again after a later window has published."""
    latest = workflow.latest(c, r["event_id"])
    if not latest or latest["round_id"] == r["id"]:
        return ""
    if latest["goal_version"] != r["goal_version"]:
        return "目标已更新，旧窗口交接已停止"
    if latest["round_id"] is not None:
        active = require(c, "tracking_rounds", latest["round_id"])
        if (active["window_end"], active["id"]) <= (r["window_end"], r["id"]):
            return ""
    elif latest["id"] <= (r["parent_snapshot"] or 0):
        return ""
    else:
        active = None
    messages = rows(c, """SELECT n.*,k.fragment_ids,i.documents FROM investigation_wakes w
                       JOIN investigation_wake_notes k ON k.job_id=w.job_id JOIN event_notes n ON n.id=k.note_id
                       JOIN handoff_inbox i ON i.note_id=n.id
                       WHERE w.round_id=? AND w.snapshot_id IS NULL AND k.forwarded_to IS NULL
                       AND NOT EXISTS(SELECT 1 FROM investigation_wake_stops s WHERE s.note_id=n.id)""", (r["id"],))
    if active is None:
        # A manual/index correction has no round to receive the handoff. Rebase a
        # narrow research continuation on that exact snapshot and retain its corrections.
        if not messages:
            return "原文已转交索引更新后的调查线"
        key = f"handoff-after-index/{r['id']}/{latest['id']}"
        docs = list(dict.fromkeys(d for m in messages for d in json.loads(m["documents"])))
        original_plan = json.loads(r["plan"] or "{}")
        plan = {"tasks": original_plan.get("tasks", []), "checklist": original_plan.get("checklist", []),
                "rationale": "按最新事件索引复核交接的原始材料", "postponed": []}
        c.execute(
            """INSERT OR IGNORE INTO tracking_rounds(event_id,goal_version,request_key,reasons,window_start,
               window_end,state,created_at,parent_snapshot,input_docs,plan)
               VALUES(?,?,?,?,?,?,'researching',?,?,?,?)""",
            (r["event_id"], r["goal_version"], key, dump(["索引更新后核对交接原文"]), r["window_end"],
             max(r["window_end"], latest["created_at"]), latest["created_at"], latest["id"], dump(docs), dump(plan)),
        )
        active = one(c, "SELECT * FROM tracking_rounds WHERE request_key=?", (key,))
    if not workflow.goal_matches(c, active):
        return "目标已更新，旧窗口交接已停止"
    stopped = False
    for message in messages:
        instruction = one(c, "SELECT id FROM research_inputs WHERE wake_note=?", (message["id"],))
        target = post(c, active, None, message["subject"], message["question"], message["body"],
                      json.loads(message["documents"]), frozen_ids=json.loads(message["fragment_ids"]),
                      review_key=(f"forward/{message['id']}" if message["reason"] == "独立复核退回原调查线" else None),
                      user_input=instruction["id"] if instruction else None)
        if not one(c, "SELECT 1 FROM investigation_wake_notes WHERE note_id=?", (target,)):
            c.execute("INSERT OR IGNORE INTO investigation_wake_stops VALUES(?,?)",
                      (message["id"], "新窗口没有可接收这份原文的调查线，交接已停止"))
            stopped = True
            continue
        c.execute("UPDATE investigation_wake_notes SET forwarded_to=? WHERE note_id=?", (target, message["id"]))
        if instruction:
            c.execute("UPDATE research_inputs SET wake_note=?,round_id=?,received_task_id=NULL WHERE id=?",
                      (target, active["id"], instruction["id"]))
        c.execute("""UPDATE tracking_rounds SET job_id=(SELECT job_id FROM investigation_wake_notes WHERE note_id=?)
                     WHERE id=? AND job_id IS NULL""", (target, active["id"]))
    return (f"已对照较新轮次 #{active['id']}；无法继续的交接已记录原因" if stopped
            else f"已有较新的判断，原文交由轮次 #{active['id']} 的原调查线核对")


def frozen_fragments(c, ids):
    """Read exactly the immutable paragraphs named at admission, even if a newer asset has appeared."""
    return rows(c, """SELECT f.id,a.document_id,f.locator,f.text,f.link,a.kind,d.title,d.url,
                    d.published_at,d.source_updated_at,d.source_times_checked
                 FROM json_each(?) i JOIN fragments f ON f.id=i.value JOIN evidence_assets a ON a.id=f.asset_id
                 JOIN documents d ON d.id=a.document_id ORDER BY i.key""", (dump(ids),))


def freeze_lookup(c, r, response, fragments):
    """A subsequent tool read may introduce a different original, once per document version."""
    docs = permitted_documents(c, r, response.get("documents", []))
    known = {f["document_id"] for f in fragments}
    extra = materials.fragments_for(c, [d for d in docs if d not in known], per_document=None)
    if "world_records" in response:
        response["world_records"] = [rec for rec in response["world_records"]
                                     if rec.get("refs") and set(rec["refs"]) <= set(docs)]
    return {**response, "documents": docs, "fragment_ids": [f["id"] for f in extra]}


def publication_fragments(c, r, docs):
    """Reuse the archived intake versions for documents delivered by a wake."""
    fragments = materials.fragments_for(c, docs, per_document=None)
    pinned = rows(c, """SELECT i.documents,n.fragment_ids FROM investigation_wake_notes n
                      JOIN handoff_inbox i ON i.note_id=n.note_id JOIN investigation_wakes w ON w.job_id=n.job_id
                      JOIN research_tasks t ON t.id=w.task_id WHERE w.round_id=? AND t.state='done'""", (r["id"],))
    if not pinned:
        return fragments
    for step in rows(c, """SELECT s.result FROM investigation_wakes w JOIN research_tool_steps s
                          ON s.task_id=w.task_id WHERE w.round_id=? ORDER BY w.job_id,s.step""", (r["id"],)):
        result = json.loads(step["result"])
        if result.get("fragment_ids"):
            pinned.append({"documents": dump(result["documents"]), "fragment_ids": dump(result["fragment_ids"])})
    restricted = {d for n in pinned for d in json.loads(n["documents"])}
    ids = list(dict.fromkeys(f for n in pinned for f in json.loads(n["fragment_ids"])))
    # Other completed roles may legitimately have cited an earlier rendition of
    # this same document. Keep those exact paragraphs available to publication.
    ids.extend(row[0] for row in c.execute(
        """SELECT DISTINCT json_extract(ref.value,'$.fragment_id') FROM research_tasks t,
           json_each(t.result,'$.findings') finding,json_each(finding.value,'$.citations') ref
           WHERE t.round_id=? AND t.state='done'""", (r["id"],)))
    available = set(docs)
    return [f for f in fragments if f["document_id"] not in restricted] + [
        f for f in frozen_fragments(c, list(dict.fromkeys(ids))) if f["document_id"] in restricted & available
    ]
