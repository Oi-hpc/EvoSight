import asyncio
import json
from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest

from evosight import feeds, forecast, views
from evosight.agents import (
    Analysis,
    Discovery,
    DiscoveryBatch,
    Forecast,
    Runner,
    SourceScout,
    StudyItem,
    Verdict,
)
from evosight.api import make_app
from evosight.db import (
    Store,
    add_event,
    book_run,
    config,
    dump,
    enqueue,
    later,
    mark_read,
    node,
    one,
    require,
    rows,
    utc,
)
from evosight.worker import Worker, schedule
from evosight.contracts import Assignment, Citation, Finding, Plan, Report
from evosight.materials import Acquisition

SOURCE = "https://example.org/releases.xml"


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path)


def event(c, title="甲"):
    return add_event(c, title, "测试研究范围", [SOURCE])


def article(slug="news", title="公告", body="材料", date="2026-01-10T10:00:00Z"):
    return {"url": "https://example.org/" + slug, "title": title, "summary": body, "published_at": date}


def doc(c, e):
    return feeds.ingest(c, e, SOURCE, article())


def definition(c, e, ref, at="2026-01-01T12:00:00Z"):
    return forecast.new(
        c, e, "截止日前是否开放？", "2026-01-15T20:00:00Z", "指定来源宣告开放。", SOURCE, 60, "初始判断", [ref], "研究员", at=at
    )


def test_watermarks_two_events_concurrent_readers_and_backfill(store):
    with store.connection(True) as c:
        a, b = event(c), event(c, "乙")
        node(c, a, "fact", "第一份事实")
        old = views.card(c, require(c, "events", a))
        node(c, b, "fact", "其他事件")
        node(c, a, "fact", "第二份事实")
        newer = views.card(c, require(c, "events", a))
        node(c, a, "fact", "补录旧闻", occurred="2020-01-01T00:00:00Z")

    def read(token):
        with store.connection(True) as c:
            mark_read(c, a, token)

    with ThreadPoolExecutor(2) as pool:
        list(pool.map(read, [newer["token"], old["token"]]))
    with store.connection(True) as c:
        assert views.card(c, require(c, "events", a))["unread"] == 1
        assert views.card(c, require(c, "events", b))["unread"] == 1
        assert (
            views.timeline(c, a, after=old["upper"], through=newer["upper"])["nodes"][0]["title"] == "第二份事实"
        )
        with pytest.raises(ValueError):
            mark_read(c, b, old["token"])


def test_reprints_corrections_and_original_settlement_history(store):
    with store.connection(True) as c:
        a, b = event(c), event(c, "乙")
        ident = doc(c, a)
        for i in range(6):
            assert feeds.ingest(c, a, f"https://press{i}.example.org/feed", article(str(i))) == ident
        assert doc(c, b) == ident
        assert len(views.documents(c, a)) == 1
        assert len(views.documents(c, a)[0]["mentions"]) == 7
        p = definition(c, a, ident)
        forecast.settle(
            c, p, "yes", "2026-01-10T10:00:00Z", "公告确认", [ident], "经验一", "人工", at="2026-01-11T00:00:00Z"
        )
        new = feeds.ingest(c, b, SOURCE, article(body="更正此前公告"))
        assert new != ident and require(c, "documents", new)["supersedes"] == ident
        assert require(c, "predictions", p)["status"] == "review"
        assert forecast.metrics(c)["auto"]["resolved"] == 0
        assert views.documents(c, a)[0]["affected"]
        forecast.settle(
            c, p, "no", "2026-01-10T10:00:00Z", "依据修订公告改判", [new], "复核后经验", "人工", at="2026-01-16T00:00:00Z"
        )
        history = forecast.detail(c, p)["settlements"]
        assert [x["outcome"] for x in history] == ["yes", "no"]
        assert history[-1]["scores"]["final"]["brier"] == 0.36
        assert views.documents(c, a)[1]["summary"] == "材料"


def test_late_revisions_and_fixed_horizon_brier(store):
    with store.connection(True) as c:
        e = event(c)
        d = doc(c, e)
        p = definition(c, e, d)
        forecast.revise(c, p, 80, "公开前的修订", [d], "模型", at="2026-01-10T09:00:00Z")
        forecast.revise(c, p, 99, "尚未采到公告", [d], "模型", at="2026-01-10T11:00:00Z")
        s = forecast.settle(
            c, p, "yes", "2026-01-10T10:00:00Z", "14时采到10时公告", [d], "经验", "人工", at="2026-01-10T14:00:00Z"
        )
        assert [s[k]["brier"] for k in ("initial", "seven_days", "final")] == [0.16, 0.16, 0.04]
        assert forecast.detail(c, p)["versions"][-1]["late"]
        with pytest.raises(ValueError):
            forecast.revise(c, p, 100, "晚于裁定", [d], "模型")


