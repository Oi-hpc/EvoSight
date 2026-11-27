"""Event-owned research, executable reading guides and publication decisions."""
import json
import re

from . import world_catalog, world_search
from .db import dump, enqueue, one, require, rows, utc


# Detect assertions about the investigation itself. A publisher reporting a negative result
# is still a world fact. Scope the match to the subject or first clause of the assertion.
PROCESS = re.compile(
    r"^(?:本轮.{0,65}(?:核查|核对|检索|未取得|无新增)|本次(?:核查|检索|调查|所见)|"
    r"(?:本轮|当前|可见|所给)(?:材料|片段|输入|证据)(?:范围|覆盖|不足|缺少|未见|仅(?:有|见|能)|只(?:有|能))|"
    r"已查(?:材料|范围)|适用范围(?:为|仅为)本轮)|"
    r"(?:尚未取得|缺少|仍无|未取得|未见|不构成|不足以).{0,20}(?:可核验|新依据|原文|证据|定价基准)"
)


def local_reason(kind, subject, body, facets):
    if kind in ("hypothesis", "conflict") or any(k in facets for k in ("预测ID", "关联事件ID", "原判断")):
        return "本事件的解释、分歧或预测依赖"
    if PROCESS.search(subject) or PROCESS.match(body.lstrip()):
        return "本次调查的材料范围或核查进度"
    return ""


