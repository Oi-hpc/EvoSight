"""User-requested subjects stay attached to one event and are checked in every tracking round."""
from __future__ import annotations

import json
import re
import unicodedata
from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field

from . import materials, person_sources, workflow
from .db import dump, one, require, rows, utc


class Addition(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    kind: Literal["person", "topic"]
    target: str = Field(min_length=2, max_length=100)
    angle: str = Field(default="", max_length=200)
    instruction: str = Field(min_length=3, max_length=600)


def key(text):
    return " ".join(unicodedata.normalize("NFKC", text).casefold().split())


TRACK = re.compile(r"跟[踪进]|追[踪蹤]|跟蹤|关注|關注|监[控测]|收[集录]|盯|纳入|加入|track|monitor|watch|follow", re.I)
META = re.compile(
    r"如果(?:我|你|用户|说|让)|假[如设]|举例|举个例|例如|比如|示例|有权限|有功能|有这个功能|是什么意思|要不要|是否(?:该|应)|为什么|怎么(?:才能|实现|操作)|支不支持|(?:报道|资料|日志|文章|新闻|文件|文档|材料)(?:中|里)?(?:说|写|要求|称)|复述|翻译|does it|what if|for example|how (?:do|would)",
    re.I,
)
NEGATE = re.compile(
    r"(?:不用|无需|不要|(?<![分特个类级鉴识区])别再?|停止|取消|不再|不想|暂缓|勿|do not|don't|stop)\s*(?:再|继续|持续|把|将)?", re.I
)


def check_request(ctx, additions):
    question = ctx["question"]
    if not ctx.get("can_add_focus") or not additions:
        raise ValueError("此轮讨论没有可执行的收集范围指令")
    direct = []
    for a in additions:
        pos = question.find(a.instruction)
        if (
            pos < 0
            or key(a.target) not in key(a.instruction)
            or (a.angle and key(a.angle) not in key(question))
        ):
            raise ValueError("收集对象、关注点及授权原句须来自用户当前消息")
        sentence = re.split(r"[。！？!?；;\n]", question[:pos])[-1] + a.instruction
        if not TRACK.search(a.instruction) or META.search(sentence) or NEGATE.search(sentence):
            raise ValueError("仅肯定、明确的持续跟踪指令可以改变收集范围")
        if key(a.target) in {
            "这个人",
            "那个人",
            "这些人",
            "这件事",
            "这个事件",
            "他自己",
            "对方",
            "某人",
            "他们",
            "她们",
            "him",
            "her",
            "this",
            "them",
        }:
            raise ValueError("持续跟踪需要明确的姓名或事项名称")
        # A topic in “跟踪某人，收集其与某公司有关的发言” qualifies a subject.
        # “另外单独跟踪某公司” explicitly makes it a second subject.
        qualified = re.compile(
            r"(?:与|和|对|关于|在)\s*" + re.escape(a.target) + r"\s*(?:有关|相关|的(?:讲话|看法|发言|评论)|事件[里中])", re.I
        )
        remaining = qualified.sub("", a.instruction)
        separate = re.search(r"(?:分别|另外|另行|单独|独立)\s*(?:请|也|再)?\s*(?:收集|跟踪|追踪|关注)", a.instruction)
        if key(a.target) in key(remaining) or separate:
            direct.append(a)
    if not direct:
        explicit = re.search(r"(?:跟踪|追踪|关注|监测)(?:一下|下)?\s*([^，,。；;]{2,38})[，,。；;]", question)
        if explicit and not any(key(a.target) in key(explicit[1]) for a in additions):
            raise ValueError("请核对指令中直接指定的收集对象。")
    return direct or additions


def add(c, turn_id, additions, *, event_id=None):
    turn = require(c, "discussion_turns", turn_id)
    ctx = json.loads(turn["context"])
    event = require(c, "events", event_id if event_id is not None else turn["event_id"])
    if event_id is not None:
        ctx = {**ctx, "can_add_focus": not event["demo"] and event["status"] == "active"}
        choice = ctx.get("workspace", {}).get("selection")
        if choice:
            ctx["question"] = choice["original_question"] + "。" + ctx["question"]
    additions = check_request(ctx, additions)
    return register_scopes(c, turn_id, event, additions)


def register_scopes(c, turn_id, event, additions):
    if event["demo"] or event["status"] != "active":
        return "此事件当前未开启跟踪。恢复事件后可以重新提交收集范围。"
    applied = []
    for a in additions:
        values = (event["id"], a.kind, key(a.target), key(a.angle))
        scope = one(
            c,
            """SELECT * FROM event_focus WHERE event_id=? AND kind=? AND target_key=?
               AND (angle_key=? OR (?='' AND active=1))
               ORDER BY active DESC,(angle_key=?) DESC,id LIMIT 1""",
            (*values, values[3], values[3]),
        )
        added = not scope or not scope["active"]
        if not scope:
            ident = c.execute(
                "INSERT INTO event_focus(event_id,kind,target,target_key,angle,angle_key,created_at) VALUES(?,?,?,?,?,?,?)",
                (event["id"], a.kind, a.target, values[2], a.angle, values[3], utc()),
            ).lastrowid
        else:
            ident = scope["id"]
        if added:
            rid, _ = workflow.request_round(
                c, event["id"], "新增收集范围", key=f"discussion-focus/{turn_id}", fresh_inputs=True
            )
            c.execute("UPDATE event_focus SET active=1,first_round_id=? WHERE id=?", (rid, ident))
        else:
            rid = scope["first_round_id"]
        c.execute("INSERT OR IGNORE INTO discussion_focus VALUES(?,?,?,?)", (turn_id, ident, rid, int(added)))
        action = "已加入每轮收集范围，首次核查已安排" if added else "已在每轮收集范围内"
        applied.append(f"「{a.target}」{action}（轮次 #{rid}）。")
    return "\n".join(dict.fromkeys(applied))


def listing(c, event):
    scopes = rows(
        c,
        """SELECT f.id,f.event_id,f.kind,f.target,f.angle,f.active,f.person_target_id,f.first_round_id,
        r.state AS first_state,k.round_id AS last_round,k.checked_at,k.status,k.detail,
        (SELECT json_group_array(json_object('id',d.id,'title',d.title)) FROM json_each(k.documents) j
         JOIN event_docs ed ON ed.event_id=f.event_id AND ed.document_id=j.value
         JOIN documents d ON d.id=ed.document_id) AS documents
        FROM event_focus f JOIN tracking_rounds r ON r.id=f.first_round_id
        LEFT JOIN focus_checks k ON k.focus_id=f.id AND k.round_id=(SELECT max(v.round_id) FROM focus_checks v WHERE v.focus_id=f.id)
        WHERE f.event_id=? ORDER BY f.active DESC,f.id""",
        (event,),
    )
    for scope in scopes:
        scope["documents"] = json.loads(scope["documents"])
    return scopes


def applicable(c, r):
    return rows(
        c,
        "SELECT * FROM event_focus WHERE event_id=? AND active=1 AND first_round_id<=? ORDER BY id",
        (r["event_id"], r["id"]),
    )


def match_people(c, r, targets):
    """Resolve against people already identified from this event's source material."""
    pinned = set()
    for f in applicable(c, r):
        if f["kind"] != "person":
            continue
        if f["person_target_id"]:
            pinned.add(f["person_target_id"])
            continue
        matches = []
        for p in targets:
            aliases = p["aliases"] if isinstance(p["aliases"], list) else json.loads(p["aliases"])
            if f["target_key"] in {key(n) for n in [p["name"], *aliases]}:
                matches.append(p)
        if len(matches) > 1 and f["angle"]:
            matches = [p for p in matches if key(p["identity"]) in key(f["angle"])]
        if len(matches) == 1:
            target = matches[0]["id"]
            c.execute("UPDATE event_focus SET person_target_id=? WHERE id=?", (target, f["id"]))
            pinned.add(target)
    return pinned


def record(c, scope, r, status, detail, docs):
    c.execute(
        """INSERT OR IGNORE INTO focus_checks(focus_id,round_id,checked_at,status,detail,documents)
        VALUES(?,?,?,?,?,?)""",
        (scope["id"], r["id"], utc(), status, detail, dump(docs)),
    )
    return docs


def collect_one(worker, r, scope, event):
    with worker.store.connection() as c:
        prior = one(
            c, "SELECT documents FROM focus_checks WHERE focus_id=? AND round_id=?", (scope["id"], r["id"])
        )
        if prior:
            return json.loads(prior["documents"])
        channels = (
            rows(
                c,
                "SELECT channel,status,document_ids FROM person_checks WHERE target_id=? AND round_id=?",
                (scope["person_target_id"], r["id"]),
            )
            if scope["person_target_id"]
            else []
        )
        first = not one(
            c,
            "SELECT 1 FROM focus_checks WHERE focus_id=? AND round_id>=? AND round_id<? LIMIT 1",
            (scope["id"], scope["first_round_id"], r["id"]),
        )
    channel_docs = list(dict.fromkeys(d for ch in channels for d in json.loads(ch["document_ids"])))
    available = sum(ch["status"] in ("found", "checked", "partial") for ch in channels)
    coverage = f"人物来源核查 {len(channels)} 项，其中 {available} 项可读取。" if channels else ""
    if channels and not scope["angle"]:
        status = "found" if channel_docs else "checked" if available else "unavailable"
        detail = coverage + f"本轮新收录 {len(channel_docs)} 份。"
        with worker.store.connection(True) as c:
            return record(c, scope, r, status, detail, channel_docs)
    query = " ".join(x for x in (scope["target"], event["title"][:80], scope["angle"]) if x)
    error = False
    try:
        results = worker.acquisition.targeted(query)
    except (OSError, ValueError, RuntimeError, TimeoutError):
        results, error = [], True
    with worker.store.connection(True) as c:
        if not workflow.goal_matches(c, r) or not require(c, "event_focus", scope["id"])["active"]:
            return []
        docs = [
            p[0]
            for p in c.execute(
                """SELECT DISTINCT d.id FROM event_docs ed
              JOIN documents d ON d.id=ed.document_id JOIN evidence_assets a ON a.document_id=d.id
              JOIN fragments f ON f.asset_id=a.id WHERE ed.event_id=? AND ed.intake NOT IN ('future','person_candidate')
              AND instr(lower(f.text),lower(?))>0 AND coalesce(d.published_at,'')<=?
              AND (? OR max(coalesce(d.published_at,''),coalesce(d.source_updated_at,''))>?)
              AND max(coalesce(d.published_at,''),coalesce(d.source_updated_at,''))<=?
              AND NOT EXISTS(SELECT 1 FROM focus_checks fc,json_each(fc.documents) j
                             WHERE fc.focus_id=? AND j.value=d.id)
              ORDER BY d.id DESC LIMIT 4""",
                (
                    event["id"],
                    scope["target"],
                    r["window_end"],
                    first,
                    r["window_start"],
                    r["window_end"],
                    scope["id"],
                ),
            )
        ]
        outside = 0
        for entry, res, ex in results[:2]:
            if urlsplit(res.url).hostname in person_sources.HOSTS:
                continue
            content = entry["title"] + " " + entry["summary"]
            if scope["target_key"] not in key(content):
                continue
            if scope["kind"] == "person" and not materials.relevance(event["title"], event["scope"], content):
                continue
            when = ex.published_at
            inside = bool(when and r["window_start"] < when <= r["window_end"])
            if (when and when > r["window_end"]) or (not inside and not first):
                outside += 1
                continue
            previous = one(
                c,
                """SELECT d.id FROM event_docs ed JOIN documents d ON d.id=ed.document_id
                WHERE ed.event_id=? AND d.url=? ORDER BY d.id DESC LIMIT 1""",
                (event["id"], materials.feeds.canonical(res.url)),
            )
            doc = materials.attach_search_result(
                c, event["id"], entry, res, ex, "window" if inside else "backfill"
            )
            if first or not previous or previous["id"] != doc:
                docs.append(doc)
        docs = list(dict.fromkeys([*channel_docs, *docs]))
        status = "found" if docs else "unavailable" if error and not available else "checked"
        detail = coverage + f"本轮取得 {len(docs)} 份相关材料，窗口外或时间待核 {outside} 份。"
        if error:
            detail += "本轮公开检索不可用。"
        if scope["kind"] == "person" and not scope["person_target_id"]:
            detail += "人物身份将根据具名来源核对。"
        return record(c, scope, r, status, detail, docs)


def collect(worker, r):
    with worker.store.connection() as c:
        event = require(c, "events", r["event_id"])
        scopes = applicable(c, r)
    return list(dict.fromkeys(d for s in scopes for d in collect_one(worker, r, s, event)))
