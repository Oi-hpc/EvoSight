"""Shared catalog access: indexed discovery, version-pinned reads and evidence projection."""
from datetime import date, timedelta
import json
import math
import re

from . import world_catalog as catalog
from .db import dump, one, require, rows

STOP = {"追踪", "跟踪", "是否", "本次", "事件", "情况", "相关", "核对", "最新", "以及", "研究", "公告", "the", "and"}
KINDS = {"fact", "reported", "view", "hypothesis", "relation", "correction", "conflict"}
INDEX_FORMAT = 3


def terms(text):
    text = catalog.normalize(text)
    out = set()
    for token in re.findall(r"[a-z0-9]+(?:[.+-][a-z0-9]+)*", text):
        if len(token) > 1:
            out.add(token[:100])
            out.update(p for p in re.split(r"[-+]", token) if len(p) > 1)
            if token.startswith("v") and token[1:2].isdigit():
                out.add(token[1:])
    for word in re.findall(r"[\u3400-\u9fff]+", text):
        if len(word) <= 20:
            out.add(word)
        out.update(word[i : i + 2] for i in range(len(word) - 1))
    return out - STOP


def query_terms(query):
    normalized = catalog.normalize(query)
    # Version numbers and catalog codes already have exact postings. A common prefix such as
    # "EV" would otherwise visit every code-sharing entry before selecting a few candidates.
    if re.fullmatch(r"[a-z0-9]+(?:[.+-][a-z0-9]+)+", normalized) and re.search(r"\d", normalized):
        return [normalized], ["=" + normalized]
    words = re.sub(r"有什么|有哪些|是什么|是多少|是否有|请问|怎么样|怎样|如何", " ", normalized)
    return sorted(terms(words), key=lambda t: (-len(t), t))[:100], ["=" + normalized[:250]]


def record_version(c, ident, category, *, path="", attributes=None):
    item = require(c, "claims", ident)
    extra = one(c, "SELECT facets FROM claim_details WHERE claim_id=?", (ident,))
    facets = json.loads(extra["facets"]) if extra else {}
    fields = catalog.attributes(category, facets, attributes)
    path = catalog.directory_for(category, item["subject"], item["text"], path)
    c.execute(
        """INSERT OR IGNORE INTO world_record_data(version_id,schema_id,schema_version,path,attributes)
                 VALUES(?,?,?,?,?)""",
        (ident, category, catalog.SCHEMA_VERSION, path, dump(fields)),
    )


def index_head(c, entry):
    w = require(c, "world_entries", entry)
    k = require(c, "claims", w["head_id"])
    data = one(c, "SELECT path,attributes FROM world_record_data WHERE version_id=?", (k["id"],))
    path = data["path"]
    c.execute(
        """INSERT INTO world_catalog_members(entry_id,head_id,demo,path,entity_id,kind,occurred_at,recorded_at,index_format)
                 VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(entry_id) DO UPDATE SET head_id=excluded.head_id,
                 path=excluded.path,entity_id=excluded.entity_id,kind=excluded.kind,
                 occurred_at=excluded.occurred_at,recorded_at=excluded.recorded_at,index_format=excluded.index_format""",
        (
            entry,
            k["id"],
            w["demo"],
            path,
            k["entity_id"],
            k["kind"],
            k["occurred_at"],
            k["created_at"],
            INDEX_FORMAT,
        ),
    )
    names = (
        [
            r[0]
            for r in c.execute(
                "SELECT alias FROM world_aliases WHERE demo=? AND entity_id=?", (w["demo"], k["entity_id"])
            )
        ]
        if k["entity_id"]
        else []
    )
    path_words = [
        word for p in catalog.prefixes(path)[:-1] for word in (catalog.PATHS[p][0], *catalog.PATHS[p][1])
    ]
    label, aliases = catalog.PATHS[path]
    weights = {}
    # Separate the searchable labels of this subject from weaker matches in prose and directory aliases.
    for text, weight in (
        (k["text"], 2),
        (" ".join(path_words), 4),
        (" ".join(str(v) for v in json.loads(data["attributes"]).values()), 6),
        (" ".join((label, *aliases)), 9),
        (k["subject"], 12),
        (" ".join(names), 14),
    ):
        for token in terms(text):
            weights[token] = weight
    weights["=" + catalog.normalize(k["subject"])] = 32
    category = path.partition("/")[0]
    for key, value in json.loads(data["attributes"]).items():
        weights[catalog.qualifier_token(category, key, value)] = 1
    c.execute("DELETE FROM world_terms WHERE entry_id=?", (entry,))
    c.executemany(
        "INSERT INTO world_terms(term,entry_id,weight) VALUES(?,?,?)",
        [(t, entry, weights[t]) for t in sorted(weights)],
    )


