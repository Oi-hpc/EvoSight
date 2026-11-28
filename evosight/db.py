"""SQLite transactions, immutable event history, and per-event reading progress."""
from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import sqlite3
from contextlib import closing, contextmanager
from datetime import datetime, timedelta, timezone
from contextvars import ContextVar
from pathlib import Path


job_lease = ContextVar("evosight_job_lease", default=None)


def utc(value: datetime | None = None) -> str:
    return (
        (value or datetime.now(timezone.utc))
        .astimezone(timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )


def instant(value: str) -> str:
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        raise ValueError("时间必须包含时区")
    return utc(dt)


def later(seconds: int) -> str:
    return utc(datetime.now(timezone.utc) + timedelta(seconds=seconds))


def dump(value) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def rows(c, sql, args=()):
    return [dict(r) for r in c.execute(sql, args)]


def one(c, sql, args=()):
    row = c.execute(sql, args).fetchone()
    return dict(row) if row is not None else None


def require(c, table, ident):
    record = one(c, "SELECT * FROM " + table + " WHERE id=?", (ident,))
    if record is None or (table == "events" and record["status"] == "deleted"):
        raise KeyError("记录不存在")
    return record


SCHEMA = """
CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS topics(id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE, feeds TEXT NOT NULL, keywords TEXT NOT NULL, discovery INTEGER NOT NULL DEFAULT 0, next_run TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY, title TEXT NOT NULL COLLATE NOCASE, scope TEXT NOT NULL, topic_id INTEGER REFERENCES topics, status TEXT NOT NULL DEFAULT 'active', demo INTEGER NOT NULL DEFAULT 0, cadence INTEGER NOT NULL DEFAULT 6, created_at TEXT NOT NULL, next_run TEXT NOT NULL, last_checked TEXT, pinned_prediction INTEGER);
CREATE TABLE IF NOT EXISTS source_state(event_id INTEGER NOT NULL REFERENCES events, feed TEXT NOT NULL, last_success TEXT, last_attempt TEXT, error TEXT, PRIMARY KEY(event_id,feed));
CREATE TABLE IF NOT EXISTS documents(id INTEGER PRIMARY KEY, url TEXT NOT NULL, digest TEXT NOT NULL, title TEXT NOT NULL, summary TEXT NOT NULL, published_at TEXT, observed_at TEXT NOT NULL, supersedes INTEGER REFERENCES documents, source_updated_at TEXT, published_label TEXT, source_times_checked INTEGER NOT NULL DEFAULT 0, UNIQUE(url,digest));
CREATE INDEX IF NOT EXISTS doc_digest ON documents(digest);
CREATE TABLE IF NOT EXISTS mentions(document_id INTEGER NOT NULL REFERENCES documents, feed TEXT NOT NULL, url TEXT NOT NULL, UNIQUE(document_id,feed,url));
CREATE TABLE IF NOT EXISTS event_docs(event_id INTEGER NOT NULL REFERENCES events, document_id INTEGER NOT NULL REFERENCES documents, analyzed INTEGER NOT NULL DEFAULT 0, intake TEXT NOT NULL DEFAULT 'initial', PRIMARY KEY(event_id,document_id));
CREATE INDEX IF NOT EXISTS events_for_document ON event_docs(document_id,event_id);
CREATE TABLE IF NOT EXISTS rss_entries(feed TEXT NOT NULL, entry_key TEXT NOT NULL, document_id INTEGER NOT NULL REFERENCES documents, PRIMARY KEY(feed,entry_key));
CREATE TABLE IF NOT EXISTS rss_validators(event_id INTEGER NOT NULL, feed TEXT NOT NULL, resource TEXT NOT NULL, etag TEXT, modified TEXT, PRIMARY KEY(event_id,feed), FOREIGN KEY(event_id,feed) REFERENCES source_state(event_id,feed));
CREATE TABLE IF NOT EXISTS items(id INTEGER PRIMARY KEY, event_id INTEGER NOT NULL REFERENCES events, name TEXT NOT NULL, pinned INTEGER NOT NULL DEFAULT 0, UNIQUE(event_id,name));
CREATE TABLE IF NOT EXISTS nodes(id INTEGER PRIMARY KEY, event_id INTEGER NOT NULL REFERENCES events, kind TEXT NOT NULL, title TEXT NOT NULL, body TEXT NOT NULL, watch TEXT NOT NULL, refs TEXT NOT NULL, created_at TEXT NOT NULL, occurred_at TEXT, item_id INTEGER REFERENCES items, prediction_id INTEGER);
CREATE INDEX IF NOT EXISTS event_nodes ON nodes(event_id,id);
CREATE TABLE IF NOT EXISTS reads(event_id INTEGER PRIMARY KEY REFERENCES events, seen INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS predictions(id INTEGER PRIMARY KEY, event_id INTEGER NOT NULL REFERENCES events, question TEXT NOT NULL, deadline TEXT NOT NULL, rule TEXT NOT NULL, authority TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'open', cohort TEXT NOT NULL DEFAULT 'auto', created_at TEXT NOT NULL, UNIQUE(event_id,question,deadline));
CREATE TABLE IF NOT EXISTS revisions(id INTEGER PRIMARY KEY, prediction_id INTEGER NOT NULL REFERENCES predictions, p INTEGER NOT NULL CHECK(p BETWEEN 0 AND 100), reason TEXT NOT NULL, refs TEXT NOT NULL, created_at TEXT NOT NULL, model TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS resolutions(id INTEGER PRIMARY KEY, prediction_id INTEGER NOT NULL REFERENCES predictions, outcome TEXT NOT NULL, public_at TEXT, reason TEXT NOT NULL, refs TEXT NOT NULL, scores TEXT NOT NULL, lesson TEXT NOT NULL, created_at TEXT NOT NULL, actor TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS candidates(id INTEGER PRIMARY KEY, event_id INTEGER REFERENCES events, topic_id INTEGER REFERENCES topics, title TEXT NOT NULL, scope TEXT NOT NULL, reason TEXT NOT NULL, refs TEXT NOT NULL, feeds TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'review', target_id INTEGER REFERENCES events, created_at TEXT NOT NULL, UNIQUE(title));
CREATE TABLE IF NOT EXISTS forecast_notes(id INTEGER PRIMARY KEY,event_id INTEGER REFERENCES events,decision TEXT NOT NULL, reason TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS fixed_questions(id INTEGER PRIMARY KEY,event_id INTEGER NOT NULL REFERENCES events,question TEXT NOT NULL,rule TEXT NOT NULL,authority TEXT NOT NULL,deadline TEXT NOT NULL,created_at TEXT NOT NULL,prediction_id INTEGER REFERENCES predictions,reason TEXT NOT NULL DEFAULT '');
CREATE TABLE IF NOT EXISTS jobs(id INTEGER PRIMARY KEY, kind TEXT NOT NULL,event_id INTEGER REFERENCES events,topic_id INTEGER REFERENCES topics,args TEXT NOT NULL,status TEXT NOT NULL,created_at TEXT NOT NULL,started_at TEXT,finished_at TEXT,lease TEXT,owner TEXT,attempts INTEGER NOT NULL DEFAULT 0,info TEXT NOT NULL DEFAULT '',retry_at TEXT, dedupe TEXT NOT NULL, slot TEXT UNIQUE);
CREATE UNIQUE INDEX IF NOT EXISTS active_jobs ON jobs(dedupe) WHERE status IN ('queued','running');
CREATE TABLE IF NOT EXISTS runs(id INTEGER PRIMARY KEY,job_id INTEGER REFERENCES jobs,role TEXT NOT NULL,model TEXT NOT NULL,effort TEXT NOT NULL,created_at TEXT NOT NULL,status TEXT NOT NULL,input_tokens INTEGER,output_tokens INTEGER,elapsed REAL);
CREATE INDEX IF NOT EXISTS runs_time ON runs(created_at);
CREATE TABLE IF NOT EXISTS job_results(job_id INTEGER PRIMARY KEY REFERENCES jobs, info TEXT NOT NULL);
"""

DEFAULTS = {
    "agent_backend": "codex",
    "news_agent_backend": "dsh",
    "max_agent_concurrency": 2,
    "daily_calls": 1000,
    "monthly_calls": 30000,
    "verify_reserve": 2,
    "capacity": 20,
    "auto_limit": 2,
    "global_pause": False,
    "watch_minutes": 10,
    "watch_sources": 2,
}


def allow_event_title_reuse(c):
    """Preserve event IDs and references while making names unique among tracked events."""
    c.execute("BEGIN IMMEDIATE")
    try:
        schema = c.execute("SELECT sql FROM sqlite_master WHERE name='events' AND type='table'").fetchone()[0]
        title = "title TEXT NOT NULL COLLATE NOCASE"
        if title + " UNIQUE" in schema:
            indexes = list(c.execute(
                "SELECT sql FROM sqlite_master WHERE tbl_name='events' AND type IN ('index','trigger') AND sql IS NOT NULL"
            ))
            replacement = schema.replace("events", "events_upgrade", 1).replace(title + " UNIQUE", title, 1)
            c.execute(replacement)
            c.execute("INSERT INTO events_upgrade SELECT * FROM events")
            c.execute("DROP TABLE events")
            c.execute("ALTER TABLE events_upgrade RENAME TO events")
            for (statement,) in indexes:
                c.execute(statement)
            if c.execute("PRAGMA foreign_key_check").fetchone():
                raise ValueError("事件迁移未通过外键核对")
        c.execute("CREATE UNIQUE INDEX IF NOT EXISTS event_title_in_use ON events(title) WHERE status!='deleted'")
        c.commit()
    except BaseException:
        c.rollback()
        raise


class Store:
    def __init__(self, directory):
        self.directory = Path(directory).resolve()
        self.directory.mkdir(parents=True, exist_ok=True)
        self.path = self.directory / "evosight.sqlite3"
        with closing(sqlite3.connect(self.path, isolation_level=None)) as initial:
            initial.execute("PRAGMA journal_mode=WAL")
            initial.execute("PRAGMA foreign_keys=OFF")
            initial.executescript(SCHEMA)
            initial.executescript(Path(__file__).with_name("workflow.sql").read_text(encoding="utf-8"))
            initial.executescript(Path(__file__).with_name("skills.sql").read_text(encoding="utf-8"))
            allow_event_title_reuse(initial)
            from .discussion import allow_workspace_turns

            allow_workspace_turns(initial)
        with self.connection(write=True) as c:
            from . import world

            world.prepare_schema(c)
            session_columns = {r["name"] for r in c.execute("PRAGMA table_info(agent_sessions)")}
            if "backend" not in session_columns:
                c.execute("ALTER TABLE agent_sessions ADD COLUMN backend TEXT CHECK(backend IN ('codex','dsh'))")
                c.execute(
                    """UPDATE agent_sessions SET backend=CASE WHEN r.model LIKE 'dashscope/%' THEN 'dsh' ELSE 'codex' END
                       FROM runs r WHERE r.id=COALESCE(agent_sessions.last_run,agent_sessions.busy_run)"""
                )
                c.execute("UPDATE agent_sessions SET backend='codex' WHERE backend IS NULL AND thread_id IS NOT NULL")
            if "dsh_session_id" not in session_columns:
                c.execute("ALTER TABLE agent_sessions ADD COLUMN dsh_session_id TEXT")
            if "grace_hours" not in {r["name"] for r in c.execute("PRAGMA table_info(predictions)")}:
                c.execute("ALTER TABLE predictions ADD COLUMN grace_hours INTEGER NOT NULL DEFAULT 72")
            doc_columns = {r["name"] for r in c.execute("PRAGMA table_info(documents)")}
            if "source_updated_at" not in doc_columns:
                c.execute("ALTER TABLE documents ADD COLUMN source_updated_at TEXT")
            if "published_label" not in doc_columns:
                c.execute("ALTER TABLE documents ADD COLUMN published_label TEXT")
            if "source_times_checked" not in doc_columns:
                c.execute("ALTER TABLE documents ADD COLUMN source_times_checked INTEGER NOT NULL DEFAULT 0")
            if "intake" not in {r["name"] for r in c.execute("PRAGMA table_info(event_docs)")}:
                c.execute("ALTER TABLE event_docs ADD COLUMN intake TEXT NOT NULL DEFAULT 'initial'")
            c.execute(
                """INSERT OR IGNORE INTO event_goals(event_id,version,title,scope,created_at)
                         SELECT id,1,title,scope,created_at FROM events"""
            )
            for key, val in {**DEFAULTS, "read_key": secrets.token_hex(32)}.items():
                c.execute("INSERT OR IGNORE INTO settings VALUES(?,?)", (key, dump(val)))
            from .workflow import migrate_existing_knowledge, refresh_shared_indexes

            world.migrate(c)
            migrate_existing_knowledge(c)
            from . import board

            board.migrate(c)
            from .discussion import link_archived_runs

            link_archived_runs(c)
            stale_heads = [
                row[0]
                for row in c.execute(
                    """SELECT DISTINCT w.head_id FROM world_entries w
                JOIN world_versions v ON v.entry_id=w.id JOIN snapshot_claims s ON s.claim_id=v.claim_id
                JOIN knowledge_snapshots n ON n.id=s.snapshot_id WHERE v.canonical_id!=w.head_id
                AND n.id=(SELECT max(id) FROM knowledge_snapshots WHERE event_id=n.event_id)"""
                )
            ]
            refresh_shared_indexes(c, 0, stale_heads)

    @contextmanager
    def connection(self, write=False):
        c = sqlite3.connect(self.path, timeout=15, isolation_level=None)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA foreign_keys=ON")
        try:
            c.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            lease = job_lease.get() if write else None
            if (
                lease
                and lease[0] == self.path
                and not c.execute(
                    """SELECT 1 FROM jobs j LEFT JOIN events e ON e.id=j.event_id
                       WHERE j.id=? AND j.owner=? AND j.status='running' AND j.lease>=?
                       AND (e.id IS NULL OR e.status!='deleted')""",
                    (lease[1], lease[2], utc()),
                ).fetchone()
            ):
                raise ValueError("任务租约已失效，旧执行者停止提交")
            yield c
            c.commit()
        except BaseException:
            c.rollback()
            raise
        finally:
            c.close()


def config(c):
    return {
        r["key"]: json.loads(r["value"]) for r in c.execute("SELECT * FROM settings WHERE key!='read_key'")
    }


def add_event(c, title, scope, feeds, *, topic_id=None, demo=False, status="active", cadence=6):
    if not title.strip() or not scope.strip():
        raise ValueError("请填写事件名称和研究问题")
    if (
        status == "active"
        and not demo
        and c.execute("SELECT count(*) FROM events WHERE status='active' AND demo=0").fetchone()[0]
        >= config(c)["capacity"]
    ):
        raise ValueError("活跃追踪容量已满")
    ident = c.execute(
        "INSERT INTO events(title,scope,topic_id,status,demo,cadence,created_at,next_run) VALUES(?,?,?,?,?,?,?,?)",
        (title.strip(), scope.strip(), topic_id, status, int(demo), cadence, utc(), utc()),
    ).lastrowid
    c.executemany(
        "INSERT INTO source_state(event_id,feed) VALUES(?,?)", [(ident, f) for f in dict.fromkeys(feeds)]
    )
    c.execute(
        "INSERT INTO event_goals(event_id,version,title,scope,created_at) VALUES(?,1,?,?,?)",
        (ident, title.strip(), scope.strip(), utc()),
    )
    return ident


def node(
    c, event, kind, title, body="", *, watch="", refs=(), item=None, prediction=None, at=None, occurred=None
):
    return c.execute(
        "INSERT INTO nodes(event_id,kind,title,body,watch,refs,created_at,occurred_at,item_id,prediction_id) VALUES(?,?,?,?,?,?,?,?,?,?)",
        (event, kind, title, body, watch, dump(list(refs)), at or utc(), occurred, item, prediction),
    ).lastrowid


def expand_node(row):
    if row:
        row["refs"] = json.loads(row["refs"])
    return row


def read_token(c, event, seq):
    text = f"{event}.{seq}"
    key = json.loads(c.execute("SELECT value FROM settings WHERE key='read_key'").fetchone()[0])
    return f"{text}.{hmac.new(key.encode(),text.encode(),hashlib.sha256).hexdigest()}"


def mark_read(c, event, token):
    try:
        eid, seq, _ = token.split(".")
        upper = int(seq)
        if int(eid) != event or upper < 0 or not hmac.compare_digest(token, read_token(c, event, upper)):
            raise ValueError()
    except (ValueError, TypeError):
        raise ValueError("该阅读范围无效") from None
    require(c, "events", event)
    c.execute(
        "INSERT INTO reads VALUES(?,?) ON CONFLICT(event_id) DO UPDATE SET seen=max(seen,excluded.seen)",
        (event, upper),
    )


def enqueue(c, kind, *, event=None, topic=None, args=None, slot=None):
    if event is not None:
        require(c, "events", event)
    args = args or {}
    dedupe = f"{kind}:{event or 0}:{topic or 0}:{args.get('prediction_id',0)}:{args.get('fixed_id',0)}"
    for field in ("round_id", "platform", "snapshot_id", "task_id", "turn_id", "dispatch_key"):
        if args.get(field):
            dedupe += f":{field}={args[field]}"
    existing = one(c, "SELECT id FROM jobs WHERE dedupe=? AND status IN ('queued','running')", (dedupe,))
    if existing:
        return existing["id"]
    if slot:
        existing = one(c, "SELECT id FROM jobs WHERE slot=?", (slot,))
        if existing:
            return existing["id"]
    return c.execute(
        "INSERT INTO jobs(kind,event_id,topic_id,args,status,created_at,dedupe,slot) VALUES(?,?,?,?,'queued',?,?,?)",
        (kind, event, topic, dump(args), utc(), dedupe, slot),
    ).lastrowid


def usage(c):
    today = utc()[:10]
    return dict(
        c.execute(
            "SELECT count(CASE WHEN substr(created_at,1,10)=? THEN 1 END) AS daily_used,count(*) AS monthly_used, coalesce(sum(input_tokens),0) AS input_tokens, coalesce(sum(output_tokens),0) AS output_tokens FROM runs WHERE substr(created_at,1,7)=?",
            (today, today[:7]),
        ).fetchone()
    )


class DispatchDeferred(ValueError):
    """A local scheduling setting can release this job without another submission."""


def book_run(c, job, role, model, effort):
    limits, used = config(c), usage(c)
    if limits["global_pause"]:
        raise DispatchDeferred("调度已暂停，恢复调度后任务会自动继续")
    reserve = 0 if role == "verify" else limits["verify_reserve"]
    reached = [
        f"{label}已用 {used[key + '_used']}/{limits[key + '_calls']} 次"
        for key, label in (("daily", "今日"), ("monthly", "本月"))
        if used[key + "_used"] >= max(0, limits[key + "_calls"] - reserve)
    ]
    if reached:
        details = "，".join(reached)
        reserved = f"，其中 {reserve} 次预留给核验" if reserve else ""
        raise DispatchDeferred(f"{details}{reserved}。调高上限或额度重置后，任务会自动继续")
    return c.execute(
        "INSERT INTO runs(job_id,role,model,effort,created_at,status) VALUES(?,?,?,?,?,'running')",
        (job, role, model, effort, utc()),
    ).lastrowid
