from io import StringIO
import json
from pathlib import Path
import tomllib

import pytest
import yaml

from evosight import agents, agent_tools, db, discussion, feeds, materials, research, web_search_tool
from evosight.contracts import Report, ResearchLookup
from evosight.worker import Worker


def test_native_permissions_and_files_are_per_turn(tmp_path, monkeypatch):
    monkeypatch.setenv("EVOSIGHT_CODEX_SEARCH", "native")
    seen = []
    config = tmp_path / "config.toml"
    config.write_text('[mcp_servers.personal]\ncommand="unused"\n', encoding="utf-8")
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    monkeypatch.setattr(agents, "executable_codex", lambda: "test-codex")
    monkeypatch.setattr(agent_tools, "scoped_shell", lambda _: True)
    lane = tmp_path / "lane"
    f = {
        "id": 19,
        "document_id": 3,
        "title": "发布方公告",
        "url": "https://source.example/doc",
        "locator": "第二段",
        "text": "档案的完整原文",
    }

    def capture(cmd, cwd, prompt, env, timeout):
        raw = [cmd[i + 1] for i, value in enumerate(cmd) if value == "-c"]
        options = tomllib.loads("\n".join(raw))
        policy = options["permissions"]["evosight"]
        folder = next(Path(path) for path in policy["filesystem"] if path not in (":minimal", str(cwd)))
        assert policy["filesystem"] == {":minimal": "read", str(cwd): "read", str(folder): "read"}
        assert policy["network"] == {"enabled": False}
        assert cmd[cmd.index("-a") + 1] == "never"
        assert cmd[cmd.index("shell_tool") - 1] == "--enable"
        assert cmd[cmd.index("multi_agent") - 1] == "--disable"
        assert options["mcp_servers"]["personal"]["enabled"] is False
        assert "evosight_web" not in options["mcp_servers"]
        assert options["notify"] == []
        content = (folder / "D3.txt").read_text(encoding="utf-8")
        assert content.endswith(f["text"]) and "fragment_id=19" in content
        assert "档案的完整原文" not in prompt and str(folder) in prompt
        assert json.loads((folder / "context.json").read_text(encoding="utf-8"))["basis"] in (
            "latest",
            "original",
        )
        seen.append((folder, options["web_search"]))
        Path(cmd[cmd.index("--output-last-message") + 1]).write_text(
            '{"publisher_urls":[],"queries":[]}', encoding="utf-8"
        )
        return [{"type": "thread.started", "thread_id": "same"}]

    monkeypatch.setattr(agents, "run_cli", capture)
    runner = agents.Runner("test")
    profile = runner.profile("source_scout", "codex")
    runner.turn("source_scout", {"basis": "latest"}, lane, archive=[f], profile=profile)
    runner.turn("source_scout", {"basis": "original"}, archive=[f], profile=profile)
    assert [mode for _, mode in seen] == ["live", "disabled"]
    assert seen[0][0] != seen[1][0] and all(not path.exists() for path, _ in seen)
    for role, ctx in [
        ("study", {}),
        ("forecast", {}),
        ("forecast", {"独立复核": True}),
        ("verify", {}),
        ("news", {"demo": True}),
        ("plan", {"replay": True}),
    ]:
        assert not agent_tools.live_web(role, ctx)
    fallback = agent_tools.codex_options(lane, tmp_path / "materials", True, False)
    assert fallback[fallback.index("shell_tool") - 1] == "--disable" and 'web_search="live"' in fallback


def test_linux_npm_codex_only_grants_read_access_to_the_native_binary(tmp_path, monkeypatch):
    package = tmp_path / "node_modules/@openai/codex"
    launcher = package / "bin/codex.js"
    launcher.parent.mkdir(parents=True)
    launcher.touch()
    native = package / "node_modules/@openai/codex-linux-x64/vendor/x86_64-unknown-linux-musl/bin/codex"
    native.parent.mkdir(parents=True)
    native.touch()
    native.chmod(0o755)
    monkeypatch.setattr(agents.platform, "machine", lambda: "x86_64")
    assert agents._linux_codex_binary(str(launcher)) == str(native.resolve())

    lane, evidence = tmp_path / "lane", tmp_path / "evidence"
    options = agent_tools.codex_options(lane, evidence, False, executable=native)
    config = tomllib.loads("\n".join(options[i + 1] for i, arg in enumerate(options) if arg == "-c"))
    policy = config["permissions"]["evosight"]
    assert policy["filesystem"] == {
        ":minimal": "read",
        str(lane.resolve()): "read",
        str(evidence.resolve()): "read",
        str(native.resolve()): "read",
    }
    assert policy["network"] == {"enabled": False}


