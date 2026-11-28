"""Event conversations grounded in immutable prediction, evidence and knowledge versions."""
from __future__ import annotations

import json
import re

from . import agent_tools, feeds, focus, people, scenarios, workflow, world_search, workspace_agent
from .db import config, dump, enqueue, one, require, rows, utc


def allow_workspace_turns(c):
    """Older workbenches required an event for every turn. Preserve IDs and their foreign keys."""
    c.execute("BEGIN IMMEDIATE")
    try:
        required = any(r[1] == "event_id" and r[3] for r in c.execute("PRAGMA table_info(discussion_turns)"))
        if required:
            table_sql = c.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name='discussion_turns'"
            ).fetchone()[0]
            indexes = list(
                c.execute(
                    "SELECT sql FROM sqlite_master WHERE tbl_name='discussion_turns' AND type IN ('index','trigger') AND sql IS NOT NULL"
                )
            )
            new_sql = re.sub(
                r"\bevent_id\s+INTEGER\s+NOT\s+NULL\b", "event_id INTEGER", table_sql, count=1, flags=re.I
            )
            if new_sql == table_sql:
                raise ValueError("无法识别原讨论表结构")
            c.execute(new_sql.replace("discussion_turns", "discussion_turns_upgrade", 1))
            c.execute("INSERT INTO discussion_turns_upgrade SELECT * FROM discussion_turns")
            c.execute("DROP TABLE discussion_turns")
            c.execute("ALTER TABLE discussion_turns_upgrade RENAME TO discussion_turns")
            for (statement,) in indexes:
                c.execute(statement)
            if c.execute("PRAGMA foreign_key_check").fetchone():
                raise ValueError("讨论迁移未通过外键核对")
        c.commit()
    except BaseException:
        c.rollback()
        raise


def link_archived_runs(c):
    """Recover only unique, committed forecasts with a matching stored input and decision."""
    c.execute(
        """WITH candidates AS (
        SELECT v.id AS version,m.run_id,
               count(*) OVER (PARTITION BY v.id) AS per_version,
               count(*) OVER (PARTITION BY m.run_id) AS per_run
        FROM model_receipts m JOIN run_inputs i ON i.run_id=m.run_id AND i.input_hash=m.input_hash
        JOIN jobs j ON j.id=m.job_id AND j.kind='forecast'
        JOIN job_results jr ON jr.job_id=j.id AND jr.info='已提交可追溯的概率版本'
        JOIN predictions p ON p.event_id=j.event_id
        JOIN revisions v ON v.prediction_id=p.id AND v.model=m.model
            AND v.created_at BETWEEN j.started_at AND j.finished_at
        LEFT JOIN fixed_questions f ON f.id=json_extract(j.args,'$.fixed_id')
        WHERE m.role='forecast' AND v.p=json_extract(m.response,'$.p')
            AND v.reason=json_extract(m.response,'$.reason')
            AND json(v.refs)=json(json_extract(m.response,'$.evidence_ids'))
            AND ((f.prediction_id=p.id AND f.event_id=p.event_id)
                OR (f.id IS NULL AND json_extract(m.response,'$.action')='revise'
                    AND json_extract(m.response,'$.prediction_id')=p.id)
                OR (f.id IS NULL AND json_extract(m.response,'$.action')='create'
                    AND trim(json_extract(m.response,'$.question'))=p.question
                    AND julianday(json_extract(m.response,'$.deadline'))=julianday(p.deadline)))
        )
        INSERT OR IGNORE INTO revision_runs SELECT version,run_id FROM candidates
        WHERE per_version=1 AND per_run=1"""
    )


