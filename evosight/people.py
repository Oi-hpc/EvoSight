"""Event watchlists point to shared people and archived evidence, with per-window source coverage."""
from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from urllib.parse import urlsplit

from . import focus, materials, person_sources as social, resources, workflow, world, world_search
from .db import dump, one, require, rows, utc


def roster(c, event):
    ev = require(c, "events", event)
    return rows(
        c,
        """SELECT t.*,e.name,e.identity,f.locator,a.document_id,
           (SELECT count(*) FROM person_materials m WHERE m.target_id=t.id) AS materials,
           (SELECT max(round_id) FROM person_checks k WHERE k.target_id=t.id) AS last_round,
           coalesce((SELECT json_group_array(alias) FROM world_aliases WHERE demo=? AND entity_id=e.id),'[]') AS aliases
           FROM person_targets t JOIN entities e ON e.id=t.entity_id JOIN fragments f ON f.id=t.fragment_id
           JOIN evidence_assets a ON a.id=f.asset_id WHERE t.event_id=?
           ORDER BY (t.role='核心人物') DESC,t.id""",
        (ev["demo"], event),
    )


def current_targets(c, r):
    pinned = {f["person_target_id"] for f in focus.applicable(c, r) if f["person_target_id"]}
    targets = []
    for t in roster(c, r["event_id"]):
        t["requested"] = t["id"] in pinned
        if t["requested"] or t["goal_version"] == r["goal_version"]:
            targets.append(t)
    # Least-recently checked first. Core people break ties, without starving other roles.
    return sorted(
        targets,
        key=lambda t: (
            not t["requested"],
            t["last_round"] != r["id"],
            t["last_round"] or 0,
            t["role"] != "核心人物",
            t["id"],
        ),
    )


def targets_for_round(c, r):
    targets = current_targets(c, r)
    return [t for t in targets if t["requested"]] + [t for t in targets if not t["requested"]][:2]


def named(text, names):
    folded = " ".join(text.casefold().split())
    return any(" ".join(n.casefold().split()) in folded for n in names if n and len(n.strip()) > 1)


def names_of(target):
    aliases = target.get("aliases", [])
    return [target["name"], *(json.loads(aliases) if isinstance(aliases, str) else aliases)]


def register(c, r, lead, visible):
    cite = lead.citation
    frag = visible.get(cite.fragment_id)
    names = [lead.name.strip(), *[n.strip() for n in lead.aliases if n.strip()]]
    if not frag or cite.quote.strip() not in frag["text"] or not named(cite.quote, names[:1]):
        return None
    identity_proof = (lead.identity_quote or lead.identity).strip()
    if (
        len(identity_proof) < 2
        or identity_proof.casefold() == names[0].casefold()
        or identity_proof.casefold() not in frag["text"].casefold()
    ):
        return None
    verified_aliases = [n for n in names[1:] if named(frag["text"], [n])]
    demo = require(c, "events", r["event_id"])["demo"]
    entity = world.resolve_entity(c, demo, "person", names[0], lead.identity.strip(), verified_aliases)
    c.execute(
        """INSERT INTO person_targets(event_id,entity_id,goal_version,role,relevance,fragment_id,quote,created_at,updated_at)
        VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(event_id,entity_id) DO UPDATE SET
        goal_version=excluded.goal_version,role=excluded.role,relevance=excluded.relevance,
        fragment_id=excluded.fragment_id,quote=excluded.quote,updated_at=excluded.updated_at""",
        (
            r["event_id"],
            entity,
            r["goal_version"],
            lead.role,
            lead.relevance.strip(),
            frag["id"],
            cite.quote.strip(),
            utc(),
            utc(),
        ),
    )
    return one(c, "SELECT id FROM person_targets WHERE event_id=? AND entity_id=?", (r["event_id"], entity))[
        "id"
    ]


def lead_fragments(fragments, names=()):
    from .research import model_fragments
    import re

    role = re.compile(
        r"创始|负责|发言人|表示|董事|教授|首席|开发者|维护者|founder|creator|chief|maintain|spokesperson|director|president", re.I
    )
    front = [f for f in fragments if role.search(f["text"][:1350])]
    targeted = []
    for f in fragments:
        positions = [f["text"].casefold().find(name.casefold()) for name in names]
        hits = [p for p in positions if p >= 0]
        if hits:
            start = max(0, min(hits) - 300)
            targeted.append({**f, "text": f["text"][start : start + 1350]})
    seen = {f["id"] for f in targeted}
    return model_fragments((targeted + [f for f in front if f["id"] not in seen])[:8])


