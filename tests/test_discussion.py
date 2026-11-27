"""Exercise human follow-ups through the same API, queue, snapshots and grounded model receipt."""
import json

from fastapi.testclient import TestClient
import pytest

from evosight import agents, db, discussion, forecast, workflow
from evosight.api import make_app
from evosight.worker import Worker
from test_workflow import HEADERS, clock, new_event, setup


@pytest.fixture
def case(tmp_path, clock, monkeypatch):
    store, source, runner, worker = setup(tmp_path, ["news"])
    event = new_event(store)["id"]
    assert worker.step() and worker.step()
    with store.connection(True) as c:
        rev = db.one(
            c,
            "SELECT r.* FROM revisions r JOIN predictions p ON p.id=r.prediction_id WHERE p.event_id=?",
            (event,),
        )
        assert rev
        c.execute("UPDATE events SET status='paused' WHERE id=?", (event,))
    contexts, invalid = [], []
    other = runner.call

    def answer(role, ctx):
        if not role.startswith("discuss_"):
            return other(role, ctx)
        contexts.append((role, ctx))
        key = next(iter(ctx["sources"]), None)
        if invalid:
            key = invalid.pop()
        return agents.DiscussionAnswer(
            kind="analysis" if key else "insufficient",
            answer=f"按登记的材料解释 [{key}]。" if key else "本事件尚无材料，需要官方记录。",
            citations=[key] if key else [],
        ), (82, 22)

    monkeypatch.setattr(runner, "call", answer)
    with TestClient(
        make_app(store, runner, background=False, acquisition=source),
        base_url="http://127.0.0.1",
        headers=HEADERS,
    ) as client:
        yield store, worker, client, event, rev, contexts, invalid


def test_original_input_followups_and_evidence_links(case):
    store, worker, api, event, v, contexts, _ = case
    base = f"/api/events/{event}/discussion"
    params = {"basis": "original", "revision": v["id"]}
    first = {**params, "question": "为什么是这个概率？", "nonce": "first-question"}
    with store.connection(True) as c:
        before = db.rows(c, "SELECT * FROM revisions")
        original = db.one(
            c,
            "SELECT rr.run_id,i.payload FROM revision_runs rr JOIN run_inputs i ON i.run_id=rr.run_id WHERE rr.revision_id=?",
            (v["id"],),
        )
        data = json.loads(original["payload"])
        replacement = "提问时才知道的更正记录"
        db.node(c, event, "correction", "后续说明", replacement)
    assert api.get(base, params=params).json()["anchor"]["run_id"] == original["run_id"]
    archive = api.get(base + "/archive/" + str(v["id"])).json()["run"]
    assert (
        archive["id"] == original["run_id"] and archive["input"] == data and archive["output"]["p"] == v["p"]
    )
    sent = api.post(base, json=first)
    assert sent.status_code == 202, sent.text
    assert api.post(base, json=first).json() == sent.json()
    assert api.post(base, json={**first, "question": "覆盖原来的问题"}).status_code == 422
    assert api.post(base, json={**first, "nonce": "second-question"}).status_code == 422
    assert worker.step()
    role, ctx = contexts[-1]
    assert role == "discuss_codex" and ctx["forecast_run"]["input"] == data
    assert ctx["forecast_run"]["output"]["p"] == v["p"] and ctx["registered_forecast"]["id"] == v["id"]
    assert ctx["original"]["index"]["selected_keys"] and ctx["history"] == [] and "latest" not in ctx
    assert replacement not in db.dump(ctx)
    assert any(k.startswith("F") for k in ctx["sources"]) and any(k.startswith("K") for k in ctx["sources"])
    turn = api.get(base, params=params).json()["turns"][0]
    assert turn["status"] == "done" and turn["model"] == "gpt-6.2-sol" and turn["effort"] == "low"
    assert turn["citations"][0]["href"] == f"#/events/{event}/p/{v['prediction_id']}/{v['id']}"
    assert (
        api.post(
            base, json={**first, "nonce": "follow-up-2", "provider": "dsh", "question": "如果前提不成立呢？"}
        ).status_code
        == 202
    )
    assert worker.step()
    assert contexts[-1][0] == "discuss_dsh" and contexts[-1][1]["history"][0]["answer"] == turn["answer"]
    assert api.get(base, params={"basis": "latest"}).json()["turns"] == []
    assert api.get(base, params={**params, "before": turn["id"]}).json()["turns"] == []
    with store.connection() as c:
        assert db.rows(c, "SELECT * FROM revisions") == before
        assert c.execute("SELECT count(*) FROM discussion_turns WHERE run_id IS NOT NULL").fetchone()[0] == 2


