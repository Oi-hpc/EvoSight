"""Deterministic publisher and model replay through the real API, queue and record writers."""
from collections import Counter
from datetime import datetime, timedelta, timezone
from html import escape
import json
import threading

from fastapi.testclient import TestClient
import pytest

from evosight import (
    agents,
    db,
    feeds,
    forecast,
    materials,
    research,
    watch,
    workflow,
    world,
    worker as worker_module,
)
from evosight.api import make_app
from evosight.contracts import (
    Assignment,
    Citation,
    Finding,
    Followup,
    KnowledgeLink,
    Plan,
    Report,
    ROLE_NAMES,
)
from evosight.db import Store, one, rows, require
from evosight.worker import Worker

HOST = "https://publisher.example.org"
FEED = HOST + "/bulletins.xml"
TITLE = "海岚市域线开放"
BASE = "海岚交通局于 2026-11-25 08:30 UTC 公布市域线验收方案。"
HISTORY = "运营方于 2026-08-10 09:30 UTC 在原方案中列明独立验收条件。"
DECISION = "海岚交通局于 2026-11-25 15:02 UTC 正式确认市域线对公众开放。"
HEADERS = {"X-EvoSight": "local"}


class Clock:
    def __init__(self):
        self.dt = datetime(2026, 11, 25, 9, tzinfo=timezone.utc)

    def utc(self, value=None):
        return (
            (value or self.dt).astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
        )

    def later(self, seconds):
        return self.utc(self.dt + timedelta(seconds=seconds))


class Publisher(materials.Acquisition):
    def __init__(self):
        self.articles = [("notice", "海岚市域线验收方案", BASE, "2026-11-25T08:30:00Z")]
        self.hits = Counter()
        self.down = False
        super().__init__(download=self.read, search=self.search_hits)

    def search_hits(self, query, maximum):
        self.hits["search"] += 1
        return [("海岚市域线运营方历史公告", HOST + "/history")] if "历史" in query else [("海岚市域线官方公告", HOST + "/")]

    def read(self, url, cached=None, **kwargs):
        self.hits[url] += 1
        if url == FEED:
            if self.down:
                raise ValueError("来源暂不可用")
            tag = str(len(self.articles))
            if cached and cached.get("etag") == tag:
                return feeds.Resource(b"", url, "", tag, None, True)
            entries = "".join(
                f"<item><guid isPermaLink='false'>{key}</guid><title>{escape(title)}</title><link>{HOST}/{key}</link><description>{escape(content)}</description><pubDate>{time}</pubDate></item>"
                for key, title, content, time in reversed(self.articles)
            )
            return feeds.Resource(
                f"<rss version='2.0'><channel>{entries}</channel></rss>".encode(),
                url,
                "application/rss+xml",
                tag,
            )
        if url == HOST + "/":
            return feeds.Resource(
                f'<html><head><link rel="alternate" type="application/rss+xml" href="{FEED}"></head><main>市域线公告</main></html>'.encode(),
                url,
                "text/html",
            )
        if url == HOST + "/remarks.vtt":
            return feeds.Resource(
                b"WEBVTT\n\n00:00:05.000 --> 00:00:09.000\nApproved for examination.\n", url, "text/vtt"
            )
        if url == HOST + "/briefing.mp4":
            raise ValueError("只有发布方字幕可取")
        if url == HOST + "/history":
            key, title, body, pub = "history", "运营方历史公告", HISTORY, "2026-08-10T09:30:00Z"
        else:
            key, title, body, pub = next(a for a in self.articles if url == HOST + "/" + a[0])
        content = f'<html><head><meta property="article:published_time" content="{pub}"></head><body><article><h1>{title}</h1><p>{body}</p><p>交通局和运营企业分别公开了检查程序与各自职责。</p><video src="/briefing.mp4"><track src="/remarks.vtt" kind="captions"></video></article></body></html>'
        return feeds.Resource(content.encode(), url, "text/html; charset=utf-8")


class Replay:
    mode = "test"

    def __init__(self, roles=None):
        self.roles = roles or list(ROLE_NAMES)
        self.calls = Counter()
        self.fail = set()
        self.finished = False
        self.on_role = None

    def profile(self, role):
        return agents.Runner("test").profile(role)

    def call(self, role, ctx):
        self.calls[role] += 1
        if self.on_role:
            self.on_role(role)
        if role in self.fail:
            raise RuntimeError("回放注入角色超时")
        if role == "source_scout":
            return agents.SourceScout(publisher_urls=[FEED], queries=[]), (12, 5)
        if role == "plan":
            tasks = [
                Assignment(
                    role=r,
                    question=ctx["研究范围"] + " · " + ROLE_NAMES[r],
                    target=TITLE,
                    time_scope="history" if r in ("specialist", "counter") else "window",
                    priority=4,
                    done_when="找到可定位原始材料",
                    search_query="市域线 历史 原方案" if r == "specialist" else "",
                )
                for r in self.roles
            ]
            return Plan(rationale="核对公告、缺口与反证", tasks=tasks), (25, 13)
        if role in ROLE_NAMES:
            snippet = next(
                (
                    f
                    for f in ctx["fragments"]
                    if (HISTORY if role == "specialist" else DECISION if self.finished else BASE) in f["text"]
                ),
                ctx["fragments"][0],
            )
            quote = next((x for x in (HISTORY, DECISION, BASE) if x in snippet["text"]), snippet["text"][:32])
            when = {
                BASE: "2026-11-25T08:30:00Z",
                HISTORY: "2026-08-10T09:30:00Z",
                DECISION: "2026-11-25T15:02:00Z",
            }.get(quote, "")
            section = {
                "people": "people",
                "organizations": "people",
                "opinion": "opinion",
                "news": "news",
            }.get(role, "background")
            fields = {
                "people": {"公开立场": "公开承诺按期披露进展"},
                "organizations": {"政策阶段": "草案"},
                "opinion": {"平台": "运营方公告评论", "去重后样本数": 2, "样本局限": "只代表该样本"},
            }.get(role, {})
            f = Finding(
                section=section,
                kind="fact" if role in ("news", "specialist") else "view",
                subject="林晴" if role == "people" else ROLE_NAMES[role],
                text=ROLE_NAMES[role] + " · " + quote,
                citations=[Citation(fragment_id=snippet["id"], quote=quote)],
                time_quote=quote if when else "",
                occurred_at=when,
                entity_kind="person" if role == "people" else "",
                identity="海岚运营发言人" if role == "people" else "",
                aliases=["发言人林晴"] if role == "people" else [],
                facets=fields,
            )
            return Report(
                findings=[f],
                gaps=["其他平台观点待核"] if role == "opinion" else [],
                followups=[Followup(question="验收文件何时签署", condition="发布方公布签字文件")]
                if role == "dependencies"
                else [],
            ), (20, 12)
        ids = [d["id"] for d in ctx["evidence"]]
        if role == "study":
            return agents.Analysis(
                fact="",
                judgment="正式公告改变了判断" if self.finished else "目前仍待正式公告",
                watch="查看运营方公告",
                items=[],
                discoveries=[],
                evidence_ids=ids[:1],
                closure_reason="主管单位已确认开放" if self.finished else "",
            ), (20, 8)
        if role == "verify":
            decisive = next((d for d in ctx["evidence"] if DECISION in d["summary"]), None)
            return agents.Verdict(
                outcome="yes" if decisive else "pending",
                reason="按预登记公告核对",
                public_at="2026-11-25T15:02:00Z" if decisive else "",
                lesson="区分验收计划和实际载客",
                evidence_ids=[decisive["id"]] if decisive else [],
            ), (24, 12)
        if role == "forecast":
            existing = ctx["已有预测"]
            return agents.Forecast(
                action="hold" if self.finished or existing else "create",
                prediction_id=0,
                question="明日以前是否正式开放？",
                deadline="2026-11-26T18:00:00Z",
                rule="主管单位公告确认实际载客",
                authority=HOST,
                p=65,
                reason="已有公开验收安排",
                evidence_ids=ids[:1],
                dependencies=[Followup(question="实际载客公告是否出现", condition="主管单位在原站发布决定性公告")],
            ), (21, 8)
        raise AssertionError(role)


