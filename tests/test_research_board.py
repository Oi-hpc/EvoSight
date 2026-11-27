from datetime import timedelta

import pytest

from evosight import agents, board, db, research, workflow, world_search
from evosight.contracts import Citation, Finding, Report, ResearchLookup
from evosight.worker import Worker
from test_world_catalog import metric
from evosight.workspace_agent import CancelRun, check_command
from test_workflow import clock, new_event, setup


def test_scheduler_completes_partial_round_without_manual_retry(tmp_path, clock):
    store, _, runner, worker = setup(tmp_path, ["news", "counter"])
    rid = new_event(store)["round"]
    runner.fail.add("counter")
    assert worker.step()
    with store.connection() as c:
        assert db.require(c, "tracking_rounds", rid)["state"] == "partial"
        assert (
            c.execute("SELECT count(*) FROM jobs WHERE kind='research_retry' AND status='queued'").fetchone()[
                0
            ]
            == 1
        )
    runner.fail.clear()
    clock.dt += timedelta(seconds=46)
    for _ in range(4):
        worker.step()
        with store.connection() as c:
            if db.require(c, "tracking_rounds", rid)["state"] == "published":
                break
    with store.connection() as c:
        assert db.require(c, "tracking_rounds", rid)["state"] == "published"
        assert c.execute("SELECT count(*) FROM tracking_rounds").fetchone()[0] == 1
    assert (runner.calls["news"], runner.calls["counter"]) == (1, 2)


def test_cancellation_binds_event_and_timer_to_one_user_clause():
    ctx = {"question": "请取消甲事件的定时任务 #17。接下来看看乙事件。", "workspace": {"current_event": 1}}
    action = CancelRun(action="cancel_scheduled", instruction="撤回预约", event_ref="乙事件", job_id=17)
    with pytest.raises(ValueError, match="事件指代"):
        check_command(ctx, action)
    action.event_ref = "甲事件"
    check_command(ctx, action)
    assert action.instruction == "请取消甲事件的定时任务 #17"


def test_guide_matches_new_version_once_without_adding_it_to_an_event(tmp_path):
    store = db.Store(tmp_path)
    with store.connection(True) as c:
        origin = db.add_event(c, "价格发布", "价格来源", [])
        reader = db.add_event(c, "预算变化", "按月核对东区价格", [])
        route = {
            "question": "东区价格有变化吗",
            "reason": "核对月报",
            "wake": "新月报",
            "lookup": {"path": "indicators/prices/consumer", "qualifiers": {"region": "东区"}},
        }
        board.save_guides(c, {"event_id": reader, "goal_version": 1}, {"guides": [route]})
        assert board.view(c, reader, 1)["guides"][0]["candidates"] == []
        _, wrong = metric(c, origin, 2, region="西区")
        entry, right = metric(c, origin, 3, region="东区")
        for _ in range(2):
            board.notify(c, origin, [wrong, right])
        assert [r["version_id"] for r in board.view(c, reader, 1)["guides"][0]["candidates"]] == [right]
        assert c.execute("SELECT count(*) FROM jobs WHERE kind='guide_check'").fetchone()[0] == 1
        assert workflow.latest(c, reader) is None
        fragment = c.execute(
            "SELECT json_extract(citations,'$[0].fragment_id') FROM claims WHERE id=?", (right,)
        ).fetchone()[0]
        with pytest.raises(workflow.VersionConflict):
            board.set_disposition(c, entry, right + 1, "withdrawn", None, "来源已订正", fragment)
        board.set_disposition(c, entry, right, "withdrawn", None, "来源已订正", fragment)
        assert board.view(c, reader, 1)["guides"][0]["candidates"] == []
        assert world_search.read_records(c, [{"entry_id": entry}])[0]["status"] == "withdrawn"
        assert world_search.read_records(c, [{"entry_id": entry, "version_id": right}])[0]["status"] == "ok"


def test_field_quote_accepts_verbatim_subspan_and_preserves_subject():
    quote = "ET5T close 88.91 on 2026-11-20"
    fragments = [{"id": 1, "text": quote}]
    finding = Finding(
        section="news",
        category="indicators",
        kind="fact",
        subject="ET5T",
        text="当前材料提供的当日收盘价为88.91。",
        attributes={"value": 88.91},
        citations=[Citation(fragment_id=1, quote=quote)],
        field_citations={"attributes.value": Citation(fragment_id=1, quote="close 88.91")},
    )
    report = Report(findings=[finding])
    research.check_report(report, fragments, {}, "news")
    assert list(report.findings[0].field_citations) == ["attributes.value"]
    assert not board.local_reason("fact", finding.subject, finding.text, {})
    assert board.local_reason("fact", "价格核查", "本轮检索仍未取得公告原文。", {})
    for sub in [Citation(fragment_id=1, quote="close 100"), Citation(fragment_id=2, quote="88.91")]:
        finding.field_citations = {"value": sub}
        with pytest.raises(ValueError, match="同一片段"):
            research.check_report(Report(findings=[finding]), fragments, {}, "news")
    finding.field_citations = {}
    finding.subject = "GL8"
    with pytest.raises(ValueError, match="对象或型号"):
        research.check_report(Report(findings=[finding]), fragments, {}, "news")