def test_entirely_late_sample_and_conflicting_evidence(store):
    with store.connection(True) as c:
        e = event(c)
        d = doc(c, e)
        p = definition(c, e, d, at="2026-01-10T11:00:00Z")
        forecast.settle(c, p, "conflict", "", "两份公告冲突", [d], "", "人", at="2026-01-11T00:00:00Z")
        assert require(c, "predictions", p)["status"] == "review"
        s = forecast.settle(
            c, p, "no", "2026-01-10T10:00:00Z", "截止前已有决定性公告", [d], "", "人", at="2026-01-16T00:00:00Z"
        )
        assert s["late_only"] and s["final"] is None
        assert forecast.metrics(c)["auto"]["scored"] == 0


def test_default_call_limits_and_saved_overrides_survive_restart(store):
    with store.connection(True) as c:
        settings = config(c)
        assert (settings["daily_calls"], settings["monthly_calls"], settings["verify_reserve"]) == (
            1000,
            30000,
            2,
        )
        c.execute("UPDATE settings SET value='24' WHERE key='daily_calls'")
    reopened = Store(store.directory)
    with reopened.connection() as c:
        assert config(c)["daily_calls"] == 24


def test_budget_reserved_and_deadline_freezes_even_when_paused(store):
    with store.connection(True) as c:
        e = event(c)
        d = doc(c, e)
        p = definition(c, e, d)
        c.execute("UPDATE settings SET value='2' WHERE key IN ('daily_calls','monthly_calls')")
        c.execute("UPDATE settings SET value='1' WHERE key='verify_reserve'")
        book_run(c, None, "study", "kimi", "low")
        with pytest.raises(ValueError, match="额度"):
            book_run(c, None, "discover", "kimi", "low")
        book_run(c, None, "verify", "codex", "low")
        with pytest.raises(ValueError, match="额度"):
            book_run(c, None, "verify", "codex", "low")
        c.execute("UPDATE settings SET value='true' WHERE key='global_pause'")
        schedule(c)
        assert require(c, "predictions", p)["status"] == "frozen"
        assert not rows(c, "SELECT id FROM jobs")


def test_concurrent_reservations_obey_hard_limit(store):
    with store.connection(True) as c:
        c.execute("UPDATE settings SET value='2' WHERE key='daily_calls'")

    def attempt(_):
        try:
            with store.connection(True) as c:
                book_run(c, None, "verify", "x", "low")
            return True
        except ValueError:
            return False

    with ThreadPoolExecutor(6) as pool:
        assert sum(pool.map(attempt, range(8))) == 2


class Fake:
    mode = "test"

    def profile(self, role):
        return Runner("test").profile(role)

    def call(self, role, ctx):
        if role == "source_scout":
            return SourceScout(publisher_urls=[], queries=[]), (8, 3)
        if role == "plan":
            return Plan(
                rationale="核对新公告",
                tasks=[
                    Assignment(
                        role="news",
                        question="公告披露了什么？",
                        target="事件",
                        time_scope="window",
                        done_when="定位原始资料",
                        priority=4,
                    )
                ],
            ), (12, 8)
        if role == "news":
            f = ctx["fragments"][0]
            return Report(
                findings=[
                    Finding(
                        section="news",
                        kind="reported",
                        subject="官方公告",
                        text="材料披露 " + f["text"][:30],
                        citations=[Citation(fragment_id=f["id"], quote=f["text"][:24])],
                    )
                ]
            ), (12, 8)
        ids = [e["id"] for e in ctx["evidence"]]
        if role == "study":
            return Analysis(
                fact="公告确认进展",
                judgment="与上轮相比，等待验收",
                watch="下一份报告",
                evidence_ids=ids[:1],
                items=[StudyItem(name="验收排期", judgment="继续关注", watch="确认日期")],
                discoveries=[],
            ), (21, 8)
        if role == "discover":
            return DiscoveryBatch(
                discoveries=[
                    Discovery(title="授权事件一", scope="范围一", reason="理由"),
                    Discovery(title="授权事件二", scope="范围二", reason="理由"),
                ],
                evidence_ids=ids[:1],
            ), (19, 5)
        if role == "verify":
            return Verdict(outcome="pending", public_at="", reason="尚无结果", lesson="", evidence_ids=[]), (
                19,
                5,
            )
        priors = ctx["已有预测"]
        return Forecast(
            action="revise" if priors else "create",
            prediction_id=priors[0]["id"] if priors else 0,
            question="下一轮能验收吗？",
            deadline=later(40 * 86400),
            rule="官方确认",
            authority="官方公告",
            p=70 if priors else 60,
            reason="基于本轮进度",
            evidence_ids=ids[:1],
        ), (31, 14)