def test_headless_dsh_can_resolve_the_program_search_key_without_a_desktop_store(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setenv("EVOSIGHT_PUBLIC_SEARCH", "deepseek")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "fixture-key")
    policy = agents._dsh_policy(tmp_path, "dashscope/kimi-k3.1-pro", "low", True, ())
    credentials = next(item for item in policy if item.get("id") == "credentials")
    assert credentials == {"id": "credentials", "disabled": False}
    plugin = next(item["insert"][0] for item in policy if "insert" in item)
    assert plugin["inject"] == ["tools", "credentials"]
    mcp = plugin["config"]
    assert mcp["expandTemplates"] is True
    assert mcp["env"]["DEEPSEEK_API_KEY"] == "${credential:DEEPSEEK_API_KEY}"
    assert "fixture-key" not in repr(policy)


@pytest.mark.parametrize("time_scope", ["window", "history"])
def test_news_prompt_uses_saved_round_window_when_resuming(tmp_path, monkeypatch, time_scope):
    monkeypatch.delenv("EVOSIGHT_CODEX_SEARCH", raising=False)
    monkeypatch.setattr(agents, "executable_codex", lambda: "test-codex")
    monkeypatch.setattr(agent_tools, "scoped_shell", lambda _: False)
    monkeypatch.setattr(agents, "utc", lambda: "2026-11-27T15:00:00Z")
    start, end = "2026-11-25T22:00:00Z", "2026-11-26T04:00:00Z"

    def capture(cmd, cwd, prompt, env, timeout):
        assert "resume" in cmd
        assert 'web_search="disabled"' in cmd
        assert any('evosight_web={enabled=true' in option and "search_public_web" in option for option in cmd)
        assert "用 EvoSight 的 search_public_web 主动搜索" in prompt
        assert f"从 {start} 之后到 {end}（含截止）" in prompt
        assert "转载、抓取时间或仅改页面日期不算实质更新" in prompt
        if time_scope == "history":
            assert "搜索截至本轮截止已公开的所需材料" in prompt
        else:
            assert "本轮绝对日期缩小搜索范围" in prompt
            assert "未找到可核实的进展，在 gaps 说明" in prompt
        Path(cmd[cmd.index("--output-last-message") + 1]).write_text("{}", encoding="utf-8")
        return []

    monkeypatch.setattr(agents, "run_cli", capture)
    agents.Runner("test").turn(
        "news",
        {"window": [start, end], "task": {"time_scope": time_scope}},
        tmp_path / "scout",
        "existing-thread",
        profile=("gpt-6.2-sol", "low"),
    )