def test_stale_lease_is_fenced_and_partial_retry_is_idempotent(tmp_path):
    store = db.Store(tmp_path)
    with store.connection(True) as c:
        event = db.add_event(c, "调查", "核查来源", [])
        rid, job = workflow.request_round(c, event, "初始化", key="test")
        task = c.execute(
            """INSERT INTO research_tasks(round_id,role,question,target,time_scope,done_when,priority,state)
                            VALUES(?,'news','问题','材料','本轮','核查出原文',1,'failed')""",
            (rid,),
        ).lastrowid
        r = db.require(c, "tracking_rounds", rid)
        failed = [db.require(c, "research_tasks", task)]
        research.queue_failed_tasks(c, r, failed, job)
        research.queue_failed_tasks(c, r, failed, job)
        assert c.execute("SELECT attempts FROM role_retries WHERE task_id=?", (task,)).fetchone()[0] == 1
        assert c.execute("SELECT count(*) FROM jobs WHERE kind='research_retry'").fetchone()[0] == 1
        c.execute("UPDATE jobs SET owner='new',status='running',lease=? WHERE id=?", (db.later(120), job))
    token = db.job_lease.set((store.path, job, "old"))
    try:
        with pytest.raises(ValueError, match="租约"), store.connection(True) as c:
            c.execute("UPDATE jobs SET info='old' WHERE id=?", (job,))
    finally:
        db.job_lease.reset(token)


@pytest.mark.parametrize("role", ["forecast", "news"])
def test_lost_native_session_rehydrates_from_business_checkpoint(tmp_path, monkeypatch, role):
    monkeypatch.delenv("EVOSIGHT_RESEARCH_BACKEND", raising=False)
    runner = agents.Runner("test")
    attempts = []

    def invoke(role, context, model, effort, directory, thread_id, archive=()):
        attempts.append((model, thread_id, context["调查检查点"]))
        if thread_id:
            raise agents.MissingSession("session not found")
        return "answer", (1, 2), "new", model

    monkeypatch.setattr(runner, "_invoke", invoke)
    assert runner.turn(role, {"调查检查点": "原题"}, tmp_path, "old")[2] == "new"
    assert [item[1:] for item in attempts] == [("old", "原题"), (None, "原题")]


def test_scout_reads_named_document_and_delivers_handoff_to_its_recipient(tmp_path):
    class Reader:
        def profile(self, role):
            return "fixture", "low"

        def call(self, role, ctx):
            assert ctx["待处理交接"][0]["documents"] == [doc]
            return agents.SourceScout(publisher_urls=[], queries=[]), (1, 1)

    store = db.Store(tmp_path)
    worker = Worker(store, Reader())
    with store.connection(True) as c:
        event = db.add_event(c, "本地价格", "读本事件材料", [])
        other = db.add_event(c, "其他事件", "资料隔离", [])
        _, vr = metric(c, event, 2)
        _, other_vr = metric(c, other, 3)
        doc, foreign = [
            c.execute("SELECT json_extract(refs,'$[0]') FROM claims WHERE id=?", (v,)).fetchone()[0]
            for v in (vr, other_vr)
        ]
        rid, jid = workflow.request_round(c, event, "初始化", key="test")
        tid = c.execute(
            """INSERT INTO research_tasks(round_id,role,question,target,time_scope,done_when,priority,state)
                         VALUES(?,'news','月报是否发布','月报','本轮','找到原文',1,'running')""",
            (rid,),
        ).lastrowid
        r, task, job = (
            db.require(c, "tracking_rounds", rid),
            db.require(c, "research_tasks", tid),
            db.require(c, "jobs", jid),
        )
    ids = {"query": "核对原文", "document_ids": [foreign, doc]}
    assert research.role_lookup(worker, r, task, ResearchLookup(tool="read", **ids))["documents"] == [doc]
    req = ResearchLookup(tool="handoff", recipient="主 Agent", **ids)
    a = research.role_lookup(worker, r, task, req)
    assert research.role_lookup(worker, r, task, req)["message_id"] == a["message_id"]
    with store.connection() as c:
        assert not board.inbox(c, event, "另一条调查线")
        assert len(board.inbox(c, event, "主 Agent")) == 1
    worker.call(job, "source_scout", {})
    with store.connection() as c:
        assert not board.inbox(c, event, "主 Agent")
        assert c.execute("SELECT received_run FROM handoff_inbox").fetchone()[0] is not None
