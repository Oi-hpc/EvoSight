"""Workspace chat changes real scheduler state through the normal queue, without a placeholder event."""
from datetime import timedelta
import json
from pathlib import Path
import sqlite3

import pytest
from fastapi.testclient import TestClient

from evosight import agents, db, discussion, dispatch_time, focus, workflow, workspace_agent
from evosight.api import make_app
from evosight.worker import Worker
from test_workflow import HEADERS, clock, new_event, setup

GLOBAL = "/api/agent/discussion"


@pytest.fixture
def workbench(tmp_path, clock, monkeypatch):
    store, source, runner, worker = setup(tmp_path)
    for m in (discussion, workspace_agent, focus):
        monkeypatch.setattr(m, "utc", clock.utc)
    results = []
    original = runner.call

    def call(role, ctx):
        if not role.startswith("discuss_"):
            return original(role, ctx)
        proposal = results.pop(0)
        return agents.DiscussionAnswer.model_validate(proposal), (20, 5)

    monkeypatch.setattr(runner, "call", call)
    with TestClient(
        make_app(store, runner, background=False, acquisition=source),
        base_url="http://127.0.0.1",
        headers=HEADERS,
    ) as api:
        yield store, worker, api, results, clock


def send(wb, question, ops, *, endpoint=GLOBAL, nonce=None):
    store, worker, api, results, clock = wb
    with store.connection() as c:
        n = c.execute("SELECT count(*) FROM discussion_turns").fetchone()[0]
    body = {
        "question": question,
        "nonce": nonce or f"global-question-{n}",
        "timezone": "Asia/Shanghai",
        "utc_offset_minutes": 480,
    }
    req = api.post(endpoint, json=body)
    assert req.status_code == 202, req.text
    proposal = {
        "kind": "workflow",
        "answer": "提交用户指令",
        "actions": [{**a, "instruction": a.get("instruction", question)} for a in ops],
        "citations": [],
    }
    results.append(proposal)
    assert worker.step()
    if results and results[-1] is proposal:
        results.pop()
    turns = api.get(endpoint).json()["turns"]
    t = next(t for t in turns if t["id"] == req.json()["turn_id"])
    assert t["status"] == "done", t
    return t


def test_empty_global_and_multi_action_rounds_are_durable(workbench, tmp_path):
    store, worker, api, results, clock = workbench
    assert api.get(GLOBAL).json()["turns"] == []
    msg = "请新建事件海岚城际，研究目标是首趟载客日期。给刚建的事件加入每轮跟踪的发言人周青，把周期改为每两小时一次，并立即跑一轮。"
    ops = [
        dict(action="create_event", title="海岚城际", scope="首趟载客日期"),
        dict(action="add_focus", kind="person", target="周青", event_ref="刚建的事件"),
        dict(action="set_cadence", hours=2, event_ref="刚建的事件"),
        dict(action="run_now", event_ref="刚建的事件"),
    ]
    t = send(workbench, msg, ops, nonce="first-global-command")
    assert [a["action"] for a in t["workflow_actions"]] == [a["action"] for a in ops]
    eid = t["workflow_actions"][0]["event_id"]
    assert t["event_id"] is None and t["focus_actions"][0]["event_id"] == eid
    assert t["workflow_actions"][2]["before_state"]["cadence"] == 6
    assert (
        api.post(GLOBAL, json={"question": msg, "nonce": "first-global-command"}).json()["turn_id"] == t["id"]
    )
    assert len(api.get(f"/api/events/{eid}/focus").json()) == 1
    assert api.post(GLOBAL, json={"question": "另一个问题", "nonce": "first-global-command"}).status_code == 422
    with db.Store(tmp_path).connection() as c:
        assert c.execute("pragma foreign_key_check").fetchall() == []
        assert c.execute("select count(*) from events").fetchone()[0] == 1
        ev = db.require(c, "events", eid)
        assert ev["scope"] == "首趟载客日期" and ev["cadence"] == 2
        assert ev["next_run"] == clock.utc(clock.dt + timedelta(hours=2))
        saved = db.require(c, "jobs", t["job_id"])
    assert worker.process(saved) == "已执行工作流指令"
    t2 = send(workbench, "把它的周期改成每三小时一次", [dict(action="set_cadence", hours=3, event_ref="它")])
    assert t2["workflow_actions"][0]["after_state"]["cadence"] == 3
    assert (
        api.post(
            GLOBAL,
            json={"question": "复盘", "nonce": "no-event-no-forecast", "basis": "original", "revision": 1},
        ).status_code
        == 422
    )