def adopt_claim(c, r, claim):
    from .contracts import PersonLead, Citation

    k = require(c, "claims", claim)
    if not k["entity_id"]:
        return
    e = require(c, "entities", k["entity_id"])
    if e["kind"] != "person" or one(
        c,
        "SELECT 1 FROM person_targets WHERE event_id=? AND entity_id=? AND goal_version=?",
        (r["event_id"], e["id"], r["goal_version"]),
    ):
        return
    for cite in json.loads(k["citations"]):
        if not named(cite["quote"], [e["name"]]):
            continue
        f = require(c, "fragments", cite["fragment_id"])
        if e["identity"].casefold() not in f["text"].casefold():
            continue
        lead = PersonLead(
            name=e["name"],
            identity=e["identity"],
            role="参与者",
            relevance=(k["reason"] or k["text"])[:400],
            citation=Citation(fragment_id=f["id"], quote=cite["quote"]),
        )
        register(c, r, lead, {f["id"]: f})
        return


def task_documents(worker, r):
    with worker.store.connection() as c:
        chosen = targets_for_round(c, r)
        first = {
            t["id"]
            for t in chosen
            if not one(
                c, "SELECT 1 FROM person_checks WHERE target_id=? AND round_id!=? LIMIT 1", (t["id"], r["id"])
            )
        }
    return list(
        dict.fromkeys(
            d
            for t in chosen
            for d in check_target(
                worker, r, t, background=t["id"] in first or "人物专项" in json.loads(r["reasons"])
            )
        )
    )


def seed(worker, r):
    reasons = json.loads(r["reasons"])
    if r["parent_snapshot"] and not any(x in reasons for x in ("人物专项", "目标更新")):
        return []
    with worker.store.connection() as c:
        saved = one(c, "SELECT document_ids FROM people_seed_checks WHERE round_id=?", (r["id"],))
        if saved:
            return json.loads(saved["document_ids"])
        event = require(c, "events", r["event_id"])
    query = f'{event["title"]} {event["scope"][:45]} 负责人 发言人 团队 采访'
    found = []
    status = "未找到可核查的具名材料"
    try:
        candidates = worker.acquisition.targeted(query)
        with worker.store.connection(True) as c:
            if not workflow.goal_matches(c, r):
                return []
            for entry, res, ex in candidates[:2]:
                if social.HOSTS.get(urlsplit(res.url).hostname) or not materials.relevance(
                    event["title"], event["scope"], entry["title"] + " " + entry["summary"][:800]
                ):
                    continue
                if ex.published_at and ex.published_at > r["window_end"]:
                    continue
                found.append(materials.attach_search_result(c, event["id"], entry, res, ex, "backfill"))
            if found:
                status = f"取得 {len(found)} 份主题相关资料待辨认人物"
    except (OSError, ValueError, RuntimeError, TimeoutError):
        status = "人物来源检索暂不可用"
    with worker.store.connection(True) as c:
        c.execute("INSERT OR IGNORE INTO people_seed_checks VALUES(?,?,?)", (r["id"], status, dump(found)))
    return found


def account_links(c, event, target, doc, res):
    ev = require(c, "events", event)
    proof_doc = target["document_id"]
    if res.mime.lower().startswith(("text/html", "application/xhtml")):
        root = materials.tree_for(res)
        if named(root.text_content(), names_of(target)):
            for href in root.xpath("//article//a/@href | //main//a/@href | //blockquote//a/@href")[:160]:
                loc = social.location(materials.safe_link(res.url, href), post=True)
                if loc:
                    c.execute(
                        "INSERT OR IGNORE INTO person_post_links VALUES(?,?,?,?)",
                        (target["id"], loc[0], loc[2], doc),
                    )
    for (platform, key, url), proof, strong in social.profile_links(res, names_of(target)):
        # The person must have been identified on this very page before its explicit identity links are trusted.
        confirmed = strong and doc == proof_doc
        occupied = one(
            c,
            "SELECT entity_id FROM person_accounts WHERE demo=? AND platform=? AND account_key=? AND state='confirmed'",
            (ev["demo"], platform, key),
        )
        state = (
            "confirmed"
            if confirmed and (not occupied or occupied["entity_id"] == target["entity_id"])
            else "candidate"
        )
        c.execute(
            """INSERT INTO person_accounts(demo,entity_id,platform,account_key,url,state,proof_doc,proof_text,created_at)
            VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(demo,entity_id,platform,account_key) DO UPDATE SET
            state=excluded.state,proof_doc=excluded.proof_doc,proof_text=excluded.proof_text
            WHERE person_accounts.state='candidate' AND excluded.state='confirmed'""",
            (ev["demo"], target["entity_id"], platform, key, url, state, doc, proof, utc()),
        )


