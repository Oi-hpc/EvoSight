"""Execute recoverable research rounds and publish their evidence, knowledge, and follow-up work."""
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from contextvars import copy_context
from datetime import datetime, timedelta
import json
from itertools import zip_longest
import re
import time

from . import (
    board,
    cards,
    claim_review,
    delivery,
    feeds,
    focus,
    grounding,
    materials,
    people,
    resources,
    steering,
    steering_reports,
    wakes,
    watch,
    workflow,
    world,
    world_catalog,
    world_search,
)
from .agents import Analysis
from .contracts import Assignment, Plan, Report, ROLE_NAMES, FACETS
from .db import DispatchDeferred, config, dump, enqueue, instant, later, node, one, require, rows, usage, utc


INTERIM_REPORT_AFTER_SECONDS = 120


def interim_deadline(c, rid, tasks):
    if not tasks:
        return None
    c.execute(
        "INSERT OR IGNORE INTO round_evidence_previews(round_id,due_at) VALUES(?,?)",
        (rid, later(INTERIM_REPORT_AFTER_SECONDS)),
    )
    row = one(c, "SELECT due_at,published_at FROM round_evidence_previews WHERE round_id=?", (rid,))
    if not row["published_at"]:
        remaining = datetime.fromisoformat(row["due_at"]) - datetime.fromisoformat(utc())
        return time.monotonic() + max(0, remaining.total_seconds())


def pending_handoffs(c, rid, visible):
    return [row[0] for row in c.execute(
        """SELECT DISTINCT '交接补查 · ' || owner.question FROM investigation_wakes w
           JOIN jobs j ON j.id=w.job_id JOIN research_tasks owner ON owner.id=w.owner_task
           WHERE w.round_id=? AND w.snapshot_id IS NULL AND (
             (w.task_id IS NULL AND j.status IN ('queued','running','deferred')) OR
             (w.task_id IS NOT NULL AND w.task_id NOT IN (SELECT value FROM json_each(?))))
           ORDER BY 1""", (rid, dump([t["id"] for t in visible]))
    )]


def publish_interim(c, r):
    """Freeze source excerpts independently of synthesis, without adopting claims or starting forecasts."""
    if not workflow.goal_matches(c, r):
        return False
    tasks = rows(c, "SELECT * FROM research_tasks WHERE round_id=? ORDER BY priority DESC,id", (r["id"],))
    tasks = claim_review.previously_returned(c, r, tasks)
    pending = [t["question"] for t in tasks if t["state"] != "done"] + pending_handoffs(c, r["id"], tasks)
    if not pending:
        return False
    excerpts = []
    for task in tasks:
        if task["state"] != "done" or not task["result"]:
            continue
        citations = [
            ref for finding in validated_task_report(c, task).findings
            if finding.kind in ("fact", "reported") for ref in finding.citations
        ]
        fragments = rows(
            c,
            """SELECT f.id,f.text,f.locator,a.document_id,d.title,d.url,
                      EXISTS(SELECT 1 FROM event_docs e WHERE e.document_id=d.id AND e.event_id=?) in_event
               FROM fragments f JOIN evidence_assets a ON a.id=f.asset_id JOIN documents d ON d.id=a.document_id
               WHERE f.id IN (SELECT value FROM json_each(?)) AND d.id IN (SELECT value FROM json_each(?))
               AND julianday(coalesce(nullif(d.published_at,''),d.observed_at))<=julianday(?)
               AND (nullif(d.source_updated_at,'') IS NULL OR julianday(d.source_updated_at)<=julianday(?))""",
            (r["event_id"], dump([ref.fragment_id for ref in citations]), task["input_ids"] or "[]",
             r["window_end"], r["window_end"]),
        )
        visible = {f["id"]: f for f in fragments}
        # Show a few excerpts from each completed line so one long filing cannot bury the others.
        seen = set()
        for ref in citations:
            f = visible.get(ref.fragment_id)
            quote = grounding.verbatim(f["text"], ref.quote) if f else None
            if not quote or (ref.fragment_id, quote) in seen:
                continue
            excerpts.append({
                "task_id": task["id"], "question": task["question"], "quote": quote, "fragment_id": ref.fragment_id,
                "document_id": f["document_id"], "locator": f["locator"], "title": f["title"],
                "in_event": bool(f["in_event"]), "url": f["url"] if f["url"].startswith(("https://", "http://")) else "",
            })
            seen.add((ref.fragment_id, quote))
            if len(seen) == 3:
                break
    if not excerpts:
        return False
    return bool(c.execute(
        """UPDATE round_evidence_previews SET published_at=?,excerpts=?,pending=?
           WHERE round_id=? AND published_at IS NULL""",
        (utc(), dump(excerpts), dump(pending), r["id"]),
    ).rowcount)


def drain_wakes(worker, r, deadline):
    """Lend the only dispatcher to queued handoffs, keeping the original delivery clock."""
    with worker.store.connection() as c:
        jobs = rows(c, """SELECT w.job_id FROM investigation_wakes w JOIN jobs j ON j.id=w.job_id
                        WHERE w.round_id=? AND j.status='queued' ORDER BY j.id""", (r["id"],))
    if not jobs or deadline is None:
        return
    with ThreadPoolExecutor(max_workers=1, thread_name_prefix="evosight-handoff") as borrowed:
        for wake in jobs:
            remaining = max(0.0, deadline - time.monotonic())
            if not remaining:
                with worker.store.connection(True) as c:
                    publish_interim(c, r)
                return  # The ordinary queue will run the remaining durable work.
            future = borrowed.submit(copy_context().run, worker.step, wake_job=wake["job_id"])
            done, _ = wait([future], timeout=remaining)
            if not done:
                with worker.store.connection(True) as c:
                    publish_interim(c, r)
            future.result()  # Leave the running wake's lease and receipts intact until completion.


def model_fragments(fragments):
    source_times = ("published_at", "source_updated_at")
    return [
        {
            k: (v[:2400] if k == "text" else v.split("#:~:text=", 1)[0] if k == "link" else v)
            for k, v in f.items()
            if k in ("id", "document_id", "text", "locator", "kind", "title", "link")
            or (k in source_times and v and f.get("source_times_checked"))
        }
        for f in fragments[:22]
    ]


def supported_time(proposed, quote):
    """Only source-visible dates and unambiguous offsets can place a fact in a six-hour window."""
    if not proposed or not quote:
        return ""
    dates = []
    for m in re.finditer(r"(?<!\d)(\d{4})[-/年](\d{1,2})[-/月](\d{1,2})(?:日|(?!\d))", quote):
        try:
            dates.append((datetime(*(int(x) for x in m.groups())).date().isoformat(), m.end()))
        except ValueError:
            continue
    months = r"Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|Jul(?:y)?|Aug(?:ust)?|Sep(?:tember)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?"
    for m in re.finditer(rf"\b({months})\.?\s+(\d{{1,2}}),?\s+(\d{{4}})\b", quote, re.I):
        try:
            dates.append(
                (datetime.strptime(f"{m[1][:3]} {m[2]} {m[3]}", "%b %d %Y").date().isoformat(), m.end())
            )
        except ValueError:
            continue
    if not dates:
        return ""
    try:
        guess = instant(proposed) if "T" in proposed else proposed
    except (ValueError, OverflowError):
        return ""
    for day, end in dates:
        if proposed == day:
            return day
        time_match = re.match(
            r"(?:[\s,T，]*(?:at\s+|北京时间\s*)?)(\d{1,2})(?:[:时点](\d{1,2}))(?:[:分](\d{2}))?(?:\.\d+)?\s*(AM|PM)?\s*(Z|(?:UTC|GMT)(?:[+-]\d{1,2}(?::?\d{2})?)?|[+-]\d{2}:?\d{2}|北京时间)?",
            quote[end:],
            re.I,
        )
        if not time_match:
            continue
        h, m, s, ampm, zone = time_match.groups()
        if not zone and "北京时间" in quote[max(0, end - 30) : end + 25]:
            zone = "北京时间"
        if not zone:
            continue
        offset = zone.upper().replace("北京时间", "+08:00").removeprefix("UTC").removeprefix("GMT")
        if offset in ("", "Z"):
            offset = "+00:00"
        elif re.fullmatch(r"[+-]\d{1,2}", offset):
            offset = f"{offset[0]}{int(offset[1:]):02d}:00"
        hour = int(h)
        if ampm:
            if not 1 <= hour <= 12:
                continue
            hour = hour % 12 + (12 if ampm.upper() == "PM" else 0)
        try:
            when = instant(f"{day}T{hour:02d}:{int(m):02d}:{int(s or '0'):02d}{offset}")
        except (ValueError, OverflowError):
            continue
        if guess == when:
            return when
    return dates[0][0] if len(dates) == 1 and dates[0][0] == proposed[:10] else ""


