import json
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from evosight import board, db, materials, research, wakes, workflow
from evosight.api import make_app
from evosight.contracts import Plan, Report, ResearchLookup
from evosight.worker import Worker
from test_workflow import HEADERS, clock, new_event, setup


def published_line(directory):
    store, source, runner, worker = setup(directory, ["news", "specialist"])
    info = new_event(store)
    assert worker.step()
    with store.connection() as c:
        r = db.require(c, "tracking_rounds", info["round"])
        assert r["state"] == "published"
        owner = db.one(c, "SELECT * FROM investigation_lines WHERE role='specialist'")
        sender = db.one(c, "SELECT * FROM research_tasks WHERE role='news'")
    return store, runner, source, worker, r, owner, sender


def test_handoff_coalesces_then_resumes_original_line_after_restart(tmp_path, clock):
    store, runner, source, worker, r, owner, sender = published_line(tmp_path)
    with store.connection(True) as c:
        doc = json.loads(r["input_docs"])[0]
        before = workflow.latest(c, r["event_id"])
        old = db.one(c, """SELECT s.lane,s.last_run FROM agent_sessions s JOIN runs r ON r.id=s.last_run
                        WHERE r.role='specialist'""")
        first = wakes.post(c, r, sender["id"], owner["line_key"], "调查问题", "请核对刚归档的原页", [doc])
        assert first == wakes.post(c, r, sender["id"], owner["line_key"], "调查问题", "请核对刚归档的原页", [doc])
        second = wakes.post(c, r, sender["id"], owner["line_key"], "另一个问题", "同一附件里还有生效时间", [doc])
        assert first != second
        wake = c.execute("SELECT job_id FROM investigation_wakes").fetchone()[0]
        assert c.execute("SELECT count(*) FROM investigation_wakes").fetchone()[0] == 1
        # Simulate losing the worker after the lease was taken, before submitting inference.
        c.execute("UPDATE jobs SET status='running',owner='lost',lease=? WHERE id=?", (clock.later(-10), wake))
        assert not board.inbox(c, r["event_id"], owner["line_key"])
    worker = Worker(db.Store(tmp_path), runner, acquisition=source)
    assert worker.step()
    with store.connection() as c:
        handoff = db.one(c, "SELECT * FROM investigation_wakes")
        assert handoff["task_id"] and handoff["snapshot_id"] is None
        receipt = db.one(c, """SELECT s.*,i.payload AS prompt FROM run_sessions s JOIN run_inputs i USING(run_id)
                                JOIN runs r ON r.id=s.run_id WHERE r.job_id=?""", (wake,))
        assert receipt["lane"] == old["lane"] and receipt["previous_run"] == old["last_run"]
        prompt = json.loads(receipt["prompt"])
        assert [m["id"] for m in prompt["待处理交接"]] == [first, second]
        assert doc in {f["document_id"] for f in prompt["fragments"]}
        assert c.execute("SELECT count(*) FROM handoff_inbox WHERE received_run IS NULL").fetchone()[0] == 0
        assert db.require(c, "knowledge_snapshots", before["id"]) == before
    assert worker.step()
    with store.connection(True) as c:
        latest = workflow.latest(c, r["event_id"])
        assert latest["id"] != before["id"]
        assert db.require(c, "knowledge_snapshots", before["id"]) == before
        assert db.one(c, "SELECT snapshot_id FROM investigation_wakes")["snapshot_id"] == latest["id"]
        detail = workflow.list_rounds(c, r["event_id"], r["id"])[0]
        assert [(h["id"], h["snapshot_id"]) for h in detail["handoffs"]] == [(first, latest["id"]), (second, latest["id"])]
        assert receipt["run_id"] in [run["id"] for run in detail["runs"]]
        assert first == wakes.post(c, r, sender["id"], owner["line_key"], "调查问题", "请核对刚归档的原页", [doc])
        assert c.execute("SELECT count(*) FROM investigation_wakes").fetchone()[0] == 1
    assert runner.calls["specialist"] == 2