def anchor(c, event, revision):
    if event is None:
        if revision:
            raise ValueError("讨论某版预测需要指定其所属事件")
        return None
    require(c, "events", event)
    if not revision:
        return None
    rec = one(
        c,
        """SELECT r.id,r.prediction_id,r.p,r.reason,r.refs,r.created_at,r.model,p.question,p.rule,
        p.authority,p.deadline,x.snapshot_id,x.dependencies,rr.run_id
        FROM revisions r JOIN predictions p ON p.id=r.prediction_id
        LEFT JOIN revision_contexts x ON x.revision_id=r.id AND EXISTS(
           SELECT 1 FROM knowledge_snapshots k WHERE k.id=x.snapshot_id AND k.event_id=p.event_id AND k.created_at<=r.created_at)
        LEFT JOIN revision_runs rr ON rr.revision_id=r.id
        WHERE r.id=? AND p.event_id=?""",
        (revision, event),
    )
    if not rec:
        raise KeyError("该版本不属于本事件")
    rec["refs"] = json.loads(rec["refs"])
    rec["dependencies"] = json.loads(rec["dependencies"] or "[]")
    attach_scenario(c, rec)
    return rec


def attach_scenario(c, rec):
    scale = scenarios.definition(c, rec["prediction_id"])
    if scale:
        rec["scale"] = scale
        rec["outlook"] = scenarios.version(c, rec["id"])
        rec["scenario_label"] = scenarios.summary(scale, rec["outlook"])


def check_scope(revision, basis):
    if basis not in ("original", "latest") or (not revision and basis == "original"):
        raise ValueError("按当时资料讨论需要指定本事件的概率版本")


def add_source(sources, key, period, **fields):
    if key not in sources:
        sources[key] = {**fields, "period": [period]}
    elif period not in sources[key]["period"]:
        sources[key]["period"].append(period)


def doc_owner(c, event, doc):
    row = one(
        c,
        """SELECT ed.event_id FROM event_docs ed JOIN events e ON e.id=ed.event_id
        WHERE ed.document_id=? AND e.status!='deleted' AND e.demo=(SELECT demo FROM events WHERE id=?)
        ORDER BY (ed.event_id=?) DESC,ed.event_id LIMIT 1""",
        (doc, event, event),
    )
    return row["event_id"] if row else None


def add_document(c, sources, event, doc, period):
    ident = doc["id"]
    owner = doc_owner(c, event, ident)
    if owner:
        add_source(
            sources,
            f"D{ident}",
            period,
            title=doc["title"],
            href=f"#/events/{owner}/doc/{ident}",
            text=doc.get("summary", "")[:1000],
            url=doc.get("url"),
            published_at=doc.get("published_at") if doc.get("source_times_checked", True) else None,
        )


def prediction_source(sources, event, rec, period, *, detailed=True):
    key = "P" + str(rec["id"])
    fields = {
        "deadline": rec["deadline"],
        "probability": rec["p"],
        "evidence": [f"D{i}" for i in rec["refs"]],
    }
    scale, outlook = rec.get("scale"), rec.get("outlook")
    if scale and outlook:
        fields["scale"] = (
            scale
            if detailed
            else {
                "window_start": scale["window_start"],
                "levels": [{"label": level["label"]} for level in scale["levels"]],
            }
        )
        fields["outlook"] = (
            outlook if detailed else {k: outlook[k] for k in ("probabilities", "primary_level", "confidence")}
        )
    add_source(
        sources,
        key,
        period,
        title=(rec["scenario_label"] + " · " if rec.get("scenario_label") else "")
        + f'{rec["p"]}% · {rec["question"]}',
        text=rec["reason"],
        href=f'#/events/{event}/p/{rec["prediction_id"]}/{rec["id"]}',
        recorded_at=rec["created_at"],
        **fields,
    )
    return key


