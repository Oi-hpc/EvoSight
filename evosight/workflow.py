"""Event goals, persistent tracking rounds and versioned knowledge read models."""
from __future__ import annotations

import hashlib
import json
import re
from collections import defaultdict
from datetime import datetime, timedelta
from urllib.parse import urlsplit

from .contracts import ROLE_NAMES, SECTIONS
from . import world
from .db import config, dump, enqueue, instant, later, node, one, require, rows, utc


class VersionConflict(ValueError):
    pass


def fingerprint(*values):
    return hashlib.sha256(dump(values).encode()).hexdigest()


def main_lane(event):
    return f"{event or 'workspace'}/main"


def main_session_backend(c, event):
    row = one(c, "SELECT backend FROM agent_sessions WHERE lane=?", (main_lane(event),))
    return row["backend"] if row else None


def current_goal(c, event):
    goal = one(
        c,
        """SELECT g.* FROM event_goals g JOIN events e ON e.id=g.event_id
           WHERE g.event_id=? AND e.status!='deleted' ORDER BY g.version DESC LIMIT 1""",
        (event,),
    )
    if not goal:
        raise KeyError("事件不存在")
    goal["related"] = [
        row[0] for row in c.execute(
            """SELECT e.id FROM json_each(?) j JOIN events e ON e.id=j.value
               WHERE e.status!='deleted' ORDER BY j.key""",
            (goal["related"],),
        )
    ]
    return goal


def latest(c, event):
    return one(c, "SELECT * FROM knowledge_snapshots WHERE event_id=? ORDER BY id DESC LIMIT 1", (event,))


def request_round(c, event, reason, *, end=None, start=None, key=None, job_id=None, fresh_inputs=False):
    e = require(c, "events", event)
    if e["status"] != "active" or e["demo"]:
        raise ValueError("此事件未开启常规研究")
    goal = current_goal(c, event)
    last = latest(c, event)
    end = end or utc()
    previous = one(
        c,
        "SELECT window_end FROM tracking_rounds WHERE event_id=? AND state IN ('published','partial','unchanged','recovered') AND window_end<=? ORDER BY window_end DESC LIMIT 1",
        (event, end),
    )
    earliest = utc(datetime.fromisoformat(end) - timedelta(hours=e["cadence"]))
    start = start or max(earliest, (previous or {}).get("window_end", earliest))
    key = f"{event}/{goal['version']}/" + (key or f"{reason}/{end}")
    existing = one(c, "SELECT * FROM tracking_rounds WHERE request_key=?", (key,))
    # A source repair must read its original interval, even when that interval's research is still running.
    if not existing and reason != "补采缺失窗口":
        existing = one(
            c,
            """SELECT * FROM tracking_rounds WHERE event_id=? AND goal_version=? AND state IN ('queued','planning','researching')
                          AND window_start<=? AND window_end>=?
                          AND NOT EXISTS (SELECT 1 FROM json_each(reasons) WHERE value='补采缺失窗口')
                          AND (?=0 OR (state='queued' AND input_docs IS NULL AND job_id IN
                            (SELECT id FROM jobs WHERE status='queued'))) ORDER BY id DESC LIMIT 1""",
            (event, goal["version"], start, end, fresh_inputs),
        )
    if existing:
        reasons = list(dict.fromkeys([*json.loads(existing["reasons"]), reason]))
        c.execute("UPDATE tracking_rounds SET reasons=? WHERE id=?", (dump(reasons), existing["id"]))
        return existing["id"], existing["job_id"]
    rid = c.execute(
        """INSERT INTO tracking_rounds(event_id,goal_version,request_key,reasons,window_start,window_end,created_at,parent_snapshot)
                     VALUES(?,?,?,?,?,?,?,?)""",
        (event, goal["version"], key, dump([reason]), start, end, utc(), last["id"] if last else None),
    ).lastrowid
    job_id = job_id or enqueue(c, "collect", event=event, args={"round_id": rid})
    c.execute("UPDATE tracking_rounds SET job_id=? WHERE id=?", (job_id, rid))
    return rid, job_id