def repair(c):
    for v in rows(
        c,
        """SELECT v.claim_id,w.category FROM world_versions v JOIN world_entries w ON w.id=v.entry_id
                       LEFT JOIN world_record_data d ON d.version_id=v.claim_id WHERE d.version_id IS NULL ORDER BY v.claim_id""",
    ):
        record_version(c, v["claim_id"], v["category"])
    for row in c.execute(
        """SELECT w.id FROM world_entries w LEFT JOIN world_catalog_members m ON m.entry_id=w.id
                            WHERE m.head_id IS NULL OR m.head_id!=w.head_id OR m.index_format!=?""",
        (INDEX_FORMAT,),
    ).fetchall():
        index_head(c, row[0])


def directory(c, path="", demo=0):
    trail = catalog.breadcrumbs(path)
    direct = {
        r[0]: r[1]
        for r in c.execute(
            "SELECT path,count(*) FROM world_catalog_members WHERE demo=? GROUP BY path", (demo,)
        )
    }
    totals = {}
    for p, n in direct.items():
        for parent in catalog.prefixes(p):
            totals[parent] = totals.get(parent, 0) + n
    folders = rows(
        c,
        "SELECT code AS path,label FROM world_directories WHERE parent_code IS ? ORDER BY rowid",
        (path or None,),
    )
    for folder in folders:
        p = folder["path"]
        folder.update(count=totals.get(p, 0), direct_count=direct.get(p, 0), schema_id=p.split("/")[0])
    return {
        "path": path,
        "breadcrumbs": trail,
        "children": folders,
        "direct_count": direct.get(path, 0),
        "count": totals.get(path, 0) if path else sum(direct.values()),
        "schema_id": path.partition("/")[0] or None,
    }


def manifest():
    return {
        "record_format_version": catalog.SCHEMA_VERSION,
        "types": catalog.CATEGORIES,
        "routes": {
            "catalog": "/api/world/catalog",
            "route": "/api/world/route",
            "schema": "/api/world/schemas/{schema_id}",
            "search": "/api/world/search",
            "read_versions": "/api/world/records",
        },
        "paths": [
            {
                "path": p,
                "label": catalog.PATHS[p][0],
                "parent": p.rpartition("/")[0] or None,
                "schema_id": p.partition("/")[0],
            }
            for p in catalog.PATHS
        ],
        "limits": {"search": 24, "read_versions": 12, "citations_per_record": 4, "qualifiers": 4},
        "qualifiers": {
            "parameter": "qualifiers",
            "encoding": "JSON object",
            "match": "all fields equal",
            "fields": "schema.properties",
            "requires": "path",
        },
    }


def candidate_query(
    q="",
    *,
    path="",
    kind="",
    entity_id=None,
    occurred_from="",
    occurred_to="",
    qualifiers=None,
    demo=0,
    event=None,
    discovery=False,
):
    conditions = ["m.demo=?"]
    args = [demo]
    if qualifiers is not None:
        for term in catalog.qualifier_terms(path, qualifiers):
            conditions.append("m.entry_id IN (SELECT entry_id FROM world_terms WHERE term=?)")
            args.append(term)
    if path:
        catalog.prefixes(path)
        scope = [p for p in catalog.PATHS if p == path or p.startswith(path + "/")]
        conditions.append("m.path IN (" + ",".join("?" for _ in scope) + ")")
        args.extend(scope)
    if kind:
        if kind not in KINDS:
            raise ValueError("未知知识性质")
        conditions.append("m.kind=?")
        args.append(kind)
    if entity_id is not None:
        conditions.append("m.entity_id=?")
        args.append(entity_id)
    for field, operator in ((occurred_from, ">="), (occurred_to, "<")):
        if field:
            if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", field):
                raise ValueError("发生日期应为 YYYY-MM-DD")
            day = date.fromisoformat(field)
            if operator == "<" and day == date.max:
                conditions.append("coalesce(m.occurred_at,'')!=''")
                continue
            conditions.append("m.occurred_at" + operator + "?")
            args.append((day + timedelta(days=1) if operator == "<" else day).isoformat())
            if operator == "<" and not occurred_from:
                conditions.append("m.occurred_at!=''")
    if occurred_from and occurred_to and occurred_from > occurred_to:
        raise ValueError("发生日期范围无效")
    if event is not None:
        conditions.append(
            """NOT EXISTS(SELECT 1 FROM world_link_choices l WHERE l.event_id=? AND l.entry_id=m.entry_id AND l.choice='exclude')"""
        )
        args.append(event)
        if discovery:
            conditions.append(
                """NOT EXISTS(SELECT 1 FROM world_versions v JOIN snapshot_claims s ON s.claim_id=v.claim_id
                            WHERE v.entry_id=m.entry_id AND s.snapshot_id=(SELECT max(id) FROM knowledge_snapshots WHERE event_id=?))"""
            )
            args.append(event)
    where = " AND ".join(conditions)
    if not q.strip():
        return "SELECT m.entry_id,m.recorded_at,0 AS score FROM world_catalog_members m WHERE " + where, args
    tokens, exact = query_terms(q)
    if not tokens:
        return "SELECT m.entry_id,m.recorded_at,0 AS score FROM world_catalog_members m WHERE 0", []
    # Long research goals use partial lexical evidence, while a concise catalog lookup expects closer coverage.
    threshold = min(2, len(tokens)) if discovery else max(1, math.ceil(len(tokens) * 0.5))
    sql = (
        """SELECT m.entry_id,m.recorded_at,sum(t.weight) AS score FROM world_terms t
           CROSS JOIN world_catalog_members m ON m.entry_id=t.entry_id WHERE t.term IN (SELECT value FROM json_each(?))
           AND """
        + where
        + " GROUP BY m.entry_id HAVING count(*)>=?"
    )
    if discovery:
        sql += " OR max(CASE WHEN length(t.term)>=3 AND t.weight>=4 THEN 1 ELSE 0 END)=1"
    return sql, [dump(tokens + exact), *args, threshold]