@pytest.fixture
def clock(monkeypatch):
    clock = Clock()
    for module in (db, feeds, forecast, materials, watch, research, workflow, worker_module):
        if hasattr(module, "utc"):
            monkeypatch.setattr(module, "utc", clock.utc)
        if hasattr(module, "later"):
            monkeypatch.setattr(module, "later", clock.later)
    return clock


def setup(tmp_path, roles=None):
    store = Store(tmp_path)
    runner = Replay(roles)
    source = Publisher()
    with store.connection(True) as c:
        c.execute("UPDATE settings SET value='180' WHERE key='daily_calls'")
    return store, source, runner, Worker(store, runner, acquisition=source)


def new_event(store, title=TITLE):
    with store.connection(True) as c:
        eid = db.add_event(c, title, "追踪海岚市域线实际载客，而非试验计划", [])
        return workflow.begin_event(c, eid)


def test_full_lifecycle_and_historical_evidence(tmp_path, clock):
    store, source, runner, worker = setup(tmp_path)
    api = TestClient(
        make_app(store, runner, background=False, acquisition=source),
        base_url="http://127.0.0.1",
        headers=HEADERS,
    )
    response = api.post("/api/events", json={"title": TITLE, "scope": "海岚市域线何时载客"})
    assert response.status_code == 201
    eid = response.json()["id"]
    base = f"/api/events/{eid}"
    assert api.get(base + "/rounds").json()[0]["state"] == "queued"
    assert worker.step() and worker.step()
    k = api.get(base + "/knowledge").json()
    assert k["snapshot"] and len(k["sections"]) == 6 and k["sources"][0]["url"] == FEED
    assert set(runner.roles) <= {r for r in runner.calls if runner.calls[r] > 0}
    assert api.get(base + "/knowledge", params={"q": "发言人林晴"}).json()["sections"]["people"]
    assert any(HISTORY in item["text"] for item in k["sections"]["background"])
    facts = [n for n in k["sections"]["timeline"] if n["kind"] == "fact"]
    assert len(facts) == 1 and facts[0]["occurred_at"] == "2026-11-25T08:30:00Z"
    first = k["snapshot"]
    cit = k["sections"]["news"][0]["citations"][0]
    assets = api.get(base + f"/documents/{cit['document_id']}/fragments").json()
    assert any(p["link"] == HOST + "/briefing.mp4#t=5,9" for a in assets for p in a["parts"])
    assert any(
        cit["quote"] in p["text"] and p["id"] == cit["fragment_id"] for a in assets for p in a["parts"]
    )
    assert api.get(base + f"/assets/{assets[0]['id']}/original").status_code == 200
    pred = api.get(base).json()["predictions"][0]
    assert pred["versions"][0]["snapshot_id"] == first and pred["versions"][0]["dependencies"]
    with store.connection(True) as c:
        db.mark_read(c, eid, api.get("/api/home").json()["cards"][0]["token"])
    clock.dt = clock.dt.replace(hour=15, minute=6)
    source.articles.append(("open", "海岚市域线正式确认开放", DECISION, "2026-11-25T15:02:00Z"))
    runner.finished = True
    worker.watcher.cycle()
    assert worker.step()
    assert (
        api.post(
            f"/api/predictions/{pred['id']}/versions",
            json={"p": 98, "reason": "公告已出", "refs": [cit["document_id"]]},
        ).status_code
        == 422
    )
    assert worker.step()
    latest = api.get(base + "/knowledge").json()
    assert latest["snapshot"] != first and latest["closure"]
    before = api.get(base + "/knowledge", params={"revision": first}).json()
    assert not before["closure"] and not any(DECISION in x["text"] for x in before["sections"]["news"])
    assert api.get(base + "/knowledge", params={"compare": first}).json()["diff"]["added"]
    p = api.get(base).json()["predictions"][0]
    assert p["status"] == "resolved" and p["settlements"][0]["scores"]["final"]["brier"] == 0.1225
    assert any(o["origin"] == "待验证经验" for o in latest["observations"])
    assert (
        api.post(base + f"/closure/{latest['closure']['id']}", json={"choice": "archived"}).status_code == 200
    )
    assert api.get(base).json()["card"]["status"] == "archived"
    assert api.get(base + "/knowledge", params={"revision": first}).json()["goal"]["version"] == 1
    api.close()


