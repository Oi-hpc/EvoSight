import pytest
from fastapi.testclient import TestClient

from evosight import agents, db, workflow
from evosight.api import make_app
from evosight.worker import Worker


def test_profiles_respect_explicit_choices_and_carry_failures_to_the_queue(monkeypatch):
    monkeypatch.delenv("EVOSIGHT_RESEARCH_BACKEND", raising=False)
    dsh = "dashscope/kimi-k3.1-pro"
    for mode, codex, effort in (("test", "gpt-6.2-sol", "low"), ("live", "gpt-6.2-astra", "max")):
        runner = agents.Runner(mode)
        for role in agents.CONTRACTS:
            assert runner.profile(role) == (dsh if role in ("discuss_dsh", "news") else codex, effort)
            assert runner.profile(role, "dsh") == (codex if role == "discuss_codex" else dsh, effort)

    runner = agents.Runner("test")
    codex, effort = runner.profile("plan")
    attempted = []

    def fail(role, ctx, model, effort, directory, thread_id, archive=()):
        attempted.append((role, model))
        raise RuntimeError("服务暂不可用")

    monkeypatch.setattr(runner, "_invoke", fail)
    for role in ("plan", "news", "forecast", "verify"):
        with pytest.raises(RuntimeError, match="服务暂不可用"):
            runner.turn(role, {})
    assert attempted == [("plan", codex), ("news", dsh), ("forecast", codex), ("verify", codex)]

    monkeypatch.setenv("EVOSIGHT_RESEARCH_BACKEND", "dsh")
    for role in agents.RESEARCH_ROLES:
        assert runner.profile(role) == (dsh, effort)
    assert (
        runner.profile("forecast")
        == runner.profile("verify")
        == runner.profile("discuss_codex")
        == (codex, effort)
    )
    monkeypatch.setenv("EVOSIGHT_RESEARCH_BACKEND", "codex")
    assert runner.profile("specialist", "dsh") == (codex, effort)
    assert runner.profile("verify", "dsh") == (dsh, effort)


