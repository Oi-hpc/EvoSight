"""Public RSS/Atom retrieval and evidence versioning."""
import gzip
import hashlib
import http.client
import io
import ipaddress
import json
import re
import socket
import ssl
from dataclasses import dataclass
from datetime import datetime
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

import feedparser

from .db import node, one, rows, utc

LIMIT = 8 * 1024 * 1024


def public_url(value, https_only=True):
    value = str(value).strip()
    if len(value) > 2048 or any(ch.isspace() or ord(ch) < 32 for ch in value) or "\\" in value:
        raise ValueError("无效的来源链接")
    u = urlsplit(value)
    if (
        u.scheme not in (("https",) if https_only else ("http", "https"))
        or not u.hostname
        or u.username is not None
        or u.password is not None
        or u.port not in (None, 80, 443)
    ):
        raise ValueError("请填写公开的 HTTPS 来源地址")
    host = u.hostname.rstrip(".").lower()
    if "." not in host or host.endswith((".local", ".localhost", ".internal")):
        raise ValueError("仅支持公开来源地址")
    try:
        address = ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        address = None
    if address is not None and not address.is_global:
        raise ValueError("仅支持公开来源地址")
    return urlunsplit((u.scheme, u.netloc, u.path or "/", u.query, ""))


@dataclass
class FeedPage:
    entries: list[dict]
    resource: str
    etag: str | None = None
    modified: str | None = None
    unchanged: bool = False
    original: "Resource | None" = None


@dataclass
class Resource:
    body: bytes
    url: str
    mime: str
    etag: str | None = None
    modified: str | None = None
    unchanged: bool = False


def _validator(value):
    if value and len(value) <= 1024 and all(32 <= ord(ch) <= 255 and ord(ch) != 127 for ch in value):
        return value
    return None


def download(url, cached=None, *, accept="*/*"):
    """Fetch bounded bytes over public HTTPS, including every redirect."""
    for _ in range(5):
        url = public_url(url)
        u = urlsplit(url)
        headers = {
            "User-Agent": "Mozilla/5.0 (compatible; EvoSightRSS/0.2)",
            "Accept": accept,
            "Accept-Encoding": "identity",
        }
        conditional = cached and cached["resource"] == url and (cached["etag"] or cached["modified"])
        if conditional:
            if cached["etag"]:
                headers["If-None-Match"] = cached["etag"]
            if cached["modified"]:
                headers["If-Modified-Since"] = cached["modified"]
        host = u.hostname.encode("idna").decode()
        port = u.port or 443
        addresses = list(
            dict.fromkeys(v[4][0] for v in socket.getaddrinfo(host, port, type=socket.SOCK_STREAM))
        )
        if not addresses or any(not ipaddress.ip_address(v).is_global for v in addresses):
            raise ValueError("来源解析到了非公开地址")
        last_error = None
        for address in addresses:
            context = ssl.create_default_context()
            conn = http.client.HTTPSConnection(host, port, timeout=12, context=context)
            try:
                # Connect to the validated IP, keeping the original TLS name and Host header.
                with socket.create_connection((address, port), timeout=10) as raw:
                    conn.sock = context.wrap_socket(raw, server_hostname=host)
                    path = urlunsplit(("", "", u.path or "/", u.query, ""))
                    conn.request("GET", path, headers=headers)
                    response = conn.getresponse()
                    if response.status in (301, 302, 303, 307, 308):
                        location = response.getheader("Location")
                        if not location:
                            raise ValueError("来源重定向缺少地址")
                        url = urljoin(url, location)
                        break
                    etag = _validator(response.getheader("ETag"))
                    modified = _validator(response.getheader("Last-Modified"))
                    if response.status == 304 and conditional:
                        return Resource(
                            b"", url, "", etag or cached["etag"], modified or cached["modified"], True
                        )
                    if response.status != 200:
                        raise ValueError(f"来源返回 HTTP {response.status}")
                    data = response.read(LIMIT + 1)
                    if len(data) > LIMIT:
                        raise ValueError("来源超过 8 MiB")
                    if response.getheader("Content-Encoding", "").lower() == "gzip":
                        data = gzip.GzipFile(fileobj=io.BytesIO(data)).read(LIMIT + 1)
                    if len(data) > LIMIT:
                        raise ValueError("来源超过 8 MiB")
                    return Resource(data, url, response.getheader("Content-Type") or "", etag, modified)
            except (OSError, http.client.HTTPException) as err:
                last_error = err
            finally:
                conn.close()
        else:
            raise ValueError("无法连接公开来源") from last_error
    raise ValueError("来源重定向次数过多")


