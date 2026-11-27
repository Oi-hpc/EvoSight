from datetime import timedelta
import json

import pytest

from evosight import agents, db, feeds, market_sources, materials, research, watch
from test_workflow import HOST, clock, new_event, setup


STOCK = "https://stockanalysis.com/stocks/hr/history/"
YAHOO = "https://finance.yahoo.com/quote/HR/history/"
SEC = "https://data.sec.gov/submissions/CIK0000000111.json"


def history(url=STOCK, last_close="21.25", *, third_day=False, issuer="Harbor Rail"):
    yahoo = url == YAHOO
    heading = f"{issuer} (HR)" if yahoo else f"{issuer} Stock Price History"
    exchange = (
        "<span>NasdaqGS - Nasdaq Real Time Price</span> • USD"
        if yahoo
        else "NASDAQ: HR · Real-Time Price · USD"
    )
    label = "Close Close price adjusted for splits." if yahoo else "Close"
    day = "26" if third_day else "25"
    stamp = f"November {day} at 4:00:02 PM EST" if yahoo else f"Nov {day}, 2026, 4:00 PM EST"
    sessions = [
        ("Nov 25, 2026", "21", "22", "19", last_close, "1,050"),
        ("Nov 24, 2026", "20", "22", "18", "20.50", "900"),
        ("Nov 23, 2026", "5", "22", "18", "20", "800"),
    ]
    if third_day:
        sessions.insert(0, ("Nov 26, 2026", "20", "22", "19", "21.10", "700"))
    cells = "".join("<tr>" + "".join(f"<td>{x}</td>" for x in row) + "</tr>" for row in sessions)
    headers = "".join(f"<th>{x}</th>" for x in ("Date", "Open", "High", "Low", label, "Volume"))
    content = f"<html><h1>{heading}</h1><div>{exchange}</div><div><span>At close:</span> <span>{stamp}</span></div><table><tr>{headers}</tr>{cells}</table></html>"
    return feeds.Resource(content.encode(), url, "text/html")


@pytest.mark.parametrize("addr,stamp", [(STOCK, "21:00:00Z"), (YAHOO, "21:00:02Z")])
def test_quote_records_keep_source_dates_and_originals(tmp_path, addr, stamp):
    store = db.Store(tmp_path)
    resource = history(addr)
    acquisition = materials.Acquisition(download=lambda *args: resource)
    source = materials.Source(addr, "daily_prices", "Harbor Rail", "按日收盘", "目录匹配")
    watcher = watch.Watcher(store, acquisition)
    with store.connection(True) as c:
        c.execute(
            "INSERT INTO platforms(url,kind,name,next_poll) VALUES(?,?,?,?)",
            (addr, source.kind, source.name, db.utc()),
        )
    quote = acquisition.poll(source).entries
    assert [q["published_label"] for q in quote] == ["2026-11-25", "2026-11-24"]
    assert quote[0]["source_updated_at"] == "2026-11-25T" + stamp and quote[1]["source_updated_at"] is None
    assert all(q["published_at"] is None for q in quote)
    assert watcher.poll(source, fresh=True) and watcher.poll(source, fresh=True)
    with store.connection() as c:
        assert c.execute("SELECT count(*),count(supersedes) FROM documents").fetchone()[:] == (2, 0)
        assert (
            c.execute(
                "SELECT count(*) FROM evidence_assets WHERE extractor='publisher-record-v1'"
            ).fetchone()[0]
            == 2
        )
        assert c.execute("SELECT count(*) FROM evidence_blobs").fetchone()[0] == 1
        assert c.execute("SELECT content FROM evidence_blobs").fetchone()[0] == resource.body
        document = db.one(c, "SELECT * FROM documents ORDER BY id DESC LIMIT 1")
    materials.enrich(store, acquisition, document)
    with store.connection(True) as c:
        assert c.execute("SELECT count(*) FROM evidence_assets WHERE extractor='feed-v1'").fetchone()[0] == 0
        materials.summary_fragment(c, document)
        fragments = materials.fragments_for(c, [document["id"]])
        assert len(fragments) == 1 and fragments[0]["kind"] == "daily_prices" and fragments[0]["link"] == addr
    resource = history(addr, third_day=True)
    assert watcher.poll(source, fresh=True)
    with store.connection() as c:
        assert c.execute("SELECT count(*),count(supersedes) FROM documents").fetchone()[:] == (3, 0)
    resource = history(addr, last_close="21.30", third_day=True)
    assert watcher.poll(source, fresh=True)
    with store.connection() as c:
        revised = c.execute(
            "SELECT d.published_label,p.published_label FROM documents d JOIN documents p ON p.id=d.supersedes"
        ).fetchall()
        assert [tuple(x) for x in revised] == [("2026-11-25", "2026-11-25")]


