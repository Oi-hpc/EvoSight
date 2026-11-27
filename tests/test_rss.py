"""RSS acquisition, source timestamps and conditional requests."""
from contextlib import closing, nullcontext
import sqlite3
from unittest.mock import Mock

import pytest

from evosight import feeds
from evosight.db import Store, add_event, one, rows
from evosight.worker import Worker, compact, evidence

FEED = "https://publisher.example.org/rss"


def story(slug, published=None, **extra):
    return {
        "url": f"https://publisher.example.org/{slug}",
        "title": slug,
        "summary": "摘要",
        "published_at": published,
        **extra,
    }


def test_legacy_chinese_rss_and_date_precision():
    xml = """<?xml version="1.0" encoding="GB2312"?>
    <rss version="2.0"><channel><item><guid isPermaLink="false">编号一</guid>
    <title>季度通报</title><link>https://publisher.example.org/one</link>
    <description><![CDATA[<b>原始摘要</b>]]></description><pubDate>2026/11/25</pubDate>
    </item></channel></rss>""".encode(
        "gb2312"
    )
    (article,) = feeds.parse_feed(xml, FEED)
    assert (article["title"], article["summary"], article["native_id"]) == ("季度通报", "原始摘要", "编号一")
    assert article["published_label"] == "2026/11/25" and article["published_at"] is None
    assert feeds.timestamp("2026-11-25 11:20:30") is None
    assert feeds.timestamp("2026-11-25T11:20:30+08:00") == "2026-11-25T03:20:30Z"


def test_atom_relative_link_and_update_only_signal():
    xml = b"""<feed xmlns="http://www.w3.org/2005/Atom" xml:base="https://publisher.example.org/release/">
    <entry><id>urn:release:1</id><title>Release</title><link href="v1"/>
    <updated>2026-11-25T04:02:00Z</updated><content type="html">&lt;p&gt;Change&lt;/p&gt;</content>
    </entry></feed>"""
    (item,) = feeds.parse_feed(xml, FEED)
    assert item["url"] == "https://publisher.example.org/release/v1"
    assert item["native_id"] == "urn:release:1" and item["summary"] == "Change"
    assert item["published_at"] is None and item["source_updated_at"] == "2026-11-25T04:02:00Z"
    assert feeds.intake_period(item, "2026-11-25T00:00:00Z", "2026-11-25T06:00:00Z") == "window"


@pytest.mark.parametrize(
    "data",
    [
        b"<html><body>Access page</body></html>",
        b"<rss><channel><item><title>Interrupted transfer",
        b'<!DOCTYPE rss [<!ENTITY label "test">]><rss/>',
    ],
)
def test_invalid_responses_do_not_become_an_empty_success(data):
    with pytest.raises(ValueError):
        feeds.parse_feed(data, FEED)


def test_guid_is_feed_scoped_and_identifies_moved_and_corrected_articles(tmp_path):
    store = Store(tmp_path)
    other = "https://other.example.org/feed"
    with store.connection(True) as c:
        event = add_event(c, "事件", "核对来源", [FEED, other])
        original = story("v1", native_id="42")
        first = feeds.ingest(c, event, FEED, original)
        moved = {**original, "url": "https://publisher.example.org/v2"}
        assert feeds.ingest(c, event, FEED, moved) == first
        update = {**moved, "url": "https://publisher.example.org/v3", "summary": "更正"}
        second = feeds.ingest(c, event, FEED, update, intake="backfill")
        assert feeds.ingest(c, event, FEED, update) == second
        unrelated = feeds.ingest(c, event, other, story("unrelated", native_id="42"))
        assert len({first, second, unrelated}) == 3
        assert one(c, "SELECT supersedes FROM documents WHERE id=?", (second,))["supersedes"] == first
        assert one(c, "SELECT supersedes FROM documents WHERE id=?", (unrelated,))["supersedes"] is None
        assert one(c, "SELECT intake FROM event_docs WHERE document_id=?", (second,))["intake"] == "revision"
        assert len(rows(c, "SELECT id FROM nodes WHERE kind='correction'")) == 1