def test_role_retry_and_edit_during_research(tmp_path, clock):
    store, source, runner, worker = setup(tmp_path, ["news", "counter"])
    info = new_event(store)
    eid = info["id"]
    rid = info["round"]
    runner.fail = {"counter"}
    assert worker.step()
    with store.connection() as c:
        assert require(c, "tracking_rounds", rid)["state"] == "partial"
        failed = one(c, "SELECT id FROM research_tasks WHERE state='failed'")["id"]
    api = TestClient(
        make_app(store, runner, background=False, acquisition=source),
        base_url="http://127.0.0.1",
        headers=HEADERS,
    )
    runner.fail.clear()
    assert api.post(f"/api/events/{eid}/rounds/{rid}/retry?task={failed}").is_success
    for _ in range(3):
        worker.step()
    with store.connection() as c:
        assert require(c, "tracking_rounds", rid)["state"] == "published"
    assert runner.calls["news"] == 1 and runner.calls["counter"] == 2
    goal = api.patch(
        f"/api/events/{eid}", json={"expected_version": 1, "scope": "只研究运营成本", "priority": 4}
    ).json()

    def change(role):
        if role == "plan":
            runner.on_role = None
            assert (
                api.patch(f"/api/events/{eid}", json={"expected_version": 2, "scope": "研究资金来源"}).status_code
                == 200
            )

    runner.on_role = change
    for _ in range(4):
        worker.step()
    assert api.patch(f"/api/events/{eid}", json={"expected_version": 1, "scope": "旧表单"}).status_code == 409
    with store.connection() as c:
        assert require(c, "tracking_rounds", goal["round"])["state"] == "superseded"
        assert not one(c, "SELECT 1 FROM knowledge_snapshots WHERE goal_version=2")
        assert workflow.latest(c, eid)["goal_version"] == 3
    api.close()


def test_shared_polling_reprints_and_unrelated_events(tmp_path, clock):
    store, source, runner, worker = setup(tmp_path, ["news"])
    a = new_event(store)["id"]
    b = new_event(store, "海岚市域线运营监管")["id"]
    for _ in range(5):
        worker.step()
    clock.dt += timedelta(minutes=15)
    before = source.hits[FEED]
    calls = sum(runner.calls.values())
    assert worker.watcher.cycle() >= 1
    assert source.hits[FEED] == before + 1 and sum(runner.calls.values()) == calls
    clock.dt += timedelta(minutes=15)
    title = "海岚市域线正式通过开放验收"
    for i, name in enumerate((title, "转载：" + title, "快讯：" + title, "转发：" + title)):
        source.articles.append((f"copy{i}", name, BASE * 6, clock.utc()))
    worker.watcher.cycle()
    with store.connection() as c:
        for eid in (a, b):
            assert (
                c.execute(
                    "SELECT count(*) FROM watch_signals WHERE event_id=? AND action='full' AND round_id IS NOT NULL",
                    (eid,),
                ).fetchone()[0]
                == 1
            )
    clock.dt += timedelta(minutes=11)
    source.articles.append(("independent", "海岚市域线宣布更改运营计划", "独立的最新决定及其效力说明。", clock.utc()))
    worker.watcher.cycle()
    with store.connection() as c:
        assert (
            c.execute(
                "SELECT count(DISTINCT round_id) FROM watch_signals WHERE event_id=? AND action='full'", (a,)
            ).fetchone()[0]
            == 2
        )


def test_world_entry_is_shared_across_events_and_corrections_preserve_forecast_history(tmp_path, clock):
    store, source, runner, worker = setup(tmp_path, ["news"])
    a = new_event(store)["id"]
    b = new_event(store, "海岚市域线无障碍通道")["id"]
    for _ in range(8):
        if not worker.step():
            break
    with TestClient(
        make_app(store, runner, background=False, acquisition=source),
        base_url="http://127.0.0.1",
        headers=HEADERS,
    ) as api:

        def index(event, **params):
            return api.get(f"/api/events/{event}/knowledge", params=params).json()

        first, second = index(a), index(b)
        old = first["sections"]["news"][0]
        other = second["sections"]["news"][0]
        assert old["id"] == other["id"] and old["world_id"] == other["world_id"]
        endpoint = f"/api/world/{old['world_id']}"
        assert {e["id"] for e in api.get(endpoint).json()["events"]} == {a, b}
        found = api.get("/api/world", params={"q": "验收", "category": "events"}).json()
        assert found["total"] == 1 and found["entries"][0]["id"] == old["world_id"]
        with store.connection() as c:
            assert c.execute("SELECT count(*) FROM claims").fetchone()[0] == 1
            pred = one(c, "SELECT id FROM predictions WHERE event_id=?", (a,))["id"]
            before_revision = forecast.detail(c, pred)["versions"][0]["snapshot_id"]
        cite = old["citations"][0]
        body = {
            "expected_snapshot": second["snapshot"],
            "text": "公告只确认验收安排。",
            "reason": "区分验收与载客",
            "fragment_id": cite["fragment_id"],
            "quote": cite["quote"],
        }
        edit = api.post(f"/api/events/{b}/knowledge/claims/{old['id']}/corrections", json=body)
        assert edit.status_code == 201, edit.text
        current = api.get(endpoint).json()
        assert current["version"] == 2 and len(current["history"]) == 2
        assert current["claim"]["event_id"] == b and current["claim"]["text"] == body["text"]
        for event, baseline in ((a, first), (b, second)):
            assert index(event)["sections"]["news"][0]["id"] == edit.json()["claim"]
            assert index(event, revision=baseline["snapshot"])["sections"]["news"][0]["id"] == old["id"]
            assert (
                api.get(f"/api/events/{event}/documents/{cite['document_id']}/fragments").status_code == 200
            )
        assert api.get(endpoint, params={"revision": old["id"]}).json()["claim"]["text"] == old["text"]
        with store.connection() as c:
            assert forecast.detail(c, pred)["versions"][0]["snapshot_id"] == before_revision
        Store(tmp_path)
        assert api.get(endpoint).json()["history"] == current["history"]


def test_world_reuses_existing_evidence_without_a_working_publisher(tmp_path, clock, monkeypatch):
    store, source, runner, worker = setup(tmp_path, ["news"])
    a = new_event(store)["id"]
    for _ in range(4):
        if not worker.step():
            break
    with store.connection() as c:
        original = workflow.knowledge(c, a)["sections"]["news"][0]
    b = new_event(store, "海岚市域线验收文件研究")["id"]
    real_call = runner.call
    planned = []

    def reuse(role, ctx):
        if role == "plan":
            pick = next(x for x in ctx["world_candidates"] if x["entry_id"] == original["world_id"])
            planned.append(pick)
            refs = [
                KnowledgeLink(
                    entry_id=pick["entry_id"], version_id=pick["version_id"], reason="作为验收文件的已核实背景"
                ),
                KnowledgeLink(entry_id=999, version_id=999, reason="未展示的对象"),
            ]
            return Plan(rationale="复用已核实背景", tasks=[], use_knowledge=refs), (10, 5)
        return real_call(role, ctx)

    monkeypatch.setattr(runner, "call", reuse)
    monkeypatch.setattr(watch, "choose", lambda *a, **kw: [])
    for _ in range(3):
        if not worker.step():
            break
    with store.connection() as c:
        result = workflow.knowledge(c, b)
        assert planned and result["sections"]["news"][0]["id"] == original["id"]
        assert result["sections"]["news"][0]["index_reason"] == "作为验收文件的已核实背景"
        assert not any(n["kind"] == "fact" for n in result["sections"]["timeline"])
        assert one(c, "SELECT count(*) AS n FROM claims")["n"] == 1
        assert one(c, "SELECT analyzed,intake FROM event_docs WHERE event_id=?", (b,)) == {
            "analyzed": 1,
            "intake": "backfill",
        }


