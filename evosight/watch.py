"""Share public-source polling across events while keeping their research watermarks separate."""
import json
import logging
import re
import threading
import uuid
from datetime import datetime, timedelta

from . import feeds, materials, resources, workflow
from .db import DispatchDeferred, config, dump, later, one, require, rows, utc
from .materials import Source, relevance, safe_link

log = logging.getLogger("evosight.watch")


def sources(c, event):
    return [
        Source(x["url"], x["kind"], x["name"], x["reason"], x["origin"])
        for x in rows(
            c,
            """SELECT p.*,ep.origin,ep.reason FROM event_platforms ep JOIN platforms p ON p.url=ep.url
           WHERE ep.event_id=? AND ep.enabled=1 ORDER BY ep.pinned DESC,ep.watching DESC,ep.rowid""",
            (event,),
        )
    ]


def choose(store, acq, event, rid, refresh=False, scout=None, empty_sources=()):
    with store.connection() as c:
        existing = sources(c, event)
        unavailable = {
            x[0]
            for x in c.execute(
                """SELECT p.url FROM platforms p JOIN event_platforms e ON p.url=e.url
                       WHERE e.event_id=? AND e.enabled=1 AND p.failures>0""",
                (event,),
            )
        }
        if (
            existing
            and not refresh
            and not empty_sources
            and len(unavailable) < len(existing)
            and any(
                one(
                    c,
                    "SELECT 1 FROM platforms WHERE url=? AND success_at IS NOT NULL AND failures<3",
                    (x.url,),
                )
                for x in existing
            )
        ):
            return existing
        e = require(c, "events", event)
        raw = rows(
            c,
            """SELECT feed FROM source_state WHERE event_id=? AND feed NOT IN
                      (SELECT url FROM event_platforms WHERE event_id=?)""",
            (event, event),
        )
        seeds = [s for s in existing if s.origin == "用户指定"]
        seeds.extend(Source(x["feed"], "feed", x["feed"], "用户为该事件提供的发布入口", "用户指定") for x in raw)
    if empty_sources:
        found = existing
        attempts = [(url, "本轮首次检查未找到与目标匹配的材料，另找发布入口") for url in empty_sources]
    else:
        found, attempts = acq.discover(e["title"], e["scope"], seeds, force_search=bool(unavailable))
    if scout and (empty_sources or not any(s.url not in unavailable for s in found)):
        try:
            suggestion = scout(
                {
                    "title": e["title"],
                    "scope": e["scope"],
                    "previous_attempts": attempts,
                    "unavailable": sorted(unavailable),
                }
            )
        except (DispatchDeferred, resources.ResourcePressure):
            raise
        except (ValueError, RuntimeError, OSError):
            attempts.append(("探索 Agent", "本次未取得可用的查源建议，下轮可重试"))
        else:
            extra, logs = acq.scout_sources(e["title"], e["scope"], suggestion)
            found = list({s.url: s for s in [*extra, *found]}.values())
            attempts.extend(logs or [("探索 Agent", "已完成查源，尚未找到可核实的发布入口")])
    if not refresh:
        # Keep previously verified subscriptions eligible for recovery after a temporary outage.
        found_urls = {candidate.url for candidate in found}
        found.extend(s for s in existing if s.url not in found_urls)
    found = sorted(found, key=lambda s: s.url in unavailable)
    with store.connection(True) as c:
        r = require(c, "tracking_rounds", rid)
        if not workflow.goal_matches(c, r):
            return []
        maximum = config(c)["watch_sources"]
        c.execute("UPDATE event_platforms SET watching=0,enabled=0 WHERE event_id=? AND pinned=0", (event,))
        for rank, s in enumerate(found):
            c.execute(
                "INSERT INTO platforms(url,kind,name,next_poll) VALUES(?,?,?,?) ON CONFLICT(url) DO NOTHING",
                (s.url, s.kind, s.name, utc()),
            )
            cursor = one(
                c, "SELECT coalesce(max(id),0) AS n FROM platform_entries WHERE platform=?", (s.url,)
            )["n"]
            c.execute(
                """INSERT INTO event_platforms(event_id,url,reason,origin,watching,watch_cursor) VALUES(?,?,?,?,?,?)
                         ON CONFLICT(event_id,url) DO UPDATE SET reason=excluded.reason,enabled=1,
                         watching=CASE WHEN pinned=1 THEN event_platforms.watching ELSE excluded.watching END""",
                (event, s.url, s.reason, s.origin, int(rank < maximum), cursor),
            )
            c.execute("INSERT OR IGNORE INTO source_state(event_id,feed) VALUES(?,?)", (event, s.url))
        c.executemany(
            "INSERT INTO discovery_log(event_id,round_id,url,reason,state,created_at) VALUES(?,?,?,?,?,?)",
            [(event, rid, url, detail, "查源", utc()) for url, detail in attempts],
        )
    return found


