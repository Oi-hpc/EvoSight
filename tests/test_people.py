"""Public-person discovery, attribution, windowed monitoring and knowledge publication through the real worker."""
from datetime import timedelta
from html import escape
import json
from urllib.parse import parse_qs, urlsplit

from fastapi.testclient import TestClient
import pytest

from evosight import agents, db, feeds, materials, people, person_sources as social, workflow
from evosight.api import make_app
from evosight.contracts import Citation, Finding, PersonLead, Report
from evosight.worker import Worker
from test_workflow import BASE, FEED, HEADERS, HOST, TITLE, Publisher, Replay, clock, new_event

PERSON = "林晴"
IDENTITY = "海岚市域线验收负责人"
PROFILE_TEXT = "海岚市域线验收负责人林晴负责复核开放条件并公布验收进展。"
WEIBO = "https://weibo.com/1234567890/ABC987"
XPOST = "https://x.com/hailan_lq/status/123450001"
XHS = "https://www.xiaohongshu.com/explore/1234567890abcdef12345678"
ACCOUNTS = [
    "https://x.com/hailan_lq",
    "https://weibo.com/u/1234567890",
    "https://www.xiaohongshu.com/user/profile/abcdef0123456789abcd1234",
]


class PublicSources(Publisher):
    def __init__(self):
        super().__init__()
        self.when = "2026-11-25T08:41:00Z"
        self.mid = "ABC987"
        self.blocked = set()
        self.lookups = []
        self.official = HOST + "/people/lin"

    def search_hits(self, q, n):
        self.lookups.append(q)
        domains = {
            ".weibo.com": WEIBO,
            "site:weibo.com": "https://weibo.com/1234567890/" + self.mid,
            "site:x.com": XPOST,
            "site:xiaohongshu.com": XHS,
        }
        for domain, url in domains.items():
            if domain in q:
                return [("海岚市域线公开帖", url)]
        if PERSON in q:
            return [("海岚市域线采访", HOST + "/people/interview")]
        if "负责人" in q:
            return [("海岚市域线负责人", self.official)]
        return super().search_hits(q, n)

    def read(self, url, cached=None, **kw):
        if url in (self.official, HOST + "/people/interview"):
            named = {"@type": "Person", "name": PERSON, "sameAs": ACCOUNTS}
            date = "2025-01-02T01:00:00Z" if "interview" in url else "2026-11-25T08:00:00Z"
            return feeds.Resource(
                (
                    '<html><head><meta property="article:published_time" content="'
                    + date
                    + '">'
                    + '<script type="application/ld+json">'
                    + json.dumps(named)
                    + "</script></head><article><h1>海岚市域线负责人公开资料</h1>"
                    + "<p>"
                    + PROFILE_TEXT
                    + "</p><p>林晴在公开采访中说明海岚市域线须经独立验收后开放。</p></article></html>"
                ).encode(),
                url,
                "text/html",
            )
        if url in ACCOUNTS:
            name = social.location(url)[0]
            target = XHS if name == "xiaohongshu" else "https://weibo.com/1234567890/" + self.mid
            return feeds.Resource(
                ('<main><a href="' + target + '">公开帖</a></main>').encode(), url, "text/html"
            )
        if "publish.twitter.com/oembed?" in url:
            text = "林晴：海岚市域线验收将以正式公告为准。"
            x = {
                "url": XPOST,
                "author_url": ACCOUNTS[0],
                "author_name": PERSON,
                "html": f'<blockquote><p>{text}</p><time datetime="{self.when}"></time></blockquote>',
            }
            return feeds.Resource(json.dumps(x).encode(), url, "application/json")
        loc = social.location(url, post=True)
        if loc and loc[0] in ("weibo", "xiaohongshu"):
            if loc[0] in self.blocked:
                return feeds.Resource(b"<main>login</main>", "https://weibo.com/login", "text/html")
            author = ACCOUNTS[1 if loc[0] == "weibo" else 2]
            post = {
                "@type": "SocialMediaPosting",
                "datePublished": self.when,
                "headline": PERSON + "谈海岚市域线开放",
                "author": {"name": PERSON, "url": author},
                "articleBody": PERSON + "说明海岚市域线本轮验收的开放条件。",
            }
            return feeds.Resource(
                ('<script type="application/ld+json">' + json.dumps(post) + "</script>").encode(),
                url,
                "text/html",
            )
        return super().read(url, cached, **kw)