def test_waiting_world_candidates_are_replanned_when_the_publisher_returns(tmp_path, clock, monkeypatch):
    store, source, runner, worker = setup(tmp_path, ["news"])
    new_event(store)
    for _ in range(4):
        if not worker.step():
            break
    waiting = new_event(store, "海岚市域线后续验收")
    with monkeypatch.context() as transient:
        call = runner.call
        transient.setattr(watch, "choose", lambda *a, **k: [])
        transient.setattr(
            runner,
            "call",
            lambda role, ctx: (Plan(rationale="尚需对应的新公告", tasks=[]), (7, 3))
            if role == "plan"
            else call(role, ctx),
        )
        assert worker.step()
        with store.connection() as c:
            assert require(c, "tracking_rounds", waiting["round"])["state"] == "waiting"
            assert not workflow.knowledge(c, waiting["id"])["sections"]["index"]
    with TestClient(
        make_app(store, runner, background=False, acquisition=source),
        base_url="http://127.0.0.1",
        headers=HEADERS,
    ) as api:
        assert api.post(f"/api/events/{waiting['id']}/rounds/{waiting['round']}/retry").is_success
    for _ in range(4):
        if not worker.step():
            break
    with store.connection() as c:
        r = require(c, "tracking_rounds", waiting["round"])
        assert (r["state"], r["world_seed_only"]) == ("published", 0)
        assert workflow.knowledge(c, waiting["id"])["sections"]["news"]


def test_move_reference_preserves_world_history_and_demo_isolation(tmp_path, clock):
    store, source, runner, worker = setup(tmp_path, ["news"])
    a = new_event(store)["id"]
    for _ in range(4):
        if not worker.step():
            break
    with store.connection(True) as c:
        b = db.add_event(c, "第二个追踪对象", "用于比较背景", [], status="paused")
        fake = db.add_event(c, "演示索引", "虚构数据", [], demo=True, status="paused")
        first = workflow.knowledge(c, a)["sections"]["news"][0]
        old = first["id"]
        entry = first["world_id"]
        ref = first["citations"][0]
        other = workflow.reference_world(c, b, entry, old, 0, "作为独立研究的背景", "background")
        assert workflow.knowledge(c, b)["sections"]["background"][0]["index_reason"] == "作为独立研究的背景"
        removed = workflow.remove_reference(c, b, entry, other["snapshot"], "当前目标已经转向")
        assert not workflow.knowledge(c, b)["sections"]["background"]
        assert workflow.knowledge(c, b, revision=other["snapshot"])["sections"]["background"][0]["id"] == old
        assert {e["id"] for e in world.detail(c, entry)["events"]} == {a}
        with pytest.raises(workflow.VersionConflict):
            workflow.reference_world(c, b, entry, old, other["snapshot"], "重新引用背景")
        workflow.reference_world(c, b, entry, old, removed["snapshot"], "重新引用背景")
        data = materials.fragments_for(c, [ref["document_id"]])
        fragments = {f["id"]: f for f in data}
        make_person = lambda identity: Finding(
            section="people",
            kind="view",
            subject="林晴",
            text="公开发言。",
            entity_kind="person",
            identity=identity,
            citations=[Citation(fragment_id=ref["fragment_id"], quote=ref["quote"])],
        )
        x, _ = research.claim(c, a, "people", make_person("第一机构发言人"), fragments)
        y, _ = research.claim(c, b, "people", make_person("第二机构发言人"), fragments)
        z, _ = research.claim(c, fake, "people", make_person("第一机构发言人"), fragments)
        assert len({world.version(c, i)["entry_id"] for i in (x, y, z)}) == 3
        assert world.browse(c, q="林晴", category="people")["total"] == 2
        assert not world.detail(c, world.version(c, x)["entry_id"])["related_entries"]
        more = make_person("第一机构发言人")
        more.aliases = ["海岚新闻发言人"]
        same, _ = research.claim(c, a, "people", more, fragments)
        assert same == x
        alias_event = db.add_event(c, "海岚新闻发言人", "公开表态", [], status="paused")
        assert world.browse(c, q="海岚新闻发言人")["total"] == 1
        assert world.version(c, x)["entry_id"] in {i["entry_id"] for i in world.suggested(c, alias_event)}
        with pytest.raises(KeyError):
            workflow.reference_world(c, b, world.version(c, z)["entry_id"], z, 0, "来自演示域")


def test_world_adds_independent_evidence_and_keeps_previous_citations(tmp_path, clock):
    store, source, runner, worker = setup(tmp_path, ["news"])
    a = new_event(store)["id"]
    for _ in range(4):
        if not worker.step():
            break
    with store.connection(True) as c:
        b = db.add_event(c, "独立机构跟踪", "单独核对验收", [], status="paused")
        original = workflow.knowledge(c, a)["sections"]["news"][0]
        old, entry = original["id"], original["world_id"]
        before = workflow.latest(c, a)["id"]
        text = "第二发布方也记录了同一验收决定。"
        url = "https://secondary.example.org/notice"
        doc = feeds.ingest(
            c, b, url, {"url": url, "title": "独立发布的核实稿", "summary": text, "published_at": clock.utc()}
        )
        materials.summary_fragment(c, require(c, "documents", doc))
        parts = materials.fragments_for(c, [doc])
        f = Finding(
            section="news",
            kind="fact",
            subject=original["subject"],
            text=original["text"],
            supports=old,
            citations=[Citation(fragment_id=parts[0]["id"], quote=text)],
        )
        ctx = {
            "prior": [],
            "world_candidates": [
                {
                    "entry_id": entry,
                    "version_id": old,
                    "subject": original["subject"],
                    "text": original["text"],
                }
            ],
        }
        research.check_report(Report(findings=[f]), parts, ctx, "news")
        new, is_new = research.claim(c, b, "news", f, {p["id"]: p for p in parts})
        assert is_new and world.version(c, new)["entry_id"] == entry
        workflow.reference_world(c, b, entry, new, 0, "核对独立证据")
        workflow.refresh_shared_indexes(c, b, [new])
        assert len(world.detail(c, entry)["claim"]["citations"]) == len(original["citations"]) + 1
        assert world.detail(c, entry, old)["claim"]["citations"] == original["citations"]
        assert workflow.knowledge(c, a, revision=before)["sections"]["news"][0]["id"] == old
        assert workflow.knowledge(c, a)["sections"]["news"][0]["id"] == new
        assert research.claim(c, b, "news", f, {p["id"]: p for p in parts}) == (new, False)
        f.text = "一项不同的决定"
        with pytest.raises(ValueError):
            research.check_report(Report(findings=[f]), parts, ctx, "news")


