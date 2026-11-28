import json
from io import StringIO
from pathlib import Path
import tomllib

import pytest
import yaml
from fastapi.testclient import TestClient

from evosight import agent_tools, agents, db, forecast, skills, web_search_tool, workflow
from evosight.api import make_app
from evosight.worker import Worker


METHOD = skills.Method(
    title="发行交割核查", when_to_use="核对多类别证券发行交割公告", scope="workspace", roles=["news", "study"],
    steps=["逐条核对公开发行数和私募股数", "对照原文段落复核股数类别和交割日期"],
)


def test_retrospective_draft_review_and_role_scope(tmp_path, monkeypatch):
    store = db.Store(tmp_path)
    with store.connection(True) as c:
        event, other = [db.add_event(c, name, "检查公告", []) for name in ("甲发行", "乙发行")]
        workflow.observe(c, event, "待验证经验", "交割公告两类股数应逐项复核", "其他案例也适用时再发布")
        lesson = c.execute("SELECT max(id) FROM observations").fetchone()[0]
    runner = agents.Runner("test")
    work = Worker(store, runner)
    def draft(role, ctx, model, effort, directory, thread_id, archive=()):
        assert role == "skill_draft" and ctx["复盘经验"] == "交割公告两类股数应逐项复核"
        assert directory is None and thread_id is None
        return skills.Extraction(method=METHOD, reason="不同发行事件都须核对两类股数"), (1, 1), None, model
    monkeypatch.setattr(runner, "_invoke", draft)
    with TestClient(make_app(store, runner, background=False), base_url="http://127.0.0.1",
                    headers={"X-EvoSight": "local"}) as api:
        url = f"/api/events/{event}/lessons/{lesson}/skill"
        queued = api.post(url).json()
        assert queued["status"] == "generating" and api.post(url).json() == queued
        assert api.post(f"/api/events/{other}/lessons/{lesson}/skill").status_code == 422
        with store.connection() as c:
            job = db.require(c, "jobs", queued["job_id"])
        work.process(job)
        pending = api.get(f"/api/events/{event}/skills").json()[0]
        assert pending["status"] == "pending" and pending["method"] == METHOD.model_dump()
        url = f"/api/events/{event}/skills/{pending['id']}/decision"
        body = {"decision": "approve", "expected_revision": pending["revision"]}
        with store.connection() as c:
            assert not skills.applicable(c, event, "news")
        assert api.post(url, json={**body, "expected_revision": pending["revision"] - 1}).status_code == 409
        edited = {**METHOD.model_dump(), "steps": ["先读发行人原始公告", *METHOD.steps]}
        approved = api.post(url, json={**body, "method": edited}).json()
        assert approved["status"] == "approved" and approved["revision"] > pending["revision"]
        assert api.post(url, json={**body, "method": edited}).json() == approved
        assert api.post(url, json={**body, "decision": "reject"}).status_code == 409
    with db.Store(tmp_path).connection(True) as c:
        used = skills.applicable(c, other, "study")
        assert used[0]["steps"] == edited["steps"] and used[0]["version"] == approved["revision"]
        assert not skills.applicable(c, other, "counter")
        c.execute("UPDATE observations SET state='withdrawn' WHERE id=?", (lesson,))
        assert not skills.applicable(c, other, "study")
        assert skills.list_cards(c, event)[0]["source_withdrawn"] is True


def test_settlement_produces_only_pending_skill_and_rejection_is_final(tmp_path):
    store = db.Store(tmp_path)
    with store.connection(True) as c:
        event = db.add_event(c, "公开发行", "审阅交割情况", [])
        c.execute("""INSERT INTO documents(url,digest,title,summary,observed_at)
                    VALUES('https://example.org','one','发行登记','发布公告','2026-01-01T00:00:00Z')""")
        doc = c.execute("SELECT last_insert_rowid()").fetchone()[0]
        c.execute("INSERT INTO event_docs(event_id,document_id) VALUES(?,?)", (event, doc))
        pred = forecast.new(c, event, "本周会交割吗", "2026-02-01T00:00:00Z", "按公告确认", "发行人", 60,
                            "发行申请中", [doc], "mock", at="2026-01-02T00:00:00Z")
        choice = {"at":"2026-02-05T00:00:00Z","proposed_method":METHOD}
        args = (c,pred,"unverifiable","","缺少原文无法裁定",[doc],"应该逐项核对","核验 Agent")
        forecast.settle(*args, **choice)
        pending, = skills.list_cards(c, event)
        assert pending["status"] == "pending" and not skills.applicable(c, event, "news")
        skills.decide(c, event, pending["id"], skills.Decision(decision="reject",expected_revision=1))
        forecast.settle(*args, **choice)
        assert skills.request(c, event, pending["lesson_id"])["status"] == "rejected"
        assert not skills.applicable(c, event, "news")


