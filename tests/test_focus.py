"""Chat commands register scoped, durable collection and leave verifiable per-round receipts."""
from datetime import timedelta
import json

import pytest
from fastapi.testclient import TestClient

from evosight import agents, db, discussion, feeds, focus, materials, people, workflow
from evosight.api import make_app
from evosight.contracts import Citation, PersonLead
from evosight.worker import Worker
from test_people import case as person_case, PERSON
from test_workflow import clock, HEADERS, HOST, new_event, setup


def install_answer(worker, monkeypatch, items):
    original = worker.runner.call
    calls = []

    def call(role, ctx):
        if not role.startswith("discuss_"):
            return original(role, ctx)
        calls.append(ctx["question"])
        return agents.DiscussionAnswer(
            kind="tracking",
            answer="提交本事件的跟踪要求。",
            citations=[],
            add_focus=[
                focus.Addition(kind=kind, target=target, angle=angle, instruction=ctx["question"])
                for kind, target, angle in items
            ],
        ), (10, 6)

    monkeypatch.setattr(worker.runner, "call", call)
    return calls


def test_event_command_immediate_round_recovery_and_each_subsequent_cycle(tmp_path, clock, monkeypatch):
    store, source, runner, worker = setup(tmp_path, ["news"])
    event = new_event(store)["id"]
    assert worker.step()
    base = f"/api/events/{event}"
    with store.connection(True) as c:
        other = db.add_event(c, "单独事件", "不同研究范围", [], status="paused")
        next_before = db.require(c, "events", event)["next_run"]
        c.execute("update jobs set retry_at='2099-01-01' where kind='forecast'")
    checks = []
    original_search = source.targeted

    def search(query):
        if "换乘客流" not in query:
            return original_search(query)
        checks.append(query)
        title = "海岚市域线换乘客流公告"
        date = (
            clock.utc(clock.dt - timedelta(days=35))
            if len(checks) < 3
            else clock.utc(clock.dt - timedelta(minutes=5))
        )
        text = "海岚市域线换乘客流的正式口径已公布。"
        url = HOST + "/scope/" + ("early" if len(checks) < 3 else "current")
        res = feeds.Resource(text.encode(), url, "text/plain")
        ex = materials.Extract("text", [("公告正文", text, url)], title=title, published_at=date)
        return [({"url": url, "title": title, "summary": text, "published_at": date}, res, ex)]

    monkeypatch.setattr(source, "targeted", search)
    calls = install_answer(worker, monkeypatch, [("topic", "换乘客流", "")])
    with TestClient(
        make_app(store, runner, background=False, acquisition=source),
        base_url="http://127.0.0.1",
        headers=HEADERS,
    ) as api:
        msg = {"question": "请把换乘客流加入当前事件的每轮收集。", "nonce": "scope-register-case"}
        req = api.post(base + "/discussion", json=msg)
        assert req.status_code == 202
        job_id = req.json()["job_id"]
        with store.connection() as c:
            j = db.require(c, "jobs", job_id)
            t = db.require(c, "discussion_turns", req.json()["turn_id"])
        worker.call_recorded(j, "discuss_dsh", json.loads(t["context"]))
        worker = Worker(store, runner, acquisition=source)
        assert worker.step()
        assert calls == [msg["question"]]
        assert api.post(base + "/discussion", json=msg).json() == req.json()
        scope = api.get(base + "/focus").json()[0]
        sid, rid = scope["id"], scope["first_round_id"]
        assert scope["first_state"] == "queued" and scope["last_round"] is None
        assert api.get(f"/api/events/{other}/focus").json() == []
        assert api.post(f"/api/events/{other}/focus/{sid}/pause", json={}).status_code == 422
        with store.connection() as c:
            assert db.require(c, "events", event)["next_run"] == next_before
            assert db.require(c, "tracking_rounds", rid)["reasons"] == '["新增收集范围"]'
        assert (
            api.post(base + "/discussion", json={**msg, "nonce": "same-target-again"}).is_success
            and worker.step()
        )
        read = api.get(base + "/discussion").json()["turns"]
        assert [turn["focus_actions"][0]["added"] for turn in read] == [1, 0]
        assert all(turn["focus_actions"][0]["round_id"] == rid for turn in read)
        assert worker.step()
        first = api.get(base + "/focus").json()[0]
        assert first["last_round"] == rid and first["status"] == "found"
        for no in (1, 2):
            clock.dt += timedelta(hours=6)
            with store.connection(True) as c:
                c.execute("UPDATE jobs SET retry_at='2099-01-01' WHERE status='queued' AND kind!='collect'")
            for _ in range(6):
                worker.step()
                with store.connection() as c:
                    if (
                        c.execute("select count(*) from focus_checks where focus_id=?", (sid,)).fetchone()[0]
                        >= no + 1
                    ):
                        break
        with store.connection() as c:
            history = db.rows(c, "select * from focus_checks where focus_id=? order by round_id", (sid,))
            assert len(history) == 3 and len(checks) == 3
            assert [r["status"] for r in history] == ["found", "checked", "found"]
            assert json.loads(history[1]["documents"]) == [] and json.loads(history[2]["documents"])
        last = api.get(base + "/focus").json()[0]
        assert last["documents"] == [
            {"id": doc, "title": "海岚市域线换乘客流公告"} for doc in json.loads(history[2]["documents"])
        ]
        for turn in api.get(base + "/discussion").json()["turns"]:
            action = turn["focus_actions"][0]
            assert action["last_round"] == last["last_round"] and action["documents"] == last["documents"]
        assert api.post(base + f"/focus/{sid}/pause", json={}).is_success
        assert not api.get(base + "/focus").json()[0]["active"]
        clock.dt += timedelta(hours=6)
        with store.connection(True) as c:
            later, _ = workflow.request_round(c, event, "定时", end=clock.utc())
            round_ = db.require(c, "tracking_rounds", later)
        assert focus.collect(worker, round_) == [] and len(checks) == 3