def test_legacy_duplicate_ids_resolve_to_one_world_entry_after_restarting(tmp_path, clock):
    assert world.classify("news", "news", "fact", "", "", "居民消费价格月度数据") == "indicators"
    assert world.classify("news", "news", "reported", "", "", "新的保障条例") == "policies"
    store, _, _, worker = setup(tmp_path, ["news"])
    a, b = new_event(store)["id"], new_event(store, "海岚市域线复核")["id"]
    for _ in range(8):
        if not worker.step():
            break
    with store.connection(True) as c:
        claim = workflow.knowledge(c, a)["sections"]["news"][0]
        sid = workflow.latest(c, b)["id"]
        columns = [
            r["name"] for r in c.execute("PRAGMA table_info(claims)") if r["name"] not in ("id", "event_id")
        ]
        fields = ",".join(columns)
        # Reproduce the earlier writer: separate claim IDs attributable to different events.
        dup = c.execute(
            f"INSERT INTO claims(event_id,{fields}) SELECT ?,{fields} FROM claims WHERE id=?",
            (b, claim["id"]),
        ).lastrowid
        c.execute(
            "INSERT INTO claim_details SELECT ?,facets,aliases FROM claim_details WHERE claim_id=?",
            (dup, claim["id"]),
        )
        c.execute(
            "UPDATE snapshot_claims SET claim_id=?,index_section='',index_reason='' WHERE snapshot_id=? AND claim_id=?",
            (dup, sid, claim["id"]),
        )
    for _ in range(2):
        reopened = Store(tmp_path)
        with reopened.connection() as c:
            old = workflow.knowledge(c, b, revision=sid)["sections"]["news"][0]
            assert (old["id"], old["world_id"]) == (dup, claim["world_id"])
            assert world.detail(c, claim["world_id"], dup)["claim"]["citations"] == claim["citations"]
            assert world.browse(c)["total"] == 1 and len(world.detail(c, claim["world_id"])["history"]) == 1
            assert world.version(c, dup)["canonical_id"] == claim["id"]
            assert c.execute("PRAGMA integrity_check").fetchone()[0] == "ok" and not list(
                c.execute("PRAGMA foreign_key_check")
            )


def test_planner_can_release_a_reference_and_must_preserve_user_pin(tmp_path, clock, monkeypatch):
    store, _, runner, worker = setup(tmp_path, ["news"])
    a, b = new_event(store)["id"], new_event(store, "海岚市域线运营研究")["id"]
    for _ in range(8):
        if not worker.step():
            break
    with store.connection(True) as c:
        fact = workflow.knowledge(c, a)["sections"]["news"][0]
        version = workflow.latest(c, b)["id"]
        pin = (b, fact["world_id"], fact["id"])
        new = workflow.reference_world(c, *pin, version, "作为长期对照组", "background")["snapshot"]
        assert workflow.reference_world(c, *pin, new, "作为长期对照组", "background")["snapshot"] == new
        for event in (a, b):
            workflow.edit_goal(c, event, 1, scope="只研究运营成本和定价")
    original = runner.call

    def drop(role, ctx):
        if role == "plan":
            pick = ctx["prior"][0]
            return Plan(
                rationale="重评目标相关性",
                tasks=[],
                release_knowledge=[
                    KnowledgeLink(entry_id=pick["world_id"], version_id=pick["id"], reason="验收公告没有票价信息")
                ],
            ), (9, 3)
        return original(role, ctx)

    monkeypatch.setattr(runner, "call", drop)
    for _ in range(8):
        if not worker.step():
            break
    with store.connection() as c:
        assert not workflow.knowledge(c, a)["sections"]["index"]
        assert workflow.knowledge(c, b)["sections"]["background"][0]["index_reason"] == "作为长期对照组"
        assert {e["id"] for e in world.detail(c, fact["world_id"])["events"]} == {b}


def test_user_correction_is_versioned_and_cannot_be_revived_by_replay(tmp_path, clock):
    store, source, runner, worker = setup(tmp_path, ["news"])
    eid = new_event(store)["id"]
    assert worker.step()
    api = TestClient(
        make_app(store, runner, background=False, acquisition=source),
        base_url="http://127.0.0.1",
        headers=HEADERS,
    )
    path = f"/api/events/{eid}/knowledge"
    first = api.get(path).json()
    old = first["sections"]["news"][0]
    ref = old["citations"][0]
    endpoint = path + f"/claims/{old['id']}/corrections"
    edit = {
        "expected_snapshot": first["snapshot"],
        "text": "本次公布了验收方案。",
        "reason": "按公告说明区分验收与载客",
        "fragment_id": ref["fragment_id"],
        "quote": ref["quote"],
    }
    assert api.post(endpoint, json={**edit, "quote": "原文没有这句话"}).status_code == 422
    result = api.post(endpoint, json=edit)
    assert result.status_code == 201
    assert api.post(endpoint, json=edit).status_code == 409
    value = api.get(path, params={"compare": first["snapshot"]}).json()
    assert value["diff"] == {"added": [result.json()["claim"]], "removed": [old["id"]]}
    assert value["sections"]["news"][0]["role"] == "user"
    for _ in range(5):
        worker.step()
    assert old["id"] not in {i["id"] for i in api.get(path).json()["sections"]["news"]}
    assert (
        api.get(path, params={"revision": first["snapshot"]}).json()["sections"]["news"][0]["text"]
        == old["text"]
    )
    api.close()


@pytest.mark.parametrize(
    ("proposed", "quote", "expected"),
    [
        ("2026-11-25T08:30:00Z", BASE, "2026-11-25T08:30:00Z"),
        ("2026-11-25T08:30:00Z", "北京时间2026年11月25日16时30分公布", "2026-11-25T08:30:00Z"),
        ("2026-11-25T08:30:00Z", "2026-11-25 08:30 公布", "2026-11-25"),
        ("2026-11-25T09:30:00Z", BASE, "2026-11-25"),
        ("2026-11-26T08:30:00Z", BASE, ""),
        ("2026-11-25", "近日公布", ""),
        ("2026-11-25T08:30:00Z", "November 25, 2026 at 8:30 AM UTC", "2026-11-25T08:30:00Z"),
    ],
)
def test_occurrence_precision_requires_source_date_clock_and_zone(proposed, quote, expected):
    assert research.supported_time(proposed, quote) == expected


