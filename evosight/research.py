"""Execute recoverable research rounds and publish their evidence, knowledge, and follow-up work."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
import json
from itertools import zip_longest
import re

from . import feeds, focus, materials, people, resources, watch, workflow, world, world_catalog, world_search
from .agents import Analysis
from .contracts import Assignment, Report, ROLE_NAMES, FACETS
from .db import DispatchDeferred, config, dump, enqueue, instant, later, node, one, require, rows, usage, utc


def model_fragments(fragments):
    return [
        {
            k: (v[:1350] if k == "text" else v.split("#:~:text=", 1)[0] if k == "link" else v)
            for k, v in f.items()
            if k in ("id", "document_id", "text", "locator", "kind", "title", "link")
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


def check_report(report, fragments, context, role):
    visible = {f["id"]: f for f in model_fragments(fragments)}
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
        world_catalog.attributes(category, finding.facets, finding.attributes)
        for citation in finding.citations:
            if (
                citation.fragment_id not in visible
                or citation.quote.strip() not in visible[citation.fragment_id]["text"]
            ):
                raise ValueError("角色引文与本次输入片段不相符，需重试")
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

    if not set(report.answered) <= observation_ids:
        raise ValueError("角色回答了不属于本轮的观察项")
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
        """SELECT d.attributes FROM world_versions v JOIN world_entries w ON w.id=v.entry_id
        JOIN world_record_data d ON d.version_id=w.head_id WHERE v.claim_id=?""",
        (finding.supports,),
    )
    old = json.loads(stored["attributes"]) if stored else {}
    changed = [k for k, v in finding.attributes.items() if k in old and old[k] != v]
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


def claim(c, event, role, item, fragments):
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
        inherited = json.loads(prior_record["attributes"])
        if any(key in inherited and inherited[key] != value for key, value in item.attributes.items()):
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
        {k: v for k, v in fields.items() if k not in derived or derived[k] != v},
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
    return ident, True


def study_context(c, r):
    event = r["event_id"]
    e = require(c, "events", event)
    prior = workflow.snapshot_claims(c, r["parent_snapshot"]) if r["parent_snapshot"] else []
    goal = workflow.current_goal(c, event)
    objects = rows(
        c, "SELECT id,title,scope FROM events WHERE id!=? AND (demo=0 OR ?=1) LIMIT 36", (event, e["demo"])
    )
    suggestions = world.suggested(c, event)
    routes = world_catalog.route(e["title"] + " " + e["scope"])
    paths = {p for w in suggestions for p in world_catalog.prefixes(w["path"])}
    paths.update(p for route in routes for p in world_catalog.prefixes(route["path"]))
    return {
        "事件": e["title"],
        "研究范围": e["scope"],
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
        return worker.call(job, "source_scout", ctx)[0]

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
    if query.get("query") or query.get("path") or query.get("entity_id"):
        hits, _ = world_search.search(
            c,
            query.get("query", ""),
            path=query.get("path", ""),
            entity_id=query.get("entity_id"),
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


def validated_links(proposals, available):
    accepted = []
    for link in proposals:
        key = (link.entry_id, link.version_id)
        if key in available:
            accepted.append(link)
            available.remove(key)
    return accepted


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
            frags = materials.fragments_for(c, inputs)
            if t["role"] == "people":
                role_context["people_watch"] = people.public_context(c, r)
        result, _ = worker.call(
            job,
            t["role"],
            {
                **role_context,
                "task": {**assignment, "time_scope": t["time_scope"]},
                **({"上次校验反馈": t["detail"] + "。请逐字复制本次输入中的引文；材料不足时留下知识缺口。"} if t["detail"] else {}),
                "fragments": model_fragments(frags),
            },
        )
        submitted = len(result.findings)
        check_report(result, frags, role_context, t["role"])
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
        if r["state"] in ("published", "unchanged", "superseded"):
            return r["detail"]
    ids = json.loads(r["input_docs"]) if r["input_docs"] is not None else acquire(worker, r, job)
    if not ids:
        with worker.store.connection() as c:
            return require(c, "tracking_rounds", rid)["detail"]
    with worker.store.connection() as c:
        docs = [require(c, "documents", i) for i in ids]
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
            tasks = rows(c, "SELECT id,role,question FROM research_tasks WHERE round_id=?", (rid,))
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
        tasks = rows(c, "SELECT * FROM research_tasks WHERE round_id=? ORDER BY priority DESC,id", (rid,))
    pending = [task for task in tasks if task["state"] not in ("done", "failed")]
    budget_error = ""
    while pending and not budget_error:
        with worker.store.connection() as c:
            width = resources.capacity(c)["effective"]
        if not width:
            raise resources.ResourcePressure("可用内存不足，已保存本轮进度")
        wave, pending = pending[:width], pending[width:]
        if len(wave) == 1:
            budget_error = run_role(worker, job, r, plan, ctx, ids, wave[0])
        else:
            with ThreadPoolExecutor(max_workers=len(wave), thread_name_prefix="evosight-role") as pool:
                futures = [pool.submit(run_role, worker, job, r, plan, ctx, ids, t) for t in wave]
                results = [future.result() for future in futures]
            budget_error = next((error for error in results if error), "")
    with worker.store.connection() as c:
        tasks = rows(c, "SELECT * FROM research_tasks WHERE round_id=? ORDER BY id", (rid,))
        if not workflow.goal_matches(c, r):
            return "研究目标已更新"
        completed = [t for t in tasks if t["state"] == "done"]
        if not completed and tasks:
            if budget_error:
                raise DispatchDeferred(budget_error)
            raise ValueError("本轮角色任务尚未完成，可单独重试失败角色")
        all_ids = list(dict.fromkeys([*ids, *[i for t in completed for i in json.loads(t["input_ids"])]]))
        docs = [require(c, "documents", i) for i in all_ids]
        reports = [
            {"角色": t["role"], "问题": t["question"], **validated_task_report(c, t).model_dump()}
            for t in completed
        ]
        old_judgments = rows(
            c,
            "SELECT title,body FROM nodes WHERE event_id=? AND kind='judgment' ORDER BY id DESC LIMIT 3",
            (r["event_id"],),
        )
    synthesis_ctx = {
        **ctx,
        "此前判断": old_judgments,
        "本轮研究": reports,
        "evidence": [{"id": d["id"], "title": d["title"], "summary": d["summary"][:600]} for d in docs],
    }
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
        return publish(c, r, tasks, synthesis, refs, plan, docs, candidates, worker, job)


def publish(c, r, tasks, synthesis, refs, plan, docs, candidates, worker, job):
    event = r["event_id"]
    rid = r["id"]
    signature = workflow.fingerprint(
        [(t["id"], t["state"], t["result"]) for t in tasks], synthesis.model_dump()
    )
    existing = one(
        c, "SELECT snapshot_id FROM round_publications WHERE round_id=? AND signature=?", (rid, signature)
    )
    if existing:
        return f"已发布知识版本 {existing['snapshot_id']}"
    last = workflow.latest(c, event)
    frags = {f["id"]: f for i in [d["id"] for d in docs] for f in materials.fragments_for(c, [i])}
    gaps = list(dict.fromkeys([*plan.get("postponed", []), *synthesis.gaps]))
    changes = []
    added = set()
    contributed = set()
    proposed_links = {w["entry_id"]: w for w in plan.get("use_knowledge", [])}
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
        report = validated_task_report(c, task)
        for link in report.use_knowledge:
            proposed_links.setdefault(link.entry_id, link.model_dump())
        gaps.extend(report.gaps)
        for f in report.findings:
            gap = conflicting_support(c, f)
            if gap:
                gaps.append(gap)
                continue
            ident, is_new = claim(c, event, task["role"], f, frags)
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
    state = "partial" if any(t["state"] != "done" for t in tasks) else "published"
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