def test_requested_person_appears_before_identity_and_all_pinned_people_are_checked(person_case, monkeypatch):
    store, worker, api, e, source, clock = person_case
    base = f"/api/events/{e}"
    names = [PERSON, "其他人物甲", "其他人物乙"]
    install_answer(worker, monkeypatch, [("person", n, "") for n in names])
    q = "请持续跟踪" + "、".join(names) + "，每轮都核查公开动态。"
    assert api.post(base + "/discussion", json={"question": q, "nonce": "three-pinned-people"}).is_success
    assert worker.step()
    unverified = api.get(base + "/people").json()
    assert {t["target"] for t in unverified["pending_people"]} == set(names)
    rid = unverified["pending_people"][0]["first_round_id"]
    assert worker.step()
    data = api.get(base + "/people").json()
    person = next(t for t in data["people"] if t["name"] == PERSON)
    assert person["requested"] and {f["target"] for f in data["pending_people"]} == set(names[1:])
    clock.dt += timedelta(hours=6)
    with store.connection(True) as c:
        old_round = db.require(c, "tracking_rounds", rid)
        for i, name in enumerate(names[1:]):
            identity = f"海岚运营部门{i}负责人"
            quote = f"{identity}{name}出席海岚市域线公开说明会。"
            url = f"{HOST}/official/staff/{i}"
            doc = feeds.ingest(
                c, e, url, {"url": url, "title": name + "公开资料", "summary": quote, "published_at": clock.utc()}
            )
            materials.summary_fragment(c, db.require(c, "documents", doc))
            frag = materials.fragments_for(c, [doc])[0]
            lead = PersonLead(
                name=name,
                identity=identity,
                role="参与者",
                relevance="出席该项目说明会",
                citation=Citation(fragment_id=frag["id"], quote=quote),
            )
            assert people.register(c, old_round, lead, {frag["id"]: frag})
        c.execute("UPDATE jobs SET retry_at='2099-01-01' WHERE status='queued' AND kind!='collect'")
    for _ in range(7):
        worker.step()
        with store.connection() as c:
            recent = c.execute("SELECT max(round_id) FROM focus_checks").fetchone()[0]
            if recent > rid:
                break
    with store.connection() as c:
        assert c.execute("SELECT count(*) FROM focus_checks WHERE round_id=?", (recent,)).fetchone()[0] == 3
        assert {
            r[0]
            for r in c.execute(
                "select channel from person_checks where target_id=? and round_id=?", (person["id"], recent)
            )
        } == {"x", "weibo", "xiaohongshu", "news"}
        assert len(api.get(base + "/focus").json()) == 3
        for watched in api.get(base + "/focus").json():
            assert watched["person_target_id"] is not None
            assert {"x", "weibo", "xiaohongshu", "news"} <= {
                row[0]
                for row in c.execute(
                    "SELECT channel FROM person_checks WHERE target_id=? AND round_id=?",
                    (watched["person_target_id"], recent),
                )
            }