def index_context(c, event, snap_id, question, sources, period):
    if not snap_id:
        return None
    snap = require(c, "knowledge_snapshots", snap_id)
    if snap["event_id"] != event:
        raise KeyError()
    words = set(world_search.query_terms(question)[0])

    def rank(subject, text):
        return 4 * len(words & world_search.terms(subject)) + len(words & world_search.terms(text))

    c.create_function("discussion_match", 2, rank, deterministic=True)
    records = rows(
        c,
        """SELECT k.subject,k.text,k.kind,k.citations,v.entry_id,v.canonical_id,v.version,d.path,d.attributes,
                  coalesce(nullif(sc.index_section,''),k.section) AS section,sc.index_reason
           FROM snapshot_claims sc JOIN claims k ON k.id=sc.claim_id JOIN world_versions v ON v.claim_id=k.id
           JOIN world_record_data d ON d.version_id=v.canonical_id WHERE sc.snapshot_id=?
           ORDER BY discussion_match(k.subject,k.text) DESC,k.id DESC LIMIT 12""",
        (snap_id,),
    )
    keys = []
    for rec in records:
        key = f'K{rec["canonical_id"]}'
        keys.append(key)
        add_source(
            sources,
            key,
            period,
            title=f'{rec["subject"]} · v{rec["version"]}',
            text=rec["text"],
            href=f'#/world/{rec["entry_id"]}/{rec["canonical_id"]}',
            kind=rec["kind"],
            path=rec["path"],
            section=rec["section"],
            attributes=json.loads(rec["attributes"]),
            relevance=rec["index_reason"],
        )
        for citation in json.loads(rec["citations"])[:2]:
            frag_id = citation["fragment_id"]
            frag = one(
                c,
                """SELECT f.locator,a.document_id FROM fragments f JOIN evidence_assets a
                              ON a.id=f.asset_id WHERE f.id=?""",
                (frag_id,),
            )
            owner = doc_owner(c, event, frag["document_id"]) if frag else None
            if owner:
                add_source(
                    sources,
                    f"F{frag_id}",
                    period,
                    title=f'证据 #{frag["document_id"]} · {frag["locator"]}',
                    text=citation["quote"],
                    href=f'#/events/{owner}/doc/{frag["document_id"]}/{frag_id}',
                )
    return dict(
        snapshot_id=snap_id,
        parent_snapshot_id=snap["parent_id"],
        created_at=snap["created_at"],
        summary=snap["summary"],
        changes=json.loads(snap["changes"])[:20],
        gaps=json.loads(snap["gaps"]),
        total=c.execute("SELECT count(*) FROM snapshot_claims WHERE snapshot_id=?", (snap_id,)).fetchone()[0],
        selected_keys=keys,
    )


