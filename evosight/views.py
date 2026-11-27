import json

from . import forecast
from .db import config, expand_node, one, read_token, require, rows, usage


def coverage(sources, last_checked):
    if any(s["error"] for s in sources):
        return "部分来源未能核查" if any(s["last_attempt"] and not s["error"] for s in sources) else "本轮来源均未能核查"
    if not last_checked:
        return "尚待首次采集"
    return "已检查"


def with_fragments(c, record):
    result = expand_node(record)
    if result:
        result["parts"] = rows(
            c,
            """SELECT f.id,f.locator,a.document_id FROM node_fragments n JOIN fragments f ON f.id=n.fragment_id
                          JOIN evidence_assets a ON a.id=f.asset_id WHERE n.node_id=? ORDER BY f.id LIMIT 8""",
            (result["id"],),
        )
    return result


def card(c, e):
    event = e["id"]
    seen = (one(c, "SELECT seen FROM reads WHERE event_id=?", (event,)) or {}).get("seen", 0)
    counts = one(
        c,
        "SELECT coalesce(max(id),0) AS upper,count(CASE WHEN id>? THEN 1 END) AS unread FROM nodes WHERE event_id=?",
        (seen, event),
    )
    upper = counts["upper"]

    def latest(kind):
        return with_fragments(
            c,
            one(
                c,
                "SELECT * FROM nodes WHERE event_id=? AND kind=? AND id>? AND id<=? ORDER BY id DESC LIMIT 1",
                (event, kind, seen, upper),
            ),
        )

    sources = rows(
        c,
        """SELECT s.* FROM source_state s LEFT JOIN event_platforms ep ON ep.event_id=s.event_id AND ep.url=s.feed
                  WHERE s.event_id=? AND coalesce(ep.enabled,1)=1 ORDER BY s.feed""",
        (event,),
    )
    source_status = coverage(sources, e["last_checked"])
    last_round = one(
        c, "SELECT id,state,detail FROM tracking_rounds WHERE event_id=? ORDER BY id DESC LIMIT 1", (event,)
    )
    if (
        last_round
        and last_round["state"] == "waiting"
        and (not sources or not any(s["last_success"] for s in sources))
    ):
        if not sources or not any(s["last_attempt"] and not s["error"] for s in sources):
            source_status = "待补来源"
    elif last_round and last_round["state"] in ("failed", "partial", "deferred"):
        source_status = {"failed": "研究失败，可重试", "partial": "部分研究待补", "deferred": "研究等待模型额度"}[
            last_round["state"]
        ]
    if source_status == "已检查" and one(
        c,
        "SELECT 1 FROM event_docs WHERE event_id=? AND analyzed=0 AND intake IN ('initial','window','revision') LIMIT 1",
        (event,),
    ):
        source_status = "材料已采到，等待研究"
    preds = rows(
        c,
        """SELECT p.id,p.question,p.deadline,p.status,r.p,
            json_extract(s.payload,'$.levels[' || json_extract(o.payload,'$.primary_level') || '].label') AS scenario_label
            FROM predictions p JOIN revisions r ON r.id=(SELECT max(id) FROM revisions WHERE prediction_id=p.id)
            LEFT JOIN forecast_scales s ON s.prediction_id=p.id LEFT JOIN forecast_outlooks o ON o.revision_id=r.id
            WHERE p.event_id=? ORDER BY (p.id=coalesce(?,0)) DESC,r.created_at DESC,r.id DESC""",
        (event, e["pinned_prediction"]),
    )
    recent = one(c, "SELECT max(created_at) AS updated FROM nodes WHERE event_id=?", (event,))["updated"]
    return {
        **e,
        **counts,
        "seen": seen,
        "token": read_token(c, event, upper),
        "fact": latest("fact"),
        "judgment": latest("judgment"),
        "prior_judgment": expand_node(
            one(
                c,
                "SELECT * FROM nodes WHERE event_id=? AND kind='judgment' AND id<=? ORDER BY id DESC LIMIT 1",
                (event, seen),
            )
        ),
        "last_round": last_round,
        "correction": latest("correction"),
        "last_node": expand_node(
            one(c, "SELECT * FROM nodes WHERE event_id=? AND id>? ORDER BY id DESC LIMIT 1", (event, seen))
        ),
        "coverage": source_status,
        "sources": sources,
        "predictions": preds,
        "updated": recent or e["created_at"],
    }


def home(c):
    cards = [card(c, e) for e in rows(c, "SELECT * FROM events")]
    cards.sort(key=lambda r: (r["unread"] > 0, r["updated"], r["id"]), reverse=True)
    return {
        "cards": cards,
        "limits": config(c),
        "usage": usage(c),
        "stats": {
            "events": len(cards),
            "unread": sum(r["unread"] for r in cards),
            "pending": c.execute(
                "SELECT count(*) FROM predictions WHERE status IN ('open','frozen','review')"
            ).fetchone()[0],
            "candidates": c.execute("SELECT count(*) FROM candidates WHERE state='review'").fetchone()[0],
        },
        "recent_jobs": rows(
            c,
            "SELECT j.*,e.title AS event_title FROM jobs j LEFT JOIN events e ON e.id=j.event_id ORDER BY j.id DESC LIMIT 4",
        ),
    }