def test_dsh_resumes_and_mounts_only_the_turns_allowed_tools(tmp_path, monkeypatch):
    patch = tmp_path / "desktop.yml"
    patch.write_text("[]", encoding="utf-8")
    monkeypatch.setenv("EVOSIGHT_DSH_PATCH", str(patch))
    monkeypatch.delenv("EVOSIGHT_RESEARCH_BACKEND", raising=False)
    monkeypatch.setattr(agents, "dsh_command", lambda: (["test-dsh"], {}))
    lane = tmp_path / "news"
    fragment = {"id": 35, "document_id": 2, "locator": "轨道周期", "title": "公告",
                "url": "https://publisher.example/news", "text": "A 75-hour orbit."}
    launches = []

    def capture(cmd, cwd, prompt, env, timeout):
        overlay = Path(cmd[cmd.index("--patch", cmd.index("--patch") + 1) + 1])
        policy = yaml.safe_load(overlay.read_text(encoding="utf-8"))
        by_id = {item["id"]: item for item in policy if "id" in item}
        mcp = next(item["insert"][0]["config"] for item in policy if "insert" in item)
        args = mcp["args"]
        path = Path(args[args.index("--archive-file") + 1])
        assert json.loads(path.read_text(encoding="utf-8")) == [fragment]
        assert by_id["tool-pwsh"]["disabled"] and by_id["tool-fs"]["disabled"]
        assert cmd[cmd.index("--permission-mode") + 1] == "read-only"
        assert cmd[cmd.index("--approval-policy") + 1] == "never"
        assert "read_archived_evidence" in prompt
        if cwd == lane:
            assert by_id["tool-web"]["config"]["search"] and not by_id["tool-web"]["config"]["fetch"]
            assert by_id["web-deepseek-api"]["config"]["searchBaseURL"] == "https://api.deepseek.com/v1/tools"
            assert "优先用 DSH 内置 web_search" in prompt and "--archive-only" not in args
            assert "mcp__evosight_web__search_public_web" in prompt
        else:
            assert by_id["tool-web"]["disabled"] and "--archive-only" in args
            assert "web-deepseek-api" not in by_id and "优先用 DSH 内置 web_search" not in prompt
        launches.append((cwd, Path(env["DSH_HOME"]), cmd, path))
        return [
            {"type": "session", "sessionId": "session-news"},
            {"type": "status", "usage": {"inputTokens": 1, "outputTokens": 2}},
            {"type": "final", "reason": {"kind": "completed"}, "text": "{}"},
        ]

    monkeypatch.setattr(agents, "run_cli", capture)
    runner = agents.Runner("test")
    _, tokens, session_id, _ = runner.turn("news", {}, lane, archive=[fragment])
    assert session_id == "session-news" and tokens == (1, 2)
    runner.turn("news", {}, lane, session_id, archive=[fragment])
    runner.turn("news", {"basis": "original"}, archive=[fragment])
    first, resumed, frozen = launches
    assert first[:2] == resumed[:2] == (lane, tmp_path / "news-dsh-state")
    assert frozen[0] != lane and frozen[1] != first[1]
    assert "--session-id" not in first[2] and "--session-id" not in frozen[2]
    assert resumed[2][resumed[2].index("--session-id") + 1] == session_id
    assert all(not launch[3].exists() for launch in launches)


def test_project_search_tool_exposes_only_search_results_with_public_links(monkeypatch):
    url = "https://publisher.example/press"
    monkeypatch.setattr(
        materials, "search_web", lambda query, limit: [("Official", url), ("Private", "https://127.0.0.1/")]
    )
    commands = [
        ("initialize", {}),
        ("tools/list", {}),
        ("tools/call", {"name": "search_public_web", "arguments": {"query": "publisher press"}}),
        ("tools/call", {"name": "search_public_web", "arguments": {"query": "x" * 251}}),
    ]
    incoming = StringIO("\n".join(
        json.dumps({"id": i, "jsonrpc": "2.0", "method": name, "params": params})
        for i, (name, params) in enumerate(commands, 1)
    ))
    outgoing = StringIO()
    web_search_tool.serve(incoming, outgoing)
    replies = [json.loads(line)["result"] for line in outgoing.getvalue().splitlines()]
    assert replies[0]["capabilities"] == {"tools": {}}
    assert [t["name"] for t in replies[1]["tools"]] == ["search_public_web"]
    assert json.loads(replies[2]["content"][0]["text"]) == [{"title": "Official", "url": url}]
    assert replies[3]["isError"]