def check_report(report, fragments, context, role, *, rejections=None):
    exposed = set(context.get("archive_fragment_ids", []))
    visible = {f["id"]: f for i, f in enumerate(fragments) if i < 22 or f["id"] in exposed}
    old_ids = {i["id"] for i in context.get("prior", [])}
    known = {i["id"]: i for i in context.get("prior", [])}
    known.update(
        {i["version_id"]: i for i in context.get("world_candidates", []) if i.get("text_complete", True)}
    )
    known.update({i["version_id"]: i for i in context.get("world_records", [])})
    available = {(i["entry_id"], i["version_id"]) for i in context.get("world_records", [])}
    report.use_knowledge = validated_links(report.use_knowledge, available)
    observation_ids = {i["id"] for i in context.get("observations", [])}

    def validate_finding(finding):
        finding.facets = {
            k: (v[:500] if isinstance(v, str) else v) for k, v in finding.facets.items() if k in FACETS[role]
        }
        for field, domain in (("关联事件ID", "related"), ("预测ID", "predictions")):
            if field in finding.facets and finding.facets[field] not in {
                v["id"] for v in context.get(domain, [])
            }:
                finding.facets.pop(field)
        if role == "people" and ("动因假设" in finding.facets or "替代解释" in finding.facets):
            finding.kind = "hypothesis"
            if finding.facets.get("动因假设") and not all(finding.facets.get(k) for k in ("替代解释", "反证条件")):
                raise ValueError("人物动因假设须同时给出替代解释和反证条件")
        if role == "people" and finding.facets.get("资料性质") == "本人公开发帖":
            cited = {cite.fragment_id for cite in finding.citations}
            if not any(
                m["fragment_id"] in cited and m["attribution"] == "本人公开发帖"
                for t in context.get("people_watch", [])
                if t["name"] == finding.subject and t["identity"] == finding.identity
                for m in t["materials"]
            ):
                raise ValueError("没有已核实账号的原帖支持本人发言归属")
        category = world.classify(
            role, finding.section, finding.kind, finding.entity_kind, finding.category, finding.subject
        )
        if finding.supports in known:
            category = (
                known[finding.supports].get("schema_id")
                or known[finding.supports].get("category")
                or category
            )
        world_catalog.directory_for(category, finding.subject, finding.text, finding.directory)
        labels = world_catalog.SCHEMA_FIELDS[category]
        aliases = {label: key for key, label in labels.items()}
        unregistered = set()
        for key in list(finding.attributes):
            if key in labels:
                continue
            value = finding.attributes.pop(key)
            if key in aliases:
                canonical = aliases[key]
                if canonical in finding.attributes and finding.attributes[canonical] != value:
                    raise ValueError("同一结构化字段给出了不同值")
                finding.attributes[canonical] = value
            else:
                unregistered.add(key)
                if rejections is not None:
                    rejections.append((f"{finding.subject} · {key}", f"{category} 未注册此属性，保留有引文的正文"))
        for key in cards.CARD_FIELDS.intersection(finding.attributes):
            value = finding.attributes[key]
            if (
                key not in world_catalog.SCHEMA_FIELDS[category]
                or not isinstance(value, str)
                or not 1 <= len(value.strip()) <= 500
            ):
                finding.attributes.pop(key)
                if rejections is not None:
                    rejections.append((f"{finding.subject} · {key}", "资料卡字段不适用于当前类型或缺少有据的短文本"))
        world_catalog.attributes(category, finding.facets, finding.attributes)
        finding.citations = [grounding.locate(ref, visible) for ref in finding.citations]
        if unregistered and category == "indicators" and not finding.attributes and not re.search(r"\d", finding.text):
            raise ValueError(f"未注册属性 {', '.join(sorted(unregistered))}，指标正文也缺少可核对的原始读数")
        if (
            unregistered
            and finding.kind in ("fact", "reported", "correction")
            and not delivery.supported_quantities(finding.text, [ref.quote for ref in finding.citations])
        ):
            raise ValueError("正文中的数值无法用所引原文核实")
        # Field-level wording may be elsewhere in the same archived paragraph.
        fields = {}
        for field, citation in finding.field_citations.items():
            short = grounding.field_path(field).split(".")[-1]
            if short in unregistered:
                continue
            optional = next((k for k in cards.CARD_FIELDS if short in (k, labels.get(k))), None)
            try:
                key = grounding.field_name(field, finding, labels)
                citation = grounding.locate(citation, visible)
                if not any(parent.fragment_id == citation.fragment_id for parent in finding.citations):
                    source = visible[citation.fragment_id]
                    if (
                        key == "attributes.source"
                        and finding.attributes.get("source") == source.get("url")
                        and any(
                            visible[parent.fragment_id].get("document_id") == source.get("document_id")
                            for parent in finding.citations
                        )
                    ):
                        # A title in the same archived article can identify the source URL.
                        # The card writer will record the URL on its primary cited document.
                        continue
                    raise ValueError("字段依据不在本条引用的归档片段")
            except ValueError:
                if optional:
                    finding.attributes.pop(optional, None)
                    finding.facets.pop(labels.get(optional), None)
                    if rejections is not None:
                        rejections.append(
                            (f"{finding.subject} · {labels.get(optional,optional)}", "资料卡字段依据无法在本条引文中定位")
                        )
                    continue
                raise ValueError("字段引文须能在本条所引同一片段的归档原文中定位") from None
            fields[key] = citation
        finding.field_citations = fields
        if finding.replaces and finding.replaces not in old_ids:
            raise ValueError("不能订正输入快照以外的声明")
        if finding.supports:
            target = known.get(finding.supports)
            if (
                finding.replaces
                or not target
                or (finding.text and finding.text.strip() != target["text"].strip())
                or (finding.subject and finding.subject.strip() != target["subject"].strip())
            ):
                raise ValueError("补证请指向已展示的世界知识版本，省略 subject 和 text，填写有引文的新增字段。改变原结论请另交订正")
            finding.text, finding.subject = target["text"], target["subject"]
        if finding.occurred_at:
            text = " ".join(visible[c.fragment_id]["text"] for c in finding.citations)
            finding.occurred_at = (
                supported_time(finding.occurred_at, finding.time_quote) if finding.time_quote in text else ""
            )
        if "value" in finding.attributes and category == "indicators":
            quotes = " ".join(ref.quote.replace(",", "") for ref in finding.citations)
            numbers = re.findall(r"(?<![\d.])[-+]?\d+(?:\.\d+)?(?![\d.])", quotes)
            if not any(float(n) == finding.attributes["value"] for n in numbers):
                raise ValueError("指标数值需要对应引文中的原始数字，换算前先保留原口径")
            models = re.findall(
                r"(?i)\b[a-z]{1,8}\d+[a-z\d]*\b",
                " ".join((finding.subject, str(finding.attributes.get("measure", "")))),
                re.ASCII,
            )
            supported = " ".join(
                x.quote + " " + visible[x.fragment_id].get("title", "")
                for x in finding.citations
                if any(
                    float(n) == finding.attributes["value"]
                    for n in re.findall(r"(?<![\d.])[-+]?\d+(?:\.\d+)?(?![\d.])", x.quote.replace(",", ""))
                )
            )
            if models and not any(
                re.search(r"(?<![a-z0-9])" + re.escape(m) + r"(?![a-z0-9])", supported, re.I | re.ASCII)
                for m in models
            ):
                raise ValueError("指标数值的来源未对应所声明的对象或型号")

    unfamiliar = sorted(set(report.answered) - observation_ids)
    if unfamiliar:
        report.answered = [ident for ident in report.answered if ident in observation_ids]
        if rejections is not None:
            rejections.append(("观察项", "已忽略不属于本轮的观察项 ID " + "、".join(map(str, unfamiliar))))
    accepted, rejected = [], []
    for finding in report.findings:
        try:
            validate_finding(finding)
        except ValueError as error:
            subject = str(finding.attributes.get("measure") or finding.subject or "补证提议")[:120]
            rejected.append((subject, str(error)[:200]))
        else:
            accepted.append(finding)
    if rejected:
        if rejections is not None:
            rejections.extend(rejected)
        if not accepted:
            raise ValueError(rejected[0][1])
        report.findings = accepted
        # The report does not attribute answered observations to individual findings.
        report.answered = []
        gap = "本轮暂缓的研究提议。" + "。".join(f"{subject}（{reason}）" for subject, reason in dict.fromkeys(rejected))
        if len(report.gaps) < 8:
            report.gaps.append(gap)
        else:
            report.gaps[-1] += "。" + gap
    return report


def conflicting_support(c, finding):
    if not finding.supports or not finding.attributes:
        return ""
    stored = one(
        c,
        """SELECT w.head_id,d.attributes FROM world_versions v JOIN world_entries w ON w.id=v.entry_id
        JOIN world_record_data d ON d.version_id=w.head_id WHERE v.claim_id=?""",
        (finding.supports,),
    )
    old = cards.read(c, stored["head_id"], json.loads(stored["attributes"]))[0] if stored else {}
    changed = [k for k, v in finding.attributes.items() if k != "publisher" and k in old and old[k] != v]
    return f"版本 #{finding.supports} 的补证提议改变了已有字段 {'、'.join(changed)}，需另提有据订正" if changed else ""


def validated_task_report(c, task):
    report = Report.model_validate_json(task["result"])
    accepted = []
    for finding in report.findings:
        gap = conflicting_support(c, finding)
        if gap:
            report.gaps.append(gap)
        else:
            accepted.append(finding)
    report.findings = accepted
    return report


