import json

from fastapi.testclient import TestClient

from evosight import db, research, steering, workflow
from evosight.api import make_app
from evosight.contracts import Plan
from test_workflow import HEADERS, clock, new_event, setup


def workbench(tmp_path, roles):
    store, source, runner, worker = setup(tmp_path, roles)
    event = new_event(store)
    api = TestClient(
        make_app(store, runner, acquisition=source, background=False),
        base_url="http://127.0.0.1",
        headers=HEADERS,
    )
    return store, runner, worker, api, event


def test_round_keeps_distinct_owners_for_identical_questions(tmp_path, clock):
    store, _, _, api, event = workbench(tmp_path, ["news", "organizations"])
    question = "核对实际开通日"
    assignments = [
        dict(role=role, line=line, question=question, target=target, time_scope="window",
             done_when="逐字核查", priority=4)
        for role, line, target in (("news", "公告线", "公告"), ("organizations", "监管线", "监管文件"))
    ]
    with store.connection(True) as c:
        r = db.require(c, "tracking_rounds", event["round"])
        research.save_plan(c, r, Plan(rationale="两条线独立查证", tasks=assignments))
        tid = c.execute("SELECT id FROM research_tasks WHERE round_id=? AND role='organizations'",
                        (r["id"],)).fetchone()[0]
        excerpts = [{"question": question, "task_id": tid}, {"question": question}]
        c.execute("INSERT INTO round_evidence_previews VALUES(?,?,?,?,?)",
                  (r["id"], clock.utc(), clock.utc(), json.dumps(excerpts), "[]"))
    detail = api.get(f"/api/events/{event['id']}/rounds", params={"rid": r["id"]}).json()[0]
    assert {t["role"]: t["line_key"] for t in detail["tasks"]} == {"news": "公告线", "organizations": "监管线"}
    exact, legacy = detail["evidence_preview"]["excerpts"]
    assert (exact["task_id"], exact["line_key"]) == (tid, "监管线")
    assert (legacy["task_id"], legacy["line_key"]) == (None, "")
    item = api.post(f"/api/events/{event['id']}/steering", json={
        "nonce": "regulator-only", "goal_version": 1, "round_id": r["id"],
        "text": "请补核", "line_key": exact["line_key"],
    }).json()
    assert item["role"] == "organizations"
    with store.connection() as c:
        assert db.require(c, "research_inputs", item["id"])["target_task_id"] == tid


def test_interruption_and_question_answer_reach_asking_session(tmp_path, clock):
    store, runner, worker, api, event = workbench(tmp_path, ["news"])
    endpoint = f"/api/events/{event['id']}/steering"
    before = {"goal_version": 1, "nonce": "before", "text": "分开报告计划与实际开通"}
    initial = api.post(endpoint, json=before)
    assert initial.status_code == 201
    original = initial.json()["id"]
    assert api.post(endpoint, json=before).json()["id"] == original
    assert api.post(endpoint, json={**before, "text": "另一项请求"}).status_code == 409
    calls = []
    base = runner.call

    def interactive(role, ctx):
        result, tokens = base(role, ctx)
        if role == "plan":
            assert [i["id"] for i in ctx["用户调查要求"]] == [original]
        if role == "news":
            calls.append(ctx)
            if len(calls) == 1:
                assert [i["id"] for i in ctx["用户调查要求"]] == [original]
                during = api.post(endpoint, json={"goal_version": 1, "nonce": "during", "text": "请复核正式运营条件"})
                assert during.status_code == 201 and during.json()["role"] == "news"
                result.user_question = "您想追踪开放试乘，还是正式收费载客？"
            else:
                assert {i["text"] for i in ctx["用户调查要求"]} == {"请复核正式运营条件"}
                assert api.get(endpoint).json()["items"][0]["status"] == "awaiting_user"
        return result, tokens

    runner.call = interactive
    assert worker.step() and len(calls) == 2
    items = api.get(endpoint).json()["items"]
    assert next(i for i in items if i["id"] == original)["role"] == "news"
    question = next(i for i in items if i["kind"] == "question")
    assert question["line_key"] and question["status"] == "awaiting_user"
    reply = {"goal_version": 1, "nonce": "reply", "text": "只看正式营业载客"}
    response = api.post(f"{endpoint}/{question['id']}/answer", json=reply)
    assert response.status_code == 200
    assert response.json()["round_id"] != question["origin_round_id"]
    assert api.post(f"{endpoint}/{question['id']}/answer", json=reply).json()["id"] == question["id"]
    assert api.post(f"{endpoint}/{question['id']}/answer", json={**reply, "text": "改口"}).status_code == 409
    runner.call = base
    for _ in range(8):
        with store.connection() as c:
            q = db.require(c, "research_inputs", question["id"])
            if q["received_task_id"]:
                break
        assert worker.step()
    with store.connection() as c:
        q = db.require(c, "research_inputs", question["id"])
        assert q["received_task_id"]
        contexts = [
            json.loads(p[0])
            for p in c.execute(
                "SELECT i.payload FROM run_inputs i JOIN runs r ON r.id=i.run_id WHERE r.role='news'"
            )
        ]
        assert any(i["answer"] == reply["text"] for ctx in contexts for i in ctx.get("用户调查要求", []))