def test_date_only_quote_waits_until_day_has_passed(tmp_path):
    entries = market_sources.daily_prices(history(), materials.tree_for(history()))
    date_only = entries[1]
    assert feeds.intake_period(date_only, None, "2026-11-24T06:00:00Z") == "future"
    assert feeds.intake_period(entries[0], None, "2026-11-25T06:00:00Z") == "future"
    assert feeds.intake_period(date_only, "2026-11-24T06:00:00Z", "2026-11-25T06:00:00Z") == "backfill"
    store = db.Store(tmp_path)
    eid = new_event(store)["id"]
    with store.connection(True) as c:
        d = feeds.ingest(c, eid, STOCK, date_only, intake="future")
        feeds.release_future(c, eid, "2026-11-24T23:00:00Z")
        assert c.execute("SELECT intake FROM event_docs WHERE document_id=?", (d,)).fetchone()[0] == "future"
        feeds.release_future(c, eid, "2026-11-25T06:00:00Z")
        assert (
            c.execute("SELECT intake FROM event_docs WHERE document_id=?", (d,)).fetchone()[0] == "backfill"
        )


def test_filings_use_verified_cik_and_original_document():
    accession = "0000000111-26-000001"
    index = {
        "cik": "111",
        "name": "Harbor Rail Corp",
        "tickers": ["HR"],
        "filings": {
            "recent": {
                "form": ["8-K", "4"],
                "filingDate": ["2026-11-25"] * 2,
                "acceptanceDateTime": ["2026-11-25T21:10:29Z"] * 2,
                "accessionNumber": [accession, "0000000111-26-000002"],
                "primaryDocument": ["hr-20261125.htm", "../unrelated.htm"],
            }
        },
    }

    def response():
        return feeds.Resource(json.dumps(index).encode(), SEC, "application/json")

    records = market_sources.filings(response())
    assert len(records) == 1
    assert records[0]["published_at"] == "2026-11-25T21:10:29Z"
    assert (
        records[0]["url"] == "https://www.sec.gov/Archives/edgar/data/111/000000011126000001/hr-20261125.htm"
    )
    assert accession in records[0]["evidence_locator"]
    index["cik"] = "222"
    with pytest.raises(ValueError, match="发行人"):
        market_sources.filings(response())


@pytest.mark.parametrize("available", [True, False])
def test_empty_search_still_runs_source_scout_and_requires_real_evidence(tmp_path, clock, available):
    store, acq, replay, worker = setup(tmp_path, ["news"])
    event = new_event(store)
    with store.connection() as c:
        r = db.require(c, "tracking_rounds", event["round"])

    def offline(*args):
        raise OSError("search unavailable")

    acq.search = offline
    previous = acq.download
    unrelated = "https://unrelated.example.org/report"

    def download(url, *args):
        if url == unrelated:
            return feeds.Resource(
                b"<html><article><h1>Garden paths</h1><p>Choosing paving stones for a garden.</p></article></html>",
                url,
                "text/html",
            )
        return previous(url, *args)

    acq.download = download
    model_call = replay.call

    def run(role, context):
        if role == "source_scout":
            replay.calls[role] += 1
            assert context["previous_attempts"] and context["title"]
            assert context["window"] == [r["window_start"], r["window_end"]]
            return agents.SourceScout(
                publisher_urls=([HOST + "/notice"] if available else []) + [unrelated], queries=[]
            ), (3, 2)
        return model_call(role, context)

    replay.call = run
    assert worker.step()
    with store.connection() as c:
        chosen = [
            x[0]
            for x in c.execute(
                "SELECT url FROM event_platforms WHERE event_id=? AND enabled=1", (event["id"],)
            )
        ]
        assert unrelated not in chosen
        log = " ".join(
            x[0] for x in c.execute("SELECT reason FROM discovery_log WHERE round_id=?", (event["round"],))
        )
        assert "搜索服务暂不可用" in log and "未匹配" in log and replay.calls["source_scout"] == 1
        count = c.execute("SELECT count(*) FROM event_docs WHERE event_id=?", (event["id"],)).fetchone()[0]
        state = c.execute("SELECT state,detail FROM tracking_rounds WHERE id=?", (event["round"],)).fetchone()
        if available:
            assert chosen == [HOST + "/notice"] and count and replay.calls["news"] and state[0] == "published"
        else:
            assert not chosen and count == 0 and state[0] == "waiting" and "未取得可核实" in state[1]