def claim(c, event, role, item, fragments, *, task_id=None):
    demo = require(c, "events", event)["demo"]
    cites = []
    for ref in item.citations:
        f = fragments[ref.fragment_id]
        cites.append(
            {
                "fragment_id": f["id"],
                "document_id": f["document_id"],
                "quote": ref.quote,
                "locator": f["locator"],
                "link": f["link"],
                "title": f["title"],
            }
        )
    entity = None
    if item.entity_kind and item.identity.strip():
        entity = world.resolve_entity(
            c, demo, item.entity_kind, item.subject, item.identity.strip(), item.aliases
        )
    predecessor = item.supports or item.replaces
    parent = world.version(c, predecessor) if predecessor else None
    if predecessor and not parent:
        raise ValueError("找不到被订正的世界知识版本")
    if parent and one(c, "SELECT 1 FROM world_dispositions WHERE entry_id=?", (parent["entry_id"],)):
        raise ValueError("该世界知识已归入事件、撤回或合并，请重新选择补证对象")
    if parent and parent["demo"] != demo:
        raise ValueError("演示资料与真实资料不能混用")
    if item.supports:
        head = require(c, "claims", parent["head_id"])
        if (item.text.strip(), item.subject.strip()) != (head["text"].strip(), head["subject"].strip()):
            return head["id"], False
        prior_cites = json.loads(head["citations"])
        known_quotes = {(c["document_id"], c["fragment_id"], c["quote"]) for c in prior_cites}
        new_cites = [v for v in cites if (v["document_id"], v["fragment_id"], v["quote"]) not in known_quotes]
        prior_record = one(c, "SELECT attributes FROM world_record_data WHERE version_id=?", (head["id"],))
        inherited, _ = cards.read(c, head["id"], json.loads(prior_record["attributes"]))
        if any(
            key != "publisher" and key in inherited and inherited[key] != value
            for key, value in item.attributes.items()
        ):
            raise ValueError("补证沿用原字段值，字段值改变时应提交有据订正")
        if not new_cites and not (item.attributes.keys() - inherited.keys()):
            return head["id"], False
        cites = [*prior_cites, *new_cites]
        predecessor = head["id"]
        item.text, item.subject = head["text"], head["subject"]
        parent = world.version(c, predecessor)
        entity = head["entity_id"]
        item.kind, item.occurred_at, item.section = head["kind"], head["occurred_at"] or "", head["section"]
        details = one(c, "SELECT facets,aliases FROM claim_details WHERE claim_id=?", (predecessor,))
        item.facets = json.loads(details["facets"]) if details else {}
        item.aliases = json.loads(details["aliases"]) if details else []
    refs = list(dict.fromkeys(f["document_id"] for f in cites))
    if parent and not entity:
        entity = require(c, "claims", predecessor)["entity_id"]
        aliases = one(c, "SELECT aliases FROM claim_details WHERE claim_id=?", (predecessor,))
        if aliases and not item.aliases:
            item.aliases = json.loads(aliases["aliases"])
    category = (
        parent["category"]
        if parent
        else world.classify(role, item.section, item.kind, item.entity_kind, item.category, item.subject)
    )
    attributes = dict(item.attributes)
    if item.supports:
        attributes = {**inherited, **attributes}
    fields = world_catalog.attributes(category, item.facets, attributes)
    verified, _ = cards.verify(
        c, cites, fields, [{"field": k, **v.model_dump()} for k, v in item.field_citations.items()]
    )
    if task_id is not None:
        c.executemany(
            "INSERT OR IGNORE INTO task_rejections VALUES(?,?,?)",
            [
                (
                    task_id,
                    f"{item.subject} · {world_catalog.SCHEMA_FIELDS[category].get(key,key)}",
                    "资料卡提议与归档依据不符",
                )
                for key in cards.CARD_FIELDS.intersection(fields)
                if fields[key] != verified.get(key)
            ],
        )
    fields = {**{k: v for k, v in fields.items() if k not in cards.CARD_FIELDS}, **verified}
    for key in cards.CARD_FIELDS:
        label = world_catalog.SCHEMA_FIELDS[category].get(key)
        if label in item.facets:
            item.facets.pop(label)
        if key not in verified:
            item.field_citations.pop("attributes." + key, None)
    derived = world_catalog.attributes(category, item.facets)
    identity = world.signature(
        event,
        item.kind,
        item.subject,
        item.text,
        item.occurred_at,
        entity,
        cites,
        item.facets,
        parent["canonical_id"] if parent else None,
        {k: v for k, v in fields.items() if k != "publisher" and (k not in derived or derived[k] != v)},
    )
    existing = world.matching(c, demo, identity)
    if existing:
        indexed = one(
            c,
            """SELECT 1 FROM snapshot_claims s JOIN world_versions v ON v.claim_id=s.claim_id
                            WHERE s.snapshot_id=(SELECT max(id) FROM knowledge_snapshots WHERE event_id=?) AND v.entry_id=?""",
            (event, existing["entry_id"]),
        )
        return existing["head_id"], not indexed and existing["canonical_id"] == existing["head_id"]
    if parent and parent["canonical_id"] != parent["head_id"]:
        if role == "user":
            raise workflow.VersionConflict("共享知识已有订正，请打开当前版本")
        return parent["head_id"], False
    precision = "instant" if "T" in item.occurred_at else "date" if item.occurred_at else "unknown"
    ident = c.execute(
        """INSERT INTO claims(event_id,role,section,kind,subject,text,occurred_at,precision,reason,refs,citations,entity_id,replaces,identity,created_at)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            event,
            role,
            item.section,
            item.kind,
            item.subject,
            item.text,
            item.occurred_at or None,
            precision,
            item.reason,
            dump(refs),
            dump(cites),
            entity,
            predecessor or None,
            identity,
            utc(),
        ),
    ).lastrowid
    c.execute("INSERT INTO claim_details VALUES(?,?,?)", (ident, dump(item.facets), dump(item.aliases)))
    world.register(c, ident, category=category, key=identity, path=item.directory, attributes=fields)
    if item.supports:
        c.execute(
            """INSERT OR IGNORE INTO field_evidence SELECT ?,field,fragment_id,quote
                     FROM field_evidence WHERE version_id=?""",
            (ident, predecessor),
        )
    c.executemany(
        "INSERT OR IGNORE INTO field_evidence VALUES(?,?,?,?)",
        [(ident, key, ref.fragment_id, ref.quote) for key, ref in item.field_citations.items()],
    )
    cards.refresh(c, ident)
    world_search.index_head(c, world.version(c, ident)["entry_id"])
    return ident, True


def study_context(c, r):
    event = r["event_id"]
    e = require(c, "events", event)
    prior = workflow.snapshot_claims(c, r["parent_snapshot"]) if r["parent_snapshot"] else []
    hidden = {row[0] for row in c.execute("SELECT entry_id FROM world_dispositions")}
    prior = [k for k in prior if k["world_id"] not in hidden]
    goal = workflow.current_goal(c, event)
    objects = rows(
        c, "SELECT id,title,scope FROM events WHERE id!=? AND status!='deleted' AND (demo=0 OR ?=1) LIMIT 36",
        (event, e["demo"]),
    )
    suggestions = world.suggested(c, event)
    routes = world_catalog.route(e["title"] + " " + e["scope"])
    paths = {p for w in suggestions for p in world_catalog.prefixes(w["path"])}
    paths.update(p for route in routes for p in world_catalog.prefixes(route["path"]))
    return {
        "事件": e["title"],
        "事件黑板": board.view(c, event, goal["version"]),
        "此前判断": rows(
            c,
            "SELECT title,body,refs FROM nodes WHERE event_id=? AND kind='judgment' ORDER BY id DESC LIMIT 3",
            (event,),
        ),
        "研究范围": e["scope"],
        "此前交付清单": delivery.view(c, r["parent_snapshot"]) if r["parent_snapshot"] else [],
        "priority": goal["priority"],
        "goal_version": r["goal_version"],
        "window": [r["window_start"], r["window_end"]],
        "reason": json.loads(r["reasons"]),
        "prior": [
            {
                k: v[k]
                for k in (
                    "id",
                    "world_id",
                    "world_version",
                    "category",
                    "subject",
                    "text",
                    "kind",
                    "section",
                    "index_reason",
                )
            }
            for v in prior[:18]
        ],
        "world_candidates": suggestions,
        "people_watch": people.public_context(c, r),
        "collection_scope": [
            {"kind": s["kind"], "target": s["target"], "angle": s["angle"]} for s in focus.applicable(c, r)
        ],
        "catalog": {
            "types": world_catalog.CATEGORIES,
            "routes": routes,
            "schemas": world_catalog.compact_schemas(route["schema_id"] for route in routes),
            "paths": {p: world_catalog.PATHS[p][0] for p in sorted(paths)},
        },
        "pinned": rows(c, "SELECT id,name FROM items WHERE event_id=? AND pinned=1", (event,)),
        "observations": rows(
            c,
            "SELECT id,origin,question,condition,answer,prediction_id,due_at FROM observations WHERE event_id=? AND state='open' ORDER BY priority DESC,id LIMIT 14",
            (event,),
        ),
        "predictions": rows(
            c,
            "SELECT id,question,rule,authority,deadline FROM predictions WHERE event_id=? AND status IN ('open','frozen','review')",
            (event,),
        ),
        "related": [{**o, "user_linked": o["id"] in goal["related"]} for o in objects],
        "候选平台": [{"url": s.url, "name": s.name, "reason": s.reason} for s in watch.sources(c, event)],
        "gaps": json.loads(require(c, "knowledge_snapshots", r["parent_snapshot"])["gaps"])
        if r["parent_snapshot"]
        else [],
        "budget": {
            **usage(c),
            **{k: v for k, v in config(c).items() if k in ("daily_calls", "monthly_calls", "verify_reserve")},
        },
    }


def poll_sources(worker, r, sources, verified=None):
    event = r["event_id"]
    reasons = json.loads(r["reasons"])
    fresh = any(v in reasons for v in ("手动刷新", "恢复追踪", "初始化"))
    results = {}
    for source in sources:
        checked = (verified or {}).get(source.url)
        ok = bool(checked) or worker.watcher.poll(source, fresh=fresh)
        with worker.store.connection(True) as c:
            if not workflow.goal_matches(c, r):
                return None
            c.execute(
                """INSERT INTO round_sources(round_id,url,window_start,window_end,state,checked_at)
                         VALUES(?,?,?,?,?,?) ON CONFLICT(round_id,url) DO UPDATE SET state=excluded.state,checked_at=excluded.checked_at""",
                (
                    r["id"],
                    source.url,
                    r["window_start"],
                    r["window_end"],
                    "covered" if ok else "missing",
                    checked or utc(),
                ),
            )
            results[source.url] = (ok, watch.take_entries(c, event, source, r, ok, from_archive=bool(checked)))
            if ok and "补采缺失窗口" in reasons:
                c.execute(
                    """UPDATE round_sources SET state='recovered',recovery_round=?
                       WHERE url=? AND state='missing' AND window_start=? AND window_end=?
                         AND round_id IN (SELECT id FROM tracking_rounds WHERE event_id=? AND goal_version=?
                                          AND state!='superseded' AND id<?)""",
                    (r["id"], source.url, r["window_start"], r["window_end"], event, r["goal_version"], r["id"]),
                )
    return results


def prepare_plan(c, r, ids, detail):
    c.execute(
        "UPDATE tracking_rounds SET state='planning',input_docs=?,detail=? WHERE id=?",
        (dump(ids), detail, r["id"]),
    )
    for ident in workflow.affected_predictions(c, r["event_id"], ids):
        c.execute("INSERT OR IGNORE INTO round_prediction_gates VALUES(?,?,'checking')", (r["id"], ident))
    return ids


def acquire_source_repair(worker, r):
    event = r["event_id"]
    with worker.store.connection() as c:
        gaps = rows(
            c,
            """SELECT g.url,max(g.checked_at) AS missed_at,p.success_at FROM round_sources g
               JOIN platforms p ON p.url=g.url WHERE g.recovery_round=? GROUP BY g.url,p.success_at""",
            (r["id"],),
        )
        targets = {g["url"] for g in gaps}
        sources = [s for s in watch.sources(c, event) if s.url in targets]
    # A successful poll commits the publisher records before updating success_at. Replay that read
    # even if the publisher goes offline again while the repair waits in the queue.
    verified = {
        g["url"]: g["success_at"]
        for g in gaps
        if g["success_at"] and g["success_at"] > g["missed_at"] and g["success_at"] >= r["window_end"]
    }
    result = poll_sources(worker, r, sources, verified)
    if result is None:
        return None
    with worker.store.connection(True) as c:
        if not workflow.goal_matches(c, r):
            return None
        materials.feeds.release_future(c, event, r["window_end"])
        ids = [
            row[0] for row in c.execute(
                """SELECT DISTINCT d.id FROM round_sources s JOIN platform_entries pe ON pe.platform=s.url
                   JOIN documents d ON d.id=pe.document_id JOIN event_docs ed ON ed.document_id=d.id
                   WHERE s.round_id=? AND s.state='covered' AND ed.event_id=? AND ed.analyzed=0
                     AND ed.intake NOT IN ('person_candidate','future')
                     AND max(coalesce(d.published_at,''),coalesce(d.source_updated_at,''))>?
                     AND max(coalesce(d.published_at,''),coalesce(d.source_updated_at,''))<=?
                   ORDER BY d.id DESC LIMIT 12""",
                (r["id"], event, r["window_start"], r["window_end"]),
            )
        ]
        covered = sum(ok for ok, _ in result.values())
        checked = f"已补查 {covered}/{len(targets)} 个缺失来源"
        if ids:
            return prepare_plan(c, r, ids, checked)
        complete = bool(targets) and covered == len(targets)
        detail = checked + ("，窗口内没有新的待分析材料" if complete else "，仍有来源待恢复")
        c.execute(
            "UPDATE tracking_rounds SET state=?,detail=?,finished_at=? WHERE id=?",
            ("unchanged" if complete else "waiting", detail, utc(), r["id"]),
        )
    return None


def acquire(worker, r, job):
    event = r["event_id"]
    if r["parent_snapshot"] and json.loads(r["reasons"]) == ["补采缺失窗口"]:
        return acquire_source_repair(worker, r)
    person_docs = people.poll_known(worker, r)
    person_seeds = people.seed(worker, r)

    def scout(ctx):
        return worker.call(job, "source_scout", {**ctx, "window": [r["window_start"], r["window_end"]]})[0]

    seeds = watch.choose(
        worker.store,
        worker.acquisition,
        event,
        r["id"],
        refresh="目标更新" in json.loads(r["reasons"]),
        scout=scout,
    )
    sources = poll_sources(worker, r, seeds)
    if sources is None:
        return None
    focus_docs = focus.collect(worker, r)
    ranked = [*focus_docs, *person_docs, *person_seeds]
    if sources and not r["parent_snapshot"] and not ranked and not any(docs for _, docs in sources.values()):
        with worker.store.connection() as c:
            has_material = one(
                c,
                "SELECT 1 FROM event_docs WHERE event_id=? AND intake NOT IN ('future','person_candidate') LIMIT 1",
                (event,),
            )
            already_scouted = one(
                c, "SELECT 1 FROM runs WHERE job_id=? AND role='source_scout' LIMIT 1", (job["id"],)
            )
        if not has_material and not already_scouted:
            seeds = watch.choose(
                worker.store, worker.acquisition, event, r["id"], scout=scout, empty_sources=tuple(sources)
            )
            extra = poll_sources(worker, r, [s for s in seeds if s.url not in sources])
            if extra is None:
                return None
            sources.update(extra)
    ok_count = sum(ok for ok, _ in sources.values())
    ranked.extend(
        doc
        for group in zip_longest(*(docs for _, docs in sources.values()))
        for doc in group
        if doc is not None
    )
    with worker.store.connection(True) as c:
        now = utc()
        c.execute("UPDATE events SET last_checked=? WHERE id=?", (now, event))
        materials.feeds.release_future(c, event, r["window_end"])
        pending = rows(
            c,
            """SELECT d.id FROM event_docs ed JOIN documents d ON d.id=ed.document_id
                          WHERE ed.event_id=? AND ed.analyzed=0 AND ed.intake NOT IN ('future','archive','person_candidate')
                          ORDER BY CASE ed.intake WHEN 'revision' THEN 0 WHEN 'window' THEN 1 ELSE 2 END,d.id DESC""",
            (event,),
        )
        eligible = {d["id"] for d in pending}
        triggers = [
            i[0] for i in c.execute("SELECT document_id FROM watch_signals WHERE round_id=?", (r["id"],))
        ]
        ids = list(
            dict.fromkeys(
                i
                for i in [*triggers, *ranked, *[x["id"] for x in pending]]
                if i in eligible or i in person_docs or i in focus_docs
            )
        )[:12]
        if not r["parent_snapshot"] and len(eligible) > len(ids):
            c.executemany(
                "UPDATE event_docs SET intake='archive' WHERE event_id=? AND document_id=? AND analyzed=0",
                [(event, i) for i in eligible - set(ids)],
            )
        reasons = json.loads(r["reasons"])
        due = one(
            c, "SELECT 1 FROM observations WHERE event_id=? AND due_at<=? AND state='open'", (event, now)
        )
        if not ids and (
            not r["parent_snapshot"]
            or due
            or steering.pending(c, r["id"])
            or any(x in reasons for x in ("目标更新", "恢复追踪", "专项补查", "人物专项", "用户订正"))
        ):
            ids = [
                x[0]
                for x in c.execute(
                    "SELECT document_id FROM event_docs WHERE event_id=? AND intake NOT IN ('future','person_candidate') ORDER BY document_id DESC LIMIT 8",
                    (event,),
                )
            ]
        if not ids and "补采缺失窗口" in reasons:
            ids = [
                x[0]
                for x in c.execute(
                    """SELECT d.id FROM documents d JOIN event_docs ed ON ed.document_id=d.id
                        WHERE ed.event_id=? AND ed.intake!='person_candidate'
                        AND max(coalesce(d.published_at,''),coalesce(d.source_updated_at,''))>?
                        AND max(coalesce(d.published_at,''),coalesce(d.source_updated_at,''))<=? ORDER BY d.id DESC LIMIT 12""",
                    (event, r["window_start"], r["window_end"]),
                )
            ]
        if not ids and (not r["parent_snapshot"] or "目标更新" in reasons):
            candidates = world.suggested(c, event)
            ids = list(dict.fromkeys(d for w in candidates for d in w["refs"]))[:8]
            if ids:
                c.execute("UPDATE tracking_rounds SET world_seed_only=1 WHERE id=?", (r["id"],))
        if not ids:
            covered = one(
                c,
                """SELECT 1 FROM person_checks WHERE round_id=? AND status IN ('found','checked','partial')
                   UNION ALL SELECT 1 FROM focus_checks WHERE round_id=? AND status IN ('found','checked') LIMIT 1""",
                (r["id"], r["id"]),
            )
            state = "waiting" if not (ok_count or covered) or not r["parent_snapshot"] else "unchanged"
            msg = "待补来源或相关材料，可调整范围或重试" if state == "waiting" else "已检查，暂无实质变化"
            if state == "waiting" and not seeds:
                msg = "查源未取得可核实的发布入口。详见来源发现记录，可重试本轮。"
            elif state == "waiting" and seeds and not ok_count:
                msg = f"本轮尝试的 {len(seeds)} 个发布入口均未能读取。详见来源检查记录。"
            elif state == "waiting" and ok_count:
                msg = f"已读取 {ok_count} 个来源，未取得本轮可用的相关材料。详见找源记录。"
            c.execute(
                "UPDATE tracking_rounds SET state=?,detail=?,finished_at=? WHERE id=?",
                (state, msg, now, r["id"]),
            )
            return None
        return prepare_plan(c, r, ids, f"已核查 {ok_count}/{len(seeds)} 个来源")


ROLE_TYPES = {
    "news": ("events", "policies", "indicators"),
    "people": ("people", "analysis"),
    "organizations": ("organizations", "places", "policies"),
    "opinion": ("opinion",),
    "related": ("relations",),
    "dependencies": ("relations", "indicators"),
    "counter": ("analysis",),
    "specialist": ("background", "indicators"),
}


def task_knowledge(c, r, task, assignment, plan):
    saved = one(c, "SELECT payload FROM world_task_reads WHERE task_id=?", (task["id"],))
    if saved:
        return json.loads(saved["payload"])
    demo = require(c, "events", r["event_id"])["demo"]
    query = assignment.get("knowledge_query") or {}
    keys = []
    if query.get("query") or query.get("path") or query.get("entity_id") or query.get("entry_id"):
        hits, _ = world_search.search(
            c,
            query.get("query", ""),
            path=query.get("path", ""),
            entity_id=query.get("entity_id"),
            entry_id=query.get("entry_id"),
            qualifiers=query.get("qualifiers"),
            event=r["event_id"],
            demo=demo,
            limit=3,
        )
        keys.extend((h["entry_id"], h["version_id"]) for h in hits)
    keys.extend((h["entry_id"], h["version_id"]) for h in plan.get("use_knowledge", []))
    requests = [
        {"entry_id": entry, "version_id": version} for entry, version in list(dict.fromkeys(keys))[:4]
    ]
    records = [
        w
        for w in world_search.read_records(c, requests, query.get("fields", []), demo=demo)
        if w["status"] == "ok"
    ]
    categories = list(dict.fromkeys([*(w["schema_id"] for w in records), *ROLE_TYPES[task["role"]]]))
    data = {
        "world_records": records,
        "record_schemas": world_catalog.compact_schemas(categories),
        "record_paths": {
            p: label for p, (label, _) in world_catalog.PATHS.items() if p.partition("/")[0] in categories
        },
    }
    c.execute("INSERT INTO world_task_reads VALUES(?,?)", (task["id"], dump(data)))
    return data


def prepare_task(worker, r, t, assignment, context, ids):
    if t["input_ids"] is not None:
        return json.loads(t["input_ids"])
    extra = people.task_documents(worker, r) if t["role"] == "people" else []
    if assignment.get("search_query"):
        try:
            query = assignment["search_query"]
            if t["time_scope"] == "window":
                last_day = (datetime.fromisoformat(r["window_end"]).date() + timedelta(days=1)).isoformat()
                query += f" after:{r['window_start'][:10]} before:{last_day}"
            found = worker.acquisition.targeted(query)
        except Exception:
            found = []
        for entry, res, ex in found:
            if ex.published_at and ex.published_at > r["window_end"]:
                continue
            if t["role"] == "people":
                from urllib.parse import urlsplit
                from .person_sources import HOSTS

                if urlsplit(res.url).hostname in HOSTS or (
                    t["time_scope"] == "window"
                    and (not ex.published_at or not r["window_start"] < ex.published_at <= r["window_end"])
                ):
                    continue
            with worker.store.connection(True) as c:
                doc = materials.attach_search_result(
                    c,
                    r["event_id"],
                    entry,
                    res,
                    ex,
                    intake="backfill" if t["time_scope"] == "history" else "undated",
                )
                extra.append(doc)
    all_ids = list(dict.fromkeys([*extra, *ids]))
    with worker.store.connection(True) as c:
        c.execute("UPDATE research_tasks SET input_ids=? WHERE id=?", (dump(all_ids), t["id"]))
    return all_ids


def save_plan(c, r, plan):
    steering.complete_assignments(c, r, plan)
    board.save_guides(c, r, plan.model_dump())
    c.execute(
        "UPDATE tracking_rounds SET plan=?,state='researching' WHERE id=?", (dump(plan.model_dump()), r["id"])
    )
    for task in plan.tasks:
        historical = (
            not r["parent_snapshot"]
            or task.role in ("specialist", "counter")
            or task.role == "people"
            and "人物专项" in json.loads(r["reasons"])
        )
        c.execute(
            """INSERT OR IGNORE INTO research_tasks(round_id,role,question,target,time_scope,done_when,priority)
                  VALUES(?,?,?,?,?,?,?)""",
            (
                r["id"],
                task.role,
                task.question,
                task.target,
                task.time_scope if historical else "window",
                task.done_when,
                task.priority,
            ),
        )
        tid = one(
            c,
            "SELECT id FROM research_tasks WHERE round_id=? AND role=? AND question=?",
            (r["id"], task.role, task.question),
        )["id"]
        c.execute(
            """INSERT INTO investigation_lines(event_id,line_key,goal_version,question,role,last_task) VALUES(?,?,?,?,?,?)
                  ON CONFLICT(event_id,line_key) DO UPDATE SET goal_version=excluded.goal_version,question=excluded.question,
                  role=excluded.role,last_task=excluded.last_task""",
            (
                r["event_id"],
                task.line or f"{task.role}/{task.target or task.question}",
                r["goal_version"],
                task.question,
                task.role,
                tid,
            ),
        )
    steering.bind_plan(c, r, plan)
    steering.ask(c, r, "main", "", plan.user_question)


def validated_links(proposals, available):
    accepted = []
    for link in proposals:
        key = (link.entry_id, link.version_id)
        if key in available:
            accepted.append(link)
            available.remove(key)
    return accepted


def role_lookup(worker, r, task, request):
    event, query = r["event_id"], request.query
    if request.tool == "web":
        docs = []
        for entry, response, extracted in worker.acquisition.targeted(query)[:4]:
            if extracted.published_at and extracted.published_at > r["window_end"]:
                continue
            with worker.store.connection(True) as c:
                docs.append(
                    materials.attach_search_result(c, event, entry, response, extracted, intake="backfill")
                )
        return {"documents": docs}
    with worker.store.connection(True) as c:
        if request.tool == "archive":
            return {"documents": board.archive_search(c, event, query, r["window_end"])}
        if request.tool == "world":
            hits, _ = world_search.search(
                c, query, event=event, demo=require(c, "events", event)["demo"], limit=3
            )
            records = world_search.read_records(
                c, hits, evidence=True, demo=require(c, "events", event)["demo"]
            )
            return {
                "world_records": records,
                "documents": list(dict.fromkeys(d for rec in records for d in rec.get("refs", []))),
            }
        permitted = wakes.permitted_documents(c, r, request.document_ids)
        if request.tool == "read":
            return {"documents": permitted}
        recipient = request.recipient or "主 Agent"
        if recipient != "主 Agent":
            matches = rows(
                c,
                """SELECT line_key FROM investigation_lines WHERE event_id=? AND goal_version=?
                               AND (line_key=? OR role=?)""",
                (event, r["goal_version"], recipient, recipient),
            )
            if len(matches) != 1:
                return {"error": "请从黑板选择一条现有调查线，或交给主 Agent"}
            recipient = matches[0]["line_key"]
        ident = wakes.post(c, r, task["id"], recipient, task["question"], query, permitted)
        return {"recipient": recipient, "message_id": ident, "documents": permitted}


def run_role(worker, job, r, plan, ctx, ids, t):
    """Read a role's pinned inputs and persist its independently validated report."""
    budget_error = ""
    with worker.store.connection(True) as c:
        if not workflow.goal_matches(c, r):
            return ""
        c.execute("UPDATE research_tasks SET state='running' WHERE id=?", (t["id"],))
    assignment = next(a for a in plan["tasks"] if a["role"] == t["role"] and a["question"] == t["question"])
    try:
        with worker.store.connection(True) as c:
            read_context = task_knowledge(c, r, t, assignment, plan)
        role_context = {**ctx, **read_context}
        historical_docs = [doc for rec in read_context["world_records"] for doc in rec["refs"]]
        inputs = prepare_task(
            worker, r, t, assignment, role_context, list(dict.fromkeys([*ids, *historical_docs]))
        )
        with worker.store.connection() as c:
            pinned = role_context.get("交接原文片段")
            frags = (wakes.frozen_fragments(c, pinned) if pinned is not None
                     else materials.fragments_for(c, inputs, per_document=None))
            if t["role"] == "people":
                role_context["people_watch"] = people.public_context(c, r)
        feedback = {"上次校验反馈": t["detail"] + "。请逐字复制输入引文，材料不足时留下缺口。"} if t["detail"] else {}
        history = []
        for step in range(3):
            role_context["archive_fragment_ids"] = [f["id"] for f in frags]
            with worker.store.connection(True) as c:
                previous = one(
                    c,
                    "SELECT request,result FROM research_tool_steps WHERE task_id=? AND step=?",
                    (t["id"], step),
                )
                if job["kind"] != "investigation_wake":
                    role_context["事件黑板"] = board.view(c, r["event_id"], r["goal_version"])
                assigned = steering.receive(c, r, t["id"], job["id"])
                if assigned:
                    role_context["用户调查要求"] = assigned
            if previous:
                request, response = json.loads(previous["request"]), json.loads(previous["result"])
            else:
                result, _ = worker.call(
                    job,
                    t["role"],
                    {
                        **role_context,
                        **feedback,
                        "task": {**assignment, "time_scope": t["time_scope"]},
                        "工具剩余次数": 2 - step,
                        "工具回执": history,
                        "fragments": model_fragments(frags),
                    },
                )
                if result.user_question.strip():
                    line = assignment.get("line") or f"{t['role']}/{t['target'] or assignment['question']}"
                    with worker.store.connection(True) as c:
                        steering.ask(c, r, t["role"], line, result.user_question)
                if step == 2 and result.lookup:
                    pending = f"本轮工具额度已用完，留待下次查证“{result.lookup.query}”。"
                    if len(result.gaps) < 8:
                        result.gaps.append(pending)
                    else:
                        result.gaps[-1] += "。" + pending
                if not result.lookup or step == 2:
                    result.lookup = None
                    break
                request = result.lookup.model_dump()
                response = role_lookup(worker, r, t, result.lookup)
                with worker.store.connection(True) as c:
                    if pinned is not None:
                        response = wakes.freeze_lookup(c, r, response, frags)
                    c.execute(
                        "INSERT OR IGNORE INTO research_tool_steps VALUES(?,?,?,?)",
                        (t["id"], step, dump(request), dump(response)),
                    )
            history.append({"request": request, "result": response})
            inputs = list(dict.fromkeys([*response.get("documents", []), *inputs]))
            role_context["world_records"] = [
                *role_context["world_records"],
                *response.get("world_records", []),
            ]
            with worker.store.connection(True) as c:
                c.execute("UPDATE research_tasks SET input_ids=? WHERE id=?", (dump(inputs), t["id"]))
                if pinned is None:
                    frags = materials.fragments_for(c, inputs, per_document=None)
                else:
                    pinned = list(dict.fromkeys([*pinned, *response.get("fragment_ids", [])]))
                    frags = wakes.frozen_fragments(c, pinned)
        submitted = len(result.findings)
        rejects = []
        try:
            check_report(result, frags, role_context, t["role"], rejections=rejects)
        finally:
            if rejects:
                with worker.store.connection(True) as c:
                    c.executemany(
                        "INSERT OR IGNORE INTO task_rejections VALUES(?,?,?)",
                        [(t["id"], subject, reason) for subject, reason in rejects],
                    )
        count = len(result.findings)
        detail = f"{count} 项通过核查，{submitted-count} 项理由见知识缺口。" if count < submitted else ""
        with worker.store.connection(True) as c:
            c.execute(
                "UPDATE research_tasks SET state='done',result=?,detail=? WHERE id=?",
                (dump(result.model_dump()), detail, t["id"]),
            )
    except resources.ResourcePressure:
        with worker.store.connection(True) as c:
            c.execute("UPDATE research_tasks SET state='queued' WHERE id=? AND state='running'", (t["id"],))
        raise
    except (ValueError, RuntimeError, OSError) as error:
        budget_error = str(error) if isinstance(error, DispatchDeferred) else ""
        with worker.store.connection(True) as c:
            c.execute(
                "UPDATE research_tasks SET state=?,detail=? WHERE id=?",
                ("deferred" if budget_error else "failed", str(error)[:250], t["id"]),
            )
    return budget_error


def run_guide(worker, job, args, candidates):
    with worker.store.connection(True) as c:
        event = job["event_id"]
        g = require(c, "event_guides", args["guide_id"])
        if (
            g["event_id"] != event
            or not g["active"]
            or g["goal_version"] != workflow.current_goal(c, event)["version"]
        ):
            return "查阅指南已更新"
        query = json.loads(g["query"])
        if "version_id" in args:
            vid = args["version_id"]
            rec = world.version(c, vid)
            if (
                not rec
                or rec["head_id"] != vid
                or not board.guide_matches(c, event, query, entry=rec["entry_id"])
            ):
                return "候选已更新"
            ids = json.loads(require(c, "claims", vid)["refs"])
            query["entry_id"] = rec["entry_id"]
        else:
            ids = [args["document_id"]]
        c.executemany(
            "INSERT OR IGNORE INTO event_docs(event_id,document_id,intake) VALUES(?,?,'backfill')",
            [(event, doc) for doc in ids],
        )
        key = f"guide/{event}/{g['goal_version']}/{args['dispatch_key']}"
        r = one(c, "SELECT * FROM tracking_rounds WHERE request_key=?", (key,))
        if not r:
            last = workflow.latest(c, event)
            now = utc()
            rid = c.execute(
                """INSERT INTO tracking_rounds(event_id,goal_version,request_key,reasons,window_start,window_end,
                          created_at,parent_snapshot,input_docs,job_id) VALUES(?,?,?,?,?,?,?,?,?,?)""",
                (
                    event,
                    g["goal_version"],
                    key,
                    dump(["指南定向核查"]),
                    now,
                    now,
                    now,
                    last["id"] if last else None,
                    dump(ids),
                    job["id"],
                ),
            ).lastrowid
            r = require(c, "tracking_rounds", rid)
            task = Assignment(
                role="specialist",
                line=f"guide-{g['id']}",
                question=g["question"],
                target=g["reason"],
                time_scope="history",
                done_when=g["wake"],
                priority=4,
                knowledge_query=query,
            )
            save_plan(c, r, Plan(rationale=g["reason"], tasks=[task]))
    return run_round(worker, job, r["id"], candidates)


def run_round(worker, job, rid, candidates):
    with worker.store.connection(True) as c:
        if not rid:
            event = job["event_id"]
            retry = one(
                c,
                """SELECT id FROM tracking_rounds WHERE event_id=? AND goal_version=?
                      AND state IN ('failed','deferred') AND input_docs IS NOT NULL ORDER BY id DESC LIMIT 1""",
                (event, workflow.current_goal(c, event)["version"]),
            )
            if retry:
                rid = retry["id"]
                c.execute("UPDATE tracking_rounds SET state='queued',job_id=? WHERE id=?", (job["id"], rid))
                c.execute(
                    "UPDATE research_tasks SET state='queued' WHERE round_id=? AND state IN ('failed','deferred')",
                    (rid,),
                )
            else:
                rid, _ = workflow.request_round(c, event, "手动刷新", key=f"job/{job['id']}", job_id=job["id"])
            c.execute("UPDATE jobs SET args=? WHERE id=?", (dump({"round_id": rid}), job["id"]))
            job["args"] = dump({"round_id": rid})
        r = require(c, "tracking_rounds", rid)
        if not workflow.goal_matches(c, r):
            return "研究目标已更新"
        retry_positions = json.loads(job["args"]).get("review_positions", []) if job["kind"] == "research_retry" else []
        if retry_positions:
            latest = workflow.latest(c, r["event_id"])
            report = delivery.latest_research_snapshot(c, latest["id"]) if latest else None
            same_round = report and require(c, "knowledge_snapshots", report)["round_id"] == rid
            open_checks = delivery.view(c, report) if same_round else []
            if not any(row["position"] in retry_positions and row["state"] == "unknown" and
                       row["answer"].startswith("独立复核待补：") for row in open_checks):
                return "该轮关键项已复核或已有新版本"
        if r["state"] in ("published", "unchanged", "superseded"):
            if not retry_positions and not (job["kind"] == "handoff_publish" and wakes.unpublished(c, rid)):
                return r["detail"]
        elif job["kind"] != "handoff_publish" and "补采缺失窗口" not in json.loads(r["reasons"]):
            steering.recover_unread(c, r)
    ids = json.loads(r["input_docs"]) if r["input_docs"] is not None else acquire(worker, r, job)
    if not ids:
        with worker.store.connection() as c:
            return require(c, "tracking_rounds", rid)["detail"]
    with worker.store.connection() as c:
        docs = [require(c, "documents", i) for i in ids]
    if job["kind"] != "handoff_publish":
        for d in docs:
            materials.enrich(worker.store, worker.acquisition, d)
    with worker.store.connection(True) as c:
        r = require(c, "tracking_rounds", rid)
        if not workflow.goal_matches(c, r):
            return "事件索引或目标已更新"
        ctx = json.loads(r["context"]) if r["context"] else study_context(c, r)
        if not r["context"]:
            c.execute("UPDATE tracking_rounds SET context=? WHERE id=?", (dump(ctx), rid))
        fragments = materials.fragments_for(c, ids)
    if not r["plan"]:
        with worker.store.connection() as c:
            inputs = steering.for_plan(c, r)
        lead_fragments = people.lead_fragments(
            fragments, [s["target"] for s in ctx.get("collection_scope", []) if s["kind"] == "person"]
        )
        plan, _ = worker.call(
            job,
            "plan",
            {
                **ctx,
                "evidence": [{"id": d["id"], "title": d["title"]} for d in docs],
                "fragments": model_fragments(fragments)[:12],
                "people_evidence": lead_fragments,
                **({"用户调查要求": inputs} if inputs else {}),
                **({"上次校验反馈": job["info"]} if job.get("info", "").startswith("模型输出未通过结构校验") else {}),
            },
        )
        with worker.store.connection(True) as c:
            if not workflow.goal_matches(c, r):
                return "研究目标已更新"
            available = {(w["entry_id"], w["version_id"]) for w in ctx.get("world_candidates", [])}
            plan.use_knowledge = validated_links(plan.use_knowledge, available)
            indexed = {(w.get("world_id"), w["id"]) for w in ctx.get("prior", [])}
            plan.release_knowledge = validated_links(plan.release_knowledge, indexed)
            visible = {f["id"]: f for f in [*model_fragments(fragments)[:12], *lead_fragments]}
            approved = [p for p in plan.person_leads if people.register(c, r, p, visible)]
            if len(approved) < len(plan.person_leads):
                plan.postponed.append("部分人物线索缺少点名的原文，待补身份依据")
            plan.person_leads = approved
            focus.match_people(c, r, people.roster(c, r["event_id"]))
            fresh_people = one(
                c,
                """SELECT 1 FROM person_materials WHERE round_id=? AND attribution!='账号归属待核'
                   UNION ALL SELECT 1 FROM focus_checks k JOIN event_focus f ON f.id=k.focus_id
                   WHERE k.round_id=? AND f.kind='person' AND k.status='found' LIMIT 1""",
                (r["id"], r["id"]),
            )
            if (approved or fresh_people) and not any(t.role == "people" for t in plan.tasks):
                if len(plan.tasks) == 8:
                    roles = [task.role for task in plan.tasks]
                    repeated = [task for task in plan.tasks if roles.count(task.role) > 1]
                    postponed = min(repeated, key=lambda task: task.priority)
                    plan.tasks.remove(postponed)
                    plan.postponed.append(f"本轮优先核实人物，延后同角色的另一项任务：{postponed.question}")
                plan.tasks.append(
                    Assignment(
                        role="people",
                        question="哪些相关人物的公开立场、行动或先前判断有新依据？",
                        target="本轮已核实的相关人物",
                        time_scope="window" if r["parent_snapshot"] else "history",
                        done_when="每项变化附直接材料，区分本人、转述、过往资料及待核线索",
                        priority=4,
                    )
                )
            save_plan(c, r, plan)
            r["plan"] = dump(plan.model_dump())
    plan = json.loads(r["plan"])
    reused_docs = {
        d
        for link in plan.get("use_knowledge", [])
        for w in ctx.get("world_candidates", [])
        if (link["entry_id"], link["version_id"]) == (w["entry_id"], w["version_id"])
        for d in w["refs"]
    }
    if r["world_seed_only"] and not reused_docs:
        with worker.store.connection(True) as c:
            tasks = rows(c, """SELECT id,role,question FROM research_tasks t WHERE round_id=?
                        AND NOT EXISTS (SELECT 1 FROM investigation_wakes w WHERE w.task_id=t.id)""", (rid,))
            by_assignment = {(a["role"], a["question"]): a for a in plan["tasks"]}
            for task in tasks:
                assignment = by_assignment[(task["role"], task["question"])]
                if assignment.get("knowledge_query"):
                    task_knowledge(c, r, task, assignment, plan)
            matched = c.execute(
                """SELECT 1 FROM world_task_reads wr JOIN research_tasks t ON t.id=wr.task_id
                                  WHERE t.round_id=? AND json_array_length(wr.payload,'$.world_records')>0 LIMIT 1""",
                (rid,),
            ).fetchone()
            if matched is None:
                c.execute(
                    "UPDATE tracking_rounds SET state='waiting',detail='世界知识候选与目标未匹配，待补来源',finished_at=? WHERE id=?",
                    (utc(), rid),
                )
                return "世界知识候选与目标未匹配，待补来源"
    ids = sorted(reused_docs) if r["world_seed_only"] else list(dict.fromkeys([*ids, *sorted(reused_docs)]))
    if job["kind"] != "handoff_publish":
        watch.apply_plan(worker.store, worker.acquisition, r, plan)
    with worker.store.connection(True) as c:
        if job["attempts"] and r["state"] == "researching":
            # The queue can retry a whole failed job once. Re-run only failed roles with their
            # original pinned reads and validation feedback when the first pass produced no report.
            c.execute(
                """UPDATE research_tasks SET state='queued' WHERE round_id=? AND state='failed'
                         AND NOT EXISTS (SELECT 1 FROM research_tasks WHERE round_id=? AND state='done')""",
                (rid, rid),
            )
        tasks = rows(c, """SELECT * FROM research_tasks t WHERE round_id=?
                     AND NOT EXISTS (SELECT 1 FROM investigation_wakes w WHERE w.task_id=t.id)
                     ORDER BY priority DESC,id""", (rid,))
        deadline = interim_deadline(c, rid, tasks)
    pending = [] if job["kind"] == "handoff_publish" else [
        task for task in tasks if task["state"] not in ("done", "failed")
    ]
    budget_error = ""
    with ThreadPoolExecutor(max_workers=resources.MAX_AGENTS, thread_name_prefix="evosight-role") as pool:
        active = set()
        while active or (pending and not budget_error):
            with worker.store.connection() as c:
                width = resources.capacity(c)["effective"]
            if not width and not active:
                raise resources.ResourcePressure("可用内存不足，已保存本轮进度")
            while pending and not budget_error and len(active) < width:
                active.add(
                    pool.submit(copy_context().run, run_role, worker, job, r, plan, ctx, ids, pending.pop(0))
                )
            # Keep the slow role's lease and work intact. The waiting coordinator can
            # deliver already archived excerpts even if every inference slot is occupied.
            remaining = max(0.0, deadline - time.monotonic()) if deadline is not None else None
            if remaining == 0:
                with worker.store.connection(True) as c:
                    if publish_interim(c, r):
                        deadline = None
                # If no quotable report has arrived, try again at the next task
                # completion. The expired deadline must never turn into a busy loop.
                remaining = None
            done, active = wait(active, timeout=remaining, return_when=FIRST_COMPLETED)
            for future in done:
                error = future.result()
                budget_error = budget_error or error
    drain_wakes(worker, r, deadline)
    with worker.store.connection(True) as c:
        tasks = rows(c, "SELECT * FROM research_tasks WHERE round_id=? ORDER BY id", (rid,))
        if not workflow.goal_matches(c, r):
            return "研究目标已更新"
        raw_tasks = tasks
        tasks = claim_review.previously_returned(c, r, raw_tasks)
        original_tasks = {t["task_id"]: t for t in rows(c, """
            SELECT w.task_id,w.owner_task AS 原调查任务,t.question AS 原调查问题,l.line_key AS 调查线
              FROM investigation_wakes w JOIN research_tasks t ON t.id=w.owner_task
              JOIN investigation_lines l ON l.id=w.line_id WHERE w.round_id=?""", (rid,))}
        for_synthesis = delivery.reconcile_handoff_gaps(tasks, original_tasks)
        completed = [t for t in for_synthesis if t["state"] == "done"]
        if not completed and tasks:
            if budget_error:
                raise DispatchDeferred(budget_error)
            raise ValueError("本轮角色任务尚未完成，可单独重试失败角色")
        all_ids = list(dict.fromkeys([*ids, *[i for t in completed for i in json.loads(t["input_ids"])]]))
        docs = [require(c, "documents", i) for i in all_ids]
        reports = [
            {
                "task_id": t["id"],
                "角色": t["role"],
                "问题": t["question"],
                **original_tasks.get(t["id"], {}),
                **validated_task_report(c, t).model_dump(),
            }
            for t in completed
        ]
        outstanding = [t["question"] for t in tasks if t["state"] != "done"] + pending_handoffs(c, rid, tasks)
        old_judgments = rows(
            c,
            "SELECT title,body FROM nodes WHERE event_id=? AND kind='judgment' ORDER BY id DESC LIMIT 3",
            (r["event_id"],),
        )
        past_docs = [row["id"] for row in ctx.get("prior", []) if row.get("id")]
        prior_sources = [doc for vid in past_docs for doc in json.loads(require(c, "claims", vid)["refs"])]
        earlier = delivery.earlier_work(c, r)
        lead_ids = list(dict.fromkeys(i for lead in earlier[0] for i in lead["fragment_ids"]))
        prior_sources.extend(
            row[0]
            for row in c.execute(
                """SELECT DISTINCT a.document_id FROM fragments f JOIN evidence_assets a ON a.id=f.asset_id
               JOIN event_docs d ON d.document_id=a.document_id WHERE d.event_id=?
               AND f.id IN (SELECT value FROM json_each(?))""",
                (r["event_id"], dump(lead_ids)),
            )
        )
        research_docs = list(dict.fromkeys([*[d["id"] for d in docs], *prior_sources]))
        review_fragments = wakes.publication_fragments(c, r, research_docs)
        plan, user_requests = steering_reports.checklist(plan, steering.for_synthesis(c, r))
        review = delivery.review_inputs(c, r, plan, review_fragments, earlier, reports)
        if user_requests:
            review["用户补充与答复"] = user_requests
            review["用户补充交付说明"] = (
                "每条已接收的补充有固定的 check_id。逐项在 delivery 填写终稿答复；"
                "用户提供的是调查范围、偏好或线索，确认事实仍须引用归档原文。"
                "证据不足时填 unknown，写清缺哪项依据。若它改变了综合判断，请在 judgment 说明。"
            )
        shown_ids = {f["id"] for f in review["交付可引用原文"]}
        review_archive = {f["id"]: f for f in review_fragments if f["id"] in shown_ids}
        # The author can cite the archived paragraphs carried from a prior snapshot as well.
        already_shown = {d["id"] for d in docs}
        for doc_id in dict.fromkeys(f["document_id"] for f in review_archive.values()):
            if doc_id not in already_shown:
                docs.append(require(c, "documents", doc_id))
    synthesis_ctx = {
        **ctx,
        **review,
        "此前判断": old_judgments,
        "本轮研究": reports,
        **({"本轮待查问题": outstanding} if outstanding else {}),
        "evidence": [{"id": d["id"], "title": d["title"], "summary": d["summary"][:600]} for d in docs],
    }
    if any(report["task_id"] in original_tasks for report in reports):
        delivered_docs = {d["id"] for d in docs}
        synthesis_ctx["archive_fragment_ids"] = [
            f["id"] for f in review_fragments if f["document_id"] in delivered_docs or f["id"] in review_archive
        ]
    if not tasks:
        synthesis = Analysis(
            fact="", judgment="", watch="", items=[], discoveries=[], evidence_ids=[], gaps=plan["postponed"]
        )
    elif budget_error:
        synthesis = Analysis(
            fact="", judgment="", watch="", items=[], discoveries=[], evidence_ids=[], gaps=["额度不足，综合判断待补"]
        )
    else:
        synthesis, _ = worker.call(job, "study", synthesis_ctx)
    checked = delivery.inspect(plan, synthesis.delivery, review_archive)
    incomplete = [row for row in checked if row["state"] in ("missing", "rejected")]
    if incomplete and plan.get("checklist") and tasks and not budget_error:
        try:
            revised, _ = worker.call(
                job,
                "study",
                {
                    **synthesis_ctx,
                    "交付前复核": {
                        "原答复": [a.model_dump() for a in synthesis.delivery],
                        "待修项": incomplete,
                        "要求": "仅修复所列核查项。无法作答时交代证据缺口。",
                    },
                },
            )
        except (DispatchDeferred, RuntimeError, ValueError, OSError) as err:
            synthesis.gaps.append("交付前补核未完成。" + str(err)[:200])
        else:
            ids_to_fix = {row["position"] for row in incomplete}
            replacements = [a for a in revised.delivery if a.check_id in ids_to_fix]
            supplied = {a.check_id for a in replacements}
            synthesis.delivery = [a for a in synthesis.delivery if a.check_id not in supplied] + replacements
            checked = delivery.inspect(plan, synthesis.delivery, review_archive)
    adjudicated = claim_review.run(
        worker, job, r, plan, reports, checked, synthesis, review_fragments, review_archive, raw_tasks
    )
    try:
        refs = worker.allowed(
            synthesis.evidence_ids,
            docs,
            bool(synthesis.judgment or synthesis.discoveries or synthesis.closure_reason),
        )
    except ValueError:
        with worker.store.connection(True) as c:
            c.execute("DELETE FROM model_receipts WHERE job_id=? AND role='study'", (job["id"],))
        raise
    with worker.store.connection(True) as c:
        if not workflow.goal_matches(c, r):
            return "研究目标已更新"
        if job["kind"] == "handoff_publish" and (redirected := wakes.forward_to_latest(c, r)):
            return redirected
        adjudicated.persist(c, r, job, raw_tasks)
        approved = delivery.reconcile_handoff_gaps(adjudicated.publish_tasks(tasks), original_tasks)
        detail = publish(c, r, approved, synthesis, refs, plan, docs, candidates,
                         worker, job, checked=checked, user_requests=user_requests)
        queue_failed_tasks(c, r, tasks, job["id"])
        return detail


def queue_failed_tasks(c, r, tasks, source_job):
    failed = []
    for task in tasks:
        if task["state"] != "failed" or one(c, "SELECT 1 FROM investigation_wakes WHERE task_id=?", (task["id"],)):
            continue
        if not c.execute(
            "INSERT OR IGNORE INTO role_retry_sources VALUES(?,?)", (task["id"], source_job)
        ).rowcount:
            continue
        attempt = c.execute(
            """INSERT INTO role_retries VALUES(?,1) ON CONFLICT(task_id)
                          DO UPDATE SET attempts=attempts+1 WHERE attempts<2 RETURNING attempts""",
            (task["id"],),
        ).fetchone()
        if attempt:
            failed.append((task["id"], attempt[0]))
    if failed:
        retry = enqueue(
            c,
            "research_retry",
            event=r["event_id"],
            args={
                "round_id": r["id"],
                "failed_tasks": [t for t, _ in failed],
                "dispatch_key": "retry/" + workflow.fingerprint(failed),
            },
        )
        delay = 45 * max(n for _, n in failed)
        c.execute("UPDATE jobs SET retry_at=?,info=? WHERE id=?", (later(delay), "稍后自动补查失败的调查线", retry))


def publish(c, r, tasks, synthesis, refs, plan, docs, candidates, worker, job, *, checked=None,
            user_requests=()):
    event = r["event_id"]
    rid = r["id"]
    signature = workflow.fingerprint(
        [(t["id"], t["state"], t["result"]) for t in tasks], synthesis.model_dump(), checked
    )
    if user_requests:
        signature = workflow.fingerprint(signature, [(u["id"], u["check_id"]) for u in user_requests])
    existing = one(
        c, "SELECT snapshot_id FROM round_publications WHERE round_id=? AND signature=?", (rid, signature)
    )
    if existing:
        return f"已发布知识版本 {existing['snapshot_id']}"
    last = workflow.latest(c, event)
    frags = {f["id"]: f for f in wakes.publication_fragments(c, r, [d["id"] for d in docs])}
    pending_wakes = pending_handoffs(c, rid, tasks)
    gaps = list(dict.fromkeys([*plan.get("postponed", []), *synthesis.gaps, *pending_wakes]))
    changes = []
    added = set()
    contributed = set()
    proposed_links = {w["entry_id"]: w for w in plan.get("use_knowledge", [])}
    decisions = {(v.task_id, v.finding): v for v in synthesis.publication}
    index_notes = {}
    excluded = {
        w[0]
        for w in c.execute(
            "SELECT entry_id FROM world_link_choices WHERE event_id=? AND choice='exclude'", (event,)
        )
    }
    replaced = {
        row[0]
        for row in c.execute(
            "SELECT replaces FROM claims WHERE event_id=? AND replaces IS NOT NULL", (event,)
        )
    }
    for task in tasks:
        if task["state"] != "done":
            gaps.append(f"{ROLE_NAMES[task['role']]} · {task['detail'] or '尚未完成'}")
            continue
        for proposed in Report.model_validate_json(task["result"]).findings:
            conflict = conflicting_support(c, proposed)
            if conflict:
                board.record_dispute(c, proposed.supports, event, proposed, conflict)
        report = validated_task_report(c, task)
        for link in report.use_knowledge:
            proposed_links.setdefault(link.entry_id, link.model_dump())
        gaps.extend(report.gaps)
        for pos, f in enumerate(report.findings, 1):
            gap = conflicting_support(c, f)
            if gap:
                gaps.append(gap)
                continue
            choice = decisions.get((task["id"], pos))
            local = board.local_reason(f.kind, f.subject, f.text, f.facets)
            if local or (choice.scope if choice else f.scope) == "event":
                citations = [
                    {
                        "document_id": frags[x.fragment_id]["document_id"],
                        "fragment_id": x.fragment_id,
                        "quote": x.quote,
                        "locator": frags[x.fragment_id]["locator"],
                    }
                    for x in f.citations
                ]
                board.note(
                    c,
                    event,
                    r["goal_version"],
                    task["question"],
                    f.subject,
                    f.text,
                    f.kind,
                    local or (choice.reason if choice else f.reason) or "事件专属材料",
                    citations,
                    rid=rid,
                    task=task["id"],
                )
                if f.facets.get("预测ID"):
                    c.execute(
                        "INSERT OR IGNORE INTO round_prediction_gates VALUES(?,?,'checking')",
                        (rid, f.facets["预测ID"]),
                    )
                continue
            equivalent = board.indicator_parent(c, event, f)
            if equivalent:
                if json.loads(equivalent["attributes"])["value"] != f.attributes["value"]:
                    conflict = f"同一指标、统计期和口径有两个值，待复核版本 #{equivalent['id']}"
                    board.record_dispute(c, equivalent["id"], event, f, conflict)
                    gaps.append(conflict)
                    continue
                f.supports, f.subject, f.text = equivalent["id"], equivalent["subject"], equivalent["text"]
            ident, is_new = claim(c, event, task["role"], f, frags, task_id=task["id"])
            if task["role"] == "people" and f.entity_kind == "person":
                people.adopt_claim(c, r, ident)
            contributed.add(ident)
            if excluded and world.version(c, ident)["entry_id"] in excluded:
                continue
            if target_prediction := f.facets.get("预测ID"):
                c.execute(
                    "INSERT OR IGNORE INTO round_prediction_gates VALUES(?,?,'checking')",
                    (rid, target_prediction),
                )
            already_added = ident in added
            added.add(ident)
            index_notes.setdefault(ident, (f.section, "用于核对“" + task["question"][:280] + "”"))
            if f.replaces:
                replaced.add(f.replaces)
            if not is_new or already_added:
                continue
            doc_ids = list(dict.fromkeys(frags[x.fragment_id]["document_id"] for x in f.citations))
            changes.append(
                {
                    "id": ident,
                    "section": f.section,
                    "text": f.text,
                    "kind": "support" if f.supports else f.kind,
                }
            )
            occurred = f.occurred_at or None
            in_window = occurred and "T" in occurred and r["window_start"] < occurred <= r["window_end"]
            if f.supports:
                node(
                    c,
                    event,
                    "historical",
                    "世界知识补证 · " + f.subject,
                    f.reason or "本轮核对了原文并补充条目资料",
                    refs=doc_ids,
                )
            elif f.kind == "fact" and in_window:
                node(c, event, "fact", f.text, refs=doc_ids, occurred=occurred)
            elif occurred and f.kind in ("fact", "correction"):
                node(c, event, "historical", "历史补证 · " + f.subject, f.text, refs=doc_ids, occurred=occurred)
            elif f.replaces or f.kind == "conflict":
                node(c, event, "correction", f.subject, f.text, refs=doc_ids)
            target = f.facets.get("关联事件ID")
            if (
                f.kind == "relation"
                and target
                and f.facets.get("机制")
                and "关联变化" not in json.loads(r["reasons"])
            ):
                linked = require(c, "events", target)
                if linked["status"] == "active" and not linked["demo"]:
                    c.executemany(
                        "INSERT OR IGNORE INTO event_docs(event_id,document_id,intake) VALUES(?,?,'window')",
                        [(target, i) for i in doc_ids],
                    )
                    workflow.request_round(
                        c, target, "关联变化", key=f"related/{event}/{workflow.fingerprint(sorted(doc_ids))}"
                    )
        role_refs = list(
            dict.fromkeys(
                frags[ref.fragment_id]["document_id"] for f in report.findings for ref in f.citations
            )
        )
        for obs in report.followups:
            workflow.observe(
                c,
                event,
                task["role"],
                obs.question,
                obs.condition,
                refs=role_refs,
                pred=obs.prediction_id or None,
                due=obs.due_at,
            )
        if report.findings:
            c.executemany(
                "UPDATE observations SET state='answered',answer=?,refs=? WHERE id=? AND event_id=?",
                [(report.findings[0].text, dump(role_refs), i, event) for i in report.answered],
            )
    if any(t["role"] == "people" and t["state"] == "done" for t in tasks):
        focus.match_people(c, r, people.roster(c, event))
    previous_j = one(
        c, "SELECT body FROM nodes WHERE event_id=? AND kind='judgment' ORDER BY id DESC LIMIT 1", (event,)
    )
    if synthesis.judgment and (not previous_j or previous_j["body"] != synthesis.judgment):
        judgment_node = node(
            c, event, "judgment", "研究判断更新", synthesis.judgment, watch=synthesis.watch, refs=refs
        )
        links = {
            cite["fragment_id"]
            for ident in added
            for cite in json.loads(require(c, "claims", ident)["citations"])
            if cite["document_id"] in refs
        }
        c.executemany("INSERT INTO node_fragments VALUES(?,?)", [(judgment_node, i) for i in sorted(links)])
    for it in synthesis.items[:4]:
        if not it.name.strip():
            continue
        c.execute("INSERT OR IGNORE INTO items(event_id,name) VALUES(?,?)", (event, it.name))
        item = one(c, "SELECT id FROM items WHERE event_id=? AND name=?", (event, it.name))["id"]
        prior = one(c, "SELECT body,watch FROM nodes WHERE item_id=? ORDER BY id DESC LIMIT 1", (item,))
        if not prior or (prior["body"], prior["watch"]) != (it.judgment, it.watch):
            node(c, event, "item", it.name, it.judgment, watch=it.watch, refs=refs, item=item)
    if synthesis.watch:
        workflow.observe(c, event, "认知更新", synthesis.watch, "出现可以定位的公开材料", refs=refs)
    e = require(c, "events", event)
    topic = require(c, "topics", e["topic_id"]) if e["topic_id"] else None
    candidates(
        c,
        synthesis.discoveries,
        event,
        topic,
        refs,
        [s.url for s in watch.sources(c, event)],
        allow_auto=bool(r["parent_snapshot"]),
    )
    if synthesis.closure_reason and refs:
        c.execute(
            "INSERT OR IGNORE INTO end_proposals(event_id,reason,refs,goal_version,created_at) VALUES(?,?,?,?,?)",
            (event, synthesis.closure_reason, dump(refs), r["goal_version"], utc()),
        )
    for link in plan.get("release_knowledge", []):
        if one(
            c,
            "SELECT 1 FROM world_link_choices WHERE event_id=? AND entry_id=? AND choice='pin'",
            (event, link["entry_id"]),
        ):
            continue
        v = world.version(c, link["version_id"])
        if v and v["head_id"] == link["version_id"] and v["entry_id"] == link["entry_id"]:
            replaced.add(link["version_id"])
            changes.append(
                {
                    "id": link["version_id"],
                    "world_id": link["entry_id"],
                    "kind": "unreference",
                    "text": link["reason"],
                }
            )
    selected_links = []
    for link in proposed_links.values():
        v = world.version(c, link["version_id"])
        if not v or v["entry_id"] != link["entry_id"] or v["entry_id"] in excluded:
            continue
        if v["head_id"] != link["version_id"]:
            old = require(c, "claims", link["version_id"])
            head = require(c, "claims", v["head_id"])
            if v["head_id"] not in contributed or (old["subject"], old["text"]) != (
                head["subject"],
                head["text"],
            ):
                gaps.append("所选世界知识在研究期间已有订正，待复核后引用")
                continue
            link = {**link, "version_id": v["head_id"]}
        knowledge = require(c, "claims", v["head_id"])
        selected_links.append(link)
        if v["head_id"] not in added:
            changes.append(
                {
                    "id": v["head_id"],
                    "world_id": v["entry_id"],
                    "kind": "reference",
                    "section": link.get("section") or knowledge["section"],
                    "text": knowledge["text"],
                    "reason": link["reason"],
                }
            )
            n = node(
                c,
                event,
                "historical",
                "引用已有知识 · " + knowledge["subject"],
                link["reason"],
                refs=json.loads(knowledge["refs"]),
            )
            c.executemany(
                "INSERT OR IGNORE INTO node_fragments VALUES(?,?)",
                [(n, ref["fragment_id"]) for ref in json.loads(knowledge["citations"])],
            )
    generation = c.execute("SELECT count(*)+1 FROM knowledge_snapshots WHERE round_id=?", (rid,)).fetchone()[
        0
    ]
    upper = c.execute("SELECT coalesce(max(id),0) FROM nodes WHERE event_id=?", (event,)).fetchone()[0]
    summary = synthesis.judgment or next((change["text"] for change in changes), "暂无新的有据判断")
    snap = c.execute(
        """INSERT INTO knowledge_snapshots(event_id,round_id,generation,parent_id,goal_version,created_at,gaps,summary,changes,node_upper)
                      VALUES(?,?,?,?,?,?,?,?,?,?)""",
        (
            event,
            rid,
            generation,
            last["id"] if last else None,
            r["goal_version"],
            utc(),
            dump(list(dict.fromkeys(gaps))),
            summary,
            dump(changes),
            upper,
        ),
    ).lastrowid
    inherited = (
        {i[0] for i in c.execute("SELECT claim_id FROM snapshot_claims WHERE snapshot_id=?", (last["id"],))}
        if last
        else set()
    )
    c.executemany(
        "INSERT INTO snapshot_claims(snapshot_id,claim_id) VALUES(?,?)",
        [(snap, i) for i in (inherited | added) - replaced],
    )
    for ident, (section, reason) in index_notes.items():
        if not last or not one(
            c,
            """SELECT 1 FROM snapshot_claims s JOIN world_versions v ON v.claim_id=s.claim_id
            WHERE s.snapshot_id=? AND v.entry_id=(SELECT entry_id FROM world_versions WHERE claim_id=?)""",
            (last["id"], ident),
        ):
            c.execute(
                "UPDATE snapshot_claims SET index_section=?,index_reason=? WHERE snapshot_id=? AND claim_id=?",
                (section, reason, snap, ident),
            )
    for link in selected_links:
        c.execute(
            """INSERT INTO snapshot_claims(snapshot_id,claim_id,index_section,index_reason) VALUES(?,?,?,?)
                      ON CONFLICT(snapshot_id,claim_id) DO UPDATE SET index_reason=excluded.index_reason,
                      index_section=CASE WHEN excluded.index_section='' THEN snapshot_claims.index_section ELSE excluded.index_section END""",
            (snap, link["version_id"], link.get("section", ""), link["reason"]),
        )
    c.execute("INSERT INTO round_publications VALUES(?,?,?)", (rid, signature, snap))
    c.executemany(
        "UPDATE investigation_wakes SET snapshot_id=? WHERE task_id=? AND snapshot_id IS NULL",
        [(snap, t["id"]) for t in tasks if t["state"] == "done"],
    )
    delivery.save(
        c, snap, checked if checked is not None else delivery.inspect(plan, synthesis.delivery, frags)
    )
    if user_requests:
        steering_reports.save(c, snap, user_requests)
    c.execute(
        """INSERT INTO snapshot_rejections SELECT ?,t.id,j.subject,j.reason FROM task_rejections j
                 JOIN research_tasks t ON t.id=j.task_id WHERE t.round_id=?""",
        (snap, rid),
    )
    board.notify(c, event, contributed)
    workflow.freeze_metadata(c, event, snap, [d["id"] for d in docs])
    workflow.refresh_shared_indexes(c, event, contributed)
    c.executemany(
        "UPDATE event_docs SET analyzed=1 WHERE event_id=? AND document_id=?",
        [(event, d["id"]) for d in docs],
    )
    for p in rows(
        c,
        """SELECT p.id FROM round_prediction_gates g JOIN predictions p ON p.id=g.prediction_id
                    WHERE g.round_id=? AND p.status IN ('open','frozen','review')""",
        (rid,),
    ):
        c.execute(
            "INSERT OR IGNORE INTO prediction_checks VALUES(?,?,'checking','等待核验',?)", (p["id"], snap, utc())
        )
        enqueue(c, "verify", event=event, args={"prediction_id": p["id"], "snapshot_id": snap})
    if changes or synthesis.judgment:
        enqueue(c, "forecast", event=event, args={"snapshot_id": snap})
    state = "partial" if pending_wakes or any(t["state"] != "done" for t in tasks) else "published"
    if any(t["state"] == "deferred" for t in tasks):
        deferred_job = enqueue(c, "research_retry", event=event, args={"round_id": rid})
        if deferred_job != job["id"]:
            c.execute("UPDATE jobs SET status='deferred',info='额度恢复后补查未完成角色' WHERE id=?", (deferred_job,))
    detail = f"已发布知识版本 {snap}，新增 {len(changes)} 条有据结论"
    c.execute(
        "UPDATE tracking_rounds SET state=?,detail=?,finished_at=? WHERE id=?", (state, detail, utc(), rid)
    )
    new_sources = one(c, "SELECT added FROM round_monitors WHERE round_id=?", (rid,))
    if new_sources and new_sources["added"] and "补充来源" not in json.loads(r["reasons"]):
        workflow.request_round(c, event, "补充来源", key=f"source-plan/{rid}")
    return detail