@pytest.fixture
def case(tmp_path, clock, monkeypatch):
    monkeypatch.delenv("EVOSIGHT_X_BEARER_TOKEN", raising=False)
    source = PublicSources()
    runner = Replay(["news"])
    normal = runner.call

    def call(role, ctx):
        if role == "plan":
            plan, t = normal(role, ctx)
            f = next((f for f in ctx["people_evidence"] if PROFILE_TEXT in f["text"]), None)
            if f and not ctx.get("people_watch"):
                lead = PersonLead(
                    name=PERSON,
                    identity=IDENTITY,
                    role="核心人物",
                    relevance="验收负责人对是否开放有直接公共职责",
                    citation=Citation(fragment_id=f["id"], quote=PROFILE_TEXT),
                )
                plan.person_leads = [lead, lead.model_copy(update={"name": "林青"})]
            return plan, t
        if role == "people":
            roster = ctx["people_watch"]
            assert roster
            frags = ctx["fragments"]
            own = {m["document_id"] for m in roster[0]["materials"] if m["attribution"] == "本人公开发帖"}
            f = next((f for f in frags if f["document_id"] in own and PERSON in f["text"]), None)
            if f:
                return Report(
                    findings=[
                        Finding(
                            section="people",
                            kind="view",
                            subject=PERSON,
                            identity=IDENTITY,
                            entity_kind="person",
                            text=f["text"],
                            citations=[Citation(fragment_id=f["id"], quote=f["text"])],
                            facets={"资料性质": "本人公开发帖", "公开立场": "开放须符合验收条件"},
                        )
                    ]
                ), (8, 5)
            return Report(gaps=["本轮缺少可归属于本人的公开表态"]), (8, 5)
        return normal(role, ctx)

    runner.call = call
    store = db.Store(tmp_path)
    with store.connection(True) as c:
        c.execute("UPDATE settings SET value='160' WHERE key='daily_calls'")
    worker = Worker(store, runner, acquisition=source)
    event = new_event(store)["id"]
    with TestClient(
        make_app(store, runner, background=False, acquisition=source),
        base_url="http://127.0.0.1",
        headers=HEADERS,
    ) as api:
        yield store, worker, api, event, source, clock


def test_discovery_all_channels_historical_and_typed_world_knowledge(case):
    store, w, api, e, source, clock = case
    assert w.step()
    base = f"/api/events/{e}/people"
    ls = api.get(base).json()["people"]
    assert [t["name"] for t in ls] == [PERSON] and ls[0]["role"] == "核心人物"
    t = ls[0]["id"]
    detail = api.get(base, params={"target": t}).json()
    assert {a["platform"] for a in detail["accounts"] if a["state"] == "confirmed"} == {
        "x",
        "weibo",
        "xiaohongshu",
    }
    voices = {m["channel"]: m["attribution"] for m in detail["documents"]}
    assert all(voices[k] == "本人公开发帖" for k in ("x", "weibo", "xiaohongshu"))
    assert "历史材料" in voices.values()
    assert {x["channel"] for x in detail["checks"]} == {"x", "weibo", "xiaohongshu", "history", "news"}
    assert any(k["facets"].get("资料性质") == "本人公开发帖" and k["world_id"] for k in detail["claims"])
    with store.connection() as c:
        rr = db.one(c, "select * from tracking_rounds where event_id=? order by id desc limit 1", (e,))
        assert "部分人物线索" in rr["plan"] and json.loads(rr["plan"])["person_leads"][0]["name"] == PERSON
        n = c.execute("select count(*) from person_materials").fetchone()[0]
        assert len(people.task_documents(w, rr)) > 0
        assert c.execute("select count(*) from person_materials").fetchone()[0] == n
    foreign = None
    with store.connection(True) as c:
        foreign = db.add_event(c, "无关事件", "另一研究目标", [], status="paused")
    assert api.get(f"/api/events/{foreign}/people?target={t}").status_code == 404
    a = detail["accounts"][0]
    assert (
        api.post(
            f"/api/events/{foreign}/people/{t}/accounts/{a['id']}", json={"confirmed": False}
        ).status_code
        == 404
    )
    assert api.post(base + f"/{t}/accounts/{a['id']}", json={"confirmed": False}).status_code == 200
    d = api.get(base, params={"target": t}).json()
    assert all(m["attribution"] == "账号归属待核" for m in d["documents"] if m["channel"] == a["platform"])