def current_context(c, event, question, sources):
    latest = workflow.latest(c, event)
    current = index_context(c, event, latest["id"], question, sources, "latest") if latest else None
    people_refs = []
    for t in people.roster(c, event):
        if len(people_refs) >= 3:
            break
        if not (people.named(question, people.names_of(t)) or "相关人物" in question):
            continue
        proof = require(c, "documents", t["document_id"])
        add_document(c, sources, event, proof, "latest")
        person = {
            "name": t["name"],
            "identity": t["identity"],
            "role": t["role"],
            "relevance": t["relevance"],
            "identity_source": "D" + str(t["document_id"]),
            "materials": [],
        }
        for row in rows(
            c,
            """SELECT d.*,m.attribution,m.channel,m.period FROM person_materials m JOIN documents d
                        ON d.id=m.document_id WHERE m.target_id=? AND m.attribution!='账号归属待核'
                        ORDER BY m.round_id DESC,d.id DESC LIMIT 6""",
            (t["id"],),
        ):
            add_document(c, sources, event, row, "latest")
            person["materials"].append(
                {
                    "source": f'D{row["id"]}',
                    "attribution": row["attribution"],
                    "channel": row["channel"],
                    "period": row["period"],
                }
            )
        people_refs.append(person)
    read = one(c, "SELECT seen FROM reads WHERE event_id=?", (event,))
    seen = read["seen"] if read else 0
    changes = rows(
        c,
        """SELECT kind,count(*) AS count FROM nodes WHERE event_id=? AND id>?
                           GROUP BY kind ORDER BY kind""",
        (event, seen),
    )
    for n in rows(
        c,
        """SELECT id,title,body,watch,kind,created_at FROM nodes WHERE event_id=?
            AND kind IN ('fact','judgment','correction','historical') ORDER BY id DESC LIMIT 8""",
        (event,),
    ):
        add_source(
            sources,
            f'N{n["id"]}',
            "latest",
            title=n["title"],
            text=n["body"],
            watch=n["watch"],
            kind=n["kind"],
            after_last_read=n["id"] > seen if read else None,
            recorded_at=n["created_at"],
            href=f'#/events/{event}/range/{n["id"]-1}/{n["id"]}',
        )
    ledger = rows(
        c,
        """SELECT r.*,p.question,p.status,p.deadline FROM predictions p JOIN revisions r ON r.id=(
        SELECT max(id) FROM revisions WHERE prediction_id=p.id) WHERE p.event_id=? ORDER BY r.id DESC LIMIT 12""",
        (event,),
    )
    for index, r in enumerate(ledger):
        attach_scenario(c, r)
        r["refs"] = json.loads(r["refs"])
        detailed = index < 3 or r["question"] in question
        prediction_source(sources, event, r, "latest", detailed=detailed)
        if detailed:
            for doc in r["refs"]:
                if "latest" not in sources.get(f"D{doc}", {}).get("period", []):
                    add_document(c, sources, event, require(c, "documents", doc), "latest")
    decisions = rows(
        c,
        """SELECT x.*,p.question FROM resolutions x JOIN predictions p ON p.id=x.prediction_id
        WHERE p.event_id=? ORDER BY x.id DESC LIMIT 5""",
        (event,),
    )
    for x in decisions:
        add_source(
            sources,
            f'R{x["id"]}',
            "latest",
            title=x["question"],
            text=x["reason"],
            outcome=x["outcome"],
            recorded_at=x["created_at"],
            href=f'#/events/{event}/p/{x["prediction_id"]}',
        )
    docs = rows(
        c,
        """SELECT d.* FROM documents d JOIN event_docs e ON e.document_id=d.id
           WHERE e.event_id=? AND e.intake NOT IN ('future','person_candidate') ORDER BY d.id DESC LIMIT 12""",
        (event,),
    )
    for doc in docs:
        add_document(c, sources, event, doc, "latest")
    return {
        "goal": workflow.current_goal(c, event),
        "index": current,
        "reading_progress": {"through_node": seen if read else None, "changes": changes},
        "people": people_refs,
    }


def archived_forecast(c, rec):
    if not rec["run_id"]:
        return None
    stored = one(
        c,
        """SELECT i.payload,m.response,r.model,r.effort FROM run_inputs i JOIN runs r ON r.id=i.run_id
            JOIN model_receipts m ON m.run_id=i.run_id AND m.input_hash=i.input_hash AND m.role='forecast'
            WHERE i.run_id=?""",
        (rec["run_id"],),
    )
    if stored:
        return {
            "id": rec["run_id"],
            "model": stored["model"],
            "effort": stored["effort"],
            "input": json.loads(stored["payload"]),
            "output": json.loads(stored["response"]),
        }
    return None


def workspace_history(c, limit=6):
    return rows(
        c,
        """SELECT t.event_id,e.title AS event,t.question,t.revision_id,t.basis,substr(t.answer,1,1200) AS answer
        FROM discussion_turns t LEFT JOIN events e ON e.id=t.event_id
        WHERE t.answer IS NOT NULL
        AND (t.event_id IS NULL OR (e.demo=0 AND e.status!='deleted')) ORDER BY t.id DESC LIMIT ?""",
        (limit,),
    )