def apply_plan(store, acquisition, r, plan):
    event, rid = r["event_id"], r["id"]
    with store.connection() as c:
        if one(c, "SELECT 1 FROM round_monitors WHERE round_id=?", (rid,)) or not workflow.goal_matches(c, r):
            return
        goal = workflow.current_goal(c, event)
        recent = {
            row[0]
            for row in c.execute(
                "SELECT url FROM discovery_log WHERE event_id=? AND state='规划查源' AND created_at>?",
                (event, later(-21600)),
            )
        }
    found = {}
    attempts = []
    for query in dict.fromkeys(str(s).strip()[:250] for s in plan.get("source_queries", [])):
        if not query or query in recent:
            continue
        choices, log_entries = acquisition.discover(goal["title"], goal["scope"], [], query=query)
        for choice in choices[:2]:
            found.setdefault(choice.url, choice)
        attempts.extend(log_entries)
    with store.connection(True) as c:
        if not workflow.goal_matches(c, r) or one(c, "SELECT 1 FROM round_monitors WHERE round_id=?", (rid,)):
            return
        active = {s.url: s for s in sources(c, event)}
        new = []
        for url, choice in found.items():
            if url in active:
                continue
            c.execute(
                "INSERT OR IGNORE INTO platforms(url,kind,name,next_poll) VALUES(?,?,?,?)",
                (url, choice.kind, choice.name, utc()),
            )
            if one(c, "SELECT 1 FROM event_platforms WHERE event_id=? AND url=? AND pinned=1", (event, url)):
                continue
            c.execute(
                """INSERT INTO event_platforms(event_id,url,reason,origin,watching) VALUES(?,?,?,'规划补源',0)
                      ON CONFLICT(event_id,url) DO UPDATE SET enabled=1,reason=excluded.reason""",
                (event, url, choice.reason),
            )
            c.execute("INSERT OR IGNORE INTO source_state(event_id,feed) VALUES(?,?)", (event, url))
            active[url] = choice
            new.append(url)
        picks = {}
        for item in plan.get("watch", []):
            url = safe_link("", item["url"])
            if url in active:
                picks.setdefault(url, item["reason"])
        if picks or new:
            pinned = one(
                c,
                "SELECT count(*) AS n FROM event_platforms WHERE event_id=? AND enabled=1 AND pinned=1 AND watching=1",
                (event,),
            )["n"]
            free = max(0, config(c)["watch_sources"] - pinned)
            c.execute(
                "UPDATE event_platforms SET watching=0 WHERE event_id=? AND enabled=1 AND pinned=0", (event,)
            )
            order = list(dict.fromkeys([*picks, *new, *active]))
            for url in order:
                if free <= 0:
                    break
                changed = c.execute(
                    """UPDATE event_platforms SET watching=1,reason=? WHERE event_id=? AND url=? AND pinned=0 AND enabled=1""",
                    (picks.get(url, active[url].reason), event, url),
                ).rowcount
                free -= changed
        c.executemany(
            "INSERT INTO discovery_log(event_id,round_id,url,reason,state,created_at) VALUES(?,?,?,?,'规划查源',?)",
            [(event, rid, url, detail, utc()) for url, detail in attempts],
        )
        c.execute("INSERT INTO round_monitors VALUES(?,?,?)", (rid, len(new), utc()))