@pytest.mark.parametrize("invalidate", ["future", "future_revision", "foreign", "new_goal"])
def test_wake_respects_goal_and_source_cutoff(tmp_path, clock, invalidate):
    store, runner, source, worker, r, owner, sender = published_line(tmp_path)
    doc = json.loads(r["input_docs"])[0]
    with store.connection(True) as c:
        if invalidate == "future":
            c.execute("UPDATE documents SET published_at=? WHERE id=?", (clock.later(10), doc))
        elif invalidate == "future_revision":
            c.execute("UPDATE documents SET source_updated_at=? WHERE id=?", (clock.later(10), doc))
        elif invalidate == "foreign":
            c.execute("DELETE FROM event_docs WHERE event_id=? AND document_id=?", (r["event_id"], doc))
        else:
            workflow.edit_goal(c, r["event_id"], 1, scope="另一项调查")
    res = research.role_lookup(worker, r, sender, ResearchLookup(
        tool="handoff", query="请核对原文", recipient=owner["line_key"], document_ids=[doc],
    ))
    if invalidate != "new_goal":
        assert res["documents"] == []
    with store.connection() as c:
        assert c.execute("SELECT count(*) FROM investigation_wakes").fetchone()[0] == 0


def test_single_slot_drains_in_round_before_study(tmp_path, clock):
    store, _, replay, worker = setup(tmp_path, ["specialist", "news"])
    with store.connection(True) as c:
        c.execute("UPDATE settings SET value='1' WHERE key='max_agent_concurrency'")
    info = new_event(store)
    call = replay.call
    seen = []

    def handoff(role, ctx):
        if role == "news" and not ctx.get("工具回执"):
            target = next(l for l in ctx["事件黑板"]["lines"] if l["role"] == "specialist")
            return Report(lookup=ResearchLookup(tool="handoff", query="核对目标公告",
                          recipient=target["line_key"], document_ids=[ctx["fragments"][0]["document_id"]])), (1, 1)
        if role == "study":
            seen.append([x["问题"] for x in ctx["本轮研究"]])
        return call(role, ctx)

    replay.call = handoff
    assert worker.step()
    assert seen and any(q.startswith("交接 #") for q in seen[0])
    with store.connection() as c:
        assert c.execute("SELECT count(*) FROM knowledge_snapshots WHERE round_id=?", (info["round"],)).fetchone()[0] == 1
        assert c.execute("SELECT count(*) FROM investigation_wakes WHERE snapshot_id IS NOT NULL").fetchone()[0] == 1
        assert c.execute("SELECT count(*) FROM runs WHERE role='specialist'").fetchone()[0] == 2


def test_source_renditions_are_frozen_when_the_handoff_is_written(tmp_path, clock):
    store, _, _, worker, r, owner, sender = published_line(tmp_path)
    with store.connection(True) as c:
        doc = json.loads(r["input_docs"])[0]
        wakes.post(c, r, sender["id"], owner["line_key"], "核对问题", "重读原文", [doc])
        url = db.require(c, "documents", doc)["url"]
        asset = materials.save_asset(c, doc, url, "html", b"later-edition", "text/html",
                                     materials.Extract("html", [("later", "之后追加的内容", url)]))
        new_id = c.execute("SELECT id FROM fragments WHERE asset_id=?", (asset,)).fetchone()[0]
    assert worker.step()
    assert worker.step()
    with store.connection() as c:
        prompts = [json.loads(x[0]) for x in c.execute(
            """SELECT i.payload FROM run_inputs i JOIN runs r ON r.id=i.run_id JOIN jobs j ON j.id=r.job_id
               WHERE j.kind IN ('investigation_wake','handoff_publish')""")]
        assert len(prompts) >= 2
        assert all(new_id not in p["archive_fragment_ids"] for p in prompts)
        assert not any(f["id"] == new_id for f in wakes.publication_fragments(c, r, [doc]))