def test_program_search_can_use_the_deployed_deepseek_provider(monkeypatch):
    import json
    from types import SimpleNamespace

    monkeypatch.setenv("EVOSIGHT_PUBLIC_SEARCH", "deepseek")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "fixture-key")
    request = []

    class SearchService:
        def __init__(self, address, timeout):
            assert address == "api.deepseek.com" and timeout == 10

        def request(self, method, path, payload, headers):
            request.append((method, path, json.loads(payload), headers))

        def getresponse(self):
            return SimpleNamespace(status=200, read=lambda _: b'{"results":[{"url":"https://example.org/news","title":"News"}]}')

        def close(self):
            pass

    monkeypatch.setattr(materials, "HTTPSConnection", SearchService)
    assert materials.search_web("项" * 150, 4) == [("News", "https://example.org/news")]
    method, path, payload, headers = request[0]
    assert (method, path) == ("POST", "/v1/tools/search")
    assert payload == {"query": "项" * 128, "count": 4, "need_content": False}
    assert headers["Authorization"] == "Bearer fixture-key"


def test_planned_news_search_excludes_publications_after_round_cutoff(tmp_path, clock):
    store, acq, _, worker = setup(tmp_path, ["news"])
    event = new_event(store)
    plan = agents.Plan.model_validate(
        {
            "rationale": "核对本轮公告",
            "tasks": [
                {
                    "role": "news",
                    "question": "有哪些新公告？",
                    "target": "海岚市域线",
                    "time_scope": "window",
                    "done_when": "核对发布方原文",
                    "priority": 2,
                    "search_query": "海岚市域线 公告",
                }
            ],
        }
    )
    with store.connection(True) as c:
        r = db.require(c, "tracking_rounds", event["round"])
        research.save_plan(c, r, plan)
        task = db.one(c, "SELECT * FROM research_tasks WHERE round_id=?", (r["id"],))

    def article(slug, published):
        url = HOST + "/" + slug
        return (
            {"url": url, "title": slug, "summary": slug, "published_at": published},
            feeds.Resource(slug.encode(), url, "text/html"),
            materials.Extract("html", [("paragraph", slug, url)], published_at=published),
        )

    cutoff = r["window_end"]
    acq.targeted = lambda _: [
        article("after-cutoff", clock.utc(clock.dt + timedelta(seconds=1))),
        article("at-cutoff", cutoff),
    ]
    docs = research.prepare_task(worker, r, task, plan.tasks[0].model_dump(), {}, [])
    with store.connection() as c:
        assert [row[0] for row in c.execute("SELECT url FROM documents")] == [HOST + "/at-cutoff"]
        fragments = materials.fragments_for(c, docs)
        assert research.model_fragments(fragments)[0]["published_at"] == cutoff
        assert "published_at" not in research.model_fragments([{**fragments[0], "source_times_checked": 0}])[0]


def test_catalog_matches_the_issuer_and_goal():
    financial = materials.catalog("Anthropic股票追踪", "涨跌")
    assert len(financial) == 5 and sum(x.kind == "daily_prices" for x in financial) == 2
    assert [s.kind for s in materials.catalog("Anthropic", "模型发布")] == ["section"]
    assert not materials.catalog("Misanthropic", "stock") and not materials.catalog("ANT", "股价")
    other = materials.Source(STOCK, "daily_prices", "Anthropic 历史日线", "交易记录", "目录匹配")
    with pytest.raises(ValueError, match="发行人"):
        materials.Acquisition(download=lambda *_: history()).poll(other)


