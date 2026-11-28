"""Shared, evidence-backed knowledge and the version pointers used by event indexes."""
import hashlib
import json
import re

from . import cards, world_catalog as catalog, world_search as search_index
from .db import dump, one, require, rows
from .world_catalog import CATEGORIES, normalize


def classify(role, section, kind, entity_kind, preferred="", subject=""):
    if preferred in CATEGORIES:
        return preferred
    if kind == "hypothesis":
        return "analysis"
    if kind == "relation":
        return "relations"
    if re.search(r"(月度数据|季度数据|年度数据|价格指数|增长率|失业率|通胀率|利率|收益率)$", subject):
        return "indicators"
    if re.search(r"(条例|法案|行政令|实施办法|监管规则|政策框架)(?:[（(].*?[）)])?$", subject):
        return "policies"
    if entity_kind in ("person", "organization", "country"):
        return {"person": "people", "organization": "organizations", "country": "places"}[entity_kind]
    if section == "opinion" or kind == "view":
        return "opinion"
    if role in ("related", "dependencies"):
        return "relations"
    if role == "organizations" and section != "news":
        return "organizations"
    return {"people": "people", "news": "events"}.get(section, "background")


def signature(event, kind, subject, text, occurred, entity, citations, facets, parent=None, attributes=None):
    # Match an assertion and its supporting passages, never people by display name alone.
    # Event-specific interpretations keep the scope in which they were proposed.
    scoped = kind in ("hypothesis", "conflict") or any(k in facets for k in ("预测ID", "关联事件ID", "原判断"))
    value = [
        kind,
        entity or normalize(subject),
        normalize(text),
        occurred or "",
        sorted({(i["document_id"], i["fragment_id"], normalize(i["quote"])) for i in citations}),
        sorted(facets.items()),
        event if scoped else None,
        parent,
    ]
    if attributes:
        value.append(sorted(attributes.items()))
    return hashlib.sha256(dump(value).encode()).hexdigest()


def version(c, claim):
    return one(
        c,
        """SELECT v.*,w.category,w.head_id FROM world_versions v JOIN world_entries w ON w.id=v.entry_id
                     WHERE v.claim_id=?""",
        (claim,),
    )


def matching(c, demo, key):
    return one(
        c,
        """SELECT v.*,w.head_id FROM world_versions v JOIN world_entries w ON w.id=v.entry_id
                     WHERE v.demo=? AND v.signature=? AND v.claim_id=v.canonical_id""",
        (demo, key),
    )


def resolve_entity(c, demo, kind, name, identity, aliases=()):
    # Name alone does not establish identity. An alias only resolves within the same verified identity.
    match = one(
        c,
        """SELECT e.id FROM entities e LEFT JOIN world_aliases a ON a.entity_id=e.id AND a.demo=?
                 WHERE e.kind=? AND e.identity=? AND (e.name=? OR a.alias=?) LIMIT 1""",
        (demo, kind, identity, name, name),
    )
    if match:
        entity = match["id"]
    else:
        c.execute(
            "INSERT OR IGNORE INTO entities(kind,name,identity) VALUES(?,?,?)",
            (kind, name, identity),
        )
        entity = one(
            c,
            "SELECT id FROM entities WHERE kind=? AND name=? AND identity=?",
            (kind, name, identity),
        )["id"]
    c.executemany(
        "INSERT OR IGNORE INTO entity_aliases VALUES(?,?)",
        [(entity, a) for a in [name, *aliases] if a.strip()],
    )
    add_aliases(c, demo, entity, [name, *aliases])
    return entity


def add_aliases(c, demo, entity_id, names):
    """Index a newly evidenced alias for every entry about this verified entity."""
    extra_terms = set()
    for name in dict.fromkeys(n.strip() for n in names if n.strip()):
        if c.execute("INSERT OR IGNORE INTO world_aliases VALUES(?,?,?)", (demo, entity_id, name)).rowcount:
            extra_terms.update(search_index.terms(name))
    c.executemany(
        """INSERT INTO world_terms(term,entry_id,weight)
                     SELECT ?,w.id,14 FROM claims k JOIN world_entries w ON w.head_id=k.id
                     WHERE k.entity_id=? AND w.demo=?
                     ON CONFLICT(term,entry_id) DO UPDATE SET weight=max(weight,excluded.weight)""",
        [(t, entity_id, demo) for t in sorted(extra_terms)],
    )