def workspace_evidence(c, ctx, lookup, events=None):
    sources = ctx["sources"]
    for index, event in enumerate(ctx["workspace"]["events"][:12] if events is None else events):
        eid = event["id"]
        snap = workflow.latest(c, eid)
        read = one(c, "SELECT seen FROM reads WHERE event_id=?", (eid,))
        changes = rows(
            c,
            """SELECT kind,count(*) AS count FROM nodes WHERE event_id=? AND id>? GROUP BY kind""",
            (eid, read["seen"] if read else 0),
        )
        add_source(
            sources,
            f"E{eid}",
            "latest",
            title=event["title"],
            text=event["scope"],
            status=event["status"],
            cadence_hours=event["cadence"],
            next_run=event["next_run"],
            running=event["running"],
            queued=event["queued"],
            reading_progress={"through_node": read["seen"] if read else None, "changes": changes},
            summary=snap["summary"][:480] if snap else "",
            href=f"#/events/{eid}/research",
        )
        if index < 4:
            for n in rows(
                c,
                """SELECT n.id,n.title,n.body,n.kind,n.refs,n.id>r.seen AS unread FROM nodes n
                LEFT JOIN reads r ON r.event_id=n.event_id WHERE n.event_id=?
                AND n.kind IN ('fact','judgment','correction') ORDER BY n.id DESC LIMIT 3""",
                (eid,),
            ):
                add_source(
                    sources,
                    f'N{n["id"]}',
                    "latest",
                    title=n["title"],
                    text=n["body"][:650],
                    kind=n["kind"],
                    event=event["title"],
                    after_last_read=None if n["unread"] is None else bool(n["unread"]),
                    href=f'#/events/{eid}/range/{n["id"]-1}/{n["id"]}',
                )
                for doc in json.loads(n["refs"])[:1]:
                    add_document(c, sources, eid, require(c, "documents", doc), "latest")
    for hit in world_search.search(c, lookup, limit=6, demo=0)[0]:
        add_source(
            sources,
            f'K{hit["version_id"]}',
            "latest",
            title=hit["subject"],
            text=hit["text"],
            path=hit["path"],
            href=f'#/world/{hit["entry_id"]}/{hit["version_id"]}',
        )


def build_context(c, event, rec, basis, question):
    revision = rec["id"] if rec else None
    sources = {}
    tracked = require(c, "events", event) if event is not None else None
    history = (
        workspace_history(c)
        if event is None
        else rows(
            c,
            """SELECT id,question,answer FROM discussion_turns WHERE event_id=? AND revision_id IS ?
                       AND basis=? AND answer IS NOT NULL ORDER BY id DESC LIMIT 6""",
            (event, revision, basis),
        )
    )
    ctx = {
        "event_id": event,
        "demo": bool(tracked and tracked["demo"]),
        "can_add_focus": bool(tracked and not tracked["demo"] and tracked["status"] == "active"),
        "question": question,
        "basis": basis,
        "asked_at": utc(),
        "sources": sources,
        "history": [
            {"event": h.get("event"), "question": h["question"], "answer": h["answer"][:2000]}
            for h in reversed(history)
        ],
    }
    lookup = " ".join([question, *[h["question"] for h in history[:2]]])
    if not ctx["demo"] and (basis == "latest" or workspace_agent.CONTROL.search(question)):
        ctx["workspace"] = workspace_agent.workspace(c, event, question, revision, basis)
        if event is not None and basis == "latest":
            ctx["workspace_history"] = list(reversed(workspace_history(c, 4)))
    if event is None:
        workspace_evidence(c, ctx, lookup)
    workspace = ctx.get("workspace", {})
    if workspace and basis == "latest":
        named = workspace["mentioned_events"]
        if (
            not named
            and event is None
            and workspace["current_event"]
            and not re.search(r"所有|全部|各个|各事件|哪些事件|整个工作台", question)
        ):
            named = [workspace["current_event"]]
        others = [e for e in workspace["events"] if e["id"] in named and e["id"] != event]
        if others:
            if event is not None:
                workspace_evidence(c, ctx, lookup, others[:6])
            ctx["event_contexts"] = [
                {
                    "event_id": e["id"],
                    "latest": current_context(c, e["id"], lookup, sources),
                    "collection_scope": focus.listing(c, e["id"]),
                }
                for e in others[:2]
            ]
    if event is None:
        return ctx, None
    snapshot = None
    if rec:
        snapshot = rec["snapshot_id"]
        ctx["registered_forecast"] = rec
        prediction_source(sources, event, rec, "original")
        stored = archived_forecast(c, rec)
        if stored:
            ctx["forecast_run"] = stored
            for doc in stored["input"].get("evidence", []):
                add_document(c, sources, event, doc, "original")
        for ident in rec["refs"]:
            if f"D{ident}" not in sources:
                add_document(c, sources, event, require(c, "documents", ident), "original")
        # Read the goal version known then, even if the event has since been renamed or paused.
        goal = one(
            c,
            """SELECT g.* FROM event_goals g WHERE g.event_id=? AND
               (CASE WHEN ? IS NULL THEN g.created_at<=? ELSE g.version=(
               SELECT goal_version FROM knowledge_snapshots WHERE id=?) END) ORDER BY g.version DESC LIMIT 1""",
            (event, snapshot, rec["created_at"], snapshot),
        )
        ctx["original"] = {
            "goal": goal,
            "index": index_context(c, event, snapshot, lookup, sources, "original"),
        }
    if basis == "latest":
        ctx["latest"] = current_context(c, event, lookup, sources)
        ctx["latest"]["collection_scope"] = focus.listing(c, event)
        snapshot = (ctx["latest"]["index"] or {}).get("snapshot_id")
    return ctx, snapshot


