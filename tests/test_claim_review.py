"""Drive review, original-owner correction and retry through the ordinary worker."""
import pytest

from evosight import claim_review, delivery, workflow
from evosight.contracts import Citation, ClaimReview, DeliveryAnswer, Finding, Plan
from test_workflow import BASE, clock, new_event, setup


def scripted_research(tmp_path, verdicts):
    store, _, runner, worker = setup(tmp_path, ["news"])
    event = new_event(store)["id"]
    normal = runner.call
    seen = []
    corrected = "公告仅确认公布验收方案。"
    bad = "交通局已确认实际开放。"
    safe = "交通局和运营企业分别公开了检查程序与各自职责。"

    def call(role, ctx):
        if role == "claim_review":
            seen.append(ctx)
            decision = verdicts[min(len(seen) - 1, len(verdicts) - 1)]
            if decision == "error":
                raise RuntimeError("复核服务超时")
            answer = ctx["核查项"][0]
            assert ctx["独立复核"] and answer["check_id"] == 1
            assert any(BASE in f["text"] for f in ctx["交付可引用原文"])
            return ClaimReview(
                items=[
                    dict(
                        check_id=1,
                        decision=decision,
                        reason="该公告只确认公布验收方案，请核实实际开放状态。",
                        citations=[{"fragment_id": answer["citations"][0]["fragment_id"], "quote": BASE}],
                    )
                ]
            ), (11, 9)
        value, usage = normal(role, ctx)
        if role == "plan":
            value.checklist = ["实际开放状态", "是否公开检查程序"]
            value.tasks[0].review_checks = [1]
        if role == "news":
            val = value.findings[0]
            val.text = corrected if "上次调查结果" in ctx else bad
            val.subject = "实际开放状态"
            fragment = next(f for f in ctx["fragments"] if safe in f["text"])
            value.findings.append(
                Finding(
                    section="news",
                    kind="fact",
                    subject="检查",
                    text=safe,
                    citations=[Citation(fragment_id=fragment["id"], quote=safe)],
                )
            )
        if role == "study":
            findings = [f for report in ctx["本轮研究"] for f in report["findings"]]
            reopened = next((f for f in findings if f["text"] == corrected), None)
            chosen = reopened or next(f for f in findings if f["text"] == bad)
            safe_f = next(f for f in findings if f["text"] == safe)
            value.judgment = chosen["text"]
            value.delivery = [
                DeliveryAnswer(check_id=i, state="confirmed", answer=f["text"], citations=f["citations"])
                for i, f in enumerate([chosen, safe_f], 1)
            ]
        return value, usage

    runner.call = call
    return store, worker, event, seen, bad, safe


def test_return_resumes_owner_and_blocks_old_conclusion_from_every_public_version(tmp_path, clock):
    store, worker, event, seen, bad, safe = scripted_research(tmp_path, ["return", "supported"])
    assert worker.step()
    with store.connection() as c:
        first = workflow.latest(c, event)
        assert [x["state"] for x in delivery.view(c, first["id"])] == ["unknown", "confirmed"]
        assert [k["text"] for k in workflow.snapshot_claims(c, first["id"])] == [safe]
        assert (
            first["summary"] != bad
            and c.execute("SELECT COUNT(*) FROM research_review_finding_returns").fetchone()[0] == 1
        )
        assert c.execute("SELECT COUNT(*) FROM investigation_wakes").fetchone()[0] == 1

    for _ in range(5):
        if len(seen) == 2:
            break
        assert worker.step()
    with store.connection() as c:
        second = workflow.latest(c, event)
        assert second["id"] != first["id"] and len(seen) == 2
        assert len({r["round_id"] for r in (first, second)}) == 1
        assert delivery.view(c, second["id"])[0]["state"] == "confirmed"
        assert delivery.view(c, first["id"])[0]["state"] == "unknown"
        assert c.execute("SELECT COUNT(*) FROM claims WHERE text=?", (bad,)).fetchone()[0] == 0
        assert c.execute("SELECT COUNT(*) FROM nodes WHERE body=?", (bad,)).fetchone()[0] == 0
        sessions = c.execute(
            """SELECT s.lane FROM runs r JOIN run_sessions s ON s.run_id=r.id
            WHERE r.role IN ('claim_review','news') ORDER BY r.id"""
        ).fetchall()
        assert [r[0] for r in sessions] == [sessions[0][0], None, sessions[0][0], None]