def register(c, ident, *, category=None, key=None, path="", attributes=None):
    known = version(c, ident)
    if known:
        return known
    item = require(c, "claims", ident)
    demo = one(c, "SELECT demo FROM events WHERE id=?", (item["event_id"],))["demo"]
    parent = version(c, item["replaces"]) if item["replaces"] else None
    detail = one(c, "SELECT * FROM claim_details WHERE claim_id=?", (ident,)) or {}
    entity = require(c, "entities", item["entity_id"]) if item["entity_id"] else {}
    aliases = json.loads(detail.get("aliases", "[]"))
    if entity:
        add_aliases(c, demo, entity["id"], [item["subject"], *aliases])
    category = (
        parent["category"]
        if parent
        else classify(
            item["role"], item["section"], item["kind"], entity.get("kind"), category, item["subject"]
        )
    )
    key = key or signature(
        item["event_id"],
        item["kind"],
        item["subject"],
        item["text"],
        item["occurred_at"],
        item["entity_id"],
        json.loads(item["citations"]),
        json.loads(detail.get("facets", "{}")),
        parent["canonical_id"] if parent else None,
    )
    same = matching(c, demo, key)
    if same:
        # Historical duplicate IDs stay resolvable in old event snapshots and run receipts.
        c.execute(
            "INSERT INTO world_versions VALUES(?,?,?,?,?,?)",
            (ident, same["entry_id"], same["version"], same["canonical_id"], demo, key),
        )
        source = one(
            c,
            "SELECT schema_id,path,attributes FROM world_record_data WHERE version_id=?",
            (same["canonical_id"],),
        )
        search_index.record_version(
            c,
            ident,
            source["schema_id"] if source else category,
            path=source["path"] if source else path,
            attributes=json.loads(source["attributes"]) if source else attributes,
        )
        return version(c, ident)
    if parent:
        entry = parent["entry_id"]
        number = c.execute("SELECT max(version)+1 FROM world_versions WHERE entry_id=?", (entry,)).fetchone()[
            0
        ]
        c.execute("UPDATE world_entries SET head_id=? WHERE id=?", (ident, entry))
    else:
        entry = c.execute(
            "INSERT INTO world_entries(demo,category,head_id) VALUES(?,?,?)", (demo, category, ident)
        ).lastrowid
        number = 1
    c.execute("INSERT INTO world_versions VALUES(?,?,?,?,?,?)", (ident, entry, number, ident, demo, key))
    if parent and not path:
        prior = one(c, "SELECT path FROM world_record_data WHERE version_id=?", (parent["canonical_id"],))
        path = prior["path"] if prior else ""
    search_index.record_version(c, ident, category, path=path, attributes=attributes)
    search_index.index_head(c, entry)
    return version(c, ident)


def prepare_schema(c):
    catalog.seed(c)
    if "index_format" not in {r["name"] for r in c.execute("PRAGMA table_info(world_catalog_members)")}:
        c.execute("ALTER TABLE world_catalog_members ADD COLUMN index_format INTEGER NOT NULL DEFAULT 1")
    if "weight" not in {r["name"] for r in c.execute("PRAGMA table_info(world_terms)")}:
        c.execute("ALTER TABLE world_terms ADD COLUMN weight INTEGER NOT NULL DEFAULT 1")
    names = {r["name"] for r in c.execute("PRAGMA table_info(snapshot_claims)")}
    for name in ("index_section", "index_reason"):
        if name not in names:
            c.execute(f"ALTER TABLE snapshot_claims ADD COLUMN {name} TEXT NOT NULL DEFAULT ''")
    if "world_seed_only" not in {r["name"] for r in c.execute("PRAGMA table_info(tracking_rounds)")}:
        c.execute("ALTER TABLE tracking_rounds ADD COLUMN world_seed_only INTEGER NOT NULL DEFAULT 0")