def submit(c, event, revision, basis, provider, question, nonce, timezone=None, utc_offset_minutes=0):
    rec = anchor(c, event, revision)
    check_scope(revision, basis)
    question = question.strip()
    if not question or len(question) > 2400:
        raise ValueError("请填写 1 至 2400 字的问题")
    if provider not in ("auto", "codex", "dsh"):
        raise ValueError("未知的讨论后端")
    prior = one(c, "SELECT * FROM discussion_turns WHERE event_id IS ? AND nonce=?", (event, nonce))
    if prior:
        if (prior["revision_id"], prior["basis"], prior["question"]) != (revision, basis, question) or (
            provider != "auto" and prior["provider"] != provider
        ):
            raise ValueError("该提交编号已用于另一个问题")
        return {"turn_id": prior["id"], "job_id": prior["job_id"]}
    if provider == "auto":
        provider = workflow.main_session_backend(c, event) or config(c)["agent_backend"]
    if one(
        c,
        """SELECT 1 FROM discussion_turns t JOIN jobs j ON j.id=t.job_id WHERE t.event_id IS ?
        AND t.revision_id IS ? AND t.basis=? AND t.answer IS NULL AND j.status IN ('queued','running','deferred')""",
        (event, revision, basis),
    ):
        raise ValueError("上一条提问仍在处理中，收到回答后可以继续追问")
    ctx, snap = build_context(c, event, rec, basis, question)
    c.execute(
        """INSERT INTO agent_sessions(lane,backend) VALUES(?,?)
           ON CONFLICT(lane) DO UPDATE SET backend=excluded.backend""",
        (workflow.main_lane(event), provider),
    )
    ctx["timezone"] = timezone
    ctx["utc_offset_minutes"] = utc_offset_minutes
    ident = c.execute(
        """INSERT INTO discussion_turns(event_id,revision_id,basis,nonce,provider,question,context,snapshot_id,created_at)
        VALUES(?,?,?,?,?,?,?,?,?)""",
        (event, revision, basis, nonce, provider, question, dump(ctx), snap, ctx["asked_at"]),
    ).lastrowid
    job = enqueue(c, "discussion", event=event, args={"turn_id": ident})
    c.execute("UPDATE discussion_turns SET job_id=? WHERE id=?", (job, ident))
    return {"turn_id": ident, "job_id": job}