def documents(c, event):
    docs = rows(
        c,
        "SELECT d.*,ed.intake FROM documents d JOIN event_docs ed ON ed.document_id=d.id WHERE ed.event_id=? ORDER BY d.id DESC",
        (event,),
    )
    for doc in docs:
        doc["mentions"] = rows(
            c, "SELECT feed,url FROM mentions WHERE document_id=? ORDER BY feed,url", (doc["id"],)
        )
        doc["linked_events"] = rows(
            c,
            "SELECT e.id,e.title FROM events e JOIN event_docs ed ON ed.event_id=e.id WHERE ed.document_id=? ORDER BY e.title",
            (doc["id"],),
        )
        if doc["supersedes"]:
            doc["affected"] = rows(
                c,
                "SELECT id,kind,title FROM nodes WHERE event_id=? AND id IN (SELECT n.id FROM nodes n,json_each(n.refs) ref WHERE ref.value=?)",
                (event, doc["supersedes"]),
            )
    return docs


def event_details(c, ident):
    e = require(c, "events", ident)
    return {
        "card": card(c, e),
        "items": rows(c, "SELECT * FROM items WHERE event_id=? ORDER BY pinned DESC,id", (ident,)),
        "documents": documents(c, ident),
        "predictions": [
            forecast.detail(c, r["id"])
            for r in rows(c, "SELECT id FROM predictions WHERE event_id=? ORDER BY id DESC", (ident,))
        ],
        "candidates": rows(
            c,
            "SELECT id,title,scope,reason,state,target_id FROM candidates WHERE event_id=? ORDER BY id DESC",
            (ident,),
        ),
        "related": rows(
            c,
            "SELECT e.id,e.title,count(*) AS shared FROM event_docs a JOIN event_docs b ON a.document_id=b.document_id JOIN events e ON e.id=b.event_id WHERE a.event_id=? AND b.event_id!=? GROUP BY e.id ORDER BY shared DESC",
            (ident, ident),
        ),
    }


def timeline(c, event, kind="", order="known", item=0, page=1, after=0, through=None):
    require(c, "events", event)
    where = "event_id=? AND id>?"
    args = [event, after]
    if through is not None:
        where += " AND id<=?"
        args.append(through)
    if kind:
        where += " AND kind=?"
        args.append(kind)
    if item:
        where += " AND item_id=?"
        args.append(item)
    sort = "(occurred_at IS NULL),occurred_at DESC,id DESC" if order == "occurred" else "id DESC"
    total = c.execute("SELECT count(*) FROM nodes WHERE " + where, args).fetchone()[0]
    results = rows(
        c,
        "SELECT * FROM nodes WHERE " + where + " ORDER BY " + sort + " LIMIT 30 OFFSET ?",
        [*args, (page - 1) * 30],
    )
    upper = c.execute(
        "SELECT coalesce(max(id),0) FROM nodes WHERE event_id=? AND (? IS NULL OR id<=?)",
        (event, through, through),
    ).fetchone()[0]
    return {
        "nodes": [with_fragments(c, r) for r in results],
        "total": total,
        "page": page,
        "token": read_token(c, event, upper),
    }


def explore(c):
    ts = rows(c, "SELECT * FROM topics ORDER BY id DESC")
    for t in ts:
        t["feeds"] = json.loads(t["feeds"])
        t["keywords"] = json.loads(t["keywords"])
    cs = rows(c, "SELECT * FROM candidates ORDER BY CASE state WHEN 'review' THEN 0 ELSE 1 END,id DESC")
    for item in cs:
        ids = json.loads(item["refs"])
        item["evidence"] = [one(c, "SELECT id,title,url FROM documents WHERE id=?", (i,)) for i in ids]
    return {"topics": ts, "candidates": cs}


def ledger(c):
    return {
        "predictions": [
            {**forecast.detail(c, r["id"]), "event_title": r["title"], "demo": r["demo"]}
            for r in rows(
                c,
                "SELECT p.id,e.title,e.demo FROM predictions p JOIN events e ON e.id=p.event_id ORDER BY p.id DESC",
            )
        ],
        "metrics": forecast.metrics(c),
        "fixed": rows(
            c,
            "SELECT f.*,e.title AS event_title FROM fixed_questions f JOIN events e ON e.id=f.event_id ORDER BY f.id DESC",
        ),
        "abstentions": rows(
            c,
            "SELECT n.*,e.title AS event_title FROM forecast_notes n JOIN events e ON e.id=n.event_id ORDER BY n.id DESC LIMIT 50",
        ),
    }