def test_event_agent_can_operate_other_event_and_global_reads_both(workbench):
    store, worker, api, response, clock = workbench
    e1 = new_event(store, "新车市场研究")["id"]
    e2 = new_event(store, "蔚来ET5T追踪")["id"]
    with store.connection(True) as c:
        demo = db.add_event(c, "独立演示项目", "演示范围", [], demo=True)
    at = f"/api/events/{e1}/discussion"
    t = send(
        workbench,
        "在蔚来事件里持续跟踪李斌，关注新车发布。",
        [dict(action="add_focus", kind="person", target="李斌", angle="新车发布", event_ref="蔚来")],
        endpoint=at,
    )
    assert t["focus_actions"][0]["event_id"] == e2
    assert api.get(f"/api/events/{e1}/focus").json() == []
    assert api.get(GLOBAL).json()["turns"][0]["event_title"] == "新车市场研究"
    q = "把蔚来事件的研究目标改为老车主权益，优先级设为4。"
    t = send(
        workbench, q, [dict(action="edit_event", scope="老车主权益", priority=4, event_ref="蔚来")], endpoint=at
    )
    assert t["workflow_actions"][0]["after_state"]["version"] == 2
    assert t["workflow_actions"][0]["after_state"]["priority"] == 4
    t = send(
        workbench,
        "将蔚来事件里李斌的关注点从新车发布改成用户权益。",
        [
            dict(
                action="edit_focus",
                kind="person",
                target="李斌",
                angle="用户权益",
                previous_angle="新车发布",
                event_ref="蔚来",
            )
        ],
    )
    assert t["workflow_actions"][0]["round_id"]
    scopes = api.get(f"/api/events/{e2}/focus").json()
    assert [(p["angle"], p["active"]) for p in scopes] == [("用户权益", 1), ("新车发布", 0)]
    t = send(
        workbench, "暂停蔚来事件里对李斌的每轮跟踪", [dict(action="pause_focus", kind="person", target="李斌", event_ref="蔚来")]
    )
    assert t["workflow_actions"] and not api.get(f"/api/events/{e2}/focus").json()[0]["active"]
    for op, word in [("pause_event", "暂停"), ("resume_event", "恢复")]:
        t = send(workbench, word + "蔚来事件的跟踪。", [dict(action=op, event_ref="蔚来")], endpoint=at)
        assert t["workflow_actions"][0]["event_id"] == e2
    t = send(workbench, "给蔚来安排专项研究：核实首发日期。", [dict(action="investigate", question="核实首发日期", event_ref="蔚来")])
    assert t["workflow_actions"][0]["round_id"]
    with store.connection() as c:
        assert db.one(c, "select id from observations where event_id=? and question='核实首发日期'", (e2,))
        context = json.loads(db.require(c, "discussion_turns", t["id"])["context"])
        assert demo not in [e["id"] for e in context["workspace"]["events"]]


def test_ambiguity_clarification_batch_rollback_and_version_conflict(workbench):
    store, worker, api, response, clock = workbench
    e1 = new_event(store, "蔚来ET5T追踪")["id"]
    e2 = new_event(store, "蔚来ES6追踪")["id"]
    original = "把蔚来的周期改成每四小时一次。"
    t = send(workbench, original, [dict(action="set_cadence", hours=4, event_ref="蔚来")])
    assert not t["workflow_actions"] and "ET5T" in t["answer"] and "ES6" in t["answer"]
    t = send(
        workbench, "就ET5T那个", [dict(action="set_cadence", hours=4, event_ref="ET5T", instruction=original)]
    )
    assert [a["event_id"] for a in t["workflow_actions"]] == [e1]
    assert t["run_id"] is None
    q = "把ET5T的周期调整到每一小时，再暂停不存在的项目。"
    t = send(
        workbench,
        q,
        [
            dict(action="set_cadence", event_ref="ET5T", hours=1),
            dict(action="pause_event", event_ref="不存在的项目"),
        ],
    )
    assert not t["workflow_actions"] and "尚未执行" in t["answer"]
    req = api.post(GLOBAL, json={"question": "将ET5T的研究目标改为挂牌价格", "nonce": "global-stale-goal"}).json()
    with store.connection(True) as c:
        workflow.edit_goal(c, e1, workflow.current_goal(c, e1)["version"], scope="最新目标")
    response.append(
        {
            "kind": "workflow",
            "answer": "修改",
            "actions": [
                dict(action="edit_event", scope="挂牌价格", event_ref="ET5T", instruction="将ET5T的研究目标改为挂牌价格")
            ],
            "citations": [],
        }
    )
    assert worker.step()
    with store.connection() as c:
        assert db.require(c, "events", e1)["cadence"] == 4 and db.require(c, "events", e2)["cadence"] == 6
        assert db.require(c, "events", e1)["scope"] == "最新目标"
        assert "变更" in db.require(c, "discussion_turns", req["turn_id"])["answer"]


