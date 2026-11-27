"""Admission and recovery across independent agents sharing one SQLite store."""
from concurrent.futures import ThreadPoolExecutor
import threading
import time

import pytest
from fastapi.testclient import TestClient

from evosight import agents, db, resources
from evosight.api import make_app
from evosight.worker import Worker
from test_workflow import HEADERS, clock, new_event, setup


@pytest.mark.parametrize("limit,expected", [(1, 1), (2, 2), (4, 2)])
def test_roles_overlap_within_machine_limit(tmp_path, clock, monkeypatch, limit, expected):
    store, _, runner, worker = setup(tmp_path, ["news", "dependencies", "counter", "organizations"])
    monkeypatch.setattr(resources, "host", lambda: (2, 1840, 1300, False))
    event = new_event(store)
    with store.connection(True) as c:
        c.execute("UPDATE settings SET value=? WHERE key='max_agent_concurrency'", (str(limit),))
    actual = runner.call
    lock = threading.Lock()
    active = peak = ended = 0

    def measured(role, ctx):
        nonlocal active, peak, ended
        if role == "study":
            assert ended == 4 and active == 0
        if role not in runner.roles:
            return actual(role, ctx)
        with lock:
            active += 1
            peak = max(peak, active)
        try:
            time.sleep(0.1)
            return actual(role, ctx)
        finally:
            with lock:
                active -= 1
                ended += 1

    monkeypatch.setattr(runner, "call", measured)
    assert worker.step()
    with store.connection() as c:
        assert db.require(c, "tracking_rounds", event["round"])["state"] == "published"
        assert (
            c.execute("SELECT count(*) FROM knowledge_snapshots WHERE event_id=?", (event["id"],)).fetchone()[
                0
            ]
            == 1
        )
        assert db.usage(c)["daily_used"] == 6
    assert peak == expected


def test_model_admission_shared_across_workers_and_idempotent(tmp_path, monkeypatch):
    store, source, runner, first = setup(tmp_path, ["news"])
    second = Worker(store, runner, acquisition=source)
    with store.connection(True) as c:
        jobs = [
            db.require(
                c,
                "jobs",
                db.enqueue(
                    c, "discussion", event=db.add_event(c, f"并发样本 {i}", "费用计数和全局准入", []), args={"turn_id": i}
                ),
            )
            for i in range(3)
        ]
        c.execute("UPDATE jobs SET status='running'")
        c.execute("UPDATE settings SET value='5' WHERE key='daily_calls'")
    monkeypatch.setattr(resources, "host", lambda: (2, 1840, 1300, False))
    release = threading.Event()
    two_started = threading.Event()
    lock = threading.Lock()
    active = peak = 0

    def call(role, ctx):
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(active, peak)
            if active == 2:
                two_started.set()
        try:
            assert release.wait(5)
            return agents.DiscussionAnswer(kind="insufficient", answer="尚无可核实资料。", citations=[]), (2, 3)
        finally:
            with lock:
                active -= 1

    monkeypatch.setattr(runner, "call", call)
    ctx = {"sources": {}}
    with ThreadPoolExecutor(max_workers=4) as pool:
        started = [pool.submit(w.call_recorded, j, "discuss_dsh", ctx) for w, j in zip((first, second), jobs)]
        try:
            assert two_started.wait(5)
            third = pool.submit(second.call_recorded, jobs[2], "discuss_dsh", ctx)
            duplicate = pool.submit(first.call_recorded, jobs[0], "discuss_dsh", ctx)
            with store.connection(True) as c:
                assert resources.capacity(c)["active"] == 2 and db.usage(c)["daily_used"] == 2
        finally:
            release.set()
        receipts = [v.result(timeout=5)[2] for v in [*started, third, duplicate]]
    assert len(set(receipts)) == 3 and receipts[0] == receipts[-1] and peak == 2
    with pytest.raises(ValueError, match="预留"):
        first.call_recorded(jobs[0], "discuss_dsh", {"sources": {}, "extra": 1})
    with store.connection() as c:
        assert db.usage(c)["daily_used"] == 3


def test_pressure_during_round_resumes_without_losing_budget(tmp_path, clock, monkeypatch):
    store, source, runner, worker = setup(tmp_path, ["news", "counter"])
    event = new_event(store)
    free = [1300]
    monkeypatch.setattr(resources, "host", lambda: (2, 1840, free[0], False))

    def trigger(role):
        if role == "plan":
            free[0] = 350

    runner.on_role = trigger
    assert worker.step()
    with store.connection(True) as c:
        job = db.one(c, "SELECT * FROM jobs WHERE status='queued' AND info LIKE '%内存%'")
        assert job and job["attempts"] == 0
        assert db.usage(c)["daily_used"] == 1
        assert resources.capacity(c)["effective"] == 0
        c.execute("UPDATE jobs SET retry_at=NULL WHERE id=?", (job["id"],))
    assert worker.step() is False
    free[0] = 1300
    assert worker.step()
    with store.connection() as c:
        assert db.require(c, "tracking_rounds", event["round"])["state"] == "published"
        assert db.usage(c)["daily_used"] == 4 and runner.calls["plan"] == 1
    with TestClient(
        make_app(store, runner, background=False, acquisition=source),
        base_url="http://127.0.0.1",
        headers=HEADERS,
    ) as api:
        data = api.get("/api/operations").json()
        cfg = data["limits"]
        assert data["concurrency"]["effective"] == cfg["max_agent_concurrency"] == 2
        assert api.post("/api/settings", json={**cfg, "max_agent_concurrency": 5}).status_code == 422
        assert api.post("/api/settings", json={**cfg, "max_agent_concurrency": 4}).is_success
        assert api.get("/api/operations").json()["concurrency"]["effective"] == 2
        for available, busy, expected in ((800, False, 1), (1250, True, 1), (420, False, 0)):
            monkeypatch.setattr(resources, "host", lambda: (2, 1840, available, busy))
            assert api.get("/api/operations").json()["concurrency"]["effective"] == expected


def test_discussion_can_run_beside_writer(tmp_path, clock, monkeypatch):
    store, source, runner, worker = setup(tmp_path, ["news"])
    event = new_event(store)
    with store.connection(True) as c:
        waiting_discussion = db.enqueue(c, "discussion", event=event["id"], args={"turn_id": 99})
        c.execute("UPDATE jobs SET retry_at='2099-01-01' WHERE id=?", (waiting_discussion,))
    monkeypatch.setattr(resources, "host", lambda: (2, 1840, 1300, False))
    writer_started, release = threading.Event(), threading.Event()

    def hold(job):
        if job["kind"] != "discussion":
            writer_started.set()
            assert release.wait(5)
        return job["kind"]

    monkeypatch.setattr(worker, "process", hold)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(worker.step)
        try:
            assert writer_started.wait(5)
            with store.connection(True) as c:
                next_writer = db.enqueue(c, "forecast", event=event["id"])
                c.execute("UPDATE jobs SET retry_at=NULL WHERE id=?", (waiting_discussion,))
            assert worker.step()
            with store.connection() as c:
                assert db.require(c, "jobs", waiting_discussion)["status"] == "done"
                assert db.require(c, "jobs", next_writer)["status"] == "queued"
            assert not worker.step()
        finally:
            release.set()
        assert first.result(timeout=5)