def test_missing_source_recovers_its_original_scheduled_window(tmp_path, clock):
    store, source, runner, worker = setup(tmp_path, ["news"])
    eid = new_event(store)["id"]
    for _ in range(3):
        worker.step()
    source.down = True
    clock.dt += timedelta(hours=6)
    for _ in range(3):
        worker.step()
    with store.connection() as c:
        gap = one(c, "SELECT * FROM round_sources WHERE state='missing'")
        assert gap and gap["window_start"] == "2026-11-25T09:00:00Z" and gap["window_end"] == clock.utc()
    source.down = False
    clock.dt += timedelta(minutes=20)
    worker.watcher.cycle()
    for _ in range(6):
        worker.step()
    clock.dt += timedelta(hours=6)
    for _ in range(6):
        worker.step()
    with store.connection() as c:
        gap = one(c, "SELECT * FROM round_sources WHERE round_id=?", (gap["round_id"],))
        assert gap["state"] == "recovered"
        repair = require(c, "tracking_rounds", gap["recovery_round"])
        assert (repair["window_start"], repair["window_end"]) == (gap["window_start"], gap["window_end"])
        assert len(rows(c, "SELECT id FROM tracking_rounds WHERE request_key LIKE '%/slot/%'")) == 2
        assert (
            c.execute("SELECT count(*) FROM nodes WHERE event_id=? AND kind='fact'", (eid,)).fetchone()[0]
            == 1
        )


@pytest.mark.parametrize(("new_material", "old_repair"), [(False, False), (True, False), (False, True)])
def test_unwatched_source_replays_verified_read_after_second_outage(tmp_path, clock, new_material, old_repair):
    store, source, runner, worker = setup(tmp_path, ["news"])
    eid = new_event(store)["id"]
    for _ in range(3):
        worker.step()
    with store.connection(True) as c:
        c.execute("UPDATE event_platforms SET watching=0,pinned=1 WHERE event_id=? AND url=?", (eid, FEED))
    source.down = True
    clock.dt += timedelta(hours=6)
    for _ in range(3):
        worker.step()
    with store.connection(True) as c:
        first = one(c, "SELECT * FROM round_sources WHERE url=? AND state='missing'", (FEED,))
        assert first
        if old_repair:
            # A previous release could queue a repair, then encounter another outage before it ran.
            first_state = require(c, "tracking_rounds", first["round_id"])["state"]
            c.execute("UPDATE tracking_rounds SET state='researching' WHERE id=?", (first["round_id"],))
            prior, job = workflow.request_round(
                c, eid, "补采缺失窗口", start=first["window_start"], end=first["window_end"], key="old-repair"
            )
            assert prior != first["round_id"]
            c.execute("UPDATE tracking_rounds SET state=? WHERE id=?", (first_state, first["round_id"]))
            c.execute("UPDATE round_sources SET recovery_round=? WHERE round_id=?", (prior, first["round_id"]))
            c.execute(
                """INSERT INTO round_sources(round_id,url,window_start,window_end,state,checked_at)
                   VALUES(?,?,?,?,'missing',?)""",
                (prior, FEED, first["window_start"], first["window_end"], clock.utc()),
            )
            c.execute("UPDATE tracking_rounds SET state='waiting' WHERE id=?", (prior,))
            c.execute("UPDATE jobs SET status='done' WHERE id=?", (job,))
    if new_material:
        source.articles.append(("revision", "海岚市域线复检安排", BASE, "2026-11-25T14:30:00Z"))
    source.articles.append(("later", "海岚市域线后续安排", BASE, "2026-11-25T15:10:00Z"))
    source.down = False
    clock.dt += timedelta(minutes=20)
    worker.watcher.cycle()
    success_at = clock.utc()
    source.down = True
    clock.dt += timedelta(minutes=20)
    for _ in range(6):
        worker.step()
    with store.connection() as c:
        restored = one(c, "SELECT * FROM round_sources WHERE round_id=? AND url=?", (first["round_id"], FEED))
        assert restored["state"] == "recovered"
        rid = restored["recovery_round"]
        if old_repair:
            assert require(c, "tracking_rounds", prior)["state"] == "recovered"
            prior_coverage = one(c, "SELECT recovery_round FROM round_sources WHERE round_id=?", (prior,))
            assert prior_coverage["recovery_round"] == rid
        receipt = one(c, "SELECT * FROM round_sources WHERE round_id=? AND url=?", (rid, FEED))
        assert receipt["checked_at"] == success_at and receipt["state"] == "covered"
        repair = require(c, "tracking_rounds", rid)
        assert repair["state"] == ("published" if new_material else "unchanged")
        late = one(c, "SELECT id FROM documents WHERE url=?", (HOST + "/later",))
        assert late["id"] not in json.loads(repair["input_docs"] or "[]")
        if new_material:
            in_window = one(c, "SELECT id FROM documents WHERE url=?", (HOST + "/revision",))
            assert json.loads(repair["input_docs"]) == [in_window["id"]]
        else:
            assert not one(c, "SELECT 1 FROM research_tasks WHERE round_id=?", (rid,))


@pytest.mark.parametrize("point", ["acquisition", "model_receipt", "before_commit", "after_commit"])
def test_process_loss_replays_a_single_publication(tmp_path, clock, monkeypatch, point):
    class LostProcess(BaseException):
        pass

    store, source, runner, worker = setup(tmp_path, ["news"])
    eid = new_event(store)["id"]
    owner, name = {
        "acquisition": (research, "acquire"),
        "model_receipt": (worker, "call"),
        "before_commit": (research, "publish"),
        "after_commit": (worker, "process"),
    }[point]
    original = getattr(owner, name)

    def interrupted(*args, **kwargs):
        original(*args, **kwargs)
        raise LostProcess()

    with monkeypatch.context() as patch:
        patch.setattr(owner, name, interrupted)
        with pytest.raises(LostProcess):
            worker.step()
    clock.dt += timedelta(seconds=95)
    restored = Worker(Store(store.directory), runner, acquisition=source)
    for _ in range(4):
        restored.step()
    with store.connection() as c:
        assert (
            c.execute("SELECT count(*) FROM knowledge_snapshots WHERE event_id=?", (eid,)).fetchone()[0] == 1
        )
        assert (
            c.execute("SELECT count(*) FROM nodes WHERE event_id=? AND kind='fact'", (eid,)).fetchone()[0]
            == 1
        )
        assert one(c, "SELECT 1 FROM fragments") and not one(c, "SELECT 1 FROM jobs WHERE status='running'")
    assert runner.calls["plan"] == 1 and runner.calls["news"] == 1