def migrate(c):
    for (ident,) in c.execute(
        """SELECT k.id FROM claims k LEFT JOIN world_versions v ON v.claim_id=k.id
                                 WHERE v.claim_id IS NULL ORDER BY k.id"""
    ).fetchall():
        register(c, ident)
    search_index.repair(c)
    c.execute(
        """UPDATE snapshot_claims SET index_section=(SELECT section FROM claims WHERE id=claim_id),
                 index_reason='沿用此次研究已引用的材料' WHERE index_section=''"""
    )


def freeze_index(c, event, snap):
    """Normalize only a new index, keeping earlier snapshots and forecast evidence immutable."""
    parent = require(c, "knowledge_snapshots", snap)["parent_id"]
    previous = {
        r["entry_id"]: r
        for r in rows(
            c,
            """SELECT v.entry_id,s.index_section,s.index_reason
                    FROM snapshot_claims s JOIN world_versions v ON v.claim_id=s.claim_id WHERE s.snapshot_id=?""",
            (parent,),
        )
    }
    new = {}
    excluded = {
        r[0]
        for r in c.execute(
            "SELECT entry_id FROM world_link_choices WHERE event_id=? AND choice='exclude'", (event,)
        )
    }
    excluded.update(row[0] for row in c.execute("SELECT entry_id FROM world_dispositions"))
    for ref in rows(
        c,
        """SELECT s.*,k.section FROM snapshot_claims s JOIN claims k ON k.id=s.claim_id
                          WHERE s.snapshot_id=? ORDER BY s.claim_id""",
        (snap,),
    ):
        v = register(c, ref["claim_id"])
        if v["entry_id"] in excluded:
            continue
        prior = previous.get(v["entry_id"], {})
        explicit = bool(ref["index_reason"])
        if v["entry_id"] not in new or explicit:
            section = ref["index_section"] or prior.get("index_section") or ref["section"]
            reason = ref["index_reason"] or prior.get("index_reason") or "本轮研究引用的证据与关注目标相关"
            new[v["entry_id"]] = (snap, v["head_id"], section, reason)
    c.execute("DELETE FROM snapshot_claims WHERE snapshot_id=?", (snap,))
    c.executemany(
        "INSERT INTO snapshot_claims(snapshot_id,claim_id,index_section,index_reason) VALUES(?,?,?,?)",
        new.values(),
    )
    c.execute(
        """INSERT OR IGNORE INTO event_docs(event_id,document_id,analyzed,intake)
                 SELECT ?,j.value,1,'backfill' FROM snapshot_claims s JOIN claims k ON k.id=s.claim_id
                 JOIN json_each(k.refs) j WHERE s.snapshot_id=?""",
        (event, snap),
    )


def suggested(c, event, limit=8):
    goal = require(c, "events", event)
    return search_index.search(
        c, goal["title"] + " " + goal["scope"], limit=limit, event=event, demo=goal["demo"], discovery=True
    )[0]


def browse(c, q="", category="", offset=0, limit=24, *, path=""):
    if category and category not in CATEGORIES:
        raise ValueError("未知世界知识分类")
    if path and category and path.partition("/")[0] != category:
        raise ValueError("目录与分类不一致")
    entries, count = search_index.search(
        c, q, path=path or category, limit=limit, offset=offset, include_total=True, compact=False
    )
    for hit in entries:
        hit.update(
            id=hit["entry_id"],
            head_id=hit["version_id"],
            created_at=hit["recorded_at"],
            breadcrumbs=catalog.breadcrumbs(hit["path"]),
        )
    counts = {
        r[0]: r[1]
        for r in c.execute(
            """SELECT category,count(*) FROM world_entries w WHERE demo=0 AND NOT EXISTS
                           (SELECT 1 FROM world_dispositions d WHERE d.entry_id=w.id) GROUP BY category"""
        )
    }
    return {
        "entries": entries,
        "total": count,
        "categories": CATEGORIES,
        "counts": counts,
        "directory": search_index.directory(c, path or category),
    }