def conversation(c, event, revision, basis, before=0):
    rec = anchor(c, event, revision)
    check_scope(revision, basis)
    turns = rows(
        c,
        """SELECT t.id,t.event_id,e.title AS event_title,t.revision_id,t.basis,t.provider,t.question,t.created_at,t.answered_at,t.snapshot_id,t.job_id,t.run_id,
            t.answer,t.citations,j.status,j.info,r.model,r.effort FROM discussion_turns t
            JOIN jobs j ON j.id=t.job_id LEFT JOIN runs r ON r.id=t.run_id LEFT JOIN events e ON e.id=t.event_id
            WHERE ((t.event_id=? AND t.revision_id IS ? AND t.basis=?)
            OR (? IS NULL AND (t.event_id IS NULL OR (e.demo=0 AND e.status!='deleted'))))
            AND (?=0 OR t.id<?)
            ORDER BY t.id DESC LIMIT 21""",
        (event, revision, basis, event, before, before),
    )
    page = turns[:20]
    by_turn = {t["id"]: [] for t in page}
    operations = {t["id"]: [] for t in page}
    if page:
        receipts = rows(
            c,
            """SELECT a.turn_id,a.focus_id,a.added,a.round_id,r.state,r.event_id FROM discussion_focus a
            JOIN tracking_rounds r ON r.id=a.round_id JOIN events e ON e.id=r.event_id
            WHERE e.status!='deleted' AND a.turn_id IN (SELECT value FROM json_each(?))
            ORDER BY a.turn_id,a.focus_id""",
            (dump(list(by_turn)),),
        )
        if receipts:
            scopes = {s["id"]: s for eid in {a["event_id"] for a in receipts} for s in focus.listing(c, eid)}
            for a in receipts:
                by_turn[a["turn_id"]].append({**scopes[a["focus_id"]], **a})
        for op in rows(
            c,
            """SELECT a.turn_id,a.position,a.action,a.event_id,e.title AS event_title,a.summary,a.round_id,
            a.job_id,j.status AS job_status,r.state AS round_state,a.before_state,a.after_state FROM workflow_actions a JOIN events e ON e.id=a.event_id
            LEFT JOIN jobs j ON j.id=a.job_id
            LEFT JOIN tracking_rounds r ON r.id=a.round_id
            WHERE e.status!='deleted' AND a.turn_id IN (SELECT value FROM json_each(?))
            ORDER BY a.turn_id,a.position""",
            (dump(list(by_turn)),),
        ):
            for k in ("before_state", "after_state"):
                op[k] = json.loads(op[k])
            operations[op["turn_id"]].append(op)
    for t in page:
        t["citations"] = json.loads(t["citations"] or "[]")
        t["focus_actions"] = by_turn[t["id"]]
        t["workflow_actions"] = operations[t["id"]]
        if t["answer"] is not None:
            t["status"] = "done"
    return {
        "anchor": rec,
        "basis": basis,
        "default_provider": config(c)["agent_backend"],
        "session_provider": workflow.main_session_backend(c, event),
        "turns": list(reversed(page)),
        "has_older": len(turns) > 20,
    }


def job_turn(c, event, job, turn):
    row = one(
        c, "SELECT * FROM discussion_turns WHERE id=? AND event_id IS ? AND job_id=?", (turn, event, job)
    )
    if not row:
        raise ValueError("找不到此任务绑定的讨论提问")
    return row