def test_manual_retrospective_retries_extraction_without_enabling_nonprocedural_lessons(tmp_path, monkeypatch):
    store = db.Store(tmp_path)
    with store.connection(True) as c:
        event = db.add_event(c, "核验报告", "审阅交割", [])
        doc = c.execute("""INSERT INTO documents(url,digest,title,summary,observed_at)
                           VALUES('https://example.org/a','a','公告','报告摘要','2026-01-01T00:00:00Z')"""
                        ).lastrowid
        c.execute("INSERT INTO event_docs(event_id,document_id) VALUES(?,?)", (event, doc))
        pred = forecast.new(c, event, "已交割吗", "2026-02-01T00:00:00Z", "按公告核对", "发行方", 50,
                            "申请中", [doc], "mock", at="2026-01-05T00:00:00Z")
        forecast.settle(c, pred, "unverifiable", "", "尚未取得公告", [], "本次未找到公告", "人工裁定",
                        at="2026-02-10T00:00:00Z", extract_lesson=True)
        queued, = skills.list_cards(c,event)
        assert queued["status"] == "generating"
        c.execute("UPDATE jobs SET status='failed',info='服务中断' WHERE id=?", (queued["job_id"],))
        failed, = skills.list_cards(c,event)
        assert (failed["status"], failed["reason"]) == ("failed", "服务中断")
        retry = skills.request(c, event, failed["lesson_id"])
        assert retry["id"] == failed["id"] and retry["job_id"] != failed["job_id"]
        job = db.require(c, "jobs", retry["job_id"])
    worker = Worker(store, agents.Runner("test"))
    monkeypatch.setattr(worker,"call",lambda *args: (skills.Extraction(reason="只有本轮事实，未发现可迁移的核查步骤"), "mock"))
    worker.process(job)
    with store.connection() as c:
        entry, = skills.list_cards(c,event)
        assert entry["status"] == "unsuitable" and entry["method"] is None
        assert not skills.applicable(c, event, "news")


@pytest.mark.parametrize("backend", ["codex", "dsh"])
def test_both_backend_launchers_retrieve_approved_steps_on_demand(tmp_path, monkeypatch, backend):
    store = db.Store(tmp_path / "store")
    runner = agents.Runner("test")
    worker = Worker(store, runner)
    with store.connection(True) as c:
        event = db.add_event(c, "甲", "复核发行", [])
        workflow.observe(c, event, "待验证经验", "比较不同股数", "证据口径一致")
        lesson = c.execute("SELECT max(id) FROM observations").fetchone()[0]
        skills.from_verdict(c,event,lesson,METHOD)
        candidate, = skills.list_cards(c,event)
        skills.decide(c,event,candidate["id"],skills.Decision(decision="approve",expected_revision=1))
        c.execute("UPDATE settings SET value=? WHERE key='agent_backend'", (db.dump(backend),))
        job = db.require(c, "jobs", db.enqueue(c, "collect", event=event))
    monkeypatch.delenv("EVOSIGHT_RESEARCH_BACKEND",raising=False)
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    desktop = tmp_path / "desktop.yml"
    desktop.touch()
    monkeypatch.setenv("EVOSIGHT_DSH_PATCH",str(desktop))
    monkeypatch.setattr(agents, "executable_codex", lambda: "test-codex")
    monkeypatch.setattr(agent_tools, "scoped_shell", lambda exe: False)
    monkeypatch.setattr(agents, "dsh_command", lambda: (["test-dsh"], {}))

    def capture(cmd, cwd, prompt, env, timeout):
        assert METHOD.title in prompt and METHOD.steps[0] not in prompt
        if backend == "codex":
            assert "mcp__evosight_skills__read_research_skill" in prompt
            settings = tomllib.loads("\n".join(cmd[i+1] for i,arg in enumerate(cmd) if arg=="-c"))
            server = settings["mcp_servers"]["evosight_skills"]
            assert server["enabled_tools"] == ["read_research_skill"]
            args = server["args"]
        else:
            overlay = Path(cmd[max(i for i,arg in enumerate(cmd) if arg=="--patch")+1])
            policy = yaml.safe_load(overlay.read_text(encoding="utf-8"))
            args = next(item["insert"][0]["config"]["args"] for item in policy if "insert" in item)
        assert "--archive-only" in args
        records = json.loads(Path(args[args.index("--skills-file")+1]).read_text(encoding="utf-8"))
        stream = StringIO()
        calls = [{"id":1,"method":"tools/list"}, {"id":2,"method":"tools/call",
                  "params":{"name":"read_research_skill","arguments":{"id":candidate["id"]}}},
                 {"id":3,"method":"tools/call","params":{"name":"read_research_skill","arguments":{"id":9999}}}]
        web_search_tool.serve(map(json.dumps,calls), stream, web=False, skills=records)
        listed, result, unknown = [json.loads(line)["result"] for line in stream.getvalue().splitlines()]
        assert [tool["name"] for tool in listed["tools"]] == ["read_research_skill"]
        assert json.loads(result["content"][0]["text"])["steps"] == METHOD.steps
        assert unknown["isError"] is True
        response = agents.Analysis(fact="",judgment="",watch="",evidence_ids=[],items=[],discoveries=[]).model_dump_json()
        if backend=="dsh":
            return [{"type":"final","reason":{"kind":"completed"},"text":response}]
        Path(cmd[cmd.index("--output-last-message")+1]).write_text(response, encoding="utf-8")
        return []
    monkeypatch.setattr(agents, "run_cli", capture)
    worker.call_recorded(job, "study", {})
    with store.connection() as c:
        recorded = json.loads(c.execute("SELECT payload FROM run_inputs").fetchone()[0])
        assert recorded["approved_skill_versions"][0]["title"] == METHOD.title