def detail(c, entry, revision=None):
    item = require(c, "world_entries", entry)
    item["disposition"] = one(
        c, "SELECT state,target,reason FROM world_dispositions WHERE entry_id=?", (entry,)
    )
    ident = revision or item["head_id"]
    meta = version(c, ident)
    if not meta or meta["entry_id"] != entry:
        raise KeyError("该版本不属于此知识条目")
    claim = require(c, "claims", ident)
    extra = one(c, "SELECT facets,aliases FROM claim_details WHERE claim_id=?", (ident,)) or {}
    claim.update({k: json.loads(claim[k]) for k in ("citations", "refs")})
    claim.update(facets=json.loads(extra.get("facets", "{}")), aliases=json.loads(extra.get("aliases", "[]")))
    record = one(
        c,
        "SELECT schema_id,schema_version,path,attributes FROM world_record_data WHERE version_id=?",
        (ident,),
    )
    links = rows(
        c,
        """SELECT e.id,e.title,s.index_section,s.index_reason FROM events e
                    JOIN knowledge_snapshots n ON n.event_id=e.id JOIN snapshot_claims s ON s.snapshot_id=n.id
                    JOIN world_versions v ON v.claim_id=s.claim_id WHERE v.entry_id=? AND e.status!='deleted'
                    AND n.id=(SELECT max(id) FROM knowledge_snapshots WHERE event_id=e.id) ORDER BY e.id""",
        (entry,),
    )
    history = rows(
        c,
        """SELECT v.claim_id AS id,v.version,k.created_at,k.role,e.id AS origin_id,e.title AS origin
                      FROM world_versions v JOIN claims k ON k.id=v.claim_id JOIN events e ON e.id=k.event_id
                      WHERE v.entry_id=? AND v.claim_id=v.canonical_id ORDER BY v.version DESC""",
        (entry,),
    )
    entity = require(c, "entities", claim["entity_id"]) if claim["entity_id"] else None
    related = []
    if entity:
        entity["aliases"] = (
            claim["aliases"]
            if ident != item["head_id"]
            else [
                r[0]
                for r in c.execute(
                    "SELECT alias FROM world_aliases WHERE demo=? AND entity_id=? ORDER BY alias",
                    (item["demo"], entity["id"]),
                )
            ]
        )
        related = rows(
            c,
            """SELECT w.id,k.subject,k.text FROM claims k JOIN world_entries w ON w.head_id=k.id
                         WHERE k.entity_id=? AND w.demo=? AND w.id!=? AND NOT EXISTS
                         (SELECT 1 FROM world_dispositions d WHERE d.entry_id=w.id) ORDER BY w.id DESC LIMIT 24""",
            (entity["id"], item["demo"], entry),
        )
    fields, card_evidence = cards.read(c, ident, json.loads(record["attributes"]))
    return {
        **item,
        "card_evidence": card_evidence,
        "disputes": rows(
            c,
            """SELECT d.*,r.reason resolution FROM world_disputes d JOIN world_versions v ON v.claim_id=d.version_id
                           LEFT JOIN dispute_resolutions r ON r.dispute_id=d.id WHERE v.entry_id=? ORDER BY d.id""",
            (entry,),
        ),
        "version": meta["version"],
        "claim": claim,
        "history": history,
        "events": links,
        "category_name": CATEGORIES[item["category"]],
        "path": record["path"],
        "breadcrumbs": catalog.breadcrumbs(record["path"]),
        "schema_id": record["schema_id"],
        "schema_version": record["schema_version"],
        "attributes": fields,
        "field_evidence": [
            ref
            for ref in rows(
                c,
                "SELECT field,fragment_id,quote FROM field_evidence WHERE version_id=? ORDER BY field,fragment_id",
                (ident,),
            )
            if ref["field"].removeprefix("attributes.") not in cards.CARD_FIELDS
            or ref["field"].removeprefix("attributes.") in fields
        ],
        "field_labels": catalog.SCHEMA_FIELDS[record["schema_id"]],
        "entity": entity,
        "related_entries": related,
    }