def test_workspace_setting_and_queued_provider_survive_switches(tmp_path, monkeypatch):
    store = db.Store(tmp_path)
    # Reopen a database created before the backend setting existed.
    with store.connection(True) as c:
        c.execute("DELETE FROM settings WHERE key='agent_backend'")
    store = db.Store(tmp_path)
    runner = agents.Runner("test")
    monkeypatch.delenv("EVOSIGHT_RESEARCH_BACKEND", raising=False)
    calls = []

    def invoke(role, ctx, model, effort, directory, thread_id, archive=()):
        calls.append((role, model, thread_id, (ctx.get("调查检查点") or {}).get("run")))
        if role == "verify":
            return (
                agents.Verdict(outcome="pending", public_at="", reason="待公告", lesson="", evidence_ids=[]),
                (1, 1),
                None,
                model,
            )
        assert directory is not None
        answer = agents.DiscussionAnswer(kind="insufficient", answer="当前材料不足以确认。", citations=[])
        return answer, (1, 1), "codex-thread" if model.startswith("gpt-") else "session-dsh", model

    monkeypatch.setattr(runner, "_invoke", invoke)
    worker = Worker(store, runner)
    chat = "/api/agent/discussion"
    with TestClient(
        make_app(store, runner, background=False),
        base_url="http://127.0.0.1",
        headers={"X-EvoSight": "local"},
    ) as api:
        limits = api.get("/api/operations").json()["limits"]
        assert limits["agent_backend"] == api.get(chat).json()["default_provider"] == "codex"
        assert api.get(chat).json()["session_provider"] is None
        first = {"question": "全局研究有哪些进展？", "nonce": "first-default"}
        queued = api.post(chat, json=first).json()
        assert (
            api.post("/api/settings", json={**limits, "agent_backend": "dsh"}).json()["agent_backend"]
            == "dsh"
        )
        assert api.post(chat, json=first).json() == queued
        assert api.post(chat, json={**first, "provider": "dsh"}).status_code == 422
        assert worker.step()
        assert api.get(chat).json()["default_provider"] == "dsh"
        assert api.get(chat).json()["session_provider"] == "codex"
        second = {"question": "下一步有什么待查？", "nonce": "second-default"}
        assert api.post(chat, json=second).status_code == 202 and worker.step()
        profiles = api.get("/api/operations").json()["profiles"]
        assert (
            profiles["forecast"][0]
            == profiles["verify"][0]
            == profiles["plan"][0]
            == "dashscope/kimi-k3.1-pro"
        )
        previous_client = {k: v for k, v in limits.items() if k != "agent_backend"}
        assert api.post("/api/settings", json=previous_client).json()["agent_backend"] == "dsh"
        assert api.post("/api/settings", json={**limits, "agent_backend": "typo"}).status_code == 422
        assert (
            api.post(chat, json={**second, "nonce": "explicit-dsh", "provider": "dsh"}).status_code == 202
            and worker.step()
        )
        assert api.get(chat).json()["session_provider"] == "dsh"
        assert api.post(chat, json={**second, "nonce": "continue-dsh"}).status_code == 202 and worker.step()
        assert (
            api.post(chat, json={**second, "nonce": "explicit-codex", "provider": "codex"}).status_code == 202
            and worker.step()
        )
        assert (
            api.post(chat, json={**second, "nonce": "return-to-dsh", "provider": "dsh"}).status_code == 202
            and worker.step()
        )
        turns = api.get(chat).json()["turns"]
        assert [t["provider"] for t in turns] == ["codex", "codex", "dsh", "dsh", "codex", "dsh"]
        assert calls == [
            ("discuss_codex", "gpt-6.2-sol", None, None),
            ("discuss_codex", "gpt-6.2-sol", "codex-thread", turns[0]["run_id"]),
            ("discuss_dsh", "dashscope/kimi-k3.1-pro", None, turns[1]["run_id"]),
            ("discuss_dsh", "dashscope/kimi-k3.1-pro", "session-dsh", turns[2]["run_id"]),
            ("discuss_codex", "gpt-6.2-sol", "codex-thread", turns[3]["run_id"]),
            ("discuss_dsh", "dashscope/kimi-k3.1-pro", "session-dsh", turns[4]["run_id"]),
        ]
        with store.connection(True) as c:
            job = db.require(c, "jobs", db.enqueue(c, "verify"))
        _, actual_model, _ = worker.call_recorded(job, "verify", {})
        assert actual_model == "dashscope/kimi-k3.1-pro" and calls[-1] == ("verify", actual_model, None, None)
    with db.Store(tmp_path).connection() as c:
        assert db.config(c)["agent_backend"] == "dsh"
        assert [
            tuple(row)
            for row in c.execute("SELECT requested_thread,observed_thread FROM run_sessions ORDER BY run_id")
        ] == [
            (None, "codex-thread"), ("codex-thread", "codex-thread"), (None, "session-dsh"),
            ("session-dsh", "session-dsh"), ("codex-thread", "codex-thread"), ("session-dsh", "session-dsh"),
            (None, None),
        ]