class Watcher:
    def __init__(self, store, acquisition):
        self.store = store
        self.acquisition = acquisition
        self.stop = threading.Event()

    def poll(self, source, *, fresh=False):
        now = utc()
        owner = uuid.uuid4().hex
        with self.store.connection(True) as c:
            p = one(c, "SELECT * FROM platforms WHERE url=?", (source.url,))
            if not p:
                return False
            if p["lease_until"] and p["lease_until"] > now:
                return bool(p["success_at"] and not p["error"])
            if not fresh and p["checked_at"] and p["next_poll"] > now:
                return bool(p["success_at"] and not p["error"])
            since = p["success_at"]
            c.execute(
                "UPDATE platforms SET lease_owner=?,lease_until=? WHERE url=?",
                (owner, later(120), source.url),
            )
        try:
            page = self.acquisition.poll(source, p)
            error = ""
        except Exception as exc:
            error = str(exc)[:220] if isinstance(exc, ValueError) else "发布方暂不可访问，稍后重新检查"
        with self.store.connection(True) as c:
            if not one(c, "SELECT 1 FROM platforms WHERE url=? AND lease_owner=?", (source.url, owner)):
                return False
            interval = config(c)["watch_minutes"] * 60
            if error:
                c.execute(
                    """UPDATE platforms SET checked_at=?,error=?,failures=failures+1,next_poll=?,lease_owner=NULL,lease_until=NULL WHERE url=?""",
                    (now, error, later(min(21600, interval * 2 ** min(5, p["failures"]))), source.url),
                )
                return False
            if source.kind == "section":
                feeds.reconcile_section(c, source.url)
            for entry in reversed(page.entries):
                doc = feeds.ingest(c, None, source.url, entry, intake=feeds.intake_period(entry, since, now))
                if (
                    page.original
                    and entry.get("evidence_locator")
                    and not one(
                        c,
                        "SELECT 1 FROM evidence_assets WHERE document_id=? AND url=? AND extractor='publisher-record-v1'",
                        (doc, page.original.url),
                    )
                ):
                    source_page = page.original
                    parsed = materials.Extract(
                        source.kind, [(entry["evidence_locator"], entry["summary"], source_page.url)]
                    )
                    materials.save_asset(
                        c,
                        doc,
                        source_page.url,
                        source.kind,
                        source_page.body,
                        source_page.mime,
                        parsed,
                        extractor="publisher-record-v1",
                    )
                c.execute(
                    "INSERT OR IGNORE INTO platform_entries(platform,document_id,data,seen_at) VALUES(?,?,?,?)",
                    (source.url, doc, dump(entry), now),
                )
            c.execute(
                """UPDATE platforms SET checked_at=?,success_at=?,resource=?,etag=?,modified=?,failures=0,error='',
                         next_poll=?,lease_owner=NULL,lease_until=NULL WHERE url=?""",
                (now, now, page.resource, page.etag, page.modified, later(interval), source.url),
            )
            if not since:
                c.execute(
                    """UPDATE event_platforms SET watch_cursor=(SELECT coalesce(max(id),0) FROM platform_entries WHERE platform=?)
                             WHERE url=?""",
                    (source.url, source.url),
                )
        return True

    def cycle(self):
        with self.store.connection() as c:
            if config(c)["global_pause"]:
                return 0
            due = rows(
                c,
                """SELECT DISTINCT p.* FROM platforms p JOIN event_platforms ep ON ep.url=p.url
                         JOIN events e ON e.id=ep.event_id WHERE ep.enabled=1 AND e.status='active' AND e.demo=0
                         AND (ep.watching=1 OR EXISTS (
                             SELECT 1 FROM round_sources rs JOIN tracking_rounds r ON r.id=rs.round_id
                             WHERE rs.url=p.url AND r.event_id=e.id AND r.state!='superseded'
                               AND r.goal_version=(SELECT max(version) FROM event_goals WHERE event_id=e.id)
                               AND rs.state='missing' AND rs.recovery_round IS NULL))
                         AND p.next_poll<=? AND (lease_until IS NULL OR lease_until<=?) ORDER BY p.next_poll LIMIT 8""",
                (utc(), utc()),
            )
        for p in due:
            self.poll(Source(p["url"], p["kind"], p["name"], "巡检", "平台"))
        # Also deliver entries ingested by a simultaneous six-hour collection.
        with self.store.connection(True) as c:
            subscriptions = rows(
                c,
                """SELECT ep.*,e.title,e.scope FROM event_platforms ep JOIN events e ON e.id=ep.event_id
                                    WHERE ep.watching=1 AND e.status='active' AND e.demo=0 AND EXISTS
                                    (SELECT 1 FROM platform_entries pe WHERE pe.platform=ep.url AND pe.id>ep.watch_cursor)""",
            )
            for sub in subscriptions:
                self.signal(c, sub)
            missing = rows(
                c,
                """SELECT rs.*,r.event_id FROM round_sources rs JOIN tracking_rounds r ON r.id=rs.round_id
                      JOIN events e ON e.id=r.event_id JOIN platforms p ON p.url=rs.url
                      JOIN event_platforms ep ON ep.event_id=e.id AND ep.url=p.url AND ep.enabled=1
                      WHERE rs.state='missing' AND rs.recovery_round IS NULL AND e.status='active' AND e.demo=0
                        AND r.goal_version=(SELECT max(version) FROM event_goals WHERE event_id=e.id)
                        AND p.success_at>rs.checked_at AND p.error='' AND r.state!='superseded' LIMIT 8""",
            )
            for gap in missing:
                rid, _ = workflow.request_round(
                    c,
                    gap["event_id"],
                    "补采缺失窗口",
                    start=gap["window_start"],
                    end=gap["window_end"],
                    key=f"missing/{gap['round_id']}/{gap['url']}",
                )
                c.execute(
                    "UPDATE round_sources SET recovery_round=? WHERE round_id=? AND url=?",
                    (rid, gap["round_id"], gap["url"]),
                )
        return len(due)

    @staticmethod
    def signal(c, sub):
        event = sub["event_id"]
        entries = rows(
            c,
            "SELECT * FROM platform_entries WHERE platform=? AND id>? ORDER BY id LIMIT 300",
            (sub["url"], sub["watch_cursor"]),
        )
        for row in entries:
            art = json.loads(row["data"])
            title = art["title"]
            score = relevance(sub["title"], sub["scope"], title + " " + art["summary"][:250])
            primary = sub["origin"] == "用户指定" or "直接" in sub["reason"]
            important = re.search(
                r"正式|宣布|确认|通过|决定|生效|撤销|暂停|开通|发布|召回|预警|breaking|released?|launch|approve|announce|^v?\d+\.\d+",
                title,
                re.I,
            )
            action = "full" if important and (score >= 3 or primary) else "local" if score >= 4 else "store"
            stamp = art.get("published_at") or art.get("source_updated_at")
            if stamp and (stamp > utc() or stamp < later(-21600)):
                action = "store"
            normalized = re.sub(r"^(?:转载|转发|快讯|消息|更新)[\s：:｜|·-]*", "", title).casefold()
            content = re.sub(r"[\W_]", "", art["summary"]).casefold()
            signal_key = workflow.fingerprint(
                content[:650] if len(content) >= 100 else re.sub(r"[\W_]", "", normalized)
            )
            found = one(
                c,
                "SELECT id FROM watch_signals WHERE event_id=? AND (signal_key=? OR document_id=?)",
                (event, signal_key, row["document_id"]),
            )
            if found:
                continue
            rid = None
            if action != "store" and workflow.latest(c, event):
                previous = one(
                    c,
                    "SELECT * FROM watch_signals WHERE event_id=? AND round_id IS NOT NULL ORDER BY id DESC LIMIT 1",
                    (event,),
                )
                reason = "重大公告" if action == "full" else "局部线索"
                if previous and previous["seen_at"] >= later(-1800) and action == "local":
                    action = "store"
                else:
                    rid, _ = workflow.request_round(c, event, reason, key="signal/" + signal_key)
            c.execute(
                "INSERT INTO watch_signals(event_id,document_id,signal_key,action,reason,seen_at,round_id) VALUES(?,?,?,?,?,?,?)",
                (
                    event,
                    row["document_id"],
                    signal_key,
                    action,
                    f"{sub['reason']} · 主题词匹配 {score}",
                    utc(),
                    rid,
                ),
            )
        if entries:
            c.execute(
                "UPDATE event_platforms SET watch_cursor=? WHERE event_id=? AND url=?",
                (entries[-1]["id"], event, sub["url"]),
            )

    def loop(self):
        while not self.stop.is_set():
            try:
                self.cycle()
            except Exception:
                log.exception("巡检未完成")
            self.stop.wait(10)