def stored_profile_links(c, event, target):
    asset = one(
        c,
        """SELECT a.url,b.mime,b.content FROM evidence_assets a JOIN evidence_blobs b ON b.digest=a.digest
             WHERE a.document_id=? AND a.kind='html' AND a.state='ready' ORDER BY a.id DESC LIMIT 1""",
        (target["document_id"],),
    )
    if asset:
        from .feeds import Resource

        account_links(
            c, event, target, target["document_id"], Resource(asset["content"], asset["url"], asset["mime"])
        )


def account_list(c, event, target):
    return rows(
        c,
        """SELECT a.*,coalesce((SELECT ed.event_id FROM event_docs ed WHERE ed.document_id=a.proof_doc AND ed.event_id=?),
              (SELECT ed.event_id FROM event_docs ed JOIN events e ON e.id=ed.event_id
              WHERE ed.document_id=a.proof_doc AND e.demo=a.demo ORDER BY ed.event_id LIMIT 1)) AS proof_event
              FROM person_accounts a WHERE a.entity_id=? AND a.demo=(SELECT demo FROM events WHERE id=?) ORDER BY a.platform,a.id""",
        (event, target["entity_id"], event),
    )


def review_account(c, event, target_id, account_id, confirm):
    target = require(c, "person_targets", target_id)
    if target["event_id"] != event:
        raise KeyError()
    account = next((a for a in account_list(c, event, target) if a["id"] == account_id), None)
    if not account:
        raise KeyError()
    state = "confirmed" if confirm else "rejected"
    if account["state"] == state:
        return {"state": state}
    c.execute("UPDATE person_accounts SET state=? WHERE id=?", (state, account_id))
    c.execute(
        "INSERT INTO person_account_reviews(account_id,previous,state,reviewed_at) VALUES(?,?,?,?)",
        (account_id, account["state"], state, utc()),
    )
    if not confirm:
        c.execute(
            """UPDATE person_materials SET attribution='账号归属待核' WHERE target_id IN (
          SELECT t.id FROM person_targets t JOIN events e ON e.id=t.event_id WHERE t.entity_id=? AND e.demo=?)
          AND channel=? AND author_key=? AND attribution IN ('本人公开发帖','转发')""",
            (account["entity_id"], account["demo"], account["platform"], account["account_key"]),
        )
    return {"state": state}


def related(text, event, target):
    tokens = set(world_search.query_terms(event["title"] + " " + event["scope"][:160])[0])
    for n in names_of(target):
        tokens -= world_search.terms(n)
    tokens -= {"关注", "进展", "发布", "人物", "分析", "相关", "以及", "进行", "公开", "动态", "update", "release", "与其"}
    return bool(tokens & world_search.terms(text)) if tokens else named(text, names_of(target))