def retry(c, job):
    turn = job_turn(c, job["event_id"], job["id"], json.loads(job["args"])["turn_id"])
    if turn["answer"] is not None:
        raise ValueError("该问题已经回答")
    if one(
        c,
        """SELECT 1 FROM discussion_turns WHERE event_id IS ? AND revision_id IS ? AND basis=? AND id>?""",
        (turn["event_id"], turn["revision_id"], turn["basis"], turn["id"]),
    ):
        raise ValueError("已有后续提问，可在当前讨论中重新提出这个问题")
    c.execute(
        "UPDATE jobs SET status='queued',retry_at=NULL,finished_at=NULL,attempts=0,info='' WHERE id=?",
        (job["id"],),
    )
    return job["id"]


def retrieve_web_sources(acquisition, ctx, urls):
    """Freeze retrieved publisher text with the turn, including global chats without an event."""
    result = {**ctx, "sources": dict(ctx["sources"]), "web_lookup": []}
    for url in dict.fromkeys(urls):
        found = acquisition.targeted(feeds.public_url(url))
        if not found:
            result["web_lookup"].append({"url": url, "result": "未取回可引用的原文"})
            continue
        entry, response, extracted = found[0]
        key = f'W{len(result["web_lookup"]) + 1}'
        add_source(
            result["sources"], key, "latest", title=entry["title"] or response.url,
            href=response.url, url=response.url, published_at=extracted.published_at, retrieved_at=utc(),
            text="\n".join(p[1] for p in extracted.parts)[:12000],
        )
        result["web_lookup"].append({"url": url, "key": key})
    return result


def validate_answer(answer, ctx):
    if answer.kind == "lookup" or answer.source_urls:
        if (
            answer.kind != "lookup" or not answer.source_urls or answer.actions or answer.add_focus
            or answer.citations or ctx.get("basis") != "latest" or "web_lookup" in ctx
            or not agent_tools.live_web("discuss_codex", ctx)
        ):
            raise ValueError("本轮仅能在最新资料对话中提交一次公开原文取证请求")
        for url in answer.source_urls:
            feeds.public_url(url)
        return
    if answer.kind == "workflow" or answer.actions:
        if (
            answer.kind != "workflow"
            or not answer.actions
            or answer.add_focus
            or answer.citations
            or not ctx.get("workspace")
        ):
            raise ValueError("工作流指令需要明确操作，且来自可调度的工作区。")
        return
    if answer.kind == "tracking" or answer.add_focus:
        if answer.kind != "tracking" or not answer.add_focus or answer.citations:
            raise ValueError("跟踪指令须给出本事件的收集对象")
        writable = {**ctx, "can_add_focus": True} if ctx.get("workspace") else ctx
        focus.check_request(writable, answer.add_focus)
        return
    allowed = ctx["sources"]
    cited = set(answer.citations)
    markers = set(re.findall(r"\[([A-Z]+\d+)\]", answer.answer))
    if (
        not answer.answer.strip()
        or cited - allowed.keys()
        or markers - cited
        or (answer.kind == "analysis" and not cited)
    ):
        raise ValueError("讨论回答的依据无法在本轮资料中核对，可重试")


def save_answer(c, turn, run, answer, ctx):
    validate_answer(answer, ctx)
    if answer.kind == "lookup":
        raise ValueError("请先取得原文再交付讨论答复")
    if answer.kind == "workflow":
        body = workspace_agent.execute(c, turn, ctx, answer.actions)
    elif answer.kind == "tracking":
        if ctx.get("workspace"):
            actions = [
                workspace_agent.ScopeChange(action="add_focus", **a.model_dump()) for a in answer.add_focus
            ]
            body = workspace_agent.execute(c, turn, ctx, actions)
        else:
            body = focus.add(c, turn, answer.add_focus)
    else:
        body = answer.answer.strip()
    links = [
        {"key": k, **{f: ctx["sources"][k][f] for f in ("title", "href", "period")}}
        for k in dict.fromkeys(answer.citations)
    ]
    c.execute(
        "UPDATE discussion_turns SET answer=?,citations=?,answered_at=?,run_id=? WHERE id=?",
        (body, dump(links), utc(), run, turn),
    )