def take_entries(c, event, source, r, ok, *, from_archive=False):
    state = one(c, "SELECT * FROM source_state WHERE event_id=? AND feed=?", (event, source.url))
    p = one(c, "SELECT * FROM platforms WHERE url=?", (source.url,))
    sub = one(c, "SELECT * FROM event_platforms WHERE event_id=? AND url=?", (event, source.url))
    if not sub or not state:
        return []
    if not from_archive:
        c.execute(
            "UPDATE source_state SET last_attempt=?,error=? WHERE event_id=? AND feed=?",
            (utc(), None if ok else p["error"] or "检查尚未完成", event, source.url),
        )
    if not ok:
        return []
    e = require(c, "events", event)
    records = rows(
        c,
        "SELECT * FROM platform_entries WHERE platform=? AND id>? ORDER BY id",
        (source.url, sub["consumed"]),
    )
    candidates = []
    for row in records:
        art = json.loads(row["data"])
        intake = feeds.intake_period(art, state["last_success"], r["window_end"])
        doc = row["document_id"]
        score = relevance(e["title"], e["scope"], art["title"] + " " + art["summary"][:350])
        directed = source.origin == "用户指定" or "直接" in source.reason or source.kind == "page"
        if not directed and not score:
            continue
        c.execute(
            "INSERT OR IGNORE INTO event_docs(event_id,document_id,intake) VALUES(?,?,?)",
            (event, doc, intake),
        )
        if intake != "future":
            candidates.append((score, doc))
    if records:
        c.execute(
            "UPDATE event_platforms SET consumed=? WHERE event_id=? AND url=?",
            (records[-1]["id"], event, source.url),
        )
    c.execute(
        "UPDATE source_state SET last_success=? WHERE event_id=? AND feed=?",
        (p["success_at"], event, source.url),
    )
    return [doc for _, doc in sorted(candidates, reverse=True)]