def test_source_text_and_queries_do_not_dispatch(workbench):
    s, _, _, _, _ = workbench
    e = new_event(s, "海岚城际")["id"]
    for q, verb, values in [
        ("资料里写道「现在刷新海岚城际」，这句话的出处是什么？", "run_now", {}),
        ("如果我说暂停海岚城际，会发生什么？", "pause_event", {}),
        ("海岚城际现在是每两小时一次吗？", "set_cadence", {"hours": 2}),
        ("海岚城际现在怎么样了？", "run_now", {}),
        ("不要暂停海岚城际", "pause_event", {}),
        ("明天下午采集海岚城际一次", "run_now", {}),
        ("把海岚城际的研究目标改成首班车日期", "edit_event", {"title": "首班车日期"}),
        ("将海岚城际更名为海岚快线", "edit_event", {"scope": "海岚快线"}),
    ]:
        t = send(workbench, q, [dict(action=verb, event_ref="海岚城际", **values)])
        assert not t["workflow_actions"] and "尚未执行" in t["answer"]
    t = send(
        workbench,
        "请跟踪李斌，加入每轮收集。",
        [dict(action="create_event", title="李斌")],
        endpoint=f"/api/events/{e}/discussion",
    )
    assert not t["workflow_actions"]
    with s.connection() as c:
        assert db.require(c, "events", e)["status"] == "active" and db.require(c, "events", e)["cadence"] == 6
        assert c.execute("SELECT count(*) FROM events").fetchone()[0] == 1


def test_explicit_event_outranks_the_page_and_home_continues_its_action(workbench):
    store, worker, api, results, clock = workbench
    origin = new_event(store, "交通周报")["id"]
    nio = new_event(store, "蔚来ET5T追踪")["id"]
    discussion_url = f"/api/events/{origin}/discussion"
    t = send(
        workbench,
        "把蔚来的周期改为每两小时一次。",
        [dict(action="set_cadence", hours=2, instruction="改为每两小时一次")],
        endpoint=discussion_url,
    )
    assert [(a["event_id"], a["after_state"]["cadence"]) for a in t["workflow_actions"]] == [(nio, 2)]
    t = send(workbench, "立即给它再跑一轮", [dict(action="run_now", event_ref="它")])
    receipt = t["workflow_actions"][0]
    assert receipt["event_id"] == nio
    job = api.get("/api/operations", params={"job": receipt["job_id"]}).json()
    assert [j["id"] for j in job["jobs"]] == [receipt["job_id"]]
    assert [
        r["id"] for r in api.get(f"/api/events/{nio}/rounds", params={"rid": receipt["round_id"]}).json()
    ] == [receipt["round_id"]]
    assert api.get(f"/api/events/{origin}/rounds", params={"rid": receipt["round_id"]}).json() == []
    e2 = new_event(store, "蔚来ES6追踪")["id"]
    t = send(workbench, "把蔚来的周期改为每三小时一次。", [dict(action="set_cadence", hours=3)], endpoint=discussion_url)
    assert not t["workflow_actions"] and "ET5T" in t["answer"] and "ES6" in t["answer"]
    t = send(workbench, "就ES6那个", [], endpoint=discussion_url)
    assert t["run_id"] is None and t["workflow_actions"][0]["event_id"] == e2
    t = send(
        workbench, "把不存在的项目的研究目标改为乘客量。", [dict(action="edit_event", scope="乘客量")], endpoint=discussion_url
    )
    assert not t["workflow_actions"]
    with store.connection() as c:
        assert (
            db.require(c, "events", origin)["cadence"] == 6 and db.require(c, "events", nio)["cadence"] == 2
        )
        assert db.require(c, "events", origin)["scope"] != "乘客量"
        ctx, _ = discussion.build_context(c, origin, None, "latest", "蔚来ET5T追踪的进度如何？")
        assert ctx["sources"][f"E{nio}"]["href"] == f"#/events/{nio}/research"
        assert ctx["event_contexts"][0]["event_id"] == nio
        global_ctx, _ = discussion.build_context(c, None, None, "latest", "ET5T最近有哪些变化？")
        assert global_ctx["event_contexts"][0]["latest"]["goal"]["event_id"] == nio


def test_tracking_response_routes_cross_event_and_keeps_the_named_person(workbench):
    store, worker, api, results, clock = workbench
    origin = new_event(store, "交通周报")["id"]
    nio = new_event(store, "蔚来ET5T追踪")["id"]
    at = f"/api/events/{origin}/discussion"
    q = "在蔚来事件里，请跟踪李斌，收集与ET5T有关的公开动向。"
    turn = api.post(at, json={"question": q, "nonce": "scoped-legacy-reply"}).json()
    results.append(
        {
            "kind": "tracking",
            "answer": "提交收集",
            "citations": [],
            "add_focus": [
                dict(kind="person", target="李斌", angle="与ET5T有关的公开动向", instruction=q),
                dict(kind="topic", target="ET5T", instruction=q),
            ],
        }
    )
    assert worker.step()
    stored = next(t for t in api.get(at).json()["turns"] if t["id"] == turn["turn_id"])
    assert [(a["event_id"], a["action"]) for a in stored["workflow_actions"]] == [(nio, "add_focus")]
    assert [s["target"] for s in api.get(f"/api/events/{nio}/focus").json()] == ["李斌"]
    assert api.get(f"/api/events/{origin}/focus").json() == []