def test_six_hour_social_only_discovery_window_and_unavailable(case):
    store, w, api, e, source, clock = case
    assert w.step() and w.step()
    clock.dt += timedelta(hours=6)
    source.when = "2026-11-25T14:58:00Z"
    source.mid = "DEF999"
    source.blocked.add("xiaohongshu")
    assert w.step()
    with store.connection() as c:
        rr = db.one(c, "SELECT * FROM tracking_rounds WHERE event_id=? AND request_key LIKE '%/slot/%'", (e,))
        mats = db.rows(c, "SELECT * FROM person_materials WHERE round_id=?", (rr["id"],))
        assert any(
            m["channel"] == "weibo" and m["period"] == "window" and m["source_at"] == source.when
            for m in mats
        )
        assert not any(m["channel"] == "history" for m in mats)
        assert (
            db.one(
                c, "SELECT status FROM person_checks WHERE round_id=? AND channel='xiaohongshu'", (rr["id"],)
            )["status"]
            == "unavailable"
        )
        assert rr["state"] in ("published", "partial")
    assert any("after:" in q and "site:weibo.com" in q for q in source.lookups)
    assert w.step()
    clock.dt += timedelta(hours=6)
    source.when = "2026-11-25T15:00:00Z"
    source.mid = "BOUNDARY9"
    assert w.step()
    with store.connection() as c:
        rr = db.one(c, "SELECT * FROM tracking_rounds WHERE event_id=? ORDER BY id DESC LIMIT 1", (e,))
        assert not db.one(
            c, "SELECT 1 FROM person_materials WHERE round_id=? AND source_at=?", (rr["id"], source.when)
        )
        assert not db.one(c, "select id from documents where url like '%BOUNDARY9'")


def test_reference_and_authorship_validation():
    assert social.location("https://not-x.com/p/status/12345", post=True) is None
    assert social.location("https://twitter.com/@Example") == ("x", "example", "https://x.com/example")
    assert social.location(XHS + "?xsec_token=example", post=True)[2] == XHS
    assert social.absolute_time("2026-11-25") is None
    assert social.absolute_time("2026-11-25T11:15:00+08:00") == "2026-11-25T03:15:00Z"

    class Fake:
        def download(self, u):
            bad = {"url": XPOST, "author_url": "https://x.com/impostor", "html": "<p>公开帖</p>"}
            return feeds.Resource(json.dumps(bad).encode(), u, "application/json")

    with pytest.raises(ValueError, match="作者"):
        social.read_post(Fake(), XPOST, "x")
    body = '<h1>Evan You</h1><p>I am the creator of Vue.js. You can follow me on <a href="https://x.com/evanyou">X</a>.</p>'
    res = feeds.Resource(("<body>" + body + "</body>").encode(), "https://person.example.com", "text/html")
    assert social.profile_links(res, ["另一位"]) == []
    assert social.profile_links(res, ["Evan You"])[0][0][1] == "evanyou"
    assert any("Evan You" in text and "creator of Vue" in text for _, text, _ in materials.article(res).parts)


def test_optional_x_timeline_verifies_public_author(monkeypatch):
    monkeypatch.setenv("EVOSIGHT_X_BEARER_TOKEN", "fixture-token")
    calls = []

    def get(path, token):
        calls.append(path)
        if "by/username" in path:
            return {"data": {"id": "123", "username": "hailan_lq", "protected": False}}
        return {
            "data": [
                {
                    "id": "21000",
                    "author_id": "123",
                    "text": "海岚市域线验收公告",
                    "created_at": "2026-11-25T13:30:00Z",
                },
                {"id": "21001", "author_id": "999", "text": "其他人", "created_at": "2026-11-25T13:30:01Z"},
            ],
            "meta": {"next_token": "another-page"},
        }

    monkeypatch.setattr(social, "x_json", get)
    posts, more = social.x_timeline(
        {"account_key": "hailan_lq"}, "2026-11-25T09:00:00Z", "2026-11-25T15:00:00Z"
    )
    assert len(posts) == 1 and posts[0].author == "hailan_lq" and more and "start_time=" in calls[1]
    monkeypatch.setattr(
        social, "x_json", lambda *_: {"data": {"id": "123", "username": "hailan_lq", "protected": True}}
    )
    with pytest.raises(ValueError):
        social.x_timeline({"account_key": "hailan_lq"}, "a", "b")