def scan(acq, target, event, channel, accounts, r, history, linked=()):
    """Bound every platform search. Return public material and an honest description of the checked surface."""
    names = names_of(target)
    query = f'"{target["name"]}" {event["title"][:65]}'
    if channel == "history":
        query += " 履历 往事 采访 background"
    elif channel in social.SITES:
        query += " site:" + social.SITES[channel]
    else:
        query += " 新闻 采访"
    if not history:
        next_day = (datetime.fromisoformat(r["window_end"]).date() + timedelta(days=1)).isoformat()
        query += f' after:{r["window_start"][:10]} before:{next_day}'
    confirmed = {
        a["account_key"]: a for a in accounts if a["state"] == "confirmed" and a["platform"] == channel
    }
    records = []
    problems = []
    candidates = list(linked[:1])
    seen = set()

    def keep(post):
        if post.resource.url in seen:
            return
        seen.add(post.resource.url)
        text = post.extract.parts[0][1]
        if not related(text, event, target) or (
            post.author not in confirmed and not named(text + " " + post.extract.title, names)
        ):
            return
        origin = ("转发" if post.repost else "本人公开发帖") if post.author in confirmed else "账号归属待核"
        records.append((post.item(), post.resource, post.extract, origin, post.author))

    if channel == "x":
        for account in list(confirmed.values())[:1]:
            try:
                result = social.x_timeline(account, r["window_start"], r["window_end"])
                if result is not None:
                    posts, more = result
                    for post in posts:
                        keep(post)
                    if more:
                        problems.append("账号时间线本轮只读取前 20 条")
            except (OSError, ValueError, RuntimeError, KeyError):
                problems.append("X 授权时间线本轮未能读取")
    # Prefer direct links from a confirmed public account when the platform serves them.
    if channel in ("weibo", "xiaohongshu"):
        for account in list(confirmed.values())[:1]:
            try:
                res = acq.download(account["url"])
                if social.location(res.url) != social.location(account["url"]):
                    raise ValueError()
                root = materials.tree_for(res)
                direct = [
                    materials.safe_link(res.url, v)
                    for v in root.xpath("//article//a/@href | //main//a/@href")
                ]
                candidates.extend(u for u in direct if social.location(u, post=True))
                if not candidates:
                    problems.append("账号页未展示公开帖链接")
            except (OSError, ValueError, RuntimeError):
                problems.append("账号页本轮不可读")
    try:
        hits = acq.search(query, 4)
        candidates.extend(url for _, url in hits)
    except Exception:
        problems.append("公开搜索本轮未返回结果")
    links = []
    for raw in candidates:
        u = materials.safe_link("", raw)
        host = social.HOSTS.get(urlsplit(u).hostname)
        loc = social.location(u, post=True)
        if channel in social.SITES:
            if not loc or loc[0] != channel:
                continue
            u = loc[2]
        elif not u or host:
            continue
        if u not in links:
            links.append(u)
    fetched = 0
    for url in links[:2]:
        try:
            if channel in social.SITES:
                keep(social.read_post(acq, url, channel))
            else:
                res = acq.download(url)
                if social.HOSTS.get(urlsplit(res.url).hostname):
                    continue
                ex = materials.extract(res)
                if not ex.parts:
                    raise ValueError()
                text = " ".join(p[1] for p in ex.parts[:18])
                if not named(ex.title + " " + text, names) or not related(
                    ex.title + " " + text, event, target
                ):
                    continue
                entry = {
                    "url": res.url,
                    "title": ex.title or url,
                    "summary": text[:3200],
                    "published_at": ex.published_at,
                }
                records.append((entry, res, ex, "历史材料" if channel == "history" else "公开报道", ""))
            fetched += 1
        except (OSError, ValueError, RuntimeError, KeyError, TypeError):
            problems.append("公开页面受限或未提供可核查的正文")
    return records, list(dict.fromkeys(problems)), fetched


def check_target(worker, r, target, *, background):
    event_id = r["event_id"]
    with worker.store.connection(True) as c:
        if not workflow.goal_matches(c, r):
            return []
        event = require(c, "events", event_id)
        stored_profile_links(c, event_id, target)
    publications = ["history", "news"] if background else ["news"]
    docs = []
    for channel in publications:
        docs.extend(check_channel(worker, r, target, event, channel, background))
    with worker.store.connection() as c:
        width = max(1, min(2, resources.capacity(c)["effective"]))
    platforms = ("x", "weibo", "xiaohongshu")
    if width == 1:
        for platform in platforms:
            docs.extend(check_channel(worker, r, target, event, platform, background))
    else:
        with ThreadPoolExecutor(max_workers=width, thread_name_prefix="evosight-public-sources") as pool:
            futures = [
                pool.submit(check_channel, worker, r, target, event, platform, background)
                for platform in platforms
            ]
            for future in futures:
                docs.extend(future.result())
    return list(dict.fromkeys(docs))