def test_late_wake_is_forwarded_to_newer_window_and_existing_idle_lane(tmp_path, clock):
    store, runner, _, worker, r, owner, sender = published_line(tmp_path)
    # The latest round no longer assigns the specialist. It still has a native session
    # and a registered line from the previous window.
    runner.roles = ["news"]
    doc = json.loads(r["input_docs"])[0]
    with store.connection(True) as c:
        active, _ = workflow.request_round(c, r["event_id"], "本轮跟进", start=r["window_end"], end=clock.later(60))
        c.execute("UPDATE tracking_rounds SET input_docs=? WHERE id=?", (db.dump([doc]), active))
    for _ in range(4):
        worker.step()
        with store.connection() as c:
            if workflow.latest(c, r["event_id"])["round_id"] == active:
                break
    with store.connection(True) as c:
        latest = workflow.latest(c, r["event_id"])
        assert latest["round_id"] == active
        # A third round already planned the lane again, advancing its mutable last_task.
        third, _ = workflow.request_round(c, r["event_id"], "下一窗口", start=clock.later(61), end=clock.later(120))
        research.save_plan(c, db.require(c, "tracking_rounds", third), Plan.model_validate_json(r["plan"]))
        assert db.require(c, "investigation_lines", owner["id"])["last_task"] != owner["last_task"]
        note = wakes.post(c, r, sender["id"], owner["line_key"], "迟到的原文", "请核对旧窗口中的原文", [doc])
    assert worker.step()
    with store.connection() as c:
        forwarded = db.one(c, "SELECT forwarded_to FROM investigation_wake_notes WHERE note_id=?", (note,))["forwarded_to"]
        assert db.require(c, "event_notes", forwarded)["round_id"] == active
        successor = db.one(c, """SELECT w.owner_task FROM investigation_wakes w JOIN investigation_wake_notes n
                              ON n.job_id=w.job_id WHERE n.note_id=?""", (forwarded,))
        assert successor["owner_task"] == owner["last_task"]
        assert workflow.latest(c, r["event_id"]) == latest
    assert worker.step()
    assert worker.step()
    with store.connection() as c:
        snapshot = workflow.latest(c, r["event_id"])
        assert snapshot["round_id"] == active and snapshot["parent_id"] == latest["id"]
        assert c.execute("SELECT count(*) FROM runs r JOIN investigation_wakes w ON w.job_id=r.job_id WHERE w.round_id=?",
                         (r["id"],)).fetchone()[0] == 0
        assert runner.calls["specialist"] == 2


def test_new_index_snapshot_keeps_its_edits_and_forwards_old_window_evidence(tmp_path, clock):
    store, _, _, worker, r, owner, sender = published_line(tmp_path)
    doc = json.loads(r["input_docs"])[0]
    with store.connection(True) as c:
        before = workflow.latest(c, r["event_id"])
        index_id = workflow.change_index(c, r["event_id"], before, [])
        corrected = workflow.latest(c, r["event_id"])
        assert corrected["round_id"] is None
        message = wakes.post(c, r, sender["id"], owner["line_key"], "新索引后的问题", "请核对交接中的原文", [doc])
    for _ in range(3):
        assert worker.step()
    with store.connection() as c:
        forwarded = db.one(c, "SELECT forwarded_to FROM investigation_wake_notes WHERE note_id=?", (message,))
        new_round = db.require(c, "event_notes", forwarded["forwarded_to"])["round_id"]
        latest = workflow.latest(c, r["event_id"])
        assert latest["round_id"] == new_round and new_round != r["id"]
        assert latest["parent_id"] == index_id
        assert db.require(c, "knowledge_snapshots", index_id) == corrected


def test_failed_handoff_can_be_retried_through_the_round_endpoint(tmp_path, clock):
    store, runner, source, worker, r, owner, sender = published_line(tmp_path)
    with store.connection(True) as c:
        doc = json.loads(r["input_docs"])[0]
        wakes.post(c, r, sender["id"], owner["line_key"], "调查问题", "请核对交接中的原文", [doc])
        wake = c.execute("SELECT job_id FROM investigation_wakes").fetchone()[0]
    runner.fail.add("specialist")
    assert worker.step()
    clock.dt += timedelta(seconds=46)
    assert worker.step()
    with store.connection() as c:
        assert db.require(c, "jobs", wake)["status"] == "failed"
        task = db.one(c, "SELECT task_id FROM investigation_wakes")["task_id"]
    api = TestClient(make_app(store, runner, background=False, acquisition=source),
                     base_url="http://127.0.0.1", headers=HEADERS)
    assert api.post(f"/api/events/{r['event_id']}/rounds/{r['id']}/retry?task={task}").json() == {"job": wake}
    runner.fail.clear()
    assert worker.step()
    with store.connection() as c:
        assert db.require(c, "jobs", wake)["status"] == "done"
        assert db.require(c, "research_tasks", task)["state"] == "done"


def test_enqueued_handoff_retired_explicitly_after_goal_change(tmp_path, clock):
    store, runner, _, worker, r, owner, sender = published_line(tmp_path)
    with store.connection(True) as c:
        doc = json.loads(r["input_docs"])[0]
        note = wakes.post(c, r, sender["id"], owner["line_key"], "调查问题", "请核对交接中的原文", [doc])
        workflow.edit_goal(c, r["event_id"], 1, scope="新目标")
    assert worker.step()
    with store.connection() as c:
        reason = db.one(c, "SELECT reason FROM investigation_wake_stops WHERE note_id=?", (note,))
        assert reason and "目标已更新" in reason["reason"]
        assert runner.calls["specialist"] == 1
