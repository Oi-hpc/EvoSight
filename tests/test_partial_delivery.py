"""Deadline delivery while all inference slots are in use, and claim-level forecast holds."""
from concurrent.futures import ThreadPoolExecutor
import threading
import time

import pytest

from evosight import db, research, resources, scenarios, workflow
from evosight.contracts import Citation, DeliveryAnswer, Report, ResearchLookup
from test_workflow import BASE, clock, new_event, setup


def test_waiting_round_delivers_immutable_excerpt_before_slow_role_returns(tmp_path, clock, monkeypatch):
    store, _, runner, worker = setup(tmp_path, ["news", "counter"])
    event = new_event(store)
    monkeypatch.setattr(resources, "host", lambda: (2, 1840, 1300, False))
    monkeypatch.setattr(research, "INTERIM_REPORT_AFTER_SECONDS", 1)
    started, release = threading.Event(), threading.Event()
    original = runner.call

    def gated(role, ctx):
        if role == "counter":
            started.set()
            assert release.wait(12)
        return original(role, ctx)

    monkeypatch.setattr(runner, "call", gated)
    with ThreadPoolExecutor(max_workers=1) as pool:
        execution = pool.submit(worker.step)
        try:
            assert started.wait(8)
            until = time.monotonic() + 5
            preview = None
            while time.monotonic() < until and not preview:
                with store.connection() as c:
                    preview = workflow.list_rounds(c, event["id"])[0]["evidence_preview"]
                if not preview:
                    time.sleep(.025)
            assert preview and preview["excerpts"][0]["quote"] == BASE
            assert len(preview["pending"]) == 1 and "反证" in preview["pending"][0]
            assert not execution.done() and runner.calls["study"] == 0
            with store.connection() as c:
                assert not workflow.latest(c, event["id"])
                assert not db.one(c, "SELECT id FROM jobs WHERE kind='forecast'")
                ref = preview["excerpts"][0]
                assert ref["task_id"] == db.one(c, "SELECT id FROM research_tasks WHERE round_id=? AND role='news'",
                                                (event["round"],))["id"]
                assert ref["line_key"].startswith("news/")
                assert ref["in_event"] and ref["quote"] in db.require(c, "fragments", ref["fragment_id"])["text"]
        finally:
            release.set()
        assert execution.result(timeout=8)
    reopened = db.Store(tmp_path)
    with reopened.connection() as c:
        result = workflow.list_rounds(c, event["id"])[0]
        assert result["state"] == "published" and result["evidence_preview"] == preview
        assert c.execute("SELECT count(*) FROM knowledge_snapshots").fetchone()[0] == 1
    assert runner.calls["news"] == runner.calls["counter"] == runner.calls["study"] == 1


def test_slow_inline_handoff_uses_original_round_deadline_on_single_slot(tmp_path, clock, monkeypatch):
    store, _, runner, worker = setup(tmp_path, ["specialist", "news"])
    event = new_event(store)
    with store.connection(True) as c:
        c.execute("UPDATE settings SET value='1' WHERE key='max_agent_concurrency'")
    monkeypatch.setattr(research, "INTERIM_REPORT_AFTER_SECONDS", 2)
    start, release = threading.Event(), threading.Event()
    original = runner.call

    def handoff(role, ctx):
        if role == "news" and not ctx.get("工具回执"):
            line = next(l for l in ctx["事件黑板"]["lines"] if l["role"] == "specialist")
            return Report(lookup=ResearchLookup(tool="handoff", query="补查原页",
                          recipient=line["line_key"], document_ids=[ctx["fragments"][0]["document_id"]])), (1, 1)
        if role == "specialist" and ctx.get("待处理交接"):
            start.set()
            assert release.wait(12)
        return original(role, ctx)

    monkeypatch.setattr(runner, "call", handoff)
    with ThreadPoolExecutor(max_workers=1) as pool:
        execution = pool.submit(worker.step)
        try:
            assert start.wait(8)
            until = time.monotonic() + 5
            preview = None
            while time.monotonic() < until and not preview:
                with store.connection() as c:
                    preview = workflow.list_rounds(c, event["id"])[0]["evidence_preview"]
                if not preview:
                    time.sleep(.025)
            assert preview and any("交接" in q for q in preview["pending"])
            assert preview["excerpts"] and not execution.done() and runner.calls["study"] == 0
        finally:
            release.set()
        assert execution.result(timeout=8)
    with store.connection() as c:
        assert db.require(c, "tracking_rounds", event["round"])["state"] == "published"