def test_latest_is_frozen_on_send_and_separate_from_original(case):
    store, worker, api, event, v, contexts, _ = case
    base = f"/api/events/{event}/discussion"
    with store.connection(True) as c:
        initial = workflow.latest(c, event)["id"]
        original_claim = workflow.snapshot_claims(c, initial)[0]
        cite = original_claim["citations"][0]
    params = {"revision": v["id"], "basis": "latest"}
    assert (
        api.post(
            base, json={**params, "question": "新证据还支持原判断吗？", "nonce": "freeze-before-update"}
        ).status_code
        == 202
    )
    with store.connection(True) as c:
        text = "后来订正为附条件的验收计划"
        workflow.correct_claim(
            c, event, initial, original_claim["id"], text, "订正登记措辞", cite["fragment_id"], cite["quote"]
        )
        latest = workflow.latest(c, event)["id"]
        assert latest != initial
    assert worker.step()
    ctx = contexts[-1][1]
    assert ctx["latest"]["index"]["snapshot_id"] == initial and text not in db.dump(ctx)
    again = api.post(base, json={**params, "question": "现在有什么变化？", "nonce": "after-correction"})
    assert again.status_code == 202 and worker.step()
    ctx = contexts[-1][1]
    assert (
        ctx["latest"]["index"]["snapshot_id"] == latest and ctx["original"]["index"]["snapshot_id"] == initial
    )
    old = set(ctx["original"]["index"]["selected_keys"])
    new = set(ctx["latest"]["index"]["selected_keys"])
    assert old - new and new - old
    assert all(ctx["sources"][k]["period"] == ["latest"] for k in new - old)
    assert api.get(base, params={"revision": v["id"], "basis": "original"}).json()["turns"] == []


def test_ownership_manual_fallback_and_legacy_matching(case):
    store, worker, api, event, v, contexts, _ = case
    base = f"/api/events/{event}/discussion"
    with store.connection(True) as c:
        other = db.add_event(c, "另一个独立事件", "另一事件的目标", [], status="paused")
        manual = forecast.revise(c, v["prediction_id"], 40, "人工依据", json.loads(v["refs"]), "人工")
        dsh_revision = forecast.revise(
            c, v["prediction_id"], 42, "较早的模型判断", json.loads(v["refs"]), "dashscope/kimi-k3.1-pro"
        )
        original = db.one(c, "SELECT * FROM revision_runs WHERE revision_id=?", (v["id"],))
        c.execute("DELETE FROM revision_runs WHERE revision_id=?", (v["id"],))
        discussion.link_archived_runs(c)
        assert db.one(c, "SELECT * FROM revision_runs WHERE revision_id=?", (v["id"],)) == original
        # Two otherwise identical revisions in the same recorded second cannot identify one historical call.
        c.execute("DELETE FROM revision_runs WHERE revision_id=?", (v["id"],))
        c.execute(
            "INSERT INTO revisions(prediction_id,p,reason,refs,model,created_at) SELECT prediction_id,p,reason,refs,model,created_at FROM revisions WHERE id=?",
            (v["id"],),
        )
        discussion.link_archived_runs(c)
        assert not db.one(c, "SELECT 1 FROM revision_runs WHERE run_id=?", (original["run_id"],))
    assert (
        api.get(
            f"/api/events/{other}/discussion", params={"revision": v["id"], "basis": "original"}
        ).status_code
        == 404
    )
    assert api.get(f"/api/events/{other}/discussion/archive/{v['id']}").status_code == 404
    assert api.get(base + f"/archive/{manual}").json()["run"] is None
    old_discussion = api.get(base, params={"revision": dsh_revision, "basis": "original"}).json()
    assert old_discussion["anchor"]["model"] == "dashscope/kimi-k3.1-pro"
    assert old_discussion["default_provider"] == "codex"
    assert api.get(base, params={"basis": "original"}).status_code == 422
    prompt = {
        "revision": manual,
        "basis": "original",
        "nonce": "manual-history-test",
        "question": "请解释人工登记的依据",
    }
    assert api.post(f"/api/events/{other}/discussion", json=prompt).status_code == 404
    assert api.post(base, json=prompt).status_code == 202 and worker.step()
    assert contexts[-1][0] == "discuss_codex" and "forecast_run" not in contexts[-1][1]
    assert contexts[-1][1]["registered_forecast"]["reason"] == "人工依据"
    other_base = f"/api/events/{other}/discussion"
    assert (
        api.post(other_base, json={"nonce": "empty-event-question", "question": "现有材料有什么？"}).status_code
        == 202
        and worker.step()
    )
    empty = api.get(other_base).json()["turns"]
    assert len(empty) == 1 and not empty[0]["citations"] and contexts[-1][1]["sources"] == {}


