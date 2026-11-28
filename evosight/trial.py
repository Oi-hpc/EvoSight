"""Run the three documented source trials through the normal event and worker entry points."""
import json
from pathlib import Path

from .agents import Runner
from .db import Store, add_event, enqueue, one, require, rows, utc
from .worker import Worker
from . import workflow
from .contracts import Plan
from .research import save_plan

CASES = {
    "software": ("Vue 核心版本发布", "追踪 Vue 3.6 正式版发布进展。区分已发布版本、测试版本与未来可核验的里程碑。"),
    "public": ("国家统计局月度经济数据发布", "追踪国家统计局工业增加值和居民消费价格月度发布，分别核实统计所属月份和正式发布时点。"),
    "industry": ("苹果公司产品发布", "关注 Apple Newsroom 对新产品的正式披露，核对宣布日期、上市地区与实际供货的区别。"),
}
CHECKS = {
    "software": {
        "primary": "Vue 项目的官方 Releases",
        "timing": "分别核对正式版、候选版以及公告的更新时间",
        "major": "官方发布独立的新版本会唤醒 Vue 事件，同一发行公告的转载只算一次",
    },
    "public": {
        "primary": "国家统计局原始发布",
        "timing": "分别记录指标所属月、公告日期和能证实的精确时刻",
        "major": "主管单位正式发布新一期指标会唤醒统计发布事件，重述旧月份数据只作背景",
    },
    "industry": {
        "primary": "Apple Newsroom 的公司披露",
        "timing": "分开记录宣布、预购、地区上市和实际供货",
        "major": "新增的正式发布或可得性变更唤醒苹果事件，同一公告的新闻转引共享原始来源",
    },
}


def run_trials(directory, cases):
    store = Store(directory)
    marker = store.directory / ".workflow-trial"
    with store.connection() as c:
        if not marker.exists() and one(c, "SELECT id FROM events LIMIT 1"):
            raise ValueError("请为真实试用选择独立的数据目录")
    marker.touch()
    worker = Worker(store, Runner("test"))
    records = []
    for case in cases:
        title, scope = CASES[case]
        with store.connection(True) as c:
            existing = one(c, "SELECT * FROM events WHERE title=? AND status!='deleted'", (title,))
            if not existing:
                event = add_event(c, title, scope, [])
                workflow.begin_event(c, event)
            else:
                event = existing["id"]
                recent = one(
                    c, "SELECT * FROM tracking_rounds WHERE event_id=? ORDER BY id DESC LIMIT 1", (event,)
                )
                if recent and recent["state"] == "partial":
                    pending = rows(
                        c, "SELECT * FROM research_tasks WHERE round_id=? AND state!='done'", (recent["id"],)
                    )
                    if existing["status"] != "active":
                        # Pausing the trial bumped the goal version. Re-evaluate only missing roles against the current goal.
                        current = workflow.current_goal(c, event)
                        if (current["title"], current["scope"]) != (title, scope):
                            raise ValueError("试用任务目标已被人工修改，请使用另一个案例目录")
                        result = workflow.edit_goal(c, event, current["version"], status="active")
                        plan = Plan.model_validate_json(recent["plan"])
                        wanted = {(p["role"], p["question"]) for p in pending}
                        plan.tasks = [t for t in plan.tasks if (t.role, t.question) in wanted]
                        plan.rationale = "针对首轮未通过校验的具体问题重新取证"
                        target = require(c, "tracking_rounds", result["round"])
                        save_plan(c, target, plan)
                        for miss in pending:
                            c.execute(
                                "UPDATE research_tasks SET detail=? WHERE round_id=? AND role=? AND question=?",
                                (miss["detail"], target["id"], miss["role"], miss["question"]),
                            )
                    else:
                        c.execute(
                            "UPDATE research_tasks SET state='queued' WHERE round_id=? AND state!='done'",
                            (recent["id"],),
                        )
                        jid = enqueue(c, "research_retry", event=event, args={"round_id": recent["id"]})
                        c.execute(
                            "UPDATE tracking_rounds SET state='queued',job_id=? WHERE id=?",
                            (jid, recent["id"]),
                        )
                elif existing["status"] != "active" and not workflow.latest(c, event):
                    workflow.edit_goal(c, event, workflow.current_goal(c, event)["version"], status="active")
        print(f"{utc()} · {title} · 开始验证", flush=True)
        for _ in range(18):
            if not worker.step():
                break
        with store.connection(True) as c:
            data = workflow.knowledge(c, event)
            rounds = workflow.list_rounds(c, event)
            evidence = [
                {"subject": v["subject"], "text": v["text"], "citations": v["citations"]}
                for section in ("background", "people", "opinion", "news")
                for v in data["sections"][section]
            ]
            record = {
                "case": case,
                "event": title,
                "at": utc(),
                "snapshot": data["snapshot"],
                "sources": data["sources"],
                "rounds": rounds,
                "replay_expectations": CHECKS[case],
                "originals": rows(
                    c,
                    """SELECT d.id,d.url,d.title,d.published_at,d.source_updated_at,d.observed_at,
                                a.kind,a.digest,a.extractor FROM event_docs ed JOIN documents d ON d.id=ed.document_id
                                JOIN evidence_assets a ON a.document_id=d.id WHERE ed.event_id=? AND a.digest IS NOT NULL ORDER BY d.id,a.id""",
                    (event,),
                ),
                "examples": evidence[:5],
                "claims": len(evidence),
                "gaps": data["gaps"],
                "predictions": rows(
                    c, "SELECT id,question,status FROM predictions WHERE event_id=?", (event,)
                ),
                "jobs": rows(c, "SELECT kind,status,info FROM jobs WHERE event_id=? ORDER BY id", (event,)),
                "runs": rows(
                    c,
                    "SELECT r.role,r.model,r.effort,r.status,r.elapsed FROM runs r JOIN jobs j ON j.id=r.job_id WHERE j.event_id=? ORDER BY r.id",
                    (event,),
                ),
            }
            records.append(record)
            record["remaining_roles"] = (
                [t["role_name"] for t in rounds[0]["tasks"] if t["state"] != "done"] if rounds else []
            )
            if data["snapshot"] and rounds and rounds[0]["state"] == "published":
                workflow.edit_goal(c, event, workflow.current_goal(c, event)["version"], status="paused")
        (Path(directory) / f"{case}.json").write_text(
            json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(
            f"{utc()} · {title} · 版本 {record['snapshot']} · {len(evidence)} 条有据结论 · {len(record['runs'])} 次模型调用",
            flush=True,
        )
    return all(
        r["snapshot"]
        and r["claims"]
        and r["rounds"]
        and r["rounds"][0]["state"] == "published"
        and not r["remaining_roles"]
        and any(s["success_at"] for s in r["sources"])
        and any(j["kind"] == "forecast" and j["status"] == "done" for j in r["jobs"])
        for r in records
    )