def note(
    c,
    event,
    goal,
    question,
    subject,
    body,
    kind,
    reason,
    citations,
    *,
    rid=None,
    task=None,
    at=None,
    documents=(),
):
    from .workflow import fingerprint

    parts = [goal, question, subject, body, citations]
    if kind == "handoff":
        parts.extend([rid, task, list(documents)])
    key = fingerprint(*parts)
    c.execute(
        """INSERT OR IGNORE INTO event_notes(event_id,goal_version,round_id,task_id,question,subject,body,
           kind,reason,citations,recorded_at,note_key) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
        (event, goal, rid, task, question, subject, body, kind, reason, dump(citations), at or utc(), key),
    )
    if kind == "handoff":
        return one(c, "SELECT id FROM event_notes WHERE event_id=? AND note_key=?", (event, key))["id"]


def inbox(c, event, recipient):
    messages = rows(
        c,
        """SELECT n.id,n.question,n.body,i.documents FROM handoff_inbox i JOIN event_notes n ON n.id=i.note_id
                        WHERE n.event_id=? AND n.subject=? AND i.received_run IS NULL AND n.goal_version=(
                            SELECT max(version) FROM event_goals WHERE event_id=n.event_id) ORDER BY n.id LIMIT 12""",
        (event, recipient),
    )
    for message in messages:
        message["documents"] = json.loads(message["documents"])
    return messages


def migrate(c):
    if one(c, "SELECT 1 FROM settings WHERE key='board_migration_v1'"):
        return
    for item in rows(
        c,
        """SELECT w.id entry_id,k.*,d.facets FROM world_entries w JOIN claims k ON k.id=w.head_id
                           LEFT JOIN claim_details d ON d.claim_id=k.id""",
    ):
        reason = local_reason(item["kind"], item["subject"], item["text"], json.loads(item["facets"] or "{}"))
        if not reason:
            continue
        snapshot = (
            one(
                c,
                """SELECT s.round_id,s.goal_version FROM snapshot_claims sc JOIN knowledge_snapshots s
                             ON s.id=sc.snapshot_id WHERE sc.claim_id=? AND s.event_id=? ORDER BY s.id LIMIT 1""",
                (item["id"], item["event_id"]),
            )
            or {}
        )
        note(
            c,
            item["event_id"],
            snapshot.get("goal_version", 1),
            item["subject"],
            item["subject"],
            item["text"],
            item["kind"],
            reason,
            json.loads(item["citations"]),
            rid=snapshot.get("round_id"),
            at=item["created_at"],
        )
        c.execute(
            "INSERT INTO world_dispositions VALUES(?,'event',NULL,?,?)", (item["entry_id"], reason, utc())
        )
    # Old tasks did not have stable investigation IDs. Seed from each goal's latest substantive
    # plan instead of subscribing an event to every historical rephrasing of the same question.
    for r in rows(
        c,
        """SELECT id,event_id,goal_version,plan FROM tracking_rounds WHERE id IN (
                         SELECT max(id) FROM tracking_rounds WHERE json_array_length(plan,'$.tasks')>0
                         OR json_array_length(plan,'$.guides')>0 GROUP BY event_id,goal_version)""",
    ):
        save_guides(c, r, json.loads(r["plan"]))
    c.execute("INSERT INTO settings VALUES('board_migration_v1','1')")


def guide_matches(c, event, query, *, entry=None, limit=6):
    if entry and query.get("entry_id") not in (None, entry):
        return []
    hits, _ = world_search.search(
        c,
        query.get("query", ""),
        path=query.get("path", ""),
        entity_id=query.get("entity_id"),
        entry_id=entry or query.get("entry_id"),
        qualifiers=query.get("qualifiers"),
        event=event,
        demo=require(c, "events", event)["demo"],
        limit=limit,
    )
    return hits


def save_guides(c, r, plan):
    proposals = list(plan.get("guides", []))
    for task in plan.get("tasks", []):
        lookup = task.get("knowledge_query")
        if not lookup and not any(p["question"] == task["question"] for p in proposals):
            routes = world_catalog.route(task["question"] + " " + task.get("target", ""), limit=1)
            lookup = (
                {"path": routes[0]["path"]}
                if routes
                else {"query": (task.get("target") or task["question"])[:200]}
            )
        if lookup:
            proposals.append(
                {
                    "question": task["question"],
                    "reason": task.get("target") or task["question"],
                    "wake": task.get("done_when") or "有新的匹配版本时核查",
                    "lookup": lookup,
                    "line": task.get("line"),
                }
            )
    for item in proposals:
        query = {k: v for k, v in item["lookup"].items() if v}
        if not any(query.get(k) for k in ("query", "path", "entity_id", "entry_id")):
            continue
        if query.get("entry_id") and not one(
            c,
            """SELECT 1 FROM world_entries w JOIN events e ON e.demo=w.demo
                WHERE e.id=? AND w.id=?""",
            (r["event_id"], query["entry_id"]),
        ):
            continue
        encoded = json.dumps(query, sort_keys=True, ensure_ascii=False)
        gid = c.execute(
            """INSERT INTO event_guides(event_id,goal_version,question,reason,wake,query)
                        VALUES(?,?,?,?,?,?) ON CONFLICT(event_id,goal_version,question,query) DO UPDATE SET
                        reason=excluded.reason,wake=excluded.wake,active=1 RETURNING id""",
            (r["event_id"], r["goal_version"], item["question"], item["reason"], item["wake"], encoded),
        ).fetchone()[0]
        if line := item.get("line"):
            key = (r["event_id"], r["goal_version"], line)
            prior = one(
                c, "SELECT guide_id FROM guide_lines WHERE event_id=? AND goal_version=? AND line=?", key
            )
            if prior and prior["guide_id"] != gid:
                c.execute("UPDATE event_guides SET active=0 WHERE id=?", (prior["guide_id"],))
            c.execute(
                """INSERT INTO guide_lines VALUES(?,?,?,?) ON CONFLICT(event_id,goal_version,line)
                         DO UPDATE SET guide_id=excluded.guide_id""",
                (*key, gid),
            )
        for hit in guide_matches(c, r["event_id"], query):
            c.execute("INSERT OR IGNORE INTO guide_candidates VALUES(?,?,?)", (gid, hit["version_id"], utc()))
    for gid in plan.get("retire_guides", []):
        c.execute(
            "UPDATE event_guides SET active=0 WHERE id=? AND event_id=? AND goal_version=?",
            (gid, r["event_id"], r["goal_version"]),
        )


def notify(c, origin, versions):
    if not versions:
        return
    guides = rows(
        c,
        """SELECT g.* FROM event_guides g JOIN events e ON e.id=g.event_id
                    WHERE g.active AND e.status='active' AND e.demo=0 AND
                    g.goal_version=(SELECT max(version) FROM event_goals WHERE event_id=e.id)""",
    )
    for vid in versions:
        rec = one(c, "SELECT id FROM world_entries WHERE head_id=?", (vid,))
        if not rec:
            continue
        for g in guides:
            if not guide_matches(c, g["event_id"], json.loads(g["query"]), entry=rec["id"], limit=1):
                continue
            new = c.execute("INSERT OR IGNORE INTO guide_candidates VALUES(?,?,?)", (g["id"], vid, utc()))
            if new.rowcount and g["event_id"] != origin:
                enqueue(
                    c,
                    "guide_check",
                    event=g["event_id"],
                    args={"guide_id": g["id"], "version_id": vid, "dispatch_key": f"guide/{g['id']}/{vid}"},
                )


def view(c, event, goal):
    guides = rows(
        c,
        """SELECT id,question,reason,wake,query FROM event_guides
                    WHERE event_id=? AND goal_version=? AND active ORDER BY id DESC LIMIT 40""",
        (event, goal),
    )
    for g in guides:
        g["query"] = json.loads(g["query"])
        g["candidates"] = rows(
            c,
            """SELECT v.entry_id,h.version_id,k.subject FROM guide_candidates h JOIN world_versions v
                ON v.claim_id=h.version_id JOIN world_entries w ON w.id=v.entry_id AND w.head_id=h.version_id
                JOIN claims k ON k.id=h.version_id LEFT JOIN world_dispositions d ON d.entry_id=w.id
                WHERE h.guide_id=? AND d.entry_id IS NULL ORDER BY h.version_id DESC LIMIT 6""",
            (g["id"],),
        )
    notes = rows(
        c,
        """SELECT id,round_id,question,subject,body,kind,reason,citations FROM event_notes
                    WHERE event_id=? AND goal_version=? ORDER BY id DESC LIMIT 24""",
        (event, goal),
    )
    for n in notes:
        n["citations"] = json.loads(n["citations"])
    lines = rows(
        c,
        """SELECT l.id,l.line_key,l.question,l.role,t.id task_id,t.state,t.done_when,t.result FROM investigation_lines l
              JOIN research_tasks t ON t.id=l.last_task WHERE l.event_id=? AND l.goal_version=? ORDER BY l.id DESC LIMIT 24""",
        (event, goal),
    )
    for line in lines:
        report = json.loads(line.pop("result") or "{}")
        line["findings"] = [
            {
                "subject": finding["subject"],
                "text": finding["text"][:320],
                "fragments": [ref["fragment_id"] for ref in finding["citations"]],
            }
            for finding in report.get("findings", [])[:4]
        ]
        line["searches"] = [
            json.loads(row[0])["query"]
            for row in c.execute(
                "SELECT request FROM research_tool_steps WHERE task_id=? ORDER BY step", (line["task_id"],)
            )
        ]
    return {"guides": guides, "notes": notes, "lines": lines}


def archive_search(c, event, query, cutoff, limit=4):
    """Read archived material already associated with the event, within the research cutoff."""
    docs = rows(
        c,
        """SELECT d.id,d.title,d.summary,d.url FROM event_docs ed JOIN documents d ON d.id=ed.document_id
                  WHERE ed.event_id=? AND ed.intake NOT IN ('future','person_candidate')
                  AND d.observed_at<=? AND (d.published_at IS NULL OR d.published_at<=?)
                  ORDER BY d.observed_at DESC,d.id DESC""",
        (event, cutoff, cutoff),
    )
    terms = set(world_search.terms(query))
    ranked = []
    seen = set()
    for d in docs:
        title = set(world_search.terms(d["title"]))
        score = 4 * len(title & terms) + len(set(world_search.terms(d["summary"][:600])) & terms)
        if score:
            ranked.append((score, d))
    out = []
    for _, d in sorted(ranked, key=lambda x: -x[0]):
        if d["url"] not in seen:
            out.append(d["id"])
            seen.add(d["url"])
        if len(out) == limit:
            break
    return out


def indicator_parent(c, event, finding):
    values = finding.attributes
    if finding.category != "indicators" or finding.supports or finding.replaces:
        return None
    if not all(values.get(k) for k in ("measure", "unit", "period", "region")) or "value" not in values:
        return None
    # All dimensions must agree. A return for another region, period, methodology or comparison
    # is a separate observation even when its number happens to be identical.
    dimensions = {k: world_catalog.normalize(str(v)) for k, v in values.items() if k != "value"}
    found = []
    for other in rows(
        c,
        """SELECT k.id,k.subject,k.text,d.attributes,owner.identity entity_identity FROM world_entries w JOIN claims k ON k.id=w.head_id
                  JOIN world_record_data d ON d.version_id=k.id JOIN events e ON e.id=? AND e.demo=w.demo
                  LEFT JOIN entities owner ON owner.id=k.entity_id
                  WHERE w.category='indicators' AND NOT EXISTS(SELECT 1 FROM world_dispositions x WHERE x.entry_id=w.id)
                  AND json_extract(d.attributes,'$.measure')=?""",
        (event, values["measure"]),
    ):
        attrs = json.loads(other["attributes"])
        if (
            "value" in attrs
            and world_catalog.normalize(other["subject"]) == world_catalog.normalize(finding.subject)
            and (not finding.identity or finding.identity == other["entity_identity"])
            and {k: world_catalog.normalize(str(v)) for k, v in attrs.items() if k != "value"} == dimensions
        ):
            found.append(other)
    return found[0] if len(found) == 1 else None


def record_dispute(c, vid, event, finding, reason):
    v = one(
        c,
        """SELECT w.head_id,d.attributes FROM world_versions v JOIN world_entries w ON w.id=v.entry_id
               JOIN world_record_data d ON d.version_id=w.head_id WHERE v.claim_id=?""",
        (vid,),
    )
    if not v:
        return
    old = json.loads(v["attributes"])
    for key, value in finding.attributes.items():
        if key in old and old[key] != value:
            c.execute(
                """INSERT OR IGNORE INTO world_disputes(version_id,event_id,field,proposed,citations,reason)
                         VALUES(?,?,?,?,?,?)""",
                (
                    v["head_id"],
                    event,
                    key,
                    dump(value),
                    dump([x.model_dump() for x in finding.citations]),
                    reason,
                ),
            )


def set_disposition(c, entry, expected, state, target, reason, fragment, target_version=None):
    from . import workflow

    w = require(c, "world_entries", entry)
    if (
        w["demo"]
        or w["head_id"] != expected
        or one(c, "SELECT 1 FROM world_dispositions WHERE entry_id=?", (entry,))
    ):
        raise workflow.VersionConflict("请核对当前可用条目及版本")
    require(c, "fragments", fragment)
    if state == "merged":
        dest = require(c, "world_entries", target)
        if dest["head_id"] != target_version:
            raise workflow.VersionConflict("合并目标已更新，请重新核对目标版本")
        if (
            entry == target
            or dest["demo"]
            or one(c, "SELECT 1 FROM world_dispositions WHERE entry_id=?", (target,))
        ):
            raise ValueError("合并目标必须为其他有效的世界知识")
    elif state != "withdrawn" or target is not None:
        raise ValueError("无效的知识状态操作")
    used = rows(
        c,
        """SELECT DISTINCT s.event_id,s.id FROM knowledge_snapshots s JOIN snapshot_claims sc ON sc.snapshot_id=s.id
               JOIN world_versions v ON v.claim_id=sc.claim_id WHERE v.entry_id=? AND
               s.id=(SELECT max(id) FROM knowledge_snapshots WHERE event_id=s.event_id)""",
        (entry,),
    )
    cited_reason = f"{reason}（片段 #{fragment}）"
    c.execute("INSERT INTO world_dispositions VALUES(?,?,?,?,?)", (entry, state, target, cited_reason, utc()))
    for use in used:
        workflow.change_index(
            c,
            use["event_id"],
            require(c, "knowledge_snapshots", use["id"]),
            [{"id": expected, "world_id": entry, "kind": "correction", "text": cited_reason}],
            removed=True,
        )
    return {
        "entry_id": entry,
        "state": state,
        "target": target,
        "affected_events": [v["event_id"] for v in used],
    }


def resolve_dispute(c, entry, dispute, version, fragment, reason):
    from . import workflow

    w = require(c, "world_entries", entry)
    d = require(c, "world_disputes", dispute)
    original = one(c, "SELECT entry_id FROM world_versions WHERE claim_id=?", (d["version_id"],))
    if (
        not original
        or original["entry_id"] != entry
        or w["demo"]
        or w["head_id"] != version
        or d["state"] != "open"
    ):
        raise workflow.VersionConflict("争议或条目版本已变化")
    require(c, "fragments", fragment)
    c.execute(
        "INSERT INTO dispute_resolutions VALUES(?,?,?,?,?)", (dispute, version, fragment, reason, utc())
    )
    c.execute("UPDATE world_disputes SET state='resolved' WHERE id=?", (dispute,))
    return {"id": dispute, "state": "resolved", "version_id": version}