def test_invalid_citations_retry_preserves_one_question_and_budget(case):
    store, worker, api, event, v, contexts, invalid = case
    invalid.extend(["D999999", "K888888"])
    base = f"/api/events/{event}/discussion"
    result = api.post(base, json={"question": "证据来自哪里？", "nonce": "bad-citation-test"}).json()
    job = result["job_id"]
    for i in range(2):
        with store.connection(True) as c:
            c.execute("UPDATE jobs SET retry_at=NULL WHERE id=?", (job,))
        assert worker.step()
    turn = api.get(base).json()["turns"][0]
    assert turn["status"] == "failed" and turn["answer"] is None
    with store.connection() as c:
        assert c.execute("SELECT count(*) FROM model_receipts WHERE job_id=?", (job,)).fetchone()[0] == 0
    with store.connection(True) as c:
        spent = db.usage(c)["daily_used"]
        c.execute(
            "UPDATE settings SET value=? WHERE key='daily_calls'",
            (str(spent + db.config(c)["verify_reserve"]),),
        )
    assert api.post(f"/api/jobs/{job}/retry", json={}).json() == {"job": job} and worker.step()
    assert api.get(base).json()["turns"][0]["status"] == "deferred"
    with store.connection() as c:
        assert db.usage(c)["daily_used"] == spent
        limits = db.config(c)
    limits.update(daily_calls=1000, monthly_calls=30000)
    saved = api.post("/api/settings", json=limits)
    assert saved.is_success and saved.json()["daily_calls"] == 1000
    assert worker.step()
    turns = api.get(base).json()["turns"]
    assert len(turns) == 1 and turns[0]["status"] == "done" and turns[0]["job_id"] == job
    with store.connection() as c:
        assert db.usage(c)["daily_used"] == spent + 1


def test_receipt_survives_worker_restart_before_answer(case):
    store, worker, api, event, v, contexts, _ = case
    base = f"/api/events/{event}/discussion"
    result = api.post(base, json={"question": "最近的证据是什么？", "nonce": "recover-chat-receipt"}).json()
    with store.connection() as c:
        job = db.require(c, "jobs", result["job_id"])
        turn = discussion.job_turn(c, event, job["id"], result["turn_id"])
    ctx = json.loads(turn["context"])
    _, _, run = worker.call_recorded(job, "discuss_" + turn["provider"], ctx)
    recovered = Worker(store, worker.runner, acquisition=worker.acquisition)
    assert recovered.step()
    with store.connection() as c:
        assert c.execute("SELECT count(*) FROM runs WHERE job_id=?", (job["id"],)).fetchone()[0] == 1
        assert db.require(c, "discussion_turns", turn["id"])["run_id"] == run
    assert len(contexts) == 1


def test_event_discussion_freezes_the_read_marker(case):
    store, worker, api, event, _, contexts, _ = case
    base = f"/api/events/{event}/discussion"
    with store.connection(True) as c:
        seen = c.execute("SELECT max(id) FROM nodes WHERE event_id=?", (event,)).fetchone()[0]
        db.mark_read(c, event, db.read_token(c, event, seen))
        change = db.node(c, event, "judgment", "条件变化", "等待主管部门进一步确认")
    assert api.post(base, json={"question": "上次阅读后判断变了什么？", "nonce": "read-boundary-01"}).status_code == 202
    with store.connection(True) as c:
        db.mark_read(c, event, db.read_token(c, event, change))
    assert worker.step()
    ctx = contexts[-1][1]
    assert ctx["latest"]["reading_progress"] == {
        "through_node": seen,
        "changes": [{"kind": "judgment", "count": 1}],
    }
    assert ctx["sources"][f"N{change}"]["after_last_read"] is True