def test_search_fulltext_attaches_without_synthetic_rss_correction(tmp_path, clock):
    store, source, runner, worker = setup(tmp_path, ["news"])
    eid = new_event(store)["id"]
    worker.step()
    res = source.read(HOST + "/notice")
    page = materials.article(res)
    with store.connection(True) as c:
        before = c.execute("SELECT count(*) FROM documents").fetchone()[0]
        original = one(c, "SELECT id FROM documents WHERE url=?", (res.url,))
        key = materials.attach_search_result(
            c,
            eid,
            {"url": res.url, "published_at": page.published_at, "title": page.title, "summary": "正文内容比订阅长"},
            res,
            page,
            "backfill",
        )
        assert key == original["id"] and c.execute("SELECT count(*) FROM documents").fetchone()[0] == before
        assert not one(c, "SELECT id FROM nodes WHERE kind='correction'")
    contaminated = feeds.Resource(
        '<html><head><title>某单位公告</title></head><body><h1><script>document.write("公告")</script></h1><main><p>正文</p></main></body></html>'.encode(),
        HOST + "/a",
        "text/html",
    )
    assert materials.article(contaminated).title == "某单位公告"


def test_unrelated_evidence_does_not_lock_other_registered_predictions(tmp_path, clock):
    store, source, runner, worker = setup(tmp_path, ["news"])
    eid = new_event(store)["id"]
    worker.step()
    with store.connection(True) as c:
        doc = one(c, "SELECT id FROM documents")["id"]
        train = forecast.new(
            c,
            eid,
            "市域线能否通过验收",
            "2026-12-30T00:00:00Z",
            "主管单位确认开放",
            "https://rail.example.org",
            65,
            "原始方案",
            [doc],
            "测试",
        )
        money = forecast.new(
            c,
            eid,
            "利率是否下调",
            "2026-12-30T00:00:00Z",
            "央行下调贷款利率",
            "https://bank.example.org",
            38,
            "固定期限",
            [doc],
            "测试",
        )
        assert workflow.affected_predictions(c, eid, [doc]) == [train]
        rid, _ = workflow.request_round(c, eid, "检验定向闸门")
        c.execute("INSERT INTO round_prediction_gates VALUES(?,?,'checking')", (rid, train))
        with pytest.raises(ValueError):
            forecast.revise(c, train, 90, "等待核查", [doc], "测试")
        assert forecast.revise(c, money, 40, "只改变贷款假设", [doc], "测试")


def test_candidate_lesson_can_be_challenged_using_another_cases_fragment(tmp_path, clock):
    store, source, runner, worker = setup(tmp_path, ["news"])
    a = new_event(store)["id"]
    b = new_event(store, "海岚市域线运营监管")["id"]
    for _ in range(5):
        worker.step()
    with store.connection(True) as c:
        initial = one(c, "SELECT id FROM documents ORDER BY id")["id"]
        workflow.observe(c, a, "待验证经验", "计划公告和实际运营分开判断", "另一主管单位正式发布独立材料", refs=[initial])
        lesson = one(c, "SELECT id FROM observations WHERE origin='待验证经验'")["id"]
        assert lesson in {r["id"] for r in workflow.applicable_lessons(c, b)}
        frag = one(c, "SELECT id FROM fragments ORDER BY id LIMIT 1")["id"]
        with pytest.raises(ValueError):
            workflow.review_lesson(c, a, lesson, frag, "support", "同一案例不能自证")
        with pytest.raises(ValueError):
            workflow.review_lesson(c, b, lesson, frag, "support", "原文转载")
        independent = feeds.ingest(
            c,
            b,
            HOST + "/independent.xml",
            {
                "title": "另一案例的验收与运营公告",
                "summary": "另一线路同日通过验收并参加运营",
                "url": HOST + "/independent",
                "published_at": clock.utc(),
            },
        )
        asset = materials.summary_fragment(c, require(c, "documents", independent))
        new_fragment = one(c, "SELECT id FROM fragments WHERE asset_id=?", (asset,))["id"]
        assert workflow.review_lesson(c, b, lesson, new_fragment, "counter", "另一线路同日通过验收并参加运营")
        assert workflow.applicable_lessons(c, b)[0]["reviews"][0]["outcome"] == "counter"
        assert workflow.review_lesson(c, a, lesson, frag, "withdraw", "经复核撤回广义推断")
        assert workflow.applicable_lessons(c, b)[0]["state"] == "withdrawn"


def test_numeric_pdf_pages_and_unavailable_media_keep_a_locator(monkeypatch):
    import io
    from pypdf import PdfWriter
    from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject
    from evosight import media

    writer = PdfWriter()
    font = DictionaryObject(
        {
            NameObject("/Type"): NameObject("/Font"),
            NameObject("/Subtype"): NameObject("/Type1"),
            NameObject("/BaseFont"): NameObject("/Helvetica"),
        }
    )
    for label in (b"Plan submitted", b"Decision issued"):
        page = writer.add_blank_page(250, 180)
        page[NameObject("/Resources")] = DictionaryObject(
            {NameObject("/Font"): DictionaryObject({NameObject("/F1"): writer._add_object(font)})}
        )
        stream = DecodedStreamObject()
        stream.set_data(b"BT /F1 12 Tf 10 70 Td (" + label + b") Tj ET")
        page[NameObject("/Contents")] = writer._add_object(stream)
    output = io.BytesIO()
    writer.write(output)
    doc = materials.extract(feeds.Resource(output.getvalue(), HOST + "/decision.pdf", "application/pdf"))
    assert doc.parts[1] == ("第 2 页", "Decision issued", HOST + "/decision.pdf#page=2")
    monkeypatch.setattr(media, "tool", lambda name: None)
    absent = materials.extract(feeds.Resource(b"image placeholder", HOST + "/scan.png", "image/png"), "image")
    assert not absent.parts and "原件" in absent.detail