@pytest.mark.parametrize("retry_decision", ["supported", "error"])
def test_review_transport_retries_published_round_once(tmp_path, clock, retry_decision):
    store, worker, event, seen, bad, safe = scripted_research(tmp_path, ["error", retry_decision])
    assert worker.step()
    with store.connection() as c:
        before = workflow.latest(c, event)
        assert delivery.view(c, before["id"])[0]["state"] == "unknown"
        assert not c.execute("SELECT 1 FROM investigation_wakes").fetchone()
        assert not c.execute("SELECT 1 FROM research_review_finding_returns").fetchone()
    for _ in range(6):
        if len(seen) == 2:
            break
        assert worker.step()
    with store.connection() as c:
        after = workflow.latest(c, event)
        assert len(seen) == 2
        assert delivery.view(c, after["id"])[0]["state"] == (
            "confirmed" if retry_decision == "supported" else "unknown"
        )
        jobs = c.execute("SELECT status FROM jobs WHERE slot LIKE 'claim-review/%'").fetchall()
        assert [j[0] for j in jobs] == ["done"]


def test_unfinished_assignment_still_has_a_reviewer_and_earlier_check_numbers_do_not_leak():
    plan = {"tasks": [{"role": "news", "question": "本轮问题", "line": "公告", "review_checks": [1]}]}
    echo = {"task_id": 3, "角色": "specialist", "问题": "交接", "调查线": "先前的监管问题", "findings": []}
    owner = {"task_id": 4, "角色": "news", "问题": "本轮问题", "findings": []}
    checked = [{"position": 1, "state": "confirmed", "answer": "待独立核实的断言", "question": "问题", "citations": []}]
    assert [r["task_id"] for r in claim_review.targets(plan, [echo, owner], checked)[0]["owners"]] == [4]


def test_duplicate_checklist_rows_keep_the_review_on_the_original_question():
    assignment = dict(
        role="news",
        question="已公布还是已实施",
        target="公告",
        time_scope="window",
        done_when="查明适用阶段",
        priority=4,
        review_checks=[3],
    )
    plan = Plan(rationale="核对原文", tasks=[assignment], checklist=["日期", "日期", "实施阶段"])
    assert plan.checklist == ["日期", "实施阶段"] and plan.tasks[0].review_checks == [2]
    assert Plan.model_validate_json(plan.model_dump_json()).tasks[0].review_checks == [2]
    with pytest.raises(ValueError, match="不存在的交付清单序号"):
        Plan(rationale="核对原文", tasks=[assignment], checklist=["实施阶段"])


def test_declared_unknown_cannot_reenter_public_claims_through_the_investigator(tmp_path, clock):
    store, worker, event, seen, bad, safe = scripted_research(tmp_path, [])
    scripted = worker.runner.call

    def unknown(role, ctx):
        result, usage = scripted(role, ctx)
        if role == "news":
            # Both assertions reuse the same evidence. Sharing a confirmed
            # answer's source does not establish the other assertion.
            result.findings[0].citations = result.findings[1].citations
            result.findings[0].occurred_at = result.findings[0].time_quote = ""
        if role == "study":
            result.delivery[0] = DeliveryAnswer(check_id=1, state="unknown", answer="尚缺实际开放的正式公告。")
            result.closure_reason = bad
        return result, usage

    worker.runner.call = unknown
    assert worker.step()
    with store.connection() as c:
        published = workflow.latest(c, event)
        assert [a["state"] for a in delivery.view(c, published["id"])] == ["unknown", "confirmed"]
        assert [f["text"] for f in workflow.snapshot_claims(c, published["id"])] == [safe]
        assert not seen and not c.execute("SELECT 1 FROM investigation_wakes").fetchone()
        assert not c.execute("SELECT 1 FROM end_proposals").fetchone()
        assert c.execute("SELECT count(*) FROM research_review_finding_returns").fetchone()[0] == 1
        assert published["summary"] != bad and not c.execute("SELECT 1 FROM nodes WHERE body=?", (bad,)).fetchone()