def test_latest_chat_freezes_native_url_before_citing_or_retrying(tmp_path):
    store = db.Store(tmp_path)
    url = "https://issuer.example/notice"
    lookups = []

    def download(address):
        assert address == url
        lookups.append(address)
        return feeds.Resource(
            b"<html><title>Issuer notice</title><article><h1>Issuer notice</h1>"
            b"<p>The revised service schedule starts on 20 May, as specified by the issuer.</p></article></html>",
            url,
            "text/html",
        )

    def no_search(*args):
        pytest.fail("An exact URL must not depend on keyword search")

    acq = materials.Acquisition(search=no_search, download=download)
    first = agents.DiscussionAnswer(kind="lookup", answer="核对发布方公告", citations=[], source_urls=[url])

    class Chat:
        def profile(self, role):
            return "fixture", "low"

        def call(self, role, ctx):
            if "web_lookup" not in ctx:
                return first, (1, 1)
            assert "20 May" in ctx["sources"]["W1"]["text"]
            return agents.DiscussionAnswer(
                kind="analysis", answer="发布方写明 5 月 20 日生效 [W1]。", citations=["W1"]
            ), (1, 1)

    with store.connection(True) as c:
        submitted = discussion.submit(c, None, None, "latest", "codex", "发布方的最新安排？", "chat-test")
        job = db.require(c, "jobs", submitted["job_id"])
    worker = Worker(store, Chat(), acquisition=acq)
    assert worker.process(job) == worker.process(job)
    with store.connection() as c:
        turn = db.require(c, "discussion_turns", submitted["turn_id"])
        stored = json.loads(turn["context"])
        assert "20 May" in stored["sources"]["W1"]["text"]
        assert json.loads(turn["citations"])[0]["href"] == url and lookups == [url]
    for ctx in [stored, {"basis": "original"}, {"basis": "latest", "demo": True}]:
        with pytest.raises(ValueError):
            discussion.validate_answer(first, ctx)
    with pytest.raises(ValueError):
        discussion.validate_answer(
            first.model_copy(update={"source_urls": ["https://127.0.0.1/"]}), {"basis": "latest"}
        )
    assert acq.targeted("https://127.0.0.1/") == []
    assert len(ResearchLookup(tool="web", query=url + "x" * 300).query) > 250
    with pytest.raises(ValueError):
        ResearchLookup(tool="handoff", query="x" * 251)


def test_archived_paragraph_beyond_prompt_is_citable_and_frozen(tmp_path):
    store = db.Store(tmp_path)
    target = "The issuer published the final rule."
    with store.connection(True) as c:
        event = db.add_event(c, "公告", "读原文", [])
        url = "https://issuer.example/report"
        doc = feeds.ingest(
            c, event, url, {"url": url, "title": "Issuer report", "summary": "", "published_at": None}
        )
        extracted = materials.Extract(
            "article", [(str(i), target if i == 30 else f"Paragraph {i}.", url) for i in range(31)]
        )
        materials.save_asset(c, doc, url, "article", b"test report", "text/html", extracted)
        archive = materials.fragments_for(c, [doc], per_document=None)
        fragment = next(f["id"] for f in archive if f["text"] == target)
        assert fragment not in {f["id"] for f in research.model_fragments(archive)}
        ids = agent_tools.archive_ids(c, {"evidence": [{"id": doc}]})
        assert fragment in ids and agent_tools.read_archive(c, [fragment])[0]["text"] == target
        allowed = agent_tools.read_archive(c, ids)
        commands = [
            ("tools/list", {}),
            ("tools/call", {"name": "read_archived_evidence", "arguments": {"query": "published final rule"}}),
            ("tools/call", {"name": "read_archived_evidence", "arguments": {"fragment_id": fragment, "document_id": doc + 1}}),
            ("tools/call", {"name": "search_public_web", "arguments": {"query": "public results"}}),
        ]
        incoming = StringIO("\n".join(
            json.dumps({"id": i, "jsonrpc": "2.0", "method": name, "params": params})
            for i, (name, params) in enumerate(commands, 1)
        ))
        outgoing = StringIO()
        web_search_tool.serve(incoming, outgoing, web=False, archive=allowed)
        listing, found, denied, web = map(json.loads, outgoing.getvalue().splitlines())
        assert [tool["name"] for tool in listing["result"]["tools"]] == ["read_archived_evidence"]
        retrieval = json.loads(found["result"]["content"][0]["text"])
        assert retrieval[0]["fragment_id"] == fragment and retrieval[0]["text"] == target
        assert json.loads(denied["result"]["content"][0]["text"]) == []
        assert web["error"]["code"] == -32601
        fields = {"basis": "original", "sources": {f"D{doc}": {}}, "forecast_run": {"input": {}}}
        assert agent_tools.archive_ids(c, fields) == []
        fields["forecast_run"]["input"]["archive_fragment_ids"] = [fragment]
        assert agent_tools.archive_ids(c, fields) == [fragment]
    report = Report.model_validate(
        {
            "findings": [
                {
                    "section": "news",
                    "kind": "fact",
                    "subject": "规则",
                    "text": "发布方公布了最终规则。",
                    "citations": [{"fragment_id": fragment, "quote": target}],
                }
            ]
        }
    )
    assert research.check_report(report, archive, {"archive_fragment_ids": ids}, "news").findings
