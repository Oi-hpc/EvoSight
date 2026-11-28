from fastapi.testclient import TestClient

from evosight import db, delivery, steering, steering_reports, workflow
from evosight.api import make_app
from evosight.contracts import Citation, DeliveryAnswer
from test_workflow import BASE, HEADERS, clock, new_event, setup


def test_requirement_reaches_the_published_answer_and_keeps_its_position(tmp_path, clock):
    store, source, runner, worker = setup(tmp_path, ["news"])
    event = new_event(store)
    api = TestClient(make_app(store, runner, background=False, acquisition=source),
                     base_url="http://127.0.0.1", headers=HEADERS)
    route = f"/api/events/{event['id']}/steering"
    question = "请核查发布验收方案的机构"
    receipt = api.post(route, json={"text": question, "nonce": "report-check",
                                    "round_id": event["round"], "goal_version": 1})
    assert receipt.status_code == 201, receipt.text
    item = receipt.json()
    original = runner.call

    def call(role, ctx):
        result, usage = original(role, ctx)
        if role == "plan":
            result.checklist = ["是否已经实际载客"]
        if role == "study":
            request, = ctx["用户补充与答复"]
            assert request["id"] == item["id"] and request["text"] == question and request["check_id"] == 2
            fragment = next(f for f in ctx["交付可引用原文"] if BASE in f["text"])
            result.delivery = [
                DeliveryAnswer(check_id=1, state="unknown", answer="仍缺正式载客公告"),
                DeliveryAnswer(check_id=2, state="confirmed", answer=BASE,
                               citations=[Citation(fragment_id=fragment["id"], quote=BASE)]),
            ]
            result.judgment = BASE
        return result, usage

    runner.call = call
    assert worker.step()
    response = api.get(route).json()["items"][0]
    report = response["report"]
    assert response["status"] == "reported" and response["role"] == "news"
    assert report["outcome"] == "added" and report["summary_excerpt"] == BASE
    published = api.get(f"/api/events/{event['id']}/knowledge", params={"revision": report["snapshot_id"]}).json()
    answer = published["delivery"][report["position"] - 1]
    assert answer["answer"] == report["after"] == BASE and answer["question"] == question
    assert answer["citations"] and not report["before"]


def test_before_after_uses_published_versions_and_reports_rejections(tmp_path, clock):
    store, _, _, _ = setup(tmp_path, ["news"])
    event = new_event(store)
    with store.connection(True) as c:
        title = "分别核查验收和实际载客"
        item = steering.submit(c, event["id"], steering.Instruction(text=title, nonce="check-editions", goal_version=1))
        plan, requests = steering_reports.checklist({"checklist": [title]}, [{"id": item, "text": title}])
        assert plan["checklist"] == [title] and requests[0]["check_id"] == 1
        parent = None

        def publish(state, answer="", reason=""):
            nonlocal parent
            version = c.execute(
                """INSERT INTO knowledge_snapshots(event_id,round_id,generation,parent_id,goal_version,created_at,
                   gaps,summary,changes) SELECT ?,?,count(*)+1,?,1,?,'[]',?,'[]' FROM knowledge_snapshots
                   WHERE round_id=?""", (event["id"], event["round"], parent, clock.utc(), answer, event["round"]),
            ).lastrowid
            delivery.save(c, version, [dict(position=1, question=title, state=state, answer=answer,
                                             reason=reason, citations=[])])
            steering_reports.save(c, version, requests)
            parent = version
            return steering_reports.for_input(c, item)

        prior = publish("unknown", "在等运营方说明")
        updated = publish("unknown", "已确认验收口径，仍缺正式载客公告")
        assert updated["previous_snapshot_id"] == prior["snapshot_id"]
        assert updated["before"] == prior["after"] and updated["outcome"] == "changed"
        assert publish("unknown", updated["after"])["outcome"] == "unchanged"
        refused = publish("rejected", reason="引文无法定位")
        assert refused["outcome"] == "rejected" and refused["before"] and not refused["after"]
        assert refused["reason"] == "引文无法定位" and not refused["summary_excerpt"]
        assert publish("missing")["outcome"] == "missing"
        assert workflow.knowledge(c, event["id"], revision=prior["snapshot_id"])["delivery"][0]["answer"] == prior["after"]


def test_identical_requests_keep_independent_answers_between_editions(tmp_path, clock):
    store, _, _, _ = setup(tmp_path, ["news"])
    event = new_event(store)
    text = "请核查这个数字"
    with store.connection(True) as c:
        items = [
            {"id": steering.submit(c, event["id"], steering.Instruction(text=text, nonce=n, goal_version=1)),
             "text": text} for n in ("news-price", "owner-discount")
        ]
        # First the news researcher replies; the owner researcher joins the next publication.
        parent = None
        for generation, inputs in enumerate((items[:1], items), 1):
            plan, requests = steering_reports.checklist({}, inputs)
            report = c.execute(
                """INSERT INTO knowledge_snapshots(event_id,round_id,generation,parent_id,goal_version,created_at,
                   gaps,summary,changes) VALUES(?,?,?,?,1,?,'[]','','[]')""",
                (event["id"], event["round"], generation, parent, clock.utc()),
            ).lastrowid
            checked = delivery.inspect(plan, [
                DeliveryAnswer(check_id=requests[0]["check_id"], state="unknown", answer="正式售价待公告")
            ], {})
            delivery.save(c, report, checked)
            steering_reports.save(c, report, requests)
            parent = report
        first, second = (steering_reports.for_input(c, i["id"]) for i in items)
        assert first["position"] != second["position"]
        assert first["before"] == first["after"] == "正式售价待公告"
        assert first["outcome"] == "unchanged"
        assert second["outcome"] == "missing" and second["before"] == second["after"] == ""