def check_channel(worker, r, target, event, channel, background):
    event_id = event["id"]
    with worker.store.connection() as c:
        prev = one(
            c,
            "SELECT document_ids FROM person_checks WHERE round_id=? AND target_id=? AND channel=?",
            (r["id"], target["id"], channel),
        )
        accounts = account_list(c, event_id, target)
        linked = [
            row[0]
            for row in c.execute(
                """SELECT url FROM person_post_links WHERE target_id=? AND platform=?
                             ORDER BY proof_doc DESC,url LIMIT 2""",
                (target["id"], channel),
            )
        ]
    if prev:
        return json.loads(prev["document_ids"])
    historical = background and (
        channel == "history" or not r["parent_snapshot"] or "人物专项" in json.loads(r["reasons"])
    )
    try:
        records, problems, fetched = scan(
            worker.acquisition, target, event, channel, accounts, r, historical, linked
        )
    except (OSError, ValueError, RuntimeError):
        records, problems, fetched = [], ["该来源本轮未能读取"], 0
    added = []
    outside = 0
    uncertain = 0
    with worker.store.connection(True) as c:
        if not workflow.goal_matches(c, r):
            return []
        for entry, res, ex, voice, author in records:
            when = ex.published_at
            if (when and when > r["window_end"]) or (
                not historical and (not when or not r["window_start"] < when <= r["window_end"])
            ):
                outside += 1
                continue
            period = "window" if when and r["window_start"] < when <= r["window_end"] else "history"
            tentative = voice == "账号归属待核"
            prior = one(
                c,
                "SELECT 1 FROM event_docs ed JOIN documents d ON d.id=ed.document_id WHERE ed.event_id=? AND d.url=?",
                (event_id, res.url),
            )
            doc = materials.attach_search_result(
                c,
                event_id,
                entry,
                res,
                ex,
                "person_candidate" if tentative else "backfill" if period == "history" else "window",
            )
            if channel in ("history", "news"):
                account_links(c, event_id, target, doc, res)
            fragment = one(
                c,
                """SELECT f.id FROM evidence_assets a JOIN fragments f ON f.asset_id=a.id
                      WHERE a.document_id=? AND a.url=? AND a.kind=? ORDER BY a.id DESC,f.id LIMIT 1""",
                (doc, materials.feeds.canonical(res.url), ex.kind),
            )
            if not fragment:
                continue
            saved = c.execute(
                """INSERT INTO person_materials(target_id,document_id,channel,source_url,fragment_id,attribution,author_key,source_at,period,round_id)
                    VALUES(?,?,?,?,?,?,?,?,?,?) ON CONFLICT(target_id,source_url,document_id) DO UPDATE SET attribution=excluded.attribution,
                    author_key=excluded.author_key,round_id=excluded.round_id,period=excluded.period
                    WHERE person_materials.attribution='账号归属待核' AND excluded.attribution!='账号归属待核'""",
                (target["id"], doc, channel, res.url, fragment["id"], voice, author, when, period, r["id"]),
            )
            if tentative:
                uncertain += 1
                if not prior:
                    c.execute(
                        "UPDATE event_docs SET analyzed=1 WHERE event_id=? AND document_id=? AND intake='person_candidate'",
                        (event_id, doc),
                    )
            elif saved.rowcount:
                added.append(doc)
                c.execute(
                    "UPDATE event_docs SET intake=?,analyzed=0 WHERE event_id=? AND document_id=? AND intake IN ('archive','person_candidate')",
                    ("backfill" if period == "history" else "window", event_id, doc),
                )
        state = (
            "partial"
            if problems and (fetched or records)
            else "unavailable"
            if problems
            else "found"
            if added
            else "checked"
        )
        detail = f"新收录 {len(set(added))} 份，归属待核 {uncertain} 份，窗口外或时间待核 {outside} 份"
        if problems:
            detail += "。" + "；".join(problems)
        elif not records:
            detail += "。本轮公开检索未找到可采用的材料"
        c.execute(
            "INSERT OR IGNORE INTO person_checks VALUES(?,?,?,?,?,?,?)",
            (r["id"], target["id"], channel, state, detail, utc(), dump(list(dict.fromkeys(added)))),
        )
    return added


