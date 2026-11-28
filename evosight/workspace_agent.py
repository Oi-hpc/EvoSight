"""The workbench agent can operate on named events through the same scheduler as the UI."""
from __future__ import annotations

import json
import re
import sqlite3
from datetime import datetime, timedelta
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from . import dispatch_time, focus, workflow, world_search
from .db import add_event, config, dump, enqueue, one, require, rows, utc


class Command(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    instruction: str = Field(min_length=2, max_length=2400)
    event_ref: str = Field(default="", max_length=250)


class CreateEvent(Command):
    action: Literal["create_event"]
    title: str = Field(min_length=1, max_length=250)
    scope: str = Field(default="", max_length=1500)


class EditEvent(Command):
    action: Literal["edit_event"]
    title: str = Field(default="", max_length=250)
    scope: str = Field(default="", max_length=1500)
    priority: int | None = Field(default=None, ge=1, le=5)


class Cadence(Command):
    action: Literal["set_cadence"]
    hours: int = Field(ge=1, le=168)


class Dispatch(Command):
    action: Literal["run_now", "pause_event", "resume_event"]


class ScheduleRun(Command):
    action: Literal["schedule_run"]
    when: str = Field(min_length=2, max_length=80)


class CancelRun(Command):
    action: Literal["cancel_scheduled"]
    job_id: int = Field(ge=1)


class ScopeChange(Command):
    action: Literal["add_focus", "pause_focus"]
    kind: Literal["person", "topic"]
    target: str = Field(min_length=2, max_length=100)
    angle: str = Field(default="", max_length=200)


class EditFocus(Command):
    action: Literal["edit_focus"]
    kind: Literal["person", "topic"]
    target: str = Field(min_length=2, max_length=100)
    angle: str = Field(min_length=1, max_length=200)
    previous_angle: str = Field(default="", max_length=200)


class Investigation(Command):
    action: Literal["investigate"]
    question: str = Field(min_length=2, max_length=600)
    role: Literal["research", "people"] = "research"


Operation = (
    CreateEvent
    | EditEvent
    | Cadence
    | Dispatch
    | ScheduleRun
    | CancelRun
    | ScopeChange
    | EditFocus
    | Investigation
)

INTENT = {
    "create_event": r"新建|创建|建立|新增.{0,10}事件|(?:开始|单独|独立|持续)跟踪|(?:帮我|请)跟踪|新开|create|start tracking",
    "edit_event": r"改|调整|更新|重命名|更名|更换|改为|设为|设成|提高|降低|优先级|rename|update|change",
    "set_cadence": r"每|周期|频率|间隔|小时.{0,4}一次|hours?|cadence",
    "run_now": r"马上|立即|现在|查一遍|查一次|跑一[遍轮次]|刷新|重跑|启动|触发|再查|run now|refresh",
    "schedule_run": r"安排|调度|定时|预约|运行|采集|跟[踪进]|[核补巡]查|查一|跑一|schedule",
    "cancel_scheduled": r"取消|撤销|撤回|cancel",
    "investigate": r"补查|补充|核查|核实|调研|研究|调查|查[一下询]|梳理|查清|investigate|research",
    "add_focus": focus.TRACK.pattern,
    "edit_focus": r"改|调整|更新|换成|换为|change|update",
    "pause_focus": r"暂停|停止|移出|取消|不再|不要再|remove|pause|stop",
    "pause_event": r"暂停|停止|停一下|停掉|先停|pause|stop",
    "resume_event": r"恢复|重启|重新开始|继续跟踪|继续追踪|resume|restart",
}
REFERENCE = {"", "当前", "当前事件", "本事件", "这个事件", "该事件", "刚建的事件", "刚才的事件", "它", "current"}
EVENT_NAME = re.compile(
    r"(?:针对|给|把|将|在|暂停|恢复|停止|重启|取消)\s*[「《“]?"
    r"((?:(?!加入|纳入|添加|改为|改成|安排)[\w ·.-]){1,80}?)[」》”]?"
    r"(?=(?:这个)?事件(?:的|[里中内]|[，,。！!\s]|$)|的(?:跟踪周期|跟踪频率|研究目标|优先级|周期|频率|定时任务|名称|标题)|安排(?:一次|一轮|定时|专项|今天|明天|后天|[零一二两三四五六七八九十\d]))"
)
EVENT_ID = re.compile(r"(?:事件\s*#?\s*|(?:暂停|恢复|重启|刷新|把|将|给|为)\s*#)(\d+)(?!\d)")
CONTROL = re.compile("|".join(INTENT.values()), re.I)
HYPOTHETICAL = re.compile(
    r"假如|如果(?:我|你|要|让|说)|假设|举例|例如|比如|复述|翻译|为什么|能否介绍|如何操作|有权限|会怎样|会发生什么|有没有能力|是否支持|只(?:是)?(?:想)?问|what if|for example",
    re.I,
)
REPORTED = re.compile(r"(?:报道|新闻|文档|资料|文章|邮件|帖子|页面|原文|提示词).{0,5}(?:写道|写着|说|称|要求|建议|指出)", re.I)
NEGATED = re.compile(r"(?:不要|不必|无需|不用|别再?|请勿|不应|先不|不需要|do not|don't)\s*(?:再|把|将|帮我|替我)?", re.I)
CLOSING_NEGATION = re.compile(r"(?:不要|勿|别|不用|不必|禁止|无需)(?:再)?(?:操作|执行|修改|更改|调整|调度|创建|新增|跟踪|触发|运行)")


def norm(text):
    return re.sub(r"[\W_]+", "", focus.key(text))


def explicit_values(pattern, text):
    return [dispatch_time.number(n) for n in re.findall(pattern, text, re.I)]


def command_question(ctx):
    choice = ctx.get("workspace", {}).get("selection")
    return choice["original_question"] + "。" + ctx["question"] if choice else ctx["question"]


def check_command(ctx, action):
    q = command_question(ctx)
    instruction = action.instruction
    if isinstance(action, CancelRun):
        matching = [
            part.strip()
            for part in re.split(r"[。！？!?；;\n]", q)
            if re.search(INTENT[action.action], part, re.I)
            and re.search(rf"(?:#|任务\s*#?\s*){action.job_id}(?!\d)", part)
        ]
        if len(matching) != 1:
            raise ValueError("请在一条取消指令中给出相应的定时任务编号。")
        instruction = action.instruction = matching[0]
    at = q.find(instruction)
    if at < 0 and ctx.get("workspace"):
        clauses = [
            s.strip() for s in re.split(r"[。！？!?；;\n]", q) if re.search(INTENT[action.action], s, re.I)
        ]
        if len(clauses) == 1:
            instruction = clauses[0]
            at = q.find(instruction)
            action.instruction = instruction
    if not ctx.get("workspace") or at < 0:
        raise ValueError("请在当前消息中明确说明要执行的操作。")
    surrounding = re.split(r"[。！？!?\n]", q[:at])[-1] + instruction
    # Explicit per-action verbs come from this message. Quoted source material cannot dispatch work.
    if (
        HYPOTHETICAL.search(surrounding)
        or REPORTED.search(q)
        or CLOSING_NEGATION.search(q)
        or not re.search(INTENT[action.action], instruction, re.I)
    ):
        raise ValueError("请明确指出要实际执行的操作及目标事件。")
    if action.action not in ("pause_focus",) and NEGATED.search(surrounding.replace("分别", "")):
        raise ValueError("本条消息有否定或互相冲突的指令，请说明要执行哪一项。")
    if action.action == "pause_focus" and re.search(r"(?:不要|勿|别|不必)(?:再)?(?:暂停|停止|取消|移出)", surrounding):
        raise ValueError("请确认是否要暂停这项收集。")
    target_clause = instruction if isinstance(action, CancelRun) else q
    if action.event_ref not in REFERENCE and norm(action.event_ref) not in norm(target_clause):
        raise ValueError("事件指代必须出现在当前指令中。")
    for field in ("title", "scope", "target", "angle", "previous_angle", "question", "when"):
        value = getattr(action, field, "")
        if value and norm(value) not in norm(q):
            raise ValueError("请给出要保存的名称、关注点或研究问题的原文。")
    digits = r"([0-9零一二两三四五六七八九十百]+)"
    if (
        isinstance(action, CreateEvent)
        and (ctx.get("event_id") is not None or re.search(r"每轮|每次跟踪|收集范围|本事件|当前事件", instruction))
        and not re.search(
            r"新建|创建|新开|新的?(?:跟踪)?事件|新增.{0,8}事件|建立.{0,8}事件|单独|独立|另开|create|new event", instruction, re.I
        )
    ):
        raise ValueError("请说明是加入事件的每轮收集范围，还是新建一个独立跟踪事件。")
    if isinstance(action, Cadence):
        if not re.search(r"改|调|设|请|我要|以后|今后|希望|一次|change|set|every", instruction, re.I) or re.search(
            r"是不是|是否已经|当前是|现在是|目前是|已经是", instruction
        ):
            raise ValueError("请说明希望设置的跟踪周期。")
        hours = explicit_values(digits + r"\s*(?:个?小时|小時|hours?|h\b)", instruction)
        hours += [n * 24 for n in explicit_values(digits + r"\s*(?:天|日|days?)", instruction)]
        if re.search(r"每小时|每小時|hourly", instruction, re.I):
            hours.append(1)
        if re.search(r"每天|每日|daily", instruction, re.I):
            hours.append(24)
        if action.hours not in hours:
            raise ValueError("请给出希望使用的跟踪间隔，例如每四小时一次。")
    if isinstance(action, EditEvent):
        if not (action.title or action.scope or action.priority is not None):
            raise ValueError("请说明要修改的事件名称、研究目标或优先级。")
        goal_words = r"目标|范围|研究方向|研究重点|跟踪内容|跟踪重点|关注|goal|scope"
        name_words = r"改名|更名|重命名|名称|名字|标题|命名|改叫|rename|title"
        if (
            action.title
            and not re.search(name_words, instruction, re.I)
            and re.search(goal_words + r"|优先级|周期|频率", instruction, re.I)
        ):
            raise ValueError("请指明是否同时修改事件名称。")
        if (
            action.scope
            and not re.search(goal_words, instruction, re.I)
            and re.search(name_words + r"|优先级|周期|频率", instruction, re.I)
        ):
            raise ValueError("请指明要设置的研究目标或收集范围。")
        if action.priority is not None and action.priority not in explicit_values(
            r"优先级.{0,6}?" + digits, instruction
        ):
            raise ValueError("请在指令中给出 1 至 5 的优先级。")
    if (
        action.action == "run_now"
        and re.search(r"明天|后天|下周|今晚|稍后|小时后|分钟后|\d月\d+日", instruction)
        and not re.search(r"立刻|立即|马上|现在", instruction)
    ):
        raise ValueError("请按指定时间安排单次采集。")
    if action.action == "run_now" and not re.search(
        r"采集|核查|跟踪|开跑|启动|运行|调度|触发|检查|刷新|重跑|跑一|查一|再查|run|refresh", instruction, re.I
    ):
        raise ValueError("请明确是否要立即运行一轮。")
    if isinstance(action, CancelRun) and not re.search(
        rf"(?:#|任务\s*#?\s*){action.job_id}(?!\d)", instruction
    ):
        raise ValueError("请给出要取消的定时任务编号。")


def registry(c):
    return rows(
        c,
        """SELECT e.id,e.title,e.status,e.cadence,e.next_run,g.version,g.priority,substr(e.scope,1,350) AS scope,
        (SELECT count(*) FROM jobs j WHERE j.event_id=e.id AND j.kind!='discussion' AND j.status='running') AS running,
        (SELECT count(*) FROM jobs j WHERE j.event_id=e.id AND j.kind!='discussion' AND j.status='queued') AS queued
        FROM events e JOIN event_goals g ON g.event_id=e.id AND g.version=(
        SELECT max(v.version) FROM event_goals v WHERE v.event_id=e.id)
        WHERE e.demo=0 AND e.status!='deleted' ORDER BY (e.status='active') DESC,e.id DESC""",
    )


def event_matches(options, ref):
    if match := re.fullmatch(r"(?:事件\s*)?#?\s*(\d+)", ref):
        return [e for e in options if e["id"] == int(match[1])]
    token = norm(ref)
    exact = [e for e in options if token and token == norm(e["title"])]
    if exact:
        return exact
    short = re.sub(r"(?:这个)?事件$", "", token)
    return [e for e in options if len(short) >= 2 and short in norm(e["title"])]


def named_refs(text):
    refs = list(EVENT_ID.findall(text))
    for raw in EVENT_NAME.findall(text):
        name = raw.strip()
        if name and name not in REFERENCE and name + "事件" not in REFERENCE:
            refs.append(name)
    return list(dict.fromkeys(refs))


def workspace(c, event, question, revision=None, basis="latest"):
    all_events = registry(c)
    previous = one(
        c,
        """SELECT t.id,t.event_id,t.question,t.answer,t.context,json_extract(m.response,'$.kind') AS kind,m.response
        FROM discussion_turns t JOIN jobs j ON j.id=t.job_id LEFT JOIN model_receipts m ON m.run_id=t.run_id
        LEFT JOIN events e ON e.id=t.event_id
        WHERE ((? IS NULL AND (t.event_id IS NULL OR (e.demo=0 AND e.status!='deleted')))
        OR (t.event_id=? AND t.revision_id IS ? AND t.basis=?))
        AND t.answer IS NOT NULL ORDER BY t.id DESC LIMIT 1""",
        (event, event, revision, basis),
    )
    recent = []
    if previous and not event:
        recent = rows(
            c,
            """SELECT DISTINCT event_id FROM workflow_actions WHERE turn_id=?
            UNION SELECT DISTINCT f.event_id FROM discussion_focus a JOIN event_focus f ON f.id=a.focus_id WHERE a.turn_id=?""",
            (previous["id"], previous["id"]),
        )
    default = event or (recent[0]["event_id"] if len(recent) == 1 else None)
    if not default and previous and not recent:
        default = (
            json.loads(previous["context"]).get("workspace", {}).get("current_event") or previous["event_id"]
        )
    if not event and default and not any(e["id"] == default for e in all_events):
        default = None
    selection = None
    if previous and (
        previous["kind"] == "clarification"
        or "要操作哪个事件" in previous["answer"]
        or "请指定其中一个事件" in previous["answer"]
    ):
        raw = re.sub(r"^(?:我选|选|就是|就用|就|指的是|是)\s*|(?:的?那个|这个|吧|[。！!])$", "", question.strip()).strip(" 「」《》")
        options = event_matches(all_events, raw) if raw else []
        old = previous["question"]
        if (
            len(options) == 1
            and CONTROL.search(old)
            and not (HYPOTHETICAL.search(old) or REPORTED.search(old) or CLOSING_NEGATION.search(old))
        ):
            default = options[0]["id"]
            selection = {"event_id": default, "original_turn": previous["id"], "original_question": old}
            if previous["kind"] == "workflow":
                selection["pending_actions"] = json.loads(previous["response"]).get("actions", [])
            elif previous["kind"] == "tracking":
                additions = json.loads(previous["response"]).get("add_focus", [])
                selection["pending_actions"] = [{"action": "add_focus", **item} for item in additions]
    words = norm(question)
    mentioned = {e["id"] for ref in named_refs(question) for e in event_matches(all_events, ref)}
    query_words = set(world_search.query_terms(question)[0])
    for e in all_events:
        short = re.sub(r"(持续)?[跟追]踪$|研究$|观察$", "", norm(e["title"]))
        if short and short in words:
            mentioned.add(e["id"])
    # A partial title can help retrieve evidence. Only explicit selectors can authorize a change.
    relevance = {
        e["id"]: sum(min(6, len(w)) for w in (world_search.terms(e["title"]) & query_words) if len(w) > 1)
        for e in all_events
    }

    def rank(e):
        return (
            e["id"] in mentioned,
            relevance[e["id"]],
            e["id"] == default,
            e["status"] == "active",
            e["id"],
        )

    events = sorted(all_events, key=rank, reverse=True)
    if not mentioned and events and relevance[events[0]["id"]]:
        mentioned = {e["id"] for e in events if relevance[e["id"]] == relevance[events[0]["id"]]}
    upcoming = rows(
        c,
        """SELECT j.id AS job_id,e.id AS event_id,e.title,j.retry_at AS run_at
        FROM jobs j JOIN events e ON e.id=j.event_id WHERE e.demo=0 AND j.kind='scheduled_collect'
        AND j.status='queued' ORDER BY j.retry_at,j.id LIMIT 30""",
    )
    return {
        "current_event": default,
        "mentioned_events": [e["id"] for e in events if e["id"] in mentioned][:8],
        "selection": selection,
        "events": events[:40],
        "event_count": len(events),
        "scheduled": upcoming,
        "scheduler_paused": bool(config(c)["global_pause"]),
    }


def target_event(c, action, state, created, question):
    ref = action.event_ref.strip()
    current = state["current_event"]
    candidates = registry(c)
    explicit = named_refs(action.instruction)
    if not explicit and not isinstance(action, CancelRun):
        explicit = named_refs(question)
    if ref in REFERENCE:
        if len(explicit) > 1:
            raise ValueError("要操作哪个事件？请给出事件名称或编号。")
        if explicit:
            ref = explicit[0]
        elif not ref:
            fields = {norm(getattr(action, k, "")) for k in ("title", "scope", "target", "angle", "question")}
            named = [
                e
                for e in candidates
                if norm(e["title"]) not in fields and norm(e["title"]) in norm(action.instruction)
            ]
            if len(named) == 1:
                ref = named[0]["title"]
            elif len(named) > 1:
                raise ValueError("要操作哪个事件？请给出事件名称或编号。")
    if ref not in REFERENCE:
        matches = event_matches(candidates, ref)
        if len(explicit) == 1:
            allowed = {e["id"] for e in event_matches(candidates, explicit[0])}
            matches = [e for e in matches if e["id"] in allowed]
        if state.get("selection") and any(e["id"] == current for e in matches):
            return current
        if len(matches) != 1:
            names = "、".join(f'{e["title"]}（#{e["id"]}）' for e in matches[:5])
            raise ValueError("请指定其中一个事件，" + names if names else f"无法确定「{ref}」指向的事件，请给出名称或编号。")
        return matches[0]["id"]
    if created and (not current or ref in ("", "刚建的事件", "刚才的事件")):
        return created
    if not current:
        raise ValueError("要操作哪个事件？请给出事件名称或编号。")
    return current


def event_state(c, ident):
    event = require(c, "events", ident)
    goal = workflow.current_goal(c, ident)
    return {k: event[k] for k in ("id", "title", "scope", "status", "cadence", "next_run")} | {
        "version": goal["version"],
        "priority": goal["priority"],
    }


def dispatch(c, turn, action, ident, position, ctx):
    e = event_state(c, ident)
    if action.action in ("edit_event", "pause_event", "resume_event"):
        if isinstance(action, EditEvent):
            changes = {
                key: value
                for key, value in action.model_dump().items()
                if key in ("title", "scope", "priority") and value
            }
        else:
            changes = {"status": "paused" if action.action == "pause_event" else "active"}
        result = workflow.edit_goal(c, ident, e["version"], **changes)
        labels = {"title": "名称", "scope": "研究目标", "priority": "优先级", "status": "跟踪状态"}
        values = {"paused": "已暂停", "active": "跟踪中"}
        detail = "，".join(f"{labels[k]}改为「{values.get(v,v)}」" for k, v in changes.items() if e[k] != v)
        return detail or "当前设置已符合要求", result
    if isinstance(action, Cadence):
        if e["cadence"] != action.hours:
            anchor = datetime.fromisoformat(e["next_run"]) - timedelta(hours=e["cadence"])
            due = utc(anchor + timedelta(hours=action.hours))
            c.execute("UPDATE events SET cadence=?,next_run=? WHERE id=?", (action.hours, due, ident))
        return f"跟踪周期为每 {action.hours} 小时一次", {}
    if isinstance(action, CancelRun):
        cancelled = c.execute(
            """UPDATE jobs SET status='cancelled',finished_at=?,info='用户取消定时采集'
            WHERE id=? AND event_id=? AND kind='scheduled_collect' AND status='queued'""",
            (utc(), action.job_id, ident),
        )
        if not cancelled.rowcount:
            raise ValueError("这个事件没有该编号的待执行定时采集。")
        return f"已取消定时任务 #{action.job_id}", {"job": action.job_id}
    if e["status"] != "active":
        raise ValueError(f'「{e["title"]}」已暂停或归档，请先明确恢复跟踪。')
    if isinstance(action, (ScopeChange, EditFocus)):
        if action.action in ("pause_focus", "edit_focus"):
            old_angle = action.previous_angle if isinstance(action, EditFocus) else action.angle
            found = [
                s
                for s in focus.listing(c, ident)
                if s["active"]
                and s["kind"] == action.kind
                and focus.key(s["target"]) == focus.key(action.target)
                and (not old_angle or focus.key(s["angle"]) == focus.key(old_angle))
            ]
            if len(found) != 1:
                raise ValueError("请指明要调整的已有收集对象及原关注点。")
            if isinstance(action, EditFocus) and focus.key(found[0]["angle"]) == focus.key(action.angle):
                return "当前关注点已符合要求", {}
            c.execute("UPDATE event_focus SET active=0 WHERE id=?", (found[0]["id"],))
            if action.action == "pause_focus":
                return f"已暂停每轮收集「{action.target}」", {}
        addition = focus.Addition(
            kind=action.kind, target=action.target, angle=action.angle, instruction=action.instruction
        )
        if isinstance(action, EditFocus):
            focus.register_scopes(c, turn, require(c, "events", ident), [addition])
            summary = f"已将「{action.target}」的每轮关注点改为「{action.angle}」"
        else:
            summary = focus.register_scopes(c, turn, require(c, "events", ident), [addition])
        receipt = one(
            c,
            """SELECT r.id AS round,r.job_id AS job FROM discussion_focus a JOIN event_focus f ON f.id=a.focus_id
                    JOIN tracking_rounds r ON r.id=a.round_id WHERE a.turn_id=? AND f.event_id=? AND f.target_key=?
                    AND (?='' OR f.angle_key=?)
                    ORDER BY f.id DESC LIMIT 1""",
            (turn, ident, focus.key(action.target), focus.key(action.angle), focus.key(action.angle)),
        )
        return summary, receipt or {}
    if config(c)["global_pause"]:
        raise ValueError("工作台调度已暂停。恢复后可以安排本轮运行。")
    if isinstance(action, ScheduleRun):
        due = dispatch_time.resolve(action.when, ctx)
        if due <= utc():
            raise ValueError("指定时间已经过去，请重新指定。")
        key = f"workspace-schedule/{turn}/{position}"
        jid = enqueue(
            c,
            "scheduled_collect",
            event=ident,
            args={"turn_id": turn, "dispatch_key": key, "run_at": due},
            slot=key,
        )
        c.execute("UPDATE jobs SET retry_at=? WHERE id=? AND status='queued'", (due, jid))
        return f"已安排「{action.when}」采集（任务 #{jid}）", {"job": jid, "run_at": due}
    reason = "手动刷新"
    if isinstance(action, Investigation):
        reason = "人物专项" if action.role == "people" else "专项补查"
        workflow.observe(c, ident, "用户专项", action.question, "取得可定位的公开材料并记录核查结论", due=utc(), priority=5)
    rid, jid = workflow.request_round(c, ident, reason, key=f"workspace/{turn}/{position}", fresh_inputs=True)
    return (f"已安排{reason}「{action.question}」" if isinstance(action, Investigation) else "已安排立即采集"), {
        "round": rid,
        "job": jid,
    }


def execute(c, turn, ctx, actions):
    state = ctx["workspace"]
    submitted = {e["id"]: e for e in state["events"]}
    seen = set()
    created = None
    lines = []
    c.execute("SAVEPOINT workspace_batch")
    try:
        groups = {}
        for action in actions:
            if action.action == "add_focus":
                groups.setdefault(action.event_ref, []).append(action)
        focus_context = {**ctx, "can_add_focus": True, "question": command_question(ctx)}
        requested = {id(a) for group in groups.values() for a in focus.check_request(focus_context, group)}
        for pos, a in enumerate(actions):
            check_command(ctx, a)
            if a.action == "add_focus" and id(a) not in requested:
                continue
            if isinstance(a, CreateEvent):
                duplicate = one(c, "SELECT id FROM events WHERE title=? AND status!='deleted'", (a.title,))
                if duplicate:
                    raise ValueError(f"该事件名称已经存在。请指定现有事件或换一个名称。")
                ident = add_event(c, a.title, a.scope or a.title, [])
                refs = workflow.begin_event(c, ident)
                before = {}
                summary = "已创建跟踪事件并安排首次采集"
                created = ident
            else:
                question = command_question(ctx) if len(actions) == 1 else a.instruction
                ident = target_event(c, a, state, created, question)
                before = event_state(c, ident)
                if ident not in seen:
                    pinned = submitted.get(ident)
                    if not pinned or (pinned["version"], pinned["cadence"]) != (
                        before["version"],
                        before["cadence"],
                    ):
                        raise ValueError("事件在提问后有变更，请核对最新设置再提交。")
                summary, refs = dispatch(c, turn, a, ident, pos, ctx)
            seen.add(ident)
            after = event_state(c, ident)
            if refs.get("run_at"):
                after["run_at"] = refs["run_at"]
            c.execute(
                """INSERT INTO workflow_actions(turn_id,position,action,event_id,summary,before_state,after_state,round_id,job_id,created_at)
                          VALUES(?,?,?,?,?,?,?,?,?,?)""",
                (
                    turn,
                    pos,
                    a.action,
                    ident,
                    summary,
                    dump(before),
                    dump(after),
                    refs.get("round"),
                    refs.get("job"),
                    utc(),
                ),
            )
            suffix = f'（轮次 #{refs["round"]}）' if refs.get("round") and a.action != "add_focus" else ""
            lines.append(f'「{after["title"]}」{summary}{suffix}。'.replace("。。", "。"))
    except (ValueError, KeyError, sqlite3.IntegrityError) as exc:
        c.execute("ROLLBACK TO workspace_batch")
        message = "名称或关联记录发生冲突，请核对目标。" if isinstance(exc, sqlite3.IntegrityError) else str(exc).strip("'")
        return "本次指令尚未执行。" + message
    finally:
        c.execute("RELEASE workspace_batch")
    return "\n".join(lines)