def test_readable_but_irrelevant_subscription_does_not_block_first_research(tmp_path, clock):
    store, acq, replay, worker = setup(tmp_path, ["news"])
    event = new_event(store)
    feed = HOST + "/general.xml"
    acq.discover = lambda *_args, **_kwargs: (
        [materials.Source(feed, "feed", "General bulletin", "广泛覆盖新闻", "目录匹配")],
        [],
    )
    original = acq.download

    def download(url, *args):
        if url == feed:
            xml = f"<rss version='2.0'><channel><item><title>Garden plants</title><description>Identifying roses</description><link>{HOST}/roses</link></item></channel></rss>"
            return feeds.Resource(xml.encode(), url, "application/rss+xml")
        return original(url, *args)

    acq.download = download
    assert worker.step()
    with store.connection() as c:
        links = [
            r[0]
            for r in c.execute(
                "SELECT url FROM documents d JOIN event_docs ed ON ed.document_id=d.id WHERE ed.event_id=?",
                (event["id"],),
            )
        ]
        assert HOST + "/notice" in links and HOST + "/roses" not in links
        assert replay.calls["source_scout"] == 1 and replay.calls["news"] == 1
        assert (
            c.execute(
                "SELECT count(*) FROM round_sources WHERE round_id=? AND state='covered'", (event["round"],)
            ).fetchone()[0]
            == 2
        )


@pytest.mark.parametrize("section_first", [False, True])
def test_section_and_article_do_not_report_a_publisher_correction(tmp_path, section_first):
    store = db.Store(tmp_path)
    url = HOST + "/notice"
    listing = HOST + "/news"
    article = {
        "url": url,
        "title": "Harbor railway opens",
        "summary": "The authority announced a new public service.",
        "published_at": "2026-11-25T12:00:00Z",
    }
    teaser = {**article, "summary": "", "published_at": None, "listing_only": True}
    with store.connection(True) as c:
        e = db.add_event(c, "Harbor railway", "Public opening", [])
        c.executemany(
            "INSERT INTO platforms(url,kind,name,next_poll) VALUES(?,?,?,?)",
            [(url, "page", "Publisher", db.utc()), (listing, "section", "News", db.utc())],
        )
        streams = [(url, article), (listing, teaser)]
        if section_first:
            streams.reverse()
        for stream, item in streams:
            d = feeds.ingest(c, e, stream, item)
            c.execute(
                "INSERT OR IGNORE INTO platform_entries(platform,document_id,data,seen_at) VALUES(?,?,?,?)",
                (stream, d, db.dump(item), db.utc()),
            )
        assert not c.execute("SELECT 1 FROM nodes WHERE kind='correction'").fetchone()
        assert not c.execute("SELECT 1 FROM documents WHERE supersedes IS NOT NULL").fetchone()
        full = c.execute("SELECT id FROM documents WHERE summary!=''").fetchone()[0]
        assert feeds.ingest(c, e, listing, teaser) == full
        assert c.execute("SELECT count(*) FROM mentions WHERE document_id=?", (full,)).fetchone()[0] == 2


def test_legacy_listing_receipt_is_preserved_and_reclassified(tmp_path):
    store = db.Store(tmp_path)
    listing = HOST + "/news"
    url = HOST + "/release"
    item = {
        "url": url,
        "title": "Original title",
        "summary": "Officer announces a release.",
        "published_at": None,
    }
    with store.connection(True) as c:
        e = db.add_event(c, "Harbor", "Announcements", [])
        c.executemany(
            "INSERT INTO platforms(url,kind,name,next_poll) VALUES(?,?,?,?)",
            [(url, "page", "Publisher", db.utc()), (listing, "section", "News", db.utc())],
        )
        full = feeds.ingest(c, e, url, item)
        c.execute(
            "INSERT INTO platform_entries(platform,document_id,data,seen_at) VALUES(?,?,?,?)",
            (url, full, db.dump(item), db.utc()),
        )
        # Older builds forwarded the title as an untyped RSS item and raised a correction.
        teaser = feeds.ingest(c, e, listing, {**item, "summary": ""})
        node = c.execute("SELECT id FROM nodes WHERE kind='correction'").fetchone()[0]
        feeds.reconcile_section(c, listing)
        feeds.reconcile_section(c, listing)
        assert c.execute("SELECT supersedes FROM documents WHERE id=?", (teaser,)).fetchone()[0] is None
        assert c.execute("SELECT kind,title FROM nodes WHERE id=?", (node,)).fetchone()[:] == (
            "historical",
            "发布方栏目收录了已有原文",
        )
        assert c.execute("SELECT count(*) FROM documents").fetchone()[0] == 2
        assert c.execute("SELECT count(*) FROM mentions WHERE document_id=?", (full,)).fetchone()[0] == 2