def test_capture_full_feed_then_apply_source_watermark_and_keep_late_evidence(tmp_path, monkeypatch):
    store = Store(tmp_path)
    with store.connection(True) as c:
        event = add_event(c, "事件", "时间窗口", [FEED])
    now = ["2030-01-01T06:00:00Z"]
    monkeypatch.setattr("evosight.worker.utc", lambda: now[0])
    baseline = [story(f"archive-{i:02}", f"2029-12-{i:02}T12:00:00Z") for i in range(1, 31)]
    pages = [
        feeds.FeedPage(baseline, FEED, '"one"'),
        ValueError("HTTP 503"),
        feeds.FeedPage(
            [
                story("in-window", "2030-01-01T08:00:00Z"),
                story("old", "2029-10-01T08:00:00Z"),
                story("day", published_label="2030-01-01"),
                story("ahead", "2030-01-01T13:00:00Z"),
                story("updated", source_updated_at="2030-01-01T09:00:00Z"),
            ],
            FEED,
            '"two"',
        ),
        feeds.FeedPage([], FEED, '"two"', unchanged=True),
    ]
    caches = []

    def poll(source, cached):
        caches.append(cached["etag"] if cached else None)
        response = pages.pop(0)
        if isinstance(response, Exception):
            raise response
        return response

    worker = Worker(store)
    worker.poll = poll
    assert worker.pull(event, [FEED])[0] == 1
    with store.connection(True) as c:
        assert c.execute("SELECT count(*) FROM event_docs").fetchone()[0] == 30
        assert len(evidence(c, event, pending=True, limit=100)) == 16
        c.execute("UPDATE event_docs SET analyzed=1 WHERE intake='initial'")
    now[0] = "2030-01-01T09:30:00Z"
    assert worker.pull(event, [FEED])[0] == 0
    with store.connection() as c:
        assert one(c, "SELECT last_success FROM source_state")["last_success"] == "2030-01-01T06:00:00Z"
    now[0] = "2030-01-01T12:00:00Z"
    assert worker.pull(event, [FEED])[0] == 1
    with store.connection() as c:
        assert {d["title"] for d in evidence(c, event, pending=True)} == {"in-window", "updated"}
        scopes = {
            r["title"]: r["intake"]
            for r in rows(c, "SELECT title,intake FROM documents JOIN event_docs ON document_id=id")
        }
        assert (scopes["old"], scopes["day"], scopes["ahead"]) == ("backfill", "undated", "future")
    now[0] = "2030-01-01T14:00:00Z"
    worker.pull(event, [FEED])
    assert caches == [None, '"one"', '"one"', '"two"']
    with store.connection() as c:
        assert "ahead" in {d["title"] for d in evidence(c, event, pending=True)}
        assert c.execute("SELECT count(*) FROM runs").fetchone()[0] == 0


def test_http_validators_follow_the_resource_not_the_redirect(monkeypatch):
    seen = []
    responses = iter([(302, {"Location": "https://cdn.example.org/news"}), (304, {"ETag": '"tag2"'})])

    class Connection:
        def __init__(self, host, *args, **kwargs):
            self.host = host

        def request(self, method, path, headers):
            seen.append((self.host, path, headers))

        def getresponse(self):
            status, headers = next(responses)
            return Mock(status=status, getheader=headers.get)

        def close(self):
            pass

    monkeypatch.setattr(feeds.socket, "getaddrinfo", lambda *a, **kw: [(0, 0, 0, "", ("93.184.216.34", 443))])
    monkeypatch.setattr(feeds.socket, "create_connection", lambda *a, **kw: nullcontext(object()))
    monkeypatch.setattr(feeds.ssl, "create_default_context", lambda: Mock())
    monkeypatch.setattr(feeds.http.client, "HTTPSConnection", Connection)
    cache = {
        "resource": "https://cdn.example.org/news",
        "etag": '"tag1"',
        "modified": "Wed, 25 Nov 2026 00:00:00 GMT",
    }
    result = feeds.poll(FEED, cache)
    assert "If-None-Match" not in seen[0][2]
    assert seen[1][2]["If-None-Match"] == '"tag1"' and seen[1][2]["If-Modified-Since"] == cache["modified"]
    assert result.unchanged and result.etag == '"tag2"' and result.modified == cache["modified"]


def test_preexisting_database_keeps_original_dates_and_reading_eligibility(tmp_path):
    with closing(sqlite3.connect(tmp_path / "evosight.sqlite3", isolation_level=None)) as c:
        c.executescript(
            """
            CREATE TABLE documents(id INTEGER PRIMARY KEY,url TEXT,digest TEXT,title TEXT,summary TEXT,
                                   published_at TEXT,observed_at TEXT,supersedes INTEGER,UNIQUE(url,digest));
            CREATE TABLE event_docs(event_id INTEGER,document_id INTEGER,analyzed INTEGER DEFAULT 0,
                                    PRIMARY KEY(event_id,document_id));
            INSERT INTO documents VALUES(1,'https://example.org/old','hash','旧记录','','2025-01-01T00:00:00Z','2025-01-05T00:00:00Z',NULL);
            INSERT INTO event_docs VALUES(1,1,0);
        """
        )
    store = Store(tmp_path)
    with store.connection() as c:
        (record,) = evidence(c, 1, pending=True)
        assert record["published_at"] == "2025-01-01T00:00:00Z"
        assert record["source_updated_at"] is None and record["published_label"] is None
        assert "published_at" not in compact([record])[0]
        assert one(c, "SELECT intake FROM event_docs")["intake"] == "initial"


def test_legacy_clock_needs_confirmation_from_original_feed(tmp_path):
    store = Store(tmp_path)
    date = "2026-11-25T00:00:00Z"
    with store.connection(True) as c:
        event = add_event(c, "旧事件", "复核旧库的时间字段", [FEED])
        item = story("release", date)
        ident = feeds.ingest(c, event, FEED, item)
        c.execute("UPDATE documents SET source_times_checked=0 WHERE id=?", (ident,))
        assert (
            feeds.ingest(c, event, FEED, {**item, "published_at": None, "source_updated_at": date}) == ident
        )
        assert "published_at" not in compact(evidence(c, event))[0]
        feeds.ingest(c, event, "https://mirror.example.org/rss", item)
        assert "published_at" not in compact(evidence(c, event))[0]
        assert feeds.ingest(c, event, FEED, item) == ident
        assert compact(evidence(c, event))[0]["published_at"] == date