def test_only_the_current_users_instruction_can_schedule_work():
    def proposal(text):
        return focus.Addition(kind="person", target="李斌", instruction=text)

    for question, instruction in [
        ("为什么看不到李斌？", "请跟踪李斌"),
        ("如果我说请跟踪李斌，会发生什么？", "请跟踪李斌"),
        ("请不要跟踪李斌", "跟踪李斌"),
        ("新闻写道请跟踪李斌，我想知道原因。", "请跟踪李斌"),
    ]:
        with pytest.raises(ValueError):
            focus.check_request({"question": question, "can_add_focus": True}, [proposal(instruction)])
    with pytest.raises(ValueError):
        focus.check_request({"question": "请跟踪李斌", "can_add_focus": False}, [proposal("请跟踪李斌")])
    focus.check_request({"question": "另外，人物可以跟踪下李斌", "can_add_focus": True}, [proposal("人物可以跟踪下李斌")])


def test_subject_and_qualifying_topic_share_one_scope_unless_separately_requested():
    q = "请跟踪李斌，以后每轮收集与蔚来 ET5T 有关的公开动向。"
    p = focus.Addition(kind="person", target="李斌", instruction=q)
    context = focus.Addition(kind="topic", target="蔚来 ET5T", instruction=q)
    assert focus.check_request({"question": q, "can_add_focus": True}, [p, context]) == [p]
    q2 = "请跟踪李斌，另外单独跟踪蔚来 ET5T 的交付量。"
    items = [
        p.model_copy(update={"instruction": q2}),
        context.model_copy(update={"instruction": q2, "target": "交付量"}),
    ]
    assert focus.check_request({"question": q2, "can_add_focus": True}, items) == items
    q3 = "请分别跟踪李斌、交付量。"
    items = [
        p.model_copy(update={"instruction": q3}),
        context.model_copy(update={"instruction": q3, "target": "交付量"}),
    ]
    assert focus.check_request({"question": q3, "can_add_focus": True}, items) == items


def test_adding_to_running_round_gets_separate_fresh_inputs(tmp_path, clock):
    store, _, _, _ = setup(tmp_path)
    init = new_event(store)
    with store.connection(True) as c:
        c.execute("UPDATE jobs SET status='running' WHERE id=?", (init["job"],))
        rid, j = workflow.request_round(c, init["id"], "新增收集范围", key="test-action", fresh_inputs=True)
        assert rid != init["round"] and j != init["job"]
        assert workflow.request_round(c, init["id"], "新增收集范围", key="test-action", fresh_inputs=True) == (
            rid,
            j,
        )


@pytest.mark.parametrize(
    "message,archive,expected",
    [
        ("No results found.", False, "checked"),
        ("upstream timeout", False, "unavailable"),
        ("upstream timeout", True, "found"),
    ],
)
def test_public_search_failures_still_record_each_focus(
    tmp_path, clock, monkeypatch, message, archive, expected
):
    import ddgs
    from ddgs.exceptions import DDGSException

    class EmptySearch:
        def __init__(self, **kwargs):
            pass

        def text(self, *args, **kwargs):
            raise DDGSException(message)

    monkeypatch.setattr(ddgs, "DDGS", EmptySearch)
    store, _, _, worker = setup(tmp_path)
    start = new_event(store)
    worker.acquisition = materials.Acquisition()
    with store.connection(True) as c:
        r = db.require(c, "tracking_rounds", start["round"])
        c.execute(
            """INSERT INTO event_focus(event_id,kind,target,target_key,angle,angle_key,created_at,first_round_id)
               VALUES(?,'person','李斌','李斌','','',?,?)""",
            (start["id"], clock.utc(), r["id"]),
        )
        if archive:
            url = HOST + "/speaker"
            doc = feeds.ingest(
                c,
                start["id"],
                url,
                {
                    "url": url,
                    "title": "海岚说明会",
                    "summary": "此前，李斌出席海岚公开说明会。",
                    "published_at": clock.utc(clock.dt - timedelta(days=1)),
                },
            )
            materials.summary_fragment(c, db.require(c, "documents", doc))
    assert focus.collect(worker, r) == ([doc] if archive else [])
    with store.connection() as c:
        rec = db.one(c, "SELECT * FROM focus_checks")
        assert rec["status"] == expected
        assert ("公开检索不可用" in rec["detail"]) == (message != "No results found.")