def begin_event(c, event):
    e = require(c, "events", event)
    rid, job = request_round(c, event, "初始化", key="initial")
    anchor = datetime.fromisoformat(e["created_at"])
    period = timedelta(hours=e["cadence"])
    intervals = max(0, (datetime.fromisoformat(utc()) - anchor) // period) + 1
    c.execute("UPDATE events SET next_run=? WHERE id=?", (utc(anchor + intervals * period), event))
    return {"id": event, "job": job, "round": rid}


EVENT_DELETED = "追踪事件已删除"


def delete_event(c, event):
    record = one(c, "SELECT status,demo FROM events WHERE id=?", (event,))
    if not record:
        raise KeyError("事件不存在")
    if record["demo"]:
        raise ValueError("演示事件只供阅读")
    if record["status"] == "deleted":
        return
    now = utc()
    c.execute("UPDATE events SET status='deleted' WHERE id=?", (event,))
    # A running worker keeps its slot until it exits, but its lease can no longer commit results.
    c.execute(
        """UPDATE jobs SET status='cancelled',info=?,finished_at=?,retry_at=NULL
           WHERE event_id=? AND status IN ('queued','deferred')""",
        (EVENT_DELETED, now, event),
    )
    c.execute(
        """UPDATE tracking_rounds SET state='cancelled',detail=?,finished_at=?
           WHERE event_id=? AND state IN ('queued','planning','researching','waiting','deferred')""",
        (EVENT_DELETED, now, event),
    )
    c.execute(
        """UPDATE research_tasks SET state='cancelled',detail=?
           WHERE state IN ('queued','running','deferred') AND round_id IN
           (SELECT id FROM tracking_rounds WHERE event_id=?)""",
        (EVENT_DELETED, event),
    )


def edit_goal(c, event, expected, *, title=None, scope=None, priority=None, related=None, status=None):
    e = require(c, "events", event)
    old = current_goal(c, event)
    if old["version"] != expected:
        raise VersionConflict("目标已被其他编辑更新，请读取最新版本")
    if e["demo"]:
        raise ValueError("演示事件只供阅读")
    status = status or e["status"]
    if status not in ("active", "paused", "archived"):
        raise ValueError("无效追踪状态")
    if (
        e["status"] != "active"
        and status == "active"
        and c.execute("SELECT count(*) FROM events WHERE status='active' AND demo=0").fetchone()[0]
        >= config(c)["capacity"]
    ):
        raise ValueError("活跃追踪容量已满")
    title = (old["title"] if title is None else title).strip()
    scope = (old["scope"] if scope is None else scope).strip()
    priority = old["priority"] if priority is None else priority
    related = list(dict.fromkeys(old["related"] if related is None else related))
    if not title or not scope or not 1 <= priority <= 5:
        raise ValueError("请填写事件、研究目标和有效优先级")
    for target in related:
        if target == event:
            raise ValueError("不能关联事件自身")
        require(c, "events", target)
    if (title, scope, priority, related, status) == (
        old["title"],
        old["scope"],
        old["priority"],
        old["related"],
        e["status"],
    ):
        return {"version": expected, "job": None}
    version = expected + 1
    c.execute(
        "INSERT INTO event_goals VALUES(?,?,?,?,?,?,?)",
        (event, version, title, scope, priority, dump(related), utc()),
    )
    c.execute("UPDATE events SET title=?,scope=?,status=? WHERE id=?", (title, scope, status, event))
    if (title, scope) != (old["title"], old["scope"]):
        c.execute("UPDATE event_platforms SET consumed=0 WHERE event_id=?", (event,))
    c.execute(
        "UPDATE tracking_rounds SET state='superseded',detail='目标版本已更新' WHERE event_id=? AND state IN ('queued','planning','researching','waiting')",
        (event,),
    )
    c.execute("UPDATE end_proposals SET status='superseded' WHERE event_id=? AND status='pending'", (event,))
    if status == "active":
        rid, job = request_round(
            c, event, "恢复追踪" if e["status"] != "active" else "目标更新", key=f"goal/{version}"
        )
        return {"version": version, "round": rid, "job": job}
    return {"version": version, "job": None}


def respond_end(c, event, proposal, choice):
    goal = current_goal(c, event)
    item = require(c, "end_proposals", proposal)
    if item["event_id"] != event:
        raise KeyError()
    if item["status"] == choice:
        return {"status": choice}
    if item["status"] != "pending" or item["goal_version"] != goal["version"]:
        raise VersionConflict("结束建议已处理或目标已更新")
    c.execute("UPDATE end_proposals SET status=? WHERE id=?", (choice, proposal))
    if choice == "archived":
        edit_goal(c, event, item["goal_version"], status="archived")
    return {"status": choice}


def observe(
    c, event, origin, question, condition, *, refs=(), pred=None, resolution=None, due="", priority=2
):
    if not question.strip():
        return
    if pred and require(c, "predictions", pred)["event_id"] != event:
        raise ValueError("观察项关联的预测不属于该事件")
    try:
        due = instant(due) if due else later(21600)
    except ValueError:
        due = later(21600)
    c.execute(
        """INSERT OR IGNORE INTO observations(event_id,prediction_id,origin,question,condition,due_at,priority,refs,resolution_id,fingerprint)
               VALUES(?,?,?,?,?,?,?,?,?,?)""",
        (
            event,
            pred,
            origin,
            question.strip(),
            condition.strip(),
            due,
            priority,
            dump(list(refs)),
            resolution,
            fingerprint(origin, pred, resolution, question.strip()),
        ),
    )


def applicable_lessons(c, event):
    linked = current_goal(c, event)["related"]
    records = rows(
        c,
        """SELECT o.id,o.event_id,e.title,o.question,o.condition,o.state FROM observations o JOIN events e ON e.id=o.event_id
                    WHERE o.origin='待验证经验' AND e.status!='deleted'
                    AND e.demo=(SELECT demo FROM events WHERE id=?) AND (o.event_id=? OR EXISTS(SELECT 1 FROM json_each(?) j WHERE j.value=o.event_id)
                      OR EXISTS(SELECT 1 FROM snapshot_claims a JOIN world_versions va ON va.claim_id=a.claim_id
                                JOIN world_versions vb ON vb.entry_id=va.entry_id JOIN snapshot_claims b ON b.claim_id=vb.claim_id
                                WHERE a.snapshot_id=(SELECT max(id) FROM knowledge_snapshots WHERE event_id=?)
                                AND b.snapshot_id=(SELECT max(id) FROM knowledge_snapshots WHERE event_id=o.event_id))
                      OR EXISTS(SELECT 1 FROM claims a JOIN claims b ON b.entity_id=a.entity_id
                                WHERE a.event_id=? AND b.event_id=o.event_id)
                      OR EXISTS(SELECT 1 FROM event_docs a JOIN event_docs b ON a.document_id=b.document_id
                                WHERE a.event_id=? AND b.event_id=o.event_id)
                      OR EXISTS(SELECT 1 FROM lesson_reviews r WHERE r.lesson_id=o.id AND r.event_id=?))
                    ORDER BY o.id DESC LIMIT 24""",
        (event, event, dump(linked), event, event, event, event),
    )
    for record in records:
        record["reviews"] = rows(
            c,
            """SELECT r.event_id,e.title,r.outcome,r.reason,r.fragment_id,a.document_id,r.created_at
                                FROM lesson_reviews r JOIN events e ON e.id=r.event_id JOIN fragments f ON f.id=r.fragment_id
                                JOIN evidence_assets a ON a.id=f.asset_id
                                WHERE r.lesson_id=? AND e.status!='deleted' ORDER BY r.id""",
            (record["id"],),
        )
    return records


def review_lesson(c, event, lesson_id, fragment_id, outcome, reason):
    lesson = require(c, "observations", lesson_id)
    require(c, "events", lesson["event_id"])
    if require(c, "events", event)["demo"] or lesson["origin"] != "待验证经验" or not reason.strip():
        raise ValueError("请选择复盘经验并说明适用范围或反例")
    if outcome == "withdraw" and lesson["event_id"] != event:
        raise ValueError("请从提出经验的原事件撤回")
    if outcome in ("support", "counter") and lesson["event_id"] == event:
        raise ValueError("需要用另一事件的独立材料复核经验")
    evidence = one(
        c,
        """SELECT a.document_id FROM fragments f JOIN evidence_assets a ON a.id=f.asset_id JOIN event_docs ed ON ed.document_id=a.document_id
                   WHERE f.id=? AND ed.event_id=?""",
        (fragment_id, event),
    )
    if not evidence:
        raise ValueError("该证据片段不属于当前事件")
    if outcome in ("support", "counter") and evidence["document_id"] in json.loads(lesson["refs"]):
        raise ValueError("这份材料已用于提出该经验，请引用独立的新证据")
    c.execute(
        "INSERT OR IGNORE INTO lesson_reviews(lesson_id,event_id,fragment_id,outcome,reason,created_at) VALUES(?,?,?,?,?,?)",
        (lesson_id, event, fragment_id, outcome, reason.strip(), utc()),
    )
    if outcome == "withdraw":
        c.execute("UPDATE observations SET state='withdrawn' WHERE id=?", (lesson_id,))
    return {"id": lesson_id, "outcome": outcome}


def assert_revisable(c, pred):
    gate = one(
        c,
        """SELECT 1 FROM round_prediction_gates g JOIN tracking_rounds r ON r.id=g.round_id
                   WHERE g.prediction_id=? AND g.state='checking' AND r.state!='superseded' LIMIT 1""",
        (pred["id"],),
    )
    if gate:
        raise ValueError("正在核验本轮材料，暂缓概率修订")
    snap = latest(c, pred["event_id"])
    if not snap:
        return
    if current_goal(c, pred["event_id"])["version"] != snap["goal_version"]:
        raise ValueError("目标已经改变，等待新目标的知识版本")
    check = one(
        c,
        "SELECT state FROM prediction_checks WHERE prediction_id=? AND snapshot_id=?",
        (pred["id"], snap["id"]),
    )
    if check and check["state"] != "clear":
        raise ValueError("此题有待核验的决定性材料，暂缓概率修订")


def authority_domains(authority):
    return {
        m.lower()
        for m in re.findall(r"(?<![\w.-])((?:[a-z0-9-]+\.)+[a-z]{2,})(?![\w.-])", authority, re.I | re.ASCII)
    }


def at_authority(url, domains):
    host = (urlsplit(url).hostname or "").lower()
    return any(host == d or host.endswith("." + d) for d in domains)


def affected_predictions(c, event, doc_ids):
    from .materials import words

    predictions = rows(
        c, "SELECT * FROM predictions WHERE event_id=? AND status IN ('open','frozen','review')", (event,)
    )
    evidence = [require(c, "documents", i) for i in doc_ids]
    terms = [
        words(
            p["question"]
            + " "
            + p["rule"]
            + " "
            + " ".join(
                x[0] + " " + x[1]
                for x in c.execute(
                    "SELECT question,condition FROM observations WHERE prediction_id=? AND state='open'",
                    (p["id"],),
                )
            )
        )
        for p in predictions
    ]
    result = []
    for i, p in enumerate(predictions):
        peers = set().union(*(v for j, v in enumerate(terms) if j != i))
        discriminating = terms[i] - peers
        domains = authority_domains(p["authority"])
        for d in evidence:
            shared = words(d["title"] + " " + d["summary"][:800]) & discriminating
            at_source = at_authority(d["url"], domains)
            if (
                p["status"] != "open"
                or shared
                or (len(predictions) == 1 and (not domains or at_source))
                or (not discriminating and at_source)
            ):
                result.append(p["id"])
                break
    return result


def goal_matches(c, r):
    return (
        current_goal(c, r["event_id"])["version"] == r["goal_version"]
        and require(c, "events", r["event_id"])["status"] == "active"
        and require(c, "tracking_rounds", r["id"])["state"] != "superseded"
    )


def list_rounds(c, event, rid=None):
    result = rows(
        c,
        """SELECT id,goal_version,reasons,window_start,window_end,state,detail,created_at,finished_at,job_id,plan
        FROM tracking_rounds WHERE event_id=? AND (? IS NULL OR id=?) ORDER BY id DESC LIMIT 25""",
        (event, rid, rid),
    )
    for r in result:
        r["reasons"] = json.loads(r["reasons"])
        r["plan"] = json.loads(r["plan"]) if r["plan"] else None
        preview = one(
            c,
            "SELECT published_at,excerpts,pending FROM round_evidence_previews WHERE round_id=? AND published_at IS NOT NULL",
            (r["id"],),
        )
        if preview:
            preview["excerpts"] = json.loads(preview["excerpts"])
            preview["pending"] = json.loads(preview["pending"])
        r["evidence_preview"] = preview
        r["tasks"] = rows(
            c,
            """SELECT t.id,t.role,t.question,t.target,t.time_scope,t.done_when,t.priority,t.state,t.detail,
                      l.line_key FROM research_tasks t
               LEFT JOIN investigation_wakes w ON w.task_id=t.id
               LEFT JOIN investigation_lines l ON l.id=w.line_id AND l.event_id=?
               WHERE t.round_id=? ORDER BY t.priority DESC,t.id""",
            (event, r["id"]),
        )
        owners = defaultdict(set)
        for a in (r["plan"] or {}).get("tasks", []):
            key = (a["role"], a["question"])
            owners[key].add(a.get("line") or f"{a['role']}/{a.get('target') or a['question']}")
        for t in r["tasks"]:
            candidates = owners[t["role"], t["question"]]
            if not t["line_key"]:
                t["line_key"] = next(iter(candidates)) if len(candidates) == 1 else ""
            t["role_name"] = ROLE_NAMES[t["role"]]
            t["rejections"] = rows(
                c,
                "SELECT subject,reason FROM task_rejections WHERE task_id=? ORDER BY subject,reason",
                (t["id"],),
            )
        if preview:
            by_id = {t["id"]: t for t in r["tasks"]}
            by_question = defaultdict(list)
            for t in r["tasks"]:
                by_question[t["question"]].append(t)
            for excerpt in preview["excerpts"]:
                task = by_id.get(excerpt.get("task_id"))
                if "task_id" not in excerpt:
                    # Old previews stored only the question. It may belong to two investigators.
                    matches = by_question[excerpt["question"]]
                    task = matches[0] if len(matches) == 1 else None
                excerpt["task_id"] = task["id"] if task else None
                excerpt["line_key"] = task["line_key"] if task else ""
        r["coverage"] = rows(
            c,
            "SELECT url,state,window_start,window_end,checked_at,recovery_round FROM round_sources WHERE round_id=?",
            (r["id"],),
        )
        r["handoffs"] = rows(
            c,
            """SELECT n.id,n.subject AS recipient,n.body,n.reason,i.documents,i.received_run,
                      j.status AS job_status,j.info,w.snapshot_id,destination.round_id AS forwarded_round,
                      stopped.reason AS stopped_reason
               FROM event_notes n JOIN handoff_inbox i ON i.note_id=n.id
               LEFT JOIN investigation_wake_notes wn ON wn.note_id=n.id
               LEFT JOIN investigation_wakes w ON w.job_id=wn.job_id
               LEFT JOIN jobs j ON j.id=w.job_id
               LEFT JOIN event_notes destination ON destination.id=wn.forwarded_to
               LEFT JOIN investigation_wake_stops stopped ON stopped.note_id=n.id
               WHERE n.event_id=? AND n.round_id=? ORDER BY n.id""",
            (event, r["id"]),
        )
        for handoff in r["handoffs"]:
            handoff["documents"] = json.loads(handoff["documents"])
        r["runs"] = rows(
            c,
            """SELECT m.id,m.role,m.model,m.effort,m.status,m.elapsed,m.input_tokens,m.output_tokens FROM runs m
                      JOIN jobs j ON j.id=m.job_id WHERE j.id=? OR (j.event_id=? AND (json_extract(j.args,'$.round_id')=?
                      OR json_extract(j.args,'$.source_round')=?
                      OR json_extract(j.args,'$.snapshot_id') IN (SELECT id FROM knowledge_snapshots WHERE round_id=?)))
                      ORDER BY m.id""",
            (r["job_id"], event, r["id"], r["id"], r["id"]),
        )
    return result


def snapshot_claims(c, snapshot_id):
    result = rows(
        c,
        """SELECT k.*,d.facets,d.aliases,v.entry_id AS world_id,v.version AS world_version,w.category,
                      sc.index_section,sc.index_reason FROM claims k JOIN snapshot_claims sc ON sc.claim_id=k.id
                      JOIN world_versions v ON v.claim_id=k.id JOIN world_entries w ON w.id=v.entry_id
                      LEFT JOIN claim_details d ON d.claim_id=k.id
                      WHERE sc.snapshot_id=? ORDER BY k.id DESC""",
        (snapshot_id,),
    )
    for item in result:
        item["section"] = item["index_section"] or item["section"]
        item["refs"] = json.loads(item["refs"])
        item["citations"] = json.loads(item["citations"])
        item["facets"] = json.loads(item["facets"] or "{}")
        item["aliases"] = json.loads(item["aliases"] or "[]")
    return result


def knowledge(c, event, *, section=None, revision=None, compare=None, query=""):
    e = require(c, "events", event)
    goal = current_goal(c, event)
    if section and section not in SECTIONS:
        raise ValueError("未知知识章节")

    def owned(ident):
        obj = require(c, "knowledge_snapshots", ident)
        if obj["event_id"] != event:
            raise KeyError()
        return obj

    snap = owned(revision) if revision else latest(c, event)
    base = owned(compare) if compare else None
    items = snapshot_claims(c, snap["id"]) if snap else []
    if not revision:
        hidden = {row[0] for row in c.execute("SELECT entry_id FROM world_dispositions")}
        items = [item for item in items if item["world_id"] not in hidden]
    inherited = snapshot_claims(c, base["id"]) if base else []
    ids = {v["id"] for v in items}
    old = {v["id"] for v in inherited}
    index = {key: [] for key in SECTIONS}
    query = query.strip().casefold()
    as_of = snap["created_at"] if revision and snap else utc()
    backlinks = {}
    for ref in rows(
        c,
        """SELECT DISTINCT p.id,p.question,j.value AS doc FROM predictions p JOIN revisions r ON r.prediction_id=p.id
                       JOIN json_each(r.refs) j WHERE p.event_id=? AND r.created_at<=?""",
        (event, as_of),
    ):
        backlinks.setdefault(ref["doc"], {})[ref["id"]] = ref["question"]
    for item in items:
        aliases = (
            item["aliases"]
            if revision
            else [
                v[0]
                for v in c.execute(
                    "SELECT alias FROM world_aliases WHERE demo=? AND entity_id=?",
                    (e["demo"], item["entity_id"]),
                )
            ]
        )
        item["aliases"] = aliases
        if (
            query
            and query
            not in " ".join(
                [item["text"], item["subject"], *aliases, *[f["quote"] for f in item["citations"]]]
            ).casefold()
        ):
            continue
        index[item["section"]].append(item)
        index["index"].append(
            {
                "id": item["id"],
                "subject": item["subject"],
                "section": item["section"],
                "text": item["text"],
                "world_id": item["world_id"],
                "world_version": item["world_version"],
                "category": item["category"],
                "index_reason": item["index_reason"],
                "refs": item["refs"],
                "aliases": aliases,
                "created_at": item["created_at"],
                "predictions": [
                    {"id": i, "question": label}
                    for i, label in {
                        i: label for d in item["refs"] for i, label in backlinks.get(d, {}).items()
                    }.items()
                ],
            }
        )
    upper = (
        snap["node_upper"]
        if snap and revision
        else (one(c, "SELECT max(id) AS n FROM nodes WHERE event_id=?", (event,))["n"] or 0)
    )
    for n in rows(
        c,
        "SELECT id,kind,title,body,created_at,occurred_at,prediction_id,refs FROM nodes WHERE event_id=? AND id<=? ORDER BY id DESC LIMIT 180",
        (event, upper),
    ):
        n["refs"] = json.loads(n["refs"])
        if not query or query in (n["title"] + n["body"]).casefold():
            index["timeline"].append(n)
    result = {
        "event_id": event,
        "goal": goal,
        "event_status": e["status"],
        "snapshot": snap["id"] if snap else None,
        "created_at": snap["created_at"] if snap else None,
        "summary": snap["summary"] if snap else "",
        "gaps": json.loads(snap["gaps"]) if snap else ["等待建立事件知识索引"],
        "changes": json.loads(snap["changes"]) if snap else [],
        "diff": {"added": sorted(ids - old), "removed": sorted(old - ids)} if base else None,
        "sections": {section: index[section]} if section else index,
        "section_names": SECTIONS,
        "versions": rows(
            c,
            "SELECT id,round_id,goal_version,created_at,summary FROM knowledge_snapshots WHERE event_id=? ORDER BY id DESC",
            (event,),
        ),
    }
    frozen = (
        one(c, "SELECT payload FROM snapshot_contexts WHERE snapshot_id=?", (revision,)) if revision else None
    )
    result.update(json.loads(frozen["payload"]) if frozen else metadata(c, event))
    from . import delivery

    report_version = delivery.latest_research_snapshot(c, snap["id"]) if snap else None
    result["delivery_snapshot"] = report_version
    result["delivery"] = delivery.view(c, report_version) if report_version else []
    result["rejections"] = (
        rows(
            c,
            "SELECT task_id,subject,reason FROM snapshot_rejections WHERE snapshot_id=? ORDER BY task_id,subject",
            (report_version,),
        )
        if report_version
        else []
    )
    return result


def metadata(c, event):
    from . import board

    goal = current_goal(c, event)
    return {
        "goal": goal,
        "board": board.view(c, event, goal["version"]),
        "event_status": require(c, "events", event)["status"],
        "lessons": applicable_lessons(c, event),
        "sources": rows(
            c,
            """SELECT ep.*,p.kind,p.name,p.success_at,p.error FROM event_platforms ep JOIN platforms p ON p.url=ep.url
                          WHERE ep.event_id=? ORDER BY ep.watching DESC,p.name""",
            (event,),
        ),
        "attempts": rows(
            c,
            "SELECT url,state,reason,created_at FROM discovery_log WHERE event_id=? ORDER BY id DESC LIMIT 15",
            (event,),
        ),
        "observations": rows(
            c,
            "SELECT * FROM observations WHERE event_id=? ORDER BY state,priority DESC,id DESC LIMIT 60",
            (event,),
        ),
        "closure": one(
            c,
            "SELECT * FROM end_proposals WHERE event_id=? AND status='pending' ORDER BY id DESC LIMIT 1",
            (event,),
        ),
    }


def freeze_metadata(c, event, snapshot, documents=()):
    world.freeze_index(c, event, snapshot)
    snap = require(c, "knowledge_snapshots", snapshot)
    if snap["parent_id"]:
        c.execute(
            "INSERT OR IGNORE INTO snapshot_documents SELECT ?,document_id FROM snapshot_documents WHERE snapshot_id=?",
            (snapshot, snap["parent_id"]),
        )
    c.executemany("INSERT OR IGNORE INTO snapshot_documents VALUES(?,?)", [(snapshot, i) for i in documents])
    c.execute(
        """INSERT OR IGNORE INTO snapshot_documents
                 SELECT ?,j.value FROM snapshot_claims sc JOIN claims k ON k.id=sc.claim_id JOIN json_each(k.refs) j
                 WHERE sc.snapshot_id=?""",
        (snapshot, snapshot),
    )
    c.execute("INSERT INTO snapshot_contexts VALUES(?,?)", (snapshot, dump(metadata(c, event))))


def documents_at(c, snapshot, limit=22):
    return rows(
        c,
        """SELECT d.* FROM documents d WHERE d.id IN
                (SELECT document_id FROM snapshot_documents WHERE snapshot_id=?
                 UNION SELECT j.value FROM snapshot_claims s JOIN claims k ON k.id=s.claim_id JOIN json_each(k.refs) j
                       WHERE s.snapshot_id=?)
                ORDER BY d.id DESC LIMIT ?""",
        (snapshot, snapshot, limit),
    )


def correct_claim(c, event, expected, claim_id, text, reason, fragment_id, quote):
    from .contracts import Citation, Finding
    from .research import claim

    if require(c, "events", event)["demo"]:
        raise ValueError("演示事件只供阅读")
    previous = latest(c, event)
    if not previous or previous["id"] != expected:
        raise VersionConflict("档案已经更新，请查看最新版后重新订正")
    original = one(
        c,
        """SELECT k.* FROM claims k JOIN snapshot_claims s ON k.id=s.claim_id
                   WHERE s.snapshot_id=? AND k.id=?""",
        (expected, claim_id),
    )
    fragment = one(
        c,
        """SELECT f.*,d.title,a.document_id FROM fragments f JOIN evidence_assets a ON a.id=f.asset_id
                  JOIN documents d ON d.id=a.document_id JOIN event_docs ed ON ed.document_id=d.id
                  WHERE ed.event_id=? AND f.id=?""",
        (event, fragment_id),
    )
    if not original or not fragment:
        raise KeyError("当前档案中找不到所选声明或证据")
    text, reason, quote = text.strip(), reason.strip(), quote.strip()
    if not reason or not text or text == original["text"] or len(quote) < 2 or quote not in fragment["text"]:
        raise ValueError("请填写修改后的内容、订正理由，并从本事件的证据片段中逐字引用")
    finding = Finding(
        section=original["section"],
        kind="correction",
        subject=original["subject"],
        text=text,
        reason=reason,
        replaces=claim_id,
        citations=[Citation(fragment_id=fragment_id, quote=quote)],
    )
    ident, _ = claim(c, event, "user", finding, {fragment_id: fragment})
    node(
        c,
        event,
        "correction",
        "用户订正 · " + original["subject"],
        text + "\n" + reason,
        refs=[fragment["document_id"]],
    )
    upper = c.execute("SELECT max(id) FROM nodes WHERE event_id=?", (event,)).fetchone()[0]
    change = {
        "id": ident,
        "replaces": claim_id,
        "section": original["section"],
        "text": text,
        "reason": reason,
        "kind": "correction",
        "author": "本机用户",
    }
    snap = c.execute(
        """INSERT INTO knowledge_snapshots(event_id,parent_id,goal_version,created_at,gaps,summary,changes,node_upper)
                   VALUES(?,?,?,?,?,?,?,?)""",
        (
            event,
            expected,
            current_goal(c, event)["version"],
            utc(),
            previous["gaps"],
            "用户订正 · " + text,
            dump([change]),
            upper,
        ),
    ).lastrowid
    c.execute(
        "INSERT INTO snapshot_claims(snapshot_id,claim_id) SELECT ?,claim_id FROM snapshot_claims WHERE snapshot_id=? AND claim_id!=?",
        (snap, expected, claim_id),
    )
    c.execute("INSERT INTO snapshot_claims(snapshot_id,claim_id) VALUES(?,?)", (snap, ident))
    freeze_metadata(c, event, snap)
    refresh_shared_indexes(c, event, [ident])
    rid = None
    if require(c, "events", event)["status"] == "active":
        c.execute(
            """UPDATE tracking_rounds SET state='superseded',detail='用户已订正档案'
                  WHERE event_id=? AND state IN ('queued','planning','researching','deferred')""",
            (event,),
        )
        rid, _ = request_round(c, event, "用户订正", key=f"correction/{ident}")
    return {"snapshot": snap, "claim": ident, "round": rid}


def change_index(c, event, previous, changes, *, explicit=None, removed=False):
    refs = sorted({d for change in changes for d in json.loads(require(c, "claims", change["id"])["refs"])})
    correction = any(v["kind"] == "correction" for v in changes)
    title = (
        "从事件索引移出引用" if removed else "引用世界知识" if explicit else "引用的世界知识已订正" if correction else "引用的世界知识有新证据"
    )
    detail = "\n".join(change["text"] for change in changes)[:1500]
    upper = node(c, event, "correction" if correction else "historical", title, detail, refs=refs)
    summary = previous["summary"] if previous else title
    if correction:
        summary = "共享依据已订正，事件判断待复核。"
        if require(c, "events", event)["status"] == "active":
            observe(c, event, "共享知识订正", "重新核对此次共享订正对本事件判断的影响", detail[:500], refs=refs, due=utc())
    for change in changes:
        for cite in json.loads(require(c, "claims", change["id"])["citations"]):
            c.execute("INSERT OR IGNORE INTO node_fragments VALUES(?,?)", (upper, cite["fragment_id"]))
    sid = c.execute(
        """INSERT INTO knowledge_snapshots(event_id,parent_id,goal_version,created_at,gaps,summary,changes,node_upper)
                       VALUES(?,?,?,?,?,?,?,?)""",
        (
            event,
            previous["id"] if previous else None,
            current_goal(c, event)["version"],
            utc(),
            previous["gaps"] if previous else "[]",
            summary,
            dump(changes),
            upper,
        ),
    ).lastrowid
    if previous:
        c.execute(
            """INSERT INTO snapshot_claims(snapshot_id,claim_id,index_section,index_reason)
                     SELECT ?,claim_id,index_section,index_reason FROM snapshot_claims WHERE snapshot_id=?""",
            (sid, previous["id"]),
        )
    if explicit:
        c.execute(
            """INSERT INTO snapshot_claims(snapshot_id,claim_id,index_section,index_reason) VALUES(?,?,?,?)
                     ON CONFLICT(snapshot_id,claim_id) DO UPDATE SET index_section=excluded.index_section,index_reason=excluded.index_reason""",
            (sid, explicit["id"], explicit["section"], explicit["reason"]),
        )
    freeze_metadata(c, event, sid)
    waiting = rows(
        c,
        """SELECT * FROM tracking_rounds WHERE event_id=? AND state IN ('queued','planning','researching','deferred')
                      AND context IS NOT NULL""",
        (event,),
    )
    c.execute(
        """UPDATE tracking_rounds SET parent_snapshot=? WHERE event_id=? AND context IS NULL AND
                 state IN ('queued','planning','researching','deferred')""",
        (sid, event),
    )
    for active in waiting:
        c.execute(
            "UPDATE tracking_rounds SET state='superseded',detail='引用的世界知识版本已变更' WHERE id=?", (active["id"],)
        )
    if require(c, "events", event)["status"] == "active":
        for active in waiting:
            request_round(
                c,
                event,
                "共享知识更新",
                start=active["window_start"],
                end=active["window_end"],
                key=f"world/{sid}/{active['id']}",
            )
    return sid


def refresh_shared_indexes(c, origin, changed):
    if not changed:
        return
    changes = {}
    for r in rows(
        c,
        """SELECT DISTINCT n.event_id,s.claim_id,w.id AS entry_id,w.head_id,k.section,k.text,prior.text AS old_text
                       FROM world_entries w JOIN world_versions v ON v.entry_id=w.id
                       JOIN snapshot_claims s ON s.claim_id=v.claim_id JOIN knowledge_snapshots n ON n.id=s.snapshot_id
                       JOIN claims prior ON prior.id=s.claim_id
                       JOIN claims k ON k.id=w.head_id JOIN events e ON e.id=n.event_id
                       WHERE n.event_id!=? AND e.status!='deleted' AND v.canonical_id!=w.head_id
                       AND w.head_id IN (SELECT value FROM json_each(?))
                       AND n.id=(SELECT max(id) FROM knowledge_snapshots WHERE event_id=n.event_id)""",
        (origin, dump(sorted(changed))),
    ):
        changes.setdefault(r["event_id"], []).append(
            {
                "id": r["head_id"],
                "replaces": r["claim_id"],
                "world_id": r["entry_id"],
                "section": r["section"],
                "text": r["text"],
                "kind": "support" if r["old_text"] == r["text"] else "correction",
            }
        )
    for event, updates in changes.items():
        change_index(c, event, latest(c, event), updates)


def reference_world(c, event, entry, version_id, expected, reason, section=""):
    e = require(c, "events", event)
    if e["demo"]:
        raise ValueError("演示事件只供阅读")
    v = world.version(c, version_id)
    if one(c, "SELECT 1 FROM world_dispositions WHERE entry_id=?", (entry,)):
        raise ValueError("该版本已转为事件资料、撤回或合并，请核对当前可引用条目")
    if not v or v["entry_id"] != entry or v["demo"] != e["demo"]:
        raise KeyError("找不到可引用的世界知识")
    old = latest(c, event)
    old_id = old["id"] if old else 0
    if old_id != expected or v["head_id"] != version_id:
        raise VersionConflict("知识条目或事件索引已有更新，请重新核对")
    k = require(c, "claims", version_id)
    reason = reason.strip()
    if len(reason) < 2:
        raise ValueError("请说明这条知识与事件的关系")
    if section and section not in ("background", "people", "opinion", "news"):
        raise ValueError("未知事件索引章节")
    ref = one(
        c,
        """SELECT s.index_section,s.index_reason,p.choice FROM snapshot_claims s
                JOIN world_versions v ON s.claim_id=v.claim_id
                LEFT JOIN world_link_choices p ON p.entry_id=v.entry_id AND p.event_id=?
                WHERE s.snapshot_id=? AND v.entry_id=?""",
        (event, old_id, entry),
    )
    section = section or (ref["index_section"] if ref else k["section"])
    if ref and ref["choice"] == "pin" and ref["index_section"] == section and ref["index_reason"] == reason:
        return {"snapshot": old_id, "entry_id": entry}
    c.execute(
        """INSERT INTO world_link_choices VALUES(?,?,'pin',?)
                 ON CONFLICT(event_id,entry_id) DO UPDATE SET choice='pin',reason=excluded.reason""",
        (event, entry, reason),
    )
    change = {
        "id": version_id,
        "world_id": entry,
        "section": section,
        "text": k["text"],
        "kind": "reference",
        "reason": reason,
    }
    return {"snapshot": change_index(c, event, old, [change], explicit=change), "entry_id": entry}


def remove_reference(c, event, entry, expected, reason):
    old = latest(c, event)
    if not old or old["id"] != expected:
        raise VersionConflict("事件索引已有更新，请重新核对")
    if require(c, "events", event)["demo"]:
        raise ValueError("演示事件只供阅读")
    ref = one(
        c,
        """SELECT s.claim_id,k.subject FROM snapshot_claims s JOIN world_versions v ON v.claim_id=s.claim_id
                 JOIN claims k ON k.id=s.claim_id WHERE s.snapshot_id=? AND v.entry_id=?""",
        (expected, entry),
    )
    if not ref:
        raise KeyError("该事件没有引用此知识")
    if len(reason.strip()) < 2:
        raise ValueError("请填写移出理由")
    c.execute(
        """INSERT INTO world_link_choices VALUES(?,?,'exclude',?)
                 ON CONFLICT(event_id,entry_id) DO UPDATE SET choice='exclude',reason=excluded.reason""",
        (event, entry, reason.strip()),
    )
    change = {
        "id": ref["claim_id"],
        "world_id": entry,
        "kind": "unreference",
        "text": ref["subject"],
        "reason": reason.strip(),
    }
    return {"snapshot": change_index(c, event, old, [change], removed=True), "entry_id": entry}


def migrate_existing_knowledge(c):
    """Make pre-workflow evidence browsable without running models or modifying old scores."""
    from .materials import summary_fragment

    for event in rows(
        c,
        """SELECT e.* FROM events e WHERE e.status!='deleted' AND EXISTS(SELECT 1 FROM nodes WHERE event_id=e.id)
                           AND NOT EXISTS(SELECT 1 FROM knowledge_snapshots WHERE event_id=e.id)
                           AND NOT EXISTS(SELECT 1 FROM tracking_rounds WHERE event_id=e.id)""",
    ):
        eid = event["id"]
        docs = rows(
            c,
            "SELECT d.* FROM documents d JOIN event_docs ed ON ed.document_id=d.id WHERE ed.event_id=? ORDER BY d.id",
            (eid,),
        )
        items = []
        for doc in docs:
            asset = summary_fragment(c, doc)
            f = one(c, "SELECT * FROM fragments WHERE asset_id=? ORDER BY id LIMIT 1", (asset,))
            if not f:
                continue
            cites = [
                {
                    "fragment_id": f["id"],
                    "document_id": doc["id"],
                    "quote": f["text"][:350],
                    "locator": f["locator"],
                    "link": f["link"],
                    "title": doc["title"],
                }
            ]
            ident = c.execute(
                """INSERT OR IGNORE INTO claims(event_id,role,section,kind,subject,text,precision,reason,refs,citations,identity,created_at)
                      VALUES(?,'news','news','reported',?,?,'unknown','来自旧版订阅档案，发生时间待核',?,?,?,?)""",
                (
                    eid,
                    doc["title"],
                    (doc["summary"] or doc["title"])[:1500],
                    dump([doc["id"]]),
                    dump(cites),
                    fingerprint("legacy", doc["id"]),
                    doc["observed_at"],
                ),
            ).lastrowid
            items.append(ident)
        state = one(
            c, "SELECT body FROM nodes WHERE event_id=? AND kind='judgment' ORDER BY id DESC LIMIT 1", (eid,)
        )
        upper = c.execute("SELECT max(id) FROM nodes WHERE event_id=?", (eid,)).fetchone()[0]
        snap = c.execute(
            """INSERT INTO knowledge_snapshots(event_id,goal_version,created_at,gaps,summary,changes,node_upper)
                          VALUES(?,1,?,?,?,'[]',?)""",
            (
                eid,
                utc(),
                dump(["旧版资料已保留，恢复研究后补充完整实体和原文档案"]),
                state["body"] if state else event["scope"],
                upper,
            ),
        ).lastrowid
        c.executemany(
            "INSERT INTO snapshot_claims(snapshot_id,claim_id) VALUES(?,?)", [(snap, i) for i in items]
        )
        freeze_metadata(c, eid, snap)