def fixture_worker(store, runner, fetcher):
    def offline(*args, **kwargs):
        raise ValueError("回放材料仅提供订阅原文")

    acq = Acquisition(download=offline, search=lambda *_: [])
    acq.poll = lambda source, cached=None: feeds.FeedPage(fetcher(source.url), source.url)
    return Worker(store, runner, fetcher, acquisition=acq)


def test_two_collection_cycles_and_crash_replay(store):
    entries = [article()]
    worker = fixture_worker(store, Fake(), lambda _: entries)
    with store.connection(True) as c:
        ident = event(c)
    assert worker.step() and worker.step()
    with store.connection(True) as c:
        first_job = one(c, "SELECT * FROM jobs WHERE kind='forecast'")
        c.execute("UPDATE jobs SET status='queued' WHERE id=?", (first_job["id"],))
        assert len(views.event_details(c, ident)["predictions"][0]["versions"]) == 1
    assert worker.step()
    entries.append(article("release", "第二份", "显著新变化", date=utc()))
    with store.connection(True) as c:
        enqueue(c, "collect", event=ident)
    for _ in range(3):
        assert worker.step()
    with store.connection(True) as c:
        detail = views.event_details(c, ident)
        assert len(detail["items"]) == 1
        assert [r["p"] for r in detail["predictions"][0]["versions"]] == [60, 70]
        assert len(detail["documents"]) == 2
        enqueue(c, "collect", event=ident)
    assert worker.step()
    with store.connection() as c:
        assert len(rows(c, "SELECT id FROM runs WHERE role='study'")) == 2
        assert views.card(c, require(c, "events", ident))["coverage"] == "已检查"


def test_topic_from_zero_opt_in_quota_and_no_duplicate_init(store):
    worker = fixture_worker(store, Fake(), lambda _: [article()])
    with store.connection(True) as c:
        t = c.execute(
            "INSERT INTO topics(name,feeds,keywords,next_run) VALUES(?,?,?,?)",
            ("主题", dump([SOURCE]), dump(["授权"]), utc()),
        ).lastrowid
        c.execute("UPDATE settings SET value='1' WHERE key='auto_limit'")
        schedule(c)
        assert not rows(c, "SELECT id FROM jobs")
        c.execute("UPDATE topics SET discovery=1 WHERE id=?", (t,))
        schedule(c)
        schedule(c)
        assert len(rows(c, "SELECT id FROM jobs")) == 1
    assert worker.step()
    with store.connection(True) as c:
        assert [r["state"] for r in rows(c, "SELECT state FROM candidates ORDER BY id")] == [
            "accepted",
            "review",
        ]
        schedule(c)
        assert len(rows(c, "SELECT id FROM jobs WHERE kind='collect'")) == 1


def test_source_failure_partial_and_pending_states(store):
    with store.connection(True) as c:
        a = event(c)
        assert views.card(c, require(c, "events", a))["coverage"] == "尚待首次采集"
        c.execute(
            "INSERT INTO source_state(event_id,feed) VALUES(?,?)", (a, "https://broken.example.org/feed")
        )

    def mixed(s):
        if s == SOURCE:
            return []
        raise ValueError("HTTP 503")

    worker = fixture_worker(store, Fake(), mixed)
    assert worker.step()
    with store.connection() as c:
        assert views.card(c, require(c, "events", a))["coverage"] == "部分来源未能核查"
        assert rows(c, "SELECT role,status FROM runs") == [{"role": "source_scout", "status": "done"}]
        assert not rows(c, "SELECT id FROM research_tasks")


def test_safe_public_feed_parsing_and_profiles():
    xml = b"<rss><channel><item><title>New</title><link>https://example.org/a?utm_source=1</link><pubDate>Wed, 25 Nov 2026 09:00:00 GMT</pubDate><description>&lt;b&gt;text&lt;/b&gt;</description></item></channel></rss>"
    assert feeds.parse_feed(xml, SOURCE) == [article("a", "New", "text", "2026-11-25T09:00:00Z")]
    for u in (
        "file:///a",
        "http://example.org",
        "https://127.0.0.1/feed",
        "https://user:secret@example.org/rss",
        "https://example.org:444/rss",
    ):
        with pytest.raises(ValueError):
            feeds.public_url(u)
    assert Runner("test").profile("forecast") == ("gpt-6.2-sol", "low")
    assert Runner().profile("forecast") == ("gpt-6.2-astra", "max")
    assert Runner().profile("study") == ("gpt-6.2-astra", "max")
    assert Runner().profile("discuss_dsh") == ("dashscope/kimi-k3.1-pro", "max")