def test_media_recovers_keyframes_when_video_has_no_audio_and_labels_transcripts(monkeypatch):
    from pathlib import Path
    import subprocess
    from evosight import media

    monkeypatch.setattr(media, "tool", lambda name: name)
    monkeypatch.setenv("EVOSIGHT_WHISPER_MODEL", "multilingual.bin")
    monkeypatch.delenv("EVOSIGHT_ASR_LANG", raising=False)
    mode = "silent"
    frames = []

    def invoke(args, timeout=45):
        if args[0] == "whisper-cli":
            assert args[args.index("-l") + 1] == "auto"
            Path(args[-1]).with_suffix(".srt").write_text(
                "1\n00:00:01,000 --> 00:00:03,000\n发布公告\n", encoding="utf-8"
            )
        elif args[-1].endswith(".wav"):
            if mode == "silent":
                raise subprocess.CalledProcessError(1, args)
        elif args[-1].endswith(".png"):
            frames.append(args[args.index("-ss") + 1])
            if frames[-1] == "0":
                Path(args[-1]).touch()
        return b""

    def read_frame(path, url, prefix, suffix):
        return [(prefix + "图像区域 10,10,50,15", "公告", url + suffix)]

    monkeypatch.setattr(media, "run", invoke)
    monkeypatch.setattr(media, "ocr", read_frame)
    video = feeds.Resource(b"video fixture", HOST + "/notice.mp4", "video/mp4")
    result = materials.extract(video)
    assert result.parts == [("0 秒 图像区域 10,10,50,15", "公告", video.url + "#t=0")]
    assert frames == ["0", "15", "45"]
    audio = feeds.Resource(b"audio fixture", HOST + "/notice.wav", "audio/wav")
    frames.clear()
    assert not materials.extract(audio).parts and not frames
    mode = "speech"
    transcript = materials.extract(audio)
    assert transcript.parts[0][0].startswith("自动转写")
    assert transcript.parts[0][1:] == ("发布公告", audio.url + "#t=1,3")
    assert not frames


def test_closed_or_unanswered_proposal_preserves_event_and_pending_forecast(tmp_path, clock):
    store, source, runner, worker = setup(tmp_path, ["news"])
    eid = new_event(store)["id"]
    for _ in range(2):
        worker.step()
    with store.connection(True) as c:
        pid = one(c, "SELECT id FROM predictions")["id"]
        item = c.execute(
            "INSERT INTO end_proposals(event_id,reason,refs,goal_version,created_at) VALUES(?,'阶段目标达成','[]',1,?)",
            (eid, clock.utc()),
        ).lastrowid
        assert require(c, "events", eid)["status"] == "active"
        workflow.respond_end(c, eid, item, "continue")
        assert require(c, "events", eid)["status"] == "active"
        assert workflow.respond_end(c, eid, item, "continue")["status"] == "continue"
        proposal = c.execute(
            "INSERT INTO end_proposals(event_id,reason,refs,goal_version,created_at) VALUES(?,'主管单位已公布结果','[]',1,?)",
            (eid, clock.utc()),
        ).lastrowid
        snapshot = workflow.latest(c, eid)["id"]
        workflow.respond_end(c, eid, proposal, "archived")
    clock.dt += timedelta(days=2)
    with store.connection(True) as c:
        worker_module.schedule(c)
        assert not one(c, "SELECT 1 FROM jobs WHERE kind='collect' AND status='queued'")
        assert one(c, "SELECT 1 FROM jobs WHERE kind='verify' AND status='queued'")
        assert forecast.detail(c, pid)["status"] == "frozen"
        result = workflow.edit_goal(c, eid, 2, status="active")
        assert result["version"] == 3 and result["round"] and workflow.latest(c, eid)["id"] == snapshot


def test_planner_can_select_monitors_and_discover_a_missing_publisher(tmp_path, clock):
    store, source, runner, worker = setup(tmp_path, ["news"])
    eid = new_event(store)["id"]
    with store.connection() as c:
        r = one(c, "SELECT * FROM tracking_rounds WHERE event_id=?", (eid,))
    watch.choose(store, source, eid, r["id"])
    another = HOST + "/operations"

    def discover(title, scope, known, *, query=None, **options):
        assert query == "海岚市域线运营通知栏"
        return [materials.Source(another, "section", "运营通知栏", "可直接核查载客安排", "搜索发现")], [(query, "回访发布方确认栏目")]

    source.discover = discover
    plan = {"watch": [{"url": another, "reason": "运营企业在此先行公布运行安排"}], "source_queries": ["海岚市域线运营通知栏"]}
    with store.connection(True) as c:
        c.execute("UPDATE settings SET value='1' WHERE key='watch_sources'")
    watch.apply_plan(store, source, r, plan)
    watch.apply_plan(store, source, r, plan)
    with store.connection() as c:
        assert [
            (x["url"], x["reason"])
            for x in rows(c, "SELECT url,reason FROM event_platforms WHERE watching=1")
        ] == [(another, "运营企业在此先行公布运行安排")]
        assert one(c, "SELECT added FROM round_monitors WHERE round_id=?", (r["id"],))["added"] == 1
        assert c.execute("SELECT count(*) FROM discovery_log WHERE state='规划查源'").fetchone()[0] == 1


def test_four_slot_resource_scenario_with_controlled_clock(tmp_path, clock):
    import os
    from soak_workflow import ModelReplay, Publisher as SoakPublisher, TITLES, metrics

    start = clock.dt
    pub = SoakPublisher(int(start.timestamp()), False, now=lambda: clock.dt.timestamp())
    store = Store(tmp_path)
    w = Worker(store, ModelReplay(pub), acquisition=pub)
    events = []
    with store.connection(True) as c:
        for title in TITLES:
            eid = db.add_event(c, title, "追踪海岚市域线的验收、开放公告与实际载客", [])
            events.append(eid)
            workflow.begin_event(c, eid)

    def drain():
        for _ in range(30):
            if not w.step():
                return
        pytest.fail("同一时刻产生了循环任务")

    drain()
    with store.connection(True) as c:
        workflow.edit_goal(c, events[1], 1, status="archived")
    for seconds in sorted({900, *range(600, 86401, 600)}):
        clock.dt = start + timedelta(seconds=seconds)
        if seconds == 900:
            with store.connection(True) as c:
                workflow.edit_goal(c, events[0], 1, scope="核对海岚市域线正式载客时间与无障碍通道")
        w.watcher.cycle()
        drain()
    result = metrics(store, os.getpid())
    assert result["scheduled"] == 4 and result["recoveries"] >= 1
    assert result["unresolved_sources"] == 0 and result["unfinished_roles"] == 0 and result["backlog"] == 0
    assert (
        result["archived_settlements"] == 1
        and result["settlements"] == 2
        and result["duplicate_settlements"] == 0
    )