def poll(url, cached=None):
    result = download(
        url,
        cached,
        accept="application/atom+xml, application/rss+xml, application/xml, text/xml;q=0.9, */*;q=0.5",
    )
    entries = [] if result.unchanged else parse_feed(result.body, result.url, result.mime)
    return FeedPage(entries, result.url, result.etag, result.modified, result.unchanged)


def fetch(url):
    return poll(url).entries


class Plain(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts = []
        self.skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self.skip += 1

    def handle_endtag(self, tag):
        if tag in ("script", "style"):
            self.skip = max(0, self.skip - 1)

    def handle_data(self, data):
        if not self.skip:
            self.parts.append(data)


def clean(value, length):
    parser = Plain()
    parser.feed(value)
    return " ".join(" ".join(parser.parts).split())[:length]


def canonical(url):
    u = urlsplit(url)
    query = [
        (k, v)
        for k, v in parse_qsl(u.query, keep_blank_values=True)
        if not k.lower().startswith("utm_") and k.lower() not in {"fbclid", "gclid"}
    ]
    return urlunsplit((u.scheme.lower(), u.netloc.lower(), u.path, urlencode(query), ""))


def timestamp(raw):
    """A date without a clock or time zone cannot determine a scoring cutoff."""
    if not raw or ":" not in raw:
        return None
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        try:
            dt = parsedate_to_datetime(raw)
        except (ValueError, TypeError, OverflowError):
            return None
    try:
        return utc(dt) if dt.tzinfo is not None else None
    except (OverflowError, ValueError):
        return None


def parse_feed(data, base, content_type=None):
    if len(data) > LIMIT or re.search(rb"<!\s*(?:DOCTYPE|ENTITY)\b", data.replace(b"\x00", b""), re.I):
        raise ValueError("订阅超出大小限制或含有不支持的 XML 声明")
    # Feedparser handles legacy Chinese encodings, RSS namespaces and Atom xml:base.
    # Passing a stream keeps downloading and redirect validation in our own transport.
    parsed = feedparser.parse(
        io.BytesIO(data),
        response_headers={"content-type": content_type} if content_type else {},
    )
    if not parsed.version.startswith(("rss", "atom")):
        raise ValueError("响应不是 RSS 或 Atom 订阅")
    if parsed.bozo and not isinstance(
        parsed.bozo_exception, (feedparser.CharacterEncodingOverride, feedparser.NonXMLContentType)
    ):
        raise ValueError("订阅解析失败，本轮不推进采集进度")
    result = []
    for entry in parsed.entries:
        link = entry.get("link")
        if not link:
            continue
        try:
            url = canonical(public_url(urljoin(base, link), False))
        except ValueError:
            continue
        title = clean(entry.get("title", ""), 400)
        if not title:
            continue
        content = next((part["value"] for part in entry.get("content", []) if part.get("value")), "")
        # Read actual keys. FeedParserDict aliases a missing 'updated' to 'published'.
        reported = dict.get(entry, "published", "")
        update = dict.get(entry, "updated", "")
        article = {
            "url": url,
            "title": title,
            "summary": clean(content or entry.get("summary", ""), 6000),
            "published_at": timestamp(reported),
        }
        if reported and not article["published_at"]:
            article["published_label"] = reported[:120]
        if stamp := timestamp(update):
            article["source_updated_at"] = stamp
        if native := entry.get("id"):
            article["native_id"] = native
        result.append(article)
    return sorted(
        result, key=lambda a: max(a["published_at"] or "", a.get("source_updated_at", "")), reverse=True
    )


def intake_period(entry, since, until):
    """Classify the publisher signal. Occurrence time requires separate evidence."""
    signal = max(entry.get("published_at") or "", entry.get("source_updated_at") or "")
    only_day = entry.get("published_label", "")
    if not signal and entry.get("distinct_native") and re.fullmatch(r"\d{4}-\d{2}-\d{2}", only_day):
        if only_day >= until[:10]:
            return "future"
        return "initial" if not since else "backfill"
    if signal > until:
        return "future"
    if not since:
        return "initial"
    if not signal:
        return "undated"
    return "window" if signal >= since else "backfill"


def release_future(c, event, until):
    stamp = "max(coalesce(published_at,''),coalesce(source_updated_at,''))"
    c.execute(
        f"""UPDATE event_docs SET intake='window' WHERE event_id=? AND intake='future'
                  AND document_id IN (SELECT id FROM documents WHERE {stamp}>'' AND {stamp}<=?)""",
        (event, until),
    )
    c.execute(
        f"""UPDATE event_docs SET intake='backfill' WHERE event_id=? AND intake='future'
                  AND document_id IN (SELECT id FROM documents WHERE {stamp}='' AND published_label GLOB '????-??-??'
                  AND published_label<substr(?,1,10))""",
        (event, until),
    )


def reconcile_section(c, feed):
    """Keep previously saved listing cards and correct their relation to an identical article."""
    duplicates = rows(
        c,
        """SELECT teaser.id,original.id AS article,teaser.url FROM rss_entries x
        JOIN documents teaser ON teaser.id=x.document_id JOIN documents original ON original.id=teaser.supersedes
        WHERE x.feed=? AND teaser.summary='' AND original.summary!=''
        AND teaser.title=original.title AND teaser.url=original.url AND EXISTS (
          SELECT 1 FROM platform_entries pe JOIN platforms p ON p.url=pe.platform
          WHERE pe.document_id=original.id AND p.kind='page')""",
        (feed,),
    )
    for duplicate in duplicates:
        teaser, article = duplicate["id"], duplicate["article"]
        c.execute("UPDATE documents SET supersedes=NULL WHERE id=? AND supersedes=?", (teaser, article))
        c.execute("INSERT OR IGNORE INTO mentions VALUES(?,?,?)", (article, feed, duplicate["url"]))
        c.execute(
            "UPDATE rss_entries SET document_id=? WHERE feed=? AND document_id=?", (article, feed, teaser)
        )
        c.execute(
            "UPDATE event_docs SET intake='archive' WHERE document_id=? AND intake='revision'", (teaser,)
        )
        c.execute(
            """UPDATE nodes SET kind='historical',title='发布方栏目收录了已有原文'
           WHERE kind='correction' AND title='来源修订了已收录材料' AND json_array_length(refs)=2
           AND json_extract(refs,'$[0]')=? AND json_extract(refs,'$[1]')=?""",
            (article, teaser),
        )


def ingest(c, event, feed, entry, *, intake="initial"):
    url = canonical(entry["url"])
    comparable = re.sub(r"\s+", " ", entry["title"] + "\n" + entry["summary"]).casefold().strip()
    digest = hashlib.sha256(comparable.encode()).hexdigest()
    key = hashlib.sha256((entry.get("native_id") or url).encode()).hexdigest()
    prior = one(
        c,
        "SELECT d.* FROM rss_entries r JOIN documents d ON d.id=r.document_id WHERE r.feed=? AND r.entry_key=?",
        (feed, key),
    )
    # A price table has one stable native key per trading day, all at the same public URL.
    if not prior and not entry.get("distinct_native"):
        prior = one(c, "SELECT * FROM documents WHERE url=? ORDER BY id DESC LIMIT 1", (url,))
    if (
        prior
        and prior["summary"] == ""
        and entry["summary"]
        and prior["title"] == entry["title"]
        and one(
            c,
            """SELECT 1 FROM platform_entries pe JOIN platforms p ON p.url=pe.platform
           WHERE pe.document_id=? AND p.kind='section'""",
            (prior["id"],),
        )
    ):
        # An article first seen as a section headline is being retrieved in full.
        prior = None
    doc = (
        prior
        if prior and prior["digest"] == digest
        else one(c, "SELECT * FROM documents WHERE url=? AND digest=?", (url, digest))
    )
    if not doc and not prior and not entry.get("distinct_native"):
        doc = one(c, "SELECT * FROM documents WHERE digest=? ORDER BY id LIMIT 1", (digest,))
    if entry.get("listing_only") and not entry["summary"]:
        full = one(
            c,
            """SELECT d.* FROM documents d WHERE d.url=? AND d.title=? AND d.summary!='' AND EXISTS
                 (SELECT 1 FROM platform_entries pe JOIN platforms p ON p.url=pe.platform
                  WHERE pe.document_id=d.id AND p.kind='page') ORDER BY d.id DESC LIMIT 1""",
            (url, entry["title"]),
        )
        if full:
            doc = full
    new_version = prior and prior["digest"] != digest and not doc and not entry.get("listing_only")
    if doc:
        ident = doc["id"]
        if (
            not doc["source_times_checked"]
            and url == doc["url"]
            and entry["published_at"] == doc["published_at"]
            and one(c, "SELECT 1 FROM mentions WHERE document_id=? AND feed=? AND url=?", (ident, feed, url))
        ):
            c.execute(
                "UPDATE documents SET source_times_checked=1,source_updated_at=?,published_label=? WHERE id=?",
                (entry.get("source_updated_at"), entry.get("published_label"), ident),
            )
    else:
        ident = c.execute(
            "INSERT INTO documents(url,digest,title,summary,published_at,observed_at,supersedes,source_updated_at,published_label,source_times_checked) VALUES(?,?,?,?,?,?,?,?,?,1)",
            (
                url,
                digest,
                entry["title"],
                entry["summary"],
                entry["published_at"],
                utc(),
                prior["id"] if new_version else None,
                entry.get("source_updated_at"),
                entry.get("published_label"),
            ),
        ).lastrowid
    c.execute(
        """INSERT INTO rss_entries(feed,entry_key,document_id) VALUES(?,?,?)
                 ON CONFLICT(feed,entry_key) DO UPDATE SET document_id=excluded.document_id
                 WHERE document_id!=excluded.document_id""",
        (feed, key, ident),
    )
    c.execute("INSERT OR IGNORE INTO mentions VALUES(?,?,?)", (ident, feed, url))
    if new_version and intake != "future":
        intake = "revision"
    if event is not None:
        c.execute(
            "INSERT OR IGNORE INTO event_docs(event_id,document_id,intake) VALUES(?,?,?)",
            (event, ident, intake),
        )
    if new_version:
        for affected in rows(c, "SELECT event_id FROM event_docs WHERE document_id=?", (prior["id"],)):
            target = affected["event_id"]
            c.execute(
                "INSERT OR IGNORE INTO event_docs(event_id,document_id,intake) VALUES(?,?,?)",
                (target, ident, intake),
            )
            node(c, target, "correction", "来源修订了已收录材料", entry["title"], refs=[prior["id"], ident])
            for r in rows(
                c,
                "SELECT p.id,r.refs FROM predictions p JOIN resolutions r ON r.id=(SELECT max(id) FROM resolutions WHERE prediction_id=p.id) WHERE p.event_id=? AND p.status IN ('resolved','unverifiable')",
                (target,),
            ):
                if prior["id"] in json.loads(r["refs"]):
                    c.execute("UPDATE predictions SET status='review' WHERE id=?", (r["id"],))
    return ident