def test_http_entry_points_local_origin_and_fixed_sample(store):
    app = make_app(store, Fake(), background=False)

    async def case():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1"
        ) as client:
            data = {"title": "HTTP 事件", "scope": "研究范围", "feeds": [SOURCE]}
            assert (await client.post("/api/events", json=data)).status_code == 403
            headers = {"X-EvoSight": "local"}
            assert (
                await client.post(
                    "/api/events", json=data, headers={**headers, "Origin": "https://foreign.example.org"}
                )
            ).status_code == 403
            r = await client.post("/api/events", json=data, headers=headers)
            assert r.status_code == 201
            ident = r.json()["id"]
            fixed = {"question": "在约定窗口内发布吗？", "rule": "公开宣布", "authority": "发行方", "deadline": later(7200)}
            assert (
                await client.post(f"/api/events/{ident}/fixed", json=fixed, headers=headers)
            ).status_code == 201
            assert (
                await client.post(f"/api/events/{ident}/fixed", json=fixed, headers=headers)
            ).status_code == 422
            assert (await client.get("/api/predictions")).json()["fixed"][0]["question"] == fixed["question"]
            home = (await client.get("/api/home")).json()
            assert home["stats"]["events"] == 1
            assert (
                await client.post(
                    f"/api/events/{ident}/read", json={"token": home["cards"][0]["token"]}, headers=headers
                )
            ).status_code == 200
            assert (await client.get("/api/operations")).json()["profiles"]["study"][1] == "low"

    asyncio.run(case())


def test_budget_deferred_work_resumes_and_invalid_proposal_is_atomic(store):
    class Invalid(Fake):
        def call(self, role, ctx):
            proposal, tokens = super().call(role, ctx)
            if role == "study":
                proposal.evidence_ids = [99999]
            return proposal, tokens

    with store.connection(True) as c:
        e = event(c)
        c.execute("UPDATE settings SET value='2' WHERE key='daily_calls'")
    worker = fixture_worker(store, Invalid(), lambda _: [article()])
    assert worker.step()
    with store.connection(True) as c:
        assert one(c, "SELECT status FROM jobs")["status"] == "deferred"
        assert not rows(c, "SELECT id FROM runs")
        assert len(views.documents(c, e)) == 1
        c.execute("UPDATE settings SET value='6' WHERE key='daily_calls'")
    assert worker.step()
    with store.connection(True) as c:
        assert not rows(c, "SELECT id FROM nodes")
        assert one(c, "SELECT analyzed FROM event_docs")["analyzed"] == 0
        j = one(c, "SELECT id,status FROM jobs")
        if j["status"] == "failed":
            enqueue(c, "collect", event=e)
        else:
            c.execute("UPDATE jobs SET retry_at=NULL WHERE id=?", (j["id"],))
    worker.runner = Fake()
    assert worker.step()
    with store.connection() as c:
        assert one(c, "SELECT analyzed FROM event_docs")["analyzed"] == 1
        assert one(c, "SELECT id FROM nodes WHERE kind='judgment'")


def test_expired_fixed_question_is_counted_and_unverifiable_grace(store):
    with store.connection(True) as c:
        e = event(c)
        d = doc(c, e)
        c.execute(
            "INSERT INTO fixed_questions(event_id,question,rule,authority,deadline,created_at) VALUES(?,?,?,?,?,?)",
            (e, "固定题", "公告确认", SOURCE, later(-10), later(-1000)),
        )
        schedule(c)
        m = forecast.metrics(c)["fixed"]
        assert m["coverage"] == 0 and m["due"] == 1 and m["submitted"] == 0
        assert one(c, "SELECT reason FROM fixed_questions")["reason"]
        p = definition(c, e, d)
        with pytest.raises(ValueError, match="宽限期"):
            forecast.settle(c, p, "unverifiable", "", "尚未收到公告", [], "", "人工", at="2026-01-16T00:00:00Z")
        forecast.settle(c, p, "unverifiable", "", "公告停止发布", [], "", "人工", at="2026-01-20T00:00:00Z")
        assert forecast.metrics(c)["auto"]["unverifiable"] == 1