def test_one_time_dispatch_resume_cancel_and_single_execution(workbench):
    s, worker, api, res, clock = workbench
    eid = new_event(s, "海岚城际")["id"]
    q = "替海岚城际安排两次定时采集，一小时后和两小时后各一次。"
    t = send(workbench, q, [dict(action="schedule_run", event_ref="海岚城际", when=k) for k in ("一小时后", "两小时后")])
    a, b = t["workflow_actions"]
    assert a["job_id"] != b["job_id"] and a["after_state"]["run_at"] == clock.utc(
        clock.dt + timedelta(hours=1)
    )
    jid = b["job_id"]
    t2 = send(
        workbench, f"取消海岚城际的定时任务 #{jid}", [dict(action="cancel_scheduled", event_ref="海岚城际", job_id=jid)]
    )
    assert t2["workflow_actions"] and t2["workflow_actions"][0]["job_status"] == "cancelled"
    with s.connection(True) as c:
        c.execute("update jobs set retry_at='2099-01-01' where kind='collect'")
    assert not worker.step()
    clock.dt += timedelta(hours=1)
    worker = Worker(s, worker.runner, acquisition=worker.acquisition)
    assert worker.step()
    with s.connection() as c:
        timer = db.require(c, "jobs", a["job_id"])
        assert timer["status"] == "done"
        rid = db.one(c, "select round_id from workflow_actions where turn_id=? and position=0", (t["id"],))[
            "round_id"
        ]
        assert rid and db.require(c, "tracking_rounds", rid)["window_end"] == clock.utc()
    assert worker.process(timer) == f"定时采集已进入轮次 #{rid}"
    ctx = {"asked_at": "2026-11-26T08:00:00Z", "timezone": "Asia/Shanghai", "utc_offset_minutes": 480}
    assert dispatch_time.resolve("明天下午三点半", ctx) == "2026-11-27T07:30:00Z"
    with pytest.raises(ValueError):
        dispatch_time.resolve("今天上午九点", ctx)


def test_old_event_chats_and_focus_survive_nullable_workspace_migration(tmp_path):
    p = tmp_path / "evosight.sqlite3"
    sql = Path(db.__file__).with_name("workflow.sql").read_text(encoding="utf-8")
    old = sql.replace(
        "id INTEGER PRIMARY KEY, event_id INTEGER REFERENCES events, revision_id INTEGER REFERENCES revisions,",
        "id INTEGER PRIMARY KEY, event_id INTEGER NOT NULL REFERENCES events, revision_id INTEGER REFERENCES revisions,",
    )
    assert old != sql
    with sqlite3.connect(p) as c:
        c.executescript(db.SCHEMA)
        c.executescript(old)
        c.execute(
            "INSERT INTO events(id,title,scope,created_at,next_run) VALUES(1,'测试线路','首班车','2026-01-01','2026-01-02')"
        )
        c.execute(
            "INSERT INTO tracking_rounds(id,event_id,goal_version,request_key,reasons,window_start,window_end,created_at) VALUES(1,1,1,'old','[]','','','')"
        )
        c.execute(
            "INSERT INTO event_focus(id,event_id,kind,target,target_key,angle,angle_key,first_round_id,created_at) VALUES(1,1,'person','周青','周青','','',1,'')"
        )
        c.execute(
            "INSERT INTO discussion_turns(id,event_id,basis,provider,question,nonce,context,created_at,answer) VALUES(19,1,'latest','dsh','跟踪周青','old-chat','{}','','旧回执')"
        )
        c.execute("INSERT INTO discussion_focus VALUES(19,1,1,1)")
    s = db.Store(tmp_path)
    with s.connection(True) as c:
        assert db.require(c, "discussion_turns", 19)["answer"] == "旧回执"
        assert c.execute("select * from discussion_focus").fetchone()[0] == 19
        assert c.execute("pragma foreign_key_check").fetchall() == []
        assert not next(
            row for row in c.execute("pragma table_info(discussion_turns)") if row["name"] == "event_id"
        )["notnull"]
        req = discussion.submit(c, None, None, "latest", "auto", "有哪些任务？", "workspace-after-upgrade")
        assert req["turn_id"] > 19