def poll_known(worker, r):
    with worker.store.connection(True) as c:
        if not workflow.goal_matches(c, r):
            return []
        if r["parent_snapshot"]:
            for row in rows(
                c,
                """SELECT sc.claim_id FROM snapshot_claims sc JOIN claims k ON k.id=sc.claim_id
                    JOIN entities e ON e.id=k.entity_id WHERE sc.snapshot_id=? AND e.kind='person' AND k.role='people'
                    AND NOT EXISTS(SELECT 1 FROM person_targets t WHERE t.event_id=? AND t.entity_id=e.id) LIMIT 12""",
                (r["parent_snapshot"], r["event_id"]),
            ):
                adopt_claim(c, r, row["claim_id"])
        focus.match_people(c, r, roster(c, r["event_id"]))
        targets = targets_for_round(c, r)
        new_pins = {s["person_target_id"] for s in focus.applicable(c, r) if s["first_round_id"] == r["id"]}
    background = "人物专项" in json.loads(r["reasons"])
    return list(
        dict.fromkeys(
            d
            for t in targets
            for d in check_target(worker, r, t, background=background or t["id"] in new_pins)
        )
    )


def public_context(c, r):
    targets = current_targets(c, r)
    requested = sum(t["requested"] for t in targets)
    return [
        {
            "entity_id": t["entity_id"],
            "name": t["name"],
            "identity": t["identity"],
            "relevance": t["relevance"],
            "role": t["role"],
            "materials": rows(
                c,
                """SELECT m.document_id,m.fragment_id,m.source_url,m.attribution,m.channel,m.source_at,m.period,d.title
               FROM person_materials m JOIN documents d ON d.id=m.document_id WHERE m.target_id=? AND m.attribution!='账号归属待核'
               ORDER BY m.round_id DESC,m.document_id DESC LIMIT 8""",
                (t["id"],),
            ),
        }
        for t in targets[: max(8, requested)]
    ]


def view(c, event, target_id=0):
    targets = roster(c, event)
    requested = [f for f in focus.listing(c, event) if f["kind"] == "person" and f["active"]]
    pinned = {f["person_target_id"] for f in requested}
    for t in targets:
        t["aliases"] = json.loads(t["aliases"])
        t["requested"] = t["id"] in pinned
    recent_seed = one(
        c,
        """SELECT s.status FROM people_seed_checks s JOIN tracking_rounds r ON r.id=s.round_id
                     WHERE r.event_id=? ORDER BY r.id DESC LIMIT 1""",
        (event,),
    )
    result = {
        "people": targets,
        "pending_people": [f for f in requested if not f["person_target_id"]],
        "seed_status": recent_seed["status"] if recent_seed else "",
        "goal_version": workflow.current_goal(c, event)["version"],
    }
    if not target_id:
        return result
    target = next((t for t in targets if t["id"] == target_id), None)
    if not target:
        raise KeyError()
    accounts = account_list(c, event, target)
    docs = rows(
        c,
        """SELECT m.*,d.title,d.url,d.observed_at,substr(d.summary,1,400) AS excerpt FROM person_materials m JOIN documents d ON d.id=m.document_id
        WHERE target_id=? ORDER BY coalesce(m.source_at,d.observed_at) DESC,m.document_id DESC LIMIT 80""",
        (target_id,),
    )
    checks = rows(
        c,
        """SELECT k.*,r.window_start,r.window_end FROM person_checks k JOIN tracking_rounds r ON r.id=k.round_id
        WHERE k.target_id=? ORDER BY k.round_id DESC,k.channel LIMIT 25""",
        (target_id,),
    )
    snap = workflow.latest(c, event)
    claims = (
        [x for x in workflow.snapshot_claims(c, snap["id"]) if x["entity_id"] == target["entity_id"]]
        if snap
        else []
    )
    uncertain = {
        row[0]
        for row in c.execute(
            "SELECT fragment_id FROM person_materials WHERE target_id=? AND attribution='账号归属待核'",
            (target_id,),
        )
    }
    for claim in claims:
        claim["attribution_review"] = any(cite["fragment_id"] in uncertain for cite in claim["citations"])
    return {
        **result,
        "person": target,
        "accounts": accounts,
        "documents": docs,
        "checks": checks,
        "claims": claims,
    }