def test_user_input_at_synthesis_goes_to_next_round_and_never_changes_old_report(tmp_path, clock):
    store, runner, worker, api, event = workbench(tmp_path, ["news"])
    endpoint = f"/api/events/{event['id']}/steering"
    base = runner.call
    late = []

    def late_input(role, ctx):
        if role == "study" and not late:
            res = api.post(
                endpoint,
                json={"goal_version": 1, "round_id": event["round"], "nonce": "late", "text": "分别核实开始与结束时间"},
            )
            assert res.status_code == 201
            late.append(res.json())
        return base(role, ctx)

    runner.call = late_input
    assert worker.step()
    assert late and late[0]["origin_round_id"] == event["round"] != late[0]["round_id"]
    with store.connection() as c:
        assert not db.one(c, "SELECT * FROM research_input_reports WHERE input_id=?", (late[0]["id"],))
    other = new_event(store, title="另一条铁路")
    assert (
        api.post(
            endpoint, json={"goal_version": 1, "round_id": other["round"], "nonce": "foreign", "text": "核实"}
        ).status_code
        == 422
    )
    with store.connection(True) as c:
        workflow.edit_goal(c, event["id"], 1, scope="新调查范围")
    assert api.get(endpoint).json()["items"][0]["status"] == "superseded"
    assert api.post(endpoint, json={"goal_version": 1, "nonce": "old", "text": "核实"}).status_code == 409


def test_open_questions_remain_visible_beyond_first_history_page(tmp_path, clock):
    store, _, _, api, event = workbench(tmp_path, ["news"])
    endpoint = f"/api/events/{event['id']}/steering"
    with store.connection(True) as c:
        r = db.require(c, "tracking_rounds", event["round"])
        steering.ask(c, r, "main", "", "请确认成本口径")
        steering.ask(c, r, "main", "", "请确认成本口径")
        for i in range(51):
            steering.submit(
                c, event["id"], steering.Instruction(goal_version=1, nonce=f"query{i}", text=f"核查 {i}")
            )
        assert steering.question_count(c, event["id"]) == 1
    feed = api.get(endpoint).json()
    oldest = feed["pending_questions"][0]
    assert len(feed["items"]) == 50 and oldest["id"] < feed["items"][-1]["id"]
    assert oldest["status"] == "awaiting_user"
    assert any(
        i["id"] == oldest["id"]
        for i in api.get(endpoint, params={"before": feed["items"][-1]["id"]}).json()["items"]
    )
    assert (
        api.post(
            f"{endpoint}/{oldest['id']}/answer", json={"goal_version": 1, "nonce": "ok", "text": "含税"}
        ).status_code
        == 200
    )
    assert not api.get(endpoint).json()["pending_questions"]


def test_undelivered_instruction_is_retried_with_next_source_window(tmp_path, clock):
    store, _, _, api, event = workbench(tmp_path, ["news"])
    endpoint = f"/api/events/{event['id']}/steering"
    item = api.post(endpoint, json={"goal_version": 1, "nonce": "retry", "text": "核实验收进度"}).json()
    with store.connection(True) as c:
        c.execute("UPDATE tracking_rounds SET state='waiting' WHERE id=?", (event["round"],))
        c.execute("UPDATE jobs SET status='done' WHERE id=?", (event["job"],))
        rid, _ = workflow.request_round(c, event["id"], "定时收集", key="next")
        steering.recover_unread(c, db.require(c, "tracking_rounds", rid))
    latest = api.get(endpoint).json()["items"][0]
    assert (
        latest["id"] == item["id"]
        and latest["origin_round_id"] == event["round"]
        and latest["round_id"] == rid
    )


def test_target_line_omitted_by_next_planner_still_returns_to_that_report(tmp_path, clock):
    store, runner, worker, api, event = workbench(tmp_path, ["news", "specialist"])
    endpoint = f"/api/events/{event['id']}/steering"
    assert worker.step()
    line = next(i for i in api.get(endpoint).json()["lines"] if i["role"] == "specialist")
    # This investigator has no usable documents in its prior assignment. The new
    # round can still send the user instruction and supply its own admitted archive.
    with store.connection(True) as c:
        c.execute(
            """UPDATE research_tasks SET input_ids='[]' WHERE id=(
                   SELECT last_task FROM investigation_lines WHERE event_id=? AND line_key=?)""",
            (event["id"], line["line_key"]),
        )
    runner.roles = ["news"]
    item = api.post(
        endpoint,
        json={
            "goal_version": 1,
            "round_id": event["round"],
            "nonce": "target",
            "line_key": line["line_key"],
            "text": "复核上一版专项结论",
        },
    ).json()
    assert item["round_id"] != item["origin_round_id"]
    for _ in range(8):
        with store.connection() as c:
            if db.one(c, "SELECT 1 FROM research_input_reports WHERE input_id=?", (item["id"],)):
                break
        assert worker.step()
    with store.connection() as c:
        assignment = db.require(c, "research_inputs", item["id"])
        task = db.require(c, "research_tasks", assignment["received_task_id"])
        assert task["role"] == "specialist" and task["round_id"] == item["round_id"]
        round_view = workflow.list_rounds(c, event["id"], task["round_id"])[0]
        assert next(t for t in round_view["tasks"] if t["id"] == task["id"])["line_key"] == line["line_key"]
        assert (
            c.execute(
                """SELECT s.round_id FROM research_input_reports h
                  JOIN knowledge_snapshots s ON s.id=h.snapshot_id WHERE h.input_id=?""",
                (item["id"],),
            ).fetchone()[0]
            == item["round_id"]
        )