def search(c, q="", *, limit=8, offset=0, include_total=False, compact=True, **filters):
    sql, args = candidate_query(q, **filters)
    order = (
        "score DESC,m.recorded_at DESC,m.entry_id DESC" if q.strip() else "m.recorded_at DESC,m.entry_id DESC"
    )
    select_text = "substr(k.text,1,240)" if compact else "k.text"
    result = rows(
        c,
        "WITH hits AS ("
        + sql
        + " ORDER BY "
        + order
        + " LIMIT ? OFFSET ?) "
        + """SELECT h.entry_id,m.head_id AS version_id,v.version,d.schema_id,d.schema_version,m.path,k.subject,
           """
        + select_text
        + """ AS text,length(k.text) AS text_length,m.kind,m.entity_id,m.occurred_at,k.precision,m.recorded_at,k.refs,h.score FROM hits h
           JOIN world_catalog_members m ON m.entry_id=h.entry_id JOIN claims k ON k.id=m.head_id
           JOIN world_record_data d ON d.version_id=m.head_id JOIN world_versions v ON v.claim_id=m.head_id
           ORDER BY h.score DESC,h.recorded_at DESC,h.entry_id DESC""",
        (*args, limit, offset),
    )
    for hit in result:
        hit["refs"] = json.loads(hit["refs"])
        hit["text_complete"] = len(hit["text"]) == hit.pop("text_length")
        hit["category"] = hit["schema_id"]
    total = c.execute("SELECT count(*) FROM (" + sql + ")", args).fetchone()[0] if include_total else None
    return result, total


def read_records(c, requests, fields=(), evidence=False, *, demo=0):
    if len(requests) > 12 or len(fields) > 12:
        raise ValueError("每批限 12 个条目和 12 个属性")
    result = []
    for requested in requests:
        entry, revision = requested["entry_id"], requested.get("version_id") or None
        row = one(
            c,
            """SELECT w.id AS entry_id,k.id AS version_id,v.version,d.schema_id,d.schema_version,d.path,d.attributes,
                     k.subject,k.text,k.kind,k.entity_id,k.occurred_at,k.precision,k.created_at AS recorded_at,k.refs,k.citations
                     FROM world_entries w JOIN claims k ON k.id=coalesce(?,w.head_id)
                     JOIN world_versions v ON v.claim_id=k.id AND v.entry_id=w.id JOIN world_record_data d ON d.version_id=k.id
                     WHERE w.id=? AND w.demo=?""",
            (revision, entry, demo),
        )
        if not row:
            result.append({**requested, "status": "not_found"})
            continue
        values = json.loads(row["attributes"])
        known = catalog.SCHEMA_FIELDS[row["schema_id"]]
        row["attributes"] = {k: v for k, v in values.items() if not fields or k in fields}
        row["missing_fields"] = [k for k in fields if k in known and k not in values]
        row["undefined_fields"] = [k for k in fields if k not in known]
        cites = json.loads(row.pop("citations"))
        row["refs"] = json.loads(row["refs"])
        row.update(status="ok", evidence_count=len(cites), source_document_count=len(set(row["refs"])))
        if evidence:
            row["citations"] = cites[:4]
        result.append(row)
    return result