def test_news_backend_setting_applies_to_new_lines_and_retry_keeps_dsh_session(tmp_path, monkeypatch):
    store = db.Store(tmp_path)
    with store.connection(True) as c:
        c.execute("DELETE FROM settings WHERE key='news_agent_backend'")
        c.execute("ALTER TABLE agent_sessions DROP COLUMN dsh_session_id")
        event = db.add_event(c, "发布方", "查找窗口内公告", [], status="paused")
        job = db.require(c, "jobs", db.enqueue(c, "collect", event=event))
    store = db.Store(tmp_path)
    monkeypatch.delenv("EVOSIGHT_RESEARCH_BACKEND", raising=False)
    attempts = []

    def invoke(self, role, ctx, model, effort, directory, thread_id, archive=()):
        attempts.append((model, directory, thread_id))
        if len(attempts) == 2:
            raise RuntimeError("模型暂时不可用")
        return agents.Report(), (1, 1), "session-dsh" if model.startswith("dashscope/") else "thread-codex", model

    monkeypatch.setattr(agents.Runner, "_invoke", invoke)
    runner = agents.Runner("test")
    context = {"task": {"line": "新闻核查", "question": "公告发布了吗？"}}
    dsh, codex = "dashscope/kimi-k3.1-pro", "gpt-6.2-sol"
    with TestClient(
        make_app(store, runner, background=False),
        base_url="http://127.0.0.1",
        headers={"X-EvoSight": "local"},
    ) as api:
        settings = api.get("/api/operations").json()
        limits = settings["limits"]
        assert limits["news_agent_backend"] == "dsh" and limits["agent_backend"] == "codex"
        assert settings["profiles"]["news"][0] == dsh and settings["profiles"]["plan"][0] == codex
        assert Worker(store, runner).call_recorded(job, "news", context)[1] == dsh
        assert api.post("/api/settings", json={**limits, "news_agent_backend": "codex"}).status_code == 200
        assert api.post("/api/settings", json={k: v for k, v in limits.items() if k != "news_agent_backend"}).json()[
            "news_agent_backend"
        ] == "codex"
        assert api.post("/api/settings", json={**limits, "news_agent_backend": "typo"}).status_code == 422
        assert api.get("/api/operations").json()["profiles"]["news"][0] == codex

    store = db.Store(tmp_path)
    worker = Worker(store, agents.Runner("test"))
    with pytest.raises(RuntimeError, match="模型暂时不可用"):
        worker.call_recorded(job, "news", {**context, "round": 2})
    assert worker.call_recorded(job, "news", {**context, "round": 2})[1] == dsh
    fresh = {"task": {**context["task"], "line": "另一条新闻线"}}
    assert worker.call_recorded(job, "news", fresh)[1] == codex
    assert [(model, ident) for model, _, ident in attempts] == [
        (dsh, None), (dsh, "session-dsh"), (dsh, "session-dsh"), (codex, None),
    ]
    assert attempts[0][1] == attempts[1][1] == attempts[2][1] != attempts[3][1]
    with store.connection() as c:
        assert c.execute(
            "SELECT backend,dsh_session_id,thread_id FROM agent_sessions WHERE dsh_session_id IS NOT NULL"
        ).fetchone()[:] == ("dsh", "session-dsh", None)


def test_research_lanes_keep_their_backend_across_restarts_and_database_upgrade(tmp_path, monkeypatch):
    store = db.Store(tmp_path)
    runner = agents.Runner("test")
    monkeypatch.delenv("EVOSIGHT_RESEARCH_BACKEND", raising=False)
    monkeypatch.setattr(
        runner, "_invoke",
        lambda role, ctx, model, effort, directory, thread_id, archive=(): (
            agents.SourceScout(publisher_urls=[], queries=[]), (1, 1),
            "saved-codex" if model.startswith("gpt-") else None, model,
        ),
    )
    with store.connection(True) as c:
        event = db.add_event(c, "后端续接测试", "核对原始披露", [], status="paused")
        job = db.require(c, "jobs", db.enqueue(c, "collect", event=event))
    worker = Worker(store, runner)
    first_line = {"question": "核查披露时间"}
    other_line = {"question": "核查交割条件"}
    codex = "gpt-6.2-sol"
    dsh = "dashscope/kimi-k3.1-pro"
    assert worker.call_recorded(job, "source_scout", {})[1] == codex
    assert worker.call_recorded(job, "source_scout", {"task": first_line})[1] == codex
    monkeypatch.setenv("EVOSIGHT_RESEARCH_BACKEND", "dsh")
    assert worker.call_recorded(job, "source_scout", {"task": other_line})[1] == dsh
    with store.connection(True) as c:
        c.execute("UPDATE settings SET value=? WHERE key='agent_backend'", (db.dump("dsh"),))
        c.execute("ALTER TABLE agent_sessions DROP COLUMN backend")
    reopened = db.Store(tmp_path)
    worker = Worker(reopened, runner)
    with reopened.connection() as c:
        assert workflow.main_session_backend(c, event) == "codex"
        assert workflow.main_session_backend(c, None) is None
    assert worker.call_recorded(job, "source_scout", {"round": 2})[1] == codex
    assert worker.call_recorded(job, "source_scout", {"task": first_line, "round": 2})[1] == codex
    monkeypatch.setenv("EVOSIGHT_RESEARCH_BACKEND", "codex")
    assert worker.call_recorded(job, "source_scout", {"task": other_line, "round": 2})[1] == dsh