def test_expired_deadline_defers_queued_handoff_and_labels_the_snapshot(tmp_path, clock, monkeypatch):
    store, _, runner, worker = setup(tmp_path, ["specialist", "news"])
    event = new_event(store)
    with store.connection(True) as c:
        c.execute("UPDATE settings SET value='1' WHERE key='max_agent_concurrency'")
    monkeypatch.setattr(research, "INTERIM_REPORT_AFTER_SECONDS", 0)
    original = runner.call

    def send(role, ctx):
        if role == "news" and not ctx.get("工具回执"):
            line = next(l for l in ctx["事件黑板"]["lines"] if l["role"] == "specialist")
            return Report(lookup=ResearchLookup(tool="handoff", query="核对最新原文",
                          recipient=line["line_key"], document_ids=[ctx["fragments"][0]["document_id"]])), (1, 1)
        return original(role, ctx)

    monkeypatch.setattr(runner, "call", send)
    assert worker.step()
    with store.connection() as c:
        partial = workflow.latest(c, event["id"])
        assert db.require(c, "tracking_rounds", event["round"])["state"] == "partial"
        assert "交接补查" in partial["gaps"]
        assert db.one(c, "SELECT status FROM jobs WHERE kind='investigation_wake'")["status"] == "queued"
    for _ in range(4):
        worker.step()
        with store.connection() as c:
            if db.require(c, "tracking_rounds", event["round"])["state"] == "published":
                break
    with store.connection() as c:
        assert db.require(c, "tracking_rounds", event["round"])["state"] == "published"
        assert db.require(c, "knowledge_snapshots", partial["id"]) == partial
        assert workflow.latest(c, event["id"])["id"] != partial["id"]


@pytest.mark.parametrize("required,allowed", [([1], True), ([], True), ([2], False), (None, False), ([3], False)])
def test_unresolved_check_holds_only_dependent_forecast(tmp_path, clock, monkeypatch, required, allowed):
    store, _, runner, worker = setup(tmp_path, ["news"])
    event = new_event(store)
    original = runner.call

    def proposal(role, ctx):
        result, usage = original(role, ctx)
        if role == "plan":
            result.checklist = ["验收计划是否已经公布", "优惠适用条件"]
        elif role == "study":
            ref = next(f for f in ctx["交付可引用原文"] if BASE in f["text"])
            result.delivery = [
                DeliveryAnswer(check_id=1, state="confirmed", answer="已公布验收方案。",
                               citations=[Citation(fragment_id=ref["id"], quote=BASE)]),
                DeliveryAnswer(check_id=2, state="unknown", answer="尚缺优惠细则。"),
            ]
        elif role == "forecast":
            assert [row["state"] for row in ctx["预测核查清单"]] == ["confirmed", "unknown"]
            result.required_checks = required
        return result, usage

    monkeypatch.setattr(runner, "call", proposal)
    assert worker.step() and worker.step()
    with store.connection() as c:
        assert bool(db.one(c, "SELECT id FROM predictions WHERE event_id=?", (event["id"],))) == allowed
        assert bool(db.one(c, "SELECT reason FROM forecast_notes WHERE event_id=?", (event["id"],))) != allowed


def test_registered_question_and_required_evidence_cannot_be_bypassed_with_an_empty_list():
    from types import SimpleNamespace

    ctx = {"固定题": {"question": "何时交割？"}, "预测核查清单": [
        {"id": 2, "question": "何时交割", "state": "unknown", "documents": []}
    ]}
    forecast = SimpleNamespace(action="create", question="", required_checks=[], evidence_ids=[7])
    assert "仍待补证" in scenarios.forecast_check_hold(forecast, ctx)
    ctx["预测核查清单"][0].update(state="confirmed", documents=[8])
    assert "原始材料" in scenarios.forecast_check_hold(forecast, ctx)
